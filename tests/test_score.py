import json

import pytest

from tingposten import pdf
from tingposten import score

BYSTYRET = "571424"


def item(tittel, **kw):
    return {"id": tittel, "utvalgId": BYSTYRET, "moteDato": "2026-09-23", "seq": 5,
            "kategori": "Saker til behandling", "tittel": tittel, "referansenr": "100/26", **kw}


def vote(forslag, resultat, stemmer=None, enstemmig=False):
    return {"forslag": forslag, "fra": [], "fremsatt_av": None, "resultat": resultat,
            "enstemmig": enstemmig, "stemmer": stemmer, "partier": [], "tekst": ""}


def prot(vedtak="Bystyret vedtar saken.", votes=None, til_stede=None, falt=False):
    votes = votes if votes is not None else [vote("Innstillingen", "vedtatt", enstemmig=True)]
    return {"vedtak": vedtak, "voteringer": votes, "til_stede": til_stede,
            "enstemmig": bool(votes) and all(v["enstemmig"] for v in votes), "innstilling_falt": falt}


def sig(tittel="Sak", p=None, s=None, **kw):
    return score.signals(item(tittel, **kw), p if p is not None else prot(), s or {})


# --- beløp -----------------------------------------------------------------

def test_amount_comes_from_binding_vedtak_not_merknad_or_parenthesis():
    vedtak = ("1. Prosjektet gjennomføres med ramme på 69 mill. (Opprinnelig var det 90 mill.)\n"
              "Merknad 1 Bystyret viser til at sektoren har 33 mrd. kr i underskudd.")
    s = sig("Gjennomføringsvedtak for idrettshall", prot(vedtak))
    assert s["belop_kroner"] == 69e6


def test_amount_log_scale():
    parts = lambda kr: score._parts({"belop_kroner": kr, "vedtak_fattet": True})["belop"]
    assert parts(None) == 0.0
    assert parts(1e6) == 0.0
    assert parts(1e9) == pytest.approx(1.0)
    assert parts(5e10) == 1.0
    assert 0.6 < parts(69e6) < 0.62


def test_kroner_badge():
    assert score._kroner(866.4e6) == "866 MNOK"
    assert score._kroner(2e6) == "2 MNOK"
    assert score._kroner(1.5e6) == "1,5 MNOK"
    assert score._kroner(15e9) == "15 mrd. kr"
    assert score._kroner(2.7e9) == "2,7 mrd. kr"


# --- rekkevidde --------------------------------------------------------------

def test_reach_whole_city_from_all_bydeler_or_wording():
    assert sig(s={"bydeler": list(score.BYDELER)})["hele_byen"]
    assert sig("Prinsipper for ny fordelingsmodell for bergensskolen")["hele_byen"]
    assert sig("Revidert budsjett 2026")["hele_byen"]


def test_reach_bergen_kommune_in_title_is_kommune_wide_not_city_wide():
    s = sig("Eiendomsstrategi for Bergen kommune")
    assert not s["hele_byen"] and s["hele_kommunen"]
    assert score._parts(s)["rekkevidde"] == 0.6


def test_reach_single_property_is_narrow():
    s = sig("Ytrebygda, gnr. 111, bnr. 83 m.fl. – endring av plan", s={"bydeler": ["Ytrebygda"]})
    assert s["smal"]
    assert score._parts(s)["rekkevidde"] == 0.2


def test_reach_by_number_of_bydeler():
    one = score._parts(sig(s={"bydeler": ["Fana"]}))["rekkevidde"]
    two = score._parts(sig(s={"bydeler": ["Fana", "Årstad"]}))["rekkevidde"]
    assert 0 < one < two < 1


# --- juridisk vekt / varighet -----------------------------------------------

@pytest.mark.parametrize("tittel, label", [
    ("Søknad om fritak fra verv og nyvalg", "valg til verv"),
    ("Krav om lovlighetskontroll av sak 152/26 - Revidert budsjett 2026", "lovlighetskontroll"),
    ("Sluttbehandling av konsekvensutredning og valg av trasé for bybane", "kommunedelplan"),
    ("Fana, reguleringsplan for gang- og sykkelveg", "reguleringsplan"),
    ("Oppheving av eldre mindre reguleringsplaner", "opprydding i planer"),
    ("Opsjonsavtale om salg av tomter", "eierskap/salg"),
    ("Interpellasjon nr. 9 om parkering", "interpellasjon"),
])
def test_case_type_from_title(tittel, label):
    assert sig(tittel)["sakstype"] == label


