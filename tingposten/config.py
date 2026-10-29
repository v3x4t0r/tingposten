import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "bergen"
SITE_DIR = ROOT / "site"
PAGES_BRANCH = "gh-pages"
# Custom domain for GitHub Pages. Leave None until DNS for tingposten.no
# points at Pages — a CNAME file without matching DNS makes the site
# unreachable (Pages serves the custom domain, not the github.io URL).
CNAME = None


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")

API_BASE = "https://www.bergen.kommune.no/politikere-utvalg/api"
# Identifiable to Bergen kommune; contact address comes from .env on the Pi.
_contact = os.environ.get("TINGPOSTEN_CONTACT")
REPO = "v3x4t0r/tingposten"  # public repo; may move to its own account later
USER_AGENT = f"Tingposten/0.1 (+https://github.com/{REPO}" + (
    f"; kontakt: {_contact})" if _contact else ")"
)

# Bergen's WAF blocks this host after a short burst of requests (seen
# 2026-10-29: ~20 requests in a few minutes). Stay well below that.
REQUEST_DELAY_S = 5.0
PAGE_SIZE = 100
TIMEOUT_S = 60
BACKOFF_S = (60, 300)  # waits before retry 1 and 2; then give up the run

# Tier 1: the organs whose decisions are final or go straight to bystyret.
TIER1 = {
    "571424": "Bergen bystyre",
    "900101": "Byrådet – saker med innstilling til bystyret",
    "900102": "Byrådet – saker avgjort av byrådet",
}
