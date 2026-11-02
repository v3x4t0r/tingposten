"""Stortinget as a source: dates, fetching, documents and the model steps' hooks.

Fixtures are Stortinget's own answers for sak 200236 (Endringer i barnevernsloven,
2025-2026), trimmed: per seat only party and vote, no names."""

import json
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from tingposten import faktasjekker, kildetekst, klassifiserer, pdf, pipeline, skribent
from tingposten.llm import FakeLLM
from tingposten.stortinget import api, dokumenter, innhenter, regler

FIX = Path(__file__).parent / "fixtures" / "stortinget"
VOTERING_MED_RESULTAT = 28289


def _les(navn):
    return json.loads((FIX / navn).read_text())


class FakeClient:
    """The endpoints innhenter uses, answered from the fixtures."""

    def __init__(self, saker=None):
        self.antall = 0
        self.saker_liste = saker or []
        self.kall = []

    def innevaerende_sesjon(self):
        return "2026-2027"

    def saker(self, sesjon):
        self.kall.append(("saker", sesjon))
        return self.saker_liste

    def sak(self, sakid):
        self.kall.append(("sak", sakid))
        return _les("sak-200236.json")

    def voteringer(self, sakid):
        return _les("voteringer-200236.json")["sak_votering_liste"]

    def voteringsresultat(self, vid):
        return _les("voteringsresultat-28289.json")["voteringsresultat_liste"] if vid == VOTERING_MED_RESULTAT else []

    def voteringsforslag(self, vid):
        return _les("voteringsforslag-28289.json")["voteringsforslag_liste"] if vid == VOTERING_MED_RESULTAT else []

    def stortingsvedtak(self, sesjon):
        self.kall.append(("stortingsvedtak", sesjon))
        return _les("stortingsvedtak-2025-2026.json")["stortingsvedtak_liste"]


@pytest.fixture
def store(tmp_path):
    innhenter.hent(tmp_path, FakeClient(), saker=[200236])
    (tmp_path / "pub").mkdir()
    shutil.copy(FIX / "vedtak-202526-086.xml", tmp_path / "pub" / "vedtak-202526-086.xml")
    return tmp_path


@pytest.fixture
def sak(store):
    return dokumenter.saker(store)[0]


def test_tid_reads_both_date_formats():
    assert api.tid("/Date(1781183647493+0200)/").isoformat() == "2026-06-11T15:14:07.493000+02:00"
    assert api.tid("11.06.2026 00:00:00").date().isoformat() == "2026-06-11"
    assert api.tid("01.01.0001 00:00:00") is None
    assert api.tid(None) is None


def test_url_makes_links_https():
    assert api.url("//www.stortinget.no/no/Sak/?p=1") == "https://www.stortinget.no/no/Sak/?p=1"
    assert api.url("http://www.stortinget.no/inns/x.html") == "https://www.stortinget.no/inns/x.html"
    assert api.url("http://www.regjeringen.no/id/PRP") == "http://www.regjeringen.no/id/PRP"


def test_client_spaces_requests_per_host_and_sends_user_agent():
    class Resp:
        status_code = 200
        content = b"%PDF"

        def json(self):
            return {"innevaerende_sesjon": {"id": "2026-2027"}}

    class Session:
        def __init__(self):
            self.headers = {}
            self.urls = []

        def get(self, url, params=None, timeout=None):
            self.urls.append((url, params))
            return Resp()

    sleeps, now = [], [0.0]
    c = api.Client(session=Session(), sleep=lambda s: sleeps.append(s), clock=lambda: now[0],
                   delay_s={"data.stortinget.no": 3.0, "www.stortinget.no": 5.0})
    c.innevaerende_sesjon()
    c.bytes("https://www.stortinget.no/x.pdf")  # another host: no wait
    c.innevaerende_sesjon()
    assert sleeps == [3.0]
    assert c.session.urls[0] == ("https://data.stortinget.no/eksport/sesjoner", {"format": "json"})
    assert c.session.headers["User-Agent"].startswith("Tingposten/")
    assert c.antall == 3