def test_case_type_from_vedtak_when_title_is_silent():
    p = prot("I medhold av plan- og bygningsloven § 12-12 vedtas følgende forslag til detaljregulering:")
    assert sig("Sluttbehandling, E39 tunnelramper", p)["sakstype"] == "reguleringsplan"


def test_til_orientering_caps_legal_weight():
    p = prot("Bystyret tar strategien til orientering.")
    assert sig("Strategi for noe", p)["juridisk_vekt"] <= 0.3


def test_no_decision_zeroes_substance():
    s = sig("Interpellasjon om bypark", prot("Det ble ikke fattet vedtak i saken.",
                                             votes=[vote("Alternativforslag", "falt", 7)]))
    assert not s["vedtak_fattet"]
    parts = score._parts(s)
    assert parts["juridisk_vekt"] == parts["rekkevidde"] == parts["endelighet"] == 0.0


# --- uenighet / innstilling ---------------------------------------------------

def test_unanimous_is_no_disagreement():
    s = sig()
    assert s["uenighet"] == 0.0 and s["split"] is None


def test_split_uses_stated_til_stede():
    p = prot(votes=[vote("Innstillingen", "vedtatt", 54)], til_stede=66)
    s = sig(p=p)
    assert s["split"] == "54–12"
    assert s["uenighet"] == pytest.approx(1 - 42 / 66)


def test_split_pairs_competing_main_proposals():
    p = prot(votes=[vote("Innstillingen", "falt", 31), vote("Alternativforslag", "vedtatt", 36)], falt=True)
    s = sig(p=p)
    assert s["split"] == "36–31"
    assert s["uenighet"] > 0.9


def test_tilleggsforslag_counts_are_not_paired_into_one_vote():
    p = prot(votes=[vote("Innstillingen", "vedtatt", enstemmig=True),
                    vote("Tilleggsforslag", "vedtatt", 55), vote("Tilleggsforslag", "falt", 32)])
    s = sig(p=p)
    assert s["split"] == "55 stemmer for"  # never "55–32"


def test_merknad_votes_are_ignored():
    p = prot(votes=[vote("Innstillingen", "vedtatt", enstemmig=True), vote("Merknad", "vedtatt", 36)])
    assert sig(p=p)["uenighet"] == 0.0


def test_innstilling_fell_whole_vs_partly():
    whole = prot(votes=[vote("Innstillingen", "falt", 31), vote("Alternativforslag", "vedtatt", 36)], falt=True)
    partly = prot(votes=[vote("Innstillingens pkt. 1 og 2", "vedtatt", enstemmig=True),
                         vote("Innstillingens pkt. 3", "falt", 33)], falt=True)
    assert (sig(p=whole)["innstilling_falt"], sig(p=whole)["innstilling_delvis_falt"]) == (True, False)
    assert (sig(p=partly)["innstilling_falt"], sig(p=partly)["innstilling_delvis_falt"]) == (False, True)


def test_endelighet_by_organ():
    assert sig()["endelighet"] == 1.0
    assert sig(utvalgId="900101")["endelighet"] < sig(utvalgId="900102")["endelighet"] < 1.0


# --- score ---------------------------------------------------------------------

def test_score_range_and_reasons():
    p = prot("Planen vedtas. Kommunen bevilger 120 mill. kroner.",
             votes=[vote("Innstillingen", "falt", 31), vote("Alternativforslag", "vedtatt", 36)], falt=True)
    s = sig("Fana, reguleringsplan for ny bydelspark", p, {"bydeler": ["Fana"]})
    value, reasons = score.score(s)
    assert 0 <= value <= 100
    assert len(reasons) <= 3
    # "innstillingen falt" now weighs less than the plan, the money and the close vote
    assert reasons == ["reguleringsplan", "120 MNOK", "36–31"]
    assert all(r for r in reasons)


def test_procedural_case_scores_low_without_badges():
    value, reasons = score.score(sig("Søknad om fritak fra verv"))
    assert value <= 10
    assert reasons == []


def test_weak_signals_are_not_shown_as_badges():
    # an interpellasjon has a little durability but must not be "langsiktig"
    _, reasons = score.score(sig("Interpellasjon om skolevei"))
    assert "langsiktig" not in reasons


