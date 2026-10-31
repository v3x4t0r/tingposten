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
to a backend: never the reader's e-mail, display name or address. Display names
are checked only by the local backends (Kjede.lokal()).
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


@dataclass(frozen=True)
class Grenser:
    """How sure a verdict must be, per rule: (stopp, tvil). A rule at or above its `stopp`
    stops the comment; at or above its `tvil` the editor decides. A comment is published
    when every rule is below its `tvil` and "ok" itself is at least `ok` likely (so a
    model that spreads its doubt thinly over many rules isn't read as sure). The strict
    rules (regler.STRENGE) get low limits: even mild doubt about hate or violence goes
    to the editor."""

    regler: dict[str, tuple[float, float]]
    ok: float = 0.5

    @classmethod
    def jevn(cls, t: float) -> "Grenser":
        """One threshold for everything: stop at `t`, publish when "ok" is at least `t`
        likely and every rule is below 1 − t."""
        return cls({k: (t, round(1 - t, 4)) for k in REGLER if k != "ok"}, ok=t)


def avgjor(v: Vurdering, grenser: Grenser, stol_pa_etikett: bool = False) -> tuple[str, str | None]:
    """One backend's verdict as (outcome, the rule a stopped comment broke). With several
    rules over their limits, the most serious (REGLER order) is the one named. A backend
    without probabilities is trusted only with `stol_pa_etikett` (set from the test
    results), otherwise it is 'unsure'."""
    if v.regel not in REGLER:
        return "redaktor", None
    if v.sannsynligheter is None:
        if not stol_pa_etikett:
            return "redaktor", None
        return ("publiser", None) if v.regel == "ok" else ("avvis", v.regel)
    p = v.sannsynligheter
    for regel, (stopp, _) in grenser.regler.items():
        if p.get(regel, 0.0) >= stopp:
            return "avvis", min((k for k in grenser.regler if p.get(k, 0.0) >= grenser.regler[k][0]),
                                key=list(REGLER).index)
    if any(p.get(regel, 0.0) >= tvil for regel, (_, tvil) in grenser.regler.items()) or p.get("ok", 0.0) < grenser.ok:
        return "redaktor", None
    return "publiser", None


class Moderator:
    name = "?"
    stol_pa_etikett = False
    lokal = True  # runs on Tingposten's own machine; False = the text leaves the house

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
    """Asks each moderator in turn until one is sure. The same `grenser` apply to all."""

    def __init__(self, ledd: list[Moderator], grenser: Grenser):
        if not ledd:
            raise ValueError("a moderator chain needs at least one backend")
        self.ledd, self.grenser = ledd, grenser

    @property
    def name(self) -> str:
        return " → ".join(m.name for m in self.ledd)

    def lokal(self) -> "Kjede | None":
        """The same chain with only the backends that run at home (for display names)."""
        ledd = [m for m in self.ledd if m.lokal]
        return Kjede(ledd, self.grenser) if ledd else None

    def avgjor(self, tekst: str, sak: Sak, forelder: str | None = None) -> Avgjorelse:
        svar = Avgjorelse("redaktor", None)
        for m in self.ledd:
            try:
                v = m.vurder(tekst, sak, forelder)
            except ModereringFeil as e:
                log.warning("%s could not judge a comment on %s: %s", m.name, sak.key, e)
                svar.feil.append(f"{m.name}: {e}")
                continue
            except Exception as e:  # a bug or a surprise in one backend: the next one (or the editor) decides
                log.exception("%s failed on a comment on %s", m.name, sak.key)
                svar.feil.append(f"{m.name}: {type(e).__name__}")
                continue
            svar.vurderinger.append(v)
            utfall, regel = avgjor(v, self.grenser, m.stol_pa_etikett)
            if utfall != "redaktor":
                svar.utfall, svar.regel = utfall, regel
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
    """(rule, probability per rule) from the top log-probabilities of one token. The
    probabilities are absolute, not renormalised over the letters: when the model
    mostly wanted to say something else, no rule gets near the threshold and the
    comment waits for the editor. " A" and "A" count as the same letter."""
    sann = {k: 0.0 for k in REGLER}
    for t in topp:
        bokstav = (t.get("token") or "").strip()
        if bokstav in BOKSTAVER:
            sann[BOKSTAVER[bokstav]] += math.exp(t["logprob"])
    if not any(sann.values()):
        raise ModereringFeil("no rule letter among the top tokens")
    sann = {k: round(min(v, 1.0), 4) for k, v in sann.items()}
    return max(sann, key=sann.get), sann


