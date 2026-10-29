import json

import pytest

from tingposten import faktasjekker, kildetekst
from tingposten.llm import FakeLLM, write_json

ITEM = {"id": "a", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "238/26", "tittel": "Eksempelhallen"}
CARD = {
    "nr": "238/26",
    "overskrift": "Klarsignal til Eksempelhallen",
    "hva_endres": "Bystyret ga klarsignal. Rammen er 69 millioner kroner.",
    "hvem_pavirkes": "",
    "hvorfor": "Byrådet skriver at hallen trengs.",
    "status": "Vedtatt enstemmig.",
    "nar_og_hvor": "",
    "kilder": [],
    "merknad": "HEMMELIG RESONNEMENT FRA SKRIBENTEN",
}


def s(felt, setning, stottet=True):
    return {"felt": felt, "setning": setning, "stottet": stottet,
            "kilde": {"dok": "protokoll", "side": 1} if stottet else None, "begrunnelse": "" if stottet else "feil tall"}


GOOD = {"setninger": [
    s("overskrift", "Klarsignal til Eksempelhallen"),
    s("hva_endres", "Bystyret ga klarsignal."),
    s("hva_endres", "Rammen er 69 millioner kroner.", stottet=False),
    s("hvorfor", "Byrådet skriver at hallen trengs."),
    s("status", "Vedtatt enstemmig."),
], "personvern": [], "blocked": ""}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    write_json(tmp_path / "kort" / "571424" / "2026-09-23" / "238-26.json", CARD)
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: {"saksframlegg": "s", "protokoll": "p"})
    return tmp_path


def saved(data_dir):
    return json.loads((data_dir / "sjekk" / "571424" / "2026-09-23" / "238-26.json").read_text())


def test_dekning_accepts_exact_cover():
    assert faktasjekker.dekning(CARD, GOOD["setninger"]) == []


def test_dekning_ignores_whitespace_around_sentences():
    padded = [{**x, "setning": " " + x["setning"] + " "} for x in GOOD["setninger"]]
    assert faktasjekker.dekning(CARD, padded) == []


def test_dekning_catches_rewording_gaps_and_order():
    reworded = [x if x["felt"] != "status" else s("status", "Vedtatt uten stemmer mot.") for x in GOOD["setninger"]]
    assert any("status" in e for e in faktasjekker.dekning(CARD, reworded))
    missing_tail = [x for x in GOOD["setninger"] if x["setning"] != "Rammen er 69 millioner kroner."]
    assert any("slutten" in e for e in faktasjekker.dekning(CARD, missing_tail))
    swapped = list(GOOD["setninger"])
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert faktasjekker.dekning(CARD, swapped)
    no_headline = [x for x in GOOD["setninger"] if x["felt"] != "overskrift"]
    assert "overskrift er ikke sjekket" in faktasjekker.dekning(CARD, no_headline)
    empty_field = GOOD["setninger"] + [s("nar_og_hvor", "Påfunn.")]
    assert "nar_og_hvor er tomt, men har setninger" in faktasjekker.dekning(CARD, empty_field)


def test_sjekk_writes_result_and_never_shows_the_merknad(data_dir):
    fake = FakeLLM([json.loads(json.dumps(GOOD))], name="fake:codex")
    assert faktasjekker.sjekk(ITEM, data_dir, fake) == "sjekket (1 uten støtte)"
    out = saved(data_dir)
    assert out["nr"] == "238/26" and out["setninger"] == GOOD["setninger"] and out["meta"]["modell"] == "fake:codex"
    assert "HEMMELIG" not in fake.prompts[0]
    assert "<<<KORT" in fake.prompts[0] and "<<<PROTOKOLL" in fake.prompts[0]


def test_sjekk_retries_bad_split_then_blocks(data_dir):
    bad = {**GOOD, "setninger": GOOD["setninger"][:2]}
    fake = FakeLLM([bad, json.loads(json.dumps(GOOD))])
    assert faktasjekker.sjekk(ITEM, data_dir, fake) == "sjekket (1 uten støtte)"
    assert "DID NOT SPLIT THE TEXT CORRECTLY" in fake.prompts[1]
    status = faktasjekker.sjekk(ITEM, data_dir, FakeLLM([bad, bad]), force=True)
    assert status.startswith("blocked: setningene dekker ikke kortet")
    assert saved(data_dir)["blocked"].startswith("setningene dekker ikke kortet")


def test_sjekk_model_blocked_is_kept(data_dir):
    assert faktasjekker.sjekk(ITEM, data_dir, FakeLLM([{**GOOD, "setninger": [], "blocked": "protokoll uleselig"}])) == \
        "blocked: protokoll uleselig"
    assert saved(data_dir)["blocked"] == "protokoll uleselig"


def test_sjekk_without_card_or_protokoll(tmp_path, monkeypatch):
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: {"saksframlegg": "", "protokoll": ""})
    assert faktasjekker.sjekk(ITEM, tmp_path, FakeLLM([])) == "mangler kort"
    write_json(tmp_path / "kort" / "571424" / "2026-09-23" / "238-26.json", CARD)
    assert faktasjekker.sjekk(ITEM, tmp_path, FakeLLM([])) == "mangler kilde: protokoll"