def test_hent_stores_votes_without_names_and_logs_changes(store):
    lagret = innhenter.lagret(store, 200236)
    v = next(v for v in lagret["voteringer"] if v["votering_id"] == VOTERING_MED_RESULTAT)
    assert v["resultat"][0].keys() == {"parti", "votering"}
    assert "president" not in v and v["president_parti"]
    assert innhenter.utvalg(store) == [200236]
    typer = [json.loads(line)["type"] for line in (store / "endringer.jsonl").read_text().splitlines()]
    assert typer == ["ny", "vedtatt"]


def test_hent_skips_unchanged_saker(tmp_path):
    liste = [{"id": 200236, "status": 1, "sist_oppdatert_dato": "/Date(1)/"}]
    innhenter.hent(tmp_path, FakeClient(liste))
    andre = FakeClient(liste)
    teller = innhenter.hent(tmp_path, andre)
    assert ("sak", 200236) not in andre.kall
    assert teller["uendret"] == 1 and teller["hentet"] == 0


def test_sesjoner_fra():
    assert innhenter.sesjoner_fra("2026-2027", "2027-2028") == ["2026-2027", "2027-2028"]


def test_item_and_key(sak):
    assert sak["kilde"] == "stortinget" and sak["type"] == "lovsak" and sak["dokumentgruppe"] == "proposisjon"
    assert sak["moteDato"] == "2026-06-11"
    assert sak["referansenr"] == "Innst. 362 L (2025-2026)"
    assert sak["komite"] == "Familie- og kulturkomiteen"
    assert pdf.key(sak) == dokumenter.key(sak) == "2025-2026/200236"


def test_formal_saker_are_noise():
    assert dokumenter.er_stoy({"korttittel": "Valg av nytt medlem til Stortingets delegasjon", "dokumentgruppe": 4})
    assert dokumenter.er_stoy({"korttittel": "Noe", "dokumentgruppe": 8})
    assert dokumenter.er_stoy({"korttittel": "Redegjørelse av utviklingsministeren om nordisk samarbeid"})
    assert dokumenter.er_stoy({"korttittel": "Endringer i de faste komiteers sammensetning"})
    assert not dokumenter.er_stoy({"korttittel": "Endringer i barnevernsloven", "dokumentgruppe": 1})
    assert not dokumenter.er_stoy({"korttittel": "Hans Majestet Kongens tale til det 171. storting"})


def test_party_counts_match_the_totals():
    v = next(v for v in _les("voteringer-200236.json")["sak_votering_liste"]
             if v["votering_id"] == VOTERING_MED_RESULTAT)
    resultat = [{"parti": r["representant"]["parti"]["id"], "votering": r["votering"]}
                for r in _les("voteringsresultat-28289.json")["voteringsresultat_liste"]]
    p = dokumenter.partier(resultat)
    assert sum(p["for"].values()) == v["antall_for"] == 54
    assert sum(p["mot"].values()) == v["antall_mot"] == 47
    assert p["mot"]["Ap"] == 32 and "A" not in p["mot"]


def test_votering_pages(sak, store):
    tekst = dokumenter.tekst(sak, "votering", store)
    assert kildetekst.sider(tekst) == list(range(1, 11))
    assert "Forslag nr. 1 på vegne av FrP og H." in tekst
    assert "For: FrP 29, H 14, Sp 5, KrF 4, V 2." in tekst
    assert "innen første halvår 2027" in tekst
    # Votes without per-seat results fetched get no party lines.
    side1 = kildetekst.utdrag(tekst, [1])
    assert "For:" not in side1 and "10 for og 91 mot" in side1


