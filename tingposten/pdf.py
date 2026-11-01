"""Build PDF download URLs from stored agenda metadata.

This module never fetches anything. robots.txt disallows /…/api/fil/*, so
the PDFs are downloaded by a person, for one meeting at a time, with the
script build_script() writes. We only turn the file IDs and slugs that are
already in the (allowed) agenda metadata into URLs and tidy filenames.
"""

import json
import re
import shlex
from pathlib import Path

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


# Attachments (vedlegg.py): the list Bergen's agendapunktfiler gave for a sak is stored as
# <data_dir>/vedlegg/<key>.json; attachment N is cited as dok "vedlegg-N" and saved as
# <key>_vedlegg-N.pdf.
VEDLEGG_DOK = re.compile(r"vedlegg-([1-9]\d*)")


def er_dok(dok) -> bool:
    """A document a citation may name: saksframlegg, protokoll or vedlegg-N."""
    return isinstance(dok, str) and (dok in {k for k, _, _ in KINDS} or bool(VEDLEGG_DOK.fullmatch(dok)))


def vedlegg_liste(item: dict, data_dir: Path = config.DATA_DIR) -> list[dict] | None:
    """The stored attachment list for a sak (None: never asked)."""
    path = Path(data_dir) / "vedlegg" / f"{key(item)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("vedlegg", [])
    except (OSError, ValueError):
        return None


def vedlegg_fil(item: dict, nr: int) -> str:
    return filename(item, f"vedlegg-{nr}")


def dok_url(item: dict, dok: str, data_dir: Path = config.DATA_DIR) -> str | None:
    """The kommune's own URL for a cited document, attachments included."""
    if m := VEDLEGG_DOK.fullmatch(dok or ""):
        v = next((v for v in vedlegg_liste(item, data_dir) or [] if v.get("nr") == int(m.group(1))), None)
        return v["url"] if v else None
    return url(item, dok) if dok in {k for k, _, _ in KINDS} else None


def vedlegg_targets(items: list[dict], data_dir: Path = config.DATA_DIR) -> list[dict]:
    """Download targets for the attachments of saker whose list is stored (same form as targets())."""
    out = []
    for item in items:
        for v in vedlegg_liste(item, data_dir) or []:
            out.append({"url": v["url"], "filename": vedlegg_fil(item, v["nr"]),
                        "tittel": f"{item.get('tittel', '')}: {v.get('navn', '')}", "kind": "vedlegg"})
    return out


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
        f"UA={shlex.quote(config.USER_AGENT)}",
        'DEST="${1:-}"',
        f'[ -n "$DEST" ] || DEST={shlex.quote(dest)}',
        "trap 'echo \"Stoppet: serveren svarte ikke som ventet. Vent til i morgen og kjør igjen.\" >&2' ERR",
        "hent() {  # nr url fil [ny]",
        '  if [ "${4:-}" != ny ] && [ -s "$DEST/$3" ]; then return; fi',
        f'  echo "[$1/{n}] $3"',
        '  mkdir -p "$(dirname "$DEST/$3")"',
        '  wget --quiet --user-agent="$UA" --tries=2 --timeout=60 -O "$DEST/$3.part" "$2"',
        '  mv "$DEST/$3.part" "$DEST/$3"',
        "  sleep 5",
        "}",
        "",
    ]
    for nr, t in enumerate(targets, start=1):
        # Titles and URLs come from the API: untrusted. Comments get one flat
        # line; arguments are shell-quoted, never Python repr.
        again = " ny" if t.get("oppdater") else ""
        note = " (hentes på nytt: protokollen manglet møtets vedtak)" if again else ""
        lines += [f'# {t["kind"]}: {_one_line(t["tittel"])}{note}',
                  f'hent {nr} {shlex.quote(t["url"])} {shlex.quote(t["filename"])}{again}']
    return "\n".join(lines) + "\n"


def _one_line(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+", " ", text or "").strip()
