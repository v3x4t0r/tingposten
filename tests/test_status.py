import pytest

from tingposten import status


def item(tittel="Sak", **kw):
    return {"tittel": tittel, "kategori": "Saker til behandling", "vedtakFilId": "1", **kw}


def vote(forslag, resultat, stemmer=None, enstemmig=False):
    return {"forslag": forslag, "resultat": resultat, "stemmer": stemmer, "enstemmig": enstemmig}


def prot(vedtak, votes=None):
    return {"vedtak": vedtak, "voteringer": votes if votes is not None else [vote("Innstillingen", "vedtatt", enstemmig=True)]}


def kode(tittel, vedtak, votes=None):
    return status.vedtaksstatus(item(tittel), prot(vedtak, votes))["kode"]


def test_adopted():
    assert kode("Gjennomføringsvedtak for idrettshall", "1. Prosjektet gjennomføres med ramme på 69 mill.") == "vedtatt"


def test_sent_back_users_example():
    # 258/26: the alternative that won sends the plan back with guidelines.
    v = ("Med hjemmel i pbl. § 12-12, første avsnitt, siste setning fatter Bergen bystyre følgende vedtak: "
         "1.Reguleringsplanforslag for gang- og sykkelveg langs Storetveitvegen, PlanID-63100000, sendes tilbake "
         "for ytterligere bearbeiding. Det gis følgende retningslinjer for det videre arbeidet med planen:")
    s = status.vedtaksstatus(item("Fana/Årstad, Reguleringsplan for gang- og sykkelanlegg"),
                             prot(v, [vote("Alternativforslag", "vedtatt", 35)]))
    assert s == {"kode": "sendt_tilbake", "tekst": "Sendt tilbake – ikke avgjort ennå"}


@pytest.mark.parametrize("vedtak", [
    "Saken sendes tilbake til byrådet.",
    "Planforslaget sendes i retur til administrasjonen for ny vurdering.",
    "Saken returneres til byrådet.",
])
def test_sent_back_wordings(vedtak):
    assert kode("Reguleringsplan", vedtak) == "sendt_tilbake"


def test_follow_up_request_is_not_sent_back():
    assert kode("Strategi", "1. Strategien vedtas. 2. Byrådet bes komme tilbake til bystyret med en sak.") == "vedtatt"


def test_postponed_by_text_or_adopted_utsettelsesforslag():
    assert kode("Sak", "Saken utsettes til neste møte.") == "utsatt"
    assert kode("Sak", "Bystyret vedtar.", [vote("Utsettelsesforslag", "vedtatt", 40)]) == "utsatt"


def test_fallen_utsettelsesforslag_does_not_postpone():
    assert kode("Sak", "Avtalen godkjennes.", [vote("Utsettelsesforslag", "falt", 4),
                                              vote("Innstillingen", "vedtatt", 60)]) == "vedtatt"


def test_orientering_only_when_every_point_is_orientering():
    assert kode("Økonomiske perspektiver", "Bystyret tar beskrivelsen til orientering. "
                "Merknad 1 Bystyret understreker betydningen av tidlig innsats.") == "orientering"
    assert kode("Eierskapskontroll", "1. Bystyret tar rapporten til orientering. "
                "2. Bystyret ber byrådet om å følge opp anbefalingene.") == "vedtatt"


def test_nothing_adopted():
    assert kode("Interpellasjon nr. 31", "Det ble ikke fattet vedtak i saken.",
                [vote("Alternativforslag", "falt", 7)]) == "falt"


def test_not_dealt_with():
    assert kode("Interpellasjon nr. 32", "Interpellasjonen ble ikke behandlet grunnet overskredet tid.", []) \
        == "ikke_behandlet"


def test_elections_and_handover_by_title():
    assert kode("Søknad om fritak fra verv og nyvalg", "Som nytt varamedlem velges: X") == "valg"
    assert kode("Melding om uttreden – X (MDG)", "Som nytt 3. varamedlem velges: Y") == "valg"
    assert kode("Sluttbehandling av konsekvensutredning og valg av trasé for bybane", "2. Trasé velges.") == "vedtatt"
    assert kode("Overlevering av representantforslag 23.09.2026", "• X overleverte representantforslag om Y", []) \
        == "overlevert"


def test_no_protokoll_yet():
    assert status.vedtaksstatus(item(), {})["kode"] == "under_behandling"


def test_merknad_and_protokolltilforsel_do_not_count_as_binding():
    v = ("Bystyret tar saken til orientering. Merknader fra utvalg for helse: Saken sendes tilbake neste år. "
         "Daniel Hägglund (FRP) fremsatte på vegne av FRP følgende protokolltilførsel: noe annet.")
    assert status.bindende(v) == "Bystyret tar saken til orientering."
    assert kode("Sak", v) == "orientering"


def test_every_code_has_a_label():
    assert status.IKKE_AVGJORT <= set(status.LABELS)
