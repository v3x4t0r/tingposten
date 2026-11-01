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


# The keys this process took from .env. A run the control panel starts doesn't
# inherit them: it reads .env itself, so an edit to .env counts from the next run.
FRA_DOTENV: set[str] = set()


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and not key.startswith("#") and key not in os.environ:
            os.environ[key] = value.strip()
            FRA_DOTENV.add(key)


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
# "/" sends readers straight to this page while Bergen is the only kommune
# (statichost _redirects; the landing page stays as the fallback elsewhere). None = landing page.
STARTSIDE = "bergen/" if PUBLISER_TIL.startswith("statichost") else None
FREMHEVET_TERSKEL = 40  # score at or above this is highlighted on the front page
MELD_FEIL = None  # None → GitHub issue on REPO, prefilled with the sak
# Who stands behind the site (shown on the Om page) and where readers write.
# None = not shown. With KONTAKT_EPOST, "Meld feil" also offers e-mail
# (prefilled with the sak) next to the GitHub issue.
ANSVARLIG_REDAKTOR = "Henrik Midttun"
UTGIVER = "RO-Studio AS"            # publisher; the Om page also says it makes Freetime
UTGIVER_ORGNR = "938 625 999"
UTGIVER_OGSA = ("Freetime", "https://freetime.no/")  # "som også står bak …" (a reference, not an ad)
KONTAKT_EPOST = "tingposten@freetime.no"  # Domeneshop alias on freetime.no, forwarded to the editor (30.10.2026)

# Debatt (reader comments). The comment server's public address (Tailscale Funnel
# on the Pi, deploy/debatt/README.md); the sak pages get a comment section that
# talks to it. None = no comments on the site. Switched on 1.11.2026.
DEBATT = "https://debatt.tailab9cf1.ts.net/"
# Moderators asked in turn until one is sure (tingposten/debatt/moderering.py):
# "hailo", "cpu", "laya", "jev", "claude". Chosen from `tingposten debatt-test` (31.10,
# 150 test comments): Jev (TypeSafe, yes/no per rule) got the rule right 87–89 % of the
# time in 0.2 s; the local models 13–69 %, Qwen3.5 on the Pi at 16 s. Every comment's
# text goes to TypeSafe (the rules page says so). Not final until the editor has set fasit.
DEBATT_MODERATOR = ("jev",)
# Per rule (stop, doubt): at `stop` the comment is stopped, at `doubt` the editor decides,
# below every rule's doubt it is published. The editor's line (31.10): strict on hate,
# discrimination and violence; relaxed on everything else.
# Tuned 31.10 on the test set (regler-3, Jev): 0 dangerous slips, 0 lawful comments stopped,
# 9 % to the editor. Re-check with `debatt-test --rapport` once the editor has set fasit.
DEBATT_GRENSER = {
    "trussel": (0.70, 0.25),
    "hets": (0.70, 0.25),
    "trakassering": (0.90, 0.25),
    "privatperson": (0.90, 0.50),
    "beskyldning": (0.90, 0.60),
    "salg": (0.80, 0.40),  # 1.11, not yet tuned on the test set: a little stricter than spam
    "spam": (0.90, 0.60),
}
DEBATT_GRENSE_OK = 0.40  # and "ok" itself at least this likely before a comment is published
# The model servers' addresses; the comment server's container sets its own (deploy/debatt).
DEBATT_HAILO_URL = os.environ.get("DEBATT_HAILO_URL", "http://127.0.0.1:8000")  # hailo-ollama on the Hailo-10H
DEBATT_HAILO_MODELL = os.environ.get("DEBATT_HAILO_MODELL", "qwen3:1.7b")
# hailo-ollama gives no probabilities, only a rule. False = its verdict alone never
# publishes or stops a comment (the next moderator or the editor decides).
DEBATT_HAILO_STOL_PA_ETIKETT = False
DEBATT_CPU_URL = os.environ.get("DEBATT_CPU_URL", "http://127.0.0.1:8081")  # llama-server (llama.cpp) on the CPU
DEBATT_CPU_MODELL = os.environ.get("DEBATT_CPU_MODELL", "qwen3.5-4b")  # names the test runs
# Laya, an open Jev-like decision model run in-process (pip install laya, with torch).
# "ja-nei" = one yes/no question per rule, "valg" = one question over all the rules.
DEBATT_LAYA_MODELL = os.environ.get("DEBATT_LAYA_MODELL", "convaiinnovations/laya-multilingual")
DEBATT_LAYA_MODUS = os.environ.get("DEBATT_LAYA_MODUS", "ja-nei")
# Jev (TypeSafe, key: TYPESAFE_API_KEY in .env), or a local server that speaks the same
# System One format (Jev-Style: `jev-style serve`, e.g. http://127.0.0.1:8765/v1/systemone).
DEBATT_JEV_URL = os.environ.get("DEBATT_JEV_URL", "https://api.typesafe.ai/v1/systemone")
DEBATT_JEV_MODELL = os.environ.get("DEBATT_JEV_MODELL", "jev-latest")  # TypeSafe SDK default alias
DEBATT_JEV_MODUS = os.environ.get("DEBATT_JEV_MODUS", "ja-nei")  # "ja-nei" or "valg" (as for Laya)
# Key: TINGPOSTEN_ANTHROPIC_API_KEY in .env (not ANTHROPIC_API_KEY, which would make
# `claude -p` bill the API instead of the subscription).
DEBATT_CLAUDE_MODELL = "claude-opus-5-5"
# Comments database, the sak index from bygg, and the moderation test set.
DEBATT_DIR = Path(os.environ.get("DEBATT_DIR") or ROOT / "data" / "debatt")

# Models, called through the subscription CLIs (no API keys): Claude writes
# and classifies, Codex fact-checks — a different provider on purpose.
MODELL_SKRIBENT = "opus"
MODELL_KLASSIFISERER = "sonnet"
MODELL_INNLEGG = "sonnet"  # the AI check of debattinnlegg from the form (debatt/innleggsjekk.py), effort low
MODELL_FAKTASJEKKER = None  # None → the model set in ~/.codex/config.toml
# When Codex can't answer (not installed, out of quota), Claude with this model
# fact-checks instead, for the rest of that run; the check records which model did
# it. Owner, 30.10.2026: Codex out of tokens, Fable as the backup reviewer. None = no reserve.
FAKTASJEKKER_RESERVE = "fable"
# Owner, 31.10.2026: the reserve runs at low effort so it doesn't use up the subscription.
FAKTASJEKKER_RESERVE_EFFORT = "low"

# Organs whose items are recommendations to another organ, not decisions.
# Their cases show up on the site when the deciding organ has decided.
INNSTILLENDE = {"900101"}

# Tier 1: the organs whose decisions are final or go straight to bystyret.
TIER1 = {
    "571424": "Bergen bystyre",
    "900101": "Byrådet – saker med innstilling til bystyret",
    "900102": "Byrådet – saker avgjort av byrådet",
}
