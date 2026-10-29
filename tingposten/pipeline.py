"""Pipeline: from fetched metadata to publishable cards and articles.

skribent → faktasjekker → klassifiserer → redaktør for every decided sak
whose protokoll is on disk; then, for the top share of each meeting's
decided saker, an article goes through the same three roles (write →
independent check → delete-only edit). A step only runs for saker whose
output is missing or older than its inputs, so a rewritten card is checked
again and a stopped run picks up where it left off. Building and publishing
the site are separate commands.

A card the redaktør rejects for limit or headline problems is rewritten
once, automatically, with the rejection as feedback, and goes through check
and edit again in the same run. Privacy rejections wait for a human.

PDFs are fetched only when config.ALLOW_PDF_FETCH is set (robots.txt; see
config). Until then missing PDFs are reported, not downloaded.
"""

import json
import logging
import math
from collections import defaultdict
from pathlib import Path

from . import config, faktasjekker, klassifiserer, pdf, redaktor, score, skribent
from .llm import ClaudeCLI, CodexCLI, read_json

log = logging.getLogger(__name__)

STEG = ("skriv", "sjekk", "klassifiser", "rediger", "artikkel", "artikkelsjekk", "artikkelrediger")
MAKS_OMSKRIVINGER = 1


def saker(data_dir: Path = config.DATA_DIR, utvalg: str | None = None, dato: str | None = None) -> list[dict]:
    """Decided, non-noise saker from the deciding Tier 1 organs."""
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
            if sources[0].exists() and _stale(kort, sources):
                out.append(i)
        elif step in ("artikkel", "artikkelsjekk", "artikkelrediger"):
            if step == "artikkel" and redigert.exists() and _stale(artikkel, [redigert]):
                out.append(i)
            elif step == "artikkelsjekk" and artikkel.exists() and _stale(_file(data_dir, "artikkelsjekk", i),
                                                                          [artikkel, redigert]):
                out.append(i)
            elif step == "artikkelrediger" and artikkel.exists():
                out.append(i)
        elif not kort.exists():
            continue
        elif step == "sjekk" and _stale(_file(data_dir, "sjekk", i), [kort]):
            out.append(i)
        elif step == "klassifiser" and _stale(_file(data_dir, "signaler", i), [kort]):
            out.append(i)
        elif step == "rediger":
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


def omskriv_kandidater(items: list[dict], data_dir: Path) -> dict[str, str]:
    """Rejected cards the writer may fix itself (once): key → feedback."""
    out = {}
    for i in items:
        r = read_json(_file(data_dir, "redigert", i))
        card = read_json(_file(data_dir, "kort", i))
        if not r or not card or not redaktor.omskrivbar(r):
            continue
        if int((card.get("meta") or {}).get("omskrevet", 0)) >= MAKS_OMSKRIVINGER:
            continue
        out[pdf.key(i)] = tilbakemelding(r)
    return out


def tilbakemelding(redigert: dict) -> str:
    """What the writer needs to know: the rejection reasons and the sentences
    the editor deleted, with why."""
    lines = ["Avvist fordi: " + (redigert.get("grunn") or "ukjent grunn") + "."]
    for s in redigert.get("slettet", []):
        lines.append(f"- Slettet fra {s.get('felt', 'kortet')}: «{s.get('setning', '')}» — {s.get('grunn', '')}")
    return "\n".join(lines)


def mangler_pdf(items: list[dict], data_dir: Path = config.DATA_DIR) -> list[dict]:
    return [i for i in items if not _pdf(data_dir, i, "protokoll").exists()]


def kjor(data_dir: Path = config.DATA_DIR, utvalg: str | None = None, dato: str | None = None,
         steg: tuple[str, ...] = STEG, parallel: int = 3, llms: dict | None = None) -> dict:
    """Run the steps in order; returns per-step counts."""
    items = saker(data_dir, utvalg, dato)
    if missing := mangler_pdf(items, data_dir):
        hint = "" if config.ALLOW_PDF_FETCH else " (automatic download is off; see `tingposten pdf-urler`)"
        log.info("%d of %d saker have no protokoll on disk yet%s", len(missing), len(items), hint)
    llms = llms or {
        "skriv": ClaudeCLI(config.MODELL_SKRIBENT),
        "sjekk": CodexCLI(config.MODELL_FAKTASJEKKER),
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
    return result
