"""Redaktør: the plan's deletion-only editor (pipeline step 5).

Takes a draft card and its fact-check and may only remove whole sentences:
the ones the checker found unsupported and the ones on the forbudsliste.
It cannot add or rewrite, so it cannot inflate the text — that is the point.
Hard limits are not trimmed: a card over a limit is rejected (avvist) and
goes back to the writer. Anything not fully fact-checked, or with a privacy
finding, is rejected too, because publishing is automatic.
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
# when the sentence reports byrådet's or the administration's own reasoning,
# because reporting that reasoning is the field's job.
VIKTIGHETSORD = [
    r"\bviktig\w*",
    r"\bhistorisk\w*",
    r"\bbetydelig\w*",
    r"\bbanebrytende\b",
    r"\bavgjørende\b",
    r"\bstor betydning\b",
]
_SUBJECT = r"(?:byrådet|administrasjonen|fagetaten|sekretariatet|kontrollutvalget)"
_ATTRIBUTED = re.compile(
    rf"^\s*(?:ifølge\s+{_SUBJECT}\b|{_SUBJECT}\b.*\b(?:mener|skriver|begrunner|viser\b.*?\btil)\b)",
    re.IGNORECASE | re.DOTALL,
)

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
    for rx in _IMPORTANCE:
        if m := rx.search(sentence):
            return f"viktighetsord «{m.group(0)}»"
    return None


def _segment(text: str, sentences: list[str]) -> list[str] | None:
    """The check's sentences located in text, in order; None unless they
    cover the text exactly (only whitespace may sit between them)."""
    pos, out = 0, []
    for s in sentences:
        i = text.find(s, pos) if s else -1
        if i < 0 or text[pos:i].strip():
            return None
        out.append(text[i:i + len(s)])
        pos = i + len(s)
    return None if text[pos:].strip() else out


def _naive_count(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def edit(card: dict, check: dict | None) -> dict:
    """Delete unsupported and forbidden sentences; reject what can't be published."""
    reasons: list[str] = []
    slettet: list[dict] = []
    blocked = (check or {}).get("blocked") or ""
    checked = check is not None and not blocked.strip()
    if check is None:
        reasons.append("ikke faktasjekket")
    elif blocked.strip():
        reasons.append(f"ikke faktasjekket: {blocked.strip()}")
    if check and check.get("personvern"):
        reasons.append("personvern: " + "; ".join(check["personvern"]))

    by_field: dict[str, list[dict]] = {}
    for s in (check or {}).get("setninger", []):
        by_field.setdefault(s.get("felt"), []).append(s)

    headline = card.get("overskrift") or ""
    if len(headline) > MAX_HEADLINE:
        reasons.append(f"overskrift over {MAX_HEADLINE} tegn ({len(headline)})")
    if checked:
        hs = by_field.get("overskrift", [])
        if not hs or hs[0].get("setning", "").strip() != headline.strip():
            reasons.append("overskrift ikke faktasjekket")
        elif hs[0].get("stottet") is not True:
            reasons.append("overskrift ikke støttet av kilden: " + (hs[0].get("begrunnelse") or "").strip())
    if hit := banned(headline, "overskrift"):
        reasons.append(f"forbudsliste i overskrift: {hit}")

    felt, segments, kept_counts = {}, {}, {}
    for f in FIELDS:
        text = card.get(f) or ""
        seg = _segment(text, [s.get("setning", "") for s in by_field.get(f, [])]) if checked else None
        if seg is None:
            if checked:
                reasons.append(f"faktasjekken dekker ikke {f}")
            felt[f], kept_counts[f] = text, _naive_count(text)
            if kept_counts[f] > MAX_SENTENCES[f]:
                reasons.append(f"{f} har {kept_counts[f]} setninger (maks {MAX_SENTENCES[f]})")
            continue
        if len(seg) > MAX_SENTENCES[f]:
            reasons.append(f"{f} har {len(seg)} setninger (maks {MAX_SENTENCES[f]})")
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
        reasons.append("ingenting igjen i Hva endres")

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
        "grunn": "; ".join(reasons),
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
        seg, pos = segments.get(f, []), 0
        for s in seg:
            if (pos := original.find(s, pos)) < 0:
                raise AssertionError(f"{f}: setning finnes ikke i kortet")
            pos += len(s)
        # Can `out` be spelled as some of seg's sentences, in order, joined by spaces?
        reachable = {0}
        for s in seg:
            for i in list(reachable):
                if out.startswith(s, i):
                    j = i + len(s)
                    reachable |= {j, j + 1} if out.startswith(" ", j) else {j}
        if len(out) not in reachable:
            raise AssertionError(f"{f}: redaktøren la til eller endret tekst")


def run(items: list[dict], data_dir: Path = config.DATA_DIR) -> dict:
    """Edit every item that has a draft card; write redigert/<key>.json and metrics."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"redigert": 0, "publiserbar": 0, "avvist": 0, "slettet": 0}
    metrics = []
    for item in items:
        key = pdf.key(item)
        card_path = data_dir / "kort" / f"{key}.json"
        if not card_path.exists():
            continue
        check_path = data_dir / "sjekk" / f"{key}.json"
        check = json.loads(check_path.read_text()) if check_path.exists() else None
        result = edit(json.loads(card_path.read_text()), check)
        _write_json(data_dir / "redigert" / f"{key}.json", result)
        counts["redigert"] += 1
        counts["avvist" if result["avvist"] else "publiserbar"] += 1
        counts["slettet"] += len(result["slettet"])
        metrics.append({
            "tid": now, "key": key, "ord": result["ord"], "setninger": result["setninger"],
            "slettet": len(result["slettet"]), "avvist": result["avvist"], "grunn": result["grunn"],
        })
    if metrics:
        path = data_dir / "metrikk.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.writelines(json.dumps(m, ensure_ascii=False) + "\n" for m in metrics)
    return counts


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)
