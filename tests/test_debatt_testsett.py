import json
import threading
import urllib.request

import pytest

from tingposten.debatt import moderering as m
from tingposten.debatt import testsett as t
from tingposten.debatt.regler import REGLER


def sett_inn(d, items):
    d.mkdir(parents=True, exist_ok=True)
    (d / "testsett.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))


ITEMS = [
    {"id": "t001", "sak": "k/1", "sak_tittel": "Bybane", "sak_ingress": "", "forelder": None,
     "tekst": "Byrådet burde skamme seg over vedtaket", "forslag": "ok", "grense": True, "merknad": ""},
    {"id": "t002", "sak": "k/1", "sak_tittel": "Bybane", "sak_ingress": "", "forelder": "Enig",
     "tekst": "Noen burde besøke ham hjemme", "forslag": "trussel", "grense": True, "merknad": ""},
    {"id": "t003", "sak": "k/1", "sak_tittel": "Bybane", "sak_ingress": "", "forelder": None,
     "tekst": "Kjøp billige klokker!", "forslag": "spam", "grense": False, "merknad": ""},
]


def v(regel, p):
    return m.Vurdering(regel, {k: (p if k == regel else (1 - p) / 7) for k in REGLER}, "fake", 0)


def test_run_records_every_comment_and_errors(tmp_path):
    sett_inn(tmp_path, ITEMS)
    fake = m.FakeModerator([v("ok", 0.9), m.ModereringFeil("nede"), v("spam", 0.99)])
    ut = t.kjor(fake, t.les_sett(tmp_path), tmp_path)
    rader = t.les_resultat(ut)
    assert [r["id"] for r in rader] == ["t001", "t002", "t003"]
    assert rader[1] == {"id": "t002", "feil": "nede"}
    assert fake.kall[1][2] == "Enig"  # the parent comment is passed on


def test_report_counts_dangerous_misses_and_wrong_stops_per_threshold(tmp_path):
    rader = [
        {"id": "t001", **v("trakassering", 0.8).som_dict()},   # ok stopped at low thresholds
        {"id": "t002", **v("ok", 0.88).som_dict()},            # a threat published at ≤ 0.85
        {"id": "t003", **v("spam", 0.99).som_dict()},
    ]
    r = t.rapport(rader, ITEMS, fasit={})
    assert r["n"] == 3 and r["kontrollert"] == 0 and r["kalibrert"] and r["regel_treff"] == pytest.approx(1 / 3, abs=1e-3)
    lav, hoy = r["terskler"]["0.7"], r["terskler"]["0.9"]
    assert (lav["farlige_slipp"], lav["slipp"], lav["feil_stoppet"], lav["stoppet"]) == (1, ["t002"], 1, ["t001"])
    assert (hoy["farlige_slipp"], hoy["feil_stoppet"], hoy["avvis"], hoy["redaktor"]) == (0, 0, 1, 2)


def test_owner_label_overrides_the_drafters_suggestion(tmp_path):
    sett_inn(tmp_path, ITEMS)
    t.lagre_fasit(tmp_path, "t001", "trakassering", "  skamme seg om en person  ")
    fasit = t.les_fasit(tmp_path)
    assert t.fasit_for(ITEMS[0], fasit) == ("trakassering", True)
    assert t.fasit_for(ITEMS[1], fasit) == ("trussel", False)
    assert fasit["t001"]["merknad"] == "skamme seg om en person"
    with pytest.raises(ValueError):
        t.lagre_fasit(tmp_path, "t001", "kanskje")


def test_uncalibrated_runs_are_scored_by_label(tmp_path):
    rader = [{"id": i["id"], **m.Vurdering(i["forslag"], None, "hailo:q", 5).som_dict()} for i in ITEMS]
    ikke_stolt = t.rapport(rader, ITEMS, {})
    stolt = t.rapport(rader, ITEMS, {}, stol_pa_etikett=True)
    assert list(ikke_stolt["terskler"]) == ["etikett"] and ikke_stolt["terskler"]["etikett"]["redaktor"] == 3
    assert stolt["terskler"]["etikett"]["publiser"] == 1 and stolt["terskler"]["etikett"]["avvis"] == 2
    assert "| hailo:q | etikett |" in t.tabell({"hailo:q": stolt})


def test_labelling_page_saves_labels_for_known_ids_only(tmp_path):
    sett_inn(tmp_path, ITEMS)
    server = t.make_server(0, tmp_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        side = urllib.request.urlopen(base + "/").read().decode()
        assert "Noen burde besøke ham hjemme" in side and "innerHTML" not in side

        def post(body):
            req = urllib.request.Request(base + "/lagre", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert post({"id": "t003", "regel": "spam"}) == 200
        assert post({"id": "t999", "regel": "spam"}) == 400
        assert post({"id": "t003", "regel": "tull"}) == 400
        assert t.les_fasit(tmp_path)["t003"]["regel"] == "spam"
    finally:
        server.shutdown()


def test_newest_run_per_moderator(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    for navn in ("hailo_qwen3_1.7b-20261031-120000", "hailo_qwen3_1.7b-20261031-130000", "jev_jev-20261031-110000"):
        (test_dir / f"{navn}.jsonl").write_text('{"id": "t001", "feil": "x"}\n')
    nyeste = t.nyeste_kjoringer(tmp_path)
    assert nyeste["hailo_qwen3_1.7b"].name == "hailo_qwen3_1.7b-20261031-130000.jsonl" and "jev_jev" in nyeste


def test_report_with_per_rule_limits():
    rader = [{"id": "t001", **v("trakassering", 0.87).som_dict()}, {"id": "t002", **v("trussel", 0.75).som_dict()}]
    streng = m.Grenser({k: ((0.7, 0.3) if k in ("trussel", "hets") else (0.9, 0.6)) for k in REGLER if k != "ok"})
    r = t.rapport(rader, ITEMS, {}, grenser={"streng": streng})["terskler"]["streng"]
    assert (r["avvis"], r["redaktor"], r["feil_stoppet"]) == (1, 1, 0)  # the threat stopped, harassment doubtful
