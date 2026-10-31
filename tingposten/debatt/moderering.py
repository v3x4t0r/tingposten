"""Moderering: every comment is judged before anyone else sees it.

A moderator picks one rule from regler.REGLER for a comment, with a probability
per rule when the backend can give one. avgjor() turns that into one of three
outcomes: publiser, avvis (with the rule the reader is told), or redaktor (the
model isn't sure, so the comment waits for the editor). A Kjede asks its next
backend only when the one before is unsure or can't answer; when every backend
fails the comment waits for the editor, never published by default.

Four backends, compared with `tingposten debatt-test` before the owner picks:
  hailo   hailo-ollama on the Pi's Hailo-10H (small Qwen models). Its API has no
          log-probabilities, so it can only name a rule: kalibrert=False.
  cpu     llama-server (llama.cpp) on the Pi's CPU: the "self-hosted Jev" trick,
          one forward pass, read the probability of each rule's letter.
  jev     TypeSafe's hosted decision model (System One API, a choice question).
  claude  the Claude API with its own key (TINGPOSTEN_ANTHROPIC_API_KEY, never
          ANTHROPIC_API_KEY: that one would make `claude -p` bill the API).

Only the comment, the comment it answers and the sak's title and ingress are sent
to a backend: never the reader's e-mail, display name or address.
"""

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .. import config
from ..llm import fence
from .regler import BOKSTAVER, REGLER, REGLER_VERSJON, VEILEDNING, regeltekst

log = logging.getLogger(__name__)

UTFALL = ("publiser", "avvis", "redaktor")
MAKS_TEGN = 4000  # a comment longer than this never reaches a model (the server refuses it first)
TIMEOUT_S = 60

# The answer Claude gives as a level of certainty, as the probability avgjor() compares.
_SIKKERHET = {"høy": 0.95, "middels": 0.75, "lav": 0.5}


class ModereringFeil(RuntimeError):
    """The backend gave no usable answer (down, timed out, refused, unparseable)."""


@dataclass
class Sak:
    key: str
    tittel: str
    ingress: str = ""


@dataclass
class Vurdering:
    regel: str
    sannsynligheter: dict[str, float] | None  # None: the backend only names a rule
    modell: str
    ms: int
    begrunnelse: str = ""  # Claude's short reason; shown to the editor only, never on the site
    kostnad_usd: float = 0.0
    regler: str = REGLER_VERSJON

    @property
    def sikkerhet(self) -> float | None:
        return None if self.sannsynligheter is None else self.sannsynligheter.get(self.regel, 0.0)

    def som_dict(self) -> dict:
        return {"regel": self.regel, "sannsynligheter": self.sannsynligheter, "modell": self.modell,
                "ms": self.ms, "begrunnelse": self.begrunnelse, "kostnad_usd": self.kostnad_usd,
                "regler": self.regler}


@dataclass
class Avgjorelse:
    utfall: str  # one of UTFALL
    regel: str | None  # the rule a stopped comment broke; None when published or waiting
    vurderinger: list[Vurdering] = field(default_factory=list)
    feil: list[str] = field(default_factory=list)


def avgjor(v: Vurdering, terskel: float, stol_pa_etikett: bool = False) -> str:
    """One backend's verdict as an outcome. A rule only counts when its probability
    reaches `terskel`; a backend without probabilities is trusted only with
    `stol_pa_etikett` (set from the test results), otherwise it is 'unsure'."""
    if v.regel not in REGLER:
        return "redaktor"
    sikker = (v.sikkerhet or 0.0) >= terskel if v.sannsynligheter is not None else stol_pa_etikett
    if not sikker:
        return "redaktor"
    return "publiser" if v.regel == "ok" else "avvis"


class Moderator:
    name = "?"
    stol_pa_etikett = False

    def vurder(self, tekst: str, sak: Sak, forelder: str | None = None) -> Vurdering:
        if len(tekst) > MAKS_TEGN:
            raise ModereringFeil(f"kommentaren er over {MAKS_TEGN} tegn")
        start = time.monotonic()
        v = self._vurder(tekst, sak, forelder)
        v.ms = round((time.monotonic() - start) * 1000)
        return v

    def _vurder(self, tekst: str, sak: Sak, forelder: str | None) -> Vurdering:
        raise NotImplementedError


