import base64
import fcntl
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from tingposten import admin, pipeline

IDAG = date(2026, 10, 30)
ROOT = Path(__file__).resolve().parent.parent


def sak(uid, dato, nr, vedtak=True, **extra):
    return {
        "id": f"{uid}-{dato}-{nr}", "utvalgId": uid, "moteDato": dato, "seq": int(nr.split("/")[0]),
        "kategori": "Saker til behandling", "tittel": f"Sak {nr}", "referansenr": nr,
        "innstillingFilId": "1", "vedtakFilId": "2" if vedtak else None, **extra,
    }


ITEMS = {
    "571424": [
        sak("571424", "2026-09-23", "10/26"),                    # ferdig: card publishable
        sak("571424", "2026-09-23", "11/26"),                    # ferdig: card rejected
        sak("571424", "2026-09-23", "3/26", vedtak=False, kategori="Interpellasjon"),  # never a protokoll
        sak("571424", "2026-10-27", "20/26", vedtak=False),      # venter
        sak("571424", "2026-10-27", "21/26", vedtak=False),
        sak("571424", "2026-08-20", "5/26", vedtak=False),       # mangler: long past the usual time
    ],
    "900102": [
        sak("900102", "2026-10-29", "30/26"),                    # klar: PDFs not downloaded
        sak("900102", "2026-11-05", "31/26", vedtak=False),      # kommende
        sak("900102", "2026-08-06", "6/26", vedtak=False,        # no public saker: left out
            tittel="Rettstvist - Unntatt offentlighet - Offl § 18"),
    ],
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data" / "bergen"
    for uid, items in ITEMS.items():
        (d / "agendapunkter").mkdir(parents=True, exist_ok=True)
        (d / "agendapunkter" / f"{uid}.json").write_text(json.dumps({i["id"]: i for i in items}))
    for nr in ("10-26", "11-26"):
        for kind in ("protokoll", "saksframlegg"):
            p = d / "pdf" / "571424" / "2026-09-23" / f"{nr}_{kind}.pdf"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 test")
    red = d / "redigert" / "571424" / "2026-09-23"
    red.mkdir(parents=True)
    (red / "10-26.json").write_text(json.dumps({"avvist": False}))
    (red / "11-26.json").write_text(json.dumps({"avvist": True}))
    # Every protokoll on disk covers its meeting; bystyret's was made two days after.
    monkeypatch.setattr(pipeline, "protokoll_klar", lambda item, data_dir=None: item["moteDato"] == "2026-09-23")
    monkeypatch.setattr(admin, "_laget", lambda path: date(2026, 9, 25))
    return d


def test_wording():
    assert admin._etter(0) == "samme dag som møtet"
    assert admin._etter(1) == "1 dag etter møtet"
    assert admin._etter(2) == "2 dager etter møtet"
    assert admin._etter(1, 5) == "1–5 dager etter møtet"
    assert admin._etter(0, 3) == "fra samme dag til 3 dager etter møtet"


def test_far_protokoll():
    assert admin.far_protokoll(sak("571424", "2026-09-23", "1/26"))
    assert not admin.far_protokoll(sak("571424", "2026-09-23", "1/26", kategori="Interpellasjon"))
    assert not admin.far_protokoll(sak("900102", "2026-09-23", "1/26", tittel="Rettstvist - Unntatt offentlighet"))
    assert not admin.far_protokoll(sak("571424", "2026-09-23", "1/26", tittel="Godkjenning av protokoll"))


def test_tempo_from_protokoll_dates(data_dir):
    assert admin.protokoll_tempo(data_dir) == {"571424": {"min": 2, "maks": 2, "typisk": 2, "moter": 1}}


def test_meetings_and_headline(data_dir):
    moter = admin.moter(data_dir, IDAG)
    by = {(m["organ"], m["dato"]): m for m in moter}
    assert ("Byrådet", "2026-08-06") not in by  # only a sak exempt from public access
    ferdig = by[("Bystyret", "2026-09-23")]  # the interpellasjon doesn't count
    assert (ferdig["status"], ferdig["saker"], ferdig["kort"], ferdig["avvist"]) == ("ferdig", 2, 1, 1)
    venter = by[("Bystyret", "2026-10-27")]
    assert venter["status"] == "venter"
    assert venter["merknad"] == ("Protokollen er ikke lagt ut ennå. "
                                 "Det har gått lenger enn vanlig (2 dager etter møtet).")
    assert by[("Bystyret", "2026-08-20")]["status"] == "mangler"
    klar = by[("Byrådet", "2026-10-29")]
    assert klar["status"] == "klar" and klar["pdf"] == 2
    assert by[("Byrådet", "2026-11-05")]["status"] == "kommende"
    assert [m["dato"] for m in moter] == sorted((m["dato"] for m in moter), reverse=True)

    assert admin.neste(moter)["type"] == "klar"
    uten_klar = [m for m in moter if m["status"] != "klar"]
    assert admin.neste(uten_klar)["tittel"] == "Bystyret tirsdag 27. oktober venter på protokoll"
    bare_kommende = [m for m in moter if m["status"] in ("kommende", "ferdig", "mangler")]
    assert admin.neste(bare_kommende)["tittel"] == "Neste møte: Byrådet torsdag 5. november"


def test_expected_date():
    tempo = {"min": 1, "maks": 5, "typisk": 2}
    moteDag = date(2026, 10, 27)
    assert admin._forventet(moteDag, tempo, date(2026, 10, 28)).endswith("ventet rundt torsdag 29. oktober.")
    assert admin._forventet(moteDag, tempo, date(2026, 10, 29)).endswith("ventet i dag.")
    assert admin._forventet(moteDag, tempo, date(2026, 10, 30)).endswith("ventet senest søndag 1. november.")
    assert admin._forventet(moteDag, tempo, date(2026, 11, 3)).startswith("Det har gått lenger enn vanlig")
    assert admin._forventet(moteDag, None, date(2026, 10, 30)) == ""


def test_last_run_from_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-10-29.log").write_text("2026-10-29T06:00:00+01:00 start\n2026-10-29T06:20:00+01:00 done\n")
    (logs / "2026-10-30.log").write_text(
        "2026-10-30T06:00:00+01:00 start\n2026-10-30T06:30:00+01:00 done\n"
        "2026-10-30T14:00:00+01:00 start (manuell)\n2026-10-30T14:00:00+01:00 == hent\n"
        "[1/17] 571424/2026-10-27/330-26_protokoll.pdf\n2026-10-30T14:03:00+01:00 == kjor\n")
    (logs / "admin.log").write_text("not a run log\n")
    siste = admin.siste_kjoring(logs)
    assert siste["modus"] == "manuell" and siste["steg"] == "kjor" and not siste["ferdig"]
    assert siste["startet"] == "2026-10-30T14:00:00+01:00"
    assert siste["logg"][0].endswith("start (manuell)")
    assert admin.siste_kjoring(tmp_path / "ingen") is None


def test_lock_detection(tmp_path):
    lock = tmp_path / "run-daily.lock"
    assert not admin.laast(lock)  # no file: no run
    lock.write_text("")
    assert not admin.laast(lock)
    with lock.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert admin.laast(lock)


@pytest.mark.parametrize("host,ok", [
    ("192.168.8.20:8787", True), ("[::1]:8787", True), ("raspberrypi:8787", True),
    ("pi.local:8787", True), ("pi.home.arpa", True), ("localhost:8787", True),
    ("evil.example:8787", False), ("tingposten.no", False), ("", False),
])
def test_local_host_names(host, ok):
    assert admin.lokal_vert(host) is ok


def test_needs_password_off_loopback():
    with pytest.raises(ValueError):
        admin.make_server("0.0.0.0", 0)


@pytest.fixture
def server(data_dir, tmp_path):
    started = []

    def kommando(modus):
        started.append(modus)
        return [sys.executable, "-c", "import time; time.sleep(1.5)"]

    kjorer = admin.Kjorer(root=tmp_path, data_dir=data_dir, kommando=kommando)
    srv = admin.make_server("127.0.0.1", 0, passord="hemmelig", kjorer=kjorer, site_dir=tmp_path / "site",
                            idag=IDAG)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", started, data_dir
    srv.shutdown()


AUTH = "Basic " + base64.b64encode(b"admin:hemmelig").decode()


def call(url, data=None, **headers):
    headers = {"Authorization": AUTH, **headers}
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={k.replace("_", "-"): v for k, v in headers.items() if v is not None})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_password_and_host(server):
    base, _, _ = server
    assert call(base + "/", Authorization=None)[0] == 401
    assert call(base + "/", Authorization="Basic " + base64.b64encode(b"admin:feil").decode())[0] == 401
    code, body = call(base + "/")
    assert code == 200 and b"Kontrollpanel" in body
    assert call(base + "/", Host="evil.example")[0] == 403


