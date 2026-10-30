import json
import os
import time

import pytest

from tingposten import pipeline


@pytest.fixture(autouse=True)
def all_dates(monkeypatch):
    """The sample meetings are older than config.TILBAKE_DAGER; include them."""
    monkeypatch.setattr(pipeline, "fra_dato", lambda idag=None: "2000-01-01")

BASE = {"moteDato": "2026-05-20", "kategori": "Saker til behandling", "vedtakFilId": "9",
        "innstillingFilId": "8"}


def item(nr, uid="571424", **kw):
    return {**BASE, "id": f"{uid}-{nr}", "utvalgId": uid, "referansenr": nr, "tittel": f"Sak {nr}", "seq": 1, **kw}


def store(data_dir, items):
    by = {}
    for i in items:
        by.setdefault(i["utvalgId"], {})[i["id"]] = i
    (data_dir / "agendapunkter").mkdir(parents=True)
    for uid, d in by.items():
        (data_dir / "agendapunkter" / f"{uid}.json").write_text(json.dumps(d))


def touch(path, age=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    t = time.time() - age
    os.utime(path, (t, t))


def test_saker_skips_recommendations_noise_and_undecided(tmp_path):
    store(tmp_path, [
        item("1/26"),
        item("2/26", vedtakFilId=None),
        item("3/26", kategori="Innkalling", tittel="Innkalling Bergen bystyre"),
        item("4/26", uid="900101"),
        item("5/26", uid="900102"),
    ])
    assert [i["referansenr"] for i in pipeline.saker(tmp_path)] == ["1/26", "5/26"]
    assert [i["referansenr"] for i in pipeline.saker(tmp_path, utvalg="900102")] == ["5/26"]


def test_todo_follows_inputs_and_staleness(tmp_path):
    a, b, c = item("1/26"), item("2/26"), item("3/26")
    k = lambda step, i: tmp_path / step / f"571424/2026-05-20/{i['referansenr'].replace('/', '-')}.json"
    p = lambda i, kind: tmp_path / "pdf" / f"571424/2026-05-20/{i['referansenr'].replace('/', '-')}_{kind}.pdf"
    for i in (a, b):
        touch(p(i, "protokoll"), age=100)
        touch(p(i, "saksframlegg"), age=100)
    touch(k("kort", a), age=50)          # a: card newer than its PDFs → nothing to write
    touch(k("sjekk", a), age=80)         # …but the check is older than the card → re-check
    # b: PDFs but no card → write; c: no protokoll → cannot write
    assert [i["referansenr"] for i in pipeline.todo("skriv", [a, b, c], tmp_path)] == ["2/26"]
    assert [i["referansenr"] for i in pipeline.todo("sjekk", [a, b, c], tmp_path)] == ["1/26"]
    assert [i["referansenr"] for i in pipeline.todo("klassifiser", [a, b, c], tmp_path)] == ["1/26"]
    touch(k("sjekk", a), age=10)
    assert pipeline.todo("sjekk", [a, b, c], tmp_path) == []
    assert [i["referansenr"] for i in pipeline.mangler_pdf([a, b, c], tmp_path)] == ["3/26"]


def test_kjor_runs_steps_in_order_with_given_backends(tmp_path, monkeypatch):
    store(tmp_path, [item("1/26")])
    calls = []
    for mod, fn in (("skribent", "run"), ("faktasjekker", "run"), ("klassifiserer", "run"),
                    ("skribent", "run_artikkel"), ("faktasjekker", "run_artikkel")):
        monkeypatch.setattr(getattr(pipeline, mod), fn,
                            lambda items, data_dir, llm, force, parallel, n=f"{mod}.{fn}", **kw: calls.append((n, llm, force)) or {"ok": len(items)})
    monkeypatch.setattr(pipeline.redaktor, "run", lambda items, data_dir: calls.append(("redaktor.run", None, None)) or {"ok": 1})
    monkeypatch.setattr(pipeline.redaktor, "run_artikler", lambda items, data_dir: calls.append(("redaktor.run_artikler", None, None)) or {"ok": 1})
    monkeypatch.setattr(pipeline, "todo", lambda step, items, data_dir: items)
    monkeypatch.setattr(pipeline, "velg_artikler", lambda items, data_dir: items)
    monkeypatch.setattr(pipeline, "omskriv_kandidater", lambda items, data_dir, **kw: {})
    pipeline.kjor(tmp_path, llms={"skriv": "W", "sjekk": "C", "klassifiser": "K"})
    assert calls == [("skribent.run", "W", True), ("faktasjekker.run", "C", True), ("klassifiserer.run", "K", True),
                     ("redaktor.run", None, None),
                     ("skribent.run_artikkel", "W", True), ("faktasjekker.run_artikkel", "C", True),
                     ("redaktor.run_artikler", None, None)]


def test_kjor_steg_subset_skips_the_rest(tmp_path, monkeypatch):
    store(tmp_path, [item("1/26")])
    monkeypatch.setattr(pipeline, "todo", lambda step, items, data_dir: items)
    monkeypatch.setattr(pipeline.redaktor, "run", lambda items, data_dir: {"ok": 1})
    result = pipeline.kjor(tmp_path, steg=("rediger",), llms={"skriv": "W", "sjekk": "C", "klassifiser": "K"})
    assert list(result) == ["rediger"]  # no rewrite pass without "skriv"


def ranked(*rows):
    """score.rank stand-in: rows of (nr, score, status kode)."""
    return lambda items, pdf_dir, data_dir=None: [
        {"nr": nr, "score": sc, "status": {"kode": kode}} for nr, sc, kode in rows]


def redigert(data_dir, it, avvist=False, **extra):
    path = data_dir / "redigert" / f"571424/2026-05-20/{it['referansenr'].replace('/', '-')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nr": it["referansenr"], "avvist": avvist, "felt": {}, "slettet": [], **extra}))
    return path