class Kjede:
    """Asks each moderator in turn until one is sure. `terskel` applies to all."""

    def __init__(self, ledd: list[Moderator], terskel: float):
        if not ledd:
            raise ValueError("a moderator chain needs at least one backend")
        self.ledd, self.terskel = ledd, terskel

    @property
    def name(self) -> str:
        return " → ".join(m.name for m in self.ledd)

    def avgjor(self, tekst: str, sak: Sak, forelder: str | None = None) -> Avgjorelse:
        svar = Avgjorelse("redaktor", None)
        for m in self.ledd:
            try:
                v = m.vurder(tekst, sak, forelder)
            except ModereringFeil as e:
                log.warning("%s could not judge a comment on %s: %s", m.name, sak.key, e)
                svar.feil.append(f"{m.name}: {e}")
                continue
            svar.vurderinger.append(v)
            utfall = avgjor(v, self.terskel, m.stol_pa_etikett)
            if utfall != "redaktor":
                svar.utfall, svar.regel = utfall, (v.regel if utfall == "avvis" else None)
                return svar
        return svar


# --- prompt -------------------------------------------------------------------

def innhold(tekst: str, sak: Sak, forelder: str | None) -> str:
    """The reader-written part of a prompt, fenced: the sak, the comment it answers, the comment."""
    deler = ["Saken kommentaren står under:", fence("SAK", f"{sak.tittel}\n{sak.ingress}".strip())]
    if forelder:
        deler += ["Kommentaren svarer på denne kommentaren:", fence("SVARER_PA", forelder)]
    deler += ["Kommentaren du skal vurdere:", fence("KOMMENTAR", tekst)]
    return "\n".join(deler)


def prompt(tekst: str, sak: Sak, forelder: str | None, bokstaver: bool) -> str:
    """Instructions and rules first (the same for every comment, so a local server can
    reuse their cache), then the comment."""
    sporsmal = "Hvilken regel passer best? Svar bare med bokstaven." if bokstaver else "Hvilken regel passer best?"
    return "\n".join([VEILEDNING, "", "Reglene:", regeltekst(bokstaver), "", innhold(tekst, sak, forelder), "",
                       sporsmal])


def _bokstav(svar: str) -> str | None:
    """The rule letter in a small model's answer, after any <think> block."""
    svar = re.sub(r"<think>.*?</think>", "", svar, flags=re.S).strip()
    m = re.match(r"[\s*(\[]*([A-H])\b", svar)
    return m.group(1) if m else None


# --- backends -----------------------------------------------------------------

