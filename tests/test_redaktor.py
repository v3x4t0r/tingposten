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
    assert set(r) == {"nr", "overskrift", "felt", "kilder", "slettet", "avvist", "grunn", "koder", "ord", "setninger"}
    assert r["koder"] == []
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


def test_word_caps_reject_instead_of_trimming():
    long = " ".join(["ord"] * 31) + "."
    card, check = make({**BASE, "hvorfor": [long]})
    r = redaktor.edit(card, check)
    assert r["avvist"] and r["koder"] == ["for_mange_ord"] and "hvorfor har en setning på 31 ord (maks 30)" in r["grunn"]
    assert r["felt"]["hvorfor"] == long  # nothing trimmed
    many = ["Kort setning her ja."] * 2 + [" ".join(["ord"] * 28) + "."]  # 3 sentences, 36 words > 35 for nar_og_hvor
    r = redaktor.edit(*make({**BASE, "nar_og_hvor": many}))
    assert "nar_og_hvor har 36 ord (maks 35)" in r["grunn"] and "nar_og_hvor har 3 setninger (maks 2)" in r["grunn"]
    assert redaktor.omskrivbar(r)
    ingress = ["Hallen bygges nå.", "Den koster 69 millioner kroner."]
    r = redaktor.edit(*make({**BASE, "ingress": ingress}))
    assert not r["avvist"] and r["felt"]["ingress"] == " ".join(ingress)


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
    card, check = make(BASE, headline="B" * 66)
    assert "overskrift over 65 tegn (66)" in redaktor.edit(card, check)["grunn"]
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
    assert counts == {"redigert": 1, "publiserbar": 1, "avvist": 0, "slettet": 1, "endret": 1, "venter": 0}
    out = json.loads((tmp_path / "redigert" / "571424" / "2026-09-23" / "1-26.json").read_text())
    assert out["felt"]["hvorfor"] == ""
    line = json.loads((tmp_path / "metrikk.jsonl").read_text().splitlines()[0])
    assert line["key"] == "571424/2026-09-23/1-26" and line["slettet"] == 1 and line["avvist"] is False
    assert line["type"] == "kort"
    assert not (tmp_path / "redigert" / "571424" / "2026-09-23" / "2-26.json").exists()


