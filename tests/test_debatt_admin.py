"""The editor's comment queue in the control panel."""
import json
import threading

import pytest

from tingposten import admin
from tingposten.debatt import moderering as m
from tingposten.debatt import tjeneste as tj
from tingposten.debatt.db import Db
from tingposten.debatt.regler import REGLER, REGLER_VERSJON

from test_admin import AUTH, call  # noqa: F401

SAK = "571424/2026-09-23/266-26"


class Usikker(m.Moderator):
    name = "usikker"

    def _vurder(self, tekst, sak, forelder):
        return m.Vurdering("trakassering", {k: (0.6 if k == "trakassering" else 0.05) for k in REGLER}, "usikker", 0)


@pytest.fixture
def oppsett(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    db_path = tmp_path / "debatt.db"
    sendt = []
    t = tj.Tjeneste(Db(db_path), m.Kjede([Usikker()], m.Grenser.jevn(0.85)), {SAK: {"tittel": "Bybanen"}},
                    lambda e, lenke: sendt.append(lenke), "https://tingposten.no/", umiddelbar_moderering=True)
    t.be_om_lenke("ola@b.no")
    okt = t.bekreft(sendt[-1].split("#t=")[1])["okt"]
    with t.db.tx() as c:
        c.execute("UPDATE brukere SET navn = 'Ola', quiz = ?", (REGLER_VERSJON,))
    ola = t.bruker(okt)
    k = t.ny_kommentar(ola, SAK, "Kanskje litt frekt")
    srv = admin.make_server("127.0.0.1", 0, passord="hemmelig", kjorer=admin.Kjorer(root=tmp_path, data_dir=tmp_path),
                            site_dir=tmp_path / "site", debatt=admin.DebattKo(db_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", t, k, okt
    srv.shutdown()


def test_queue_shows_name_text_and_verdict_but_never_the_email(oppsett):
    base, _, k, _ = oppsett
    code, body = call(base + "/debatt.json")
    d = json.loads(body)
    assert code == 200 and d["aktiv"] and [x["id"] for x in d["ko"]] == [k["id"]]
    rad = d["ko"][0]
    assert rad["navn"] == "Ola" and rad["tekst"] == "Kanskje litt frekt" and rad["status"] == "til_redaktor"
    assert rad["vurderinger"][0]["regel"] == "trakassering" and rad["vurderinger"][0]["sikkerhet"] == 0.6
    assert rad["lenke"].endswith(f"/bergen/sak/{SAK}/#k{k['id']}")
    assert b"ola@b.no" not in body


def test_editor_decisions_need_the_panel_header(oppsett):
    base, t, k, okt = oppsett
    sett = {"id": k["id"], "handling": "publiser", "tekst": "Kanskje litt frekt"}
    assert call(base + "/debatt", sett)[0] == 403
    assert call(base + "/debatt", {"id": k["id"], "handling": "slett alt"}, X_Tingposten="admin")[0] == 400
    assert call(base + "/debatt", {**sett, "id": 999}, X_Tingposten="admin")[0] == 404
    assert call(base + "/debatt", {**sett, "tekst": "en annen tekst"}, X_Tingposten="admin")[0] == 409
    assert call(base + "/debatt", sett, X_Tingposten="admin")[0] == 200
    assert t.trad(SAK, None)["antall"] == 1 and json.loads(call(base + "/debatt.json")[1])["ko"] == []
    k2 = t.ny_kommentar(t.bruker(okt), SAK, "Enda en")
    code, body = call(base + "/debatt", {"id": k2["id"], "handling": "sperr", "regel": "trakassering"},
                      X_Tingposten="admin")
    assert code == 200 and "sperret" in json.loads(body)["melding"]
    assert t.bruker(okt) is None  # logged out and blocked


def test_no_comment_database_means_no_section(tmp_path):
    ko = admin.DebattKo(tmp_path / "finnes-ikke.db")
    assert ko.status() == {"aktiv": False, "ko": [], "stoppet": []}
    assert ko.handle({"id": 1, "handling": "publiser"})[0] == 400
    assert not (tmp_path / "finnes-ikke.db").exists()


def test_local_test_panel_never_starts_a_run():
    from tingposten.debatt.lokal import _IngenKjoring
    k = _IngenKjoring()
    assert k.start("manuell")[0] == 403 and k.start("sjekk")[0] == 403
    assert k.pagar() in (True, False)  # the rest of the panel still reads the status


def test_editor_can_undo_a_removal_and_overrule_the_model(oppsett):
    base, t, k, okt = oppsett
    assert call(base + "/debatt", {"id": k["id"], "handling": "fjern"}, X_Tingposten="admin")[0] == 200
    d = json.loads(call(base + "/debatt.json")[1])
    assert d["ko"] == [] and [x["id"] for x in d["stoppet"]] == [k["id"]] and d["stoppet"][0]["status"] == "fjernet"
    assert b"ola@b.no" not in call(base + "/debatt.json")[1]
    angre = {"id": k["id"], "handling": "publiser", "tekst": "Kanskje litt frekt"}
    assert call(base + "/debatt", angre, X_Tingposten="admin")[0] == 200
    assert t.trad(SAK, None)["antall"] == 1 and json.loads(call(base + "/debatt.json")[1])["stoppet"] == []


def test_the_full_list_shows_every_comment_with_state_votes_and_verdict(oppsett):
    base, t, k, okt = oppsett
    ola = t.bruker(okt)
    k2 = t.ny_kommentar(ola, SAK, "Helt greit")  # the model is unsure about everything in this test
    t.redaktor_avgjor(k2["id"], "publiser", tekst="Helt greit")
    code, body = call(base + "/debatt-alle.json")
    d = json.loads(body)
    assert code == 200 and d["aktiv"] and [x["id"] for x in d["kommentarer"]] == [k2["id"], k["id"]]  # newest first
    assert d["antall"] == {"til_redaktor": 1, "publisert": 1, "laert": 0}
    assert list(d["regler"]) == list(REGLER) and d["regler"]["hets"] == "Hat og diskriminering"
    rad = d["kommentarer"][1]
    assert rad["navn"] == "Ola" and rad["status"] == "til_redaktor" and rad["opp"] == 0 and rad["laert"] is None
    assert rad["vurderinger"][0]["regel"] == "trakassering" and rad["lenke"].endswith(f"/bergen/sak/{SAK}/#k{k['id']}")
    assert b"ola@b.no" not in body
    d = json.loads(call(base + "/debatt-alle.json?status=publisert")[1])
    assert [x["id"] for x in d["kommentarer"]] == [k2["id"]]
    d = json.loads(call(base + "/debatt-alle.json?sok=frekt")[1])
    assert [x["id"] for x in d["kommentarer"]] == [k["id"]]
    assert json.loads(call(base + "/debatt-alle.json?status=tull")[1])["kommentarer"] != []  # unknown filter: everything


def test_not_ok_removes_the_comment_and_stores_it_as_an_example(oppsett):
    base, t, k, okt = oppsett
    assert call(base + "/debatt", {"id": k["id"], "handling": "laer"}, X_Tingposten="admin")[0] == 400  # a rule is needed
    assert call(base + "/debatt", {"id": k["id"], "handling": "laer", "regel": "tull"}, X_Tingposten="admin")[0] == 400
    code, body = call(base + "/debatt", {"id": k["id"], "handling": "laer", "regel": "hets", "tekst": "Kanskje litt frekt"},
                      X_Tingposten="admin")
    assert code == 200 and "eksempel" in json.loads(body)["melding"]
    d = json.loads(call(base + "/debatt-alle.json")[1])
    assert d["kommentarer"][0]["status"] == "fjernet" and d["kommentarer"][0]["regel"] == "hets"
    assert d["kommentarer"][0]["laert"] == "hets" and d["antall"]["laert"] == 1
    assert t.trad(SAK, None)["antall"] == 0
    eks = t.laeresett()
    assert eks == [{"id": f"k{k['id']}", "sak": SAK, "sak_tittel": "Bybanen", "sak_ingress": "", "forelder": None,
                    "tekst": "Kanskje litt frekt", "forslag": "hets", "merknad": eks[0]["merknad"]}]
    # The editor changes their mind: OK publishes it again and the example follows.
    code, _ = call(base + "/debatt", {"id": k["id"], "handling": "laer", "regel": "ok", "tekst": "Kanskje litt frekt"},
                   X_Tingposten="admin")
    assert code == 200 and t.trad(SAK, None)["antall"] == 1 and t.laeresett()[0]["forslag"] == "ok"
    # An edit since the panel showed the text: no blind publish.
    with t.db.tx() as c:
        c.execute("UPDATE kommentarer SET status = 'avvist' WHERE id = ?", (k["id"],))
    assert call(base + "/debatt", {"id": k["id"], "handling": "laer", "regel": "ok", "tekst": "noe annet"},
                X_Tingposten="admin")[0] == 409
    assert call(base + "/debatt", {"id": k["id"], "handling": "glem"}, X_Tingposten="admin")[0] == 200
    assert t.laeresett() == [] and json.loads(call(base + "/debatt-alle.json")[1])["antall"]["laert"] == 0


def test_examples_go_when_the_comment_or_the_account_is_deleted(oppsett):
    base, t, k, okt = oppsett
    ola = t.bruker(okt)
    k2 = t.ny_kommentar(ola, SAK, "Nummer to")
    t.laer(k["id"], "spam")
    t.laer(k2["id"], "ok")
    assert len(t.laeresett()) == 2
    t.slett(ola, k["id"])  # the author deletes one
    assert [e["id"] for e in t.laeresett()] == [f"k{k2['id']}"]
    with pytest.raises(tj.Feil):
        t.laer(k["id"], "ok")  # nothing left to learn from
    assert t.db.eksport(ola.id)["laeresett"] == [{"kommentar": k2["id"], "regel": "ok", "tid": t.laeresett()[0]["merknad"] and
                                                  t.db.en("SELECT tid FROM laeresett")["tid"]}]
    t.db.slett_bruker(ola.id)
    assert t.laeresett() == [] and t.db.en("SELECT COUNT(*) AS n FROM laeresett")["n"] == 0


def test_debatt_test_can_score_the_editors_examples(oppsett, tmp_path, monkeypatch):
    from tingposten.debatt import testsett
    base, t, k, okt = oppsett
    t.laer(k["id"], "trakassering")
    (tmp_path / "saker.json").write_text(json.dumps({SAK: {"tittel": "Bybanen", "ingress": "Linje 2."}}))
    sett, fasit = testsett.med_laerte([{"id": "t001", "tekst": "x", "forslag": "ok"}], {"t001": {"regel": "ok"}}, tmp_path)
    assert [i["id"] for i in sett] == ["t001", f"k{k['id']}"] and sett[1]["sak_ingress"] == "Linje 2."
    assert fasit[f"k{k['id']}"]["regel"] == "trakassering" and testsett.fasit_for(sett[1], fasit) == ("trakassering", True)
    assert testsett.med_laerte([], {}, tmp_path / "ingen") == ([], {})
