"""Debattinnlegg: op-eds readers send by e-mail, published by the editor on the Debatt page.

One file per innlegg in data/bergen/innlegg/<dato>-<slug>.txt: a few "key: value" lines,
a line with "---", then the text, paragraphs separated by a blank line.

    tittel: Bybanen må til Åsane nå
    forfatter: Kari Nordmann
    om: lærer, bor i Åsane           (optional: who the writer is)
    dato: 2026-11-01
    sak: 900102/2026-10-29/1187-26   (optional: the sak the innlegg answers)
    ---
    Første avsnitt …

    Andre avsnitt …

The editor's rules (owner, 1.11.2026): full name, the writer's own text, the editor
decides what is published and may shorten. Nothing here is written or checked by a
model: the text is the writer's, as the editor approved it.
"""

import re
import unicodedata
from datetime import date
from pathlib import Path

from . import config

FELT = ("tittel", "forfatter", "om", "dato", "sak")
MAKS_TEGN = 4000   # what the Debatt page asks for; the editor may still take a longer one


def mappe(data_dir: Path | None = None) -> Path:
    return (data_dir or config.DATA_DIR) / "innlegg"


def slug(tekst: str) -> str:
    t = unicodedata.normalize("NFKD", tekst.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a"))
    t = re.sub(r"[^a-z0-9]+", "-", t.encode("ascii", "ignore").decode()).strip("-")
    return t[:60].rstrip("-") or "innlegg"


def les(path: Path) -> dict:
    hode, skille, kropp = path.read_text(encoding="utf-8").partition("\n---\n")
    if not skille:
        raise ValueError(f"{path.name}: mangler linjen «---» mellom feltene og teksten")
    felt = {}
    for linje in hode.splitlines():
        if linje.strip():
            k, _, v = linje.partition(":")
            if k.strip() in FELT:
                felt[k.strip()] = v.strip()
    for k in ("tittel", "forfatter", "dato"):
        if not felt.get(k):
            raise ValueError(f"{path.name}: mangler «{k}»")
    avsnitt = [" ".join(a.split()) for a in re.split(r"\n\s*\n", kropp.strip()) if a.strip()]
    if not avsnitt:
        raise ValueError(f"{path.name}: ingen tekst")
    navn = path.stem
    return {
        "slug": navn,
        "key": f"innlegg/{navn}",
        "side": f"bergen/debatt/{navn}/",
        "tittel": felt["tittel"],
        "forfatter": felt["forfatter"],
        "om": felt.get("om") or "",
        "dato": date.fromisoformat(felt["dato"]),
        "sak": felt.get("sak") or None,
        "avsnitt": avsnitt,
    }


def last(data_dir: Path | None = None) -> list[dict]:
    """Every innlegg, newest first."""
    d = mappe(data_dir)
    innlegg = [les(p) for p in sorted(d.glob("*.txt"))] if d.is_dir() else []
    return sorted(innlegg, key=lambda i: (i["dato"], i["slug"]), reverse=True)


def ny(tittel: str, forfatter: str, tekst: str, om: str = "", dato: date | None = None, sak: str | None = None,
       data_dir: Path | None = None) -> Path:
    """Write a new innlegg file and return its path. The editor builds and publishes as usual."""
    tittel, forfatter = " ".join(tittel.split()), " ".join(forfatter.split())
    if not tittel or not forfatter or not tekst.strip():
        raise ValueError("tittel, forfatter og tekst må fylles ut")
    if len(forfatter.split()) < 2:
        raise ValueError("debattinnlegg trykkes med fullt navn (fornavn og etternavn)")
    dato = dato or date.today()
    d = mappe(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{dato.isoformat()}-{slug(tittel)}.txt"
    if path.exists():
        raise FileExistsError(f"{path} finnes allerede")
    hode = [f"tittel: {tittel}", f"forfatter: {forfatter}"]
    if om.strip():
        hode.append(f"om: {' '.join(om.split())}")
    hode.append(f"dato: {dato.isoformat()}")
    if sak:
        hode.append(f"sak: {sak}")
    avsnitt = [" ".join(a.split()) for a in re.split(r"\n\s*\n", tekst.strip()) if a.strip()]
    path.write_text("\n".join(hode) + "\n---\n" + "\n\n".join(avsnitt) + "\n", encoding="utf-8")
    les(path)  # fails loudly if the file can't be read back
    return path