def sporsmal_en(modus: str) -> dict:
    """The rules as System One questions (Jev's request format, which Laya and Jev-Style
    also read). "ja-nei": one yes/no question per rule with what counts as yes and no, the
    pattern of TypeSafe's guardrail cookbook. "valg": one question over all the rules."""
    from .regler import SPORSMAL_EN

    if modus == "valg":
        return {"regel": {
            "type": "choice",
            "instructions": "Which rule does the reader comment in `kommentar` break? All opinions are allowed, "
                            "also harsh and unpopular ones; choose ok if it breaks none.",
            "criteria": {"ok": "breaks none of the rules", **{k: q for k, (q, _, _) in SPORSMAL_EN.items()}},
        }}
    if modus == "ja-nei":
        return {k: {"type": "noul", "instructions": q, "criteria": {"true": ja, "false": nei}}
                for k, (q, ja, nei) in SPORSMAL_EN.items()}
    raise ValueError(f"unknown question mode {modus!r} (ja-nei, valg)")


def fra_svar(svar: dict, modus: str) -> tuple[str, dict[str, float]]:
    """(rule, probability per rule) from System One answers. In "ja-nei" every rule has
    its own probability and ok is the chance that none applies (1 − the likeliest)."""
    if modus == "valg":
        sann = {k: float(svar["regel"]["probabilities"].get(k, 0.0)) for k in REGLER}
        return max(sann, key=sann.get), sann
    brudd = {k: float(svar[k]["noul"]) for k in REGLER if k != "ok"}
    verst = max(brudd, key=brudd.get)
    sann = {"ok": round(1 - brudd[verst], 4), **{k: round(v, 4) for k, v in brudd.items()}}
    return (verst if brudd[verst] >= 0.5 else "ok"), sann


def _er_lokal(url: str) -> bool:
    vert = (url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]).strip("[]")
    return vert in ("127.0.0.1", "localhost", "::1", "modell", "vert")


