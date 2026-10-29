import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tingposten import bygg, grafikk, pdf, score

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
        {"tekst": "Hallen har vært planlagt siden 2019. <b>fet</b>", "kilder": [{"dok": "saksframlegg", "side": 2}]},
        {"tekst": "Byggestart er satt til våren 2027.", "kilder": [{"dok": "protokoll", "side": 1}, {"dok": "bilag", "side": 9}]},
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
    for page in sorted(site.rglob("*.html")):
        p = Links()
        p.feed(page.read_text())
        assert p.nested == 0, page.relative_to(site)
        for ref in p.refs:
            assert not ref.startswith("/"), (page, ref)
            if re.match(r"^[a-z]+:", ref) or ref.startswith("#"):
                continue
            target = (page.parent / ref.split("#")[0]).resolve()
            if ref.split("#")[0].endswith("/") or target.is_dir():
                target = target / "index.html"
            assert target.exists(), (page.relative_to(site), ref)


def test_lead_is_highest_score_with_a_summary(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    lead = re.search(r'<article class="blokk lead">(.*?)</article>', forside, re.S).group(1)
    # 243/26 scores 90 but has no card, so the lead is 238/26 (70)
    assert "Klarsignal til å bygge Eksempelhallen" in lead
    assert "Sak med høy score men uten kort" in forside  # still listed, in its tema section
    secondary = re.findall(r'<article class="blokk sekundaer">(.*?)</article>', forside, re.S)
    assert len(secondary) == 1 and "Skolen rehabiliteres" in secondary[0]
    annet = re.search(r'id="tema-annet">.*?</section>', forside, re.S).group(0)
    # fremhevede without a summary: 243 (90, no card) before 241 (45, avvist)
    assert annet.index("Sak med høy score men uten kort") < annet.index("Sak som ble avvist av redaktøren")
    assert "Alle 5 vedtak i perioden" in forside  # 240 (score 10) is in its section, so nothing is left over


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
    assert '<h1 class="artikkel-tittel">Klarsignal til å bygge Eksempelhallen</h1>' in page
    # dek = first sentence, abbreviation "mill." doesn't end it; body keeps the rest, nothing repeated
    assert '<p class="dek">Bystyret ga klarsignal for 69 mill. kroner til hallen.</p>' in page
    body = page.split("<body", 1)[1]
    assert body.count("Bystyret ga klarsignal") == 1
    assert "Arbeidet starter i 2027." in page
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/222/Protokoll-x#page=1" in page
    assert "Flere saker fra samme møte" in page and "Skolen rehabiliteres" in page
    assert "github.com/v3x4t0r/tingposten/issues/new?title=Feil+i+sak+238%2F26" in page
    assert 'href="../../../../../bergen/mote/' + MOTE + '/"' in page


def test_one_sentence_dek_takes_its_sources_and_status(site):
    site, _ = site
    page = sak_page(site, "239/26").read_text()
    dek = re.search(r'<p class="dek">(.*?)</p>', page, re.S).group(1)
    assert dek.startswith("Skolen rehabiliteres for 876 millioner kroner.")
    assert "protokoll s.&nbsp;1" in dek  # the hva_endres source moved with it
    assert ">Hva endres<" not in page
    assert "Sendt tilbake – ikke avgjort ennå" in page and "status-apen" in page
    assert "Maskinvurdering: Angår mange familier." in page


def test_avvist_and_missing_cards(site):
    site, _ = site
    for nr, title in (("241/26", "Sak som ble avvist av redaktøren"), ("243/26", "Sak med høy score men uten kort")):
        page = sak_page(site, nr).read_text()
        assert f'<h1 class="artikkel-tittel">{title}</h1>' in page
        assert "Sammendrag kommer." in page
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
    assert "Fremhevet" in page and "er-fremhevet" in page


def test_rss_links_to_sak_pages(site):
    site, _ = site
    root = ET.parse(site / "bergen/rss.xml").getroot()
    links = [i.findtext("link") for i in root.findall("./channel/item")]
    assert len(links) == 5
    assert all(l.startswith(f"https://v3x4t0r.github.io/tingposten/bergen/sak/{BYSTYRET}/2026-09-23/") for l in links)


def test_om_page_publishes_weights(site):
    site, _ = site
    om = (site / "om/index.html").read_text()
    for name in ("Beløp", "Rekkevidde", "Juridisk vekt"):
        assert name in om
    assert "Plassholder" in om


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
    assert page.count('<figure class="figur figur-') == 2 and 'figur-liten' not in page
    assert "Kart: © OpenStreetMap-bidragsytere" in page
    assert "tpf-0123abcd-571424-238-26-t" in page  # ids made unique per sak
    forside = (site / "bergen/index.html").read_text()
    lead = re.search(r'<article class="blokk lead">(.*?)</article>', forside, re.S).group(1)
    assert lead.count("<figure") == 1 and "OpenStreetMap" in lead  # map preferred on the front page
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
])
def test_split_first_sentence(text, first):
    assert bygg.split_first_sentence(text)[0] == first


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


