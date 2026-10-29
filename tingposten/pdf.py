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


def tag(item: dict) -> str:
    """Filesystem-safe id of a sak within its meeting, e.g. '238-26'."""
    return _safe(item.get("referansenr") or item.get("saksnr") or item["id"])


def key(item: dict) -> str:
    """'<utvalgId>/<moteDato>/<tag>': where a sak's files live under each step's
    directory (pdf/, kort/, sjekk/, …). Case numbers are only unique per organ."""
    return f"{item.get('utvalgId', 'ukjent')}/{item.get('moteDato', 'udatert')}/{tag(item)}"


def filename(item: dict, kind: str) -> str:
    """Local path of an item's PDF, relative to the pdf/ directory."""
    return f"{key(item)}_{kind}.pdf"


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


def build_script(targets: list[dict], dest: str = "pdf") -> str:
    """A polite bash script the USER runs to download PDFs.

    One file at a time, 5 s apart, identifiable User-Agent. Files already on
    disk are skipped, so an interrupted run can simply be started again, and
    the first refused download stops the whole script."""
    n = len(targets)
    lines = [
        "#!/usr/bin/env bash",
        f"# Last ned {n} PDF-er fra bergen.kommune.no. Kjør dette selv, fra din egen",
        "# tilkobling. Én fil om gangen, 5 s mellom, identifiserbar User-Agent.",
        "# Filer som alt finnes hoppes over, så et avbrutt løp kan startes på nytt.",
        "set -euo pipefail",
        f'UA={config.USER_AGENT!r}',
        f'DEST="${{1:-{dest}}}"',
        "trap 'echo \"Stoppet: serveren svarte ikke som ventet. Vent til i morgen og kjør igjen.\" >&2' ERR",
        "hent() {  # nr url fil",
        '  if [ -s "$DEST/$3" ]; then return; fi',
        f'  echo "[$1/{n}] $3"',
        '  mkdir -p "$(dirname "$DEST/$3")"',
        '  wget --quiet --user-agent="$UA" --tries=2 --timeout=60 -O "$DEST/$3.part" "$2"',
        '  mv "$DEST/$3.part" "$DEST/$3"',
        "  sleep 5",
        "}",
        "",
    ]
    for nr, t in enumerate(targets, start=1):
        lines += [f'# {t["kind"]}: {t["tittel"]}', f'hent {nr} {t["url"]!r} {t["filename"]!r}']
    return "\n".join(lines) + "\n"
