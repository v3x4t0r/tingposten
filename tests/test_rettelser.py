"""Rettelser: the editor's visible corrections, kept apart from the pipeline's
files and applied when the site is built (rettelser.py)."""

import json
import logging
from datetime import date

import pytest

from tingposten import bygg, cli, config, pdf, rettelser, score

BYSTYRET = "571424"
DATO = "2026-10-27"
NB = " "
NN = " "
KEY = f"{BYSTYRET}/{DATO}/250-26"
URL = f"https://tingposten.no/bergen/sak/{KEY}/"


def item(nr, tittel, seq):
    return {"id": f"{nr}:{DATO}", "utvalgId": BYSTYRET, "moteDato": DATO, "seq": seq,
            "kategori": "Saker til behandling", "tittel": tittel, "referansenr": nr,
            "innstillingFilId": "111", "innstillingAlternativBeskrivelse": "Framstilling-x",
            "vedtakFilId": "222", "vedtakAlternativBeskrivelse": "Protokoll-x"}


ITEMS = [item("250/26", "Prinsipper for fordelingsmodell for skolene", 1),
         item("251/26", "Tilskudd til idrettslag", 2)]
HVA = "Bystyret vedtok en ny modell for skolene. Prinsippene gjelder fra 2027. Hver skole får et grunnbeløp."
CARD = {"nr": "250/26", "overskrift": "Nye prinsipper for hvordan skolene får penger",
        "felt": {"hva_endres": HVA, "hvem_pavirkes": "Alle grunnskolene i kommunen.",
                 "hvorfor": "Modellen fra 2004 var utdatert.", "status": "Vedtatt med 45 mot 22 stemmer.",
                 "nar_og_hvor": ""},
        "kilder": [{"felt": "hva_endres", "dok": "saksframlegg", "side": 3},
                   {"felt": "hvem_pavirkes", "dok": "saksframlegg", "side": 4},
                   {"felt": "hvorfor", "dok": "saksframlegg", "side": 7},
                   {"felt": "status", "dok": "protokoll", "side": 2}],
        "slettet": [], "avvist": False, "grunn": ""}
SJEKK = {"nr": "250/26", "setninger": [
    {"felt": "hva_endres", "setning": "Bystyret vedtok en ny modell for skolene.", "stottet": True,
     "kilde": {"dok": "saksframlegg", "side": 9}}]}
ARTIKKEL = {"nr": "250/26", "avvist": False,
            "avsnitt": [{"tekst": "Modellen fra 2004 ga skolene midler etter elevtall.",
                         "kilder": [{"dok": "saksframlegg", "side": 2}]},
                        {"tekst": "Rektorene ba om et grunnbeløp.", "kilder": [{"dok": "saksframlegg", "side": 5}]},
                        {"tekst": "Ordningen evalueres i 2029.", "kilder": [{"dok": "saksframlegg", "side": 8}]}]}
PROTOKOLL = "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/222/Protokoll-x"
SAKSFRAMLEGG = "https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/111/Framstilling-x"


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "agendapunkter").mkdir(parents=True)
    (d / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in ITEMS}))
    for sub, obj in (("redigert", CARD), ("sjekk", SJEKK), ("artikler-redigert", ARTIKKEL)):
        (d / sub / KEY).parent.mkdir(parents=True, exist_ok=True)
        (d / sub / f"{KEY}.json").write_text(json.dumps(obj))
    return d


@pytest.fixture(autouse=True)
def fake_rank(monkeypatch):
    def rank(items, pdf_dir, **kw):
        return [{"nr": i["referansenr"], "tittel": i["tittel"], "score": 50, "grunner": [],
                 "status": {"kode": "vedtatt", "tekst": "Vedtatt"}, "signaler": {}} for i in items]
    monkeypatch.setattr(score, "rank", rank)


def rett(data_dir, *entries, key=KEY):
    path = rettelser.sti(data_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(entries), ensure_ascii=False))


def bygg_side(tmp_path, data_dir):
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 30))
    return site


def side(site, key=KEY):
    return (site / "bergen" / "sak" / key / "index.html").read_text()


def felt_html(page, label):
    """The Kort fortalt row of one field."""
    start = page.index(f"<h3>{label}")
    return page[start:page.index('<div class="felt lag">', start) if '<div class="felt lag">' in page[start:]
                else page.index('class="meld-feil', start)]