class Hailo(Moderator):
    """hailo-ollama (Ollama's /api/chat) on the Hailo-10H. No log-probabilities in
    its API, so the answer is just a rule."""

    def __init__(self, url: str, modell: str, session=None):
        self.url, self.modell = url.rstrip("/"), modell
        self.name = f"hailo:{modell}"
        self.session = session or requests.Session()

    def _vurder(self, tekst, sak, forelder):
        # hailo-ollama 5.3 copies the message into JSON without escaping it: a line
        # break or a quote mark gives HTTP 500 (seen 31.10). One line, typographic quotes.
        innhold = (prompt(tekst, sak, forelder, bokstaver=True).replace("\n", " ").replace('"', "”")
                   .replace("\\", "/"))
        if self.modell.startswith("qwen3"):
            innhold += " /no_think"
        try:
            r = self.session.post(f"{self.url}/api/chat", timeout=TIMEOUT_S, json={
                "model": self.modell, "stream": False,
                "messages": [{"role": "user", "content": innhold}],
                "options": {"temperature": 0, "num_predict": 3},
            })
            r.raise_for_status()
            svar = r.json()["message"]["content"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise ModereringFeil(f"hailo-ollama: {e}") from None
        bokstav = _bokstav(svar)
        if bokstav is None:
            raise ModereringFeil(f"hailo-ollama answered no rule letter: {svar[:80]!r}")
        return Vurdering(BOKSTAVER[bokstav], None, self.name, 0)


class LlamaCpp(Moderator):
    """llama-server's OpenAI-style chat endpoint, one token, the top log-probabilities:
    the probability of each rule is the softmax over the rule letters ("self-hosted
    Jev"). The fixed instructions come first so llama-server can reuse their cache."""

    def __init__(self, url: str, modell: str = "lokal", session=None):
        self.url, self.modell = url.rstrip("/"), modell
        self.name = f"cpu:{modell}"
        self.session = session or requests.Session()

    def _vurder(self, tekst, sak, forelder):
        try:
            r = self.session.post(f"{self.url}/v1/chat/completions", timeout=TIMEOUT_S, json={
                "model": self.modell,
                "messages": [{"role": "user", "content": prompt(tekst, sak, forelder, bokstaver=True)}],
                "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20,
                "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
            })
            r.raise_for_status()
            topp = r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
            raise ModereringFeil(f"llama-server: {e}") from None
        return Vurdering(*_fra_logprobs(topp), self.name, 0)


def _fra_logprobs(topp: list[dict]) -> tuple[str, dict[str, float]]:
    """(rule, probability per rule) from the top log-probabilities of one token."""
    beste: dict[str, float] = {}
    for t in topp:
        bokstav = (t.get("token") or "").strip()
        if bokstav in BOKSTAVER and bokstav not in beste:
            beste[bokstav] = t["logprob"]
    if not beste:
        raise ModereringFeil("no rule letter among the top tokens")
    hoyest = max(beste.values())
    vekt = {BOKSTAVER[b]: math.exp(lp - hoyest) for b, lp in beste.items()}
    total = sum(vekt.values())
    sann = {k: round(vekt.get(k, 0.0) / total, 4) for k in REGLER}
    return max(sann, key=sann.get), sann


class Jev(Moderator):
    """TypeSafe's System One API: the rules as one choice question, the comment as state."""

    def __init__(self, url: str, modell: str, nokkel: str | None, session=None):
        self.url, self.modell, self.nokkel = url, modell, nokkel
        self.name = f"jev:{modell}"
        self.session = session or requests.Session()

    def _vurder(self, tekst, sak, forelder):
        if not self.nokkel:
            raise ModereringFeil("TYPESAFE_API_KEY is not set")
        state = {"sak": f"{sak.tittel}\n{sak.ingress}".strip(), "kommentar": tekst}
        if forelder:
            state["svarer_pa"] = forelder
        try:
            r = self.session.post(self.url, timeout=TIMEOUT_S, headers={"Authorization": f"Bearer {self.nokkel}"},
                                  json={"model": self.modell, "state": state, "questions": {"regel": {
                                      "type": "choice", "instructions": VEILEDNING,
                                      "criteria": {k: tekst_ for k, (_, tekst_) in REGLER.items()}}}})
            r.raise_for_status()
            svar = r.json()["answers"]["regel"]
            sann = {k: float(svar["probabilities"].get(k, 0.0)) for k in REGLER}
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
            raise ModereringFeil(f"jev: {e}") from None
        regel = svar.get("choice") if svar.get("choice") in REGLER else max(sann, key=sann.get)
        # Price: $0.042 per million input tokens, output free (TypeSafe, 2026); ~4 characters per token.
        tokens = (len(json.dumps(state)) + len(VEILEDNING) + sum(len(t) for _, t in REGLER.values())) / 4
        return Vurdering(regel, sann, self.name, 0, kostnad_usd=tokens * 0.042e-6)


CLAUDE_SKJEMA = {
    "type": "object", "additionalProperties": False, "required": ["regel", "sikkerhet", "begrunnelse"],
    "properties": {
        "regel": {"type": "string", "enum": list(REGLER)},
        "sikkerhet": {"type": "string", "enum": list(_SIKKERHET)},
        "begrunnelse": {"type": "string"},
    },
}
# $ per million tokens (input, output), first-party API prices cached 2026-09-25.
CLAUDE_PRIS = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}


