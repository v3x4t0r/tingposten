import json

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from tingposten.debatt import moderering as m  # noqa: E402
from tingposten.debatt import server as srv  # noqa: E402
from tingposten.debatt import tjeneste as tj  # noqa: E402
from tingposten.debatt.db import Db  # noqa: E402
from tingposten.debatt.regler import REGLER  # noqa: E402

SAK = "571424/2026-09-23/266-26"
SIDE = "https://tingposten.no"


class Ok(m.Moderator):
    name = "ok"

    def _vurder(self, tekst, sak, forelder):
        regel = "personangrep" if "idiot" in tekst else "ok"
        return m.Vurdering(regel, {k: (0.99 if k == regel else 0.0) for k in REGLER}, "ok", 0)


@pytest.fixture
def klient(tmp_path):
    sendt = []
    t = tj.Tjeneste(Db(tmp_path / "d.db"), m.Kjede([Ok()], 0.85), {SAK: {"tittel": "Bybanen", "ingress": ""}},
                    lambda e, lenke: sendt.append(lenke), SIDE + "/", umiddelbar_moderering=True)
    with TestClient(srv.lag_app(t, [SIDE], start_ko=False)) as c:
        c.sendt = sendt
        yield c


def logg_inn(c, epost="ola@b.no", navn="Ola") -> dict:
    assert c.post("/api/innlogging", json={"epost": epost}).json() == {"sendt": True}
    token = c.sendt[-1].split("#t=")[1]
    svar = c.post("/api/innlogging/bekreft", json={"token": token}).json()
    h = {"Authorization": f"Bearer {svar['okt']}"}
    if navn:
        assert c.post("/api/meg", json={"navn": navn}, headers=h).json() == {"navn": navn}
    return h


def test_full_flow_login_comment_vote(klient, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, kari = logg_inn(klient), logg_inn(klient, "kari@b.no", "Kari")
    r = klient.post("/api/kommentar", json={"sak": SAK, "tekst": "Godt vedtak."}, headers=ola)
    assert r.status_code == 201 and r.json()["status"] == "publisert"
    kid = r.json()["id"]
    assert klient.post(f"/api/kommentar/{kid}/stem", json={"verdi": 1}, headers=kari).json()["poeng"] == 1
    tre = klient.get(f"/api/sak/{SAK}/kommentarer?sortering=topp").json()
    assert tre["antall"] == 1 and tre["kommentarer"][0]["navn"] == "Ola" and tre["kommentarer"][0]["min_stemme"] == 0
    assert klient.get(f"/api/sak/{SAK}/kommentarer", headers=kari).json()["kommentarer"][0]["min_stemme"] == 1
    stopp = klient.post("/api/kommentar", json={"sak": SAK, "tekst": "Ola er en idiot", "forelder": kid},
                        headers=kari).json()
    assert stopp["status"] == "avvist" and stopp["regel"]["navn"] == "Personangrep"
    assert klient.post(f"/api/kommentar/{stopp['id']}/manuell", headers=kari).json()["status"] == "til_redaktor"
    assert klient.get(f"/api/kommentar/{stopp['id']}").status_code == 404  # others can't see it
    eksport = klient.get("/api/meg/eksport", headers=kari).json()
    assert eksport["konto"]["epost"] == "kari@b.no" and eksport["stemmer"] == [{"kommentar": kid, "verdi": 1}]
    assert klient.delete("/api/meg", headers=kari).json() == {"slettet": True}
    assert klient.get("/api/meg", headers=kari).status_code == 401


def test_writing_needs_a_session(klient):
    assert klient.post("/api/kommentar", json={"sak": SAK, "tekst": "Hei"}).status_code == 401
    r = klient.post("/api/kommentar", json={"sak": SAK, "tekst": "Hei"}, headers={"Authorization": "Bearer feil"})
    assert r.status_code == 401 and r.json() == {"feil": "Du må logge inn."}


def test_json_only_and_body_cap(klient):
    assert klient.post("/api/innlogging", content=b"epost=a@b.no",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    stor = json.dumps({"epost": "a@b.no", "x": "y" * srv.MAKS_KROPP})
    assert klient.post("/api/innlogging", content=stor, headers={"Content-Type": "application/json"}).status_code == 413
    assert klient.post("/api/innlogging", content=b"[1]", headers={"Content-Type": "application/json"}).status_code == 400


def test_cors_allows_only_the_site(klient):
    ok = klient.options("/api/kommentar", headers={"Origin": SIDE, "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": "authorization,content-type"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == SIDE
    assert "access-control-allow-credentials" not in ok.headers  # bearer tokens, no cookies
    ond = klient.options("/api/kommentar", headers={"Origin": "https://ond.example",
                                                    "Access-Control-Request-Method": "POST"})
    assert ond.status_code == 400
    r = klient.get(f"/api/sak/{SAK}/kommentarer", headers={"Origin": "https://ond.example"})
    assert "access-control-allow-origin" not in r.headers


def test_security_headers_and_public_cache(klient):
    r = klient.get(f"/api/sak/{SAK}/kommentarer")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "public, max-age=15"
    assert klient.get("/api/sak/finnes/ikke/kommentarer").status_code == 404
    assert klient.get("/api/kommentar/abc").status_code == 404


def test_ip_limit_counts_only_writes():
    teller = srv.IpTeller(grense=2, vindu_s=60)
    assert [teller.for_mange("1.2.3.4") for _ in range(3)] == [False, False, True]
    assert teller.for_mange("5.6.7.8") is False


def test_forwarded_address_is_trusted_only_from_loopback():
    class R:
        def __init__(self, peer, fwd):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": fwd} if fwd else {}
    assert srv._ip(R("127.0.0.1", "9.9.9.9, 127.0.0.1")) == "9.9.9.9"
    assert srv._ip(R("10.0.0.5", "9.9.9.9")) == "10.0.0.5"


def test_sak_index_is_reread_when_the_build_changes_it(tmp_path):
    path = tmp_path / "saker.json"
    saker = srv.Saker(path)
    assert saker.get(SAK) is None
    path.write_text(json.dumps({SAK: {"tittel": "A"}}))
    assert saker.get(SAK)["tittel"] == "A"
    import os
    path.write_text(json.dumps({SAK: {"tittel": "B"}}))
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert saker.get(SAK)["tittel"] == "B"
