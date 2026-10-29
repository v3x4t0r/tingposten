import json
import os
import time

from tingposten import pipeline

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
    for name in ("skribent", "faktasjekker", "klassifiserer"):
        monkeypatch.setattr(getattr(pipeline, name), "run",
                            lambda items, data_dir, llm, force, parallel, n=name: calls.append((n, llm, force)) or {"ok": len(items)})
    monkeypatch.setattr(pipeline.redaktor, "run", lambda items, data_dir: calls.append(("redaktor", None, None)) or {"ok": 1})
    monkeypatch.setattr(pipeline, "todo", lambda step, items, data_dir: items)
    pipeline.kjor(tmp_path, llms={"skriv": "W", "sjekk": "C", "klassifiser": "K"})
    assert calls == [("skribent", "W", True), ("faktasjekker", "C", True), ("klassifiserer", "K", True), ("redaktor", None, None)]
