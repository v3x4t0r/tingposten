"""Rettelser: the editor's visible corrections to a published sak.

The editorial statement promises that a correction is visible: the sak gets
a note saying what was changed and when. The pipeline may write a card or a
background article again (the sources change, a prompt is bumped), so a
correction is never an edit of redigert/ or artikler-redigert/. It lives in
its own file, applied when the site is built:

    data/bergen/rettelser/<utvalg>/<dato>/<tag>.json   (pdf.key, as the card)

The file is a list, applied in date order (the file's order within a day):

    [{"dato": "2026-10-30",
      "merknad": "Vi skrev 45 boliger. Riktig er 54.",  # shown on the page, as the editor wrote it
      "felt": "hva_endres",                            # optional: what is changed
      "tekst": "…",                                    # the new text; "" removes it
      "kilder": [{"dok": "saksframlegg", "side": 5}],  # optional: the new text's pages
      "var": "…"}]                                     # the text it replaced (`tingposten rett` writes it)

Targets: "overskrift", a card field (ingress, hva_endres, hvem_pavirkes,
hvorfor, status, nar_og_hvor) and "artikkel:<n>" (the body's paragraph n,
1-based, numbered as in the edited article). On an older card without an
ingress the dek is the first sentence of hva_endres: correct hva_endres and
the dek follows. Without "felt" a correction is a note only. A target keeps its sources unless
the correction gives "kilder".

Every note is shown, also when its change can't be made: the target is gone,
or (with "var") its text is no longer the text the correction replaced,
because the pipeline wrote the card again. The page then shows the
pipeline's text under the note, and the build logs a warning. An entry
without a date or a note is never applied: no change without a visible note.
"""

import copy
import json
import logging
import re
from datetime import date
from pathlib import Path

from . import pdf

log = logging.getLogger(__name__)

FELT = ("ingress", "hva_endres", "hvem_pavirkes", "hvorfor", "status", "nar_og_hvor")
DOKUMENTER = ("saksframlegg", "protokoll")
_AVSNITT = re.compile(r"artikkel:([1-9]\d*)")
# "https://tingposten.no/bergen/sak/571424/2026-09-23/266-26/", "bergen/sak/…" or the key itself.
_SAK = re.compile(r"(?:^|/)(\d+)/(\d{4}-\d{2}-\d{2})/([A-Za-z0-9._-]+)/?$")


def sti(data_dir: Path, key: str) -> Path:
    return data_dir / "rettelser" / f"{key}.json"


def gyldig_felt(felt: str) -> bool:
    return felt in ("overskrift", *FELT) or bool(_AVSNITT.fullmatch(felt))


def _ord(text) -> str:
    return " ".join(str(text or "").split())


def les(data_dir: Path, key: str) -> list[dict]:
    """The sak's corrections, in date order. An unreadable file or entry is
    skipped with a warning: the build never stops for one."""
    path = sti(data_dir, key)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("rettelser %s: kan ikke leses (%s), ingen rettelser vist", path, e)
        return []
    if not isinstance(raw, list):
        log.warning("rettelser %s: skal være en liste, ingen rettelser vist", path)
        return []
    out = []
    for n, r in enumerate(raw, start=1):
        try:
            dato = date.fromisoformat(r["dato"])
            merknad = _ord(r["merknad"])
        except (TypeError, KeyError, ValueError):
            log.warning("rettelse nr. %d i %s mangler dato eller merknad, brukes ikke", n, path)
            continue
        if not merknad:
            log.warning("rettelse nr. %d i %s har tom merknad, brukes ikke", n, path)
            continue
        out.append({**r, "dato": dato, "merknad": merknad})
    return sorted(out, key=lambda r: r["dato"])


def _kilder(kilder, hvor: str) -> list[dict]:
    """The correction's sources, in the card's shape without "felt"."""
    out = []
    for k in kilder if isinstance(kilder, list) else []:
        if isinstance(k, dict) and k.get("dok") in DOKUMENTER and isinstance(k.get("side"), int) and k["side"] >= 1:
            out.append({"dok": k["dok"], "side": k["side"]})
        else:
            log.warning("%s: kilden %r er ikke {dok, side}, hoppes over", hvor, k)
    return out


