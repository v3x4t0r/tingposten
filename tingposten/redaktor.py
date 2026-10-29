"""Redaktør: the plan's deletion-only editor (pipeline step 5).

Takes a draft card and its fact-check and may only remove whole sentences:
the ones the checker found unsupported and the ones on the forbudsliste.
It cannot add or rewrite, so it cannot inflate the text — that is the point.
Hard limits are not trimmed: a card over a limit is rejected (avvist) and
goes back to the writer. Anything not fully fact-checked, or with a privacy
finding, is rejected too, because publishing is automatic.

Articles get the same treatment (rediger_artikkel): sentences that are
unsupported, that only repeat the card (plan rule 1), or that hit the
forbudsliste are deleted; an article over the limits, or with too little left,
is rejected.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config, pdf

FIELDS = ("hva_endres", "hvem_pavirkes", "hvorfor", "status", "nar_og_hvor")
MAX_SENTENCES = {"hva_endres": 3, "hvem_pavirkes": 3, "hvorfor": 3, "status": 2, "nar_og_hvor": 2}
MAX_HEADLINE = 70
MAX_ARTICLE_WORDS = 250
MAX_PARAGRAPHS = 5
MIN_ARTICLE_WORDS = 40

# Rejection codes ("koder" in the output). Only these may be fixed by the
# writer automatically; personvern always waits for a human, and checker
# problems (missing, blocked, bad split) are not the writer's to fix.
OMSKRIVBARE = {"overskrift_lang", "overskrift_ustottet", "overskrift_forbudsliste",
               "for_mange_setninger", "tom_hva_endres"}
OMSKRIVBARE_ARTIKKEL = {"for_lang", "for_lite_nytt"}

# Forbudsliste (plan: "Redaksjonelle regler mot oppblåst tekst", rule 4).
# A sentence matching any of these is deleted, never reworded.
FORBUDSLISTE = {
    "innledning": [
        r"^I dette vedtaket\b",
        r"^Dette vedtaket innebærer\b",
        r"\bDet er verdt å merke seg\b",
        r"\bDet er viktig å merke seg\b",
    ],
    "oppsummering": [r"^Oppsummert\b", r"^Kort sagt\b", r"^Alt i alt\b"],
    "retorisk spørsmål": [r"\?[\s»\"')\]]*$"],
    "du-form": [r"\b(?:du|deg|din|ditt|dine)\b"],
    # Case-sensitive: "1. Prosjektet …" is a list item, "8. oktober …" is a date.
    "punktliste": [r"(?m)^\s*[-•*–·]\s", r"(?m)^\s*\d{1,2}[.)]\s+[A-ZÆØÅ]"],
}
_CASE_SENSITIVE = {"punktliste"}

# The score shows importance; the text must not claim it. Allowed in "hvorfor"
# and in articles when the sentence reports someone's stated reasoning —
# byrådet's, the administration's, or (in articles, which say who argued for
# what) a party's — because reporting that reasoning is the job there.
VIKTIGHETSORD = [
    r"\bviktig\w*",
    r"\bhistorisk\w*",
    r"\bbetydelig\w*",
    r"\bbanebrytende\b",
    r"\bavgjørende\b",
    r"\bstor betydning\b",
]
_SUBJECT = r"(?:byrådet|administrasjonen|fagetaten|sekretariatet|kontrollutvalget)"
_GROUPS = r"(?:flertallet|mindretallet|forslagsstillerne)"
_PARTIES = r"(?:Ap|H|FrP|SV|Sp|KrF|V|MDG|R|INP|BL|PP)"
_SAYS = r"(?:mener|mente|skriver|skrev|begrunner|begrunnet|viser\b.*?\btil|viste\b.*?\btil|peker\b.*?\bpå|pekte\b.*?\bpå)"
_ATTRIBUTED = re.compile(
    rf"^\s*(?:ifølge\s+{_SUBJECT}\b|{_SUBJECT}\b.*\b{_SAYS}\b)",
    re.IGNORECASE | re.DOTALL,
)
# Party abbreviations are matched case-sensitively ("V" the party, not "v").
_ATTRIBUTED_PARTY = re.compile(rf"^\s*(?:ifølge\s+{_PARTIES}\b|{_PARTIES}\b.*\b{_SAYS}\b)", re.DOTALL)
_ATTRIBUTED_GROUP = re.compile(rf"^\s*{_GROUPS}\b.*\b{_SAYS}\b", re.IGNORECASE | re.DOTALL)

_BANNED = [
    (kategori, re.compile(p, 0 if kategori in _CASE_SENSITIVE else re.IGNORECASE))
    for kategori, patterns in FORBUDSLISTE.items()
    for p in patterns
]
_IMPORTANCE = [re.compile(p, re.IGNORECASE) for p in VIKTIGHETSORD]


def banned(sentence: str, felt: str) -> str | None:
    """The forbudsliste match in a sentence, or None."""
    for kategori, rx in _BANNED:
        if m := rx.search(sentence):
            return f"{kategori} «{m.group(0).strip()}»"
    if felt == "hvorfor" and _ATTRIBUTED.search(sentence):
        return None
    if felt == "artikkel" and any(rx.search(sentence) for rx in (_ATTRIBUTED, _ATTRIBUTED_PARTY, _ATTRIBUTED_GROUP)):
        return None
    for rx in _IMPORTANCE:
        if m := rx.search(sentence):
            return f"viktighetsord «{m.group(0)}»"
    return None


def _segment(text: str, sentences: list[str]) -> list[str] | None:
    """The check's sentences located in text, in order; None unless they
    cover the text exactly (only whitespace may sit between them)."""
    pos, out = 0, []
    for s in sentences:
        s = s.strip()
        i = text.find(s, pos) if s else -1
        if i < 0 or text[pos:i].strip():
            return None
        out.append(text[i:i + len(s)])
        pos = i + len(s)
    return None if text[pos:].strip() else out


def _naive_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


class _Reasons:
    """Rejection reasons as (code, text); the text goes to readers of the log."""

    def __init__(self):
        self.items: list[tuple[str, str]] = []

    def add(self, code: str, text: str) -> None:
        self.items.append((code, text))

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def text(self) -> str:
        return "; ".join(t for _, t in self.items)

    @property
    def codes(self) -> list[str]:
        return list(dict.fromkeys(c for c, _ in self.items))


def _check_gate(check: dict | None, reasons: _Reasons) -> bool:
    """Missing, blocked or privacy-flagged checks reject; True if usable."""
    blocked = (check or {}).get("blocked") or ""
    if check is None:
        reasons.add("ikke_sjekket", "ikke faktasjekket")
    elif blocked.strip():
        reasons.add("ikke_sjekket", f"ikke faktasjekket: {blocked.strip()}")
    if check and check.get("personvern"):
        reasons.add("personvern", "personvern: " + "; ".join(check["personvern"]))
    return check is not None and not blocked.strip()


def omskrivbar(result: dict, tillatt: set[str] = OMSKRIVBARE) -> bool:
    """A rejected card (or, with tillatt=OMSKRIVBARE_ARTIKKEL, article) the
    writer may fix on its own: only limit and headline problems, and never
    when there is a privacy finding."""
    koder = set(result.get("koder") or [])
    return bool(result.get("avvist")) and "personvern" not in koder and bool(koder & tillatt)


def edit(card: dict, check: dict | None) -> dict:
    """Delete unsupported and forbidden sentences; reject what can't be published."""
    reasons = _Reasons()
    slettet: list[dict] = []
    checked = _check_gate(check, reasons)

    by_field: dict[str, list[dict]] = {}
    for s in (check or {}).get("setninger", []):
        by_field.setdefault(s.get("felt"), []).append(s)

    headline = card.get("overskrift") or ""
    if len(headline) > MAX_HEADLINE:
        reasons.add("overskrift_lang", f"overskrift over {MAX_HEADLINE} tegn ({len(headline)})")
    if checked:
        hs = by_field.get("overskrift", [])
        if not hs or hs[0].get("setning", "").strip() != headline.strip():
            reasons.add("dekning", "overskrift ikke faktasjekket")
        elif hs[0].get("stottet") is not True:
            reasons.add("overskrift_ustottet",
                        "overskrift ikke støttet av kilden: " + (hs[0].get("begrunnelse") or "").strip())
    if hit := banned(headline, "overskrift"):
        reasons.add("overskrift_forbudsliste", f"forbudsliste i overskrift: {hit}")

    felt, segments, kept_counts = {}, {}, {}
    for f in FIELDS:
        text = card.get(f) or ""
        seg = _segment(text, [s.get("setning", "") for s in by_field.get(f, [])]) if checked else None
        if seg is None:
            if checked:
                reasons.add("dekning", f"faktasjekken dekker ikke {f}")
            felt[f], kept_counts[f] = text, _naive_count(text)
            if kept_counts[f] > MAX_SENTENCES[f]:
                reasons.add("for_mange_setninger", f"{f} har {kept_counts[f]} setninger (maks {MAX_SENTENCES[f]})")
            continue
        if len(seg) > MAX_SENTENCES[f]:
            reasons.add("for_mange_setninger", f"{f} har {len(seg)} setninger (maks {MAX_SENTENCES[f]})")
        kept = []
        for s, original in zip(by_field.get(f, []), seg):
            if s.get("stottet") is not True:
                why = "ikke støttet av kilden" + (f": {s['begrunnelse'].strip()}" if (s.get("begrunnelse") or "").strip() else "")
            elif hit := banned(original, f):
                why = f"forbudsliste: {hit}"
            else:
                kept.append(original)
                continue
            slettet.append({"felt": f, "setning": original, "grunn": why})
        segments[f] = seg
        felt[f], kept_counts[f] = " ".join(kept), len(kept)

    if not felt["hva_endres"].strip():
        reasons.add("tom_hva_endres", "ingenting igjen i Hva endres")

    result = {
        "nr": card.get("nr", ""),
        "overskrift": headline,
        "felt": felt,
        "kilder": [
            {"felt": k.get("felt"), "dok": k.get("dok"), "side": k.get("side")}
            for k in card.get("kilder", [])
            if felt.get(k.get("felt"), "").strip()
        ],
        "slettet": slettet,
        "avvist": bool(reasons),
        "grunn": reasons.text,
        "koder": reasons.codes,
        "ord": len(headline.split()) + sum(len(v.split()) for v in felt.values()),
        "setninger": (1 if headline.strip() else 0) + sum(kept_counts.values()),
    }
    _assert_deletion_only(card, result, segments)
    return result