def test_new_draft_without_fresh_check_keeps_the_published_card(tmp_path):
    """A writer-only run (the checker out of quota, or a prompt-version bump
    that rewrites every card) must not knock checked cards off the site."""
    import os
    item = {"id": "x", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26"}
    card, check = make(BASE)
    paths = {step: tmp_path / step / "571424" / "2026-09-23" / "1-26.json" for step in ("kort", "sjekk", "redigert")}
    for p_ in paths.values():
        p_.parent.mkdir(parents=True, exist_ok=True)
    paths["kort"].write_text(json.dumps(card))
    paths["sjekk"].write_text(json.dumps(check))
    assert redaktor.run([item], tmp_path)["publiserbar"] == 1
    published = json.loads(paths["redigert"].read_text())
    # the writer rewrites the card; the check is now older than the draft
    new_card, _ = make({**BASE, "hva_endres": ["Helt ny tekst om planen.", "Og mer nytt."]})
    paths["kort"].write_text(json.dumps(new_card))
    os.utime(paths["sjekk"], (1_000, 1_000))
    counts = redaktor.run([item], tmp_path)
    assert counts["venter"] == 1 and counts["redigert"] == 0
    assert json.loads(paths["redigert"].read_text()) == published  # untouched
    # once the new check lands (fresh), the new text goes through as usual
    _, new_check = make({**BASE, "hva_endres": ["Helt ny tekst om planen.", "Og mer nytt."]})
    paths["sjekk"].write_text(json.dumps(new_check))
    counts = redaktor.run([item], tmp_path)
    assert counts["venter"] == 0 and counts["publiserbar"] == 1
    assert json.loads(paths["redigert"].read_text())["felt"]["hva_endres"] == "Helt ny tekst om planen. Og mer nytt."
    # without anything published, an unchecked draft is rejected as before (never published unchecked)
    paths["redigert"].unlink()
    os.utime(paths["sjekk"], (1_000, 1_000))
    counts = redaktor.run([item], tmp_path)
    assert counts["venter"] == 0 and counts["avvist"] == 1 and json.loads(paths["redigert"].read_text())["avvist"]
    # and a rejected published edit is not worth keeping: the draft goes through
    counts = redaktor.run([item], tmp_path)
    assert counts["venter"] == 0 and counts["avvist"] == 1


def test_new_article_draft_without_fresh_check_keeps_the_published_one(tmp_path):
    import os
    item = {"id": "x", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26"}
    art, check = article(ART)
    paths = {step: tmp_path / step / "571424" / "2026-09-23" / "1-26.json" for step in ("artikler", "artikkelsjekk", "artikler-redigert")}
    for p_ in paths.values():
        p_.parent.mkdir(parents=True, exist_ok=True)
    paths["artikler"].write_text(json.dumps(art))
    paths["artikkelsjekk"].write_text(json.dumps(check))
    assert redaktor.run_artikler([item], tmp_path)["publiserbar"] == 1
    published = json.loads(paths["artikler-redigert"].read_text())
    new_art, _ = article([[WORDS, "Ny setning."]])
    paths["artikler"].write_text(json.dumps(new_art))
    os.utime(paths["artikkelsjekk"], (1_000, 1_000))
    counts = redaktor.run_artikler([item], tmp_path)
    assert counts["venter"] == 1 and counts["redigert"] == 0
    assert json.loads(paths["artikler-redigert"].read_text()) == published


def test_run_rewrites_only_on_change(tmp_path):
    """Unchanged edits keep the file's mtime (articles compare against it) and log nothing."""
    import os
    item = {"id": "x", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26"}
    card, check = make(BASE)
    for step, obj in (("kort", card), ("sjekk", check)):
        path = tmp_path / step / "571424" / "2026-09-23" / "1-26.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(obj))
    redaktor.run([item], tmp_path)
    out = tmp_path / "redigert" / "571424" / "2026-09-23" / "1-26.json"
    os.utime(out, (1_000_000, 1_000_000))
    assert redaktor.run([item], tmp_path)["endret"] == 0
    assert out.stat().st_mtime == 1_000_000
    assert len((tmp_path / "metrikk.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("change, codes, rewritable", [
    ({"headline": "B" * 71}, ["overskrift_lang"], True),
    ({"stottet": {("overskrift", 0): False}}, ["overskrift_ustottet"], True),
    ({"sentences": {**BASE, "status": ["En.", "To.", "Tre."]}}, ["for_mange_setninger"], True),
    ({"stottet": {("hva_endres", 0): False, ("hva_endres", 1): False}}, ["tom_hva_endres"], True),
    ({"personvern": ["Navn på privatperson."]}, ["personvern"], False),
    ({"headline": "B" * 71, "personvern": ["Navn."]}, ["personvern", "overskrift_lang"], False),
])
def test_rejection_codes_and_what_the_writer_may_fix(change, codes, rewritable):
    card, check = make(change.get("sentences", BASE), headline=change.get("headline", "Bystyret vedtok ny plan"),
                       stottet=change.get("stottet"), **({"personvern": change["personvern"]} if "personvern" in change else {}))
    r = redaktor.edit(card, check)
    assert r["avvist"] and r["koder"] == codes
    assert redaktor.omskrivbar(r) is rewritable


def test_checker_problems_are_not_for_the_writer():
    card, check = make(BASE)
    assert not redaktor.omskrivbar(redaktor.edit(card, None))                     # unchecked
    card["status"] = "Vedtatt enstemmig. En setning ingen har sjekket."
    r = redaktor.edit(card, check)
    assert r["koder"] == ["dekning"] and not redaktor.omskrivbar(r)
    assert not redaktor.omskrivbar(redaktor.edit(*make(BASE)))                    # not rejected at all


# --- articles -------------------------------------------------------------------

WORDS = "Bystyret behandlet saken etter en lang debatt der flere partier la fram egne forslag om saken."  # 16 words


def article(paragraphs: list[list[str]], stottet: dict | None = None, gjentar: dict | None = None, **extra):
    """An article and a matching check; maps (paragraph 1-based, index) → flag."""
    stottet, gjentar = stottet or {}, gjentar or {}
    roller = extra.pop("roller_tags", None) or [""] * len(paragraphs)
    art = {"nr": "1/26", "avsnitt": [{"rolle": roller[n - 1], "tekst": " ".join(p), "kilder": [{"dok": "protokoll", "side": n}]}
                                    for n, p in enumerate(paragraphs, start=1)]}
    check = {"nr": "1/26", "personvern": [], "blocked": "", "roller": [], "setninger": [
        {"avsnitt": n, "setning": s, "stottet": stottet.get((n, i), True), "gjentar_kortet": gjentar.get((n, i), False),
         "kilde": None, "begrunnelse": "" if stottet.get((n, i), True) else "Feil tall."}
        for n, p in enumerate(paragraphs, start=1) for i, s in enumerate(p)]}
    check.update(extra)
    return art, check


ART = [[WORDS, "Høyre ville utsette saken."], ["Arbeidet starter i mars.", WORDS, WORDS]]


def test_clean_article_passes():
    art, check = article(ART)
    r = redaktor.rediger_artikkel(art, check)
    assert not r["avvist"] and r["slettet"] == [] and [a["tekst"] for a in r["avsnitt"]] == [a["tekst"] for a in art["avsnitt"]]
    assert r["avsnitt"][1]["kilder"] == [{"dok": "protokoll", "side": 2}]
    assert r["ord"] == r["ord_for"] and r["nye_fakta"] == 5 and r["setninger_for"] == 5


def test_article_deletes_repetition_unsupported_and_forbidden():
    art, check = article([[WORDS, "Dette er et viktig vedtak.", "Høyre ville utsette saken."],
                          [WORDS, "Kortet sier det samme som her.", WORDS]],
                         stottet={(1, 2): False}, gjentar={(2, 1): True})
    r = redaktor.rediger_artikkel(art, check)
    reasons = {x["setning"]: x["grunn"] for x in r["slettet"]}
    assert reasons["Kortet sier det samme som her."] == "gjentar kortet"
    assert reasons["Høyre ville utsette saken."].startswith("ikke støttet av kilden")
    assert reasons["Dette er et viktig vedtak."].startswith("forbudsliste: viktighetsord")
    assert r["avsnitt"][0]["tekst"] == WORDS and not r["avvist"]


def test_article_first_sentence_may_anchor_in_the_card():
    # A repeating first sentence stays when the paragraph adds something; it goes when nothing is new.
    art, check = article([["Kortet sier dette.", WORDS, WORDS], ["Kortet sier også dette.", "Og dette."]],
                         gjentar={(1, 0): True, (2, 0): True, (2, 1): True})
    r = redaktor.rediger_artikkel(art, check)
    assert r["avsnitt"][0]["tekst"] == f"Kortet sier dette. {WORDS} {WORDS}" and len(r["avsnitt"]) == 1
    assert {x["setning"] for x in r["slettet"]} == {"Kortet sier også dette.", "Og dette."}
    assert all(x["grunn"] == "gjentar kortet" for x in r["slettet"])
    assert r["nye_fakta"] == 2  # the anchor is not a new fact


def test_article_empty_paragraph_is_dropped_with_its_sources():
    art, check = article([["Gjentakelse."], [WORDS, WORDS, WORDS]], gjentar={(1, 0): True})
    r = redaktor.rediger_artikkel(art, check)
    assert len(r["avsnitt"]) == 1 and r["avsnitt"][0]["kilder"] == [{"dok": "protokoll", "side": 2}]


def test_article_limits_reject_not_trim():
    long = [[WORDS] * 5] * 4 + [[WORDS] * 3]  # 23 sentences × 16 words = 368 words
    r = redaktor.rediger_artikkel(*article(long))
    assert r["avvist"] and "artikkelen har 368 ord (maks 350)" in r["grunn"]
    assert r["ord"] == 368  # nothing trimmed
    six = [[WORDS]] * 6
    assert "artikkelen har 6 avsnitt (maks 5)" in redaktor.rediger_artikkel(*article(six))["grunn"]


def test_article_roles_are_kept_checked_and_rejected_not_edited():
    art, check = article(ART, roller_tags=["vedtak", "videre"])
    r = redaktor.rediger_artikkel(art, check)
    assert not r["avvist"] and [a["rolle"] for a in r["avsnitt"]] == ["vedtak", "videre"]
    # order, repeats and unknown names
    assert "avsnitt 2 (vedtak) står etter videre" in redaktor.rediger_artikkel(*article(ART, roller_tags=["videre", "vedtak"]))["grunn"]
    r = redaktor.rediger_artikkel(*article(ART, roller_tags=["vedtak", "vedtak"]))
    assert r["koder"] == ["rolle_feil"] and "rollen vedtak brukes 2 ganger" in r["grunn"]
    assert "ukjent rolle «intro»" in redaktor.rediger_artikkel(*article(ART, roller_tags=["intro", "videre"]))["grunn"]
    assert not redaktor.rediger_artikkel(*article(ART, roller_tags=["bakgrunn", "bakgrunn"]))["avvist"]
    # the checker's judgement: a paragraph that doesn't fit its role is a rewrite, never a deletion
    art, check = article(ART, roller_tags=["vedtak", "begrunnelse"])
    check["roller"] = [{"avsnitt": 2, "passer": False, "begrunnelse": "Avsnittet gjengir stemmetall."}]
    r = redaktor.rediger_artikkel(art, check)
    assert r["avvist"] and r["koder"] == ["rolle_feil"] and "avsnitt 2 passer ikke rollen begrunnelse: Avsnittet gjengir stemmetall." in r["grunn"]
    assert r["slettet"] == [] and redaktor.omskrivbar(r, redaktor.OMSKRIVBARE_ARTIKKEL)
    # untagged paragraphs (older articles) pass
    assert not redaktor.rediger_artikkel(*article(ART))["avvist"]


def test_article_with_too_little_left_is_rejected():
    art, check = article([[WORDS, "Gjentar."]], gjentar={(0 + 1, 1): True})
    r = redaktor.rediger_artikkel(art, check)
    assert r["avvist"] and r["koder"] == ["for_lite_nytt"] and r["ord"] == 16
    r = redaktor.rediger_artikkel({"nr": "1/26", "avsnitt": []}, {"setninger": [], "personvern": [], "blocked": ""})
    assert r["avvist"] and r["avsnitt"] == []


def test_article_unchecked_blocked_or_personvern_rejects():
    art, check = article(ART)
    assert redaktor.rediger_artikkel(art, None)["grunn"] == "ikke faktasjekket"
    assert redaktor.rediger_artikkel(art, {**check, "blocked": "uleselig"})["avvist"]
    assert redaktor.rediger_artikkel(art, {**check, "personvern": ["Navn."]})["koder"] == ["personvern"]


def test_article_missing_gjentar_flag_counts_as_repetition():
    art, check = article(ART)
    del check["setninger"][1]["gjentar_kortet"]
    r = redaktor.rediger_artikkel(art, check)
    assert r["slettet"][0] == {"avsnitt": 1, "setning": "Høyre ville utsette saken.", "grunn": "gjentar kortet"}


def test_article_output_is_always_a_deletion_of_the_input():
    rng = random.Random(4321)
    pool = [WORDS, "Høyre ville utsette saken.", "Dette er viktig.", "Arbeidet starter i mars.",
            "Hva nå?", "Du får vite mer.", "SV stemte mot.", "Kostnaden er 3 mill. kroner."]
    for _ in range(400):
        paragraphs = [rng.sample(pool, rng.randint(1, 4)) for _ in range(rng.randint(0, 6))]
        flags = {(n, i): rng.random() > 0.3 for n in range(1, 8) for i in range(5)}
        rep = {(n, i): rng.random() < 0.2 for n in range(1, 8) for i in range(5)}
        art, check = article(paragraphs, stottet=flags, gjentar=rep)
        mutation = rng.randint(0, 5)
        if mutation == 1 and check["setninger"]:
            check["setninger"].insert(rng.randrange(len(check["setninger"])),
                                      {"avsnitt": 1, "setning": "Oppdiktet påstand.", "stottet": True, "gjentar_kortet": False})
        elif mutation == 2 and check["setninger"]:
            check["setninger"].pop(rng.randrange(len(check["setninger"])))
        elif mutation == 3:
            rng.shuffle(check["setninger"])
        elif mutation == 4:
            check = None
        r = redaktor.rediger_artikkel(art, check)
        originals = [" ".join(p) for p in paragraphs]
        pos = 0
        for a in r["avsnitt"]:
            # each kept paragraph comes from a later input paragraph, as a deletion of it
            while pos < len(paragraphs) and a["tekst"] not in _joins(paragraphs[pos]) | {originals[pos]}:
                pos += 1
            assert pos < len(paragraphs), a["tekst"]
            pos += 1
        assert "Oppdiktet" not in json.dumps(r["avsnitt"], ensure_ascii=False)


def test_run_artikler_ignores_a_check_older_than_the_article(tmp_path):
    import os
    item = {"id": "x", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26"}
    art, check = article(ART)
    paths = {}
    for step, obj in (("artikler", art), ("artikkelsjekk", check)):
        paths[step] = tmp_path / step / "571424" / "2026-09-23" / "1-26.json"
        paths[step].parent.mkdir(parents=True)
        paths[step].write_text(json.dumps(obj))
    os.utime(paths["artikkelsjekk"], (1_000, 1_000))
    assert redaktor.run_artikler([item], tmp_path) == {"redigert": 1, "publiserbar": 0, "avvist": 1, "slettet": 0, "endret": 1,
                                                       "venter": 0}
    out = json.loads((tmp_path / "artikler-redigert" / "571424" / "2026-09-23" / "1-26.json").read_text())
    assert out["grunn"] == "ikke faktasjekket"
    os.utime(paths["artikkelsjekk"], None)  # fresh check now
    assert redaktor.run_artikler([item], tmp_path)["publiserbar"] == 1
    line = json.loads((tmp_path / "metrikk.jsonl").read_text().splitlines()[-1])
    assert line["type"] == "artikkel" and line["ord"] == line["ord_for"] == 56 and line["setninger_per_nytt_faktum"] == 1.0


def test_importance_words_allowed_when_reported_in_articles():
    from tingposten.redaktor import banned
    assert banned("Byrådet viste til at det vil ta betydelig tid å flytte havnen.", "artikkel") is None
    assert banned("SV mente forslaget var viktig for barna.", "artikkel") is None
    assert banned("Flertallet pekte på at saken er avgjørende for økonomien.", "artikkel") is None
    # unattributed claims are still deleted, and cards keep the narrower rule
    assert banned("Vedtaket er historisk for Bergen.", "artikkel").startswith("viktighetsord")
    assert banned("SV mente forslaget var viktig.", "status").startswith("viktighetsord")
    # "V" only counts as the party when capitalised as a subject
    assert banned("Vedtaket er viktig.", "artikkel").startswith("viktighetsord")
