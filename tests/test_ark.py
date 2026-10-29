import csv

from tingposten import ark

ITEM = {
    "id": "a",
    "utvalgId": "571424",
    "moteDato": "2026-09-23",
    "seq": 5,
    "kategori": "Saker til behandling",
    "tittel": "Områdereguleringsplan for Eksempelområdet",
    "referansenr": "258/26",
    "innstillingFilId": "111",
    "innstillingAlternativBeskrivelse": "Framstilling-x",
    "vedtakFilId": "222",
    "vedtakAlternativBeskrivelse": "Protokoll-x",
}
NOISE = {**ITEM, "id": "b", "seq": 1, "kategori": "Innkalling", "tittel": "Innkalling", "referansenr": None}


def vote(forslag, resultat, stemmer=None, enstemmig=False):
    return {"forslag": forslag, "fra": [], "fremsatt_av": None, "resultat": resultat,
            "enstemmig": enstemmig, "stemmer": stemmer, "partier": [], "tekst": ""}


SPLIT = {
    "talere": ["A (H)", "B (AP)", "C (R)"],
    "voteringer": [vote("Innstillingen", "falt", 31), vote("Alternativforslag", "vedtatt", 36)],
    "til_stede": 67,
    "enstemmig": False,
    "innstilling_falt": True,
    "vedtak": "1. Planen vedtas. 2. Kommunen bevilger 12 mill. kroner til tiltaket.",
    "vedtak_side": 1,
}
SAKSFRAMLEGG = {
    "belop": [{"tekst": "2 mill.", "kroner": 2e6, "side": 1}, {"tekst": "69 mill.", "kroner": 69e6, "side": 3}],
    "bydeler": ["Fana", "Ytrebygda"],
    "hjemler": [{"tekst": "plan- og bygningsloven § 12-12", "side": 2}],
    "hva_saken_gjelder": "Saken gjelder en plan. Den omfatter to bydeler. Tredje setning.",
}


def test_votering_variants():
    assert ark.votering({}) == ""
    assert ark.votering({"voteringer": [vote("Innstillingen", "vedtatt", enstemmig=True)], "enstemmig": True}) == "enstemmig"
    assert ark.votering(SPLIT) == "36–31"
    assert ark.votering({**SPLIT, "til_stede": None}) == "vedtatt med 36"
    unanimous_but_addition_fell = {
        "voteringer": [vote("Innstillingen", "vedtatt", enstemmig=True), vote("Tilleggsforslag", "falt", 32)],
        "enstemmig": False,
    }
    assert ark.votering(unanimous_but_addition_fell) == "enstemmig, forslag falt"


def test_row_prefills_signals():
    r = ark.row(ITEM, SPLIT, SAKSFRAMLEGG)
    assert r["beløp i vedtaket (MNOK)"] == "12,0"
    assert r["største beløp i saksframlegg (MNOK)"] == "69,0"
    assert r["beløp i teksten"] == "vedtak: 12 mill. kroner | saksframlegg s. 3: 69 mill."
    assert r["bydeler"] == "Fana, Ytrebygda"
    assert r["tung sakstype"] == "ja"
    assert r["innstilling falt"] == "ja"
    assert r["største tapende forslag"] == 31
    assert r["antall talere"] == 3
    assert r["hva saken gjelder"] == "Saken gjelder en plan. Den omfatter to bydeler."
    assert r["protokoll"].endswith("/fil/bk360/222/Protokoll-x")
    assert r["DIN VURDERING (forside/kort/støy)"] == ""


def test_build_skips_noise_and_tolerates_missing_or_broken_files(tmp_path):
    (tmp_path / "571424" / "2026-09-23").mkdir(parents=True)
    (tmp_path / "571424" / "2026-09-23" / "258-26_protokoll.pdf").write_bytes(b"%PDF")
    # saksframlegg missing on disk; protokoll parser raises → empty signals, no crash
    def broken(path):
        raise ValueError("bad pdf")
    rows = ark.build([ITEM, NOISE], tmp_path, parse_protokoll=broken, parse_saksframlegg=broken)
    assert [r["nr"] for r in rows] == ["258/26"]
    assert rows[0]["votering"] == ""


def test_write_csv_semicolon_bom(tmp_path):
    path = tmp_path / "ark.csv"
    ark.write_csv([ark.row(ITEM, SPLIT, SAKSFRAMLEGG)], path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["votering"] == "36–31"


def test_summary_falls_back_to_untitled_first_section():
    s = {"hva_saken_gjelder": "", "seksjoner": [{"overskrift": "", "tekst": "Interpellasjonen spør om X. Mer.", "side": 1}]}
    assert ark.row(ITEM, {}, s)["hva saken gjelder"] == "Interpellasjonen spør om X. Mer."
