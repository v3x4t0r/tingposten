"""Source text for the model steps: a sak's two PDFs, with page markers, and
excerpts from its attachments.

The models cite "dok + side", so every page is introduced by "--- side N ---"
exactly as the page numbers in the PDF viewer count them; utdrag() keeps
only some of those pages. An attachment is never given whole: only the pages
the cheap reader picked (vedlegg.py), as "vedlegg-N".
"""

import copy
import json
import re
from pathlib import Path

import pymupdf

from . import config, pdf


def dokumenttekst(path: Path) -> str:
    with pymupdf.open(path) as doc:
        pages = [_tidy(page.get_text()) for page in doc]
    return "\n".join(f"--- side {i} ---\n{text}" for i, text in enumerate(pages, 1))


def kilder_for(item: dict, data_dir: Path = config.DATA_DIR) -> dict[str, str]:
    """{"saksframlegg": text, "protokoll": text}; "" for a missing PDF. Plus
    "vedlegg-N": the pages the attachment reader picked, under a short header, for
    each attachment it read."""
    out = {}
    for kind, _, _ in pdf.KINDS:
        path = data_dir / "pdf" / pdf.filename(item, kind)
        out[kind] = dokumenttekst(path) if path.exists() else ""
    digest = _read(data_dir / "vedleggsdigest" / f"{pdf.key(item)}.json")
    for v in (digest or {}).get("vedlegg", []):
        path = data_dir / "pdf" / pdf.vedlegg_fil(item, v["nr"])
        valgt = [s["side"] for s in v.get("sider", [])]
        if not path.exists() or not valgt:
            continue
        hode = (f"Vedlegg {v['nr']}: {v.get('navn', '')}\nAvsender: {v.get('avsender', 'ukjent')}. "
                f"{v.get('sammendrag', '')}\nBare sidene som betyr noe for saken er tatt med, av {v.get('antall_sider', '?')}.")
        out[f"vedlegg-{v['nr']}"] = hode + "\n" + utdrag(dokumenttekst(path), valgt)
    return out


def vedlegg_blokker(kilder: dict[str, str]) -> list[str]:
    """The attachment excerpts as fenced prompt blocks (VEDLEGG_1 …), in order."""
    from .llm import fence

    nr = sorted(int(k.split("-")[1]) for k in kilder if pdf.VEDLEGG_DOK.fullmatch(k))
    return [fence(f"VEDLEGG_{n}", kilder[f"vedlegg-{n}"]) for n in nr]


def med_dokumenter(schema: dict, kilder: dict[str, str]) -> dict:
    """The schema with every citation's "dok" enum widened to this sak's attachments."""
    doks = [k for k, _, _ in pdf.KINDS] + sorted((k for k in kilder if pdf.VEDLEGG_DOK.fullmatch(k)),
                                                key=lambda k: int(k.split("-")[1]))
    ut = copy.deepcopy(schema)

    def gaa(node):
        if isinstance(node, dict):
            if node.get("enum") == ["saksframlegg", "protokoll"]:
                node["enum"] = list(doks)
            for v in node.values():
                gaa(v)
        elif isinstance(node, list):
            for v in node:
                gaa(v)
    gaa(ut)
    return ut


def sider(tekst: str) -> list[int]:
    """The page numbers in a dokumenttekst(), in order."""
    return [int(m) for m in re.findall(r"(?m)^--- side (\d+) ---$", tekst)]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None


def utdrag(tekst: str, sider) -> str:
    """Only the given pages of a dokumenttekst(), each with its "--- side N ---" marker."""
    want = set(sider)
    marks = list(re.finditer(r"(?m)^--- side (\d+) ---$", tekst))
    parts = []
    for n, m in enumerate(marks):
        if int(m.group(1)) in want:
            end = marks[n + 1].start() if n + 1 < len(marks) else len(tekst)
            parts.append(tekst[m.start():end].rstrip())
    return "\n".join(parts)


def _tidy(text: str) -> str:
    # Generated PDFs pad lines with spaces and stack blank lines; both cost
    # tokens and carry nothing.
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
