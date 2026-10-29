import json

import pymupdf
import pytest

from tingposten import kildetekst, skribent
from tingposten.llm import FakeLLM

ITEM = {
    "id": "a", "utvalgId": "571424", "moteDato": "2026-09-23", "seq": 5,
    "kategori": "Saker til behandling", "tittel": "Gjennomføringsvedtak for Eksempelhallen",
    "referansenr": "238/26", "innstillingFilId": "111", "vedtakFilId": "222",
}
KILDER = {"saksframlegg": "--- side 1 ---\nHallen koster 69 mill.", "protokoll": "--- side 1 ---\nVedtatt enstemmig."}
CARD = {
    "overskrift": "Klarsignal til å bygge Eksempelhallen",
    "hva_endres": "Bystyret ga endelig klarsignal til å bygge hallen. Rammen er 69 millioner kroner.",
    "hvem_pavirkes": "",
    "hvorfor": "Byrådet skriver at hallen trengs.",
    "status": "Vedtatt enstemmig.",
    "nar_og_hvor": "",
    "kilder": [{"felt": "hva_endres", "dok": "saksframlegg", "side": 1},
               {"felt": "hvorfor", "dok": "saksframlegg", "side": 1},
               {"felt": "status", "dok": "protokoll", "side": 1}],
    "merknad": "",
    "blocked": "",
}


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: dict(KILDER))


def card_path(tmp_path):
    return tmp_path / "kort" / "571424" / "2026-09-23" / "238-26.json"


def test_dokumenttekst_marks_pages(tmp_path):
    doc = pymupdf.open()
    for text in ("Første side", "Andre side"):
        doc.new_page().insert_text((72, 72), text)
    doc.save(tmp_path / "x.pdf")
    text = kildetekst.dokumenttekst(tmp_path / "x.pdf")
    assert text.index("--- side 1 ---") < text.index("Første side") < text.index("--- side 2 ---") < text.index("Andre side")


def test_kilder_for_missing_file_is_empty(tmp_path):
    assert kildetekst.kilder_for(ITEM, tmp_path) == {"saksframlegg": "", "protokoll": ""}


def test_skriv_writes_card_with_nr_and_meta(tmp_path, sources):
    fake = FakeLLM([dict(CARD)], name="fake:opus")
    assert skribent.skriv(ITEM, tmp_path, fake) == "skrevet"
    saved = json.loads(card_path(tmp_path).read_text())
    assert saved["nr"] == "238/26" and "blocked" not in saved
    assert saved["meta"]["modell"] == "fake:opus" and saved["meta"]["prompt_versjon"] == skribent.PROMPT_VERSJON
    prompt = fake.prompts[0]
    assert "<<<PROTOKOLL" in prompt and "Vedtatt enstemmig." in prompt
    assert "EXAMPLE CARDS" in prompt and len(list(skribent.EKSEMPLER.glob("*.json"))) == 5
    assert "«bør» is not «skal»" in prompt or '"bør" is not "skal"' in prompt


def test_skriv_skips_existing_unless_forced(tmp_path, sources):
    skribent.skriv(ITEM, tmp_path, FakeLLM([dict(CARD)]))
    assert skribent.skriv(ITEM, tmp_path, FakeLLM([])) == "finnes"
    assert skribent.skriv(ITEM, tmp_path, FakeLLM([dict(CARD)]), force=True) == "skrevet"


def test_skriv_asks_once_for_a_rewrite_when_over_the_limits(tmp_path, sources):
    long = {**CARD, "overskrift": "x" * 80, "status": "En. To. Tre."}
    fake = FakeLLM([long, dict(CARD)])
    assert skribent.skriv(ITEM, tmp_path, fake) == "skrevet"
    assert "overskrift har 80 tegn" in fake.prompts[1] and "status har 3 setninger" in fake.prompts[1]
    # still over after the rewrite: written, but flagged, and the redaktør will reject it
    assert skribent.skriv(ITEM, tmp_path, FakeLLM([long, long]), force=True) == "skrevet (over grensen)"


def test_skriv_blocked_or_without_protokoll_writes_nothing(tmp_path, monkeypatch, sources):
    assert skribent.skriv(ITEM, tmp_path, FakeLLM([{**CARD, "blocked": "tomt dokument"}])) == "blocked: tomt dokument"
    assert not card_path(tmp_path).exists()
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: {"saksframlegg": "x", "protokoll": ""})
    assert skribent.skriv(ITEM, tmp_path, FakeLLM([])) == "mangler kilde: protokoll"


def test_grenser():
    assert skribent.grenser(CARD) == []
    problems = skribent.grenser({**CARD, "hvorfor": "Byrådet mener du bør vite det.", "kilder": []})
    assert "hvorfor henvender seg til leseren med «du»" in problems
    assert "hva_endres mangler kilde" in problems


