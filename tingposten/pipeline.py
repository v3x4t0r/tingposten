"""Pipeline: from fetched metadata to publishable cards and articles.

skribent → faktasjekker → klassifiserer → redaktør for every decided sak
whose protokoll is on disk; then, for the top share of each meeting's
decided saker, an article goes through the same three roles (write →
independent check → delete-only edit). A step only runs for saker whose
output is missing or older than its inputs, so a rewritten card is checked
again and a stopped run picks up where it left off. Building and publishing
the site are separate commands.

Cards and articles written under earlier writer rules (skribent.PROMPT_VERSJON)
stay as published: they are not rewritten, re-edited or rewritten after a
rejection for the new rules, so the model quota goes to new saker (owner,
30.10.2026). A sak whose documents change is written again under the current
rules, like any stale card.

A card the redaktør rejects for limit or headline problems is rewritten
once, automatically, with the rejection as feedback, and goes through check
and edit again in the same run; so is an article rejected for length or for
too little new, or for a paragraph that doesn't fit its role. Privacy
rejections wait for a human.

PDFs are fetched only when config.ALLOW_PDF_FETCH is set (robots.txt; see
config). Until then missing PDFs are reported, not downloaded.
"""

import json
import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import config, faktasjekker, klassifiserer, pdf, protokoll, redaktor, score, skribent, status
from .llm import ClaudeCLI, CodexCLI, MedReserve, read_json

log = logging.getLogger(__name__)

STEG = ("skriv", "sjekk", "klassifiser", "rediger", "artikkel", "artikkelsjekk", "artikkelrediger")
MAKS_OMSKRIVINGER = 1


def fra_dato(idag: date | None = None) -> str:
    """The oldest meeting date new cards and PDFs are made for (config.TILBAKE_DAGER)."""
    return ((idag or date.today()) - timedelta(days=config.TILBAKE_DAGER)).isoformat()


def saker(data_dir: Path = config.DATA_DIR, utvalg: str | None = None, dato: str | None = None,
          fra: str | None = None) -> list[dict]:
    """Decided, non-noise saker from the deciding Tier 1 organs (meetings on or after `fra`)."""
    out = []
    for uid in config.TIER1:
        if uid in config.INNSTILLENDE or (utvalg and uid != utvalg):
            continue
        path = data_dir / "agendapunkter" / f"{uid}.json"
        if not path.exists():
            continue
        out += [
            i for i in json.loads(path.read_text()).values()
            if i.get("vedtakFilId") and not pdf.is_noise(i) and (not dato or i.get("moteDato") == dato)
            and (not fra or (i.get("moteDato") or "") >= fra)
        ]
    return sorted(out, key=lambda i: (i["utvalgId"], i["moteDato"], i.get("seq") or 0))


def _file(data_dir: Path, step: str, item: dict) -> Path:
    return data_dir / step / f"{pdf.key(item)}.json"


def _pdf(data_dir: Path, item: dict, kind: str) -> Path:
    return data_dir / "pdf" / pdf.filename(item, kind)


def _stale(out: Path, inputs: list[Path]) -> bool:
    if not out.exists():
        return True
    mtime = out.stat().st_mtime
    return any(p.exists() and p.stat().st_mtime > mtime for p in inputs)


def protokoll_klar(item: dict, data_dir: Path = config.DATA_DIR) -> bool:
    """The protokoll on disk covers this meeting (see status.protokoll_klar)."""
    path = _pdf(data_dir, item, "protokoll")
    if not path.exists():
        return False
    try:
        return status.protokoll_klar(item, protokoll.parse_pdf(path))
    except Exception:  # an unreadable PDF is the writer's problem to report
        return True


def ufullstendige_protokoller(items: list[dict], data_dir: Path = config.DATA_DIR, idag=None,
                              dager: int = 30) -> list[dict]:
    """Saker whose protokoll on disk doesn't cover their meeting yet, for
    meetings in the last `dager` days: their protokoll should be fetched again."""
    grense = ((idag or date.today()) - timedelta(days=dager)).isoformat()
    return [i for i in items if (i.get("moteDato") or "") >= grense
            and _pdf(data_dir, i, "protokoll").exists() and not protokoll_klar(i, data_dir)]


def eldre_regler(path: Path) -> bool:
    """A card or article written under earlier writer rules (skribent.PROMPT_VERSJON),
    or from before they recorded their version. These stay as published: the
    pipeline doesn't redo them for new rules (see the module docstring)."""
    written = read_json(path) or {}
    if "overskrift" not in written and "avsnitt" not in written:
        return False  # nothing written yet (or not a card/article)
    return (written.get("meta") or {}).get("prompt_versjon") != skribent.PROMPT_VERSJON


