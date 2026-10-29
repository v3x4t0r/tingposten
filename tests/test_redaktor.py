import itertools
import json
import random

import pytest

from tingposten import redaktor

FIELDS = redaktor.FIELDS


def make(sentences: dict[str, list[str]], headline="Bystyret vedtok ny plan for Eksempelparken",
         stottet: dict | None = None, **check_extra):
    """A card and a matching check; `stottet` maps (felt, index) → False."""
    stottet = stottet or {}
    card = {"nr": "1/26", "overskrift": headline, "merknad": "", "kilder": []}
    check = {"nr": "1/26", "personvern": [], "blocked": "", "setninger": [
        {"felt": "overskrift", "setning": headline, "stottet": stottet.get(("overskrift", 0), True),
         "kilde": None, "begrunnelse": ""}]}
    for f in FIELDS:
        sents = sentences.get(f, [])
        card[f] = " ".join(sents)
        for i, s in enumerate(sents):
            ok = stottet.get((f, i), True)
            check["setninger"].append({"felt": f, "setning": s, "stottet": ok, "kilde": None,
                                       "begrunnelse": "" if ok else "Kilden sier noe annet."})
        if sents:
            card["kilder"].append({"felt": f, "dok": "protokoll", "side": 1})
    check.update(check_extra)
    return card, check


BASE = {
    "hva_endres": ["Bystyret vedtok planen.", "Parken får nye benker."],
    "hvem_pavirkes": ["Beboere i Fana."],
    "hvorfor": ["Byrådet mener parken trenger vedlikehold."],
    "status": ["Vedtatt enstemmig."],
    "nar_og_hvor": ["Arbeidet starter i 2027."],
}


def test_clean_card_passes_unchanged():
    card, check = make(BASE)
    r = redaktor.edit(card, check)
    assert not r["avvist"] and r["grunn"] == "" and r["slettet"] == []
    assert r["felt"]["hva_endres"] == "Bystyret vedtok planen. Parken får nye benker."
    assert set(r) == {"nr", "overskrift", "felt", "kilder", "slettet", "avvist", "grunn", "ord", "setninger"}
    assert r["setninger"] == 7
    assert r["ord"] == sum(len(s.split()) for v in BASE.values() for s in v) + len(card["overskrift"].split())


def test_unsupported_sentence_is_deleted_with_reason():
    card, check = make(BASE, stottet={("hva_endres", 1): False})
    r = redaktor.edit(card, check)
    assert r["felt"]["hva_endres"] == "Bystyret vedtok planen."
    assert r["slettet"] == [{"felt": "hva_endres", "setning": "Parken får nye benker.",
                             "grunn": "ikke støttet av kilden: Kilden sier noe annet."}]
    assert not r["avvist"]


def test_missing_or_blocked_check_rejects():
    card, check = make(BASE)
    r = redaktor.edit(card, None)
    assert r["avvist"] and r["grunn"] == "ikke faktasjekket"
    assert r["felt"]["hva_endres"] == card["hva_endres"]  # untouched, still only input text
    r = redaktor.edit(card, {**check, "blocked": "PDF mangler"})
    assert r["avvist"] and "PDF mangler" in r["grunn"]


def test_check_not_covering_field_rejects():
    card, check = make(BASE)
    card["status"] = "Vedtatt enstemmig. En setning ingen har sjekket."
    r = redaktor.edit(card, check)
    assert r["avvist"] and "faktasjekken dekker ikke status" in r["grunn"]
    assert r["felt"]["status"] == card["status"]


