import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tingposten import bygg, config, grafikk, pdf, score

BYSTYRET = "571424"
MOTE = f"{BYSTYRET}-2026-09-23"


def item(nr, tittel, dato="2026-09-23", seq=5, **kw):
    return {
        "id": f"{nr}:{dato}",
        "utvalgId": BYSTYRET,
        "moteDato": dato,
        "seq": seq,
        "kategori": "Saker til behandling",
        "tittel": tittel,
        "referansenr": nr,
        "innstillingFilId": "111",
        "innstillingAlternativBeskrivelse": "Framstilling-x",
        "vedtakFilId": "222",
        "vedtakAlternativBeskrivelse": "Protokoll-x",
        **kw,
    }


ITEMS = [
    item("238/26", "Gjennomføringsvedtak for Eksempelhallen", seq=5),
    item("239/26", "Storetveit skole – rehabilitering", seq=6),
    item("240/26", "Nedre Eksempel bofellesskap", seq=7),
    item("241/26", "Sak som ble avvist av redaktøren", seq=8),
    item("243/26", "Sak med høy score men uten kort", seq=10),
    item("10/26", "Gammel sak fra januar", dato="2026-01-28", seq=1),
    {**item("1/26", "Innkalling Bergen bystyre 23.09.2026", seq=1), "kategori": "Innkalling"},
    {**item("242/26", "Sak uten vedtak ennå", seq=9), "vedtakFilId": None},
]

CARD = {
    "nr": "238/26",
    "overskrift": "Klarsignal til å bygge Eksempelhallen",
    "felt": {"hva_endres": "Bystyret ga klarsignal for 69 mill. kroner til hallen. Arbeidet starter i 2027. <script>alert(1)</script>",
             "hvem_pavirkes": "", "hvorfor": "Byrådet viser til planen.", "status": "Vedtatt enstemmig.", "nar_og_hvor": ""},
    "kilder": [{"felt": "hva_endres", "dok": "protokoll", "side": 1},
               {"felt": "hvorfor", "dok": "saksframlegg", "side": 3}],
    "slettet": [], "avvist": False, "grunn": "", "ord": 12, "setninger": 4,
}
ONE_SENTENCE = {**CARD, "nr": "239/26", "overskrift": "Skolen rehabiliteres",
                "felt": {**CARD["felt"], "hva_endres": "Skolen rehabiliteres for 876 millioner kroner."}}
ARTICLE = {
    "nr": "238/26",
    "avsnitt": [
        {"rolle": "vedtak", "tekst": "Hallen har vært planlagt siden 2019. <b>fet</b>", "kilder": [{"dok": "saksframlegg", "side": 2}]},
        {"rolle": "videre", "tekst": "Byggestart er satt til våren 2027.", "kilder": [{"dok": "protokoll", "side": 1}, {"dok": "bilag", "side": 9}]},
    ],
    "slettet": [], "avvist": False, "grunn": "", "ord": 14,
}
SCORES = {"238/26": 70, "239/26": 55, "240/26": 10, "241/26": 45, "243/26": 90, "10/26": 60}


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "agendapunkter").mkdir(parents=True)
    (d / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in ITEMS}))
    red = d / "redigert" / BYSTYRET / "2026-09-23"
    red.mkdir(parents=True)
    (red / "238-26.json").write_text(json.dumps(CARD))
    (red / "239-26.json").write_text(json.dumps(ONE_SENTENCE))
    (red / "241-26.json").write_text(json.dumps({**CARD, "nr": "241/26", "avvist": True, "overskrift": "UTKAST SOM IKKE SKAL VISES"}))
    (red / "240-26.json").write_text(json.dumps({**CARD, "nr": "240/26", "overskrift": "Bofellesskapet blir dyrere"}))
    # The classifier's tema wins over the title fallback (bofellesskap → helse).
    sig = d / "signaler" / BYSTYRET / "2026-09-23"
    sig.mkdir(parents=True)
    (sig / "240-26.json").write_text(json.dumps({"tema": "okonomi", "tema_begrunnelse": "Investeringsramme."}))
    art = d / "artikler-redigert" / BYSTYRET / "2026-09-23"
    art.mkdir(parents=True)
    (art / "238-26.json").write_text(json.dumps(ARTICLE))
    (art / "239-26.json").write_text(json.dumps({**ARTICLE, "nr": "239/26", "avvist": True,
                                                  "avsnitt": [{"tekst": "AVVIST ARTIKKEL", "kilder": []}]}))
    return d


@pytest.fixture(autouse=True)
def fake_rank(monkeypatch):
    def rank(items, pdf_dir, **kw):
        out = []
        for i in items:
            nr = i["referansenr"]
            r = {"nr": nr, "tittel": i["tittel"], "score": SCORES.get(nr, 0), "grunner": ["69 MNOK", "enstemmig"], "signaler": {}}
            if nr == "239/26":
                r["status"] = {"kode": "sendt_tilbake", "tekst": "Sendt tilbake – ikke avgjort ennå"}
                r["modell"] = {"angar": {"verdi": 4, "begrunnelse": "Angår mange familier."}}
            out.append(r)
        return sorted(out, key=lambda r: -r["score"])
    monkeypatch.setattr(score, "rank", rank)


