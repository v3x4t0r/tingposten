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



def test_split_bar_says_for_and_against_with_parties():
    t = texts(grafikk.stemmediagram(SPLIT)["svg"])
    assert t[0] == "Vedtatt: Alternativforslag fra H, FrP og BL"
    assert t[1:3] == ["36", "31"]
    # the parties as colour chips, in protokoll order, independents counted last
    assert t[3:11] == ["For:", "BL", "FrP", "H", "INP", "KrF", "PP", "Sp"]
    assert t[11:] == ["Mot:", "Ap", "MDG", "R", "SV", "V", "én uavhengig", "(stemte for innstillingen)"]
    svg = grafikk.stemmediagram(SPLIT)["svg"]
    assert 'fill="var(--tp-p-h, #4aa8df)"' in svg and 'fill="var(--tp-p-ap, #da4b0d)"' in svg
    assert "url(" not in svg  # no clipPath: the ends are shaped paths
    # the innstilling is the against-side of the bar, not a bar of its own
    assert not any(x.startswith("Falt:") for x in t)
    joined = " ".join(t)
    assert "én uavhengig" in joined and "UAV" not in joined
    assert "KrF" in joined and "KRF" not in joined
    assert grafikk.stemmediagram(SPLIT)["stemmer"] == {"for": 36, "mot": 31}


def test_against_is_attendance_minus_for_when_stated():
    p = prot(vote("Innstillingen", "vedtatt", 37, ["H", "FRP"]), til_stede=67)
    f = grafikk.stemmediagram(p)
    assert texts(f["svg"]) == ["Vedtatt: Innstillingen", "37", "30", "For:", "H", "FrP", "Mot: partiene er ikke oppgitt i protokollen"]
    assert f["stemmer"] == {"for": 37, "mot": 30} and f["alt"].startswith("Avstemning i Bergen bystyre: 37 mot 30. ")


def test_sequential_votes_are_not_paired():
    # 58 + 26 is more than the house: two votes, so only the votes for are known (393/26)
    p = prot(vote("Alternativforslag", "falt", 26, ["AP", "SV"], fra=["AP"]), vote("Innstillingen", "vedtatt", 58, ["H", "FRP"]))
    f = grafikk.stemmediagram(p)
    t = texts(f["svg"])
    assert t[:6] == ["Vedtatt: Innstillingen", "58", "For:", "H", "FrP", "Mot: antallet er ikke oppgitt i protokollen"]
    assert "Falt: Alternativforslag fra Ap" in t and "26" in t
    assert f["stemmer"] == {"for": 58, "mot": None} and " mot " not in f["alt"]
    # a pair that fills the house is one vote: the fallen side is named as against
    p = prot(vote("Innstillingen", "vedtatt", 36, ["H"]), vote("Alternativforslag", "falt", 31, ["AP"], fra=["AP"]))
    t = texts(grafikk.stemmediagram(p)["svg"])
    assert t == ["Vedtatt: Innstillingen", "36", "31", "For:", "H", "Mot:", "Ap", "(stemte for alternativforslag fra Ap)"]


def test_rest_of_a_party_counted_in_part():
    p = prot(vote("Alternativforslag", "falt", 26, ["AP", "3V", "SV"], fra=["AP"]),
             vote("Innstillingen", "vedtatt", 41, ["H", "FRP", "V", "KRF"]), til_stede=67)
    t = " ".join(texts(grafikk.stemmediagram(p)["svg"]))
    assert "For: H FrP resten av V KrF" in t and "Mot: Ap 3 fra V SV (stemte for alternativforslag fra Ap)" in t


def test_party_segments_follow_seats_or_the_stated_number():
    assert grafikk._segments(["H", "3V", "UAV A", "UAV B", "X"], {"V": 3}) == \
        [("H", "H", 18), ("3 fra V", "V", 3), ("X", "X", 1), ("to uavhengige", "UAV", 2)]
    assert grafikk._segments(["REST V", "KRF"], {"V": 1}) == [("resten av V", "V", 2), ("KrF", "KRF", 2)]
    assert sum(grafikk.SEATS.values()) == 67
    # the for-side of SPLIT: H's segment is 18/37 of the side, with a 2 px paper gap to its neighbours
    svg = grafikk.stemmediagram(SPLIT)["svg"]
    ws = [float(m) for m in re.findall(r'<path d="M[\d.]+,[\d.]+h([\d.]+)v18h', svg)]
    assert len(ws) >= 5 and max(ws) == pytest.approx((36 / 67 * grafikk.W - 1) * 18 / 37 - 2, abs=0.2)  # 36+31 fill the house: 1 px to the paper gap


def test_no_figure_for_unanimous_or_single_vote():
    assert grafikk.stemmediagram(prot(vote("Innstillingen", "vedtatt", enstemmig=True))) is None
    assert grafikk.stemmediagram(prot(vote("Alternativforslag", "vedtatt", 35, ["H"]))) is None
    assert grafikk.stemmediagram(prot()) is None



