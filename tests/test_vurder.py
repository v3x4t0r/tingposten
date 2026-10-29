import json
import threading
import urllib.error
import urllib.request

import pytest

from tingposten import vurder

ITEM = {
    "id": "a",
    "utvalgId": "571424",
    "moteDato": "2026-09-23",
    "seq": 5,
    "kategori": "Saker til behandling",
    "tittel": "Gjennomføringsvedtak for Eksempelhallen",
    "referansenr": "238/26",
    "innstillingFilId": "111",
    "innstillingAlternativBeskrivelse": "Framstilling-x",
    "vedtakFilId": "222",
    "vedtakAlternativBeskrivelse": "Protokoll-x",
}
NOISE = {**ITEM, "id": "b", "seq": 1, "kategori": "Innkalling", "tittel": "Innkalling", "referansenr": "1/26"}
DRAFT = {
    "nr": "238/26",
    "overskrift": "Klarsignal til å bygge Eksempelhallen for 69 millioner",
    "hva_endres": "Bystyret ga endelig klarsignal. Prosjektet koster 69 millioner kroner.",
    "hvem_pavirkes": "",
    "hvorfor": "Byrådet viser til vedtatt plan.",
    "status": "Vedtatt enstemmig.",
    "nar_og_hvor": "",
    "kilder": [
        {"felt": "hva_endres", "dok": "protokoll", "side": 1},
        {"felt": "status", "dok": "protokoll", "side": 1},
        {"felt": "hvorfor", "dok": "saksframlegg", "side": 2},
    ],
    "merknad": "",
}


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "kort" / "571424" / "2026-09-23").mkdir(parents=True)
    (tmp_path / "kort" / "571424" / "2026-09-23" / "238-26.json").write_text(json.dumps(DRAFT))
    (tmp_path / "pdf" / "571424" / "2026-09-23").mkdir(parents=True)
    (tmp_path / "pdf" / "571424" / "2026-09-23" / "238-26_protokoll.pdf").write_bytes(b"%PDF-1.4 test")
    (tmp_path / "secret.txt").write_text("nope")
    return tmp_path


def test_load_cards_joins_draft_and_skips_noise(data_dir):
    cards = vurder.load_cards([ITEM, NOISE], "2026-09-23", data_dir)
    assert [c["nr"] for c in cards] == ["238/26"]
    c = cards[0]
    assert c["overskrift"].startswith("Klarsignal")
    assert [f["label"] for f in c["felt"]] == ["Hva endres", "Hvorfor", "Status"]  # empty fields dropped
    assert c["felt"][0]["kilder"][0]["href"] == "/pdf/571424/2026-09-23/238-26_protokoll.pdf#page=1"
    assert c["mangler"] is False and c["sjekket"] is False


def test_missing_draft_is_flagged(data_dir):
    other = {**ITEM, "id": "c", "referansenr": "239/26"}
    cards = vurder.load_cards([other], "2026-09-23", data_dir)
    assert cards[0]["mangler"] is True and cards[0]["felt"] == []


def test_fact_check_marks_unsupported_sentence(data_dir):
    (data_dir / "sjekk" / "571424" / "2026-09-23").mkdir(parents=True)
    check = {"nr": "238/26", "setninger": [
        {"felt": "hva_endres", "setning": "Bystyret ga endelig klarsignal.", "stottet": True, "begrunnelse": ""},
        {"felt": "hva_endres", "setning": "Prosjektet koster 69 millioner kroner.", "stottet": False,
         "begrunnelse": "Kilden sier 69 mill. som investeringsramme"},
    ], "personvern": [], "blocked": ""}
    (data_dir / "sjekk" / "571424" / "2026-09-23" / "238-26.json").write_text(json.dumps(check))
    c = vurder.load_cards([ITEM], "2026-09-23", data_dir)[0]
    assert c["sjekket"] and c["uten_stotte"] == 1
    parts = c["felt"][0]["deler"]
    assert "".join(p["tekst"] for p in parts) == DRAFT["hva_endres"]  # nothing lost or duplicated
    assert [p["stottet"] for p in parts] == [True, None, False]


def test_clean_validates():
    known = {"238/26"}
    assert vurder.clean({"nr": "238/26", "vurdering": "forside", "merkbar": 4, "kommentar": " ok "}, known) == (
        "238/26", {"vurdering": "forside", "merkbar": 4, "kommentar": "ok"})
    for bad in (
        {"nr": "999/26"},
        {"nr": "238/26", "vurdering": "kanskje"},
        {"nr": "238/26", "merkbar": 6},
        {"nr": "238/26", "merkbar": True},
        {"nr": "238/26", "kommentar": "x" * 2001},
    ):
        with pytest.raises(ValueError):
            vurder.clean(bad, known)


def test_page_cannot_be_broken_out_of_script():
    html = vurder.page("Test", [{"nr": "1", "tittel": "</script><script>alert(1)</script>"}], {})
    assert "</script><script>alert(1)" not in html
    assert html.count("</script>") == 1


def test_nb_date():
    assert vurder.nb_date("2026-09-23") == "23. september 2026"


@pytest.fixture
def server(data_dir):
    srv = vurder.make_server("571424", "2026-09-23", [ITEM, NOISE], port=0, data_dir=data_dir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", data_dir
    srv.shutdown()


def post(base, body):
    req = urllib.request.Request(base + "/lagre", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req)


def test_server_round_trip(server):
    base, data_dir = server
    assert "Klarsignal" in urllib.request.urlopen(base + "/").read().decode()
    assert post(base, {"nr": "238/26", "vurdering": "stoy", "merkbar": 1, "kommentar": "intern"}).status == 200
    saved = json.loads((data_dir / "vurdering" / "571424-2026-09-23.json").read_text())
    assert saved["238/26"]["vurdering"] == "stoy" and saved["238/26"]["tid"]
    # the saved rating is embedded on the next load
    assert '"vurdering": "stoy"' in urllib.request.urlopen(base + "/").read().decode()


def test_server_rejects_bad_rating(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base, {"nr": "238/26", "vurdering": "kanskje"})
    assert e.value.code == 400


def test_server_serves_local_pdf_but_not_other_files(server):
    base, _ = server
    assert urllib.request.urlopen(base + "/pdf/571424/2026-09-23/238-26_protokoll.pdf").read().startswith(b"%PDF")
    for path in ("/pdf/../secret.txt", "/pdf/%2e%2e/secret.txt", "/pdf/571424/2026-09-23/missing.pdf"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + path)
        assert e.value.code == 404
