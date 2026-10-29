"""Source text for the model steps: a sak's two PDFs, with page markers.

The models cite "dok + side", so every page is introduced by "--- side N ---"
exactly as the page numbers in the PDF viewer count them.
"""

import re
from pathlib import Path

import pymupdf

from . import config, pdf


def dokumenttekst(path: Path) -> str:
    with pymupdf.open(path) as doc:
        pages = [_tidy(page.get_text()) for page in doc]
    return "\n".join(f"--- side {i} ---\n{text}" for i, text in enumerate(pages, 1))


def kilder_for(item: dict, data_dir: Path = config.DATA_DIR) -> dict[str, str]:
    """{"saksframlegg": text, "protokoll": text}; "" for a missing PDF."""
    out = {}
    for kind, _, _ in pdf.KINDS:
        path = data_dir / "pdf" / pdf.filename(item, kind)
        out[kind] = dokumenttekst(path) if path.exists() else ""
    return out


def _tidy(text: str) -> str:
    # Generated PDFs pad lines with spaces and stack blank lines; both cost
    # tokens and carry nothing.
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
