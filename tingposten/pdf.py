"""Build PDF download URLs from stored agenda metadata.

This module never fetches anything. robots.txt disallows /…/api/fil/*, so
the PDFs are downloaded by a person, for one meeting at a time, with the
script build_script() writes. We only turn the file IDs and slugs that are
already in the (allowed) agenda metadata into URLs and tidy filenames.
"""

import re

from . import config

# Titles filtered out before any model runs (plan: "Støyfilter på tittel").
# These never carry a real decision worth summarising.
NOISE = (
    "innkalling",
    "godkjenning av protokoll",
    "godkjenning av møteprotokoll",
    "muntlige spørsmål",
    "skriftlige spørsmål",
    "saker til gjennomsyn",
    "orienteringssak",
    "referatsak",
)


def is_noise(item: dict) -> bool:
    text = f"{item.get('kategori', '')} {item.get('tittel', '')}".lower()
    return any(word in text for word in NOISE)


def pdf_url(fil_id: str, slug: str | None) -> str:
    # The slug is cosmetic; the file ID resolves the document. Keep the slug
    # when we have it so the URL matches what the site itself serves.
    tail = f"{fil_id}/{slug}" if slug else fil_id
    return f"{config.API_BASE}/fil/bk360/{tail}"


KINDS = (
    ("saksframlegg", "innstillingFilId", "innstillingAlternativBeskrivelse"),
    ("protokoll", "vedtakFilId", "vedtakAlternativBeskrivelse"),
)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "sak"


def filename(item: dict, kind: str) -> str:
    """Local path of an item's PDF, relative to the pdf/ directory."""
    tag = _safe(item.get("referansenr") or item.get("saksnr") or item["id"])
    return f"{item.get('moteDato', 'udatert')}/{tag}_{kind}.pdf"


def url(item: dict, kind: str) -> str | None:
    _, fil_key, slug_key = next(k for k in KINDS if k[0] == kind)
    fil_id = item.get(fil_key)
    return pdf_url(str(fil_id), item.get(slug_key)) if fil_id else None


def targets(items: list[dict], drop_noise: bool = True) -> list[dict]:
    """One entry per existing saksframlegg/protokoll PDF across the items."""
    out = []
    for item in sorted(items, key=lambda i: (i.get("moteDato", ""), i.get("seq") or 0)):
        if drop_noise and is_noise(item):
            continue
        for kind, _, _ in KINDS:
            if u := url(item, kind):
                out.append(
                    {
                        "url": u,
                        "filename": filename(item, kind),
                        "tittel": item.get("tittel", ""),
                        "kind": kind,
                    }
                )
    return out


def build_script(targets: list[dict]) -> str:
    """A polite bash script the USER runs to download one meeting's PDFs."""
    lines = [
        "#!/usr/bin/env bash",
        "# Last ned PDF-ene for ETT møte. Kjør dette selv, fra din egen",
        "# tilkobling, når sperringen mot bergen.kommune.no er borte.",
        "# Én fil om gangen, 5 s mellom, identifiserbar User-Agent.",
        "set -euo pipefail",
        f'UA={config.USER_AGENT!r}',
        'DEST="${1:-pdf}"',
        "",
    ]
    for t in targets:
        path = f'$DEST/{t["filename"]}'
        lines += [
            f'# {t["kind"]}: {t["tittel"]}',
            f'mkdir -p "$(dirname "{path}")"',
            f'wget --user-agent="$UA" --tries=2 --timeout=60 -O "{path}" {t["url"]!r}',
            "sleep 5",
            "",
        ]
    return "\n".join(lines)
