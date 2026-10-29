import json

import pytest

from tingposten import klassifiserer
from tingposten.llm import FakeLLM, LLMError, write_json

ITEM = {"id": "a", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "248/26",
        "tittel": "Høringsuttalelse om kjernekraft", "kategori": "Saker til behandling"}
CARD = {"nr": "248/26", "overskrift": "Bergen ber staten åpne for kjernekraft", "hva_endres": "Bystyret vedtok en uttalelse.",
        "hvem_pavirkes": "", "hvorfor": "", "status": "Vedtatt med 56 stemmer.", "nar_og_hvor": "", "kilder": [],
        "merknad": "skribentens notat"}
ANSWER = {"angar": {"verdi": 5, "begrunnelse": "Kjernekraft er et tema mange har meninger om."},
          "konkret": {"verdi": 1, "begrunnelse": "Uttalelsen endrer ingenting i hverdagen nå."},
          "tema": "miljo", "tema_begrunnelse": "Kjernekraft er energi.", "blocked": ""}


@pytest.fixture
def data_dir(tmp_path):
    write_json(tmp_path / "kort" / "571424" / "2026-09-23" / "248-26.json", CARD)
    return tmp_path


def test_klassifiser_writes_both_signals(data_dir):
    fake = FakeLLM([dict(ANSWER)], name="fake:sonnet")
    assert klassifiserer.klassifiser(ITEM, data_dir, fake) == "klassifisert"
    out = json.loads((data_dir / "signaler" / "571424" / "2026-09-23" / "248-26.json").read_text())
    assert out["angar"] == ANSWER["angar"] and out["konkret"] == ANSWER["konkret"]
    assert out["meta"]["modell"] == "fake:sonnet" and "blocked" not in out
    prompt = fake.prompts[0]
    assert "Angår mange" in prompt and "Konkret endring i hverdagen" in prompt
    assert CARD["overskrift"] in prompt and "skribentens notat" not in prompt


def test_klassifiser_skips_existing_and_missing_card(data_dir, tmp_path_factory):
    klassifiserer.klassifiser(ITEM, data_dir, FakeLLM([dict(ANSWER)]))
    assert klassifiserer.klassifiser(ITEM, data_dir, FakeLLM([])) == "finnes"
    assert klassifiserer.klassifiser(ITEM, tmp_path_factory.mktemp("empty"), FakeLLM([])) == "mangler kort"


def test_out_of_range_rating_is_rejected(data_dir):
    bad = {**ANSWER, "angar": {"verdi": 7, "begrunnelse": "x"}}
    with pytest.raises(LLMError):
        klassifiserer.klassifiser(ITEM, data_dir, FakeLLM([bad, bad]))


def test_run_reports_per_status(data_dir):
    other = {**ITEM, "id": "b", "referansenr": "999/26"}
    result = klassifiserer.run([ITEM, other], data_dir, FakeLLM([dict(ANSWER)]), parallel=1)
    assert result["antall"] == {"klassifisert": 1, "mangler kort": 1}


TEMA_ANSWER = {"tema": "miljo", "tema_begrunnelse": "Saken handler om energi.", "blocked": ""}


def test_full_classification_includes_tema(data_dir):
    fake = FakeLLM([{**ANSWER, "tema": "miljo", "tema_begrunnelse": "Energi."}])
    klassifiserer.klassifiser(ITEM, data_dir, fake)
    out = json.loads((data_dir / "signaler" / "571424" / "2026-09-23" / "248-26.json").read_text())
    assert out["tema"] == "miljo" and out["tema_begrunnelse"] == "Energi."
    assert "Klima og miljø" in fake.prompts[0] and "byutvikling" in fake.prompts[0]


def test_unknown_tema_is_rejected(data_dir):
    bad = {**ANSWER, "tema": "sport", "tema_begrunnelse": ""}
    with pytest.raises(LLMError):
        klassifiserer.klassifiser(ITEM, data_dir, FakeLLM([bad, bad]))


def test_tema_backfill_keeps_signals_and_only_runs_where_missing(data_dir):
    path = data_dir / "signaler" / "571424" / "2026-09-23" / "248-26.json"
    old = {"angar": ANSWER["angar"], "konkret": ANSWER["konkret"], "meta": {"modell": "claude:sonnet"}}
    write_json(path, old)
    assert klassifiserer.mangler_tema(ITEM, data_dir)
    fake = FakeLLM([dict(TEMA_ANSWER)])
    result = klassifiserer.run_tema([ITEM], data_dir, fake, parallel=1)
    assert result["antall"] == {"tema lagt til": 1}
    out = json.loads(path.read_text())
    assert out["angar"] == old["angar"] and out["konkret"] == old["konkret"]
    assert out["tema"] == "miljo" and out["meta"]["modell"] == "claude:sonnet" and "tema" in out["meta"]
    prompt = fake.prompts[0]
    assert "Angår mange" not in prompt and "DATA" in prompt and CARD["overskrift"] in prompt
    assert not klassifiserer.mangler_tema(ITEM, data_dir)
    assert klassifiserer.run_tema([ITEM], data_dir, FakeLLM([]), parallel=1)["antall"] == {}


def test_mangler_tema_needs_a_card(tmp_path):
    assert not klassifiserer.mangler_tema(ITEM, tmp_path)