def test_vedtak_pages_hold_the_adopted_text(sak, store):
    tekst = dokumenter.tekst(sak, "vedtak", store)
    assert tekst.startswith("--- side 1 ---\nStortingsvedtak nr. 960 (Anmodninger), 11.06.2026")
    assert "innen første halvår 2027" in tekst
    assert "Lovvedtak 86 (2025-2026)" in tekst and "§ 1-2 fjerde ledd skal lyde:" in tekst
    assert dokumenter.doc_url(sak, "vedtak", 1, store).startswith(
        "https://www.stortinget.no/no/Saker-og-publikasjoner/Vedtak/Vedtak/Sak/?did=40036865")


def test_dokument8_url():
    sak = {"sak_sesjon": "2025-2026", "publikasjon_referanse_liste": [
        {"eksport_id": "dok8-202526-226s", "lenke_tekst": "Dokument 8:226 S (2025-2026)"},
        {"eksport_id": "inns-202526-457s", "lenke_tekst": "Innst. 457 S (2025-2026)"}]}
    assert dokumenter.pdf_url(sak, "forslag") == (
        "https://www.stortinget.no/globalassets/pdf/representantforslag/2025-2026/dok8-202526-226s.pdf")
    assert dokumenter.pdf_url({"publikasjon_referanse_liste": []}, "forslag") is None


def test_innstilling_url(sak, store):
    assert dokumenter.doc_url(sak, "innstilling", 3, store) == (
        "https://www.stortinget.no/globalassets/pdf/innstillinger/stortinget/2025-2026/inns-202526-362l.pdf#page=3")


def test_kilder_and_schema(sak, store):
    kilder = kildetekst.kilder_for(sak, store)
    assert set(kilder) == {"vedtak", "votering", "innstilling", "forslag"}
    assert kilder["forslag"] == ""  # a law has no Dokument 8
    assert kildetekst.hovedkilde(kilder) == "vedtak"
    schema = kildetekst.med_dokumenter(skribent.CARD_SCHEMA, kilder)
    assert schema["properties"]["kilder"]["items"]["properties"]["dok"]["enum"] == list(regler.DOKUMENTER)


def test_long_innstilling_is_cut(sak, store, monkeypatch):
    lang = "\n".join(f"--- side {n} ---\ntekst {n}" for n in range(1, 61))
    monkeypatch.setattr(dokumenter, "tekst", lambda item, dok, d: lang if dok in ("innstilling", "vedtak") else "x")
    kilder = dokumenter.kilder_for(sak, store)
    assert kildetekst.sider(kilder["innstilling"]) == list(range(1, 21))
    assert kildetekst.sider(kilder["vedtak"]) == list(range(1, 16))
    (store / "innstillingsdigest" / "2025-2026").mkdir(parents=True)
    (store / "innstillingsdigest" / "2025-2026" / "200236.json").write_text(json.dumps({"sider": [{"side": 55}]}))
    assert kildetekst.sider(dokumenter.kilder_for(sak, store)["innstilling"]) == [55]


KORT = {"overskrift": "Barn kan holdes lenger på institusjon", "ingress": "I.", "hva_endres": "H.",
        "hvem_pavirkes": "", "hvorfor": "", "status": "Vedtatt.", "nar_og_hvor": "",
        "kilder": [{"felt": f, "dok": "vedtak", "side": 1} for f in ("ingress", "hva_endres", "status")],
        "merknad": "", "blocked": ""}


def test_writer_uses_stortinget_rules_and_documents(sak, store):
    llm = FakeLLM([dict(KORT)])
    assert skribent.skriv(sak, store, llm) == "skrevet"
    prompt = llm.prompts[0]
    assert "summarises what Stortinget\ndecides" in prompt and "Bergen kommune decides" not in prompt.split("EXAMPLE")[0]
    assert "<<<VEDTAK" in prompt or "VEDTAK" in prompt.split("THE SAK:")[1]
    assert "SAKSFRAMLEGG" not in prompt.split("THE SAK:")[1]
    kort = json.loads((store / "kort" / "2025-2026" / "200236.json").read_text())
    assert kort["meta"]["prompt_versjon"] == regler.SKRIBENT_VERSJON
    assert kort["nr"] == "Innst. 362 L (2025-2026)"
    assert not pipeline.eldre_regler(store / "kort" / "2025-2026" / "200236.json")