@pytest.mark.parametrize("sentence, felt", [
    ("I dette vedtaket får parken nye benker.", "hva_endres"),
    ("Det er verdt å merke seg at parken er gammel.", "hva_endres"),
    ("Oppsummert blir parken finere.", "hva_endres"),
    ("Dette er et viktig skritt for byen.", "hva_endres"),
    ("Vedtaket er historisk.", "status"),
    ("Hva betyr dette for nabolaget?", "hva_endres"),
    ("Dette kan gi deg kortere vei til jobb.", "hvem_pavirkes"),
    ("- Nye benker i parken.", "hva_endres"),
    ("1. Prosjektet gjennomføres.", "hva_endres"),
])
def test_forbudsliste_deletes(sentence, felt):
    sents = {**BASE, felt: BASE[felt][:1] + [sentence]}
    card, check = make(sents)
    r = redaktor.edit(card, check)
    assert [s["setning"] for s in r["slettet"]] == [sentence]
    assert r["slettet"][0]["grunn"].startswith("forbudsliste: ")
    assert sentence not in r["felt"][felt]


@pytest.mark.parametrize("sentence", [
    "Høringsfristen var 8. oktober 2026.",          # a date is not a list item
    "Bystyret vedtok merknad 9 i budsjettet.",
    "Prosjektet koster 2,3 mrd. kroner.",
])
def test_forbudsliste_leaves_ordinary_sentences(sentence):
    sents = {**BASE, "nar_og_hvor": [sentence]}
    card, check = make(sents)
    assert redaktor.edit(card, check)["slettet"] == []


def test_importance_word_allowed_in_attributed_reasoning_only():
    attributed = "Byrådet viser til at tiltaket er viktig for tryggheten."
    card, check = make({**BASE, "hvorfor": [attributed]})
    assert redaktor.edit(card, check)["slettet"] == []
    card, check = make({**BASE, "hvorfor": ["Tiltaket er viktig for tryggheten."]})
    assert len(redaktor.edit(card, check)["slettet"]) == 1
    # the exemption is for "hvorfor" only
    card, check = make({**BASE, "hva_endres": ["Byrådet mener planen er viktig."]})
    assert len(redaktor.edit(card, check)["slettet"]) == 1
    # and never for the other forbudsliste categories
    card, check = make({**BASE, "hvorfor": ["Byrådet mener det er viktig å merke seg at du får benk."]})
    assert len(redaktor.edit(card, check)["slettet"]) == 1


def test_hard_limits_reject_instead_of_trimming():
    long = {**BASE, "hva_endres": ["En.", "To.", "Tre.", "Fire."]}
    card, check = make(long)
    r = redaktor.edit(card, check)
    assert r["avvist"] and "hva_endres har 4 setninger (maks 3)" in r["grunn"]
    assert r["felt"]["hva_endres"] == "En. To. Tre. Fire."  # not trimmed
    card, check = make({**BASE, "status": ["En.", "To.", "Tre."]})
    assert "status har 3 setninger (maks 2)" in redaktor.edit(card, check)["grunn"]
    # counted on the original draft: deleting down to 3 does not save it
    card, check = make(long, stottet={("hva_endres", 3): False})
    assert redaktor.edit(card, check)["avvist"]


def test_long_or_unsupported_headline_rejects():
    card, check = make(BASE, headline="B" * 71)
    assert "overskrift over 70 tegn (71)" in redaktor.edit(card, check)["grunn"]
    card, check = make(BASE, stottet={("overskrift", 0): False})
    r = redaktor.edit(card, check)
    assert r["avvist"] and r["grunn"].startswith("overskrift ikke støttet av kilden")
    card, check = make(BASE)
    check["setninger"] = check["setninger"][1:]  # headline never checked
    assert "overskrift ikke faktasjekket" in redaktor.edit(card, check)["grunn"]


def test_empty_hva_endres_after_deletions_rejects():
    card, check = make(BASE, stottet={("hva_endres", 0): False, ("hva_endres", 1): False})
    r = redaktor.edit(card, check)
    assert r["avvist"] and "ingenting igjen i Hva endres" in r["grunn"]


def test_personvern_finding_rejects():
    card, check = make(BASE, personvern=["Navn på privatperson i hvem_pavirkes."])
    r = redaktor.edit(card, check)
    assert r["avvist"] and r["grunn"].startswith("personvern: Navn på privatperson")


