import json

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from tingposten.debatt import moderering as m  # noqa: E402
from tingposten.debatt import server as srv  # noqa: E402
from tingposten.debatt import tjeneste as tj  # noqa: E402
from tingposten.debatt.db import Db  # noqa: E402
from tingposten.debatt.regler import QUIZ, REGLER  # noqa: E402

SAK = "571424/2026-09-23/266-26"
SIDE = "https://tingposten.no"


class Ok(m.Moderator):
    name = "ok"

    def _vurder(self, tekst, sak, forelder):
        regel = "trakassering" if "idiot" in tekst else "ok"
        return m.Vurdering(regel, {k: (0.99 if k == regel else 0.0) for k in REGLER}, "ok", 0)


@pytest.fixture
def klient(tmp_path):
    sendt = []
    t = tj.Tjeneste(Db(tmp_path / "d.db"), m.Kjede([Ok()], m.Grenser.jevn(0.85)), {SAK: {"tittel": "Bybanen", "ingress": ""}},
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
        svar = {q["id"]: q["lov"] for q in QUIZ}
        assert c.post("/api/quiz", json={"svar": svar}, headers=h).json()["bestatt"] is True
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
    assert stopp["status"] == "avvist" and stopp["regel"]["navn"] == "Trakassering"
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


def test_ip_counter_is_bounded():
    teller = srv.IpTeller(grense=1, vindu_s=60, maks=3)
    for ip in ("a", "b", "c", "d"):
        teller.for_mange(ip)
    assert list(teller._tider) == ["b", "c", "d"]  # the least recently seen went first


def test_login_mail_has_its_own_tighter_ip_limit(klient):
    koder = [klient.post("/api/innlogging", json={"epost": f"l{i}@b.no"}).status_code for i in range(7)]
    assert koder[: srv.IP_GRENSE_LENKER] == [200] * srv.IP_GRENSE_LENKER and koder[-1] == 429


def test_forwarded_address_is_trusted_only_from_loopback():
    class R:
        def __init__(self, peer, fwd):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": fwd} if fwd else {}
    # The proxy appends the real address last; anything before it came from the client.
    assert srv._ip(R("127.0.0.1", "6.6.6.6, 9.9.9.9")) == "9.9.9.9"
    assert srv._ip(R("10.0.0.5", "9.9.9.9")) == "10.0.0.5"
    assert srv._ip(R("127.0.0.1", "2001:db8:1:2:aaaa::1")) == srv._ip(R("127.0.0.1", "2001:db8:1:2:bbbb::9")) \
        == "2001:db8:1:2::/64"
    assert srv._ip(R("::ffff:9.9.9.9", None)) == "9.9.9.9"


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


def test_polling_a_waiting_comment_is_not_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "IP_GRENSE", 4)
    sendt = []
    t = tj.Tjeneste(Db(tmp_path / "d.db"), m.Kjede([Ok()], m.Grenser.jevn(0.85)), {SAK: {"tittel": "Bybanen", "ingress": ""}},
                    lambda e, lenke: sendt.append(lenke), SIDE + "/", umiddelbar_moderering=True)
    with TestClient(srv.lag_app(t, [SIDE], start_ko=False)) as c:
        c.sendt = sendt
        ola = logg_inn(c)  # 4 writes: link, confirm, name, quiz
        assert c.post("/api/kommentar", json={"sak": SAK, "tekst": "Hei"}, headers=ola).status_code == 429
        for _ in range(10):
            assert c.get(f"/api/sak/{SAK}/kommentarer", headers=ola).status_code == 200
            assert c.get("/api/meg", headers=ola).status_code == 200


def test_quiz_over_http(klient):
    sporsmal = klient.get("/api/quiz").json()["sporsmal"]
    assert len(sporsmal) == 5 and "lov" not in sporsmal[0]
    h = logg_inn(klient, navn=None)
    assert klient.post("/api/meg", json={"navn": "Kari"}, headers=h).status_code == 200
    assert klient.get("/api/meg", headers=h).json()["quiz"] is False
    assert klient.post("/api/kommentar", json={"sak": SAK, "tekst": "Hei"}, headers=h).status_code == 403
    alle_lov = {q["id"]: True for q in sporsmal}
    r = klient.post("/api/quiz", json={"svar": alle_lov}, headers=h).json()
    assert r["bestatt"] is False and len(r["svar"]) == 5
    assert klient.post("/api/quiz", json={"svar": alle_lov}).status_code == 401


def test_login_link_comes_back_only_in_local_test_mode(klient, tmp_path):
    assert "lenke" not in klient.post("/api/innlogging", json={"epost": "a@b.no"}).json()
    t = tj.Tjeneste(Db(tmp_path / "l.db"), m.Kjede([Ok()], m.Grenser.jevn(0.85)), {SAK: {"tittel": "B"}},
                    lambda e, lenke: None, SIDE + "/", vis_lenke=True)
    with TestClient(srv.lag_app(t, [SIDE], start_ko=False)) as c:
        assert c.post("/api/innlogging", json={"epost": "a@b.no"}).json()["lenke"].startswith(SIDE + "/bergen/logg-inn/#t=")


def test_the_real_server_never_shows_login_links(monkeypatch, tmp_path):
    from tingposten import config
    monkeypatch.setattr(config, "DEBATT_DIR", tmp_path)
    monkeypatch.setenv("DEBATT_EPOST", "konsoll")
    app = srv.app_fra_config()
    with TestClient(app) as c:
        assert "lenke" not in c.post("/api/innlogging", json={"epost": "a@b.no"}).json()