class _Sak:
    """A card and an article as their files have them, with a getter and a
    setter per target. Paragraphs are numbered as the page numbers them before
    any correction (empty ones aren't shown), so "artikkel:3" stays the same
    paragraph when an earlier correction removed another."""

    def __init__(self, kort: dict | None, artikkel: dict | None):
        self.kort = copy.deepcopy(kort) if kort else None
        self.artikkel = copy.deepcopy(artikkel) if artikkel else None
        art = self.artikkel
        self.avsnitt = [a for a in (art.get("avsnitt") or []) if isinstance(a, dict) and _ord(a.get("tekst"))] \
            if art and not art.get("avvist") else []

    def _kort_ok(self) -> bool:
        return bool(self.kort) and not self.kort.get("avvist")

    def hent(self, felt: str) -> str | None:
        """The target's text now; None if the page has no such target."""
        if felt == "overskrift":
            return _ord(self.kort.get("overskrift")) if self._kort_ok() else None
        if felt in FELT:
            return _ord((self.kort.get("felt") or {}).get(felt)) if self._kort_ok() else None
        if m := _AVSNITT.fullmatch(felt):
            n = int(m[1])
            return _ord(self.avsnitt[n - 1].get("tekst")) if n <= len(self.avsnitt) else None
        return None

    def sett(self, felt: str, tekst: str, kilder: list[dict] | None) -> None:
        if felt == "overskrift":
            self.kort["overskrift"] = tekst
        elif felt in FELT:
            self.kort.setdefault("felt", {})[felt] = tekst
            if kilder is not None:
                self.kort["kilder"] = [k for k in self.kort.get("kilder") or [] if k.get("felt") != felt] + \
                    [{"felt": felt, **k} for k in kilder]
        elif m := _AVSNITT.fullmatch(felt):
            a = self.avsnitt[int(m[1]) - 1]
            a["tekst"] = tekst
            a["rettet"] = True
            if kilder is not None:
                a["kilder"] = kilder


def bruk(rettelser: list[dict], kort: dict | None, artikkel: dict | None,
         key: str = "") -> tuple[dict | None, dict | None, list[dict]]:
    """Copies of the card and the article with the corrections made, and the
    notes to show: {"dato", "merknad", "felt", "brukt"}, in date order.
    A corrected paragraph gets "rettet": True. The inputs are not changed."""
    sak = _Sak(kort, artikkel)
    notes = []
    for r in rettelser:
        felt = r.get("felt") or None
        brukt = False
        if felt is not None:
            hvor = f"rettelse {key} {r['dato']} ({felt})"
            gyldig = isinstance(felt, str) and gyldig_felt(felt)
            naa = sak.hent(felt) if gyldig else None
            tekst = r.get("tekst")
            if not gyldig:
                log.warning("%s: ukjent felt, bare merknaden vises", hvor)
            elif not isinstance(tekst, str):
                log.warning("%s: mangler tekst, bare merknaden vises", hvor)
            elif naa is None:
                log.warning("%s: finnes ikke på saken (skrevet på nytt?), bare merknaden vises", hvor)
            elif "var" in r and _ord(r["var"]) != naa:
                log.warning("%s: teksten er endret siden rettelsen (skrevet på nytt?), bare merknaden vises. "
                            "Se over saken og rett på nytt om feilen er der fortsatt", hvor)
            else:
                # No usable sources given: the target keeps its own.
                sak.sett(felt, _ord(tekst), (_kilder(r["kilder"], hvor) or None) if "kilder" in r else None)
                brukt = True
        notes.append({"dato": r["dato"], "merknad": r["merknad"], "felt": felt, "brukt": brukt})
    return sak.kort, sak.artikkel, notes


# --- `tingposten rett` ---------------------------------------------------------

