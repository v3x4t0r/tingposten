"""The sak page after the design review of 30.10: honest unknown states, a
sourced dek, the key facts on top, typography and the smaller fixes."""

import json
import re
from datetime import date

import pytest

from tingposten import bygg, pdf, score
from tingposten.protokoll import parse_pages

BYSTYRET = "571424"
NB = " "
NN = " "
PROTOKOLL = "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/222/Protokoll-x"
SAKSFRAMLEGG = "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/111/Framstilling-x"
LANG_TITTEL = ("Årstad, Gnr. 160, Bnr. 184 m.fl, Områdereguleringsplan Slettebakken, arealplanID 66340000 - "
               "sluttbehandling av reguleringsplan")


def item(nr, tittel, dato="2026-09-23", seq=5):
    return {"id": f"{nr}:{dato}", "utvalgId": BYSTYRET, "moteDato": dato, "seq": seq,
            "kategori": "Saker til behandling", "tittel": tittel, "referansenr": nr,
            "innstillingFilId": "111", "innstillingAlternativBeskrivelse": "Framstilling-x",
            "vedtakFilId": "222", "vedtakAlternativBeskrivelse": "Protokoll-x"}


ITEMS = [
    item("250/26", "Prinsipper for fordelingsmodell for skolene", seq=1),
    item("251/26", "Representantforslag om forsvarsinvesteringer", dato="2026-10-27", seq=1),
    item("252/26", LANG_TITTEL, seq=2),
    item("253/26", "Fritak fra verv og valg av nytt varamedlem", seq=3),
    item("254/26", "Reguleringsplan der innstillingen falt", seq=4),
]


def card(nr, overskrift, **felt):
    base = {"hva_endres": "", "hvem_pavirkes": "", "hvorfor": "", "status": "", "nar_og_hvor": ""}
    kilder = [{"felt": k, "dok": "protokoll", "side": 1} for k, v in felt.items() if v]
    return {"nr": nr, "overskrift": overskrift, "felt": {**base, **felt}, "kilder": kilder,
            "slettet": [], "avvist": False, "grunn": "", "ord": 0, "setninger": 0}


CARDS = {
    "250-26": card("250/26", "Nye prinsipper for hvordan skolene får penger",
                   hva_endres=("Bystyret vedtok en ny modell, til erstatning for modellen fra 2004. "
                               "Prinsippene gjelder fra 2027. Hver skole får 2 330 692 kroner."),
                   hvorfor="Vilkåret i kommuneloven § 7-9 var oppfylt, og 54 stemmer var for.",
                   status="Vedtatt med 36 mot 31 stemmer."),
    "253-26": card("253/26", "Ap får nytt varamedlem i Takstnemnda",
                   hva_endres="Bystyret valgte et nytt varamedlem.", status="Vedtatt enstemmig."),
    "254-26": card("254/26", "Plan for inntil 76 boliger ved bybanestoppet vedtatt med endringer etter at "
                             "innstillingen falt",
                   hva_endres="Bystyret vedtok planen med endringer."),
}
# The fact-checker's own sentences for 250/26, each with the page it found support on.
SJEKK_250 = {"nr": "250/26", "setninger": [
    {"felt": "overskrift", "setning": CARDS["250-26"]["overskrift"], "stottet": True,
     "kilde": {"dok": "protokoll", "side": 1}},
    {"felt": "hva_endres", "setning": "Bystyret vedtok en ny modell, til erstatning for modellen fra 2004.",
     "stottet": True, "kilde": {"dok": "saksframlegg", "side": 4}},
    {"felt": "hva_endres", "setning": "Prinsippene gjelder fra 2027.", "stottet": True,
     "kilde": {"dok": "saksframlegg", "side": 9}},
    {"felt": "hva_endres", "setning": "Hver skole får 2 330 692 kroner.", "stottet": True,
     "kilde": {"dok": "protokoll", "side": 2}},
]}
ARTIKKEL_250 = {"nr": "250/26", "avvist": False, "avsnitt": [
    {"tekst": "Modellen fra 2004 ga ca. 200 skoler midler etter elevtall.", "kilder": [{"dok": "saksframlegg", "side": 2}]},
]}
STATUS = {
    "250/26": {"kode": "vedtatt", "tekst": "Vedtatt"},
    "251/26": {"kode": "venter_protokoll", "tekst": "Venter på protokoll"},
    "252/26": {"kode": "ukjent", "tekst": ""},
    "253/26": {"kode": "valg", "tekst": "Valg og fritak"},
    "254/26": {"kode": "vedtatt", "tekst": "Vedtatt"},
}
SIGNALER = {
    "250/26": {"stemmer_for": 36, "stemmer_mot": 31, "uenighet": 0.9, "forslag_falt": 3, "belop_kroner": 866_000_000,
               "hele_byen": True},
    "254/26": {"innstilling_falt": True, "forslag_falt": 1},
}
# What the protokoll parser would read. 251/26 is on the agenda 27.10, but the
# protokoll on disk is still June's.
PROTOKOLLER = {
    "251-26": {"motedato": "2026-06-17", "vedtak": "Representantforslaget blir utsatt.", "voteringer": []},
    "254-26": {"motedato": "2026-09-23", "vedtak": "Bystyret vedtar planen.", "vedtak_side": 2, "voteringer": []},
}


