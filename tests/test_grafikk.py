import re
import xml.etree.ElementTree as ET

import pytest

from tingposten import grafikk

NS = "{http://www.w3.org/2000/svg}"


def vote(forslag, resultat, stemmer=None, partier=(), fra=(), enstemmig=False):
    return {"forslag": forslag, "fra": list(fra), "fremsatt_av": None, "resultat": resultat,
            "enstemmig": enstemmig, "stemmer": stemmer, "partier": list(partier), "tekst": ""}


def prot(*votes, tittel="Reguleringsplan for Eksempelområdet", til_stede=None, vedtak=""):
    return {"tittel": tittel, "organ": "Bergen bystyre", "voteringer": list(votes),
            "til_stede": til_stede, "vedtak": vedtak, "vedtak_side": 1}


SPLIT = prot(
    vote("Innstillingen", "falt", 31, ["AP", "MDG", "R", "SV", "UAV Bollmann", "V"]),
    vote("Alternativforslag", "vedtatt", 36, ["BL", "FRP", "H", "INP", "KRF", "PP", "SP"], fra=["H", "FRP", "BL"]),
)


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)  # raises if not well-formed


def texts(svg: str) -> list[str]:
    return ["".join(t.itertext()) for t in parse(svg).iter(NS + "text")]


def assert_self_contained(svg: str):
    for bad in ("href", "url(", "<image", "<script", "<foreignObject", "http://", "https://"):
        assert bad not in svg.replace('xmlns="http://www.w3.org/2000/svg"', ""), bad
    root = parse(svg)
    assert root.find(NS + "title") is not None and root.find(NS + "desc") is not None


# --- stemmediagram --------------------------------------------------------------

def test_split_vote_draws_stated_numbers_only():
    f = grafikk.stemmediagram(SPLIT)
    assert_self_contained(f["svg"])
    numbers = {t for t in texts(f["svg"]) if t.isdigit()}
    assert numbers == {"36", "31"}
    assert "vedtatt" in f["alt"] and "stemt ned" in f["alt"]
    assert f["kilde"].startswith("Protokoll")


def test_adopted_first_and_parties_in_text():
    t = texts(grafikk.stemmediagram(SPLIT)["svg"])
    assert t[0].startswith("Alternativforslag fra H, FrP og BL")
    joined = " ".join(t)
    assert "én uavhengig" in joined and "UAV" not in joined
    assert "KrF" in joined and "KRF" not in joined


def test_no_figure_for_unanimous_or_single_vote():
    assert grafikk.stemmediagram(prot(vote("Innstillingen", "vedtatt", enstemmig=True))) is None
    assert grafikk.stemmediagram(prot(vote("Alternativforslag", "vedtatt", 35, ["H"]))) is None
    assert grafikk.stemmediagram(prot()) is None


def test_merknader_and_tillegg_do_not_make_a_split():
    p = prot(
        vote("Innstillingen", "vedtatt", enstemmig=True),
        vote("Tilleggsforslag", "falt", 32, ["AP"]),
        vote("Merknad", "vedtatt", 36, ["H"]),
        vote("Merknad", "falt", 31, ["AP"]),
    )
    assert grafikk.stemmediagram(p) is None


def test_tillegg_counts_when_there_is_no_innstilling():
    p = prot(vote("Tilleggsforslag", "falt", 32, ["AP"]), vote("Alternativforslag", "vedtatt", 34, ["H"]),
             tittel="Interpellasjon nr. 30")
    assert {t for t in texts(grafikk.stemmediagram(p)["svg"]) if t.isdigit()} == {"32", "34"}


def test_eventuelt_is_skipped():
    assert grafikk.stemmediagram({**SPLIT, "tittel": "Eventuelt bystyret 23.09.2026"}) is None


def test_more_than_four_proposals_are_summarised_with_their_counts():
    p = prot(vote("Innstillingen", "vedtatt", 56, ["H"]), *[vote("Alternativforslag", "falt", n, ["R"]) for n in (20, 19, 8, 3)])
    t = texts(grafikk.stemmediagram(p)["svg"])
    assert {x for x in t if x.isdigit()} == {"56", "20", "19", "8"}
    assert any("1 forslag til fikk 3 stemmer" in x for x in t)


def test_party_tokens():
    assert grafikk._party("6SV") == "6 fra SV"
    assert grafikk._party("11AP") == "11 fra Ap"
    assert grafikk._party("UAV Bollmann") == "én uavhengig"
    assert grafikk._party("FRP") == "FrP"
    assert grafikk._join(["Ap", "H", "V"]) == "Ap, H og V"
    assert grafikk._join(["Ap"]) == "Ap"
    assert grafikk._join([]) == ""


def test_text_is_escaped():
    p = prot(vote("Innstillingen", "falt", 31, ["<script>"]), vote("Alternativforslag", "vedtatt", 36, ["H"], fra=["A&B"]))
    svg = grafikk.stemmediagram(p)["svg"]
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    parse(svg)


# --- bydelskart -------------------------------------------------------------------