def sak(data_dir, key=KEY):
    i = next(i for i in ITEMS if pdf.key(i) == key)
    return bygg._sak(i, {}, data_dir)


# --- applying ---------------------------------------------------------------------

def test_replacement_keeps_the_sources_and_marks_the_field(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Vi skrev at modellen var fra 2004. Den er fra 2006.",
                    "felt": "hvorfor", "tekst": "Modellen fra 2006 var utdatert."})
    page = side(bygg_side(tmp_path, data_dir))
    hvorfor = felt_html(page, "Hvorfor")
    assert f"Modellen fra 2006{NB}var utdatert." in hvorfor and f"2004{NB}var utdatert" not in page
    assert f'href="{SAKSFRAMLEGG}#page=7"' in hvorfor           # the field's own sources stay
    assert '<a class="rettet-merke" href="#rettelser">Rettet</a>' in hvorfor
    assert "rettet-merke" not in felt_html(page, "Hvem påvirkes")  # only the changed field
    # the pipeline's file is never touched
    assert json.loads((data_dir / "redigert" / f"{KEY}.json").read_text()) == CARD


def test_replacement_with_new_sources(data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Feil tall.", "felt": "status",
                    "tekst": "Vedtatt med 54 mot 13 stemmer.", "kilder": [{"dok": "protokoll", "side": 3}]})
    status = next(f for f in sak(data_dir)["kort_felt"] if f["key"] == "status")
    assert status["tekst"] == "Vedtatt med 54 mot 13 stemmer."
    assert status["kilder"] == [{"dok": "protokoll", "side": 3, "href": f"{PROTOKOLL}#page=3"}]


def test_unusable_sources_keep_the_fields_own(data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Feil tall.", "felt": "status",
                    "tekst": "Vedtatt med 54 mot 13 stemmer.", "kilder": [{"dok": "vedlegg", "side": 3}]})
    status = next(f for f in sak(data_dir)["kort_felt"] if f["key"] == "status")
    assert status["tekst"] == "Vedtatt med 54 mot 13 stemmer." and [k["side"] for k in status["kilder"]] == [2]


def test_removal(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Begrunnelsen sto ikke i saksframlegget, og er fjernet.",
                    "felt": "hvorfor", "tekst": ""})
    s = sak(data_dir)
    assert "hvorfor" not in [f["key"] for f in s["kort_felt"]] and s["rettet"] == ["hvorfor"]
    hvorfor = felt_html(side(bygg_side(tmp_path, data_dir)), "Hvorfor")
    assert "felt-tom" in hvorfor and "rettet-merke" in hvorfor and "utdatert" not in hvorfor


def test_note_only_changes_no_text(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Saken ble først lagt under feil møte."})
    s = sak(data_dir)
    assert s["rettet"] == [] and [n["merknad"] for n in s["rettelser"]] == ["Saken ble først lagt under feil møte."]
    assert [f["tekst"] for f in s["kort_felt"]] == [v for v in CARD["felt"].values() if v]
    page = side(bygg_side(tmp_path, data_dir))
    assert "Saken ble først lagt under feil møte." in page and "rettet-merke" not in page


def test_overskrift(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Overskriften var for bred.", "felt": "overskrift",
                    "tekst": "Ny modell for pengene til grunnskolene"})
    page = side(bygg_side(tmp_path, data_dir))
    assert "<title>Ny modell for pengene til grunnskolene · Tingposten</title>" in page
    assert ">Ny modell for pengene til grunnskolene</h1>" in page