def test_velg_artikler_takes_top_share_of_decided_publishable(tmp_path, monkeypatch):
    items = [item(f"{n}/26") for n in range(1, 10)]
    for it in items:
        redigert(tmp_path, it, avvist=it["referansenr"] == "2/26")
    # 8 decided (9/26 was sent back) → ceil(0.25 × 8) = 2 articles; 2/26 is avvist so 3/26 moves up
    monkeypatch.setattr(pipeline.score, "rank", ranked(
        ("9/26", 99, "sendt_tilbake"), ("2/26", 90, "vedtatt"), ("1/26", 80, "vedtatt"), ("3/26", 70, "vedtatt"),
        *((f"{n}/26", 10, "vedtatt") for n in range(4, 9))))
    chosen = pipeline.velg_artikler(items, tmp_path, andel=0.25)
    assert [i["referansenr"] for i in chosen] == ["1/26", "3/26"]


def test_velg_artikler_at_least_one_and_none_without_decided(tmp_path, monkeypatch):
    items = [item("1/26"), item("2/26")]
    for it in items:
        redigert(tmp_path, it)
    monkeypatch.setattr(pipeline.score, "rank", ranked(("1/26", 5, "vedtatt"), ("2/26", 50, "orientering")))
    assert [i["referansenr"] for i in pipeline.velg_artikler(items, tmp_path, andel=0.01)] == ["1/26"]
    monkeypatch.setattr(pipeline.score, "rank", ranked(("1/26", 5, "valg"), ("2/26", 50, "orientering")))
    assert pipeline.velg_artikler(items, tmp_path, andel=0.5) == []


