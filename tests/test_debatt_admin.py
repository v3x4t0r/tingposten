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
    assert ko.status() == {"aktiv": False, "ko": []}
    assert ko.handle({"id": 1, "handling": "publiser"})[0] == 400
    assert not (tmp_path / "finnes-ikke.db").exists()
