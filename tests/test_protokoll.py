import json
from pathlib import Path

import pytest

from tingposten.protokoll import parse_pages

FIXTURES = Path(__file__).parent / "fixtures" / "protokoll"


def load(name):
    return parse_pages(json.loads((FIXTURES / name).read_text()))


def test_unanimous():
    r = load("238-26_enstemmig.json")
    assert r["tittel"] == "Gjennomføringsvedtak for Slettebakken idrettsby fase 1 A"
    assert (r["organ"], r["motedato"], r["saksnr"], r["vedtak_side"]) == ("Bergen bystyre", "2026-09-23", "238/26", 1)
    assert r["vedtak"].startswith("1. Prosjekt 4974 Slettebakken idrettsby - fase 1A gjennomføres")
    assert r["vedtak"].splitlines()[1].startswith("2. Prosjektet finansieres")
    assert len(r["vedtak"].splitlines()) == 3
    assert r["talere"] == ["Vasan Singaravel (H)", "Richard Martin Laloo Storevik (AP)", "Torolv Onarheim (FRP)"]
    assert r["enstemmig"] is True
    assert r["innstilling_falt"] is False
    [v] = r["voteringer"]
    assert v == {
        "forslag": "Innstillingen",
        "fra": [],
        "fremsatt_av": None,
        "resultat": "vedtatt",
        "enstemmig": True,
        "stemmer": None,
        "partier": [],
        "tekst": "Innstillingen, ble enstemmig vedtatt.",
    }
    assert r["saksgang"] == [
        {"organ": "Utvalg for barnehage, skole og idrett", "dato": "2026-09-08", "saksnr": "79/26", "type": "orientering"},
        {"organ": "Utvalg for finans, kultur og næring", "dato": "2026-09-08", "saksnr": "139/26", "type": "innstilling"},
        {"organ": "Byrådet", "dato": "2026-08-27", "saksnr": "253/26", "type": "innstilling"},
    ]


def test_split_vote_alternative_falls():
    r = load("242-26_alternativ_falt.json")
    assert r["enstemmig"] is False
    assert r["innstilling_falt"] is False
    alt, inn = r["voteringer"]
    assert alt["forslag"] == "Alternativforslag"
    assert alt["fra"] == ["AP", "SV", "MDG", "V", "R", "UAV"]
    assert alt["fremsatt_av"] == "Ingrid Nergaard Fjeldstad (V)"
    assert (alt["resultat"], alt["stemmer"]) == ("falt", 31)
    assert alt["partier"] == ["AP", "MDG", "R", "SV", "UAV Bollmann", "V"]
    assert (inn["forslag"], inn["resultat"], inn["stemmer"]) == ("Innstillingen", "vedtatt", 36)
    # a name wrapped over two lines in the PDF
    assert "Ingrid Nergaard Fjeldstad (V)" in r["talere"]


def test_innstilling_falls_and_alternative_is_adopted():
    r = load("260-26_innstilling_falt.json")
    assert r["innstilling_falt"] is True
    inn, alt = r["voteringer"]
    assert (inn["forslag"], inn["resultat"], inn["stemmer"]) == ("Innstillingen", "falt", 31)
    assert (alt["forslag"], alt["resultat"], alt["stemmer"]) == ("Alternativforslag", "vedtatt", 36)
    assert alt["fra"] == ["H", "FRP", "BL", "INP", "SP", "PP"]
    # adopted text, with the shared compound kept intact
    assert "plan- og bygningsloven § 13-1" in r["vedtak"]
    assert r["vedtak"].splitlines()[-1] == "4. Dette vedtaket erstatter bystyrets vedtak av 27.05.25 i sak 13/25"
    # the vedtak stops before the debate section; the saksgang comes after the votes
    assert "Følgende representanter" not in r["vedtak"]
    assert len(r["talere"]) == 13
    assert [s["type"] for s in r["saksgang"]] == ["innstilling", "vedtak", "innstilling"]
    assert r["saksgang"][1]["dato"] == "2026-06-04"


def test_pointwise_vote():
    r = load("245-26_punktvis.json")
    assert r["innstilling_falt"] is True  # pkt. 3 fell
    assert [(v["forslag"], v["resultat"]) for v in r["voteringer"]] == [
        ("Innstillingens pkt. 1 og 2", "vedtatt"),
        ("Innstillingens pkt. 3", "falt"),
        ("Merknader", "vedtatt"),
    ]
    assert r["voteringer"][2]["fra"] == ["utvalg for helse og sosial"]
    organs = [s["organ"] for s in r["saksgang"]]
    assert organs[0] == "Eldrerådet"
    assert "Kommunalt råd for personer med funksjonsnedsettelse" in organs