def test_article_steps_follow_the_edited_card(tmp_path):
    a = item("1/26")
    k = lambda step: tmp_path / step / "571424/2026-05-20/1-26.json"
    touch(k("redigert"), age=50)
    assert pipeline.todo("artikkel", [a], tmp_path) == [a]
    touch(k("artikler"), age=10)
    assert pipeline.todo("artikkel", [a], tmp_path) == []
    assert pipeline.todo("artikkelsjekk", [a], tmp_path) == [a]
    touch(k("artikkelsjekk"), age=5)
    assert pipeline.todo("artikkelsjekk", [a], tmp_path) == []
    touch(k("redigert"), age=0)  # the card changed → new article, and the check is stale too
    assert pipeline.todo("artikkel", [a], tmp_path) == [a]
    assert pipeline.todo("artikkelsjekk", [a], tmp_path) == [a]
    assert pipeline.todo("artikkelrediger", [a], tmp_path) == [a]


def card(data_dir, it, omskrevet=None):
    path = data_dir / "kort" / f"571424/2026-05-20/{it['referansenr'].replace('/', '-')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"modell": "x"} | ({"omskrevet": omskrevet} if omskrevet is not None else {})
    path.write_text(json.dumps({"nr": it["referansenr"], "meta": meta}))


def test_omskriv_kandidater(tmp_path):
    lang, privat, igjen, ok = item("1/26"), item("2/26"), item("3/26"), item("4/26")
    for it, kw in ((lang, {"koder": ["for_mange_setninger"], "grunn": "status har 3 setninger (maks 2)",
                           "slettet": [{"felt": "status", "setning": "Feil tall.", "grunn": "ikke støttet av kilden"}]}),
                   (privat, {"koder": ["personvern", "overskrift_lang"], "grunn": "personvern: navn"}),
                   (igjen, {"koder": ["overskrift_lang"], "grunn": "overskrift over 70 tegn (71)"}),
                   (ok, {"koder": []})):
        redigert(tmp_path, it, avvist=bool(kw["koder"]), **kw)
    for it in (lang, privat, ok):
        card(tmp_path, it)
    card(tmp_path, igjen, omskrevet=1)  # already had its one rewrite
    fb = pipeline.omskriv_kandidater([lang, privat, igjen, ok], tmp_path)
    assert list(fb) == ["571424/2026-05-20/1-26"]
    assert "status har 3 setninger" in fb["571424/2026-05-20/1-26"] and "«Feil tall.»" in fb["571424/2026-05-20/1-26"]


def test_kjor_rewrites_a_rejected_card_once_end_to_end(tmp_path, monkeypatch):
    """Real writer, checker and editor with fake models: an over-limit card is
    rejected, rewritten once with the rejection as feedback, checked again and
    published."""
    from tingposten import kildetekst
    from tingposten.llm import FakeLLM
    it = item("1/26")
    store(tmp_path, [it])
    for kind in ("protokoll", "saksframlegg"):
        touch(tmp_path / "pdf" / f"571424/2026-05-20/1-26_{kind}.pdf", age=1000)
    monkeypatch.setattr(kildetekst, "kilder_for", lambda item, data_dir: {"saksframlegg": "s", "protokoll": "p"})

    def card(status):
        return {"overskrift": "Planen for parken er vedtatt", "ingress": "", "hva_endres": "Planen er vedtatt.", "hvem_pavirkes": "",
                "hvorfor": "", "status": status, "nar_og_hvor": "", "merknad": "", "blocked": "",
                "kilder": [{"felt": "hva_endres", "dok": "protokoll", "side": 1}, {"felt": "status", "dok": "protokoll", "side": 1}]}

    def check(status_sentences):
        return {"personvern": [], "blocked": "", "setninger": [
            {"felt": "overskrift", "setning": "Planen for parken er vedtatt", "stottet": True, "kilde": None, "begrunnelse": ""},
            {"felt": "hva_endres", "setning": "Planen er vedtatt.", "stottet": True, "kilde": None, "begrunnelse": ""},
            *({"felt": "status", "setning": x, "stottet": True, "kilde": None, "begrunnelse": ""} for x in status_sentences)]}

    long = "Vedtatt. Med 40 stemmer. Mot 27."
    writer = FakeLLM([card(long), card(long), card("Vedtatt med 40 stemmer.")])
    checker = FakeLLM([check(["Vedtatt.", "Med 40 stemmer.", "Mot 27."]), check(["Vedtatt med 40 stemmer."])])
    result = pipeline.kjor(tmp_path, steg=("skriv", "sjekk", "rediger"), parallel=1,
                           llms={"skriv": writer, "sjekk": checker, "klassifiser": None})
    assert result["rediger"]["avvist"] == 1
    assert result["omskriv"]["antall"] == {"omskrevet": 1}
    assert "THE EDITOR REJECTED THE PREVIOUS DRAFT" in writer.prompts[2] and "status har 3 setninger" in writer.prompts[2]
    final = json.loads((tmp_path / "redigert/571424/2026-05-20/1-26.json").read_text())
    assert not final["avvist"] and final["felt"]["status"] == "Vedtatt med 40 stemmer."
    assert json.loads((tmp_path / "kort/571424/2026-05-20/1-26.json").read_text())["meta"]["omskrevet"] == 1
    # a second run finds nothing to rewrite (and no model calls are left)
    again = pipeline.kjor(tmp_path, steg=("skriv", "sjekk", "rediger"), llms={"skriv": writer, "sjekk": checker, "klassifiser": None})
    assert "omskriv" not in again and again["skriv"] == {} and again["sjekk"] == {}


