"""The background article's own headline: written from the edited article,
checked by Codex against the pages the article cites, kept or dropped whole
by the redaktør, and redone only when its text or rules change."""

import json
import os
import time

import pytest

from tingposten import artikkeltittel, faktasjekker, kildetekst, pipeline, redaktor, skribent
from tingposten.llm import FakeLLM, LLMError, write_json

ITEM = {"id": "a", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "266/26", "tittel": "Bybanen",
        "kategori": "Saker til behandling", "vedtakFilId": "9", "innstillingFilId": "8", "seq": 1}
KEY = "571424/2026-09-23/266-26"
CARD_HEADLINE = "Bybanen til Åsane skal gå i tunnel under Dokken"
AVSNITT = [
    {"tekst": "Konsekvensutredningen vurderte sju traséalternativer for strekningen fra sentrum til Vestkanten. "
              "B1 ble rangert best i den samfunnsøkonomiske analysen, men byrådet mener B1 betjener Kirkebukten og "
              "gamle Laksevåg sentrum dårligere og gir lengre reisetid.",
     "kilder": [{"dok": "saksframlegg", "side": 5}, {"dok": "saksframlegg", "side": 7}]},
    {"tekst": "SV foreslo å planlegge sykkelbroen i indre trasé, slik plan- og bygningsetaten anbefalte.",
     "kilder": [{"dok": "protokoll", "side": 2}]},
]
EDITED = {"nr": "266/26", "avsnitt": AVSNITT, "slettet": [], "avvist": False, "grunn": "", "koder": [],
          "ord": 20, "ord_for": 20, "setninger_for": 3, "nye_fakta": 3}
KILDER = {
    "saksframlegg": "\n".join(f"--- side {n} ---\nSAKSFRAMLEGG SIDE {n}" for n in range(1, 10)),
    "protokoll": "--- side 1 ---\nPROTOKOLL SIDE 1\n--- side 2 ---\nPROTOKOLL SIDE 2",
}
TITLE = "Sju traseer ble vurdert før valget"


def verdict(stottet=True, noytral=True, gjentar=False, kilder=None, personvern=(), blocked=""):
    return {"stottet": stottet, "noytral": noytral, "gjentar_kortet": gjentar,
            "kilder": [{"dok": "saksframlegg", "side": 5}] if kilder is None else kilder,
            "begrunnelse": "Side 5 sier sju alternativer.", "personvern": list(personvern), "blocked": blocked}


def proposal(tittel=TITLE, blocked=""):
    return {"tittel": tittel, "blocked": blocked}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    write_json(tmp_path / "artikler-redigert" / f"{KEY}.json", EDITED)
    write_json(tmp_path / "artikler" / f"{KEY}.json", {"nr": "266/26", "avsnitt": AVSNITT, "meta": {}})
    write_json(tmp_path / "redigert" / f"{KEY}.json", {"nr": "266/26", "overskrift": CARD_HEADLINE, "avvist": False,
                                                       "felt": {}})
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: dict(KILDER))
    return tmp_path


def record(data_dir):
    return json.loads((data_dir / "artikkeltittel" / f"{KEY}.json").read_text())


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


# --- the writer ----------------------------------------------------------------

def test_writer_prompt_states_the_rules_and_shows_only_the_edited_article():
    fake = FakeLLM([proposal()])
    out = skribent.skriv_tittel(EDITED, CARD_HEADLINE, fake)
    assert out == {"tittel": TITLE, "grenser": [], "blocked": ""}
    prompt = fake.prompts[0]
    for rule in ("At most 55 characters (hard limit 60)", 'NEVER start with "Bystyret" or "Byrådet"',
                 "Do not repeat the card's headline", "No names of private persons", "Sentence case",
                 "Norwegian Bokmål", "Exactly as certain as the article", "Never evaluative or loaded",
                 '"Bakgrunn for saken"'):
        assert rule in prompt, rule
    assert AVSNITT[1]["tekst"] in prompt and CARD_HEADLINE in prompt
    assert "SAKSFRAMLEGG SIDE" not in prompt  # the article's text only, not the documents


