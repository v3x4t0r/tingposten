import json
from pathlib import Path

import pymupdf
import pytest

from tingposten.saksframlegg import find_amounts, find_hjemler, parse_pdf

FIXTURES = Path(__file__).parent / "fixtures" / "saksframlegg"


def render(name: str, tmp_path: Path) -> Path:
    """Draw a fixture's positioned lines into a real PDF (Helvetica / Helvetica-Bold)."""
    spec = json.loads((FIXTURES / f"{name}.json").read_text())
    fonts = {False: pymupdf.Font("helv"), True: pymupdf.Font("hebo")}
    doc = pymupdf.open()
    for _ in range(max(l["page"] for l in spec["lines"])):
        doc.new_page(width=595, height=842)
    for line in spec["lines"]:
        page = doc[line["page"] - 1]
        writer = pymupdf.TextWriter(page.rect)
        pos = pymupdf.Point(line["x"], line["y"])
        spans = line.get("spans") or [{"text": line["text"], "bold": line.get("bold", False)}]
        for span in spans:
            _, pos = writer.append(pos, span["text"], font=fonts[span.get("bold", False)], fontsize=line.get("size", 11))
        writer.write_text(page)
    path = tmp_path / f"{name}.pdf"
    doc.save(path)
    return path


def headings(r):
    return [s["overskrift"] for s in r["seksjoner"]]


def test_byradssak(tmp_path):
    r = parse_pdf(render("byradssak", tmp_path))
    assert r["tittel"] == "Gjennomføringsvedtak for Slettebakken idrettsby fase 1A"
    assert r["referanse"] == "2026/175817-2"
    assert r["sider"] == 2
    # the bold table cell indented from the margin is not a heading
    assert headings(r) == [
        "Hva saken gjelder:",
        "Økonomiske konsekvenser:",
        "Byrådets vurdering og anbefaling/konklusjon:",
        "Begrunnelse for framleggelse til bystyret:",
        "Byrådet innstiller til bystyret å fatte følgende vedtak:",
        "Vedlegg:",
    ]
    assert [s["side"] for s in r["seksjoner"]] == [1, 1, 1, 2, 2, 2]
    assert r["hva_saken_gjelder"] == (
        "Saken gjelder gjennomføringsvedtak for første del av utviklingen av Slettebakken idrettsby – "
        "fase 1A. Områdereguleringsplan for Slettebakken er nå vedtatt, jf. bystyresak 172/26.\n\n"
        "Prosjektet omfatter en rekke tiltak:\n\n"
        "• Ny avkjørsel til Slettebakken borettslag\n"
        "• Planovergang og kryssing av Bybanen, inkludert lysregulering\n"
        "• Snarvei til fysak"
    )
    # own reasoning, not the formal "why this goes to bystyret"
    assert r["begrunnelse"].startswith("Byrådet mener")
    # signature block (date, name, "godkjent elektronisk") is dropped
    assert r["forslag_til_vedtak"] == (
        "1. Prosjekt 4974 Slettebakken idrettsby - fase 1A gjennomføres med en total investeringsramme på 69 mill.(P50).\n"
        "2. Prosjektet medfører ingen økte driftskostnader."
    )
    assert r["vedlegg"] == ["Fagnotat, datert 2. juni 2026", "Justert kostnadskalkyle 4974 Slettebakken idrettsby fase 1A"]
    assert [(a["kroner"], a["side"]) for a in r["belop"]] == [(69_000_000, 1), (352_000_000, 1), (50_000_000, 2), (69_000_000, 2)]
    all_text = " ".join(s["tekst"] for s in r["seksjoner"])
    assert "[navn]" not in all_text  # saksbehandler in the header, byråd in the signature
    assert "Saksframstilling" not in all_text
    assert not any(t.strip() in {"1", "2"} for t in all_text.split("\n"))  # page numbers


def test_interpellasjon(tmp_path):
    r = parse_pdf(render("interpellasjon", tmp_path))
    # title wraps over three lines and switches from 11 to 14 pt
    assert r["tittel"] == (
        "Interpellasjon nr. 28 fra [representant] (H) om forebygging av bruk av "
        "illegale rusmidler blant ungdomsskoleelever. Bergen bystyre 23.09.2026"
    )
    assert r["referanse"] == "2026/223922-2"
    # a regular-weight colon after a bold heading must not become "byråden :"
    assert headings(r) == ["", "Spørsmål til byråden:"]
    assert r["seksjoner"][0]["tekst"] == "Det er økende bekymring for bruk av illegale rusmidler blant ungdom i Bergen."
    assert r["hva_saken_gjelder"] == r["begrunnelse"] == r["forslag_til_vedtak"] == ""
    assert "Intern korrespondanse" not in " ".join(s["tekst"] for s in r["seksjoner"])
    assert r["bydeler"] == []