def test_omskriv_kandidater_for_articles(tmp_path):
    lang, privat, igjen, kort_avvist = item("1/26"), item("2/26"), item("3/26"), item("4/26")
    path = lambda step, it: tmp_path / step / f"571424/2026-05-20/{it['referansenr'].replace('/', '-')}.json"
    for it, koder, omskrevet in ((lang, ["for_lang"], None), (privat, ["personvern", "for_lang"], None),
                                 (igjen, ["for_lite_nytt"], 1), (kort_avvist, ["ikke_sjekket"], None)):
        path("artikler-redigert", it).parent.mkdir(parents=True, exist_ok=True)
        path("artikler-redigert", it).write_text(json.dumps({
            "avvist": True, "koder": koder, "grunn": "artikkelen har 262 ord (maks 250)",
            "slettet": [{"avsnitt": 2, "setning": "Feil tall.", "grunn": "ikke støttet av kilden"}]}))
        path("artikler", it).parent.mkdir(parents=True, exist_ok=True)
        meta = {"omskrevet": omskrevet} if omskrevet else {}
        path("artikler", it).write_text(json.dumps({"avsnitt": [], "meta": meta}))
    fb = pipeline.omskriv_kandidater([lang, privat, igjen, kort_avvist], tmp_path, artikler=True)
    assert list(fb) == ["571424/2026-05-20/1-26"]
    assert "262 ord" in fb["571424/2026-05-20/1-26"] and "Slettet fra avsnitt 2: «Feil tall.»" in fb["571424/2026-05-20/1-26"]
    # the card rules don't apply to articles and vice versa
    assert pipeline.omskriv_kandidater([lang], tmp_path) == {}


def test_kjor_rewrites_a_rejected_article_once(tmp_path, monkeypatch):
    store(tmp_path, [item("1/26")])
    calls = []
    monkeypatch.setattr(pipeline, "todo", lambda step, items, data_dir: items)
    monkeypatch.setattr(pipeline, "velg_artikler", lambda items, data_dir: items)
    monkeypatch.setattr(pipeline.skribent, "run_artikkel",
                        lambda items, data_dir, llm, force, parallel, **kw: calls.append(("artikkel", kw.get("feedback"))) or {})
    monkeypatch.setattr(pipeline.faktasjekker, "run_artikkel",
                        lambda items, data_dir, llm, force, parallel: calls.append(("artikkelsjekk", None)) or {})
    monkeypatch.setattr(pipeline.redaktor, "run_artikler", lambda items, data_dir: calls.append(("artikkelrediger", None)) or {})
    rounds = iter([{"571424/2026-05-20/1-26": "Avvist fordi: for lang."}, {}])
    monkeypatch.setattr(pipeline, "omskriv_kandidater",
                        lambda items, data_dir, artikler=False: next(rounds) if artikler else {})
    result = pipeline.kjor(tmp_path, steg=("artikkel", "artikkelsjekk", "artikkelrediger"), llms={"skriv": "W", "sjekk": "C"})
    assert calls == [("artikkel", None), ("artikkelsjekk", None), ("artikkelrediger", None),
                     ("artikkel", {"571424/2026-05-20/1-26": "Avvist fordi: for lang."}),
                     ("artikkelsjekk", None), ("artikkelrediger", None)]
    assert "omskriv_artikkel" in result