def test_tema_sections_pages_and_breadcrumb(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    # sections ordered by best score: annet (243: 90) before okonomi (240: 10)
    assert forside.index('id="tema-annet"') < forside.index('id="tema-okonomi"')
    assert "Flere vedtak" not in forside
    annet = re.search(r'id="tema-annet">.*?</section>', forside, re.S).group(0)
    assert 'href="../bergen/tema/annet/">Flere saker (3)</a>' in annet  # 238 is the lead, so 3 > 2 shown
    okonomi = re.search(r'id="tema-okonomi">.*?</section>', forside, re.S).group(0)
    assert "Bofellesskapet blir dyrere" in okonomi and "Flere saker" not in okonomi
    page = (site / "bergen/tema/okonomi/index.html").read_text()
    assert "<h1>Økonomi</h1>" in page and "Bofellesskapet blir dyrere" in page and "Andre temaer" in page
    assert (site / "bergen/i-ar/tema/annet/index.html").exists() and (site / "bergen/30-dager/tema/skole/index.html").exists()
    assert "Gammel sak fra januar" in (site / "bergen/i-ar/tema/annet/index.html").read_text()
    sak = sak_page(site, "238/26").read_text()
    assert 'href="../../../../../bergen/i-ar/tema/annet/">Annet</a>' in sak
    assert '<span class="kicker-tema">Annet</span>' in sak
    assert '<span class="kicker-tema">Økonomi</span>' in sak_page(site, "240/26").read_text()


def test_article_rendering_and_cue(site):
    site, _ = site
    page = sak_page(site, "238/26").read_text()
    bakgrunn = re.search(r'<section class="bakgrunn".*?</section>', page, re.S).group(0)
    assert ">Bakgrunn<" in bakgrunn
    assert "Hallen har vært planlagt siden 2019. &lt;b&gt;fet&lt;/b&gt;" in bakgrunn  # escaped
    assert "/fil/bk360/111/Framstilling-x#page=2" in bakgrunn and "/fil/bk360/222/Protokoll-x#page=1" in bakgrunn
    assert "bilag" not in bakgrunn  # only the two known documents become links
    assert bakgrunn.index("Hallen har vært") < bakgrunn.index("Byggestart")
    assert page.index('class="brodtekst"') < page.index('class="bakgrunn"')
    assert "AVVIST ARTIKKEL" not in sak_page(site, "239/26").read_text()
    assert 'class="bakgrunn"' not in sak_page(site, "240/26").read_text()
    forside = (site / "bergen/index.html").read_text()
    lead = re.search(r'<article class="blokk lead">(.*?)</article>', forside, re.S).group(1)
    assert '<span class="cue">Les bakgrunn</span>' in lead
    sekundaer = re.search(r'<article class="blokk sekundaer">(.*?)</article>', forside, re.S).group(1)
    assert "Les bakgrunn" not in sekundaer


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