def test_merknader_never_count_and_one_tillegg_is_no_figure():
    p = prot(
        vote("Innstillingen", "vedtatt", enstemmig=True),
        vote("Tilleggsforslag", "falt", 32, ["AP"]),
        vote("Merknad", "vedtatt", 36, ["H"]),
        vote("Merknad", "falt", 31, ["AP"]),
    )
    assert grafikk.stemmediagram(p) is None


def test_contested_tilleggsforslag_get_their_own_group():
    # 384/26: the innstilling unanimous, the fight over tilleggsforslag
    p = prot(vote("Innstillingen", "vedtatt", enstemmig=True),
             vote("Tilleggsforslag", "vedtatt", 35, ["AP", "SV"], fra=["SV"]),
             vote("Tilleggsforslag", "vedtatt", 36, ["AP", "SV", "KRF"], fra=["AP"]),
             vote("Tilleggsforslag", "falt", 5, ["R"], fra=["R"]), vote("Tilleggsforslag", "falt", 28, ["H", "FRP"], fra=["H"]))
    f = grafikk.stemmediagram(p)
    t = texts(f["svg"])
    assert t[0] == "Innstillingen ble enstemmig vedtatt." and t[1] == "TILLEGGSFORSLAG"
    assert labels(f["svg"]) == ["Vedtatt: Tilleggsforslag fra Ap", "Vedtatt: Tilleggsforslag fra SV",
                                "Falt: Tilleggsforslag fra H", "Falt: Tilleggsforslag fra R"]
    assert f["stemmer"] is None and "tilleggsforslag: Tilleggsforslag fra Ap fikk 36 stemmer" in f["alt"]
    # with a contested main vote the tilleggsforslag come under it (393/26: the 63-vote proposal is shown)
    p = prot(vote("Innstillingen", "vedtatt", 58, ["H"]), vote("Alternativforslag", "falt", 26, ["AP"], fra=["AP"]),
             vote("Tilleggsforslag", "vedtatt", 63, ["H", "AP"], fra=["H"]))
    t = texts(grafikk.stemmediagram(p)["svg"])
    assert t.index("TILLEGGSFORSLAG") > t.index("Falt: Alternativforslag fra Ap")
    assert t[t.index("TILLEGGSFORSLAG") + 1:][:2] == ["Vedtatt: Tilleggsforslag fra H", "63"]


def test_tillegg_counts_when_there_is_no_innstilling():
    p = prot(vote("Tilleggsforslag", "falt", 32, ["AP"]), vote("Alternativforslag", "vedtatt", 34, ["H"]),
             tittel="Interpellasjon nr. 30")
    assert {t for t in texts(grafikk.stemmediagram(p)["svg"]) if t.isdigit()} == {"32", "34"}


def test_eventuelt_is_skipped():
    assert grafikk.stemmediagram({**SPLIT, "tittel": "Eventuelt bystyret 23.09.2026"}) is None



def test_more_than_four_proposals_are_summarised_by_name_and_count():
    p = prot(vote("Innstillingen", "vedtatt", 56, ["H"]), *[vote("Alternativforslag", "falt", n, ["R"], fra=["R"]) for n in (20, 19, 8, 3)])
    t = texts(grafikk.stemmediagram(p)["svg"])
    assert {x for x in t if x.isdigit()} == {"56", "20", "19", "8"}
    assert t[-1] == "Falt også: alternativforslag fra R (3 stemmer)."


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
        elif tag == "text" and cur is not None and not "".join(el.itertext()).isdigit():
            cur += " " + "".join(el.itertext())
        elif tag in ("path", "rect") and cur is not None:
            out.append(cur)
            cur = None
    return out



def test_fallen_proposals_always_get_bars():
    f = grafikk.stemmediagram(SAK_266)
    got = labels(f["svg"])
    assert [x.split(":")[0] for x in got] == ["Vedtatt", "Vedtatt", "Falt", "Falt", "Falt"]
    assert "Falt: Utsettelsesforslag fra MDG" in got
    assert got[-1] == "Falt: Tilleggsforslag fra R"
    # five adopted votes with distinct counts and three that fell: the split bar, then one adopted and two fallen
    many = prot(*[vote(f"Innstillingens punkt {n}", "vedtatt", c, ["H", f"P{n}"]) for n, c in enumerate((60, 55, 50, 45, 40))],
                *[vote("Alternativforslag", "falt", c, ["R"], fra=[p]) for c, p in ((20, "R"), (12, "SV"), (9, "MDG"))])
    t = texts(grafikk.stemmediagram(many)["svg"])
    assert {x for x in t if x.isdigit()} == {"60", "55", "20", "12"}
    assert " ".join(t).endswith(" Vedtatt også: innstillingens punkt 2 (50 stemmer), innstillingens punkt 3 (45 stemmer) "
                                "og innstillingens punkt 4 (40 stemmer). Falt også: alternativforslag fra MDG (9 stemmer).")