def test_saker_and_kjor_only_go_back_tilbake_dager(tmp_path, monkeypatch):
    old, new = item("1/26", moteDato="2026-01-28"), item("2/26", moteDato="2026-09-23")
    store(tmp_path, [old, new])
    assert [i["referansenr"] for i in pipeline.saker(tmp_path, fra="2026-07-29")] == ["2/26"]
    seen = []
    monkeypatch.setattr(pipeline, "fra_dato", lambda idag=None: "2026-07-29")
    monkeypatch.setattr(pipeline, "todo", lambda step, items, data_dir: seen.extend(i["referansenr"] for i in items) or [])
    pipeline.kjor(tmp_path, steg=("skriv",), llms={"skriv": None, "sjekk": None, "klassifiser": None})
    assert seen == ["2/26"]
    seen.clear()  # an explicit meeting date is always allowed
    pipeline.kjor(tmp_path, dato="2026-01-28", steg=("skriv",), llms={"skriv": None, "sjekk": None, "klassifiser": None})
    assert seen == ["1/26"]


def test_fra_dato_counts_tilbake_dager(monkeypatch):
    import datetime
    monkeypatch.undo()  # the real fra_dato, not the autouse stand-in
    monkeypatch.setattr(pipeline.config, "TILBAKE_DAGER", 92)
    assert pipeline.fra_dato(datetime.date(2026, 10, 29)) == "2026-07-29"


def test_cards_from_older_writer_rules_are_rewritten(tmp_path):
    a = item("1/26")
    for kind in ("protokoll", "saksframlegg"):
        touch(tmp_path / "pdf" / f"571424/2026-05-20/1-26_{kind}.pdf", age=100)
    kort = tmp_path / "kort/571424/2026-05-20/1-26.json"
    kort.parent.mkdir(parents=True)
    kort.write_text(json.dumps({"overskrift": "x", "meta": {"prompt_versjon": pipeline.skribent.PROMPT_VERSJON}}))
    assert pipeline.todo("skriv", [a], tmp_path) == []
    kort.write_text(json.dumps({"overskrift": "x", "meta": {"prompt_versjon": "skribent-0"}}))
    assert pipeline.todo("skriv", [a], tmp_path) == [a]
    kort.write_text(json.dumps({"overskrift": "x"}))  # from before versions were recorded
    assert pipeline.todo("skriv", [a], tmp_path) == [a]


def test_no_card_until_the_protokoll_covers_the_meeting(tmp_path, monkeypatch):
    import datetime
    a = item("1/26", moteDato="2026-10-27")
    for kind in ("protokoll", "saksframlegg"):
        touch(tmp_path / "pdf" / f"571424/2026-10-27/1-26_{kind}.pdf", age=100)
    monkeypatch.setattr(pipeline.protokoll, "parse_pdf", lambda path: {"motedato": "2026-06-17"})
    assert pipeline.todo("skriv", [a], tmp_path) == []
    assert pipeline.ufullstendige_protokoller([a], tmp_path, idag=datetime.date(2026, 10, 29)) == [a]
    assert pipeline.ufullstendige_protokoller([a], tmp_path, idag=datetime.date(2026, 12, 29)) == []  # gave up
    monkeypatch.setattr(pipeline.protokoll, "parse_pdf", lambda path: {"motedato": "2026-10-27"})
    assert pipeline.todo("skriv", [a], tmp_path) == [a]
    assert pipeline.ufullstendige_protokoller([a], tmp_path, idag=datetime.date(2026, 10, 29)) == []