def todo(step: str, items: list[dict], data_dir: Path) -> list[dict]:
    """The saker this step still has to process. ("artikkel" also needs the
    selection from velg_artikler; kjor passes only the selected saker.)"""
    out = []
    for i in items:
        kort = _file(data_dir, "kort", i)
        redigert = _file(data_dir, "redigert", i)
        artikkel = _file(data_dir, "artikler", i)
        sources = [_pdf(data_dir, i, "protokoll"), _pdf(data_dir, i, "saksframlegg")]
        if step == "skriv":
            if sources[0].exists() and _stale(kort, sources) and protokoll_klar(i, data_dir):
                out.append(i)
        elif step in ("artikkel", "artikkelsjekk", "artikkelrediger"):
            if step == "artikkel" and redigert.exists() and _stale(artikkel, [redigert]):
                out.append(i)
            elif step == "artikkelsjekk" and artikkel.exists() and _stale(_file(data_dir, "artikkelsjekk", i),
                                                                          [artikkel, redigert]):
                out.append(i)
            elif step == "artikkelrediger" and artikkel.exists() and not (
                    _file(data_dir, "artikler-redigert", i).exists() and eldre_regler(artikkel)):
                out.append(i)
        elif not kort.exists():
            continue
        elif step == "sjekk" and _stale(_file(data_dir, "sjekk", i), [kort]):
            out.append(i)
        elif step == "klassifiser" and _stale(_file(data_dir, "signaler", i), [kort]):
            out.append(i)
        elif step == "rediger" and not (redigert.exists() and eldre_regler(kort)):
            out.append(i)
    return out


def velg_artikler(items: list[dict], data_dir: Path = config.DATA_DIR, andel: float | None = None) -> list[dict]:
    """Per meeting: the top `andel` of its decided saker by score, among those
    whose edited card is publishable (at least one if any qualifies)."""
    andel = getattr(config, "ARTIKKEL_ANDEL", 0.25) if andel is None else andel
    meetings: dict[tuple, list[dict]] = defaultdict(list)
    for i in items:
        meetings[(i["utvalgId"], i["moteDato"])].append(i)
    chosen = []
    for meeting in meetings.values():
        by_nr = {i.get("referansenr") or pdf.tag(i): i for i in meeting}
        ranked = score.rank(meeting, data_dir / "pdf", data_dir=data_dir)
        decided = [r for r in ranked if (r.get("status") or {}).get("kode") == "vedtatt"]
        n = max(1, math.ceil(andel * len(decided))) if decided else 0
        eligible = []
        for r in decided:
            item = by_nr.get(r["nr"])
            card = read_json(_file(data_dir, "redigert", item)) if item else None
            if card and not card.get("avvist"):
                eligible.append(item)
        chosen += eligible[:n]
    return chosen


def omskriv_kandidater(items: list[dict], data_dir: Path, artikler: bool = False) -> dict[str, str]:
    """Rejected cards (or articles) the writer may fix itself, once: key → feedback."""
    edited, draft = ("artikler-redigert", "artikler") if artikler else ("redigert", "kort")
    tillatt = redaktor.OMSKRIVBARE_ARTIKKEL if artikler else redaktor.OMSKRIVBARE
    out = {}
    for i in items:
        r = read_json(_file(data_dir, edited, i))
        utkast = read_json(_file(data_dir, draft, i))
        if not r or not utkast or not redaktor.omskrivbar(r, tillatt) or eldre_regler(_file(data_dir, draft, i)):
            continue
        if int((utkast.get("meta") or {}).get("omskrevet", 0)) >= MAKS_OMSKRIVINGER:
            continue
        out[pdf.key(i)] = tilbakemelding(r)
    return out


def tilbakemelding(redigert: dict) -> str:
    """What the writer needs to know: the rejection reasons and the sentences
    the editor deleted, with why."""
    lines = ["Avvist fordi: " + (redigert.get("grunn") or "ukjent grunn") + "."]
    for s in redigert.get("slettet", []):
        hvor = f"avsnitt {s['avsnitt']}" if "avsnitt" in s else s.get("felt", "kortet")
        lines.append(f"- Slettet fra {hvor}: «{s.get('setning', '')}» — {s.get('grunn', '')}")
    return "\n".join(lines)


def mangler_pdf(items: list[dict], data_dir: Path = config.DATA_DIR) -> list[dict]:
    return [i for i in items if not _pdf(data_dir, i, "protokoll").exists()]