@pytest.fixture
def site(tmp_path, data_dir):
    site = tmp_path / "site"
    stats = bygg.build(site, data_dir, idag=date(2026, 10, 1))
    return site, stats


def sak_page(site, nr):
    tag = nr.replace("/", "-")
    return site / "bergen" / "sak" / BYSTYRET / "2026-09-23" / tag / "index.html"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth, self.nested, self.refs = 0, 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self.nested += self.depth > 0
            self.depth += 1
        for k in ("href", "src"):
            if k in a:
                self.refs.append(a[k])

    def handle_endtag(self, tag):
        if tag == "a":
            self.depth -= 1


def test_pages_exist(site):
    site, stats = site
    for rel in ("index.html", "bergen/index.html", "bergen/alle/index.html", "bergen/30-dager/index.html",
                "bergen/30-dager/alle/index.html", "bergen/i-ar/index.html", "bergen/i-ar/alle/index.html",
                "bergen/moter/index.html", f"bergen/mote/{MOTE}/index.html",
                f"bergen/mote/{BYSTYRET}-2026-01-28/index.html", "om/index.html", "bergen/rss.xml",
                "static/style.css", ".nojekyll"):
        assert (site / rel).exists(), rel
    assert not (site / "CNAME").exists()
    assert stats["saker"] == 6  # noise and undecided items left out
    assert stats["moter"] == 2


def test_every_sak_has_a_page(site):
    site, _ = site
    for i in ITEMS:
        decided = i["vedtakFilId"] and not pdf.is_noise(i)
        page = site / "bergen" / "sak" / f"{pdf.key(i)}" / "index.html"
        assert page.exists() == bool(decided), i["referansenr"]


def test_links_resolve_are_relative_and_never_nested(site):
    site, _ = site
    root_path = bygg.urlsplit(bygg._site_url()).path
    for page in sorted(site.rglob("*.html")):
        p = Links()
        p.feed(page.read_text())
        assert p.nested == 0, page.relative_to(site)
        # 404.html is served at any depth: its links start at the site's root path instead.
        absolute = page == site / "404.html"
        for ref in p.refs:
            if re.match(r"^[a-z]+:", ref) or ref.startswith("#"):
                continue
            assert ref.startswith(root_path) if absolute else not ref.startswith("/"), (page, ref)
            if absolute:
                ref = ref[len(root_path):]
            target = ((site if absolute else page.parent) / ref.split("#")[0]).resolve()
            if ref.split("#")[0].endswith("/") or target.is_dir():
                target = target / "index.html"
            assert target.exists(), (page.relative_to(site), ref)


def lead_html(forside):
    return re.search(r'<article class="blokk lead[^"]*">(.*?)</article>', forside, re.S).group(1)