@pytest.fixture
def side(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "agendapunkter").mkdir(parents=True)
    (d / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in ITEMS}))
    for i in ITEMS:
        tag = pdf.tag(i)
        if tag in CARDS:
            (d / "redigert" / pdf.key(i)).parent.mkdir(parents=True, exist_ok=True)
            (d / "redigert" / f"{pdf.key(i)}.json").write_text(json.dumps(CARDS[tag]))
        if tag in PROTOKOLLER:  # the PDF exists on disk; the parser below reads it
            (d / "pdf" / pdf.filename(i, "protokoll")).parent.mkdir(parents=True, exist_ok=True)
            (d / "pdf" / pdf.filename(i, "protokoll")).write_bytes(b"%PDF")
    key = pdf.key(ITEMS[0])
    for sub, obj in (("sjekk", SJEKK_250), ("artikler-redigert", ARTIKKEL_250)):
        (d / sub / key).parent.mkdir(parents=True, exist_ok=True)
        (d / sub / f"{key}.json").write_text(json.dumps(obj))

    def rank(items, pdf_dir, **kw):
        return [{"nr": i["referansenr"], "tittel": i["tittel"], "score": 50, "grunner": [],
                 "status": STATUS[i["referansenr"]], "signaler": SIGNALER.get(i["referansenr"], {})} for i in items]

    monkeypatch.setattr(score, "rank", rank)
    monkeypatch.setattr(bygg._Parsers, "protokoll", lambda self, path: PROTOKOLLER.get(path.name.split("_")[0], {}))
    site = tmp_path / "site"
    bygg.build(site, d, idag=date(2026, 10, 30))
    return lambda i: (site / "bergen" / "sak" / pdf.key(ITEMS[i]) / "index.html").read_text()


def body(page):
    """The sak itself, without "Fra samme møte"."""
    return page.split('<article class="sak">', 1)[1].split("</article>", 1)[0]


def test_waiting_for_the_protokoll_says_so_and_nothing_more(side):
    page = side(1)
    assert '<span class="vh">Status: </span>Venter på protokoll</span>' in page
    assert ("Protokollen fra møtet 27. oktober 2026 er ikke publisert ennå. Når den kommer, skriver vi et kort "
            "sammendrag med kilde til hver setning.") in page
    assert "Protokoll fra en tidligere behandling" in page and PROTOKOLL in page
    for gone in ("Kort fortalt", "Til da finner du vedtaket", "Under behandling", "Sammendrag kommer", 'id="kort"'):
        assert gone not in body(page), gone


def test_unknown_outcome_has_no_status_and_one_quiet_line(side):
    page = side(2)
    assert "faktum-status" not in body(page) and "Under behandling" not in page
    assert '<p class="sak-melding-tekst">Ikke oppsummert ennå.</p>' in page
    assert "De samme fem punktene" not in page and 'href="#kort"' not in page
    assert PROTOKOLL in page and SAKSFRAMLEGG in page  # the documents are still listed
    # the kommune's title as h1: the quieter class, and its typography
    h1 = re.search(r'<h1 class="([^"]+)">(.*?)</h1>', page).groups()
    assert h1[0] == "sak-tittel sak-tittel-raa"
    assert f"Gnr.{NB}160, Bnr.{NB}184{NB}m.fl," in h1[1] and f"66340000{NB}– sluttbehandling" in h1[1]