def test_article_paragraph_and_ingress(tmp_path, data_dir):
    rett(data_dir,
         {"dato": "2026-10-30", "merknad": "Det var foreldrene, ikke rektorene.", "felt": "artikkel:2",
          "tekst": "Foreldreutvalget ba om et grunnbeløp."},
         {"dato": "2026-10-30", "merknad": "Ingressen manglet.", "felt": "ingress",
          "tekst": "Skolene får penger etter en ny modell.", "kilder": [{"dok": "saksframlegg", "side": 2}]})
    s = sak(data_dir)
    assert s["har_ingress"] and s["dek"] == "Skolene får penger etter en ny modell."
    assert s["dek_kilder"][0]["side"] == 2 and "ingress" in s["rettet"]
    assert [a["tekst"] for a in s["artikkel"]] == [ARTIKKEL["avsnitt"][0]["tekst"], "Foreldreutvalget ba om et grunnbeløp.",
                                                   ARTIKKEL["avsnitt"][2]["tekst"]]
    assert [a["rettet"] for a in s["artikkel"]] == [False, True, False]
    assert s["artikkel"][1]["kilder"][0]["side"] == 5            # the paragraph's sources stay
    page = side(bygg_side(tmp_path, data_dir))
    p2 = page[page.index('id="avsnitt-2"'):page.index('id="avsnitt-3"')]
    assert "Foreldreutvalget ba om et grunnbeløp." in p2 and 'class="rettet-merke"' in p2
    assert "rektorene" not in page.lower().replace("ikke rektorene", "")
    # the pipeline's article is never touched
    assert json.loads((data_dir / "artikler-redigert" / f"{KEY}.json").read_text()) == ARTIKKEL


def test_removed_paragraph_keeps_the_numbering_of_later_corrections(data_dir):
    rett(data_dir, {"dato": "2026-10-29", "merknad": "Avsnitt 1 er fjernet.", "felt": "artikkel:1", "tekst": ""},
         {"dato": "2026-10-30", "merknad": "Evalueringen er i 2030.", "felt": "artikkel:3",
          "tekst": "Ordningen evalueres i 2030."})
    s = sak(data_dir)
    assert [a["tekst"] for a in s["artikkel"]] == ["Rektorene ba om et grunnbeløp.", "Ordningen evalueres i 2030."]


def test_missing_target_shows_the_note_and_warns(tmp_path, data_dir, caplog):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Avsnitt 9 var feil.", "felt": "artikkel:9", "tekst": "Ny."},
         {"dato": "2026-10-30", "merknad": "Ukjent felt.", "felt": "ingressen", "tekst": "Ny."})
    # a sak without a card: the note still shows
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Kortet var feil og er tatt ned.", "felt": "hvorfor",
                    "tekst": "Ny."}, key=f"{BYSTYRET}/{DATO}/251-26")
    with caplog.at_level(logging.WARNING, logger="tingposten.rettelser"):
        site = bygg_side(tmp_path, data_dir)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("artikkel:9" in w and "finnes ikke" in w for w in warnings)
    assert any("ingressen" in w and "ukjent felt" in w for w in warnings)
    assert any("251-26" in w and "hvorfor" in w for w in warnings)
    page = side(site)
    assert f"Avsnitt 9{NB}var feil." in page and "Ukjent felt." in page and "rettet-merke" not in page
    assert "Kortet var feil og er tatt ned." in side(site, f"{BYSTYRET}/{DATO}/251-26")


def test_regenerated_text_is_not_overwritten(data_dir, caplog):
    """With "var" (written by `tingposten rett`), a correction only replaces the text it
    was made against: a card the pipeline wrote again keeps its new text, with the note."""
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Feil år.", "felt": "hvorfor",
                    "tekst": "Modellen fra 2006 var utdatert.", "var": "Modellen fra 2002 var utdatert."})
    with caplog.at_level(logging.WARNING, logger="tingposten.rettelser"):
        s = sak(data_dir)
    assert next(f["tekst"] for f in s["kort_felt"] if f["key"] == "hvorfor") == "Modellen fra 2004 var utdatert."
    assert s["rettet"] == [] and s["rettelser"][0]["brukt"] is False
    assert any("endret siden rettelsen" in r.getMessage() for r in caplog.records)


def test_corrections_apply_in_date_order(tmp_path, data_dir):
    rett(data_dir,  # out of order in the file
         {"dato": "2026-10-30", "merknad": "Andre rettelse.", "felt": "hvorfor", "tekst": "Tredje versjon."},
         {"dato": "2026-10-28", "merknad": "Første rettelse.", "felt": "hvorfor", "tekst": "Andre versjon."})
    s = sak(data_dir)
    assert next(f["tekst"] for f in s["kort_felt"] if f["key"] == "hvorfor") == "Tredje versjon."
    assert [n["merknad"] for n in s["rettelser"]] == ["Første rettelse.", "Andre rettelse."]
    page = side(bygg_side(tmp_path, data_dir))
    assert page.index("Første rettelse.") < page.index("Andre rettelse.")