def test_lead_is_highest_score_with_a_summary(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    lead = lead_html(forside)
    # 243/26 scores 90 but has no card, so the lead is 238/26 (70)
    assert "Klarsignal til å bygge Eksempelhallen" in lead
    assert "Sak med høy score men uten kort" in forside  # still listed, in its tema tile
    secondary = re.findall(r'<article class="blokk sek">(.*?)</article>', forside, re.S)
    # 239 is over the threshold; 240 (score 10) fills up because it has a summary
    assert len(secondary) == 2 and "Skolen rehabiliteres" in secondary[0] and "Bofellesskapet" in secondary[1]
    annet = re.search(r'id="tema-annet".*?</section>', forside, re.S).group(0)
    # not among the stories: 243 (90, no card) before 241 (45, avvist)
    assert annet.index("Sak med høy score men uten kort") < annet.index("Sak som ble avvist av redaktøren")
    assert "Alle 5 vedtak i perioden" in forside  # the rest are in their tiles, so nothing is left over


def test_front_page_order_and_window(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    assert "Gammel sak fra januar" not in forside
    assert forside.index("Klarsignal til å bygge Eksempelhallen") < forside.index("Skolen rehabiliteres")
    assert "Tingposten" in forside and "Torsdag 1. oktober 2026" in forside
    assert 'href="../bergen/alle/"' in forside
    assert "Gammel sak fra januar" in (site / "bergen/i-ar/index.html").read_text() + (site / "bergen/i-ar/alle/index.html").read_text()


def test_window_respects_idag(tmp_path, data_dir):
    site = tmp_path / "site2"
    bygg.build(site, data_dir, idag=date(2026, 9, 1))
    assert "Ingen vedtak i perioden" in (site / "bergen/index.html").read_text()


def test_sak_page(site):
    site, _ = site
    page = sak_page(site, "238/26").read_text()
    assert '<h1 class="sak-tittel">Klarsignal til å bygge Eksempelhallen</h1>' in page
    # dek = first sentence, abbreviation "mill." doesn't end it, with its sources (no sjekk file here:
    # the whole field's); the card keeps "Hva endres" in full, the dek is its ingress (test_sakside.py)
    dek = re.search(r'<p class="sak-dek">(.*?)</p>', page, re.S).group(1)
    assert dek.startswith("Bystyret ga klarsignal for 69\u00a0mill. kroner til hallen.<span class=\"kilde\">")
    hva = re.search(r'<h3>Hva endres</h3>(.*?)</div></div>', page, re.S).group(1)
    assert "Bystyret ga klarsignal for 69\u00a0mill. kroner til hallen. Arbeidet starter i 2027." in hva
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/222/Protokoll-x#page=1" in page
    # Kort fortalt: the same five rows in the same order; a missing field is a dash
    rows = re.findall(r'<div class="felt lag"><h3>([^<]+)</h3>', page)
    assert rows == ["Hva endres", "Hvem påvirkes", "Hvorfor", "Status", "Når og hvor"]
    hvem = re.search(r'<h3>Hvem påvirkes</h3>(.*?)</div></div>', page, re.S).group(1)
    assert 'class="felt-tom"' in hvem and "–" in hvem
    # sources grouped per document, each page its own link
    hvorfor = re.search(r'<h3>Hvorfor</h3>(.*?)</div></div>', page, re.S).group(1)
    assert 'Saksframlegg s.&nbsp;<a href="https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/111/Framstilling-x#page=3" aria-label="Saksframlegg, side 3">3</a>' in hvorfor
    assert "Fra samme møte" in page and "Skolen rehabiliteres" in page
    assert 'href="https://github.com/v3x4t0r/tingposten/issues/new?title=Feil+i+sak+238%2F26' in page
    assert ">Meld feil</a>" in page
    assert 'href="../../../../../bergen/mote/' + MOTE + '/"' in page


def test_one_sentence_dek_takes_its_sources_and_status(site):
    site, _ = site
    page = sak_page(site, "239/26").read_text()
    dek = re.search(r'<p class="sak-dek">(.*?)</p>', page, re.S).group(1)
    assert dek.startswith("Skolen rehabiliteres for 876\u00a0millioner kroner.")
    # the dek carries the hva_endres source, still a link
    assert '<a href="https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/222/Protokoll-x#page=1" aria-label="Protokoll, side 1">1</a>' in dek
    # The row stays (same five rows on every sak) with the full text and its own sources.
    hva = re.search(r'<h3>Hva endres</h3>(.*?)</div></div>', page, re.S).group(1)
    assert "Står i ingressen over." not in hva and "Skolen rehabiliteres for 876\u00a0millioner kroner." in hva
    assert "Protokoll-x#page=1" in hva
    assert "Sendt tilbake – ikke avgjort ennå" in page and "status-apen" in page
    # The classifier's reasoning is not fact-checked, so it never reaches a reader.
    assert "Angår mange familier." not in page


def test_avvist_and_missing_cards(site):
    site, _ = site
    for nr, title in (("241/26", "Sak som ble avvist av redaktøren"), ("243/26", "Sak med høy score men uten kort")):
        page = sak_page(site, nr).read_text()
        # no card: the kommune's title, set quieter, and one quiet line (test_sakside.py)
        assert f'<h1 class="sak-tittel sak-tittel-raa">{title}</h1>' in page
        assert "Ikke oppsummert ennå." in page
        assert "Protokoll-x" in page
    everything = "".join(p.read_text() for p in site.rglob("*.html"))
    assert "UTKAST SOM IKKE SKAL VISES" not in everything


def test_meeting_page_is_a_headline_list_in_agenda_order(site):
    site, _ = site
    page = (site / f"bergen/mote/{MOTE}/index.html").read_text()
    assert "<article" not in page
    order = [page.index(t) for t in ("Klarsignal til å bygge", "Skolen rehabiliteres", "Bofellesskapet blir dyrere",
                                     "Sak som ble avvist", "Sak med høy score")]
    assert order == sorted(order)
    # internal ranking words never reach readers
    assert "fremhevet" not in page.lower()


def test_rss_links_to_sak_pages(site):
    site, _ = site
    root = ET.parse(site / "bergen/rss.xml").getroot()
    links = [i.findtext("link") for i in root.findall("./channel/item")]
    assert len(links) == 5
    assert all(l.startswith(f"{config.SITE_URL}bergen/sak/{BYSTYRET}/2026-09-23/") for l in links)


def test_om_page_publishes_weights(site):
    site, _ = site
    om = (site / "om/index.html").read_text()
    for name in ("Beløp", "Rekkevidde", "Juridisk vekt"):
        assert name in om
    assert "Plassholder" not in om and "Redaksjonell erklæring" in om and "Når vi ikke vet" in om


def test_figures(monkeypatch, tmp_path, data_dir):
    svg = '<svg role="img" aria-labelledby="tpf-0123abcd-t tpf-0123abcd-d"><title id="tpf-0123abcd-t">T</title><desc id="tpf-0123abcd-d">D</desc></svg>'
    figs = [{"svg": svg, "alt": "Avstemning", "kilde": "Protokollen, votering"},
            {"svg": svg, "alt": "Kart", "kilde": "Kart: © OpenStreetMap-bidragsytere (ODbL)."}]
    monkeypatch.setattr(grafikk, "figurer", lambda item, p, s: figs)
    monkeypatch.setattr(bygg._Parsers, "protokoll", lambda self, path: {"vedtak": "x"})
    monkeypatch.setattr(bygg._Parsers, "saksframlegg", lambda self, path: {})
    site = tmp_path / "site-fig"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    page = sak_page(site, "238/26").read_text()
    assert page.count('<figure class="figur figur-') == 2
    assert "Kart: © OpenStreetMap (ODbL)" in page
    assert "tpf-0123abcd-571424-238-26-t" in page  # ids made unique per sak
    forside = (site / "bergen/index.html").read_text()
    # the lead has key figures, no figure; the row of four shows the map as a small locator
    assert "<figure" not in lead_html(forside) and "tp-figur" not in lead_html(forside)
    sek = re.findall(r'<article class="blokk sek">(.*?)</article>', forside, re.S)[0]
    assert 'class="minikart"' in sek and 'aria-hidden="true"' in sek
    fire = re.search(r'<section class="fire.*?</section>', forside, re.S).group(0)
    assert fire.count("OpenStreetMap") == 1 and "OpenStreetMap" not in sek  # one credit under the row
    # no figures for an avvist card
    assert "<figure" not in sak_page(site, "241/26").read_text()
    ids = re.findall(r'id="(tpf-[^"]+)"', forside)
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("text, first", [
    ("Bystyret vedtok planen. Den gjelder Fana.", "Bystyret vedtok planen."),
    ("Rammen er 69 mill. kroner. Mer tekst.", "Rammen er 69 mill. kroner."),
    ("Saken gjelder gnr. 173 bnr. 5 i Åsane. Resten.", "Saken gjelder gnr. 173 bnr. 5 i Åsane."),
    ("Fra 1. januar 2027 gjelder nye regler. Resten.", "Fra 1. januar 2027 gjelder nye regler."),
    ("Bare én setning.", "Bare én setning."),
    ("Byrådet ga ledelsen til byrådsleder Chris Jørgen K. Rødland. Resten.", "Byrådet ga ledelsen til byrådsleder Chris Jørgen K. Rødland."),
    ("Planen ble vedtatt i 2023. Tunnelene flyttes.", "Planen ble vedtatt i 2023. Tunnelene flyttes."),
])
def test_split_first_sentence(text, first):
    assert bygg.split_first_sentence(text)[0] == first


def test_front_page_first_sentence_may_end_after_a_year():
    assert bygg.forste_setning("Planen ble vedtatt i 2023. Tunnelene flyttes.") == "Planen ble vedtatt i 2023."
    assert bygg.forste_setning("Gjelder fra punkt 1. Bystyret vedtok.") == "Gjelder fra punkt 1. Bystyret vedtok."
    assert bygg.forste_setning("Leiegården i St. Hansstredet 20. Den er taksert.") == "Leiegården i St. Hansstredet 20. Den er taksert."


def test_rebuild_replaces_old_files_and_guard(tmp_path, data_dir):
    site = tmp_path / "site3"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    (site / "stale.html").write_text("old")
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    assert not (site / "stale.html").exists()
    other = tmp_path / "not-a-site"
    other.mkdir()
    (other / "important.txt").write_text("keep")
    with pytest.raises(ValueError):
        bygg.build(other, data_dir, idag=date(2026, 10, 1))
    assert (other / "important.txt").exists()


def test_meld_feil_template(monkeypatch):
    from tingposten import config
    monkeypatch.setattr(config, "MELD_FEIL", "mailto:feil@example.org?subject=Feil%20i%20sak%20{nr}", raising=False)
    assert bygg.meld_feil("238/26", "T", "https://x") == "mailto:feil@example.org?subject=Feil%20i%20sak%20238%2F26"


def test_tema_tiles_pages_and_breadcrumb(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    # a tile only for a tema with saker left over: annet; okonomi's one sak (240) is a story
    assert 'id="tema-annet"' in forside and 'id="tema-okonomi"' not in forside
    assert "I korthet" not in forside  # only 3 stories, so no list after the four
    annet = re.search(r'id="tema-annet".*?</section>', forside, re.S).group(0)
    assert 'href="../bergen/tema/annet/">Alle 3 vedtak<' in annet  # the count link at the tile's foot
    assert "Klarsignal" not in annet  # the lead isn't repeated in its tile
    ogsa = re.search(r'<p class="ogsa">.*?</p>', forside, re.S).group(0)
    assert "Også i perioden:" in ogsa and 'href="../bergen/tema/okonomi/">Økonomi</a>' in ogsa
    # "Perioden i tall": the tema bars link to the tema pages, never to anchors on the page
    assert 'href="../bergen/tema/okonomi/"><span class="stolpe-navn">' in forside
    assert 'href="#tema-' not in forside
    page = (site / "bergen/tema/okonomi/index.html").read_text()
    assert '<h1 class="side-tittel">Økonomi</h1>' in page and "Bofellesskapet blir dyrere" in page and "Andre temaer" in page
    assert (site / "bergen/i-ar/tema/annet/index.html").exists() and (site / "bergen/30-dager/tema/skole/index.html").exists()
    assert "Gammel sak fra januar" in (site / "bergen/i-ar/tema/annet/index.html").read_text()
    sak = sak_page(site, "238/26").read_text()
    assert 'href="../../../../../bergen/i-ar/tema/annet/">Annet</a>' in sak
    assert '<span class="kicker-tema">Annet</span>' in sak
    assert '<span class="kicker-tema">Økonomi</span>' in sak_page(site, "240/26").read_text()


def test_article_rendering_and_cue(site):
    site, _ = site
    page = sak_page(site, "238/26").read_text()
    artikkel = re.search(r'<section class="artikkel lag".*?</section>', page, re.S).group(0)
    assert '<h2 class="etikett etikett-strek artikkel-etikett" id="artikkel-tittel">Saken</h2>' in artikkel
    assert "Bygger på saksframlegget og protokollen" in artikkel and "Ca. 1 minutt å lese" in artikkel
    assert "Hallen har vært planlagt siden 2019. &lt;b&gt;fet&lt;/b&gt;" in artikkel  # escaped
    # each paragraph's sources beside it (a margin note), every page a link; no footnote numbers
    assert '<p class="randnote" id="kilde-1"><span class="randnote-etikett">Kilde</span>' in artikkel and 'id="kilde-2"' in artikkel
    assert 'class="fn"' not in artikkel and "fotnoter" not in artikkel
    assert "/fil/bk360/111/Framstilling-x#page=2" in artikkel and "/fil/bk360/222/Protokoll-x#page=1" in artikkel
    assert "bilag" not in artikkel  # only the two known documents become links
    assert artikkel.index("Hallen har vært") < artikkel.index("Byggestart")
    # the body first, with a sidehead where the role changes (never above the opening paragraph); Kort fortalt in the rail
    assert '<p id="avsnitt-1" class="forste">' in artikkel and 'class="mellomtittel"' not in artikkel.split("Byggestart")[0].split('id="avsnitt-1"')[0]
    assert '<h3 class="mellomtittel">Veien videre</h3>' in artikkel
    assert page.index('id="artikkel"') < page.index('id="kort"') < page.index('id="dokumenter"')
    assert '<section class="kort kort-rail" id="kort"' in page
    assert "AVVIST ARTIKKEL" not in sak_page(site, "239/26").read_text()
    # without an article the card is the main column
    uten = sak_page(site, "240/26").read_text()
    assert 'id="artikkel"' not in uten and '<section class="kort" id="kort"' in uten
    forside = (site / "bergen/index.html").read_text()
    assert "min lesing" not in lead_html(forside) and "faktum-bakgrunn" in lead_html(forside)
    sek = re.search(r'<article class="blokk sek">(.*?)</article>', forside, re.S).group(1)
    assert "Artikkel" not in sek


def test_article_roles_give_sideheads_and_no_second_headline(tmp_path, data_dir):
    path = data_dir / "artikler-redigert" / BYSTYRET / "2026-09-23" / "238-26.json"
    avsnitt = [
        {"rolle": "vedtak", "tekst": "Vedtaket i detalj.", "kilder": [{"dok": "saksframlegg", "side": 1}]},
        {"rolle": "avstemning", "tekst": "SV ville utsette.", "kilder": [{"dok": "protokoll", "side": 1}]},
        {"rolle": "bakgrunn", "tekst": "Første bakgrunn.", "kilder": [{"dok": "saksframlegg", "side": 2}]},
        {"rolle": "bakgrunn", "tekst": "Andre bakgrunn.", "kilder": [{"dok": "saksframlegg", "side": 3}]},
        {"rolle": "videre", "tekst": "Neste steg.", "kilder": [{"dok": "saksframlegg", "side": 4}]},
    ]
    path.write_text(json.dumps({**ARTICLE, "avsnitt": avsnitt, "tittel": "SKAL IKKE VISES",
                                "tittel_kilder": [{"dok": "protokoll", "side": 1}], "tittel_grunn": ""}))
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    page = sak_page(site, "238/26").read_text()
    assert "SKAL IKKE VISES" not in page and page.count("<h1") == 1
    heads = re.findall(r'<h3 class="mellomtittel">(.*?)</h3>', page)
    assert heads == ["Slik gikk avstemningen", "Bakgrunnen", "Veien videre"]  # once per role change, none for vedtak
    assert page.index("Slik gikk avstemningen") < page.index("SV ville utsette.")
    s = {x["nr"]: x for x in bygg.load(data_dir)}["238/26"]
    assert [a["mellomtittel"] for a in s["artikkel"]] == ["", "Slik gikk avstemningen", "Bakgrunnen", "", "Veien videre"]


def test_ingress_is_the_dek_and_leaves_the_fields_whole(tmp_path, data_dir):
    path = data_dir / "redigert" / BYSTYRET / "2026-09-23" / "238-26.json"
    card = json.loads(path.read_text())
    card["felt"]["ingress"] = "Hallen får 69 millioner kroner, og byggingen kan starte."
    card["kilder"].append({"felt": "ingress", "dok": "saksframlegg", "side": 3})
    path.write_text(json.dumps(card))
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    s = {x["nr"]: x for x in bygg.load(data_dir)}["238/26"]
    assert s["har_ingress"] and s["dek"] == "Hallen får 69 millioner kroner, og byggingen kan starte."
    assert s["dek_kilder"] == [{"dok": "saksframlegg", "side": 3,
                                "href": "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/111/Framstilling-x#page=3"}]
    hva = next(f for f in s["felt"] if f["key"] == "hva_endres")
    assert hva["tekst"] == card["felt"]["hva_endres"]  # not cut: the ingress is its own text
    assert [f["key"] for f in s["kort_felt"]][0] == "hva_endres"  # the ingress is not a Kort fortalt row
    page = sak_page(site, "238/26").read_text()
    body = page[page.index('<article class="sak">'):]
    assert body.count("Hallen får 69") == 1  # the dek once (typo filter adds no-break spaces), not again in Kort fortalt


def test_rss_has_tema_category(site):
    site, _ = site
    root = ET.parse(site / "bergen" / "rss.xml").getroot()
    cats = {c.text for c in root.iter("category")}
    assert {"Annet", "Økonomi", "Skole og barnehage"} <= cats


@pytest.mark.parametrize("tittel,tema", [
    ("Søknad om fritak fra verv og nyvalg i Utvalg for barnehage", "styring"),
    ("Storetveit skole - Totalrehabilitering", "skole"),
    ("E39 Fløyfjelltunnelen - tunnelramper", "samferdsel"),
    ("Høringsuttalelse til NOU 2026:4 Kjernekraft i Norge", "miljo"),
    ("Fana, Gnr. 13, Bnr. 644 mfl., Stavkirkevegen, boliger", "byutvikling"),
    ("Kommunal forkjøpsrett - Gnr 159 Bnr 706 Rogagaten 33", "byutvikling"),
    ("Eierskapskontroll av Eviny AS", "okonomi"),
    ("Alvøen idrettshall I0420. Sluttmelding", "kultur"),
    ("Rehabilitering først. Strategi for rehabilitering", "helse"),
    ("Noe helt annet", "annet"),
])
def test_tema_fra_tittel(tittel, tema):
    assert bygg.tema_fra_tittel(tittel) == tema


# --- facts under a story: one fixed rule (bygg.fakta) --------------------------

def facts(**kw):
    s = {"status_klasse": "", "status_tekst": "Vedtatt", "innstilling_falt": False, "bydeler": [],
         "hele_byen": False, "hele_kommunen": False, "stemmer": None, "forslag_falt": 0, "belop": None,
         "figurer": [], "har_artikkel": False, "lesetid": 0, **kw}
    return [(f["type"], f["verdi"]) for f in bygg.fakta(s)]


def test_fakta_never_say_vedtatt_and_keep_a_fixed_order():
    assert facts() == []  # an ordinary adopted sak: nothing to say
    assert facts(innstilling_falt=True) == [("status", "Innstillingen falt")]
    assert facts(status_klasse="apen", status_tekst="Sendt tilbake – ikke avgjort ennå", innstilling_falt=True) == [
        ("status", "Sendt tilbake – ikke avgjort ennå")]
    full = facts(status_klasse="noytral", status_tekst="Tatt til orientering", bydeler=["Fana"],
                 stemmer={"for": 36, "mot": 32, "jevn": True}, forslag_falt=4, belop=866_400_000,
                 har_artikkel=True, lesetid=2)
    assert full == [("status", "Tatt til orientering"), ("sted", "Fana"), ("stemmer", "36–32"),
                    ("kr", "866 mill. kr"), ("bakgrunn", "Ca. 2 min lesing")]


def test_fakta_place_vote_and_amount_rules():
    assert facts(bydeler=["Arna", "Fana"]) == [("sted", "Arna og Fana")]
    assert facts(bydeler=["Arna", "Fana", "Åsane"], hele_byen=True) == [("sted", "3 bydeler")]
    assert facts(hele_byen=True, hele_kommunen=True) == [("sted", "Hele kommunen")]
    assert facts(hele_byen=True) == [("sted", "Hele kommunen")]
    assert facts(hele_kommunen=True) == [("sted", "Hele kommunen")]
    # a lopsided vote isn't shown; then the number of proposals that fell, from two
    assert facts(stemmer={"for": 60, "mot": 7, "jevn": False}, forslag_falt=3) == [("falt", "3")]
    assert facts(stemmer={"for": 60, "mot": 7, "jevn": False}, forslag_falt=1) == []
    assert facts(stemmer={"for": 35, "mot": None, "jevn": True}) == [("stemmer", "35 stemmer for")]
    assert facts(belop=900_000) == []  # under 1 mill. kr
    assert facts(belop=3_600_000) == [("kr", "3,6 mill. kr")]
    assert facts(belop=1_250_000_000) == [("kr", "1,2 mrd. kr")]
    meter = {"type": "belop", "svg": "", "belop": {"type": "økning", "del": "1 mill.", "total": "3,6 mill.",
                                                   "tekst": "Ny sum: 3,6 mill."}}
    assert facts(figurer=[meter]) == [("kr", "Ny sum: 3,6 mill.")]


def test_structured_facts_come_from_the_signals(site):
    site, _ = site
    saker = {s["nr"]: s for s in bygg.load(site.parent / "data")}
    s = saker["238/26"]
    assert {k: s[k] for k in ("stemmer", "forslag_falt", "innstilling_falt", "bydeler", "hele_byen", "belop")} == {
        "stemmer": None, "forslag_falt": 0, "innstilling_falt": False, "bydeler": [], "hele_byen": False, "belop": None}
    assert s["lesetid"] == 1 and s["fakta"] == [{"type": "bakgrunn", "etikett": "Artikkel", "verdi": "Ca. 1 min lesing"}]


def test_score_signals_expose_the_vote_as_numbers():
    p = {"vedtak": "Bystyret vedtar planen.", "voteringer": [
        {"forslag": "Innstillingen", "resultat": "falt", "stemmer": 31, "partier": [], "enstemmig": False},
        {"forslag": "Alternativforslag", "resultat": "vedtatt", "stemmer": 36, "partier": [], "enstemmig": False}],
        "innstilling_falt": True}
    sig = score.signals({"tittel": "Plan", "utvalgId": BYSTYRET}, p, {})
    assert (sig["split"], sig["stemmer_for"], sig["stemmer_mot"]) == ("36–31", 36, 31)
    assert sig["innstilling_falt"] and sig["forslag_falt"] == 1


# --- front page: 15 stories ----------------------------------------------------

def fake_sak(i, score_, sammendrag=True, mote=f"{BYSTYRET}-2026-09-23", tema="annet"):
    d = date.fromisoformat(mote.split("-", 1)[1])
    return {"nr": f"{i}/26", "score": score_, "dato": d, "dato_tekst": bygg.nb_date(d), "seq": i,
            "fremhevet": score_ >= 40, "har_sammendrag": sammendrag, "tema": tema, "mote": mote,
            "organ": "Bergen bystyre", "organ_kort": "Bystyret"}


def test_front_page_fills_to_15_stories():
    window = [fake_sak(i, 90 - i) for i in range(5)]                       # 5 over the threshold
    window += [fake_sak(10 + i, 30 - i) for i in range(15)]               # 15 below, with a summary
    window += [fake_sak(40, 85, sammendrag=False), fake_sak(41, 0)]       # no card / valg
    front = bygg.front_page(window)
    stories = [front["lead"], *front["sekundaer"], *front["liste"]]
    assert len(front["sekundaer"]) == 4 and len(front["liste"]) == 10 and len(stories) == 15
    assert [s["nr"] for s in stories[:5]] == ["0/26", "1/26", "2/26", "3/26", "4/26"]  # over the threshold first
    assert [s["score"] for s in stories[5:]] == sorted((s["score"] for s in stories[5:]), reverse=True)
    assert all(s["har_sammendrag"] and s["score"] > 0 for s in stories)
    # the rest go to the tiles (3 per tema) and the "andre" count; in a tile the
    # saker with a summary come first, a high score without a card only after them
    assert len(front["fliser"]) == 1 and len(front["fliser"][0]["saker"]) == 3
    assert [s["nr"] for s in front["fliser"][0]["saker"]] == ["20/26", "21/26", "22/26"]
    assert front["andre"] == len(window) - 15 - 3


def test_front_page_with_few_saker_and_standard_meeting():
    window = [fake_sak(1, 70), fake_sak(2, 10), fake_sak(3, 50, mote=f"{BYSTYRET}-2026-09-30")]
    front = bygg.front_page(window)
    assert [s["nr"] for s in [front["lead"], *front["sekundaer"]]] == ["1/26", "3/26", "2/26"] and front["liste"] == []
    assert front["standard"]["slug"] == f"{BYSTYRET}-2026-09-23"  # 2 of the 3 stories come from it
    assert [m["slug"] for m in front["panel_moter"]] == [f"{BYSTYRET}-2026-09-30", f"{BYSTYRET}-2026-09-23"]  # newest first
    assert bygg.front_page([])["lead"] is None


def test_organ_and_date_only_off_the_standard_meeting(monkeypatch, tmp_path, data_dir):
    items = ITEMS + [item("250/26", "Ekstra sak fra et annet møte", dato="2026-09-30", seq=2)]
    (data_dir / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in items}))
    red = data_dir / "redigert" / BYSTYRET / "2026-09-30"
    red.mkdir(parents=True)
    (red / "250-26.json").write_text(json.dumps({**CARD, "nr": "250/26", "overskrift": "Ekstra sak fra et annet møte"}))
    monkeypatch.setitem(SCORES, "250/26", 50)
    site = tmp_path / "site-std"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    forside = (site / "bergen/index.html").read_text()
    assert "Fra bystyret 23. september 2026 der ikke annet står" in forside
    sek = {re.search(r'<h3 class="sek-tittel"><a[^>]*>([^<]+)<', b).group(1): b
           for b in re.findall(r'<article class="blokk sek">(.*?)</article>', forside, re.S)}
    assert '<li class="faktum faktum-mote"><span>Bystyret, <time datetime="2026-09-30">30. sep.</time></span></li>' in sek["Ekstra sak fra et annet møte"]
    assert "kicker-del" not in sek["Ekstra sak fra et annet møte"]  # the kicker is the tema alone
    assert "faktum-mote" not in sek["Skolen rehabiliteres"]
    # the lead always says where and when
    assert '<span class="kicker-del">Bystyret, <time datetime="2026-09-23">23. september</time></span>' in lead_html(forside)


def test_static_folder_is_copied_and_fonts_linked(site):
    site, _ = site
    src = Path(bygg.__file__).parent / "static"
    for f in src.rglob("*"):
        if f.is_file():
            assert (site / "static" / f.relative_to(src)).exists(), f
    forside = (site / "bergen/index.html").read_text()
    if (src / "fonts.css").exists():
        assert forside.index('href="../static/fonts.css"') < forside.index('href="../static/style.css"')
    css = (site / "static/style.css").read_text()
    assert "fonts.googleapis" not in css and "@import" not in css
    assert all("googleapis" not in p.read_text() for p in site.rglob("*.html"))


def test_no_jargon_or_unchecked_text_on_public_pages(site):
    site, _ = site
    for page in site.rglob("*.html"):
        text = page.read_text()
        low = text.lower()
        for word in ("fremhevet", "angår mange", "maskinvurdering"):
            assert word not in low, (page, word)
        assert ">Vedtatt<" not in text and "Status: </span>Vedtatt<" not in text, page
        # classifier reasoning is never fact-checked, so it never reaches a reader
        assert "Investeringsramme." not in text and "Angår mange familier." not in text


def test_window_leaves_out_saker_waiting_for_their_protokoll():
    venter = {**fake_sak(1, 80), "venter_protokoll": True}
    klar = {**fake_sak(2, 20), "venter_protokoll": False}
    assert bygg._window([venter, klar], date(2026, 9, 30), 14) == [klar]
    assert bygg._window([venter, klar], date(2026, 9, 30), None) == [klar]


def test_hidden_from_search_until_launch(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", True)
    bygg.build(tmp_path / "skjult", data_dir, idag=date(2026, 10, 1))
    pages = list((tmp_path / "skjult").rglob("*.html"))
    assert pages and all('<meta name="robots" content="noindex, nofollow">' in p.read_text() for p in pages)
    assert (tmp_path / "skjult" / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", False)
    bygg.build(tmp_path / "apen", data_dir, idag=date(2026, 10, 1))
    assert not (tmp_path / "apen" / "robots.txt").exists()
    assert "noindex" not in (tmp_path / "apen" / "bergen" / "index.html").read_text()


def test_goatcounter_is_self_hosted_and_explained(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(config, "GOATCOUNTER", "https://eksempel.goatcounter.com/count")
    monkeypatch.setattr(config, "GOATCOUNTER_VIA_SIDEN", True)
    bygg.build(tmp_path / "med", data_dir, idag=date(2026, 10, 1))
    forside = (tmp_path / "med" / "bergen" / "index.html").read_text()
    assert '<script data-goatcounter="../count" async src="../static/count.js"></script>' in forside
    assert "globalPrivacyControl" in forside  # readers who say no aren't counted
    assert "/count https://eksempel.goatcounter.com/count 200" in (tmp_path / "med" / "_redirects").read_text().splitlines()
    assert "gc.zgo.at" not in forside and (tmp_path / "med" / "static" / "count.js").exists()
    assert "Besøkstall" in (tmp_path / "med" / "om" / "index.html").read_text()
    monkeypatch.setattr(config, "GOATCOUNTER", None)
    bygg.build(tmp_path / "uten", data_dir, idag=date(2026, 10, 1))
    assert "goatcounter" not in (tmp_path / "uten" / "bergen" / "index.html").read_text()
    assert "Besøkstall" not in (tmp_path / "uten" / "om" / "index.html").read_text()
    redirects = tmp_path / "uten" / "_redirects"
    assert not redirects.exists() or "/count" not in redirects.read_text()
