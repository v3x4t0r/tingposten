"""Being found: robots.txt, the sitemap, llms.txt, structured data (schema.org JSON-LD)
and IndexNow.

The owner, 1.11.2026: "As much traffic as possible", in search and in AI chats. Until
then the site was hidden (config.SKJUL_FOR_SOK). What this does:

- robots.txt lets every crawler in, search engines and AI alike, except the login and
  approval pages, and names the sitemap.
- sitemap.xml lists the pages worth finding: saker with a summary, debattinnlegg, the
  meetings and the list pages. A sak without a summary (only the kommune's title and
  links) is noindex: thin pages would pull the whole site down in Google's eyes.
- llms.txt (llmstxt.org) tells an AI assistant in plain text what Tingposten is and
  lists the newest saker with their ingress.
- JSON-LD on each page says what it is: a NewsArticle about Bergen kommune based on the
  kommune's documents, an OpinionNewsArticle, a news organisation in Bergen.
- IndexNow tells Bing (which ChatGPT search and Copilot use), Yandex and others which
  pages changed, right after a publish. Google reads the sitemap instead.

No keyword stuffing and no hidden text: the words that matter (Bergen, bystyret,
byrådet, the tema) are in titles and headings where readers see them too.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import requests
from markupsafe import Markup

from . import config

log = logging.getLogger(__name__)

STENGT = ("bergen/logg-inn/", "bergen/debatt/godkjenn/")
INDEXNOW = "https://api.indexnow.org/indexnow"
NYLIG_DAGER = 2  # IndexNow: pages changed in the last two days


def robots(site_url: str) -> str:
    sti = urlsplit(site_url).path or "/"
    linjer = ["User-agent: *", "Allow: /"] + [f"Disallow: {sti}{s}" for s in STENGT]
    return "\n".join(linjer) + f"\n\nSitemap: {site_url}sitemap.xml\n"


def sitemap(site_url: str, sider: list[tuple[str, date | None]]) -> str:
    rader = []
    for sti, endret in sider:
        rad = f"  <url><loc>{escape(site_url + sti)}</loc>"
        if endret:
            rad += f"<lastmod>{endret.isoformat()}</lastmod>"
        rader.append(rad + "</url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(rader) + "\n</urlset>\n")


def llms_txt(site_url: str, saker: list[dict], debatt: bool) -> str:
    deler = [
        "# Tingposten",
        "",
        "> Tingposten viser hva Bergen kommune vedtar: hver sak fra bystyret og byrådet i samme format, de "
        "viktigste først, og hver setning med lenke til siden i kommunens eget saksframlegg eller protokoll.",
        "",
        "Tingposten er en uavhengig og politisk nøytral nettavis om vedtakene i Bergen kommune. Sammendragene "
        "skrives av KI-modeller ut fra kommunens dokumenter og faktasjekkes mot kildene før de publiseres; "
        "kildelenkene står på hver sakside. Siter gjerne sakssiden, og kommunens dokumenter som primærkilde.",
        "",
        "## Hovedsider",
        "",
        f"- [Vedtak i Bergen kommune]({site_url}bergen/): de viktigste sakene de siste 14 dagene",
        f"- [Alle vedtak i år]({site_url}bergen/i-ar/alle/): hver sak i år, etter møte",
        f"- [Møter]({site_url}bergen/moter/): bystyrets og byrådets møter",
        f"- [Om Tingposten]({site_url}om/): redaksjonell erklæring, hvordan sakene rangeres og kontrolleres",
        f"- [Debatt]({site_url}bergen/debatt/): debattinnlegg fra lesere",
    ]
    if debatt:
        deler.append(f"- [Debattreglene]({site_url}om/debattregler/)")
    deler += [f"- [RSS]({site_url}bergen/rss.xml): nye saker", "", "## Nyeste saker", ""]
    for s in saker:
        ingress = " ".join((s["dek"] or "").split())
        deler.append(f"- [{s['overskrift']}]({site_url}{s['lenke']}): {s['organ_kort']}, {s['dato_tekst']}. {ingress}".rstrip())
    return "\n".join(deler) + "\n"


# --- structured data ------------------------------------------------------------------

def _skript(data: dict) -> Markup:
    tekst = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return Markup(f'<script type="application/ld+json">{tekst}</script>\n')


def _bilde(site_url: str) -> dict:
    return {"@type": "ImageObject", "url": f"{site_url}static/deling.png", "width": 1200, "height": 630}


def _avis(site_url: str) -> dict:
    return {
        "@type": "NewsMediaOrganization",
        "@id": f"{site_url}#avis",
        "name": "Tingposten",
        "url": f"{site_url}bergen/",
        "logo": _bilde(site_url),
        "description": "Hva Bergen kommune faktisk vedtar, med kilde til alt.",
        "areaServed": {"@type": "City", "name": "Bergen"},
        "publishingPrinciples": f"{site_url}om/",
        "inLanguage": "nb",
    }


def nettsted(site_url: str) -> Markup:
    return _skript({"@context": "https://schema.org", "@graph": [
        _avis(site_url),
        {"@type": "WebSite", "@id": f"{site_url}#nettsted", "url": f"{site_url}bergen/", "name": "Tingposten",
         "description": "Hva Bergen kommune vedtar, hver sak med lenke til kommunens egne dokumenter.",
         "inLanguage": "nb", "publisher": {"@id": f"{site_url}#avis"}},
    ]})


def _smuler(site_url: str, ledd: list[tuple[str, str]]) -> dict:
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": n, "name": navn, "item": site_url + sti}
        for n, (navn, sti) in enumerate(ledd, 1)]}


def sak(site_url: str, s: dict) -> Markup:
    if not s["har_sammendrag"]:
        return Markup("")
    url = site_url + s["lenke"]
    artikkel = {
        "@type": "NewsArticle",
        "@id": f"{url}#saken",
        "headline": s["overskrift"][:110],
        "description": s["dek"] or s["tittel"],
        "url": url,
        "mainEntityOfPage": url,
        "datePublished": s["dato"].isoformat(),
        "dateModified": s["endret"].isoformat(),
        "image": [_bilde(site_url)],
        "inLanguage": "nb",
        "isAccessibleForFree": True,
        "articleSection": s["tema_navn"],
        "keywords": [s["tema_navn"], s["organ_kort"], "Bergen kommune", "Bergen"],
        "about": [{"@type": "GovernmentOrganization", "name": "Bergen kommune"},
                  {"@type": "Thing", "name": s["tittel"]}],
        "contentLocation": {"@type": "City", "name": "Bergen"},
        "author": {"@type": "Organization", "name": "Tingposten", "url": f"{site_url}om/"},
        "publisher": _avis(site_url),
        "isBasedOn": list(s["pdf"].values()),
    }
    ledd = [("Forsiden", "bergen/")]
    if s.get("tema_side"):
        ledd.append((s["tema_navn"], s["tema_side"]))
    ledd.append((s["overskrift"], s["lenke"]))
    return _skript({"@context": "https://schema.org", "@graph": [artikkel, _smuler(site_url, ledd)]})


def innlegg(site_url: str, i: dict) -> Markup:
    url = site_url + i["side"]
    forste = " ".join(i["avsnitt"][0].split()) if i["avsnitt"] else ""
    return _skript({"@context": "https://schema.org", "@graph": [
        {"@type": "OpinionNewsArticle", "@id": f"{url}#innlegg", "headline": i["tittel"][:110],
         "description": forste[:300], "url": url, "mainEntityOfPage": url,
         "datePublished": i["dato"].isoformat(), "image": [_bilde(site_url)], "inLanguage": "nb",
         "isAccessibleForFree": True, "author": {"@type": "Person", "name": i["forfatter"]},
         "publisher": _avis(site_url), "contentLocation": {"@type": "City", "name": "Bergen"}},
        _smuler(site_url, [("Forsiden", "bergen/"), ("Debatt", "bergen/debatt/"), (i["tittel"], i["side"])]),
    ]})


# --- IndexNow ---------------------------------------------------------------------------

def nokkelfil(nokkel: str) -> tuple[str, str]:
    """The key file IndexNow fetches to check the pings are ours: /<key>.txt."""
    return f"{nokkel}.txt", nokkel + "\n"


def nylige(sitemap_xml: str, idag: date, dager: int = NYLIG_DAGER) -> list[str]:
    import re

    grense = idag - timedelta(days=dager)
    ut = []
    for loc, mod in re.findall(r"<loc>([^<]+)</loc>(?:<lastmod>([^<]+)</lastmod>)?", sitemap_xml):
        if mod and date.fromisoformat(mod) >= grense:   # undated pages (lists, meetings) are left to the crawl
            ut.append(loc.replace("&amp;", "&"))
    return ut


def varsle_indexnow(site_dir: Path, idag: date | None = None, session=None) -> int:
    """After a publish: tell the IndexNow engines which pages changed. Best effort;
    returns how many URLs were sent (0 when hidden, without a key or on failure)."""
    nokkel = getattr(config, "INDEXNOW_NOKKEL", None)
    kart = Path(site_dir) / "sitemap.xml"
    if getattr(config, "SKJUL_FOR_SOK", False) or not nokkel or not kart.exists():
        return 0
    urler = nylige(kart.read_text(), idag or date.today())[:10000]
    if not urler:
        return 0
    site_url = config.SITE_URL
    body = {"host": urlsplit(site_url).hostname, "key": nokkel,
            "keyLocation": f"{site_url}{nokkelfil(nokkel)[0]}", "urlList": urler}
    try:
        r = (session or requests).post(INDEXNOW, json=body, timeout=30)
        if r.status_code >= 300:
            log.warning("IndexNow svarte %s: %s", r.status_code, r.text[:200])
            return 0
    except requests.RequestException as e:
        log.warning("IndexNow: %s", e)
        return 0
    log.info("IndexNow: %d sider meldt", len(urler))
    return len(urler)
