"""Artikkeltittel: a fact-checked headline for each background article.

The article is written, checked and edited first (skribent → faktasjekker →
redaktør); this step then titles the EDITED article, the text readers see,
so a headline is never written for sentences that were later deleted, and a
checked article is never rewritten just to get a headline. Per sak:

1. the writer (Claude, skribent.skriv_tittel) proposes a headline from the
   edited article and the card's headline, which it must not repeat; the
   script checks the limits and asks once more when they don't hold;
2. Codex (faktasjekker.sjekk_tittel) checks it against the article and the
   pages of the saksframlegg/protokoll the article cites;
3. if the check rejects it, the writer gets one new try with the check's
   objection, and the new headline is checked the same way;
4. the redaktør (redaktor.rediger_tittel, in run_artikler) keeps it only if
   it is supported, neutral, new next to the card's headline and within the
   hard limits. Otherwise the article is published without a headline.

Output: data/bergen/artikkeltittel/<key>.json — the proposal, the check, and
"grunnlag", a fingerprint of the text it was written for. A sak is done again
when that text changes, when its source PDFs are newer than the file, or when
the writer's prompt version changes (only the check, when the checker's
does). The proposal is saved before the check, so a failed check (quota,
timeout) is retried on the next run without asking the writer again; a
failed writer call leaves no file, so it is retried too.
"""

from datetime import datetime, timezone
from pathlib import Path

from . import config, faktasjekker, pdf, redaktor, skribent
from .llm import read_json, run_parallel, write_json


def path(item: dict, data_dir: Path = config.DATA_DIR) -> Path:
    return data_dir / "artikkeltittel" / f"{pdf.key(item)}.json"


def _inputs(item: dict, data_dir: Path) -> tuple[dict, str] | None:
    """The publishable edited article and the card's headline, or None."""
    art = read_json(data_dir / "artikler-redigert" / f"{pdf.key(item)}.json")
    if not art or art.get("avvist") or not art.get("avsnitt"):
        return None
    card = read_json(data_dir / "redigert" / f"{pdf.key(item)}.json") or {}
    return art, card.get("overskrift") or ""


def _sources_newer(item: dict, data_dir: Path, out: Path) -> bool:
    mtime = out.stat().st_mtime
    return any(p.exists() and p.stat().st_mtime > mtime
               for p in (data_dir / "pdf" / pdf.filename(item, k) for k in ("protokoll", "saksframlegg")))


def _fresh(record: dict | None, grunnlag: str, item: dict, data_dir: Path) -> bool:
    """The proposal was written for this text, under the current writer rules."""
    return (record is not None and record.get("grunnlag") == grunnlag
            and (record.get("meta") or {}).get("prompt_versjon") == skribent.TITTEL_PROMPT_VERSJON
            and not _sources_newer(item, data_dir, path(item, data_dir)))


def _needs_check(record: dict) -> bool:
    """A proposal worth checking whose check is missing or from older checker rules.
    One that breaks the hard limits is not checked: the redaktør drops it anyway."""
    if not record.get("tittel") or redaktor.tittel_brudd(record["tittel"], record.get("kort_overskrift", "")):
        return False
    check = record.get("sjekk")
    return (not check or check.get("tittel") != record["tittel"]
            or (check.get("meta") or {}).get("prompt_versjon") != faktasjekker.TITTEL_PROMPT_VERSJON)


FORSOK = 2  # the first headline, and one new try after the fact-check rejects it


def _ok(check: dict) -> bool:
    return (check.get("stottet") is True and check.get("noytral") is True
            and check.get("gjentar_kortet") is False and not check.get("personvern"))


def _ny_runde(record: dict) -> bool:
    """The check rejected the headline (not blocked: that's no verdict), and the writer hasn't had its new try."""
    check = record.get("sjekk") or {}
    return (bool(record.get("tittel")) and check.get("tittel") == record["tittel"]
            and not (check.get("blocked") or "").strip() and not _ok(check)
            and record.get("forsok", 1) < FORSOK)


def trenger(item: dict, data_dir: Path = config.DATA_DIR) -> bool:
    """True if this sak has a publishable edited article without a current, checked headline decision."""
    inputs = _inputs(item, data_dir)
    if inputs is None:
        return False
    art, headline = inputs
    record = read_json(path(item, data_dir))
    if not _fresh(record, redaktor.tittel_grunnlag(headline, art["avsnitt"]), item, data_dir):
        return True
    return _needs_check(record) or _ny_runde(record)


def lag(item: dict, data_dir: Path = config.DATA_DIR, llm=None, sjekk_llm=None) -> str:
    """Write (if needed) and check (if needed) one article's headline. Returns a short status."""
    inputs = _inputs(item, data_dir)
    if inputs is None:
        return "ingen publiserbar artikkel"
    art, headline = inputs
    out = path(item, data_dir)
    grunnlag = redaktor.tittel_grunnlag(headline, art["avsnitt"])
    record = read_json(out)
    if not _fresh(record, grunnlag, item, data_dir):
        forslag = skribent.skriv_tittel(art, headline, llm)
        record = {"nr": art.get("nr", ""), **forslag, "kort_overskrift": headline, "sjekk": None,
                  "grunnlag": grunnlag, "meta": _meta(llm)}
        write_json(out, record)  # saved before the check, so a failed check only redoes the check
    if not record.get("tittel"):
        return f"ingen tittel: {record.get('blocked') or 'tom'}"
    if brudd := redaktor.tittel_brudd(record["tittel"], headline):
        return f"bryter grensene: {'; '.join(brudd)}"
    if _needs_check(record):
        record["sjekk"] = faktasjekker.sjekk_tittel(item, record["tittel"], art, headline, data_dir, sjekk_llm)
        write_json(out, record)
    if _ny_runde(record):
        avvist = {"tittel": record["tittel"], "begrunnelse": record["sjekk"].get("begrunnelse", "")}
        forslag = skribent.skriv_tittel(art, headline, llm, avvist=avvist)
        record = {**record, **forslag, "sjekk": None, "forsok": record.get("forsok", 1) + 1,
                  "forrige": {"tittel": avvist["tittel"], "sjekk": record["sjekk"]}, "meta": _meta(llm)}
        write_json(out, record)  # as above: a failed check only redoes the check
        if not record.get("tittel"):
            return f"ingen tittel etter ny runde: {record.get('blocked') or 'tom'}"
        if brudd := redaktor.tittel_brudd(record["tittel"], headline):
            return f"bryter grensene etter ny runde: {'; '.join(brudd)}"
        record["sjekk"] = faktasjekker.sjekk_tittel(item, record["tittel"], art, headline, data_dir, sjekk_llm)
        write_json(out, record)
    check = record["sjekk"]
    if (check.get("blocked") or "").strip():
        return f"blocked: {check['blocked'].strip()}"
    runde = " (ny runde)" if record.get("forsok", 1) > 1 else ""
    return ("støttet" if _ok(check) else "ikke støttet") + runde


def run(items: list[dict], data_dir: Path = config.DATA_DIR, llm=None, sjekk_llm=None, parallel: int = 3) -> dict:
    return run_parallel(lambda item: lag(item, data_dir, llm, sjekk_llm), items, parallel)


def _meta(llm) -> dict:
    return {"modell": llm.name, "prompt_versjon": skribent.TITTEL_PROMPT_VERSJON,
            "tid": datetime.now(timezone.utc).isoformat(timespec="seconds")}
