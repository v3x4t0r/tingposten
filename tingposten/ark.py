"""Arbeidsark: one row per sak in a meeting, for sorting by hand.

The pre-filled columns are the score signals from the plan, read off the
documents (money, reach, legal weight, disagreement). The person fills in
the last columns: front page / card / noise, and whether a resident would
notice. The sheet stays in data/ (not in git): saksframlegg can name
private persons.
"""

import csv
import logging
from pathlib import Path

from . import config, pdf
from .saksframlegg import find_amounts

log = logging.getLogger(__name__)

# Title words that hint at high legal weight (plan: "Juridisk vekt").
HEAVY = (
    "reguleringsplan",
    "områderegulering",
    "kommuneplan",
    "kommunedelplan",
    "forskrift",
    "vedtekter",
    "budsjett",
    "økonomiplan",
    "strategi",
)

COLUMNS = [
    "nr",
    "tittel",
    "type",
    "beløp i vedtaket (MNOK)",
    "største beløp i saksframlegg (MNOK)",
    "beløp i teksten",
    "bydeler",
    "hjemler",
    "tung sakstype",
    "votering",
    "innstilling falt",
    "største tapende forslag",
    "antall talere",
    "hva saken gjelder",
    "DIN VURDERING (forside/kort/støy)",
    "MERKER INNBYGGERE DET? (1-5)",
    "KOMMENTAR",
    "saksframlegg",
    "protokoll",
]


def build(items: list[dict], pdf_dir: Path, parse_protokoll=None, parse_saksframlegg=None) -> list[dict]:
    if parse_protokoll is None:
        from .protokoll import parse_pdf as parse_protokoll
    if parse_saksframlegg is None:
        from .saksframlegg import parse_pdf as parse_saksframlegg
    rows = []
    for item in sorted(items, key=lambda i: i.get("seq") or 0):
        if pdf.is_noise(item):
            continue
        p = _parse(parse_protokoll, pdf_dir / pdf.filename(item, "protokoll"))
        s = _parse(parse_saksframlegg, pdf_dir / pdf.filename(item, "saksframlegg"))
        rows.append(row(item, p, s))
    return rows


def _parse(fn, path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return fn(path)
    except Exception:
        log.exception("could not parse %s", path)
        return {}


def row(item: dict, p: dict, s: dict) -> dict:
    # The adopted vedtak is the safer money signal: the saksframlegg's largest
    # amount is often context (the whole sector, a state project).
    i_vedtak = _largest(find_amounts(p.get("vedtak", ""), p.get("vedtak_side") or 1))
    i_saken = _largest(s.get("belop") or [])
    tittel = item.get("tittel", "")
    tapt = [v["stemmer"] for v in p.get("voteringer", []) if v["resultat"] == "falt" and v["stemmer"]]
    return {
        "nr": item.get("referansenr") or "",
        "tittel": tittel,
        "type": item.get("kategori", ""),
        "beløp i vedtaket (MNOK)": _mnok(i_vedtak["kroner"]) if i_vedtak else "",
        "største beløp i saksframlegg (MNOK)": _mnok(i_saken["kroner"]) if i_saken else "",
        "beløp i teksten": " | ".join(
            ([f"vedtak: {i_vedtak['tekst']}"] if i_vedtak else [])
            + ([f"saksframlegg s. {i_saken['side']}: {i_saken['tekst']}"] if i_saken else [])
        ),
        "bydeler": ", ".join(s.get("bydeler", [])),
        "hjemler": "; ".join(h["tekst"] for h in s.get("hjemler", [])[:3]),
        "tung sakstype": "ja" if any(w in tittel.lower() for w in HEAVY) else "",
        "votering": votering(p),
        "innstilling falt": "ja" if p.get("innstilling_falt") else "",
        "største tapende forslag": max(tapt) if tapt else "",
        "antall talere": len(p["talere"]) if p.get("talere") else "",
        "hva saken gjelder": _first_sentences(_summary(s), 2),
        "DIN VURDERING (forside/kort/støy)": "",
        "MERKER INNBYGGERE DET? (1-5)": "",
        "KOMMENTAR": "",
        "saksframlegg": pdf.url(item, "saksframlegg") or "",
        "protokoll": pdf.url(item, "protokoll") or "",
    }


def _largest(amounts: list[dict]) -> dict | None:
    return max(amounts, key=lambda b: b["kroner"], default=None)


def _summary(s: dict) -> str:
    # Interpellasjoner and some saker have no "Hva saken gjelder" heading;
    # their summary is the untitled first section.
    if s.get("hva_saken_gjelder"):
        return s["hva_saken_gjelder"]
    return next((sec["tekst"] for sec in s.get("seksjoner", []) if not sec["overskrift"]), "")


def votering(p: dict) -> str:
    """'enstemmig', '36–31', 'vedtatt med 36', or 'enstemmig, forslag falt'."""
    votes = p.get("voteringer") or []
    if not votes:
        return ""
    if p.get("enstemmig"):
        return "enstemmig"
    adopted = [
        v for v in votes
        if v["resultat"] == "vedtatt" and not v["forslag"].lower().startswith("merknad")
    ]
    split = [v for v in adopted if not v["enstemmig"] and v["stemmer"]]
    if not split:
        return "enstemmig, forslag falt" if adopted else "alle forslag falt"
    narrowest = min(split, key=lambda v: v["stemmer"])
    if p.get("til_stede"):
        return f"{narrowest['stemmer']}–{p['til_stede'] - narrowest['stemmer']}"
    return f"vedtatt med {narrowest['stemmer']}"


def _mnok(kroner: float) -> str:
    return f"{kroner / 1e6:.1f}".replace(".", ",")


def _first_sentences(text: str, n: int) -> str:
    text = " ".join(text.split())
    end, count = 0, 0
    while count < n:
        i = text.find(". ", end)
        if i < 0:
            return text
        end, count = i + 2, count + 1
    return text[:end].strip()


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Semicolons and a BOM so Norwegian Excel/LibreOffice open it in columns.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def default_path(utvalg: str, dato: str) -> Path:
    return config.DATA_DIR / "ark" / f"{utvalg}-{dato}.csv"