class Claude(Moderator):
    """The Claude API, low effort, a JSON answer from a fixed schema. Server-side
    fallbacks are on: if the model declines, the API re-runs the request on a
    fallback model; a decline that stays a decline sends the comment to the editor."""

    def __init__(self, modell: str, nokkel: str | None, effort: str = "low", klient=None):
        self.modell, self.nokkel, self.effort = modell, nokkel, effort
        self.name = f"claude:{modell}"
        self._klient = klient

    def _klienten(self):
        if self._klient is None:
            if not self.nokkel:
                raise ModereringFeil("TINGPOSTEN_ANTHROPIC_API_KEY is not set")
            import anthropic  # the `debatt` extra

            self._klient = anthropic.Anthropic(api_key=self.nokkel, timeout=TIMEOUT_S, max_retries=2)
        return self._klient

    def _vurder(self, tekst, sak, forelder):
        import anthropic

        klient = self._klienten()
        system = (VEILEDNING + " Svar med regelen, hvor sikker du er (høy, middels eller lav) og én kort "
                  "setning til redaktøren om hvorfor.\n\nReglene:\n" + regeltekst())
        try:
            svar = klient.beta.messages.create(
                model=self.modell, max_tokens=2000, system=system,
                messages=[{"role": "user", "content": innhold(tekst, sak, forelder) + "\n\nHvilken regel passer best?"}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": CLAUDE_SKJEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            raise ModereringFeil(f"claude API {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ModereringFeil(f"claude API unreachable: {e}") from None
        if svar.stop_reason == "refusal":
            raise ModereringFeil("claude declined to judge the comment")
        try:
            data = json.loads(next(b.text for b in svar.content if b.type == "text"))
        except (StopIteration, ValueError):
            raise ModereringFeil("claude gave no JSON") from None
        inn, ut = CLAUDE_PRIS.get(self.modell, (0.0, 0.0))
        kostnad = (svar.usage.input_tokens * inn + svar.usage.output_tokens * ut) / 1e6
        regel = data["regel"] if data.get("regel") in REGLER else "ok"
        p = _SIKKERHET.get(data.get("sikkerhet"), 0.5)
        sann = {k: (p if k == regel else round((1 - p) / (len(REGLER) - 1), 4)) for k in REGLER}
        return Vurdering(regel, sann, f"claude:{svar.model}", 0, begrunnelse=data.get("begrunnelse", "")[:300],
                         kostnad_usd=round(kostnad, 6))


class FakeModerator(Moderator):
    """For tests: returns the given Vurderinger (or raises the given exceptions) in order."""

    def __init__(self, svar, name: str = "fake", stol_pa_etikett: bool = False):
        self.svar, self.name, self.stol_pa_etikett = list(svar), name, stol_pa_etikett
        self.kall: list[tuple[str, Sak, str | None]] = []

    def _vurder(self, tekst, sak, forelder):
        self.kall.append((tekst, sak, forelder))
        s = self.svar.pop(0)
        if isinstance(s, Exception):
            raise s
        return s


# --- from config --------------------------------------------------------------

def backend(navn: str) -> Moderator:
    if navn == "hailo":
        m = Hailo(config.DEBATT_HAILO_URL, config.DEBATT_HAILO_MODELL)
        m.stol_pa_etikett = config.DEBATT_HAILO_STOL_PA_ETIKETT
        return m
    if navn == "cpu":
        return LlamaCpp(config.DEBATT_CPU_URL, config.DEBATT_CPU_MODELL)
    if navn == "jev":
        return Jev(config.DEBATT_JEV_URL, config.DEBATT_JEV_MODELL, os.environ.get("TYPESAFE_API_KEY"))
    if navn == "claude":
        return Claude(config.DEBATT_CLAUDE_MODELL, os.environ.get("TINGPOSTEN_ANTHROPIC_API_KEY"))
    raise ValueError(f"unknown moderator {navn!r} (hailo, cpu, jev, claude)")


def kjede(navn: tuple[str, ...] | list[str] | None = None, terskel: float | None = None) -> Kjede:
    return Kjede([backend(n) for n in (navn or config.DEBATT_MODERATOR)],
                 config.DEBATT_TERSKEL if terskel is None else terskel)
