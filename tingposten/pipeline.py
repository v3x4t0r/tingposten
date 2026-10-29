"""Pipeline: from fetched metadata to publishable cards.

skribent → faktasjekker → klassifiserer → redaktør, for every decided sak
whose protokoll is on disk. A step only runs for saker whose output is
missing or older than its inputs, so a rewritten card is checked again and a
stopped run picks up where it left off. Building and publishing the site are
separate commands.

PDFs are fetched only when config.ALLOW_PDF_FETCH is set (robots.txt; see
config). Until then missing PDFs are reported, not downloaded.
"""

import json
import logging
from pathlib import Path

from . import config, faktasjekker, klassifiserer, pdf, redaktor, skribent
from .llm import ClaudeCLI, CodexCLI

log = logging.getLogger(__name__)

STEG = ("skriv", "sjekk", "klassifiser", "rediger")


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
    """The saker this step still has to process."""
    out = []
    for i in items:
        kort = _file(data_dir, "kort", i)
        sources = [_pdf(data_dir, i, "protokoll"), _pdf(data_dir, i, "saksframlegg")]
        if step == "skriv":
            if sources[0].exists() and _stale(kort, sources):
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
    steps = {"skriv": skribent, "sjekk": faktasjekker, "klassifiser": klassifiserer}
    result = {}
    for step in STEG:
        if step not in steg:
            continue
        work = todo(step, items, data_dir)
        if step == "rediger":
            result[step] = redaktor.run(work, data_dir) if work else {}
        elif work:
            log.info("%s: %d saker", step, len(work))
            # todo() already decided these need (re)doing, stale outputs included.
            result[step] = steps[step].run(work, data_dir, llm=llms[step], force=True, parallel=parallel)
        else:
            result[step] = {}
        log.info("%s: %s", step, result[step] or "nothing to do")
    return result