class Jev(Moderator):
    """A System One API: TypeSafe's Jev, or a server that speaks the same format on
    our own machine (Jev-Style: `jev-style serve`). The comment and its context are the
    state; the rules are the questions (sporsmal_en). A local server counts as lokal."""

    def __init__(self, url: str, modell: str, nokkel: str | None, modus: str = "ja-nei", session=None):
        sporsmal_en(modus)  # validates the mode
        self.url, self.modell, self.nokkel, self.modus = url, modell, nokkel, modus
        self.lokal = _er_lokal(url)
        self.name = f"jev:{modell}:{modus}"
        self.session = session or requests.Session()

    def _vurder(self, tekst, sak, forelder):
        if not self.nokkel and not self.lokal:
            raise ModereringFeil("TYPESAFE_API_KEY is not set")
        state = {"sak": f"{sak.tittel}\n{sak.ingress}".strip(), "kommentar": tekst}
        if forelder:
            state["svarer_pa"] = forelder
        sporsmal = sporsmal_en(self.modus)
        headers = {"Authorization": f"Bearer {self.nokkel}"} if self.nokkel else {}
        try:
            r = self.session.post(self.url, timeout=TIMEOUT_S, headers=headers,
                                  json={"model": self.modell, "state": state, "questions": sporsmal})
            r.raise_for_status()
            regel, sann = fra_svar(r.json()["answers"], self.modus)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
            raise ModereringFeil(f"jev: {e}") from None
        # Price: $0.042 per million input tokens, output free (TypeSafe, 2026); ~4 characters per token.
        kostnad = 0.0 if self.lokal else (len(json.dumps(state)) + len(json.dumps(sporsmal))) / 4 * 0.042e-6
        return Vurdering(regel, sann, self.name, 0, kostnad_usd=kostnad)


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

    lokal = False

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
        # Anything but a known rule and certainty fails closed: the next backend or the editor decides.
        if not isinstance(data, dict) or data.get("regel") not in REGLER or data.get("sikkerhet") not in _SIKKERHET:
            raise ModereringFeil("claude's answer does not follow the schema")
        inn, ut = CLAUDE_PRIS.get(self.modell, (0.0, 0.0))
        kostnad = (svar.usage.input_tokens * inn + svar.usage.output_tokens * ut) / 1e6
        regel = data["regel"]
        p = _SIKKERHET[data["sikkerhet"]]
        sann = {k: (p if k == regel else round((1 - p) / (len(REGLER) - 1), 4)) for k in REGLER}
        return Vurdering(regel, sann, f"claude:{svar.model}", 0, begrunnelse=str(data.get("begrunnelse", ""))[:300],
                         kostnad_usd=round(kostnad, 6))


class Laya(Moderator):
    """Laya (Convai Innovations, Apache-2.0): an open decision model of the same kind as
    Jev, run in-process on the Pi's CPU; nothing leaves the house. `modus` "ja-nei" asks
    one yes/no question per rule (each rule its own probability; ok = no rule likely),
    "valg" asks one question over all the rules. The questions are in English
    (regler.SPORSMAL_EN); the comment stays as written. Needs `pip install laya` with torch."""

    def __init__(self, modell: str = "convaiinnovations/laya-multilingual", modus: str = "ja-nei", agent=None):
        sporsmal_en(modus)  # validates the mode
        self.modell, self.modus, self._agent = modell, modus, agent
        self.name = f"laya:{modell.rsplit('/', 1)[-1]}:{modus}"

    def _agenten(self):
        if self._agent is None:
            try:
                import laya
            except ImportError:
                raise ModereringFeil("laya is not installed") from None
            self._agent = laya.load(self.modell)
        return self._agent

    def _vurder(self, tekst, sak, forelder):
        state = {"sak": f"{sak.tittel}\n{sak.ingress}".strip(), "kommentar": tekst}
        if forelder:
            state["svarer_pa"] = forelder
        try:
            regel, sann = fra_svar(self._agenten().predict(state, sporsmal_en(self.modus))["answers"], self.modus)
        except ModereringFeil:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise ModereringFeil(f"laya gave an unexpected answer: {e}") from None
        return Vurdering(regel, sann, self.name, 0)


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
        return Jev(config.DEBATT_JEV_URL, config.DEBATT_JEV_MODELL, os.environ.get("TYPESAFE_API_KEY"),
                   config.DEBATT_JEV_MODUS)
    if navn == "laya":
        return Laya(config.DEBATT_LAYA_MODELL, config.DEBATT_LAYA_MODUS)
    if navn == "claude":
        return Claude(config.DEBATT_CLAUDE_MODELL, os.environ.get("TINGPOSTEN_ANTHROPIC_API_KEY"))
    raise ValueError(f"unknown moderator {navn!r} (hailo, cpu, laya, jev, claude)")


def kjede(navn: tuple[str, ...] | list[str] | None = None, grenser: Grenser | None = None) -> Kjede:
    return Kjede([backend(n) for n in (navn or config.DEBATT_MODERATOR)],
                 grenser or Grenser(config.DEBATT_GRENSER, config.DEBATT_GRENSE_OK))