def test_kilder_only_for_fields_with_text_left():
    card, check = make(BASE, stottet={("nar_og_hvor", 0): False})
    r = redaktor.edit(card, check)
    assert "nar_og_hvor" not in {k["felt"] for k in r["kilder"]}
    assert {"hva_endres", "status"} <= {k["felt"] for k in r["kilder"]}


def test_hallucinated_check_sentence_is_never_injected():
    card, check = make(BASE)
    check["setninger"].append({"felt": "status", "setning": "Kommunen bevilget 9 milliarder.",
                               "stottet": True, "kilde": None, "begrunnelse": ""})
    r = redaktor.edit(card, check)
    assert "milliarder" not in json.dumps(r["felt"], ensure_ascii=False)
    assert r["avvist"]


def _joins(sentences: list[str]) -> set[str]:
    return {" ".join(c) for n in range(len(sentences) + 1) for c in itertools.combinations(sentences, n)}


def test_output_is_always_a_deletion_of_the_input():
    """Property test: whatever the check says (wrong, partial, hallucinated,
    adversarial), every output field is the input field or the input's
    sentences with some removed — never anything else."""
    rng = random.Random(1234)
    pool = ["Bystyret vedtok planen.", "Parken får nye benker.", "Dette er viktig.",
            "Byrådet mener det haster.", "Vedtatt enstemmig.", "Hva nå?", "Du får benk.",
            "Arbeidet starter i 2027.", "Kostnaden er 3 mill. kroner."]
    for _ in range(400):
        sents = {f: rng.sample(pool, rng.randint(0, 4)) for f in FIELDS}
        stottet = {(f, i): rng.random() > 0.3 for f in FIELDS for i in range(len(sents[f]))}
        stottet[("overskrift", 0)] = rng.random() > 0.1
        card, check = make(sents, stottet=stottet)
        mutation = rng.randint(0, 5)
        if mutation == 1:  # checker invents a sentence
            check["setninger"].insert(rng.randint(0, len(check["setninger"])),
                                      {"felt": rng.choice(FIELDS), "setning": "Oppdiktet påstand.", "stottet": True})
        elif mutation == 2 and len(check["setninger"]) > 1:  # checker skips one
            check["setninger"].pop(rng.randrange(1, len(check["setninger"])))
        elif mutation == 3:  # checker reorders
            rng.shuffle(check["setninger"])
        elif mutation == 4:
            check = None
        r = redaktor.edit(card, check)
        assert r["overskrift"] == card["overskrift"]
        for f in FIELDS:
            assert r["felt"][f] in _joins(sents[f]) | {card[f]}, (f, r["felt"][f])
        assert "Oppdiktet" not in json.dumps(r["felt"], ensure_ascii=False)


def test_run_writes_files_and_metrics(tmp_path):
    item = {"id": "x", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26"}
    other = {**item, "id": "y", "referansenr": "2/26"}  # no draft → skipped
    card, check = make(BASE, stottet={("hvorfor", 0): False})
    base = tmp_path / "kort" / "571424" / "2026-09-23"
    base.mkdir(parents=True)
    (base / "1-26.json").write_text(json.dumps(card))
    (tmp_path / "sjekk" / "571424" / "2026-09-23").mkdir(parents=True)
    (tmp_path / "sjekk" / "571424" / "2026-09-23" / "1-26.json").write_text(json.dumps(check))
    counts = redaktor.run([item, other], tmp_path)
    assert counts == {"redigert": 1, "publiserbar": 1, "avvist": 0, "slettet": 1}
    out = json.loads((tmp_path / "redigert" / "571424" / "2026-09-23" / "1-26.json").read_text())
    assert out["felt"]["hvorfor"] == ""
    line = json.loads((tmp_path / "metrikk.jsonl").read_text().splitlines()[0])
    assert line["key"] == "571424/2026-09-23/1-26" and line["slettet"] == 1 and line["avvist"] is False
    assert not (tmp_path / "redigert" / "571424" / "2026-09-23" / "2-26.json").exists()
