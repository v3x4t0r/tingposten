import pytest
from markupsafe import Markup

from tingposten.typo import NBSP, NNBSP, mellomrom, typo

NB = NBSP
NN = NNBSP


@pytest.mark.parametrize("text, expected", [
    # thousands: narrow no-break space; the number then stays with its word
    ("Utbyggeren betaler 2 330 692 kroner.", f"Utbyggeren betaler 2{NN}330{NN}692{NB}kroner."),
    ("solgt for 18 535 000 kroner", f"solgt for 18{NN}535{NN}000{NB}kroner"),
    ("påbygg på 2359 kvadratmeter", f"påbygg på 2359{NB}kvadratmeter"),
    # a number with its word or unit, and before %
    ("fikk 54 stemmer og 11 stemmer", f"fikk 54{NB}stemmer og 11{NB}stemmer"),
    ("en økning på 18 %", f"en økning på 18{NB}%"),
    ("Damsgårdsveien 143–145 og", f"Damsgårdsveien 143–145{NB}og"),
    # abbreviations keep their number
    ("til kl. 06.00 i perioden", f"til kl.{NB}06.00{NB}i perioden"),
    ("Ca. 200 boliger", f"Ca.{NB}200{NB}boliger"),
    ("sak nr. 5", f"sak nr.{NB}5"),
    ("Årstad, Gnr. 160, Bnr. 184 m.fl,", f"Årstad, Gnr.{NB}160, Bnr.{NB}184{NB}m.fl,"),
    ("Saksframlegg s. 12", f"Saksframlegg s.{NB}12"),
    ("pbl. § 12-12 og §§ 3-1", f"pbl. §{NB}12-12{NB}og §§{NB}3-1"),
    # a line never starts with a dash, spaced en dash or the kommune's spaced hyphen
    ("Torget - Skuteviken – høring", f"Torget{NB}- Skuteviken{NB}– høring"),
])
def test_mellomrom(text, expected):
    assert mellomrom(text) == expected


@pytest.mark.parametrize("text", [
    "Bystyret valgte Kjøs. Neste sak",   # "s." inside a word is not "side"
    "blant annet bl.a. fem saker",         # not one of the abbreviations
    "i 2023. Tunnelene",                   # a stop between number and word
    "A3-A4, V1",                           # letters around the hyphen
    "år 2023–2027",                        # nothing to do
])
def test_mellomrom_leaves_other_spaces(text):
    assert mellomrom(text) == text


def test_typo_escapes_and_returns_markup():
    out = typo('<script>alert("1 000")</script> & 5 % \'x\'')
    assert isinstance(out, Markup)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "&amp;" in out and "&#34;" in out and "&#39;x&#39;" in out
    assert f"1{NN}000" in out and f"5{NB}%" in out
    # Jinja won't escape it again
    assert str(Markup("{}").format(out)) == str(out)


def test_typo_holds_a_paragraph_reference_together_with_a_span():
    assert typo("etter § 7-9 i loven") == f'etter §{NB}<span class="nb">7-9</span>{NB}i loven'
    assert typo("planID 66340000 - sluttbehandling") == f"planID 66340000{NB}- sluttbehandling"
    assert typo("A3-A4") == "A3-A4" and typo("Hellberg-Kvalsvik") == "Hellberg-Kvalsvik"


def test_typo_on_markup_changes_text_only():
    html = Markup('<span class="nb">Dokken–Lyngbø</span> til 2 000 kroner <a href="x?a=1 000">s. 3</a>')
    out = typo(html)
    assert '<span class="nb">Dokken–Lyngbø</span>' in out
    assert f"2{NN}000{NB}kroner" in out and '<a href="x?a=1 000">' in out and f"s.{NB}3</a>" in out


def test_typo_handles_empty_and_none():
    assert typo(None) == "" and typo("") == "" and isinstance(typo(None), Markup)


def test_rydd_tittel_removes_bookkeeping_but_keeps_the_words():
    from tingposten.typo import rydd_tittel
    assert rydd_tittel("Representantforslag fra Camilla Nøttveit (V) om fysisk aktivitet i Bergensskolen Innsendt: 18.2.2026") \
        == "Representantforslag fra Camilla Nøttveit (V) om fysisk aktivitet i Bergensskolen"
    assert rydd_tittel("Representantforslag fra X (SV) om sommerskole i Bergen. Innsendt: 18.2.2026") \
        == "Representantforslag fra X (SV) om sommerskole i Bergen"
    assert rydd_tittel("Representantforslag fra Y (PP) om innfartsparkering ved Åsane Terminal -19.11.2025") \
        == "Representantforslag fra Y (PP) om innfartsparkering ved Åsane Terminal"
    assert rydd_tittel("Aurdalslia skole - oppgradering av ventilasjonsanlegg. Gjennomføringsvedtak ") \
        == "Aurdalslia skole – oppgradering av ventilasjonsanlegg. Gjennomføringsvedtak"
    assert rydd_tittel("Godkjenning av protokoll fra bystyrets møte 17 og 18.12.2025\n") \
        == "Godkjenning av protokoll fra bystyrets møte 17 og 18.12.2025"   # dates that say something stay
    assert rydd_tittel("ASSS- statistikk") == "ASSS- statistikk" and rydd_tittel("") == ""
