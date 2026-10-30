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
    assert "og ble vedtatt" in f["alt"] and "og falt" in f["alt"]
    assert f["kilde"].startswith("Protokoll")


def test_outcome_word_and_parties_in_text():
    t = texts(grafikk.stemmediagram(SPLIT)["svg"])
    assert t[0] == "Vedtatt: Alternativforslag fra H, FrP og BL"
    assert "Falt: Innstillingen" in t
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
    assert t[-1] == "1 forslag til falt (3 stemmer)."


# Sak 266/26 as the protokoll has it: five adopted parts (three with the same
# count and parties), three proposals that fell, a tilleggsforslag that doesn't count.
P266 = ["AP", "BL", "FRP", "H", "KRF", "PP", "R", "SP", "UAV Bollmann", "V"]
SAK_266 = prot(
    vote("Utsettelsesforslag", "falt", 11, ["MDG", "SV"], fra=["SV"]),
    vote("Utsettelsesforslag", "falt", 14, ["MDG", "R", "SV"], fra=["MDG"]),
    vote("Alternativforslag", "falt", 11, ["MDG", "SV"], fra=["SV"]),
    vote("Innstillingen - punkt 1", "vedtatt", 54, P266),
    vote("Innstillingen - punkt 2", "vedtatt", 54, P266),
    vote("Innstillingen - punkt 3, a-b", "vedtatt", enstemmig=True),
    vote("Innstillingens punkt 3, c-d", "vedtatt", 65, sorted(P266 + ["MDG", "SV"])),
    vote("Innstillingen - punkt 4, avsnitt 1", "vedtatt", 54, P266),
    vote("Innstillingens punkt 4, avsnitt 2 og 3", "vedtatt", 58, sorted(P266 + ["MDG"])),
    vote("Tilleggsforslag", "falt", 14, ["MDG", "R", "SV"], fra=["R"]),
    til_stede=66,
)


def labels(svg: str) -> list[str]:
    """Bar labels with their wrapped lines joined: the text between a bar's
    "Vedtatt:/Falt:" and its bar."""
    out, cur = [], None
    for el in parse(svg):
        tag = el.tag.replace(NS, "")
        if tag == "text" and "".join(el.itertext()).startswith(("Vedtatt:", "Falt:")):
            cur = "".join(el.itertext())
        elif tag == "text" and cur is not None:
            cur += " " + "".join(el.itertext())
        elif tag == "path" and cur is not None:
            out.append(cur)
            cur = None
    return out


def test_fallen_proposals_always_get_bars():
    f = grafikk.stemmediagram(SAK_266)
    got = labels(f["svg"])
    assert [x.split(":")[0] for x in got] == ["Vedtatt", "Vedtatt", "Falt", "Falt"]
    assert "Falt: Utsettelsesforslag fra MDG" in got
    # five adopted votes with distinct counts and three that fell: two bars each
    many = prot(*[vote(f"Innstillingens punkt {n}", "vedtatt", c, ["H", str(n)]) for n, c in enumerate((60, 55, 50, 45, 40))],
                *[vote("Alternativforslag", "falt", c, ["R"], fra=[p]) for c, p in ((20, "R"), (12, "SV"), (9, "MDG"))])
    t = texts(grafikk.stemmediagram(many)["svg"])
    assert {x for x in t if x.isdigit()} == {"60", "55", "20", "12"}
    assert " ".join(t).endswith(" 3 forslag til ble vedtatt (50, 45 og 40 stemmer) og 1 falt (9 stemmer).")