def test_no_votering():
    r = load("32-26_uten_votering.json")
    assert r["saksnr"] == "32/26"
    assert r["vedtak"] == "Interpellasjonen ble ikke behandlet grunnet overskredet tid."
    assert r["talere"] == []
    assert r["voteringer"] == []
    assert r["enstemmig"] is False
    assert r["saksgang"] == []


def test_record_without_vedtak_or_behandling():
    # 233/26 "Overlevering av representantforslag": a bullet list, nothing else
    page = (
        " \n \nOverlevering av representantforslag 23.09.2026 \n \n \n"
        "Bergen bystyre behandlet saken i møtet 23.9.2026 sak 233/26:  \n \n"
        "• \nTorstein Gya (SV) og Lillian Blom (SV) overleverte representantforslag om \n"
        "gateopprustning og trafikksikkerhet på Solheim \n"
        "• \nMailiss Solheim-Åkerblom (R) overleverte representantforslag om reparasjon av \n"
        "arbeidstøy og -sko. \n \n"
    )
    r = parse_pages([page])
    assert (r["tittel"], r["saksnr"]) == ("Overlevering av representantforslag 23.09.2026", "233/26")
    assert r["vedtak"].splitlines() == [
        "• Torstein Gya (SV) og Lillian Blom (SV) overleverte representantforslag om gateopprustning og trafikksikkerhet på Solheim",
        "• Mailiss Solheim-Åkerblom (R) overleverte representantforslag om reparasjon av arbeidstøy og -sko.",
    ]
    assert (r["talere"], r["voteringer"], r["saksgang"]) == ([], [], [])


def _sak(votering: str, saksgang: str) -> list[str]:
    return [
        "Tittel\n\nBergen bystyre behandlet saken i møtet 23.9.2026 sak 1/26 og fattet følgende vedtak:\n"
        "Vedtakstekst.\n\nBergen bystyres behandling\nIngen representanter tok ordet.\n\n"
        f"Votering:\n{votering}\n\n{saksgang}"
    ]


ALT_WINS = "Alternativforslag fra H fremsatt av Ola Nordmann (H), ble vedtatt med 35 stemmer (H, FRP)."


def test_adopted_alternative_without_innstilling_vote_counts_as_falt():
    # 258/26: only the alternativ is voted on; it replaces the innstilling
    saksgang = "Byrådet behandlet saken i møtet 21.05.2026 sak 9/26\nByrådet innstiller til bystyret å fatte følgende vedtak:\nNoe."
    assert parse_pages(_sak(ALT_WINS, saksgang))["innstilling_falt"] is True


def test_adopted_alternative_without_any_innstilling_is_not_falt():
    # interpellasjoner and eventuelt-saker have no innstilling to fall
    assert parse_pages(_sak(ALT_WINS, ""))["innstilling_falt"] is False


def test_til_stede_and_page_numbers():
    votering = (
        "Utsettelsesforslag fra R fremsatt av Kari Nordmann (R), fikk 3 stemmer (R), og var dermed falt. "
        "(det var 66 representanter til stede)\n\nInnstillingen, ble enstemmig vedtatt."
    )
    pages = _sak(votering, "")
    pages = [" \n \n \n1 \n" + pages[0], " \n \n2 \n \nUtvalg for helse og sosial behandlet saken i møtet 10.9.2026 sak 97/26: \nUtvalg for helse og sosial tok saken til orientering."]
    r = parse_pages(pages)
    assert r["til_stede"] == 66
    assert [v["resultat"] for v in r["voteringer"]] == ["falt", "vedtatt"]
    # the page number "2" at the top of page 2 is not glued onto the organ
    assert r["saksgang"] == [
        {"organ": "Utvalg for helse og sosial", "dato": "2026-09-10", "saksnr": "97/26", "type": "orientering"}
    ]


def test_line_wraps_in_names_and_compounds():
    page = (
        "Tittel\n\nBergen bystyre behandlet saken i møtet 23.9.2026 sak 1/26 og fattet følgende vedtak:\n"
        "Saken behandles etter plan- \nog bygningsloven.\n\nBergen bystyres behandling\n"
        "Følgende representanter tok ordet: \nPer-\nErik Gåskjenn (KRF), Andreas \nMadsen Berg (SV).\n\n"
        "Votering:\nInnstillingen, ble enstemmig vedtatt.\n"
    )
    r = parse_pages([page])
    assert r["vedtak"] == "Saken behandles etter plan- og bygningsloven."
    assert r["talere"] == ["Per-Erik Gåskjenn (KRF)", "Andreas Madsen Berg (SV)"]


@pytest.mark.parametrize("pages", [[], [""], ["Bare litt tekst uten struktur."], ["Votering:\nfikk 3 stemmer"]])
def test_garbage_never_raises(pages):
    r = parse_pages(pages)
    assert r["voteringer"] == [] and r["saksnr"] == "" and r["enstemmig"] is False
