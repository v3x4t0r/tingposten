import pytest

from tingposten.debatt import moderering as m
from tingposten.debatt import tjeneste as tj
from tingposten.debatt.db import Db
from tingposten.debatt.regler import REGLER

SAK = "571424/2026-09-23/266-26"
SAKER = {SAK: {"tittel": "Bybanen til Åsane", "ingress": "Bystyret vedtok traseen."}}


class Alltid(m.Moderator):
    """Gives the same verdict to everything; `regler` maps a text fragment to a rule."""

    name = "test"

    def __init__(self, regler=None, p=0.95):
        self.regler, self.p, self.sett = regler or {}, p, []

    def _vurder(self, tekst, sak, forelder):
        self.sett.append((tekst, forelder))
        regel = next((r for frag, r in self.regler.items() if frag in tekst), "ok")
        return m.Vurdering(regel, {k: (self.p if k == regel else 0.0) for k in REGLER}, self.name, 0)


def lag(tmp_path, *ledd, terskel=0.85):
    sendt = []
    svc = tj.Tjeneste(Db(tmp_path / "d.db"), m.Kjede(list(ledd), terskel), SAKER,
                      lambda epost, lenke: sendt.append((epost, lenke)), "https://tingposten.no/",
                      umiddelbar_moderering=True)
    svc.sendt = sendt
    return svc


@pytest.fixture
def t(tmp_path):
    return lag(tmp_path, Alltid({"idiot": "personangrep"}))


def logg_inn(t, epost="Ola@Example.no", navn="Ola"):
    t.be_om_lenke(epost, "https://tingposten.no/bergen/sak/x/")
    token = t.sendt[-1][1].split("#t=")[1]
    svar = t.bekreft(token)
    b = t.bruker(svar["okt"])
    if navn:
        t.sett_navn(b, navn)
        b = t.bruker(svar["okt"])
    return b, svar


def test_login_link_is_single_use_and_sessions_are_hashed(t):
    b, svar = logg_inn(t)
    epost, lenke = t.sendt[0]
    assert epost == "ola@example.no" and lenke.startswith("https://tingposten.no/bergen/logg-inn/#t=")
    assert svar["tilbake"] == "https://tingposten.no/bergen/sak/x/" and b.navn == "Ola"
    with pytest.raises(tj.Feil):
        t.bekreft(lenke.split("#t=")[1])  # used
    token = lenke.split("#t=")[1]
    assert not t.db.les("SELECT 1 FROM innlogging WHERE hash = ?", token)
    assert not t.db.les("SELECT 1 FROM okter WHERE hash = ?", svar["okt"])


def test_login_rejects_bad_addresses_foreign_return_links_and_floods(t):
    with pytest.raises(tj.Feil):
        t.be_om_lenke("ikke-en-adresse")
    t.be_om_lenke("a@b.no", "https://ondsinnet.example/")
    assert t.bekreft(t.sendt[-1][1].split("#t=")[1])["tilbake"] is None
    t.be_om_lenke("a@b.no")
    t.be_om_lenke("a@b.no")
    with pytest.raises(tj.Feil) as e:
        t.be_om_lenke("A@B.no")
    assert e.value.kode == 429


def test_smtp_failure_is_a_clear_error(tmp_path):
    svc = lag(tmp_path, Alltid())

    def feiler(*_):
        raise OSError("nede")
    svc.send_lenke = feiler
    with pytest.raises(tj.Feil) as e:
        svc.be_om_lenke("a@b.no")
    assert e.value.kode == 503


def test_display_names(t):
    b, _ = logg_inn(t, navn=None)
    with pytest.raises(tj.Feil):
        t.ny_kommentar(b, SAK, "Hei hei")  # no name yet
    for daarlig in ("R", "Redaksjonen", "Tingposten-fan", "<script>", "x" * 31, "Du er en idiot"):
        with pytest.raises(tj.Feil):
            t.sett_navn(b, daarlig)
    assert t.sett_navn(b, "  Kari   Nordmann ") == "Kari Nordmann"
    andre, _ = logg_inn(t, "kari@b.no", navn=None)
    with pytest.raises(tj.Feil) as e:
        t.sett_navn(andre, "kari nordmann")
    assert e.value.kode == 409