def test_missing_model_signals_are_left_out_not_counted_as_zero():
    s = sig("Fana, reguleringsplan for park", s={"bydeler": ["Fana"]})
    base, _ = score.score(s)
    assert score.score({**s, "angar": 5})[0] > base > score.score({**s, "angar": 1})[0]
    assert score.score({**s, "konkret": 5})[0] > base > score.score({**s, "konkret": 1})[0]


def test_model_signals_carry_half_the_weight():
    w = score.WEIGHTS
    assert w["angar"] + w["konkret"] == sum(w.values()) / 2
    assert "hverdag" not in w


def test_model_signal_badges_only_when_strong():
    s = sig("Strategi for noe")
    assert "angår mange" in score.score({**s, "angar": 5})[1]
    assert "konkret endring" in score.score({**s, "konkret": 4})[1]
    assert "angår mange" not in score.score({**s, "angar": 3})[1]


def test_model_signal_values_are_validated():
    good = {"angar": {"verdi": 4, "begrunnelse": "x"}, "konkret": {"verdi": 2, "begrunnelse": "y"}}
    s = score.signals(item("Sak"), prot(), {}, good)
    assert (s["angar"], s["konkret"]) == (4, 2)
    for bad in ({"verdi": 6}, {"verdi": "4"}, {"verdi": True}, "4", None):
        assert score.signals(item("Sak"), prot(), {}, {"angar": bad})["angar"] is None


# --- status ------------------------------------------------------------------

def test_sent_back_is_held_back_and_says_so():
    p = prot("1. Reguleringsplanforslaget sendes tilbake for ytterligere bearbeiding.",
             votes=[vote("Alternativforslag", "vedtatt", 35)])
    s = sig("Fana, reguleringsplan for sykkelvei", p, {"bydeler": ["Fana"]})
    decided = sig("Fana, reguleringsplan for sykkelvei",
                  prot(votes=[vote("Alternativforslag", "vedtatt", 35)]), {"bydeler": ["Fana"]})
    assert s["status"] == "sendt_tilbake"
    value, reasons = score.score(s)
    assert reasons[0] == "ikke avgjort ennå" and len(reasons) <= 3
    assert value == pytest.approx(score.score(decided)[0] * score.IKKE_AVGJORT_FAKTOR, abs=1)


def test_election_scores_zero_even_with_model_signals():
    s = sig("Søknad om fritak fra verv og valg av nytt varamedlem")
    assert s["status"] == "valg"
    assert score.score({**s, "angar": 5, "konkret": 5}) == (0, [])


# --- debatt --------------------------------------------------------------------

def test_debatt_grows_with_fallen_proposals_and_votes():
    calm = score._debatt({"forslag_falt": 0, "voteringer": 1})
    some = score._debatt({"forslag_falt": 2, "voteringer": 3})
    many = score._debatt({"forslag_falt": 6, "voteringer": 10})
    assert calm == 0.0 < some < many < 1.0


def test_interpellasjon_starts_at_half():
    assert score._debatt({"interpellasjon": True, "voteringer": 0}) == 0.5


def test_debatt_badge():
    p = prot(votes=[vote("Innstillingen", "vedtatt", enstemmig=True)] + [vote("Tilleggsforslag", "falt", 10)] * 4)
    s = sig("Strategi for noe", p)
    assert s["forslag_falt"] == 4
    assert score._badge("debatt", s) == "4 forslag falt"
    assert score._badge("debatt", sig("Interpellasjon nr. 3 om bypark")) == "interpellasjon"


def test_custom_weights():
    s = sig("Revidert budsjett 2026")
    only_reach = {k: (1 if k == "rekkevidde" else 0) for k in score.WEIGHTS}
    assert score.score(s, only_reach)[0] == 100


# --- rank ---------------------------------------------------------------------

