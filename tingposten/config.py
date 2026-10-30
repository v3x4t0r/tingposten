import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "bergen"
SITE_DIR = ROOT / "site"
# The branch that holds the built site. Not "gh-pages": GitHub keeps Pages
# switched on while a branch with that name exists (seen 2026-10-29).
PAGES_BRANCH = "nettside"
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

# robots.txt disallows /api/fil/*. Automatic PDF downloads stay off until
# Bystyrets kontor has said yes; until then PDFs are downloaded by hand.
ALLOW_PDF_FETCH = False
MAKS_PDF_PER_KJORING = 200  # cap per daily run once fetching is allowed

# How far back the AI writes cards and articles, and the PDF script reaches.
# Older meetings already written stay on the site; nothing new is made for them.
TILBAKE_DAGER = 92

# Share of each meeting's decided saker that get a background article
# (plan: about 20–30 %; tune after more meetings are rated).
ARTIKKEL_ANDEL = 0.25

# Where `publiser` puts the built site. statichost.eu = EU-owned hosting in
# Stockholm; free Hobby plan = 1 site, 10 GB traffic, custom domain.
# - "statichost-git": commit site/ to PAGES_BRANCH and push; statichost deploys
#   that branch on the push (webhook), as-is (statichost.yml: public: .).
# - "statichost-upload": upload a zip straight to statichost (STATICHOST_APIKEY in .env).
# - "github-pages": push PAGES_BRANCH and let GitHub Pages serve it (set Pages to that branch).
PUBLISER_TIL = "statichost-git"
STATICHOST_SITE = os.environ.get("STATICHOST_SITE", "tingposten")  # → https://<site>.statichost.page/
STATICHOST_BUILDER = "https://builder.statichost.eu"

# Public site
# tingposten.no points at statichost (ALIAS; www redirects there). The site is
# also reachable at https://<STATICHOST_SITE>.statichost.page/.
SITE_URL = ("https://tingposten.no/" if PUBLISER_TIL.startswith("statichost")
            else "https://v3x4t0r.github.io/tingposten/")
# Until launch the site is online but kept out of search engines: noindex on
# every page and a robots.txt that disallows everything (user's choice, 2026-10-30).
SKJUL_FOR_SOK = True
# Visitor counts: GoatCounter (cookieless, no IP addresses stored). The count.js
# in static/ is GoatCounter's own script (ISC), unmodified and served from this
# site; only the count goes to this endpoint. None = no counting.
GOATCOUNTER = "https://v3x4t0r.goatcounter.com/count"
# Count through the site itself: the page sends the count to <site>/count and
# statichost forwards it to GOATCOUNTER (a proxy rule in _redirects), so readers'
# browsers only talk to tingposten.no. Off for now: statichost's proxy sends the
# Host header as "v3x4t0r.goatcounter.com:443" and GoatCounter answers 400 "no site
# at this domain" (tested 2026-10-30). Turn on if statichost fixes that.
GOATCOUNTER_VIA_SIDEN = False
FREMHEVET_TERSKEL = 40  # score at or above this is highlighted on the front page
MELD_FEIL = None  # None → GitHub issue on REPO, prefilled with the sak

# Models, called through the subscription CLIs (no API keys): Claude writes
# and classifies, Codex fact-checks — a different provider on purpose.
MODELL_SKRIBENT = "opus"
MODELL_KLASSIFISERER = "sonnet"
MODELL_FAKTASJEKKER = None  # None → the model set in ~/.codex/config.toml

# Organs whose items are recommendations to another organ, not decisions.
# Their cases show up on the site when the deciding organ has decided.
INNSTILLENDE = {"900101"}

# Tier 1: the organs whose decisions are final or go straight to bystyret.
TIER1 = {
    "571424": "Bergen bystyre",
    "900101": "Byrådet – saker med innstilling til bystyret",
    "900102": "Byrådet – saker avgjort av byrådet",
}