@pytest.mark.parametrize("tittel, problem", [
    ("Konsekvensutredningen vurderte sju ulike traséalternativer for bybanen", "tegn (maks 55"),
    ("Bystyret valgte trasé etter sju alternativer", "begynner med organet"),
    ("Byrådet la fram sju alternativer", "begynner med organet"),
    ("Bakgrunn for saken", "merkelapp"),
    ("Sju traseer ble vurdert før valget.", "tegnsetting"),
    ("Hvorfor ble B1 valgt bort?", "forbudslisten"),
    ("En historisk trasé for bybanen", "forbudslisten"),
    (CARD_HEADLINE.lower() + "!", "gjentar kortets overskrift"),
    ("Sju Traseer Ble Vurdert Før Valget", "setningsform"),
])
def test_tittel_grenser(tittel, problem):
    assert any(problem in p for p in skribent.tittel_grenser(tittel, CARD_HEADLINE)), skribent.tittel_grenser(tittel, CARD_HEADLINE)


def test_tittel_grenser_passes_a_plain_headline():
    assert skribent.tittel_grenser(TITLE, CARD_HEADLINE) == []
    assert skribent.tittel_grenser("SV ville ha sykkelbroen i indre trasé i Bergen", CARD_HEADLINE) == []


def test_writer_gets_one_rewrite_then_reports_what_is_left():
    long = "Konsekvensutredningen vurderte sju ulike traséalternativer for bybanen"
    fake = FakeLLM([proposal("Bystyret valgte trasé"), proposal()])
    assert skribent.skriv_tittel(EDITED, CARD_HEADLINE, fake)["tittel"] == TITLE
    assert "BROKE THE HARD LIMITS" in fake.prompts[1] and "begynner med organet" in fake.prompts[1]
    out = skribent.skriv_tittel(EDITED, CARD_HEADLINE, FakeLLM([proposal(long), proposal(long)]))
    assert out["tittel"] == long and any("tegn" in p for p in out["grenser"])
    blocked = skribent.skriv_tittel(EDITED, CARD_HEADLINE, FakeLLM([proposal("x", blocked="Ingenting å bygge på.")]))
    assert blocked == {"tittel": "", "grenser": [], "blocked": "Ingenting å bygge på."}


# --- the check -----------------------------------------------------------------

def test_utdrag_keeps_only_the_given_pages():
    text = KILDER["saksframlegg"]
    assert kildetekst.utdrag(text, {5, 7}) == "--- side 5 ---\nSAKSFRAMLEGG SIDE 5\n--- side 7 ---\nSAKSFRAMLEGG SIDE 7"
    assert kildetekst.utdrag(text, set()) == ""


def test_check_sees_the_cited_pages_the_article_and_the_card_headline(data_dir):
    fake = FakeLLM([verdict()], name="fake:codex")
    out = faktasjekker.sjekk_tittel(ITEM, TITLE, EDITED, CARD_HEADLINE, data_dir, fake)
    assert out["tittel"] == TITLE and out["stottet"] is True
    assert out["meta"]["prompt_versjon"] == faktasjekker.TITTEL_PROMPT_VERSJON and out["meta"]["modell"] == "fake:codex"
    prompt = fake.prompts[0]
    assert "SAKSFRAMLEGG SIDE 5" in prompt and "SAKSFRAMLEGG SIDE 7" in prompt and "PROTOKOLL SIDE 2" in prompt
    assert "SAKSFRAMLEGG SIDE 6" not in prompt and "PROTOKOLL SIDE 1" not in prompt  # not cited by the article
    assert f"<<<TITTEL\n{TITLE}\nTITTEL>>>" in prompt and CARD_HEADLINE in prompt and AVSNITT[0]["tekst"] in prompt
    for rule in ("most likely to overstate", '"noytral"', '"gjentar_kortet"', "same certainty"):
        assert rule in prompt


def test_check_without_cited_pages_needs_no_model(data_dir):
    art = {**EDITED, "avsnitt": [{"tekst": "Uten kilder.", "kilder": []}]}
    out = faktasjekker.sjekk_tittel(ITEM, TITLE, art, CARD_HEADLINE, data_dir, FakeLLM([]))
    assert out["blocked"] and out["stottet"] is False


