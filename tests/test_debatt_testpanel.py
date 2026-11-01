"""The test panel (debatt/testpanel.py): invite links, ratings, the others' ratings only
after rating, the editor's meetings, the server endpoints and the control panel."""
import json
from datetime import date

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from tingposten import admin, bygg, config  # noqa: E402
from tingposten.debatt import server as srv  # noqa: E402
from tingposten.debatt import testpanel as tp  # noqa: E402
from tingposten.debatt import tjeneste as tj  # noqa: E402
from tingposten.debatt.db import Db  # noqa: E402

from test_bygg import BYSTYRET, data_dir, fake_rank  # noqa: E402,F401 (fixtures)

SIDE = "https://tingposten.no"


def _mote(id_, organ, dato, *keys):
    return {"id": id_, "organ": organ, "dato": dato, "dato_tekst": dato, "saker": [
        {"key": k, "nr": "Sak 1/26", "overskrift": f"Overskrift {k}", "ingress": "Ingress", "felt": [], "status": "",
         "lenke": f"{SIDE}/bergen/sak/{k}/"} for k in keys]}


MOTER = [
    _mote("571424/2026-10-27", "Bystyret", "2026-10-27", "571424/2026-10-27/1-26", "571424/2026-10-27/2-26"),
    _mote("571424/2026-09-23", "Bystyret", "2026-09-23", "571424/2026-09-23/3-26"),
    _mote("571424/2026-08-20", "Bystyret", "2026-08-20", "571424/2026-08-20/4-26"),
    _mote("900102/2026-10-29", "Byrådet", "2026-10-29", "900102/2026-10-29/5-26"),
]
A, B = "571424/2026-10-27/1-26", "571424/2026-10-27/2-26"


@pytest.fixture
def db(tmp_path):
    return Db(tmp_path / "d.db")


def test_invite_link_hash_only_and_closing(db):
    tid, token = tp.ny_tester(db, "  Kari  Nordmann ")
    assert tp.tester(db, token) == {"id": tid, "navn": "Kari Nordmann"}
    assert token not in json.dumps([dict(r) for r in db.les("SELECT * FROM testere")])
    tp.steng(db, tid)
    assert tp.tester(db, token) is None
    ny = tp.ny_lenke(db, tid)
    assert tp.tester(db, token) is None and tp.tester(db, ny)["navn"] == "Kari Nordmann"
    with pytest.raises(tj.Feil):
        tp.ny_tester(db, " ")


def test_newest_meetings_unless_the_editor_chose(db):
    assert [m_["id"] for m_ in tp.aktuelle(db, MOTER)] == ["571424/2026-10-27", "571424/2026-09-23", "900102/2026-10-29"]
    tp.velg(db, ["571424/2026-08-20"])
    assert [m_["id"] for m_ in tp.aktuelle(db, MOTER)] == ["571424/2026-08-20"]
    tp.velg(db, [])
    assert len(tp.aktuelle(db, MOTER)) == 3


def test_rating_saves_each_click_and_shows_the_others_only_after_rating(db):
    kari = {"id": tp.ny_tester(db, "Kari")[0], "navn": "Kari"}
    ola = {"id": tp.ny_tester(db, "Ola")[0], "navn": "Ola"}
    tp.vurder(db, ola, MOTER, {"sak": A, "bor_vite": 5, "konkret": 2, "bunke": "forside", "klar": "ja", "kommentar": "Bra"})
    r = tp.vurder(db, kari, MOTER, {"sak": A, "bor_vite": 4})
    assert r["mine"]["bor_vite"] == 4 and r["andre"] is None            # not done yet: the others stay hidden
    side = tp.side(db, kari, MOTER)
    sak = side["moter"][0]["saker"][0]
    assert sak["key"] == A and sak["andre"] is None
    r = tp.vurder(db, kari, MOTER, {"sak": A, "konkret": 3, "bunke": "kort"})
    assert r["mine"] == {"bor_vite": 4, "konkret": 3, "bunke": "kort", "klar": None, "kommentar": None, "bekreftet": 0}
    assert r["andre"] == {"antall": 1, "bor_vite": 5.0, "konkret": 2.0, "bunker": {"forside": 1}, "klar": {"ja": 1},
                          "kommentarer": ["Bra"]}                        # no names
    r = tp.vurder(db, kari, MOTER, {"sak": A, "bunke": None})          # a second press undoes
    assert r["mine"]["bunke"] is None and r["andre"] is None
    for feil in ({"sak": "571424/2026-08-20/4-26", "bor_vite": 1},      # not among the meetings shown
                 {"sak": A, "bor_vite": 6}, {"sak": A, "bor_vite": True}, {"sak": A, "bunke": "toppsak"}):
        with pytest.raises(tj.Feil):
            tp.vurder(db, kari, MOTER, feil)
    res = tp.resultater(db)
    assert {(v["tester"], v["sak"]) for v in res["vurderinger"]} == {("Ola", A), ("Kari", A)}
    assert [t["vurdert"] for t in res["testere"]] == [1, 1]