def pdf_som_mangler(items: list[dict], data_dir: Path = config.DATA_DIR) -> list[dict]:
    """Download targets (pdf.targets) for every saksframlegg/protokoll the saker
    still need: files not on disk, and protokoller that don't cover their meeting
    yet (marked "oppdater": fetched again over the old file)."""
    alle = pdf.targets(items)
    out = [t for t in alle if not (data_dir / "pdf" / t["filename"]).exists()]
    oppdater = {pdf.filename(i, "protokoll") for i in ufullstendige_protokoller(items, data_dir)}
    return out + [{**t, "oppdater": True} for t in alle if t["filename"] in oppdater]


def kjor(data_dir: Path = config.DATA_DIR, utvalg: str | None = None, dato: str | None = None,
         steg: tuple[str, ...] = STEG, parallel: int = 3, llms: dict | None = None, fra: str | None = None) -> dict:
    """Run the steps in order; returns per-step counts. Without `dato`, only
    meetings on or after `fra` (default: config.TILBAKE_DAGER back)."""
    items = saker(data_dir, utvalg, dato, None if dato else (fra or fra_dato()))
    if missing := mangler_pdf(items, data_dir):
        hint = "" if config.ALLOW_PDF_FETCH else " (automatic download is off; see `tingposten pdf-urler`)"
        log.info("%d of %d saker have no protokoll on disk yet%s", len(missing), len(items), hint)
    llms = llms or {
        "skriv": ClaudeCLI(config.MODELL_SKRIBENT),
        # Codex checks; when it can't answer, Claude (config.FAKTASJEKKER_RESERVE) does.
        "sjekk": MedReserve(CodexCLI(config.MODELL_FAKTASJEKKER),
                            ClaudeCLI(config.FAKTASJEKKER_RESERVE, config.FAKTASJEKKER_RESERVE_EFFORT)
                            if config.FAKTASJEKKER_RESERVE else None),
        "klassifiser": ClaudeCLI(config.MODELL_KLASSIFISERER),
    }
    llm = {**llms, "artikkel": llms.get("artikkel", llms.get("skriv")),
           "artikkelsjekk": llms.get("artikkelsjekk", llms.get("sjekk"))}
    runners = {
        "skriv": skribent.run, "sjekk": faktasjekker.run, "klassifiser": klassifiserer.run,
        "artikkel": skribent.run_artikkel, "artikkelsjekk": faktasjekker.run_artikkel,
    }
    result = {}

    def step_run(step: str, work: list[dict], **kw) -> dict:
        if step == "rediger":
            return redaktor.run(work, data_dir) if work else {}
        if step == "artikkelrediger":
            return redaktor.run_artikler(work, data_dir) if work else {}
        if not work:
            return {}
        log.info("%s: %d saker", step, len(work))
        # todo() already decided these need (re)doing, stale outputs included.
        return runners[step](work, data_dir, llm=llm[step], force=True, parallel=parallel, **kw)

    for step in STEG:
        if step not in steg:
            continue
        pool = velg_artikler(items, data_dir) if step == "artikkel" else items
        result[step] = step_run(step, todo(step, pool, data_dir))
        log.info("%s: %s", step, result[step] or "nothing to do")

        if step == "rediger" and "skriv" in steg:
            feedback = omskriv_kandidater(items, data_dir)
            if feedback:
                again = [i for i in items if pdf.key(i) in feedback]
                log.info("omskriv: %d avviste kort skrives på nytt", len(again))
                result["omskriv"] = step_run("skriv", again, feedback=feedback)
                for s in ("sjekk", "klassifiser", "rediger"):
                    if s in steg:
                        result[f"omskriv/{s}"] = step_run(s, todo(s, again, data_dir))
                log.info("omskriv: %s", {k: v for k, v in result.items() if k.startswith("omskriv")})

        if step == "artikkelrediger" and "artikkel" in steg:
            feedback = omskriv_kandidater(items, data_dir, artikler=True)
            if feedback:
                again = [i for i in items if pdf.key(i) in feedback]
                log.info("omskriv artikkel: %d avviste artikler skrives på nytt", len(again))
                result["omskriv_artikkel"] = step_run("artikkel", again, feedback=feedback)
                for s in ("artikkelsjekk", "artikkelrediger"):
                    if s in steg:
                        result[f"omskriv_artikkel/{s}"] = step_run(s, todo(s, again, data_dir))
                log.info("omskriv artikkel: %s",
                         {k: v for k, v in result.items() if k.startswith("omskriv_artikkel")})
    return result