def test_rank_excludes_noise_and_sorts(tmp_path):
    items = [
        item("Fana, reguleringsplan for park", referansenr="2/26"),
        item("Søknad om fritak fra verv", referansenr="3/26"),
        {**item("Innkalling Bergen bystyre"), "kategori": "Innkalling", "referansenr": None},
    ]
    for it in items[:2]:
        for kind in ("protokoll", "saksframlegg"):
            path = tmp_path / pdf.filename(it, kind)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF")

    def fake_prot(path):
        return prot()

    def fake_saks(path):
        return {"bydeler": ["Fana"]} if "2-26" in path.name else {}
    ranked = score.rank(items, tmp_path, parse_protokoll=fake_prot, parse_saksframlegg=fake_saks,
                        data_dir=tmp_path)
    assert [r["nr"] for r in ranked] == ["2/26", "3/26"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert set(ranked[0]) == {"nr", "tittel", "score", "grunner", "signaler", "status", "modell"}
    assert ranked[0]["status"] == {"kode": "vedtatt", "tekst": "Vedtatt"}
    assert ranked[1]["status"]["kode"] == "valg" and ranked[1]["score"] == 0
    assert ranked[0]["modell"] == {}


def test_rank_reads_model_signals(tmp_path):
    it = item("Fana, reguleringsplan for park", referansenr="2/26")
    signal = {"angar": {"verdi": 5, "begrunnelse": "Mange bruker parken."},
              "konkret": {"verdi": 4, "begrunnelse": "Ny park."}}
    path = tmp_path / "signaler" / f"{pdf.key(it)}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(signal))
    with_model = score.rank([it], tmp_path, parse_protokoll=lambda p: prot(), parse_saksframlegg=lambda p: {},
                            data_dir=tmp_path)[0]
    without = score.rank([it], tmp_path / "x", parse_protokoll=lambda p: prot(), parse_saksframlegg=lambda p: {},
                         data_dir=tmp_path / "x")[0]
    assert with_model["modell"] == signal
    assert with_model["score"] > without["score"]
    assert "angår mange" in with_model["grunner"]


# --- spearman / compare --------------------------------------------------------

def test_spearman():
    assert score.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert score.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert score.spearman([1, 2], [1, 2]) is None
    assert score.spearman([1, 1, 1], [1, 2, 3]) is None
    # ties get average ranks
    assert score.spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.949, abs=1e-3)


def test_compare():
    ranked = [
        {"nr": "a", "tittel": "A", "score": 80, "grunner": ["hele byen"]},
        {"nr": "b", "tittel": "B", "score": 60, "grunner": []},
        {"nr": "c", "tittel": "C", "score": 30, "grunner": []},
        {"nr": "d", "tittel": "D", "score": 10, "grunner": []},
    ]
    ratings = {
        "a": {"vurdering": "forside", "merkbar": 5, "kommentar": "alle merker det"},
        "b": {"vurdering": "stoy", "merkbar": 1, "kommentar": "bare internt"},
        "c": {"vurdering": "kort", "merkbar": 3, "kommentar": ""},
        "d": {"vurdering": "forside", "merkbar": None, "kommentar": "min skole"},
        "zzz": {"vurdering": "forside", "merkbar": 5},  # not in the ranking: ignored
    }
    out = score.compare(ranked, ratings)
    assert out["par"] == 3
    assert out["spearman"] == pytest.approx(0.5)
    assert out["hauger"]["forside"] == {"antall": 2, "snitt": 45.0}
    assert out["hauger"]["stoy"] == {"antall": 1, "snitt": 60.0}
    assert out["forside_i_topp"] == {"n": 2, "treff": 1}
    assert out["forside_men_lav_score"][0]["nr"] == "d"
    assert out["forside_men_lav_score"][0]["kommentar"] == "min skole"
    assert out["stoy_men_hoy_score"][0] == {"nr": "b", "tittel": "B", "score": 60, "grunner": [],
                                             "kommentar": "bare internt"}


def test_compare_without_ratings():
    out = score.compare([{"nr": "a", "tittel": "A", "score": 50, "grunner": []}], {})
    assert out["spearman"] is None
    assert out["forside_i_topp"] == {"n": 0, "treff": 0}
    assert out["hauger"]["forside"]["snitt"] is None


def test_orientering_ranks_like_not_decided():
    s = sig("Revidert budsjett 2026")
    full, _ = score.score({**s, "status": "vedtatt"})
    noted, reasons = score.score({**s, "status": "orientering"})
    assert noted == round(full * score.IKKE_AVGJORT_FAKTOR) or abs(noted - full * score.IKKE_AVGJORT_FAKTOR) <= 1
    assert reasons[0] == "ingen ny beslutning"