def test_headline_may_not_start_with_stortinget():
    problems = skribent.grenser({**KORT, "overskrift": "Stortinget vedtok endringer"}, regler.ORGAN_START)
    assert any("organet" in p for p in problems)
    assert not any("organet" in p for p in skribent.grenser({**KORT, "overskrift": "Stortinget vedtok"}))


def test_checker_and_classifier_use_stortinget_rules(sak, store):
    skribent.skriv(sak, store, FakeLLM([dict(KORT)]))
    sjekk = FakeLLM([{"setninger": [{"felt": f, "setning": KORT[f], "stottet": True, "kilde": None,
                                      "begrunnelse": ""} for f in ("overskrift", "ingress", "hva_endres", "status")],
                      "personvern": [], "blocked": ""}])
    assert faktasjekker.sjekk(sak, store, sjekk).startswith("sjekket")
    assert "what Stortinget decides" in sjekk.prompts[0]
    meta = json.loads((store / "sjekk" / "2025-2026" / "200236.json").read_text())["meta"]
    assert meta["prompt_versjon"] == regler.FAKTASJEKKER_VERSJON

    signal = {"verdi": 3, "begrunnelse": "x"}
    k = FakeLLM([{"angar": signal, "konkret": signal, "tema": "justis", "tema_begrunnelse": "", "blocked": ""}])
    assert klassifiserer.klassifiser(sak, store, k) == "klassifisert"
    assert "people in Norway" in k.prompts[0]
    assert klassifiserer.tema_for(sak) is regler.TEMA
    assert not klassifiserer.mangler_tema(sak, store)


def test_vedtaksdato_without_votes_uses_saksgang():
    lagret = {"sak": {"saksgang": {"saksgang_steg_liste": [{"saksgang_hendelse_liste": [
        {"dato": "01.01.0001 00:00:00"}, {"dato": "09.04.2026 00:00:00"}]}]}}, "voteringer": []}
    assert innhenter.vedtaksdato(lagret) == datetime(2026, 4, 9, tzinfo=api.NORSK_TID)


def test_kjor_writes_checks_and_edits_a_card(store):
    from tingposten.stortinget import pipeline as st_pipeline

    sjekk = {"setninger": [{"felt": f, "setning": KORT[f], "stottet": True, "kilde": None, "begrunnelse": ""}
                           for f in ("overskrift", "ingress", "hva_endres", "status")],
             "personvern": [], "blocked": ""}
    signal = {"verdi": 4, "begrunnelse": "x"}
    llms = {"les": FakeLLM([]), "skriv": FakeLLM([dict(KORT)]), "sjekk": FakeLLM([sjekk]),
            "klassifiser": FakeLLM([{"angar": signal, "konkret": signal, "tema": "justis",
                                     "tema_begrunnelse": "", "blocked": ""}]),
            "artikkel": FakeLLM([{"avsnitt": [], "blocked": ""}] * 2), "artikkelsjekk": FakeLLM([])}
    result = st_pipeline.kjor(store, llms=llms, parallel=1)
    assert result["skriv"]["antall"] == {"skrevet": 1}
    assert result["rediger"]["publiserbar"] == 1
    assert (store / "tekst" / "2025-2026" / "200236" / "vedtak.txt").exists()
    # A second run with the same documents does nothing new: no rewrite.
    again = st_pipeline.kjor(store, llms={k: FakeLLM([]) for k in llms}, parallel=1,
                             steg=("dokumenter", "les", "skriv", "sjekk", "klassifiser"))
    assert again["dokumenter"]["antall"] == {"uendret": 1}
    assert not (store / "tekst" / "2025-2026" / "200236" / "forslag.txt").exists()
    assert again["skriv"] == {} and again["sjekk"] == {} and again["klassifiser"] == {}