def test_long_card_headline_is_also_set_quieter(side):
    assert len(CARDS["254-26"]["overskrift"]) > 90
    assert '<h1 class="sak-tittel sak-tittel-raa">' in side(4)
    assert '<h1 class="sak-tittel">' in side(0)


def test_dek_is_the_checkers_first_sentence_with_its_own_source(side):
    page = side(0)
    dek = re.search(r'<p class="sak-dek">(.*?)</p>', page, re.S).group(1)
    # split_first_sentence wouldn't end a sentence after "2004."; the checker's sentence does
    assert dek.startswith("Bystyret vedtok en ny modell, til erstatning for modellen fra 2004.<span class=\"kilde\">")
    assert f'href="{SAKSFRAMLEGG}#page=4"' in dek and "#page=9" not in dek
    # "Hva endres" keeps its full text: the dek is its ingress, not cut out of it
    hva = re.search(r'<h3>Hva endres</h3>(.*?)</div></div>', page, re.S).group(1)
    assert "Bystyret vedtok en ny modell, til erstatning for modellen fra 2004. Prinsippene gjelder" in hva
    assert "Står i ingressen over." not in page


def test_dek_takes_the_field_sources_without_a_checked_sentence():
    felt = [{"key": "hva_endres", "label": "Hva endres", "tekst": "Én setning. To setninger.",
             "kilder": [{"dok": "protokoll", "side": 1, "href": "p#page=1"}]}]
    dek, kilder, rest = bygg._dek(felt, [])
    assert (dek, kilder) == ("Én setning.", felt[0]["kilder"])
    assert rest[0]["tekst"] == "To setninger."  # lists: the lead's "Hva endres" still follows the dek
    one = [{**felt[0], "tekst": "Bare én."}]
    assert bygg._dek(one, [{"tekst": "Bare én.", "kilder": []}]) == ("Bare én.", one[0]["kilder"], [])


def test_key_facts_and_status_pill_come_from_fakta(side):
    page = side(0)
    fakta = re.search(r'<ul class="fakta sak-fakta">(.*?)</ul>', page, re.S).group(1)
    assert "Hele kommunen" in fakta and "36–31" in fakta and f"866{NB}mill. kr" in fakta
    assert "Bakgrunn" not in fakta and "Status" not in fakta
    assert page.index('class="sak-dek"') < page.index("sak-fakta") < page.index('id="kort"')
    assert "faktum-status" not in body(page)  # "Vedtatt" is never a pill
    falt = side(4)
    assert '<span class="vh">Status: </span>Innstillingen falt</span>' in falt


def test_empty_status_row_is_filled_from_the_protokoll(side):
    page = side(4)
    status = re.search(r'<h3>Status</h3>(.*?)</div></div>', page, re.S).group(1)
    assert '<p class="felt-tekst">Vedtatt.</p>' in status
    assert f'href="{PROTOKOLL}#page=2"' in status
    hvem = re.search(r'<h3>Hvem påvirkes</h3>(.*?)</div></div>', page, re.S).group(1)
    assert 'class="felt-tom"' in hvem  # other empty rows keep the dash


def test_valg_is_a_kind_not_a_pill(side):
    page = side(3)
    assert "faktum-status" not in body(page)
    assert '<span class="nb">Valg og fritak</span>' in page and "Valg/fritak" not in page


def test_typography_on_card_and_article(side):
    page = side(0)
    assert f"2{NN}330{NN}692{NB}kroner" in page
    assert f'§{NB}<span class="nb">7-9</span>' in page and f"54{NB}stemmer" in page
    artikkel = re.search(r'<section class="artikkel lag".*?</section>', page, re.S).group(0)
    assert f"ca.{NB}200{NB}skoler" in artikkel