def test_map_highlights_and_labels_only_named_bydeler():
    f = grafikk.bydelskart(["Fana", "Årstad", "Ukjentby"])
    assert_self_contained(f["svg"])
    assert set(texts(f["svg"])) == {"Fana", "Årstad"}
    root = parse(f["svg"])
    titles = {"".join(t.itertext()) for p in root.iter(NS + "path") for t in p.iter(NS + "title")}
    assert len(titles) == 8 and "Fana – nevnt i saken" in titles and "Åsane" in titles
    assert root.get("viewBox").startswith("0 0 240 ")
    assert "OpenStreetMap" in f["kilde"]
    assert "Fana og Årstad" in f["alt"]


def test_map_bydeler_title_decides_and_passing_mentions_are_ignored():
    pick = grafikk.saksbydeler
    # plan sak: the title decides, other bydeler in the text don't count
    assert pick({"tittel": "Bergenhus, Gnr. 168, Områdereguleringsplan"},
                {"bydel_treff": {"Bergenhus": 3, "Åsane": 3}}) == (["Bergenhus"], "tittel")
    # one passing mention in a city-wide sak (255/26) → no map
    assert pick({"tittel": "Prinsipper for ny fordelingsmodell for bergensskolen"}, {"bydel_treff": {"Arna": 5}}) == ([], "")
    assert pick({"tittel": "Statusmelding for legetjenesten"}, {"bydel_treff": {"Fana": 1, "Åsane": 2}}) == ([], "saksframlegg")
    # named repeatedly without being in the title → shown
    assert pick({"tittel": "Alvøen idrettshall. Sluttmelding"}, {"bydel_treff": {"Laksevåg": 3}}) == (["Laksevåg"], "saksframlegg")
    # most of the city named repeatedly → it's about the whole city, no map
    assert pick({"tittel": "Tilskudd"}, {"bydel_treff": dict.fromkeys(grafikk.BYDELER, 3)}) == ([], "")
    assert "sakens tittel nevner den" in grafikk.bydelskart(["Fana"], "tittel")["alt"]
    assert "flere ganger i saksframlegget" in grafikk.bydelskart(["Fana"])["kilde"]


def test_map_none_without_named_bydel():
    assert grafikk.bydelskart([]) is None
    assert grafikk.bydelskart(["Oslo"]) is None


def test_geodata_has_all_eight_with_attribution():
    assert set(grafikk._GEO["bydeler"]) == set(grafikk.BYDELER)
    assert "OpenStreetMap" in grafikk._GEO["attribusjon"]
    assert "ODbL" in grafikk._GEO["lisens"]


# --- beløp i kontekst -------------------------------------------------------------

def sf(*sections):
    return {"seksjoner": [{"overskrift": "", "tekst": t, "side": side} for t, side in sections]}


@pytest.mark.parametrize("text", [
    "Prosjektet har en total investeringsramme på 69 mill. kroner.",
    "Driftsresultatet ble nedjustert fra 174 mill. i vedtatt budsjett til 107,5 mill.",
    "Amortiseringen vil øke fra nær 0,8 mrd. kroner i 2026 til over 1 mrd. kroner i 2029.",
    "For skole utgjør det en P50 på 576,2 mill. og en P85 på 640,3 mill. (74 %).",
])
def test_no_money_figure_without_an_explicit_total(text):
    assert grafikk.belop_i_kontekst(prot(), sf((text, 3))) is None


def test_increase_to_new_total():
    text = ("Prosjektet har behov for å øke den totale investeringsrammen for prosjektet med 31,4 mill., "
            "slik at ny anbefalt total investeringsramme (P85) blir 135,9 mill. kr og ny forventet kostnad 120 mill.")
    f = grafikk.belop_i_kontekst(prot(), sf(("Innledning.", 1), (text, 2)))
    assert_self_contained(f["svg"])
    joined = " ".join(texts(f["svg"]))
    assert "31,4 mill." in joined and "135,9 mill. kr" in joined
    assert f["kilde"] == "Saksframlegg s. 2"
    assert f["alt"].startswith("Saksframlegget beskriver")


def test_share_of_total_and_vedtak_wins():
    vedtak = "Bystyret bevilger 12 mill. kroner av totalt 40 mill. kroner til tiltaket."
    other = "Rammen økes med 1 mill. til 2 mill."
    f = grafikk.belop_i_kontekst(prot(vedtak=vedtak), sf((other, 4)))
    assert f["kilde"] == "Vedtak s. 1"
    joined = " ".join(texts(f["svg"]))
    assert "12 mill. kroner" in joined and "40 mill. kroner" in joined


def test_part_must_be_smaller_than_total():
    assert grafikk.belop_i_kontekst(prot(), sf(("Rammen økes med 50 mill. til 20 mill.", 1))) is None


# --- figurer ---------------------------------------------------------------------

def test_figurer_combines_and_reads_bydel_from_title():
    item = {"tittel": "Fana/Årstad, Reguleringsplan for gang- og sykkelanlegg"}
    figs = grafikk.figurer(item, SPLIT, {"bydeler": [], "seksjoner": []})
    kinds = [re.search(r"<title id=\"[^\"]+\">([^<]+)", f["svg"]).group(1) for f in figs]
    assert kinds == ["Slik ble det stemt", "Bydeler nevnt i saken"]
    assert set(texts(figs[1]["svg"])) == {"Fana", "Årstad"}
    ids = [re.search(r'aria-labelledby="([^ ]+)-t', f["svg"]).group(1) for f in figs]
    assert len(set(ids)) == len(ids)