def sak_key(arg: str) -> str:
    """'571424/2026-09-23/266-26' from a sak page's URL, its path or the key."""
    path = (arg or "").strip().split("?", 1)[0].split("#", 1)[0]
    path = path.removesuffix("index.html")
    m = _SAK.search(path)
    if not m:
        raise ValueError(f"forstår ikke «{arg}»: bruk sakens adresse (…/bergen/sak/571424/2026-09-23/266-26/) "
                         "eller nøkkelen 571424/2026-09-23/266-26")
    return "/".join(m.groups())


def _finn_sak(data_dir: Path, key: str) -> dict:
    utvalg, dato, tag = key.split("/")
    store = data_dir / "agendapunkter" / f"{utvalg}.json"
    items = json.loads(store.read_text()).values() if store.exists() else []
    for item in items:
        if item.get("moteDato") == dato and pdf.tag(item) == tag:
            return item
    raise ValueError(f"fant ingen sak {key} i {store}")


def _les_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def ny(data_dir: Path, key: str, merknad: str, felt: str | None = None, tekst: str | None = None,
       kilder: list[dict] | None = None, dato: date | None = None) -> dict:
    """Append a correction to the sak's file and return it. Checked against
    the card and article as the page shows them now (earlier corrections
    made): the target must exist. Raises ValueError, in Norwegian, otherwise."""
    _finn_sak(data_dir, key)
    merknad = _ord(merknad)
    if not merknad:
        raise ValueError("merknaden kan ikke være tom: skriv hva som var feil og hva som er riktig")
    entry = {"dato": (dato or date.today()).isoformat(), "merknad": merknad}
    if felt is None:
        if tekst is not None or kilder:
            raise ValueError("--tekst og --kilde trenger --felt: hva skal rettes?")
    else:
        if felt == "dek":
            raise ValueError("ingressen rettes som feltet ingress (på eldre kort uten ingress: rett hva_endres, "
                             "så følger ingressen med)")
        if not gyldig_felt(felt):
            raise ValueError(f"ukjent felt «{felt}»: bruk overskrift, {', '.join(FELT)} eller artikkel:<n>")
        if tekst is None:
            raise ValueError("--felt trenger --tekst (ny tekst, eller --tekst '' for å fjerne)")
        if kilder and felt == "overskrift":
            raise ValueError("overskriften har ingen kildelenker: dropp --kilde")
        if kilder and not _ord(tekst):
            raise ValueError("en fjernet tekst har ingen kilder: dropp --kilde")
        kort = _les_json(data_dir / "redigert" / f"{key}.json")
        art = _les_json(data_dir / "artikler-redigert" / f"{key}.json")
        tidligere = les(data_dir, key)
        kort, art, _ = bruk(tidligere, kort, art, key)
        naa = (sak := _Sak(kort, art)).hent(felt)
        if naa is None:
            if not felt.startswith("artikkel"):
                grunn = "saken har ikke noe publisert kort" if not sak._kort_ok() else "feltet er tomt på dette kortet"
            elif not sak.avsnitt:
                grunn = "saken har ingen brødtekst"
            else:
                grunn = f"brødteksten har {len(sak.avsnitt)} avsnitt"
            raise ValueError(f"«{felt}» finnes ikke på siden: {grunn}")
        if _ord(tekst) == naa:
            raise ValueError(f"«{felt}» har allerede den teksten")
        entry.update({"felt": felt, "tekst": _ord(tekst)})
        if kilder:
            entry["kilder"] = kilder
        entry["var"] = naa
    path = sti(data_dir, key)
    try:
        alle = json.loads(path.read_text()) if path.exists() else []
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} kan ikke leses ({e}): rett filen for hånd først") from None
    if not isinstance(alle, list):
        raise ValueError(f"{path} er ikke en liste: rett filen for hånd først")
    alle.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(alle, ensure_ascii=False, indent=1) + "\n")
    tmp.replace(path)
    return entry


def kilde(arg: str) -> dict:
    """'saksframlegg:5' → {"dok": "saksframlegg", "side": 5} (for argparse)."""
    dok, _, side = arg.partition(":")
    if dok not in DOKUMENTER or not side.isdigit() or int(side) < 1:
        raise ValueError(f"kilden «{arg}» skal være dokument:side, f.eks. saksframlegg:5 eller protokoll:2")
    return {"dok": dok, "side": int(side)}
