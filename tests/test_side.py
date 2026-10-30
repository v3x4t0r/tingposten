"""Site-wide details (review 30.10): 404, favicon, Open Graph and canonical,
titles, the list pages, the Om page and print."""
import re
from datetime import date
from html.parser import HTMLParser

import pytest

from tingposten import bygg, config
from test_bygg import BYSTYRET, MOTE, data_dir, fake_rank, sak_page, site  # noqa: F401  (fixtures)


class Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.title, self._in_title = {}, [], "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and ("property" in a or "name" in a):
            self.meta[a.get("property") or a["name"]] = a.get("content")
        if tag == "link":
            self.links.append(a)
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def head(path):
    h = Head()
    h.feed(path.read_text())
    return h


def test_404_page_links_from_the_root(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", False)
    bygg.build(tmp_path / "s", data_dir, idag=date(2026, 10, 1))
    page = (tmp_path / "s" / "404.html").read_text()
    h = head(tmp_path / "s" / "404.html")
    assert h.title == "Fant ikke siden · Tingposten"
    assert h.meta["robots"] == "noindex, nofollow"           # even when the site is open to search
    assert "<h1 class=\"side-tittel\">Vi finner ikke denne siden</h1>" in page
    assert "Adressen kan være skrevet feil, eller siden kan ha flyttet. Alle saker ligger under møtet der de ble behandlet." in page
    for href, tekst in (("/bergen/", "Til forsiden"), ("/bergen/moter/", "Alle møter"), ("/om/", "Om Tingposten")):
        assert f'<a class="pil" href="{href}">{tekst}</a>' in page
    assert 'href="/static/style.css"' in page and 'href="../' not in page
    assert not any(l.get("rel") == "canonical" for l in h.links)
    # under a sub-path (GitHub Pages) the links start there
    monkeypatch.setattr(config, "SITE_URL", "https://eksempel.github.io/tingposten/")
    bygg.build(tmp_path / "gh", data_dir, idag=date(2026, 10, 1))
    assert 'href="/tingposten/static/style.css"' in (tmp_path / "gh" / "404.html").read_text()


def test_favicon_open_graph_and_canonical(site):
    site, _ = site
    assert (site / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"   # an ICO file
    svg = (site / "static/favicon.svg").read_text()
    assert svg.startswith("<svg") and "<text" not in svg and "@font-face" not in svg  # drawn, no webfont
    front = head(site / "bergen/index.html")
    icons = [l["href"] for l in front.links if l.get("rel") == "icon"]
    assert icons == ["../favicon.ico", "../static/favicon.svg"]
    assert any(l.get("rel") == "canonical" and l["href"] == config.SITE_URL + "bergen/" for l in front.links)
    assert front.meta["og:url"] == config.SITE_URL + "bergen/"
    assert front.meta["og:type"] == "website" and front.meta["og:site_name"] == "Tingposten"
    assert front.meta["og:locale"] == "nb_NO" and front.meta["og:title"] == front.title
    assert front.meta["og:description"] == front.meta["description"]
    sak = head(sak_page(site, "238/26"))
    assert sak.meta["og:type"] == "article"
    assert sak.meta["og:url"] == f"{config.SITE_URL}bergen/sak/{BYSTYRET}/2026-09-23/238-26/"
    assert head(site / "index.html").meta["og:url"] == config.SITE_URL
    # the big nameplate's font is preloaded where it's shown, not on every page
    preload = [l for l in front.links if l.get("rel") == "preload"]
    assert preload and preload[0]["href"].endswith("unifrakturmaguntia-latin-400-normal.woff2")
    assert not [l for l in head(site / "om/index.html").links if l.get("rel") == "preload"]


def test_titles_end_with_the_site_name(site):
    site, _ = site
    for page in site.rglob("*.html"):
        title = head(page).title
        assert title == "Tingposten" if page == site / "index.html" else title.endswith(" · Tingposten"), (page, title)
    assert head(site / "om/index.html").title == "Om · Tingposten"
    assert head(site / "bergen/i-ar/index.html").title == "I år · Bergen · Tingposten"


def test_om_page_without_the_andel_column(site):
    site, _ = site
    om = (site / "om/index.html").read_text()
    assert "Andel" not in om and '<th scope="col">Poeng</th>' in om
    assert '<p class="kicker">' not in om
    assert "til sammen 100 poeng" in om


def test_list_pages_one_column_and_no_repeated_kicker(site):
    site, _ = site
    for rel in ("bergen/30-dager/alle/index.html", "bergen/i-ar/alle/index.html", "bergen/tema/annet/index.html"):
        page = (site / rel).read_text()
        assert '<header class="sidehode">\n  <p class="kicker">' not in page, rel
        assert 'class="rader rader-en"' in page or 'class="indeks"' in page, rel
    alle = (site / "bergen/30-dager/alle/index.html").read_text()
    assert "Sakene står i samme rekkefølge som i møtet." in alle and 'class="rad-dek"' in alle
    # the year: month subheads and one line per sak, no deks
    aar = (site / "bergen/i-ar/alle/index.html").read_text()
    assert '<h2 class="maned-tittel" id="mnd-2026-09">September 2026</h2>' in aar
    assert '<h2 class="maned-tittel" id="mnd-2026-01">Januar 2026</h2>' in aar
    assert aar.index("mnd-2026-09") < aar.index("mnd-2026-01")
    assert 'class="rad-dek"' not in aar and '<li class="blokk indeksrad">' in aar
    assert re.search(r'<h4 class="indeks-tittel"><a class="blokklenke" href="[^"]+">Klarsignal til å bygge Eksempelhallen</a></h4>', aar)
    assert re.search(r'<h4 class="indeks-tittel raatittel">', aar)       # a sak without a card
    assert '<span class="faktum-status"><span class="vh">Status: </span>Sendt tilbake' in aar


def test_print_and_short_page_css(site):
    site, _ = site
    css = (site / "static/style.css").read_text()
    printing = css[css.index("@media print"):]
    assert "--papir: #fff" in printing and "--blekk: #000" in printing
    for sel in (".meny", ".hopp", ".periodelinje", ".brodsmule", ".relaterte", ".paa-siden", ".bunn-lenker"):
        assert sel in printing.split("display: none")[0], sel
    assert "break-after: avoid" in printing and "break-inside: avoid" in printing
    assert "body > main { flex: 1 0 auto; width: 100%; }" in css     # the footer sits at the bottom of short pages


def test_footer_and_navigation(site):
    site, _ = site
    page = (site / "bergen/index.html").read_text()
    assert '<footer class="bunn ramme">\n  <div class="bunn-innhold">' in page
    assert '<p class="bunn-lenker">' in page


def test_editor_contact_and_email_report(tmp_path, data_dir, monkeypatch):
    from tingposten import config
    monkeypatch.setattr(config, "ANSVARLIG_REDAKTOR", "Ola Redaktør")
    monkeypatch.setattr(config, "KONTAKT_EPOST", "avis@eksempel.no")
    bygg.build(tmp_path / "med", data_dir, idag=date(2026, 10, 1))
    om = (tmp_path / "med" / "om" / "index.html").read_text()
    assert "Ola Redaktør, som er ansvarlig redaktør" in om and 'href="mailto:avis@eksempel.no"' in om
    sak = next((tmp_path / "med" / "bergen" / "sak").rglob("index.html")).read_text()
    assert 'href="mailto:avis@eksempel.no?subject=Feil%20i%20sak' in sak and "meld feilen på GitHub" in sak
    monkeypatch.setattr(config, "ANSVARLIG_REDAKTOR", None)
    monkeypatch.setattr(config, "KONTAKT_EPOST", None)
    bygg.build(tmp_path / "uten", data_dir, idag=date(2026, 10, 1))
    assert "Hvem står bak" not in (tmp_path / "uten" / "om" / "index.html").read_text()
    sak = next((tmp_path / "uten" / "bergen" / "sak").rglob("index.html")).read_text()
    assert "mailto:" not in sak and ">Meld feil</a>" in sak


def test_root_goes_straight_to_bergen_on_statichost(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(config, "STARTSIDE", "bergen/")
    bygg.build(tmp_path / "s", data_dir, idag=date(2026, 10, 1))
    assert "/ /bergen/ 302!" in (tmp_path / "s" / "_redirects").read_text().splitlines()
    assert (tmp_path / "s" / "index.html").exists()  # the landing page stays as the fallback
    monkeypatch.setattr(config, "STARTSIDE", None)
    bygg.build(tmp_path / "u", data_dir, idag=date(2026, 10, 1))
    assert not (tmp_path / "u" / "_redirects").exists()