def test_tittel_schema_is_strict_for_codex():
    def walk(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
            for p in schema["properties"].values():
                walk(p)
        if "items" in schema:
            walk(schema["items"])
    walk(faktasjekker.TITTEL_SCHEMA)
    walk(skribent.TITTEL_SCHEMA)


# --- the redaktør ---------------------------------------------------------------

def checked(tittel=TITLE, avsnitt=AVSNITT, headline=CARD_HEADLINE, **v):
    return {"tittel": tittel, "grenser": [], "blocked": "", "kort_overskrift": headline,
            "grunnlag": redaktor.tittel_grunnlag(headline, avsnitt),
            "sjekk": {"tittel": tittel, **verdict(**v), "meta": {}}}


def test_redaktor_keeps_a_checked_headline_unchanged_with_its_pages():
    out = redaktor.rediger_tittel(checked(), AVSNITT, CARD_HEADLINE)
    assert out == {"tittel": TITLE, "tittel_kilder": [{"dok": "saksframlegg", "side": 5}], "tittel_grunn": ""}


@pytest.mark.parametrize("change, grunn", [
    (lambda r: r.update(sjekk=None), "ikke faktasjekket"),
    (lambda r: r["sjekk"].update(tittel="En annen tittel"), "ikke faktasjekket"),  # checked another headline
    (lambda r: r["sjekk"].update(blocked="dokument mangler"), "ikke faktasjekket: dokument mangler"),
    (lambda r: r["sjekk"].update(stottet=False), "ikke støttet"),
    (lambda r: r["sjekk"].update(noytral=False), "ikke nøytral"),
    (lambda r: r["sjekk"].update(gjentar_kortet=True), "gjentar kortets overskrift"),
    (lambda r: r["sjekk"].pop("gjentar_kortet"), "gjentar kortets overskrift"),
    (lambda r: r["sjekk"].update(personvern=["Privatperson navngitt"]), "personvern"),
    (lambda r: r["sjekk"].update(kilder=[{"dok": "saksframlegg", "side": 6}]), "sidene artikkelen bygger på"),
    (lambda r: r["sjekk"].update(kilder=[]), "sidene artikkelen bygger på"),
    (lambda r: r.update(grunnlag="annen tekst"), "laget for en annen tekst"),
    (lambda r: r.update(tittel=""), "ingen tittel"),
])
def test_redaktor_drops_what_is_not_checked_as_supported(change, grunn):
    r = checked()
    change(r)
    out = redaktor.rediger_tittel(r, AVSNITT, CARD_HEADLINE)
    assert out["tittel"] == "" and out["tittel_kilder"] == [] and grunn in out["tittel_grunn"]


@pytest.mark.parametrize("tittel, grunn", [
    ("Sju traseer ble vurdert før bystyret valgte tunnel under Dokken i 2026", "over 60 tegn"),
    ("Bystyret fikk sju traseer å velge mellom", "begynner med organet"),
    ("Byrådet vurderte sju traseer", "begynner med organet"),
    ("Bakgrunn for saken", "merkelapp"),
    ("Hvorfor ble B1 valgt bort?", "forbudsliste"),
    ("Sju traseer ble vurdert.", "tegnsetting"),
    (CARD_HEADLINE, "gjentar kortets overskrift"),
])
def test_redaktor_drops_headlines_over_the_hard_limits_even_when_checked(tittel, grunn):
    out = redaktor.rediger_tittel(checked(tittel), AVSNITT, CARD_HEADLINE)
    assert out["tittel"] == "" and grunn in out["tittel_grunn"]


def test_hard_limit_is_60_characters_the_writer_aims_at_55():
    t58 = "Sju traseer ble vurdert, og B1 kom best ut i analysen av dem"[:58]
    assert len(t58) == 58 and redaktor.tittel_brudd(t58) == [] and skribent.tittel_grenser(t58)
    assert redaktor.rediger_tittel(checked(t58), AVSNITT, CARD_HEADLINE)["tittel"] == t58
    assert redaktor.MAX_TITTEL == skribent.MAX_TITTEL == 60 and skribent.MAL_TITTEL == 55


def test_no_headline_for_a_rejected_or_empty_article():
    assert redaktor.rediger_tittel(checked(), [], CARD_HEADLINE)["tittel"] == ""


def test_run_artikler_merges_only_a_checked_headline(data_dir):
    write_json(data_dir / "artikkelsjekk" / f"{KEY}.json", {"setninger": [
        {"avsnitt": n, "setning": s, "stottet": True, "gjentar_kortet": False, "kilde": None, "begrunnelse": ""}
        for n, a in enumerate(AVSNITT, 1) for s in redaktor._segment(a["tekst"], skribent.setninger(a["tekst"]))],
        "personvern": [], "blocked": ""})
    out = data_dir / "artikler-redigert" / f"{KEY}.json"
    out.unlink()
    counts = redaktor.run_artikler([ITEM], data_dir)
    assert counts["publiserbar"] == 1 and counts["tittel"] == 0
    assert json.loads(out.read_text())["tittel"] == "" and json.loads(out.read_text())["tittel_grunn"] == "ingen tittel ennå"
    write_json(data_dir / "artikkeltittel" / f"{KEY}.json", checked())
    assert redaktor.run_artikler([ITEM], data_dir)["tittel"] == 1
    assert json.loads(out.read_text())["tittel"] == TITLE
    lines = [json.loads(x) for x in (data_dir / "metrikk.jsonl").read_text().splitlines()]
    # one article line (first edit), one headline line; adding the headline doesn't log the article again
    assert [x["type"] for x in lines] == ["artikkel", "artikkeltittel"] and lines[1]["tittel"] == TITLE
    write_json(data_dir / "artikkeltittel" / f"{KEY}.json", checked(stottet=False))
    assert redaktor.run_artikler([ITEM], data_dir)["tittel"] == 0
    assert json.loads(out.read_text())["tittel"] == ""


# --- the step -------------------------------------------------------------------

def test_lag_writes_checks_and_then_has_nothing_to_do(data_dir):
    assert artikkeltittel.trenger(ITEM, data_dir)
    writer, checker = FakeLLM([proposal()], name="fake:opus"), FakeLLM([verdict()], name="fake:codex")
    assert artikkeltittel.lag(ITEM, data_dir, writer, checker) == "støttet"
    r = record(data_dir)
    assert r["tittel"] == TITLE and r["sjekk"]["tittel"] == TITLE and r["sjekk"]["stottet"] is True
    assert r["meta"]["prompt_versjon"] == skribent.TITTEL_PROMPT_VERSJON and r["meta"]["modell"] == "fake:opus"
    assert r["grunnlag"] == redaktor.tittel_grunnlag(CARD_HEADLINE, AVSNITT)
    assert not artikkeltittel.trenger(ITEM, data_dir)
    assert artikkeltittel.lag(ITEM, data_dir, FakeLLM([]), FakeLLM([])) == "støttet"  # no model call


def test_a_failed_check_is_retried_next_run_without_the_writer(data_dir):
    writer = FakeLLM([proposal()])
    with pytest.raises(LLMError):
        artikkeltittel.lag(ITEM, data_dir, writer, FakeLLM([LLMError("usage limit"), LLMError("usage limit")]))
    assert record(data_dir)["tittel"] == TITLE and record(data_dir)["sjekk"] is None  # the proposal is kept
    assert artikkeltittel.trenger(ITEM, data_dir)
    assert artikkeltittel.lag(ITEM, data_dir, FakeLLM([]), FakeLLM([verdict()])) == "støttet"


def test_a_failed_writer_call_leaves_nothing_and_is_retried(data_dir):
    with pytest.raises(LLMError):
        artikkeltittel.lag(ITEM, data_dir, FakeLLM([LLMError("x"), LLMError("x")]), FakeLLM([]))
    assert not (data_dir / "artikkeltittel" / f"{KEY}.json").exists() and artikkeltittel.trenger(ITEM, data_dir)


def test_headline_over_the_hard_limits_is_not_sent_to_the_checker(data_dir):
    bad = "Bystyret valgte trasé"
    assert artikkeltittel.lag(ITEM, data_dir, FakeLLM([proposal(bad), proposal(bad)]), FakeLLM([])).startswith("bryter")
    assert record(data_dir)["sjekk"] is None and not artikkeltittel.trenger(ITEM, data_dir)


def test_staleness_follows_the_text_the_sources_and_the_prompt_versions(data_dir, monkeypatch):
    def done():
        artikkeltittel.lag(ITEM, data_dir, FakeLLM([proposal()]), FakeLLM([verdict()]))
        assert not artikkeltittel.trenger(ITEM, data_dir)

    done()
    edited = data_dir / "artikler-redigert" / f"{KEY}.json"
    write_json(edited, {**EDITED, "avsnitt": AVSNITT[:1]})  # the edited article lost a paragraph
    assert artikkeltittel.trenger(ITEM, data_dir)
    done()
    write_json(data_dir / "redigert" / f"{KEY}.json", {"overskrift": "Ny overskrift på kortet", "felt": {}})
    assert artikkeltittel.trenger(ITEM, data_dir)  # it may now repeat the card's headline
    done()
    pdf_file = data_dir / "pdf" / "571424/2026-09-23/266-26_protokoll.pdf"
    pdf_file.parent.mkdir(parents=True)
    pdf_file.write_bytes(b"%PDF")  # a newer protokoll on disk
    age(data_dir / "artikkeltittel" / f"{KEY}.json", 60)
    assert artikkeltittel.trenger(ITEM, data_dir)
    done()
    monkeypatch.setattr(skribent, "TITTEL_PROMPT_VERSJON", "artikkeltittel-99")
    assert artikkeltittel.trenger(ITEM, data_dir)
    done()
    # new checker rules: only the check is redone, the writer is not asked
    monkeypatch.setattr(faktasjekker, "TITTEL_PROMPT_VERSJON", "tittelsjekk-99")
    assert artikkeltittel.trenger(ITEM, data_dir)
    assert artikkeltittel.lag(ITEM, data_dir, FakeLLM([]), FakeLLM([verdict()])) == "støttet"
    assert not artikkeltittel.trenger(ITEM, data_dir)


def test_no_headline_work_for_a_rejected_article(data_dir):
    write_json(data_dir / "artikler-redigert" / f"{KEY}.json", {**EDITED, "avvist": True})
    assert not artikkeltittel.trenger(ITEM, data_dir)
    assert artikkeltittel.lag(ITEM, data_dir, FakeLLM([]), FakeLLM([])) == "ingen publiserbar artikkel"


# --- in the pipeline -------------------------------------------------------------

@pytest.fixture
def kjor_dir(data_dir, monkeypatch):
    monkeypatch.setattr(pipeline, "fra_dato", lambda idag=None: "2000-01-01")
    (data_dir / "agendapunkter").mkdir()
    other = {**ITEM, "id": "b", "referansenr": "267/26"}
    (data_dir / "agendapunkter" / "571424.json").write_text(json.dumps({"a": ITEM, "b": other}))
    other_key = "571424/2026-09-23/267-26"
    write_json(data_dir / "artikler-redigert" / f"{other_key}.json", EDITED)
    write_json(data_dir / "artikler" / f"{other_key}.json", {"nr": "267/26", "avsnitt": AVSNITT, "meta": {}})
    write_json(data_dir / "redigert" / f"{other_key}.json", {"overskrift": "Noe annet", "felt": {}})
    # fresh checks for both drafts, so the redaktør publishes them
    for key in (KEY, other_key):
        write_json(data_dir / "artikkelsjekk" / f"{key}.json", {"setninger": [
            {"avsnitt": n, "setning": s, "stottet": True, "gjentar_kortet": False, "kilde": None, "begrunnelse": ""}
            for n, a in enumerate(AVSNITT, 1) for s in skribent.setninger(a["tekst"])], "personvern": [], "blocked": ""})
    return data_dir


def test_kjor_titles_the_edited_articles_and_publishes_only_checked_ones(kjor_dir):
    writer = FakeLLM([proposal(), proposal("SV ville planlegge sykkelbroen i indre trasé")])
    checker = FakeLLM([verdict(), verdict(stottet=False)])
    result = pipeline.kjor(kjor_dir, steg=("artikkelrediger", "artikkeltittel"), parallel=1,
                           llms={"skriv": writer, "sjekk": checker})
    assert result["artikkeltittel"]["antall"] == {"støttet": 1, "ikke støttet": 1}
    assert result["artikkeltittel/rediger"]["tittel"] == 1
    titles = sorted(json.loads((kjor_dir / "artikler-redigert" / f"571424/2026-09-23/{t}.json").read_text())["tittel"]
                    for t in ("266-26", "267-26"))
    assert titles == ["", TITLE]
    # next run: nothing left to do, no model calls (the fakes are empty)
    again = pipeline.kjor(kjor_dir, steg=("artikkelrediger", "artikkeltittel"), llms={"skriv": writer, "sjekk": checker})
    assert again["artikkeltittel"] == {} and "artikkeltittel/rediger" not in again


def test_kjor_carries_on_when_a_headline_fails(kjor_dir):
    writer = FakeLLM([LLMError("usage limit"), LLMError("usage limit"), proposal()])
    result = pipeline.kjor(kjor_dir, steg=("artikkeltittel",), parallel=1,
                           llms={"skriv": writer, "sjekk": FakeLLM([verdict()])})
    assert result["artikkeltittel"]["antall"] == {"feil": 1, "støttet": 1} and len(result["artikkeltittel"]["feil"]) == 1
    assert result["artikkeltittel/rediger"]["redigert"] == 2


def test_kjor_survives_a_broken_headline_step(kjor_dir, monkeypatch):
    def boom(item, data_dir):
        raise ValueError("ødelagt fil")
    monkeypatch.setattr(artikkeltittel, "trenger", boom)
    result = pipeline.kjor(kjor_dir, steg=("artikkelrediger", "artikkeltittel"), llms={"skriv": None, "sjekk": None})
    assert result["artikkelrediger"]["publiserbar"] == 2
    assert "steget feilet: ødelagt fil" in result["artikkeltittel"]["feil"][0]


def test_headline_step_is_last_and_listed():
    assert pipeline.STEG[-1] == "artikkeltittel"