def test_an_entry_without_a_note_is_never_applied(data_dir, caplog):
    rett(data_dir, {"dato": "2026-10-30", "felt": "hvorfor", "tekst": "Stille endring."},
         {"dato": "i dag", "merknad": "Uten dato.", "felt": "hvorfor", "tekst": "Stille endring."})
    with caplog.at_level(logging.WARNING, logger="tingposten.rettelser"):
        s = sak(data_dir)
    assert s["rettelser"] == [] and "Stille endring." not in json.dumps(s["kort_felt"], ensure_ascii=False)
    assert len([r for r in caplog.records if "brukes ikke" in r.getMessage()]) == 2


def test_unreadable_file_never_stops_the_build(tmp_path, data_dir):
    path = rettelser.sti(data_dir, KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ikke json")
    assert "Kort fortalt" in side(bygg_side(tmp_path, data_dir))


# --- rendering --------------------------------------------------------------------

def test_notice_under_the_top_block_before_the_card(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Vi skrev 45 stemmer. Riktig er 54.", "felt": "status",
                    "tekst": "Vedtatt med 54 mot 13 stemmer."})
    page = side(bygg_side(tmp_path, data_dir))
    notice = page[page.index('<section class="rettelser'):page.index("</section>", page.index('<section class="rettelser'))]
    assert 'id="rettelser"' in notice and '<h2 class="rettelser-etikett" id="rettelser-tittel">Rettet</h2>' in notice
    assert f'<time datetime="2026-10-30">30. oktober 2026</time>: Vi skrev 45{NB}stemmer. Riktig er 54.' in notice
    assert page.index("</header>") < page.index('class="rettelser') < page.index('id="kort"')
    # no notice without a correction
    other = side(tmp_path / "site", f"{BYSTYRET}/{DATO}/251-26")
    assert 'class="rettelser' not in other


def test_dek_follows_a_corrected_hva_endres(data_dir):
    s = sak(data_dir)
    assert s["dek"] == "Bystyret vedtok en ny modell for skolene."
    assert s["dek_kilder"][0]["side"] == 9                       # the checker's own sentence and page
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Det var en endret modell, ikke en ny.", "felt": "hva_endres",
                    "tekst": "Bystyret vedtok en endret modell for skolene. Prinsippene gjelder fra 2027."})
    s = sak(data_dir)
    assert s["dek"] == "Bystyret vedtok en endret modell for skolene."
    assert [k["side"] for k in s["dek_kilder"]] == [3]          # no checked sentence now: the field's pages
    assert s["felt"][0] == {**s["felt"][0], "key": "hva_endres", "tekst": "Prinsippene gjelder fra 2027."}


def test_dek_keeps_its_checked_source_when_a_later_sentence_is_corrected(data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Prinsippene gjelder fra 2028.", "felt": "hva_endres",
                    "tekst": "Bystyret vedtok en ny modell for skolene. Prinsippene gjelder fra 2028."})
    s = sak(data_dir)
    assert s["dek"] == "Bystyret vedtok en ny modell for skolene." and s["dek_kilder"][0]["side"] == 9


def test_typo_filter_on_corrected_text_and_note(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-30", "merknad": "Beløpet var 2 330 692 kr, ikke 2 330 629 kr.",
                    "felt": "hva_endres", "tekst": "Hver skole får 2 330 692 kroner etter § 7-9."},
         {"dato": "2026-10-30", "merknad": "Feil år.", "felt": "artikkel:3", "tekst": "Ordningen evalueres ca. 2030."})
    page = side(bygg_side(tmp_path, data_dir))
    assert f"Hver skole får 2{NN}330{NN}692{NB}kroner etter §{NB}<span class=\"nb\">7-9</span>." in page  # dek and field
    assert f"Beløpet var 2{NN}330{NN}692{NB}kr, ikke 2{NN}330{NN}629{NB}kr." in page
    assert f"Ordningen evalueres ca.{NB}2030." in page