def test_identical_adopted_votes_merge_into_one_bar():
    f = grafikk.stemmediagram(SAK_266)
    got = labels(f["svg"])
    assert "Vedtatt: Innstillingen – punkt 1, punkt 2 og punkt 4, avsnitt 1" in got
    assert [x for x in texts(f["svg"]) if x.isdigit()] == ["65", "54", "14", "11"]
    # proposals from the same proposer that fell alike share a bar too
    assert "Falt: Utsettelsesforslag og alternativforslag fra SV" in got
    # same count, other parties: not merged
    p = prot(vote("Innstillingen punkt 1", "vedtatt", 40, ["H", "AP"]), vote("Innstillingen punkt 2", "vedtatt", 40, ["H", "V"]),
             vote("Alternativforslag", "falt", 20, ["R"], fra=["R"]))
    assert len(labels(grafikk.stemmediagram(p)["svg"])) == 3
    p = prot(vote("Innstillingen punkt 1", "vedtatt", 40, ["H"]), vote("Innstillingen punkt 2", "vedtatt", 40, ["H"]),
             vote("Alternativforslag", "falt", 20, ["R"], fra=["R"]))
    assert labels(grafikk.stemmediagram(p)["svg"])[0] == "Vedtatt: Innstillingen punkt 1 og 2"
    # fallen proposals from different proposers stay apart
    p = prot(vote("Innstillingen", "vedtatt", 47, ["H"]), vote("Alternativforslag", "falt", 20, ["AP"], fra=["AP", "MDG"]),
             vote("Alternativforslag", "falt", 20, ["AP"], fra=["AP", "MDG", "V"]))
    assert len(labels(grafikk.stemmediagram(p)["svg"])) == 3


def test_alt_text_lists_every_vote():
    alt = grafikk.stemmediagram(SAK_266)["alt"]
    assert alt.startswith("Avstemning i Bergen bystyre: Innstillingens punkt 3, c–d fikk 65 stemmer")
    assert "Innstillingens punkt 4, avsnitt 2 og 3 fikk 58 stemmer" in alt  # the one only in the rest line
    assert "punkt 1, punkt 2 og punkt 4, avsnitt 1 fikk 54 stemmer hver (Ap, BL, FrP, H, KrF, PP, R, Sp, én uavhengig og V) og ble vedtatt" in alt
    assert "Utsettelsesforslag fra MDG fikk 14 stemmer (MDG, R og SV) og falt" in alt
    assert "Utsettelsesforslag og alternativforslag fra SV fikk 11 stemmer hver (MDG og SV) og falt" in alt
    assert "Tilleggsforslag" not in alt


def test_bars_follow_the_count_not_the_outcome():
    p = prot(vote("Innstillingen", "vedtatt", 30, ["H"]), vote("Alternativforslag", "falt", 33, ["AP"], fra=["AP"]))
    svg = grafikk.stemmediagram(p)["svg"]
    assert labels(svg) == ["Falt: Alternativforslag fra Ap", "Vedtatt: Innstillingen"]
    # one weight for every label; the outcome word is set alike for both
    root = parse(svg)
    heads = [t for t in root.iter(NS + "text") if "".join(t.itertext()).startswith(("Vedtatt:", "Falt:"))]
    assert {t.get("font-weight") for t in heads} == {"400"}
    assert [(s.text, s.get("font-weight")) for t in heads for s in t.iter(NS + "tspan")] == [("Falt:", "600"), ("Vedtatt:", "600")]


def test_rest_line_keeps_every_count_and_says_the_outcome():
    def v(res, n):
        return vote("Alternativforslag", res, n, ["R"])
    assert grafikk._rest([v("vedtatt", 54), v("falt", 14), v("falt", 11), v("falt", 11)]) == \
        "1 forslag til ble vedtatt (54 stemmer) og 3 falt (14, 11 og 11 stemmer)."
    assert grafikk._rest([v("vedtatt", 49), v("vedtatt", 40)]) == "2 forslag til ble vedtatt (49 og 40 stemmer)."
    assert grafikk._rest([v("falt", 1)]) == "1 forslag til falt (1 stemme)."
    assert grafikk._rest([v("vedtatt", 58), v("falt", 11)]) == "1 forslag til ble vedtatt (58 stemmer) og 1 falt (11 stemmer)."
    assert texts(grafikk.stemmediagram(SAK_266)["svg"])[-1] == "1 forslag til ble vedtatt (58 stemmer)."


@pytest.mark.parametrize("raw, shown", [
    ("Innstillingen - punkt 1", "Innstillingen – punkt 1"),
    ("Innstillingens punkt 3, c-d", "Innstillingens punkt 3, c–d"),
    ("Alternativforslag punkt 4.1-4.3", "Alternativforslag punkt 4.1–4.3"),
    ("Innstillingens punkt 1.b.ii-iii", "Innstillingens punkt 1.b.ii–iii"),
    ("Tilleggsforslag punkt 1-3 og 5", "Tilleggsforslag punkt 1–3 og 5"),
    ("Alternativforslag til punkt 1b) viii - x", "Alternativforslag til punkt 1b) viii – x"),
    ("Ingen motsatte seg å votere over forslagene. Alternativforslag", "Alternativforslag"),
    # hyphens that are not dashes stay
    ("Alternativforslag om gang- og sykkelveg", "Alternativforslag om gang- og sykkelveg"),
    ("SV-forslaget om e-post", "SV-forslaget om e-post"),
    ("Forslag etter pbl. § 12-1", "Forslag etter pbl. § 12-1"),
])
def test_labels_get_en_dashes_and_lose_run_in_text(raw, shown):
    assert grafikk._name(vote(raw, "falt", 3)) == shown