def test_status_json(server):
    base, _, _ = server
    code, body = call(base + "/status.json")
    s = json.loads(body)
    assert code == 200 and s["neste"]["type"] == "klar" and not s["kjoring"]["pagar"]
    assert s["gjenstar"]["pdf"] == 2


def test_start_needs_header_and_same_origin(server):
    base, started, _ = server
    assert call(base + "/start", {"modus": "manuell"})[0] == 403
    assert call(base + "/start", {"modus": "manuell"}, X_Tingposten="admin", Origin="http://evil.example")[0] == 403
    assert call(base + "/start", {"modus": "slett"}, X_Tingposten="admin")[0] == 400
    assert started == []


def test_start_one_run_at_a_time(server):
    base, started, _ = server
    origin = base  # same host as the panel
    assert call(base + "/start", {"modus": "manuell"}, X_Tingposten="admin", Origin=origin)[0] == 202
    assert call(base + "/start", {"modus": "manuell"}, X_Tingposten="admin")[0] == 409
    assert json.loads(call(base + "/status.json")[1])["kjoring"]["pagar"]
    assert started == ["manuell"]


def test_check_waits_after_a_fresh_fetch(server):
    base, started, _ = server  # the fixture's agenda files were written just now
    code, body = call(base + "/start", {"modus": "sjekk"}, X_Tingposten="admin")
    assert code == 429 and "Du kan sjekke igjen kl." in json.loads(body)["melding"]
    assert started == []


def test_static_files_only(server):
    base, _, _ = server
    assert call(base + "/static/fonts.css")[0] == 200
    for path in ("/static/count.js", "/static/../admin.py", "/static/%2e%2e/admin.py", "/static/nope.css", "/x"):
        assert call(base + path)[0] == 404


def test_run_script_modes():
    script = ROOT / "deploy" / "run-daily.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    r = subprocess.run(["bash", str(script), "--slett-alt"], capture_output=True, text=True)
    assert r.returncode == 64 and "usage" in r.stderr


def test_missing_pdfs(data_dir):
    items = pipeline.saker(data_dir, fra="2026-07-30")
    assert {t["filename"] for t in pipeline.pdf_som_mangler(items, data_dir)} == {
        "900102/2026-10-29/30-26_protokoll.pdf", "900102/2026-10-29/30-26_saksframlegg.pdf"}
