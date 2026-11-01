"""Debattinnlegg from the form: submission, the AI check, the editor's panel, the writer's approval."""
import sys

import pytest

from tingposten import admin, config, innlegg, llm
from tingposten.debatt import innleggsjekk, innsendt
from tingposten.debatt.tjeneste import Feil

from test_debatt_tjeneste import Alltid, lag, logg_inn

TEKST = ("Bergen trenger flere informasjonspunkt, og folk må vite hvor de er før en krise. "
         "Byrådet bør lage en plan for å nå eldre uten mobil.\n\nDet haster før vinteren.")
INNLEGG = {"navn": "Kari Nordmann", "om": "lærer", "tittel": "Folk må vite hvor punktene er", "tekst": TEKST}
SORTER_BORT = {"utfall": "sorter_bort", "kategori": "spam", "sammendrag": "Reklame for kasino.", "grunn": "Reklame.", "merknader": []}
TIL_RED = {"utfall": "til_redaktor", "kategori": "debattinnlegg", "sammendrag": "Vil ha bedre beredskap.",
           "grunn": "Et vanlig innlegg.", "merknader": ["Påstand om eldre uten mobil bør sjekkes"]}


@pytest.fixture
def t(tmp_path):
    return lag(tmp_path, Alltid())


def test_submission_needs_login_full_name_and_a_real_text(t):
    with pytest.raises(Feil) as e:
        innsendt.send_inn(t.db, None, INNLEGG)
    assert e.value.kode == 401
    kari, _ = logg_inn(t, navn=None)
    for feil, endring in (("fullt navn", {"navn": "Kari"}), ("for kort", {"tekst": "Kort."}),
                          ("for lang", {"tekst": "x" * 6001})):
        with pytest.raises(Feil, match=feil):
            innsendt.send_inn(t.db, kari, {**INNLEGG, **endring})
    assert innsendt.send_inn(t.db, kari, INNLEGG)["mottatt"]
    innsendt.send_inn(t.db, kari, INNLEGG)
    with pytest.raises(Feil, match="2 innlegg siste døgn"):
        innsendt.send_inn(t.db, kari, INNLEGG)
    assert [r["navn"] for r in innsendt.til_vurdering(t.db)] == ["Kari Nordmann"] * 2


def test_ai_check_sorts_away_rubbish_and_never_judges_opinions(t):
    kari, _ = logg_inn(t, navn=None)
    innsendt.send_inn(t.db, kari, INNLEGG)
    modell = llm.FakeLLM([TIL_RED])
    svar = innleggsjekk.vurder(modell, innsendt.til_vurdering(t.db)[0])
    assert svar["utfall"] == "til_redaktor"
    prompt = modell.prompts[0]
    assert "Du vurderer aldri meningene" in prompt and "<<<INNLEGG" in prompt and "Kari Nordmann" in prompt


def _panel(t, tmp_path, svar, sendt):
    kommando = lambda modus: [sys.executable, "-c", "pass"]  # noqa: E731
    kjorer = admin.Kjorer(root=tmp_path, data_dir=tmp_path / "data", kommando=kommando)
    ko = admin.DebattKo(tmp_path / "d.db")
    return admin.InnleggKo(ko, kjorer, modell=llm.FakeLLM(svar), sender=(sendt.append, "tingposten@freetime.no"),
                           data_dir=tmp_path / "data")