def test_setninger_ignores_abbreviations_and_dates():
    assert skribent.setninger("Rammen er 69 mill. kroner. Arbeidet starter 1. januar 2027. Pkt. 3 falt.") == [
        "Rammen er 69 mill. kroner.", "Arbeidet starter 1. januar 2027.", "Pkt. 3 falt."]
    assert skribent.setninger("") == []


def test_examples_have_no_private_reasons():
    for path in skribent.EKSEMPLER.glob("*.json"):
        card = json.loads(path.read_text())
        assert set(card) == {"nr", "overskrift", "hva_endres", "hvem_pavirkes", "hvorfor", "status", "nar_og_hvor",
                             "kilder", "merknad"}
        text = json.dumps(card, ensure_ascii=False).lower()
        for word in ("helse", "sykdom", "bosted", "flytt", "ny jobb"):
            assert word not in text, (path.name, word)


ARTICLE = {"avsnitt": [{"tekst": "Saken har vært utsatt to ganger.", "kilder": [{"dok": "saksframlegg", "side": 1}]}],
           "blocked": ""}


def test_skriv_artikkel(tmp_path, sources):
    fake = FakeLLM([dict(ARTICLE)])
    assert skribent.skriv_artikkel(ITEM, CARD, tmp_path, fake) == "skrevet"
    saved = json.loads((tmp_path / "artikler" / "571424" / "2026-09-23" / "238-26.json").read_text())
    assert saved["avsnitt"] == ARTICLE["avsnitt"] and saved["nr"] == "238/26"
    assert "do not repeat any of it" in fake.prompts[0] and CARD["hva_endres"] in fake.prompts[0]


def test_artikkel_grenser_and_rewrite(tmp_path, sources):
    long = {"avsnitt": [{"tekst": "ord " * 60, "kilder": [{"dok": "protokoll", "side": 1}]}] * 6, "blocked": ""}
    assert skribent.artikkel_grenser(long) == ["artikkelen har 360 ord (maks 250)", "artikkelen har 6 avsnitt (maks 5)"]
    fake = FakeLLM([long, dict(ARTICLE)])
    assert skribent.skriv_artikkel(ITEM, CARD, tmp_path, fake) == "skrevet"
    assert "artikkelen har 360 ord" in fake.prompts[1]


def test_rewrite_with_feedback_counts_and_tells_the_writer(tmp_path, sources):
    skribent.skriv(ITEM, tmp_path, FakeLLM([dict(CARD)]))
    assert "omskrevet" not in json.loads(card_path(tmp_path).read_text())["meta"]
    fake = FakeLLM([dict(CARD)])
    status = skribent.skriv(ITEM, tmp_path, fake, force=True, feedback="Avvist fordi: status har 3 setninger (maks 2).")
    assert status == "omskrevet"
    assert "THE EDITOR REJECTED THE PREVIOUS DRAFT" in fake.prompts[0] and "status har 3 setninger" in fake.prompts[0]
    assert json.loads(card_path(tmp_path).read_text())["meta"]["omskrevet"] == 1
    skribent.skriv(ITEM, tmp_path, FakeLLM([dict(CARD)]), force=True, feedback="igjen")
    assert json.loads(card_path(tmp_path).read_text())["meta"]["omskrevet"] == 2


def test_run_passes_feedback_by_key(tmp_path, sources):
    other = {**ITEM, "id": "b", "referansenr": "239/26"}
    fake = FakeLLM([dict(CARD), dict(CARD)])
    result = skribent.run([ITEM, other], tmp_path, fake, parallel=1, feedback={"571424/2026-09-23/238-26": "Rett dette."})
    assert result["antall"] == {"omskrevet": 1, "skrevet": 1}
    assert sum("Rett dette." in p for p in fake.prompts) == 1


def test_run_artikkel_writes_against_the_edited_card(tmp_path, sources):
    from tingposten.llm import write_json
    write_json(tmp_path / "redigert" / "571424" / "2026-09-23" / "238-26.json", {
        "nr": "238/26", "overskrift": "Klarsignal", "avvist": False,
        "felt": {"hva_endres": "Kun det som overlevde redigeringen.", "hvem_pavirkes": "", "hvorfor": "",
                 "status": "Vedtatt.", "nar_og_hvor": ""}})
    fake = FakeLLM([dict(ARTICLE)])
    other = {**ITEM, "id": "b", "referansenr": "239/26"}  # no edited card
    result = skribent.run_artikkel([ITEM, other], tmp_path, fake, parallel=1)
    assert result["antall"] == {"skrevet": 1, "mangler redigert kort": 1}
    assert "Kun det som overlevde redigeringen." in fake.prompts[0]