def test_server_endpoints_need_a_valid_link(tmp_path):
    t = tj.Tjeneste(Db(tmp_path / "d.db"), None, {}, None, SIDE + "/")
    fil = tmp_path / "testpanel.json"
    fil.write_text(json.dumps({"moter": MOTER}))
    _, token = tp.ny_tester(t.db, "Kari")
    with TestClient(srv.lag_app(t, [SIDE], start_ko=False, testpanel_saker=tp.Saker(fil))) as c:
        assert c.get("/api/testpanel").status_code == 401
        assert c.get("/api/testpanel", headers={"Authorization": "Bearer feil-token-som-er-langt"}).status_code == 401
        h = {"Authorization": f"Bearer {token}"}
        side = c.get("/api/testpanel", headers=h).json()
        assert side["navn"] == "Kari" and len(side["moter"]) == 3
        r = c.post("/api/testpanel/vurdering", headers=h, json={"sak": B, "bor_vite": 2, "konkret": 1, "bunke": "stoy"})
        assert r.status_code == 200 and r.json()["andre"] == {"antall": 0}   # done, nobody else has rated yet
        assert c.post("/api/testpanel/vurdering", headers=h, json={"sak": B, "klar": "kanskje"}).status_code == 400


def test_control_panel_makes_links_and_picks_meetings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SITE_URL", "https://tingposten.no/")
    Db(tmp_path / "d.db")                                              # the comment database exists
    fil = tmp_path / "testpanel.json"
    fil.write_text(json.dumps({"moter": MOTER}))
    ko = admin.TestpanelKo(admin.DebattKo(tmp_path / "d.db"), fil)
    code, svar = ko.handle({"handling": "ny", "navn": "Kari"})
    assert code == 200 and svar["lenke"].startswith("https://tingposten.no/bergen/testpanel/#t=")
    st = ko.status()
    assert st["testere"][0]["navn"] == "Kari" and [x["valgt"] for x in st["moter"]] == [True, True, False, True]
    assert ko.handle({"handling": "moter", "moter": ["571424/2026-08-20"]})[0] == 200
    assert [x["valgt"] for x in ko.status()["moter"]] == [False, False, True, False]
    assert ko.handle({"handling": "steng", "id": st["testere"][0]["id"]})[0] == 200
    assert ko.status()["testere"][0]["stengt"]
    assert ko.handle({"handling": "steng", "id": "1"})[0] == 400


def test_build_writes_the_cards_and_a_hidden_page(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "DEBATT", "https://debatt.example.ts.net/")
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", False)
    site, debatt_dir = tmp_path / "site", tmp_path / "debatt"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=debatt_dir)
    moter = json.loads((debatt_dir / "testpanel.json").read_text())["moter"]
    sak = moter[0]["saker"][0]
    assert moter[0]["id"] == f"{BYSTYRET}/2026-09-23" and sak["overskrift"] and sak["felt"] and "score" not in sak
    side = (site / "bergen/testpanel/index.html").read_text()
    assert 'name="robots" content="noindex' in side and "static/testpanel.js" in side and "data-testpanel" in side
    assert "Disallow: /bergen/testpanel/" in (site / "robots.txt").read_text()
    assert "testpanel" not in (site / "sitemap.xml").read_text()


def test_part_two_points_out_own_contradictions_and_kept_answers_go_away(db):
    kari = {"id": tp.ny_tester(db, "Kari")[0], "navn": "Kari"}
    saker = [{**s, "ingen_ny_beslutning": s["key"] == B} for m_ in MOTER[:1] for s in m_["saker"]]
    moter = [{**MOTER[0], "saker": saker}] + MOTER[1:]
    tp.velg(db, [MOTER[0]["id"]])
    tp.vurder(db, kari, moter, {"sak": A, "bor_vite": 2, "konkret": 1, "bunke": "forside"})
    assert tp.side(db, kari, moter)["del2"]["klar"] is False             # not before every sak is rated
    tp.vurder(db, kari, moter, {"sak": B, "bor_vite": 5, "konkret": 4, "bunke": "kort"})
    d = tp.side(db, kari, moter)["del2"]
    assert d["klar"] and d["forside"] is None                              # no nudge about the forside share
    assert any("forsiden" in g for g in d["saker"][A])                     # forside, but «bør vite» 2
    assert any("Overskrift " + B in g for g in d["saker"][A])              # B: higher «bør vite», lower pile
    assert any("Ingenting nytt er vedtatt" in g for g in d["saker"][B])
    tp.vurder(db, kari, moter, {"sak": A, "behold": True, "del": 2})
    assert A not in tp.side(db, kari, moter)["del2"]["saker"]
    tp.vurder(db, kari, moter, {"sak": B, "konkret": 1, "del": 2})        # corrected in the review
    assert B not in tp.side(db, kari, moter)["del2"]["saker"]
    logg = tp.resultater(db)["logg"]
    assert ("konkret", "4", 1) in [(r["felt"], r["verdi"], r["del"]) for r in logg if r["sak"] == B]
    assert ("konkret", "1", 2) in [(r["felt"], r["verdi"], r["del"]) for r in logg if r["sak"] == B]