def test_identical_adopted_votes_merge_into_one_bar():
    f = grafikk.stemmediagram(SAK_266)
    got = labels(f["svg"])
    assert got[0] == "Vedtatt: Innstillingen – punkt 1, punkt 2 og punkt 4, avsnitt 1"
    assert [x for x in texts(f["svg"]) if x.isdigit()] == ["54", "12", "65", "14", "11", "14"]
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
    p = prot(vote("Innstillingen", "vedtatt", 46, ["H"]), vote("Alternativforslag", "falt", 20, ["AP"], fra=["AP", "MDG"]),
             vote("Alternativforslag", "falt", 20, ["AP"], fra=["AP", "MDG", "V"]))
    assert len(labels(grafikk.stemmediagram(p)["svg"])) == 3


def test_alt_text_lists_every_vote():
    alt = grafikk.stemmediagram(SAK_266)["alt"]
    assert alt.startswith("Avstemning i Bergen bystyre: 54 mot 12. Innstillingens punkt 3, c–d fikk 65 stemmer")
    assert "Innstillingens punkt 4, avsnitt 2 og 3 fikk 58 stemmer" in alt  # the one only in the rest line
    assert "punkt 1, punkt 2 og punkt 4, avsnitt 1 fikk 54 stemmer hver (Ap, BL, FrP, H, KrF, PP, R, Sp, én uavhengig og V) og ble vedtatt" in alt
    assert "Utsettelsesforslag fra MDG fikk 14 stemmer (MDG, R og SV) og falt" in alt
    assert "Utsettelsesforslag og alternativforslag fra SV fikk 11 stemmer hver (MDG og SV) og falt" in alt
    assert alt.endswith("; tilleggsforslag: Tilleggsforslag fra R fikk 14 stemmer (MDG, R og SV) og falt.")



def test_bars_under_the_split_follow_the_count_not_the_outcome():
    p = prot(vote("Innstillingen", "vedtatt", 30, ["H"]), vote("Alternativforslag", "falt", 33, ["AP"], fra=["AP"]),
             vote("Alternativforslag", "falt", 25, ["SV"], fra=["SV"]), vote("Innstillingens punkt 9", "vedtatt", 28, ["H"]))
    svg = grafikk.stemmediagram(p)["svg"]
    assert labels(svg) == ["Vedtatt: Innstillingen", "Falt: Alternativforslag fra Ap", "Vedtatt: Innstillingens punkt 9",
                           "Falt: Alternativforslag fra SV"]
    # one weight for every label; the outcome word is set alike for both
    root = parse(svg)
    heads = [t for t in root.iter(NS + "text") if "".join(t.itertext()).startswith(("Vedtatt:", "Falt:"))]
    assert {t.get("font-weight") for t in heads} == {"400"}
    assert {(s.text, s.get("font-weight")) for t in heads for s in t.iter(NS + "tspan")} == {("Falt:", "600"), ("Vedtatt:", "600")}



def test_rest_line_names_every_proposal_with_its_count():
    def v(res, n, fra=("R",)):
        return vote("Alternativforslag", res, n, ["R"], fra=fra)
    assert grafikk._rest([v("vedtatt", 54, ()), v("falt", 14), v("falt", 11, ("SV",)), v("falt", 11, ("MDG",))]) == \
        "Vedtatt også: alternativforslag (54 stemmer). Falt også: alternativforslag fra R (14 stemmer), " \
        "alternativforslag fra SV (11 stemmer) og alternativforslag fra MDG (11 stemmer)."
    assert grafikk._rest([v("falt", 1)]) == "Falt også: alternativforslag fra R (1 stemme)."
    assert "Vedtatt også: innstillingens punkt 4, avsnitt 2 og 3 (58 stemmer)." in " ".join(texts(grafikk.stemmediagram(SAK_266)["svg"]))


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
    # there was an innstilling, so the tilleggsforslag goes in its own group
    p = prot(vote("Innstillingen fikk ingen stemmer, og var dermed falt. Alternativforslag punkt 1-3", "vedtatt", 53, ["H"], fra=["H"]),
             vote("Alternativforslag", "falt", 14, ["SV"], fra=["SV"]), vote("Tilleggsforslag", "falt", 32, ["AP"], fra=["AP"]))
    t = texts(grafikk.stemmediagram(p)["svg"])
    assert t[:8] == ["Vedtatt: Alternativforslag punkt 1–3 fra H", "53", "14", "For:", "H", "Mot:", "SV",
                     "(stemte for alternativforslag fra SV)"]
    assert t[8:10] == ["TILLEGGSFORSLAG", "Falt: Tilleggsforslag fra Ap"]


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