def test_score_reads_votes_and_signals(store, sak):
    from tingposten.stortinget import score as st_score

    r = st_score.rank([sak], store)[0]
    assert r["signaler"]["forslag_falt"] == 5
    assert r["signaler"]["type"] == "lovsak"
    assert 0 < r["score"] <= 100
    assert "lovendring" in r["grunner"]


def test_testkopi_builds_stortinget_privately_and_cannot_be_published(store, tmp_path, monkeypatch):
    from tingposten import bygg, config, publiserer

    monkeypatch.setattr(config, "STORTINGET_DATA_DIR", store)
    monkeypatch.setattr(config, "DEBATT", None)
    skribent.skriv(dokumenter.saker(store)[0], store, FakeLLM([dict(KORT)]))
    site = tmp_path / "site-test"
    stats = bygg.build(site, data_dir=tmp_path / "bergen", idag=date(2026, 6, 20),
                       med_stortinget=True, testkopi=True)
    assert stats["stortinget"]["saker"] == 1
    side = (site / "stortinget" / "sak" / "2025-2026" / "200236" / "index.html").read_text()
    assert "Stortingets egne dokumenter" in side and 'name="robots" content="noindex' in side
    assert "Bergen kommune" not in side.split("<footer")[0].split("<main")[1]
    assert (site / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    assert (site / "TESTKOPI").exists() and not (site / "sitemap.xml").exists()
    with pytest.raises(RuntimeError, match="testkopi"):
        publiserer.publish(site, repo_dir=tmp_path)
    (site / "TESTKOPI").unlink()
    with pytest.raises(RuntimeError, match="Stortinget"):
        publiserer.publish_statichost(site, repo_dir=tmp_path, apikey="x")


def test_normal_build_has_no_stortinget(store, tmp_path, monkeypatch):
    from tingposten import bygg, config

    monkeypatch.setattr(config, "STORTINGET_DATA_DIR", store)
    monkeypatch.setattr(config, "DEBATT", None)
    bygg.build(tmp_path / "site", data_dir=tmp_path / "bergen", idag=date(2026, 6, 20))
    assert not (tmp_path / "site" / "stortinget").exists()


def test_reader_picks_pages_of_long_innstilling_and_vedtak(sak, store, monkeypatch):
    from tingposten.stortinget import pipeline as st_pipeline

    lang = "\n".join(f"--- side {n} ---\ntekst {n}" for n in range(1, 81))
    monkeypatch.setattr(dokumenter, "tekst", lambda item, dok, d: lang if dok in ("innstilling", "vedtak") else "")
    llm = FakeLLM([{"sider": [{"side": n, "hva": "x"} for n in (2, 3, 999)]},
                   {"sider": [{"side": 50, "hva": "y"}]}] * 2)
    assert st_pipeline.les(sak, store, llm) == "lest: innstilling, vedtak"
    digest = json.loads(dokumenter.digest_fil(store, sak, "vedtak").read_text())
    assert [s["side"] for s in digest["sider"]] == [2, 3, 50]
    assert "vedtakene som betyr mest" in llm.prompts[2]


def test_long_vote_documents_get_short_proposal_texts(sak, store, monkeypatch):
    monkeypatch.setattr(dokumenter, "MAKS_VOTERING_TEGN", 1000)
    tekst = dokumenter.tekst(sak, "votering", store)
    assert "(forkortet; hele teksten står i vedtaket)" not in tekst  # 200236's proposal is short
    monkeypatch.setattr(dokumenter, "FORSLAG_KORT", 50)
    tekst = dokumenter.tekst(sak, "votering", store)
    assert "(forkortet; hele teksten står i vedtaket)" in tekst
    assert "For: FrP 29, H 14, Sp 5, KrF 4, V 2." in tekst