def _assert_deletion_only(card: dict, result: dict, segments: dict[str, list[str]]) -> None:
    """Every output field is its input field with whole sentences removed."""
    if result["overskrift"] != (card.get("overskrift") or ""):
        raise AssertionError("redaktøren endret overskriften")
    for f in FIELDS:
        original, out = card.get(f) or "", result["felt"][f]
        if out == original:
            continue
        if not _is_deletion(original, out, segments.get(f, [])):
            raise AssertionError(f"{f}: redaktøren la til eller endret tekst")


def _is_deletion(original: str, out: str, seg: list[str]) -> bool:
    """True if `out` is some of `seg`'s sentences, in order, joined by single
    spaces — and every sentence in `seg` really occurs in `original`, in order."""
    pos = 0
    for s in seg:
        if (pos := original.find(s, pos)) < 0:
            return False
        pos += len(s)
    reachable = {0}
    for s in seg:
        for i in list(reachable):
            if out.startswith(s, i):
                j = i + len(s)
                reachable |= {j, j + 1} if out.startswith(" ", j) else {j}
    return len(out) in reachable


def run(items: list[dict], data_dir: Path = config.DATA_DIR) -> dict:
    """Edit every item that has a draft card; write redigert/<key>.json and metrics.

    A file is only rewritten when its content changes, so its mtime tells the
    article steps whether the published card is newer than their output; the
    metrics log gets one line per change, not one per run."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"redigert": 0, "publiserbar": 0, "avvist": 0, "slettet": 0, "endret": 0}
    metrics = []
    for item in items:
        key = pdf.key(item)
        card_path = data_dir / "kort" / f"{key}.json"
        if not card_path.exists():
            continue
        check = _read(data_dir / "sjekk" / f"{key}.json")
        result = edit(json.loads(card_path.read_text()), check)
        counts["redigert"] += 1
        counts["avvist" if result["avvist"] else "publiserbar"] += 1
        counts["slettet"] += len(result["slettet"])
        if _write_if_changed(data_dir / "redigert" / f"{key}.json", result):
            counts["endret"] += 1
            metrics.append({
                "tid": now, "type": "kort", "key": key, "ord": result["ord"], "setninger": result["setninger"],
                "slettet": len(result["slettet"]), "avvist": result["avvist"], "grunn": result["grunn"],
            })
    _log_metrics(data_dir, metrics)
    return counts


# --- articles -------------------------------------------------------------------

def rediger_artikkel(article: dict, check: dict | None) -> dict:
    """Delete-only editing of an article against its check (see module docstring)."""
    reasons = _Reasons()
    slettet: list[dict] = []
    checked = _check_gate(check, reasons)
    paragraphs = [a.get("tekst") or "" for a in article.get("avsnitt", [])]

    words_before = sum(len(p.split()) for p in paragraphs)
    if words_before > MAX_ARTICLE_WORDS:
        reasons.add("for_lang", f"artikkelen har {words_before} ord (maks {MAX_ARTICLE_WORDS})")
    if len(paragraphs) > MAX_PARAGRAPHS:
        reasons.add("for_lang", f"artikkelen har {len(paragraphs)} avsnitt (maks {MAX_PARAGRAPHS})")

    by_par: dict[int, list[dict]] = {}
    for s in (check or {}).get("setninger", []):
        if isinstance(s.get("avsnitt"), int):
            by_par.setdefault(s["avsnitt"], []).append(s)

    kept_pars, segments, sentences_before, new_facts = [], {}, 0, 0
    for n, text in enumerate(paragraphs, start=1):
        checks = by_par.get(n, [])
        seg = _segment(text, [s.get("setning", "") for s in checks]) if checked else None
        if seg is None:
            if checked:
                reasons.add("dekning", f"faktasjekken dekker ikke avsnitt {n}")
            sentences_before += _naive_count(text)
            kept_pars.append((n, text))
            continue
        sentences_before += len(seg)
        kept = []
        for s, original in zip(checks, seg):
            if s.get("stottet") is not True:
                why = "ikke støttet av kilden" + (f": {s['begrunnelse'].strip()}" if (s.get("begrunnelse") or "").strip() else "")
            elif s.get("gjentar_kortet") is not False:
                why = "gjentar kortet"
            elif hit := banned(original, "artikkel"):
                why = f"forbudsliste: {hit}"
            else:
                kept.append(original)
                new_facts += 1
                continue
            slettet.append({"avsnitt": n, "setning": original, "grunn": why})
        segments[n] = seg
        if kept:
            kept_pars.append((n, " ".join(kept)))

    avsnitt = [{"tekst": t, "kilder": article["avsnitt"][n - 1].get("kilder", [])} for n, t in kept_pars]
    words = sum(len(a["tekst"].split()) for a in avsnitt)
    if words < MIN_ARTICLE_WORDS:
        reasons.add("for_lite_nytt", f"for lite nytt: {words} ord igjen (minst {MIN_ARTICLE_WORDS})")

    result = {
        "nr": article.get("nr", ""),
        "avsnitt": avsnitt,
        "slettet": slettet,
        "avvist": bool(reasons),
        "grunn": reasons.text,
        "koder": reasons.codes,
        "ord": words,
        "ord_for": words_before,
        "setninger_for": sentences_before,
        "nye_fakta": new_facts,
    }
    _assert_article_deletion_only(paragraphs, kept_pars, segments)
    return result


def _assert_article_deletion_only(paragraphs: list[str], kept: list[tuple[int, str]],
                                  segments: dict[int, list[str]]) -> None:
    """Kept paragraphs come from the input, in order, each with whole sentences removed."""
    last = 0
    for n, text in kept:
        if n <= last or not 1 <= n <= len(paragraphs):
            raise AssertionError("redaktøren flyttet eller la til avsnitt")
        last = n
        if text != paragraphs[n - 1] and not _is_deletion(paragraphs[n - 1], text, segments.get(n, [])):
            raise AssertionError(f"avsnitt {n}: redaktøren la til eller endret tekst")


def run_artikler(items: list[dict], data_dir: Path = config.DATA_DIR) -> dict:
    """Edit every item that has an article; write artikler-redigert/<key>.json."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"redigert": 0, "publiserbar": 0, "avvist": 0, "slettet": 0, "endret": 0}
    metrics = []
    for item in items:
        key = pdf.key(item)
        art_path = data_dir / "artikler" / f"{key}.json"
        if not art_path.exists():
            continue
        check_path = data_dir / "artikkelsjekk" / f"{key}.json"
        # A check older than the article was made for another text.
        fresh = check_path.exists() and check_path.stat().st_mtime >= art_path.stat().st_mtime
        result = rediger_artikkel(json.loads(art_path.read_text()), _read(check_path) if fresh else None)
        counts["redigert"] += 1
        counts["avvist" if result["avvist"] else "publiserbar"] += 1
        counts["slettet"] += len(result["slettet"])
        if _write_if_changed(data_dir / "artikler-redigert" / f"{key}.json", result):
            counts["endret"] += 1
            metrics.append({
                "tid": now, "type": "artikkel", "key": key, "ord_for": result["ord_for"], "ord": result["ord"],
                "setninger_for": result["setninger_for"], "nye_fakta": result["nye_fakta"],
                # Plan: "Logg … setninger per nytt faktum" — sentences written per sentence that survived.
                "setninger_per_nytt_faktum": round(result["setninger_for"] / result["nye_fakta"], 2) if result["nye_fakta"] else None,
                "slettet": len(result["slettet"]), "avvist": result["avvist"], "grunn": result["grunn"],
            })
    _log_metrics(data_dir, metrics)
    return counts


# --- files ----------------------------------------------------------------------

def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write_if_changed(path: Path, obj) -> bool:
    if path.exists():
        try:
            if json.loads(path.read_text()) == obj:
                return False
        except ValueError:
            pass
    _write_json(path, obj)
    return True


def _log_metrics(data_dir: Path, metrics: list[dict]) -> None:
    if not metrics:
        return
    path = data_dir / "metrikk.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.writelines(json.dumps(m, ensure_ascii=False) + "\n" for m in metrics)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)