def test_comment_is_published_or_stopped_with_the_rule_shown_only_to_the_author(t):
    ola, _ = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    ok = t.ny_kommentar(ola, SAK, "Et dårlig vedtak, men forståelig.")
    stopp = t.ny_kommentar(kari, SAK, "Ola er en idiot")
    assert ok["status"] == "publisert"
    assert stopp["status"] == "avvist" and stopp["regel"]["key"] == "personangrep"
    for hvem, ser in ((None, [ok["id"]]), (ola, [ok["id"]]), (kari, [ok["id"], stopp["id"]])):
        assert sorted(k["id"] for k in t.trad(SAK, hvem)["kommentarer"]) == ser
    with pytest.raises(tj.Feil):
        t.ny_kommentar(ola, "ukjent/sak", "Hei hei")


def test_unsure_moderator_sends_the_comment_to_the_editor(tmp_path):
    svc = lag(tmp_path, Alltid(p=0.5))
    ola, _ = logg_inn(svc)
    k = svc.ny_kommentar(ola, SAK, "Hmm.")
    assert k["status"] == "til_redaktor" and [r["id"] for r in svc.redaktor_ko()] == [k["id"]]
    svc.redaktor_avgjor(k["id"], "publiser", tekst="Hmm.")
    assert svc.trad(SAK, None)["antall"] == 1 and svc.redaktor_ko() == []
    logg = svc.db.les("SELECT hvem, utfall FROM vurderinger WHERE kommentar = ? ORDER BY id", k["id"])
    assert [(r["hvem"], r["utfall"]) for r in logg] == [("test", "redaktor"), ("redaktør", "publiser")]


def test_rate_limit_between_comments(t, monkeypatch):
    ola, _ = logg_inn(t)
    t.ny_kommentar(ola, SAK, "Første kommentar")
    with pytest.raises(tj.Feil) as e:
        t.ny_kommentar(ola, SAK, "Andre kommentar")
    assert e.value.kode == 429


