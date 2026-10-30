"""Meeting pages and the list of meetings (review 30.10): one column in agenda
order, valg and fritak at the end, an outcome line from the statuses, an
honest page for a meeting whose protokoll isn't out, interpellasjoner
labelled as such."""
import json
import re
from datetime import date
from html.parser import HTMLParser

import pytest

from tingposten import bygg, grafikk, pdf
from test_bygg import BYSTYRET, ITEMS, MOTE, data_dir, fake_rank, fake_sak, item  # noqa: F401  (fixtures)

VENTER = f"{BYSTYRET}-2026-09-29"


@pytest.fixture
def site(tmp_path, data_dir, monkeypatch):
    items = ITEMS + [
        {**item("28/26", "Interpellasjon nr.28 fra NN om skoleveier", seq=2), "kategori": "Interpellasjon"},
        item("286/26", "Søknad om fritak fra verv og nyvalg av varamedlem", seq=40),
        item("330/26", "Representantforslag om båtslipper", dato="2026-09-29", seq=1),
        item("331/26", "Representantforslag om solenergi", dato="2026-09-29", seq=2),
    ]
    (data_dir / "agendapunkter" / f"{BYSTYRET}.json").write_text(json.dumps({i["id"]: i for i in items}))
    # 29 September is held, but the protokoll on file still covers an earlier meeting.
    for i in items[-2:]:
        path = data_dir / "pdf" / pdf.filename(i, "protokoll")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    monkeypatch.setattr(bygg._Parsers, "protokoll", lambda self, path: {"motedato": "2026-06-17"} if path.exists() else {})
    monkeypatch.setattr(grafikk, "figurer", lambda item, p, s: [])
    out = tmp_path / "site"
    bygg.build(out, data_dir, idag=date(2026, 10, 1))
    return out


class Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.levels = []

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]", tag):
            self.levels.append(int(tag[1]))


def heading_order_ok(page: str) -> bool:
    p = Headings()
    p.feed(page)
    return p.levels[0] == 1 and all(b <= a + 1 for a, b in zip(p.levels, p.levels[1:]))


def test_nr_etikett():
    assert bygg.nr_etikett({"referansenr": "235/26", "tittel": "Plan"}) == "Sak 235/26"
    assert bygg.nr_etikett({"referansenr": "28/26", "kategori": "Interpellasjon", "tittel": "x"}) == "Interpellasjon 28/26"
    assert bygg.nr_etikett({"referansenr": "30/26", "tittel": "Interpellasjon nr. 30 fra NN"}) == "Interpellasjon 30/26"


def test_utfall_line():
    def s(tekst, sammendrag=True, artikkel=False, falt=False):
        return {"status_tekst": tekst, "har_sammendrag": sammendrag, "har_artikkel": artikkel, "innstilling_falt": falt}
    saker = ([s("Vedtatt", artikkel=True)] * 3 + [s("Vedtatt", falt=True)] + [s("Tatt til orientering")] * 2
             + [s("Sendt tilbake – ikke avgjort ennå", falt=True), s("Valg og fritak", sammendrag=False),
                s("", sammendrag=False),                       # outcome unknown: not counted
                s("Venter på protokoll", sammendrag=False)])   # a label this list doesn't know
    assert bygg.utfall(saker) == ["3 vedtatt", "1 der innstillingen falt", "2 tatt til orientering", "1 sendt tilbake",
                                  "1 valg og fritak", "1 venter på protokoll", "3 med bakgrunnsartikkel",
                                  "3 uten sammendrag ennå"]
    assert bygg.utfall([]) == []


def test_meeting_page_one_column_in_agenda_order(site):
    page = (site / f"bergen/mote/{MOTE}/index.html").read_text()
    assert heading_order_ok(page)
    saker = re.search(r'<ol class="rader rader-mote">.*?</ol>', page, re.S).group(0)
    # the sak number in the label column, interpellasjoner in their own series
    nrs = re.findall(r'<p class="rad-nr">([^<]+)</p>', saker)
    assert nrs == ["Interpellasjon 28/26", "Sak 238/26", "Sak 239/26", "Sak 240/26", "Sak 241/26", "Sak 243/26"]
    assert "Sak 28/26" not in page
    # valg and fritak grouped compactly at the end, after the saker
    valg = re.search(r'<section class="motedel" aria-labelledby="valg">.*?</section>', page, re.S).group(0)
    assert "Sak 286/26" in valg and "fritak" in valg.lower() and "Sak 286/26" not in saker
    assert page.index('id="sakene"') < page.index('id="valg"')
    # the outcome line, from the statuses
    utfall = re.search(r'<p class="utfall">(.*?)</p>', page, re.S).group(1)
    assert "1 sendt tilbake" in utfall and "1 med bakgrunnsartikkel" in utfall and "uten sammendrag ennå" in utfall
    assert '<meta name="description" content="Alle sakene bystyret behandlet 23. september 2026' in page
    # breadcrumbs start with Forsiden; Møter is the section, not the page
    assert '<li><a href="../../../bergen/">Forsiden</a></li>' in page
    assert re.search(r'<a href="../../../bergen/moter/" aria-current="true">Møter</a>', page)


def test_meeting_waiting_for_its_protokoll_says_so(site):
    page = (site / f"bergen/mote/{VENTER}/index.html").read_text()
    assert "Kommunen har ikke lagt ut protokollen fra møtet ennå, så vi vet ikke hva som ble vedtatt." in page
    assert "Sammendragene kommer når protokollen er ute." in page
    description = re.search(r'<meta name="description" content="([^"]*)"', page).group(1)
    assert "avgjorde" not in description and "behandlet" not in description
    assert "protokollen ennå" in description
    assert 'class="utfall"' not in page          # nothing is known about the outcome
    assert "Representantforslag om båtslipper" in page and heading_order_ok(page)
    moter = (site / "bergen/moter/index.html").read_text()
    row = re.search(rf'<li class="bystyret"><a href="../../bergen/mote/{VENTER}/">.*?</li>', moter).group(0)
    assert "2 saker · venter på protokoll" in row
    assert "venter på protokoll" not in re.search(rf'<a href="../../bergen/mote/{MOTE}/">.*?</li>', moter).group(0)
    # and the front pages leave its saker out
    assert "båtslipper" not in (site / "bergen/index.html").read_text()


def test_list_of_meetings(site):
    page = (site / "bergen/moter/index.html").read_text()
    assert "Alle møter i bystyret og byrådet, nyeste først." in page
    assert '<p class="kicker">' not in page          # no kicker repeating the h1
    assert "tirsdag 29. september" in page and "onsdag 23. september" in page  # with the weekday
    assert page.index(VENTER) < page.index(MOTE)
    assert '<li><a href="../../bergen/">Forsiden</a></li>' in page
    assert re.search(r'<a href="../../bergen/moter/" aria-current="page">Møter</a>', page)
    assert heading_order_ok(page)


def test_meeting_facts_for_the_moter_page():
    saker = [{**fake_sak(1, 0), "venter_protokoll": True}, {**fake_sak(2, 0), "venter_protokoll": True}]
    assert bygg._meetings(saker)[0]["venter_protokoll"] and bygg._meetings(saker)[0]["bystyret"]
    assert not bygg._meetings([fake_sak(1, 0), {**fake_sak(2, 0), "venter_protokoll": True}])[0]["venter_protokoll"]
