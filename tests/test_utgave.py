"""The printed edition (utgave.py) and the short addresses it prints (bygg.kortlenker).

Most tests need no browser. The ones that build a whole edition need Chromium
through Playwright (uv run --extra utgave) and are skipped without it."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from tingposten import bygg, config, grafikk, score, utgave

BYSTYRET = "571424"
BYRADET = "900102"
DATO = "2026-09-23"
MOTE = f"{BYSTYRET}-{DATO}"

SETNINGER = (
    "Bystyret vedtok at kommunen skal bygge en ny skole i bydelen med plass til 450 elever.",
    "Prosjektet koster 312 millioner kroner og skal stå ferdig i 2029.",
    "Elever og ansatte flytter til midlertidige lokaler mens skolen bygges.",
    "Byrådet mener den gamle bygningen er for dårlig til å pusses opp.",
    "Et forslag om å utsette saken falt med 30 mot 37 stemmer.",
    "Planen skal behandles på nytt dersom kostnadene øker med mer enn ti prosent.",
)


def item(nr, tittel, seq, utvalg=BYSTYRET, dato=DATO, **kw):
    return {"id": f"{utvalg}:{nr}:{dato}", "utvalgId": utvalg, "moteDato": dato, "seq": seq,
            "kategori": "Saker til behandling", "tittel": tittel, "referansenr": nr,
            "innstillingFilId": "111", "innstillingAlternativBeskrivelse": "Framstilling-x",
            "vedtakFilId": "222", "vedtakAlternativBeskrivelse": "Protokoll-x", **kw}


def tekst(i, n):
    return " ".join(SETNINGER[(i + k) % len(SETNINGER)] for k in range(n))


def kort(nr, i, lang=True):
    felt = {"hva_endres": f"Sak {nr} gjelder skole nummer {i}. " + tekst(i, 3 if lang else 1),
            "hvem_pavirkes": f"Endringen i sak {nr} gjelder elevene. " + tekst(i + 1, 2 if lang else 1),
            "hvorfor": f"Begrunnelsen i sak {nr} er plassmangel. " + tekst(i + 2, 2) if lang else "",
            "status": f"Sak {nr} ble vedtatt.", "nar_og_hvor": f"Arbeidet med sak {nr} starter i 2027. " + tekst(i + 3, 1)}
    kilder = [{"felt": k, "dok": "saksframlegg", "side": 2 + j} for j, k in enumerate(felt) if felt[k]]
    kilder.append({"felt": "status", "dok": "protokoll", "side": 1})
    return {"nr": nr, "overskrift": f"Ny skole nummer {i} skal bygges i bydelen", "felt": felt, "kilder": kilder,
            "slettet": [], "avvist": False}


def artikkel(nr, i, tittel, sjekket=True):
    art = {"nr": nr, "avsnitt": [{"tekst": tekst(i + k, 3), "kilder": [{"dok": "saksframlegg", "side": 5 + k}]}
                                 for k in range(4)], "avvist": False, "tittel": tittel}
    if sjekket:
        art["tittel_kilder"] = [{"dok": "saksframlegg", "side": 5}]
    return art


@pytest.fixture
def data_dir(tmp_path):
    """A meeting of 22 saker (four with a background article), an interpellasjon
    that shares its number with a sak, a valg, a sak without a card, and a
    byrådet meeting with the same sak number as one in bystyret."""
    d = tmp_path / "data"
    (d / "agendapunkter").mkdir(parents=True)
    items = [item(f"{200 + i}/26", f"Sak om skole nummer {i}", seq=10 + i) for i in range(22)]
    items.append(item("28/26", "Interpellasjon om skoleveier", seq=1, kategori="Interpellasjon"))
    items.append(item("228/26", "Fritak fra verv som varamedlem", seq=40))
    items.append(item("229/26", "Sak uten kort ennå", seq=41))
    by = [item("200/26", "Byrådssak om skole", seq=1, utvalg=BYRADET, dato="2026-09-24")]
    (d / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in items}))
    (d / "agendapunkter" / f"{BYRADET}.json").write_text(json.dumps({i["id"]: i for i in by}))
    red = d / "redigert" / BYSTYRET / DATO
    red.mkdir(parents=True)
    for i in range(22):
        (red / f"{200 + i}-26.json").write_text(json.dumps(kort(f"{200 + i}/26", i, lang=i < 8)))
    (red / "28-26.json").write_text(json.dumps(kort("28/26", 30, lang=False)))
    (red / "228-26.json").write_text(json.dumps({**kort("228/26", 31, lang=False),
                                                 "overskrift": "Nytt varamedlem i utvalget"}))
    art = d / "artikler-redigert" / BYSTYRET / DATO
    art.mkdir(parents=True)
    for i in range(4):
        (art / f"{200 + i}-26.json").write_text(json.dumps(artikkel(f"{200 + i}/26", i, f"Sjekket bakgrunnstittel {i}")))
    # Checked by nobody: the redaktør didn't keep this headline (no tittel_kilder).
    (art / "204-26.json").write_text(json.dumps(artikkel("204/26", 4, "UKONTROLLERT TITTEL", sjekket=False)))
    by_red = d / "redigert" / BYRADET / "2026-09-24"
    by_red.mkdir(parents=True)
    (by_red / "200-26.json").write_text(json.dumps(kort("200/26", 40)))
    return d


@pytest.fixture(autouse=True)
def fake_rank(monkeypatch):
    def rank(items, pdf_dir, **kw):
        out = []
        for i in items:
            nr = i["referansenr"]
            n = int(nr.split("/")[0])
            r = {"nr": nr, "tittel": i["tittel"], "score": 0 if nr == "228/26" else 100 - (n % 100), "grunner": [],
                 "signaler": {}}
            if nr == "228/26":
                r["status"] = {"kode": "valg", "tekst": "Valg og fritak"}
            out.append(r)
        return sorted(out, key=lambda r: -r["score"])
    monkeypatch.setattr(score, "rank", rank)


@pytest.fixture
def saker(data_dir):
    return bygg.load(data_dir)


def mote(saker):
    return [s for s in saker if s["mote"] == MOTE]


# ------------------------------------------------------------ short links ----

def test_short_ids_keep_the_series_apart(saker):
    ids = {s["nr_etikett"] + "@" + s["utvalg"]: s["kort"] for s in saker}
    assert ids[f"Sak 200/26@{BYSTYRET}"] == "s/bs-200-26"
    assert ids[f"Sak 200/26@{BYRADET}"] == "s/br-200-26"          # byrådet's own number series
    assert ids[f"Interpellasjon 28/26@{BYSTYRET}"] == "s/bs-i28-26"  # interpellasjoner: their own series
    assert bygg.kort_id({"utvalg": BYRADET, "nr": "1037.1/26", "nr_etikett": "Sak 1037.1/26"}) == "br-1037.1-26"
    assert bygg.kort_id({"utvalg": "900101", "nr": "47/26", "nr_etikett": "Sak 47/26"}) == "bi-47-26"
    for s in saker:
        assert re.fullmatch(r"s/[a-z]+-i?[0-9a-z.-]+", s["kort"]), s["kort"]
        assert s["kortlenke"] == config.SITE_URL + s["kort"]


def test_short_ids_are_unique_and_an_old_one_never_changes(saker):
    assert len({s["kort"] for s in saker}) == len(saker)
    # The same number at a later meeting: the earlier sak keeps the plain address.
    tidlig = {**saker[0], "kort": None, "dato": date(2026, 1, 1)}
    sen = {**saker[0], "kort": None, "dato": date(2026, 9, 23), "side": "bergen/sak/x/"}
    bygg.kortlenker([sen, tidlig])
    assert tidlig["kort"] == "s/" + bygg.kort_id(tidlig)
    assert sen["kort"] == f"s/{bygg.kort_id(sen)}-20260923"


def test_redirects_lead_every_short_address_to_its_sak_page(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(config, "STARTSIDE", "bergen/")
    site = tmp_path / "site"
    stats = bygg.build(site, data_dir, idag=date(2026, 10, 1))
    regler = (site / "_redirects").read_text().splitlines()
    assert regler[0] == "/ /bergen/ 302!"
    korte = [r.split() for r in regler if r.startswith("/s/")]
    assert len(korte) == stats["saker"] and len({k[0] for k in korte}) == len(korte)
    root = bygg.urlsplit(bygg._site_url()).path
    for fra, til, kode in korte:
        assert kode == "301" and re.fullmatch(r"/s/[a-z]+-i?[0-9a-z.-]+", fra), (fra, kode)
        assert (site / til.removeprefix(root) / "index.html").exists(), til
    assert ["/s/bs-i28-26", "/bergen/sak/571424/2026-09-23/28-26/", "301"] in korte
    assert ["/s/br-200-26", "/bergen/sak/900102/2026-09-24/200-26/", "301"] in korte


# -------------------------------------------------------------- content ----

def test_only_publishable_saker_go_in_print(saker):
    m = mote(saker)
    venter = next(s for s in m if s["nr"] == "210/26")
    venter["venter_protokoll"] = True  # protokoll on disk is from an earlier meeting
    innhold = utgave.velg(saker, MOTE)
    trykt = innhold["fulle"] + innhold["korte"] + innhold["liste"] + innhold["valg"]
    assert venter not in trykt and innhold["venter"] == [venter]
    assert [s["nr"] for s in innhold["valg"]] == ["228/26"]
    assert [s["nr"] for s in innhold["liste"]] == ["229/26"]   # no card: a plain line only
    assert all(s["har_sammendrag"] for s in innhold["fulle"] + innhold["korte"])
    # The site's front-page order: the best score first
    assert [s["nr"] for s in innhold["fulle"][:3]] == ["200/26", "201/26", "202/26"]
    assert [s["seq"] for s in innhold["korte"]] == sorted(s["seq"] for s in innhold["korte"])
    with pytest.raises(utgave.UtgaveFeil):
        utgave.velg(saker, f"{BYSTYRET}-2026-01-01")


def test_unchecked_background_title_is_never_printed(saker):
    maler = utgave.Maler(utgave.FORMATER["tabloid"])
    m = {s["nr"]: s for s in mote(saker)}
    for nr, tittel, vises in (("200/26", "Sjekket bakgrunnstittel 0", True), ("204/26", "UKONTROLLERT TITTEL", False)):
        s = m[nr]
        v = utgave.varianter(s, maler.fmt, 5)[0]
        html = str(maler.full(s, v)) + str(maler.full(utgave.del_av(s, "bakgrunn"), v))
        assert (tittel in html) is vises, nr
        assert s["artikkel"] and "Bakgrunn" in html  # the article itself is checked and printed


def test_every_story_ends_with_sources_address_and_qr(saker):
    maler = utgave.Maler(utgave.FORMATER["a4"])
    s = next(s for s in mote(saker) if s["nr"] == "200/26")
    for html in (str(maler.full(s, utgave.varianter(s, maler.fmt, 3)[0])), str(maler.kort(s))):
        fot = html[html.index('class="kildefot"'):]
        assert "Kilde:" in fot and '<svg viewBox="0 0' in fot and 'class="qr"' in fot
        assert "tingposten.no/s/</span><wbr><span class=\"nb\">bs-200-26" in fot
    # Full story: every page its text rests on, grouped per document; short story: the dek's.
    assert utgave.kildelinje(s, True) == "Saksframlegg s. 2, 3, 4, 5, 6, 7, 8 · Protokoll s. 1"
    assert utgave.kildelinje(s, False) == "Saksframlegg s. 2"
    assert utgave.qr_svg(s["kortlenke"]) == utgave.qr_svg("https://tingposten.no/s/bs-200-26")


def test_print_is_ink_only():
    """Black, white and greys: no party colours, nothing that tells winners from losers by colour."""
    css = (utgave.MAL / "utgave.css").read_text() + (utgave.MAL / "dokument.html").read_text()
    farger = re.findall(r"#[0-9a-fA-F]{3,8}\b", css) + list(utgave.FARGER.values())
    assert farger
    for f in farger:
        h = f.lstrip("#")
        h = "".join(c * 2 for c in h) if len(h) == 3 else h[:6]
        assert h[0:2] == h[2:4] == h[4:6], f
    assert not re.search(r"\b(rgb|rgba|hsl|hsla)\(", css)
    assert not re.search(r":\s*(red|green|blue|orange|yellow|purple|navy|teal)\b", css)
    def stemme(forslag, resultat, stemmer, partier):
        return {"forslag": forslag, "fra": [], "fremsatt_av": None, "resultat": resultat, "enstemmig": False,
                "stemmer": stemmer, "partier": partier, "tekst": ""}
    proto = {"tittel": "Reguleringsplan", "organ": "Bergen bystyre", "til_stede": 67, "vedtak": "", "vedtak_side": 1,
             "voteringer": [stemme("Innstillingen", "vedtatt", 40, ["AP", "H"]),
                            stemme("Alternativforslag", "falt", 27, ["FRP"])]}
    figurer = [f for f in (grafikk.stemmediagram(proto), grafikk.bydelskart(["Fana"])) if f]
    assert figurer
    for fig in figurer:
        svg = str(utgave.figur_svg(fig["svg"]))
        assert "var(" not in svg and "#2a78d6" not in svg
        for f in re.findall(r"#[0-9a-fA-F]{6}\b", svg):
            assert f[1:3] == f[3:5] == f[5:7], f
        assert not re.search(r"<text[^>]*stroke=", svg)  # no halo text: Chromium makes it Type 3


def test_colophon(monkeypatch):
    monkeypatch.setattr(config, "KONTAKT_EPOST", "tingposten@freetime.no")
    maler = utgave.Maler(utgave.FORMATER["tabloid"])
    tekst = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(maler.kolofon())))
    assert ("Utgitt av RO-Studio AS (org.nr. 938 625 999) · Ansvarlig redaktør: Henrik Midttun · "
            "tingposten@freetime.no · tingposten.no") in tekst
    assert "Tingposten utgis av RO-Studio AS, som også står bak Freetime (freetime.no)." in tekst
    slik = re.sub(r"<[^>]+>", "", str(maler.slik()))
    assert "faktasjekker" in slik and "kommunens dokumenter som gjelder" in slik
    rettelser = re.sub(r"<[^>]+>", "", str(maler.rettelser_boks()))
    assert "Feil i en tidligere utgave rettes her." in rettelser and "Meld feil: tingposten@freetime.no" in rettelser


def test_statement_sections_are_the_approved_text():
    deler = {d["tittel"]: d["avsnitt"] for d in utgave.erklaering_deler(utgave.OM_SIDE_DELER)}
    assert set(deler) == set(utgave.OM_SIDE_DELER)
    erklaering = (bygg.HERE / "templates" / "erklaering.html").read_text()
    for avsnitt in deler.values():
        for a in avsnitt:
            assert a[:60] in re.sub(r"\s+", " ", erklaering)


def test_formats():
    for navn, f in utgave.FORMATER.items():
        assert all(n % 4 == 0 for n in f.sider), navn
        assert min(f.topp, f.bunn, f.inner, f.ytre) >= (12 if navn == "tabloid" else 10), navn
    t = utgave.FORMATER["tabloid"]
    assert (t.bredde, t.hoyde, t.spalter) == (280, 400, 5)
    assert (utgave.FORMATER["a4"].bredde, utgave.FORMATER["a4"].spalter) == (210, 3)
    a5 = utgave.FORMATER["a5"]
    assert (a5.bredde, a5.hoyde, a5.spalter) == (148, 210, 2) and a5.inner > a5.ytre


# ------------------------------------------------------------ A5 booklet ----

def test_booklet_order():
    assert utgave.hefte_rekkefolge(8) == [(8, 1), (2, 7), (6, 3), (4, 5)]
    assert utgave.hefte_rekkefolge(12) == [(12, 1), (2, 11), (10, 3), (4, 9), (8, 5), (6, 7)]
    for n in (4, 8, 12, 16, 20):
        sider = [p for ark in utgave.hefte_rekkefolge(n) for p in ark]
        assert sorted(sider) == list(range(1, n + 1))
        # Folded, each sheet's two halves hold pages that add up to n + 1.
        assert all(a + b == n + 1 for a, b in utgave.hefte_rekkefolge(n))
    for n in (0, 6, 10):
        with pytest.raises(ValueError):
            utgave.hefte_rekkefolge(n)


def test_booklet_file(tmp_path):
    import pymupdf

    kilde = tmp_path / "a5.pdf"
    with pymupdf.open() as doc:
        for i in range(1, 9):
            side = doc.new_page(width=148 / 25.4 * 72, height=210 / 25.4 * 72)
            side.insert_text((150, 300), f"SIDE{i}", fontsize=20)
        doc.save(kilde)
    ark = utgave.hefte(kilde, tmp_path / "hefte.pdf")
    assert ark == 2
    with pymupdf.open(tmp_path / "hefte.pdf") as doc:
        assert doc.page_count == 4
        r = doc[0].rect
        assert abs(r.width * 25.4 / 72 - 297) < 0.05 and abs(r.height * 25.4 / 72 - 210) < 0.05  # A4 landscape
        for nr, (venstre, hoyre) in enumerate(utgave.hefte_rekkefolge(8)):
            ord_ = sorted(doc[nr].get_text("words"), key=lambda w: w[0])
            assert [w[4] for w in ord_] == [f"SIDE{venstre}", f"SIDE{hoyre}"]


# --------------------------------------------------- whole editions (Chromium) ----

trenger_chromium = pytest.mark.skipif(not utgave.chromium_tilgjengelig(),
                                      reason="trenger Playwright og Chromium (uv run --extra utgave)")


@pytest.fixture(scope="session")
def fontmappe(tmp_path_factory):
    return tmp_path_factory.mktemp("fonter")


@trenger_chromium
@pytest.mark.parametrize("fmt", ["tabloid", "a4", "a5"])
def test_whole_edition(tmp_path, saker, fmt, fontmappe, monkeypatch):
    monkeypatch.setattr(utgave, "font_mappe", lambda: fontmappe)
    venter = next(s for s in mote(saker) if s["nr"] == "210/26")
    venter["venter_protokoll"] = True
    til = tmp_path / f"utgave-{fmt}.pdf"
    info = utgave.lag(MOTE, fmt, til, saker=saker, forhandsvisning=tmp_path / "png", dpi=30)
    assert info["sider"] % 4 == 0 and info["sider"] >= 4
    assert len([p for p in info["png"] if "hefte" not in p and "skjerm" not in p]) == info["sider"]
    # Fonts: embedded, TrueType, only the edition's own (sjekk_pdf fails otherwise)
    assert all(re.search(r"\+(TP|Unifraktur)", f) for f in info["fonter"])
    html = Path(info["html"]).read_text()
    kort = {s["kort"]: s for s in mote(saker)}
    historier = re.findall(r'<article class="sak (?:sak-full|kort)[^"]*" data-sak="([^"]+)" data-type="(full|kort)"'
                           r'(.*?)</article>', html, re.S)
    assert historier
    for adresse, typ, innhold in historier:
        assert adresse in kort and adresse != venter["kort"]
        assert 'class="kildefot"' in innhold and 'class="qr"' in innhold, adresse
        assert bygg.kort_id(kort[adresse]) in innhold
        assert "UKONTROLLERT" not in innhold
    trykt = {a for a, _, _ in historier}
    assert venter["nr_etikett"] not in info["hele"] + info["korte"] and info["venter"] == [venter["nr_etikett"]]
    # Every other sak with a summary is in print, as a full or a short story; the valg and the sak without a card in the listing
    med = {s["kort"] for s in mote(saker) if s["har_sammendrag"] and not bygg.er_valg(s) and s is not venter}
    assert med <= trykt
    assert "Nytt varamedlem i utvalget" in html and "Sak uten kort ennå" in html
    assert "Prøveutgave" in html and "Utgitt av RO-Studio AS" in html
    if fmt == "a5":
        assert Path(info["hefte"]).exists() and info["ark"] == info["sider"] // 4
    # The masthead and folios carry the meeting's weekday and date.
    assert "Bergen, Onsdag 23. september 2026" in html and "Tingposten<span class=\"skille\">" in html
    # The lead jumps: what the front page prints of it isn't printed again inside.
    if "Fortsettes på side" in html:
        assert "Fra side 1" in html
        forside = html[:html.index('data-side="2"')]
        for f in utgave.felt(innhold_lead(saker)):
            start = f["tekst"].split(". ")[0]
            if start in forside:
                assert html.count(start) == 1, f["label"]
    # A screen copy on a newsprint tone; the print file stays white.
    import pymupdf

    for sti, ventet in ((til, (255, 255, 255)), (Path(info["skjerm"]), utgave_farge(utgave.SKJERM_PAPIR))):
        with pymupdf.open(sti) as doc:
            assert doc.page_count == info["sider"]
            pix = doc[0].get_pixmap(dpi=20)
            assert all(abs(a - b) <= 2 for a, b in zip(pix.pixel(2, 2)[:3], ventet)), (sti, pix.pixel(2, 2))


def innhold_lead(saker):
    return utgave.velg(saker, MOTE)["fulle"][0]


def utgave_farge(hex_):
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def test_map_labels_are_outlines_not_stroked_text(fontmappe, monkeypatch):
    pytest.importorskip("fontTools")
    monkeypatch.setattr(utgave, "font_mappe", lambda: fontmappe)
    utgave.fonter(fontmappe)
    svg = str(utgave.figur_svg(grafikk.bydelskart(["Fana"])["svg"]))
    assert 'class="etikett-sti"' in svg and not re.search(r"<text[^>]*stroke=", svg)
    assert re.search(r'<path class="etikett-sti" d="M[^"]+"', svg)


def test_screen_copy_only_retints_the_paper():
    html = '<svg><path fill="#ffffff" stroke="#ffffff"/><path fill="#333333"/></svg>'
    ut = utgave.skjermkopi(html)
    assert utgave.SKJERM_PAPIR in ut and "#ffffff" not in ut and "#333333" in ut
    assert utgave.skjerm_sti(Path("a/tingposten-2026-09-23-a5.pdf")).name == "tingposten-2026-09-23-a5-skjerm.pdf"