def test_votes_and_best_sorting(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    forfatter, _ = logg_inn(t)
    lesere = [logg_inn(t, f"l{i}@b.no", f"Leser {i}")[0] for i in range(6)]
    a = t.ny_kommentar(forfatter, SAK, "A: tidlig, ett ja")
    b = t.ny_kommentar(forfatter, SAK, "B: mange ja, ett nei")
    c = t.ny_kommentar(forfatter, SAK, "C: flere nei enn ja")
    t.stem(lesere[0], a["id"], 1)
    for leser in lesere[:5]:
        t.stem(leser, b["id"], 1)
    t.stem(lesere[5], b["id"], -1)
    for leser in lesere[:4]:
        t.stem(leser, c["id"], -1)
    t.stem(lesere[4], c["id"], 1)
    with pytest.raises(tj.Feil):
        t.stem(forfatter, a["id"], 1)  # own comment
    with pytest.raises(tj.Feil):
        t.stem(lesere[0], a["id"], 2)
    assert t.stem(lesere[0], a["id"], 0)["poeng"] == 0  # vote withdrawn
    t.stem(lesere[0], a["id"], 1)
    ids = lambda s: [k["id"] for k in t.trad(SAK, None, s)["kommentarer"]]
    assert ids("best") == [b["id"], a["id"], c["id"]]
    assert ids("topp") == [b["id"], a["id"], c["id"]]
    assert ids("nye") == [c["id"], b["id"], a["id"]]
    mine = {k["id"]: k["min_stemme"] for k in t.trad(SAK, lesere[5])["kommentarer"]}
    assert mine == {a["id"]: 0, b["id"]: -1, c["id"]: 0}


def test_wilson_prefers_many_votes_over_one():
    assert tj.wilson(50, 5) > tj.wilson(1, 0) > tj.wilson(0, 0) == 0
    assert tj.wilson(10, 10) < tj.wilson(10, 1)


def test_replies_threads_and_deleted_parents(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    rot = t.ny_kommentar(ola, SAK, "Hva med syklistene?")
    svar = t.ny_kommentar(kari, SAK, "De får egen trasé.", forelder=rot["id"])
    with pytest.raises(tj.Feil):
        t.ny_kommentar(kari, SAK, "Svar på noe som ikke finnes", forelder=999)
    tre = t.trad(SAK, None)["kommentarer"]
    assert tre[0]["id"] == rot["id"] and tre[0]["svar"][0]["id"] == svar["id"]
    t.slett(ola, rot["id"])
    tre = t.trad(SAK, None)["kommentarer"]
    assert tre[0]["status"] == "slettet" and tre[0]["tekst"] == "" and tre[0]["navn"] == ""
    assert tre[0]["svar"][0]["tekst"] == "De får egen trasé."
    t.slett(kari, svar["id"])
    assert t.trad(SAK, None)["kommentarer"] == []  # nothing published left


def test_edit_is_moderated_again_and_reports_reach_the_editor(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    k = t.ny_kommentar(ola, SAK, "Fint vedtak")
    assert t.endre(ola, k["id"], "Kari er en idiot")["status"] == "avvist"
    with pytest.raises(tj.Feil):
        t.endre(kari, k["id"], "Kapret")
    assert t.be_om_manuell(ola, k["id"])["status"] == "til_redaktor"
    k2 = t.ny_kommentar(ola, SAK, "Noe som kan rapporteres")
    t.rapporter(kari, k2["id"], "Usant")
    t.rapporter(kari, k2["id"], "To ganger teller én gang")
    ko = {r["id"]: r for r in t.redaktor_ko()}
    assert set(ko) == {k["id"], k2["id"]} and ko[k2["id"]]["rapporter"] == 1
    t.redaktor_avgjor(k2["id"], "fjern", "beskyldning")
    assert [x["status"] for x in t.trad(SAK, None)["kommentarer"]] == []


def test_moderation_sees_the_parent_and_ignores_stale_texts(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    rot = t.ny_kommentar(ola, SAK, "Rot")
    t.ny_kommentar(ola, SAK, "Svar", forelder=rot["id"])
    assert t.moderator.ledd[0].sett[-1] == ("Svar", "Rot")
    t.umiddelbar = False
    k = t.ny_kommentar(ola, SAK, "Venter i køen")
    assert k["status"] == "venter"
    t.slett(ola, k["id"])
    t.moderer(k["id"])  # deleted meanwhile: nothing is decided
    assert t.db.en("SELECT status FROM kommentarer WHERE id = ?", k["id"])["status"] == "slettet"


def test_all_backends_failing_never_publishes(tmp_path):
    svc = lag(tmp_path, m.FakeModerator([m.ModereringFeil("nede")] * 2, "a"),
              m.FakeModerator([m.ModereringFeil("nede")] * 2, "b"))
    svc.sett_navn = lambda b, navn: None  # the name check would also go to the failing moderators
    b, _ = logg_inn(svc, navn=None)
    with svc.db.tx() as c:
        c.execute("UPDATE brukere SET navn = 'Ola' WHERE id = ?", (b.id,))
    k = svc.ny_kommentar(svc.bruker(svc.bekreft(_ny_token(svc, "ola@example.no"))["okt"]), SAK, "Hei")
    assert k["status"] == "til_redaktor"
    logg = svc.db.en("SELECT data FROM vurderinger WHERE kommentar = ?", k["id"])["data"]
    assert '"a: nede"' in logg and '"b: nede"' in logg


def _ny_token(svc, epost):
    svc.be_om_lenke(epost)
    return svc.sendt[-1][1].split("#t=")[1]


def test_account_deletion_removes_everything_personal(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, okt = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    rot = t.ny_kommentar(ola, SAK, "Ola skriver")
    t.ny_kommentar(kari, SAK, "Kari svarer", forelder=rot["id"])
    k = t.ny_kommentar(kari, SAK, "Kari alene")
    t.stem(ola, k["id"], 1)
    eksport = t.db.eksport(ola.id)
    assert eksport["konto"]["epost"] == "ola@example.no" and len(eksport["kommentarer"]) == 1
    t.db.slett_bruker(ola.id)
    assert t.bruker(okt["okt"]) is None
    assert t.db.en("SELECT COUNT(*) AS n FROM brukere WHERE epost = 'ola@example.no'")["n"] == 0
    assert t.db.en("SELECT opp FROM kommentarer WHERE id = ?", k["id"])["opp"] == 0
    tre = t.trad(SAK, None)["kommentarer"]
    ids = {x["id"]: x for x in tre}
    assert ids[rot["id"]]["tekst"] == "" and ids[rot["id"]]["svar"][0]["tekst"] == "Kari svarer"
    assert "Ola skriver" not in str(t.db.les("SELECT tekst FROM kommentarer"))


def test_blocked_author_is_logged_out(t):
    ola, okt = logg_inn(t)
    k = t.ny_kommentar(ola, SAK, "Hei hei")
    t.sperr(k["id"])
    assert t.bruker(okt["okt"]) is None
    with pytest.raises(tj.Feil):
        t.bekreft(_ny_token(t, "ola@example.no"))


@pytest.mark.parametrize("tekst, feil", [("", True), (" a ", True), ("x" * 3001, True), ("Hei\x00du", False)])
def test_text_cleaning(tekst, feil):
    if feil:
        with pytest.raises(tj.Feil):
            tj.rens_tekst(tekst)
    else:
        assert tj.rens_tekst(tekst) == "Heidu"
    assert tj.rens_tekst("a\r\n\n\n\n\nb") == "a\n\nb"


# --- regressions from the review of 31.10 -------------------------------------------------

def test_editor_never_publishes_a_text_changed_since_they_saw_it(tmp_path, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    svc = lag(tmp_path, Alltid({"idiot": "personangrep", "hmm": "ok"}, p=0.5))
    ola, _ = logg_inn(svc)
    k = svc.ny_kommentar(ola, SAK, "Hmm, tja.")
    assert k["status"] == "til_redaktor"
    svc.moderator.ledd[0].p = 0.95
    assert svc.endre(ola, k["id"], "Du er en idiot")["status"] == "avvist"
    with pytest.raises(tj.Feil) as e:
        svc.redaktor_avgjor(k["id"], "publiser", tekst="Hmm, tja.")  # what the editor read
    assert e.value.kode == 409 and svc.trad(SAK, None)["antall"] == 0
    svc.slett(ola, k["id"])
    with pytest.raises(tj.Feil):
        svc.redaktor_avgjor(k["id"], "publiser", tekst="")


def test_a_broken_backend_never_publishes(tmp_path):
    class Krasj(m.Moderator):
        name = "krasj"

        def _vurder(self, tekst, sak, forelder):
            raise AttributeError("'list' object has no attribute 'get'")
    svc = lag(tmp_path, Krasj())
    svc.sett_navn = lambda b, navn: None
    b, _ = logg_inn(svc, navn=None)
    with svc.db.tx() as c:
        c.execute("UPDATE brukere SET navn = 'Ola'")
    k = svc.ny_kommentar(svc.bruker(svc.bekreft(_ny_token(svc, "ola@example.no"))["okt"]), SAK, "Hei hei")
    assert k["status"] == "til_redaktor"


def test_edits_are_limited(t, monkeypatch):
    ola, _ = logg_inn(t)
    k = t.ny_kommentar(ola, SAK, "Første versjon")
    with pytest.raises(tj.Feil) as e:
        t.endre(ola, k["id"], "Rett etterpå")
    assert e.value.kode == 429
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    for i in range(tj.MAKS_ENDRINGER):
        t.endre(ola, k["id"], f"Versjon {i}")
    with pytest.raises(tj.Feil):
        t.endre(ola, k["id"], "En for mye")
    assert len(t.moderator.ledd[0].sett) == 1 + 1 + tj.MAKS_ENDRINGER  # name + comment + edits


def test_name_attempts_are_limited(t):
    b, _ = logg_inn(t, navn=None)
    for i in range(tj.NAVNEFORSOK_PER_TIME):
        t.sett_navn(b, f"Navn {i}")
    with pytest.raises(tj.Feil) as e:
        t.sett_navn(b, "Ett til")
    assert e.value.kode == 429


def test_published_replies_stay_visible_when_the_parent_is_edited_into_hiding(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    rot = t.ny_kommentar(ola, SAK, "Et standpunkt")
    svar = t.ny_kommentar(kari, SAK, "Et motsvar", forelder=rot["id"])
    t.endre(ola, rot["id"], "Kari er en idiot")  # stopped
    for hvem in (None, kari):
        tre = t.trad(SAK, hvem)["kommentarer"]
        assert tre[0]["status"] == "skjult" and tre[0]["tekst"] == "" and tre[0]["navn"] == ""
        assert tre[0]["svar"][0]["id"] == svar["id"]
    assert t.trad(SAK, ola)["kommentarer"][0]["tekst"] == "Kari er en idiot"  # the author sees their own


def test_login_mail_limit_holds_under_parallel_requests_and_plus_tags(t):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(10) as pool:
        resultater = list(pool.map(lambda i: _prov(t, f"offer+{i}@b.no"), range(20)))
    assert resultater.count("sendt") == tj.LENKER_PER_TIME and len(t.sendt) == tj.LENKER_PER_TIME


def _prov(t, epost):
    try:
        t.be_om_lenke(epost)
        return "sendt"
    except tj.Feil as e:
        return e.kode


def test_global_login_mail_cap(t, monkeypatch):
    monkeypatch.setattr(tj, "LENKER_PER_TIME_TOTALT", 2)
    t.be_om_lenke("a@b.no")
    t.be_om_lenke("c@d.no")
    with pytest.raises(tj.Feil) as e:
        t.be_om_lenke("e@f.no")
    assert e.value.kode == 503


def test_deleting_the_account_removes_login_links_and_moderator_notes(t):
    ola, _ = logg_inn(t)
    t.ny_kommentar(ola, SAK, "Ola skriver")
    t.be_om_lenke("ola@example.no")  # an unused link
    ubrukt = t.sendt[-1][1].split("#t=")[1]
    assert t.db.eksport(ola.id)["innloggingslenker"] and t.db.eksport(ola.id)["vurderinger"]
    t.db.slett_bruker(ola.id)
    assert t.db.en("SELECT COUNT(*) AS n FROM innlogging WHERE epost = 'ola@example.no'")["n"] == 0
    assert all(r["data"] == "{}" for r in t.db.les("SELECT data FROM vurderinger"))
    with pytest.raises(tj.Feil):
        t.bekreft(ubrukt)  # no account comes back from an old link


def test_display_names_never_go_to_external_moderators(tmp_path):
    class Ute(Alltid):
        lokal = False
    ute = Ute()
    svc = lag(tmp_path, ute)
    b, _ = logg_inn(svc, navn=None)
    assert svc.sett_navn(b, "Ola") == "Ola" and ute.sett == []


def test_blocking_removes_what_the_author_has_waiting(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    synlig = t.ny_kommentar(ola, SAK, "Publisert")
    t.umiddelbar = False
    venter = t.ny_kommentar(ola, SAK, "Venter i køen")
    t.sperr(synlig["id"])
    t.moderer(venter["id"])
    assert t.db.en("SELECT status FROM kommentarer WHERE id = ?", venter["id"])["status"] == "fjernet"
    t.db.slett_bruker(ola.id)
    t.sperr(synlig["id"])  # account gone: nothing to block, no error


@pytest.mark.parametrize("navn", ["Redaktør Midttun", "Bergen kommune.", "Byrådet i Bergen", "Тingposten",
                                  "TING-posten", "Ｔｉｎｇｐｏｓｔｅｎ", "Henrik Midttun", "RO-Studio AS"])
def test_impersonating_names_are_refused(t, navn, monkeypatch):
    from tingposten import config
    monkeypatch.setattr(config, "ANSVARLIG_REDAKTOR", "Henrik Midttun")
    svc = tj.Tjeneste(t.db, t.moderator, SAKER, t.send_lenke, "https://tingposten.no/")
    b, _ = logg_inn(t, navn=None)
    with pytest.raises(tj.Feil):
        svc.sett_navn(b, navn)


def test_names_are_unique_after_folding(t):
    a, _ = logg_inn(t, navn="Kari Nordmann")
    b, _ = logg_inn(t, "b@b.no", navn=None)
    with pytest.raises(tj.Feil) as e:
        t.sett_navn(b, "kari  NORDMANN")
    assert e.value.kode == 409


def test_threads_have_a_maximum_depth(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    monkeypatch.setattr(tj, "KOMMENTARER_PER_DOGN", 100)
    monkeypatch.setattr(tj, "NYE_KONTO_PER_DOGN", 100)
    ola, _ = logg_inn(t)
    forelder = None
    for i in range(tj.MAKS_DYBDE):
        forelder = t.ny_kommentar(ola, SAK, f"Nivå {i}", forelder=forelder)["id"]
    with pytest.raises(tj.Feil):
        t.ny_kommentar(ola, SAK, "For dypt", forelder=forelder)
    assert t.trad(SAK, None)["antall"] == tj.MAKS_DYBDE


def test_editor_queue_keeps_the_models_verdict_and_report_reasons(t, monkeypatch):
    monkeypatch.setattr(tj, "KOMMENTAR_PAUSE_S", 0)
    ola, _ = logg_inn(t)
    kari, _ = logg_inn(t, "kari@b.no", "Kari")
    stopp = t.ny_kommentar(ola, SAK, "Kari er en idiot")
    t.be_om_manuell(ola, stopp["id"])
    k = t.ny_kommentar(ola, SAK, "Noe å rapportere")
    t.rapporter(kari, k["id"], "Usant om meg")
    ko = {r["id"]: r for r in t.redaktor_ko()}
    assert ko[stopp["id"]]["ba_om_vurdering"] and '"personangrep"' in ko[stopp["id"]]["vurdering"]
    assert ko[k["id"]]["grunner"] == ["Usant om meg"]