def test_innstilling_in_a_run_in_sentence_still_counts():
    # "Innstillingen fikk ingen stemmer, og var dermed falt. Alternativforslag …":
    # there was an innstilling, so tilleggsforslag stay out
    p = prot(vote("Innstillingen fikk ingen stemmer, og var dermed falt. Alternativforslag punkt 1-3", "vedtatt", 53, ["H"], fra=["H"]),
             vote("Alternativforslag", "falt", 14, ["SV"], fra=["SV"]), vote("Tilleggsforslag", "falt", 32, ["AP"], fra=["AP"]))
    assert labels(grafikk.stemmediagram(p)["svg"]) == ["Vedtatt: Alternativforslag punkt 1–3 fra H", "Falt: Alternativforslag fra SV"]


def test_vote_text_is_at_least_12_px_and_fits():
    svg = grafikk.stemmediagram(SAK_266)["svg"]
    root = parse(svg)
    sizes = {"".join(t.itertext()): float(t.get("font-size")) for t in root.iter(NS + "text")}
    assert min(sizes.values()) >= 12
    assert all(size >= 12 for s, size in sizes.items() if s.startswith(("For:", "én", "og")))
    assert root.get("viewBox").startswith(f"0 0 {grafikk.W} ") and grafikk.W <= 320
    # every line within the line budget for its size (checked in a browser with fallback fonts too)
    assert all(len(s) <= grafikk._chars(size) for s, size in sizes.items())


def test_independents_are_counted():
    assert grafikk._parties(["AP", "UAV Amin", "UAV Bollmann", "V"]) == "Ap, to uavhengige og V"
    assert grafikk._parties(["UAV Bollmann"]) == "én uavhengig"
    lines = grafikk._wrap("For: Ap, BL, FrP, H, KrF, PP, R, Sp, SV, én uavhengig og V", 40)
    assert lines[1].startswith("én uavhengig")


def test_party_tokens():
    assert grafikk._party("6SV") == "6 fra SV"
    assert grafikk._party("11AP") == "11 fra Ap"
    assert grafikk._party("UAV Bollmann") == "én uavhengig"
    assert grafikk._party("FRP") == "FrP"
    assert grafikk._join(["Ap", "H", "V"]) == "Ap, H og V"
    assert grafikk._join(["Ap"]) == "Ap"
    assert grafikk._join([]) == ""
    assert grafikk._join(["14", "11", "11"]) == "14, 11 og 11"  # never de-duplicated


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


def test_map_halo_validates():
    # paint-order is not a valid SVG attribute in HTML: it lives in style
    svg = grafikk.bydelskart(["Fana"])["svg"]
    for el in parse(svg).iter():
        assert "paint-order" not in el.attrib
    assert 'style="paint-order:stroke"' in svg


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


def test_million_unit_agrees_with_the_number():
    text = "For å kompensere foreslår Byrådet å øke ekstratilskuddet med 2,6 million kroner, til 3,6 millioner kroner."
    f = grafikk.belop_i_kontekst(prot(), sf((text, 1)))
    joined = " ".join(texts(f["svg"]))
    assert "Herav økning: 2,6 millioner kroner" in joined and "million kroner" not in joined
    assert f["belop"]["del"] == "2,6 millioner kroner" and "2,6 millioner kroner" in f["alt"]
    assert grafikk._unit("1 million kroner") == "1 million kroner"
    assert grafikk._unit("1,5 milliard kroner") == "1,5 milliarder kroner"
    assert grafikk._unit("31,4 mill.") == "31,4 mill."
    assert grafikk._unit("3,6 millioner kroner") == "3,6 millioner kroner"
    assert grafikk._unit("kroner 11 millionar") == "kroner 11 millionar"


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