def test_article_heading_and_meld_feil_lines(side):
    page = side(0)
    artikkel = re.search(r'<section class="artikkel lag".*?</section>', page, re.S).group(0)
    assert '<h2 class="etikett etikett-strek artikkel-etikett" id="artikkel-tittel">Saken</h2>' in artikkel
    assert "Bakgrunn" not in page.split('id="artikkel"')[1].split("</section>")[0]
    kort = re.search(r'<section class="kort[^"]*".*?</section>', page, re.S).group(0)
    for part in (kort, artikkel):
        assert re.search(r'Ser du en feil\? <a href="https://github.com/[^"]+">Meld feil</a>', part)


def test_dateline_links_the_meeting_and_nav_drops_kort_without_summary(side):
    page = side(0)
    assert re.search(r'<a class="dateline-mote" href="\.\./\.\./\.\./\.\./\.\./bergen/mote/571424-2026-09-23/">'
                     r'<span class="nb">Bystyret</span><span class="skille" aria-hidden="true">&nbsp;· </span>', page)
    assert '<a href="#kort">Kort fortalt</a>' in page
    assert 'class="paa-siden"' not in side(2)  # nothing to jump to on a page without summary or article


def test_lists_get_the_typography_through_their_filters():
    env = bygg._env()
    out = env.from_string("{{ t|tittel }}|{{ d|forste_setning }}").render(
        t="Dokken–Lyngbø: 2 000 boliger <b>", d="Ca. 54 stemmer for. Neste setning.")
    assert out == f'<span class="nb">Dokken–Lyngbø</span>: 2{NN}000{NB}boliger &lt;b&gt;|Ca.{NB}54{NB}stemmer for.'


def test_figure_sources_are_links():
    i = ITEMS[0]
    vote = bygg._figur_kilder(i, {"kilde": "Protokollen, votering"}, {"vedtak_side": 1, "votering_side": 3})
    assert vote == [{"dok": "protokoll", "side": 3, "href": f"{PROTOKOLL}#page=3"}]
    assert bygg._figur_kilder(i, {"kilde": "Saksframlegg s. 5"}, {})[0]["href"] == f"{SAKSFRAMLEGG}#page=5"
    assert bygg._figur_kilder(i, {"kilde": "Vedtak s. 2"}, {})[0]["href"] == f"{PROTOKOLL}#page=2"
    assert bygg._figur_kilder(i, {"kilde": "Kart: © OpenStreetMap-bidragsytere (ODbL)."}, {}) == []


def test_figure_caption_links_its_source():
    t = bygg._env().from_string('{% from "macros.html" import figur_sak %}{{ figur_sak(s, f) }}')
    f = {"svg": "<svg></svg>", "type": "stemmer", "kilde": "Protokollen, votering",
         "kilder": [{"dok": "protokoll", "side": 3, "href": f"{PROTOKOLL}#page=3"}]}
    out = t.render(s={"bydeler": []}, f=f)
    assert ('<figcaption><span class="kilde"><span class="kilde-etikett">Kilde:</span>Protokoll s.&nbsp;'
            f'<a href="{PROTOKOLL}#page=3" aria-label="Protokoll, side 3">3</a></span>') in out
    # a figure from an older card, without page: the plain text as before
    old = t.render(s={"bydeler": []}, f={k: v for k, v in f.items() if k != "kilder"})
    assert "Kilde: protokollen, votering." in old


def test_protokoll_parser_finds_the_votering_page():
    from pathlib import Path
    pages = json.loads((Path(__file__).parent / "fixtures/protokoll/260-26_innstilling_falt.json").read_text())
    r = parse_pages(pages)
    assert (r["vedtak_side"], r["votering_side"]) == (1, 2)


def test_status_pill_kinds():
    k = bygg._status_class
    assert k({"kode": "venter_protokoll", "tekst": "Venter på protokoll"}) == "venter"
    assert k({"kode": "ukjent", "tekst": ""}) == "" and k({"kode": "valg", "tekst": "Valg og fritak"}) == ""
    assert k({"kode": "under_behandling", "tekst": "Under behandling"}) == "apen"
    assert k({"kode": "falt", "tekst": "Forslaget falt"}) == "falt"
    assert k({"kode": "vedtatt", "tekst": "Vedtatt"}) == ""