def test_panel_checks_publishes_and_mails_the_writer(t, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SITE_URL", "https://tingposten.no/")
    kari, _ = logg_inn(t, "kari@example.no", navn=None)
    innsendt.send_inn(t.db, kari, INNLEGG)
    innsendt.send_inn(t.db, kari, {**INNLEGG, "tittel": "Billig kasino her", "tekst": "Kjøp nå! " * 30})
    sendt = []
    panel = _panel(t, tmp_path, [TIL_RED, SORTER_BORT], sendt)
    assert panel.sjekk() == 2
    status = panel.status()
    assert [i["tittel"] for i in status["ko"]] == ["Folk må vite hvor punktene er"]
    assert status["ko"][0]["ki"]["merknader"] == ["Påstand om eldre uten mobil bør sjekkes"]
    assert [i["tittel"] for i in status["sortert_bort"]] == ["Billig kasino her"]
    assert "epost" not in str(status) and "kari@example.no" not in str(status)   # the panel never shows the address
    iid = status["ko"][0]["id"]
    # a changed text is never published blind
    code, svar = panel.handle({"id": iid, "handling": "publiser", "tittel": "Annen tittel", "tekst": TEKST})
    assert code == 409
    code, svar = panel.handle({"id": iid, "handling": "publiser", "tittel": INNLEGG["tittel"], "tekst": innsendt.hent(t.db, iid)["tekst"]})
    assert code == 200 and "Publisert som" in svar["melding"]
    publisert = innlegg.last(tmp_path / "data")
    assert [(i["tittel"], i["forfatter"], i["om"]) for i in publisert] == [(INNLEGG["tittel"], "Kari Nordmann", "lærer")]
    assert innsendt.hent(t.db, iid)["status"] == "publisert"
    assert sendt[-1]["To"] == "kari@example.no" and sendt[-1]["Subject"].startswith("Publisert:")
    # rubbish can be brought back
    bort = status["sortert_bort"][0]["id"]
    assert panel.handle({"id": bort, "handling": "hent_tilbake"})[0] == 200
    assert innsendt.hent(t.db, bort)["status"] == "til_redaktor"


def test_editor_edit_goes_to_the_writer_for_approval(t, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SITE_URL", "https://tingposten.no/")
    kari, _ = logg_inn(t, "kari@example.no", navn=None)
    innsendt.send_inn(t.db, kari, INNLEGG)
    sendt = []
    panel = _panel(t, tmp_path, [TIL_RED], sendt)
    panel.sjekk()
    iid = panel.status()["ko"][0]["id"]
    ny_tekst = TEKST.replace("Det haster før vinteren.", "Det haster.")
    code, _ = panel.handle({"id": iid, "handling": "send", "tittel": "Folk må vite hvor de er", "tekst": ny_tekst})
    assert code == 200
    mail = sendt[-1]
    assert mail["To"] == "kari@example.no" and "Det haster." in mail.get_content()
    token = mail.get_content().split("/bergen/debatt/godkjenn/#t=")[1].split()[0]
    vis = innsendt.for_godkjenning(t.db, token)
    assert (vis["tittel"], vis["tekst"], vis["godkjent"]) == ("Folk må vite hvor de er", ny_tekst, False)
    innsendt.godkjenn(t.db, token)
    rad = panel.status()["ko"][0]
    assert rad["status"] == "godkjent"
    # publishing uses the approved version
    code, _ = panel.handle({"id": iid, "handling": "publiser", "tittel": "Folk må vite hvor de er", "tekst": ny_tekst})
    assert code == 200 and innlegg.last(tmp_path / "data")[0]["avsnitt"][-1] == "Det haster."
    with pytest.raises(Feil):
        innsendt.for_godkjenning(t.db, token)   # the link is spent once published


def test_failed_ai_check_reaches_the_editor_after_three_tries(t, tmp_path):
    kari, _ = logg_inn(t, navn=None)
    innsendt.send_inn(t.db, kari, INNLEGG)
    panel = _panel(t, tmp_path, [llm.LLMError("ute av tokens")] * 6, [])
    for _ in range(3):
        panel.sjekk()
    rad = panel.status()["ko"][0]
    assert rad["status"] == "til_redaktor" and "ute av tokens" in rad["ki"]["feil"]


def test_manual_innlegg_from_email_and_account_deletion(t, tmp_path):
    panel = _panel(t, tmp_path, [], [])
    code, svar = panel.handle({"handling": "ny", "tittel": "Fra e-post", "forfatter": "Ola Nordmann", "tekst": TEKST})
    assert code == 200 and innlegg.last(tmp_path / "data")[0]["tittel"] == "Fra e-post"
    assert panel.handle({"handling": "ny", "tittel": "X", "forfatter": "Ola", "tekst": TEKST})[0] == 400
    kari, _ = logg_inn(t, navn=None)
    innsendt.send_inn(t.db, kari, INNLEGG)
    assert t.db.eksport(kari.id)["debattinnlegg"][0]["tittel"] == INNLEGG["tittel"]
    t.db.slett_bruker(kari.id)
    assert t.db.les("SELECT * FROM innsendt") == []