def test_rss_and_lists_say_rettet(tmp_path, data_dir):
    rett(data_dir, {"dato": "2026-10-29", "merknad": "Første merknad.", "felt": "hvorfor", "tekst": "Ny."},
         {"dato": "2026-10-30", "merknad": "Vi skrev 45 stemmer. Riktig er 54.", "felt": "status",
          "tekst": "Vedtatt med 54 mot 13 stemmer."})
    site = bygg_side(tmp_path, data_dir)
    rss = (site / "bergen" / "rss.xml").read_text()
    item_ = rss[rss.index(f"sak/{KEY}/"):]
    assert "<description>Rettet 30. oktober 2026: Vi skrev 45 stemmer. Riktig er 54. Bystyret vedtok" in item_
    assert rss.count("Rettet") == 1
    mote = (site / "bergen" / "mote" / f"{BYSTYRET}-{DATO}" / "index.html").read_text()
    assert mote.count('<li class="faktum faktum-rettet">Rettet</li>') == 1


# --- `tingposten rett` ------------------------------------------------------------

@pytest.mark.parametrize("arg", [URL, URL + "index.html", f"bergen/sak/{KEY}", f"/bergen/sak/{KEY}/", KEY,
                                 f"site/bergen/sak/{KEY}/index.html#kort"])
def test_sak_key(arg):
    assert rettelser.sak_key(arg) == KEY


def test_cli_writes_the_correction(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    assert cli.main(["rett", URL, "--merknad", "Vi skrev 2004. Riktig er 2006.", "--felt", "hvorfor",
                     "--tekst", "Modellen fra 2006 var utdatert.", "--kilde", "saksframlegg:8"]) == 0
    assert cli.main(["rett", f"bergen/sak/{KEY}", "--merknad", "Overskriften er gjort tydeligere."]) == 0
    saved = json.loads(rettelser.sti(data_dir, KEY).read_text())
    today = date.today().isoformat()
    assert saved == [{"dato": today, "merknad": "Vi skrev 2004. Riktig er 2006.", "felt": "hvorfor",
                      "tekst": "Modellen fra 2006 var utdatert.", "kilder": [{"dok": "saksframlegg", "side": 8}],
                      "var": "Modellen fra 2004 var utdatert."},
                     {"dato": today, "merknad": "Overskriften er gjort tydeligere."}]
    out = capsys.readouterr().out
    assert '"felt": "hvorfor"' in out and "tingposten bygg" in out
    # a second correction of the same field is checked against the corrected text
    assert cli.main(["rett", KEY, "--merknad", "Og 2007.", "--felt", "hvorfor", "--tekst", "Fra 2007."]) == 0
    assert json.loads(rettelser.sti(data_dir, KEY).read_text())[-1]["var"] == "Modellen fra 2006 var utdatert."
    s = sak(data_dir)
    hvorfor = next(f for f in s["kort_felt"] if f["key"] == "hvorfor")
    assert hvorfor["tekst"] == "Fra 2007." and [k["side"] for k in hvorfor["kilder"]] == [8]
    assert json.loads((data_dir / "redigert" / f"{KEY}.json").read_text()) == CARD


@pytest.mark.parametrize("args", [
    ["--felt", "fakta", "--tekst", "x"],              # unknown target
    ["--felt", "dek", "--tekst", "x"],                # the dek follows hva_endres
    ["--felt", "artikkel:4", "--tekst", "x"],         # the article has three paragraphs
    ["--felt", "hvorfor"],                            # a target needs a text
    ["--tekst", "x"],                                 # a text needs a target
    ["--felt", "hvorfor", "--tekst", "Modellen fra 2004 var utdatert."],  # nothing changes
    ["--felt", "hvorfor", "--tekst", "x", "--kilde", "side 5"],
])
def test_cli_rejects_a_bad_correction(data_dir, monkeypatch, args):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    assert cli.main(["rett", KEY, "--merknad", "Feil.", *args]) == 1
    assert not rettelser.sti(data_dir, KEY).exists()


def test_cli_rejects_an_unknown_sak_or_a_sak_without_a_card(data_dir, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    assert cli.main(["rett", f"{BYSTYRET}/{DATO}/999-26", "--merknad", "Feil."]) == 1
    assert cli.main(["rett", "https://tingposten.no/bergen/", "--merknad", "Feil."]) == 1
    other = f"{BYSTYRET}/{DATO}/251-26"
    assert cli.main(["rett", other, "--merknad", "Feil.", "--felt", "hvorfor", "--tekst", "x"]) == 1
    assert cli.main(["rett", other, "--merknad", "Saken mangler sammendrag fordi kortet ble tatt ned."]) == 0