def test_nynorsk_hjemler_and_amounts(tmp_path):
    r = parse_pdf(render("nynorsk_hjemler", tmp_path))
    assert r["tittel"] == "Ytrebygda, Gnr. 111, Bnr. 83 m. fl., Kokstaddalen - Midlertidig forbod mot tiltak"
    assert r["hva_saken_gjelder"].startswith("Byrådet føreslår")
    assert r["begrunnelse"].startswith("Byrådet meiner")
    assert r["forslag_til_vedtak"].startswith("1. Bergen bystyre vedtek")
    assert [h["tekst"] for h in r["hjemler"]] == [
        "plan- og bygningslova § 13-1",
        "kommuneloven § 7-10",
        "plan- og bygningsloven § 13-1",
    ]
    assert [(a["tekst"], a["kroner"]) for a in r["belop"]] == [("kroner 11 millionar", 11_000_000), ("kr 26 900", 26_900)]
    assert r["bydeler"] == ["Ytrebygda"]


def test_margin_with_many_bullets(tmp_path):
    # Most lines start at the bullet indent; the margin is still the leftmost common x.
    r = parse_pdf(render("punktliste_marg", tmp_path))
    assert headings(r) == ["Hva saken gjelder:", "Forslag til vedtak:", "Vedlegg:"]
    assert "• Medlem i Bergen bystyre\n• Medlem i Forretningsutvalget\n" in r["hva_saken_gjelder"]
    assert r["forslag_til_vedtak"].startswith("Forretningsutvalget innstiller")
    assert r["vedlegg"] == ["Epost fra partiet"]  # "Vedlegg:" bold, rest of the line regular


@pytest.mark.parametrize(
    "text, expected",
    [
        ("investeringsramme på 69 mill.(P50).", [("69 mill.", 69_000_000)]),
        ("1,5 mill. kroner", [("1,5 mill. kroner", 1_500_000)]),
        ("mellom 3,5 og 4,0 milliarder (se figur)", [("4,0 milliarder", 4_000_000_000)]),
        ("nettobeløp på MNOK 1.170, jf.", [("MNOK 1.170", 1_170_000_000)]),
        ("kr 1 234 567,-", [("kr 1 234 567,-", 1_234_567)]),
        ("om lag 115 mill. kroner", [("115 mill. kroner", 115_000_000)]),
        ("49,3 mrd. 2025-kroner", [("49,3 mrd.", 49_300_000_000)]),
        ("ca. 23 550 kr. per innbygger", [("23 550 kr.", 23_550)]),
        ("satsen for 2026 kr 26 900 per barn", [("kr 26 900", 26_900)]),
        ("5,4 millioner innbyggere", []),
        ("har nok 10 plasser", []),
        ("40 % usikkerhet", []),
    ],
)
def test_find_amounts(text, expected):
    assert [(a["tekst"], a["kroner"]) for a in find_amounts(text)] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("etter plan- og bygningsloven § 12-12.", ["plan- og bygningsloven § 12-12"]),
        ("jf. § 12-12 i plan- og bygningsloven", ["plan- og bygningsloven § 12-12"]),
        ("pbl. § 12-14", ["plan- og bygningsloven § 12-14"]),
        ("Kommuneloven § 7-9 2. ledd", ["kommuneloven § 7-9"]),
        ("kommuneloven § 7-10, 3. ledd", ["kommuneloven § 7-10"]),
        ("helse- og omsorgstjenesteloven §§ 6-1 og 6-2.", ["helse- og omsorgstjenesteloven §§ 6-1 og 6-2"]),
        ("naturmangfoldloven §§ 8–12", ["naturmangfoldloven §§ 8–12"]),
        ("lovens § 5", []),
    ],
)
def test_find_hjemler(text, expected):
    assert [h["tekst"] for h in find_hjemler(text)] == expected


def test_broken_or_missing_file_gives_empty_result(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    for path in (junk, tmp_path / "missing.pdf"):
        r = parse_pdf(path)
        assert r["tittel"] == "" and r["seksjoner"] == [] and r["belop"] == [] and r["sider"] == 0