def test_schema_is_strict_for_codex():
    def walk(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
            for p in schema["properties"].values():
                walk(p)
        for opt in schema.get("anyOf", []):
            walk(opt)
        if "items" in schema:
            walk(schema["items"])
    walk(faktasjekker.SCHEMA)


# --- articles -------------------------------------------------------------------

REDIGERT = {"nr": "238/26", "overskrift": "Klarsignal til Eksempelhallen",
            "felt": {"hva_endres": "Bystyret ga klarsignal.", "hvem_pavirkes": "", "hvorfor": "",
                     "status": "Vedtatt enstemmig.", "nar_og_hvor": ""},
            "kilder": [], "slettet": [], "avvist": False, "grunn": "", "ord": 7, "setninger": 3}
ARTIKKEL = {"nr": "238/26", "meta": {"modell": "fake:opus", "merknad": "HEMMELIG SKRIBENTNOTAT"},
            "avsnitt": [{"tekst": "Saken ble utsatt i juni. Bystyret ga klarsignal.", "kilder": [{"dok": "protokoll", "side": 2}]},
                        {"tekst": "SV ville ha en mindre hall.", "kilder": [{"dok": "protokoll", "side": 3}]}]}


def a(avsnitt, setning, stottet=True, gjentar=False):
    return {"avsnitt": avsnitt, "setning": setning, "stottet": stottet, "gjentar_kortet": gjentar,
            "kilde": None, "begrunnelse": ""}


ART_GOOD = {"setninger": [a(1, "Saken ble utsatt i juni."), a(1, "Bystyret ga klarsignal.", gjentar=True),
                          a(2, "SV ville ha en mindre hall.", stottet=False)], "personvern": [], "blocked": ""}


@pytest.fixture
def art_dir(data_dir):
    write_json(data_dir / "redigert" / "571424" / "2026-09-23" / "238-26.json", REDIGERT)
    write_json(data_dir / "artikler" / "571424" / "2026-09-23" / "238-26.json", ARTIKKEL)
    return data_dir


def art_saved(data_dir):
    return json.loads((data_dir / "artikkelsjekk" / "571424" / "2026-09-23" / "238-26.json").read_text())


def test_artikkel_check_sees_edited_card_and_article_but_not_writer_meta(art_dir):
    fake = FakeLLM([json.loads(json.dumps(ART_GOOD))], name="fake:codex")
    assert faktasjekker.sjekk_artikkel(ITEM, art_dir, fake) == "sjekket (1 uten støtte, 1 gjentar kortet)"
    prompt = fake.prompts[0]
    assert "HEMMELIG" not in prompt and "<<<ARTIKKEL" in prompt and "<<<KORT" in prompt
    assert '"hva_endres": "Bystyret ga klarsignal."' in prompt  # the edited card, flattened
    assert '"avsnitt": 2' in prompt  # paragraphs are numbered from 1
    out = art_saved(art_dir)
    assert out["setninger"] == ART_GOOD["setninger"] and out["meta"]["prompt_versjon"] == faktasjekker.ARTIKKEL_PROMPT_VERSJON


def test_dekning_artikkel():
    paragraphs = [p["tekst"] for p in ARTIKKEL["avsnitt"]]
    assert faktasjekker.dekning_artikkel(paragraphs, ART_GOOD["setninger"]) == []
    assert "avsnitt 2 er ikke sjekket" in faktasjekker.dekning_artikkel(paragraphs, ART_GOOD["setninger"][:2])
    assert "avsnitt 3 finnes ikke" in faktasjekker.dekning_artikkel(paragraphs, ART_GOOD["setninger"] + [a(3, "x")])
    reworded = [a(1, "Saken ble utsatt."), *ART_GOOD["setninger"][1:]]
    assert any("avsnitt 1" in e for e in faktasjekker.dekning_artikkel(paragraphs, reworded))


def test_artikkel_check_retries_bad_split_then_blocks(art_dir):
    bad = {**ART_GOOD, "setninger": ART_GOOD["setninger"][:1]}
    fake = FakeLLM([bad, json.loads(json.dumps(ART_GOOD))])
    assert faktasjekker.sjekk_artikkel(ITEM, art_dir, fake).startswith("sjekket")
    assert "DID NOT SPLIT THE TEXT CORRECTLY" in fake.prompts[1]
    assert faktasjekker.sjekk_artikkel(ITEM, art_dir, FakeLLM([bad, bad]), force=True).startswith(
        "blocked: setningene dekker ikke artikkelen")


def test_empty_article_needs_no_model(art_dir):
    write_json(art_dir / "artikler" / "571424" / "2026-09-23" / "238-26.json", {"nr": "238/26", "avsnitt": []})
    assert faktasjekker.sjekk_artikkel(ITEM, art_dir, FakeLLM([])) == "tom artikkel"
    assert art_saved(art_dir)["setninger"] == [] and art_saved(art_dir)["meta"]["tom"] is True


def test_artikkel_check_needs_article_and_edited_card(data_dir):
    assert faktasjekker.sjekk_artikkel(ITEM, data_dir, FakeLLM([])) == "mangler artikkel"
    write_json(data_dir / "artikler" / "571424" / "2026-09-23" / "238-26.json", ARTIKKEL)
    assert faktasjekker.sjekk_artikkel(ITEM, data_dir, FakeLLM([])) == "mangler redigert kort"


def test_artikkel_schema_is_strict_for_codex():
    def walk(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
            for p in schema["properties"].values():
                walk(p)
        for opt in schema.get("anyOf", []):
            walk(opt)
        if "items" in schema:
            walk(schema["items"])
    walk(faktasjekker.ARTIKKEL_SCHEMA)
