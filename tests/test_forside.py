"""Front pages that fit the number of saker (review 30.10): tiles only for temaer
with saker left, the standard meeting only when it holds half the stories, a
capped panel, raw kommune titles that don't look like headlines."""
import re
from datetime import date

from tingposten import bygg
from test_bygg import BYSTYRET, data_dir, fake_rank, fake_sak, lead_html, site  # noqa: F401  (fixtures)

BYRADET = "900102"


def mote(dato, organ=BYSTYRET):
    return f"{organ}-{dato}"


def test_standard_meeting_needs_half_the_shown_stories():
    a, b, c = mote("2026-09-23"), mote("2026-09-30"), mote("2026-06-17")
    stories = [fake_sak(1, 90, mote=a), fake_sak(2, 80, mote=a), fake_sak(3, 70, mote=a),
               fake_sak(4, 60, mote=b), fake_sak(5, 50, mote=b)]
    moter = bygg._meetings(stories + [fake_sak(i, 0, mote=c) for i in range(10, 40)])
    # c has the most saker in the period, but none of the stories: never the standard
    assert bygg.standard_mote(stories, moter)["slug"] == a
    assert bygg.standard_mote(stories[2:], moter)["slug"] == b      # 2 of 3
    assert bygg.standard_mote(stories[1:5] + [fake_sak(6, 40, mote=c)], moter) is None  # 2 of 5
    assert bygg.standard_mote([], moter) is None
    # a tie goes to the latest meeting
    assert bygg.standard_mote(stories[2:4], moter)["slug"] == b


def test_year_page_has_no_false_standard_line():
    # most saker come from one meeting, but the stories don't: no "Fra … der ikke annet står"
    big = [fake_sak(i, 0, mote=mote("2026-06-17")) for i in range(100, 160)]
    stories = [fake_sak(i, 80 - i, mote=mote(f"2026-0{3 + i % 6}-1{i % 9}")) for i in range(15)]
    front = bygg.front_page(big + stories)
    assert front["standard"] is None


def test_tema_order_puts_annet_last():
    saker = [fake_sak(i, 10, tema="annet") for i in range(5)] + [fake_sak(10, 10, tema="okonomi"),
                                                                  fake_sak(11, 10, tema="skole"),
                                                                  fake_sak(12, 10, tema="skole")]
    assert [t["tema"] for t in bygg.tema_counts(saker)] == ["skole", "okonomi", "annet"]


def test_tiles_only_for_temaer_with_saker_left_and_summaries_first():
    window = [fake_sak(1, 90, tema="okonomi"), fake_sak(2, 80, tema="skole"),
              fake_sak(3, 30, tema="skole", sammendrag=False),      # high score, no card
              fake_sak(4, 5, tema="skole"),                          # low score, has a card: fills up the stories
              fake_sak(5, 0, tema="skole")]                          # valg-like, score 0, has a card
    front = bygg.front_page(window)
    # okonomi's only sak is a story: no tile, a link in the "Også i perioden" line
    assert [t["tema"] for t in front["fliser"]] == ["skole"]
    assert [t["tema"] for t in front["ogsa"]] == ["okonomi"]
    tile = front["fliser"][0]["saker"]
    assert [s["nr"] for s in tile] == ["5/26", "3/26"]  # a summary before a higher score without one
    assert front["andre"] == 0


def test_no_tiles_when_every_sak_is_a_story():
    window = [fake_sak(i, 60 - i, tema=t) for i, t in enumerate(["okonomi", "skole", "helse", "skole"])]
    front = bygg.front_page(window)
    assert front["fliser"] == [] and {t["tema"] for t in front["ogsa"]} == {"okonomi", "skole", "helse"}
    assert front["panel_temaer"] == front["temaer"]  # nothing for "og N temaer til" to point at


def test_panel_caps_without_cutting_off_a_single_one():
    temaer = ["styring", "skole", "helse", "samferdsel", "miljo", "kultur", "byutvikling", "okonomi", "annet"]
    window = [fake_sak(100 * k + i, 0, tema=t) for k, t in enumerate(temaer) for i in range(9 - k)]
    front = bygg.front_page(window)
    assert len(front["fliser"]) == 9
    assert [t["tema"] for t in front["panel_temaer"]] == temaer[:6]  # 9 temaer: 6 bars + "og 3 temaer til"
    assert len(bygg.front_page([s for s in window if s["tema"] not in ("okonomi", "annet")])["panel_temaer"]) == 7
    days = [f"2026-09-{d:02d}" for d in (1, 3, 8, 10, 15, 17)]
    moter = bygg._meetings([fake_sak(i, 0, mote=mote(d)) for i, d in enumerate(days)])
    assert [m["dato"].day for m in bygg.panel_moter(moter)] == [17, 15, 10, 8]   # newest first, 4 + "og 2 til"
    assert len(bygg.panel_moter(moter[:5])) == 5                                  # never "og 1 møte til"


def test_bystyret_before_byradet_on_the_same_day():
    saker = [{**fake_sak(1, 0, mote=mote("2026-09-23", BYRADET)), "organ": "Byrådet – saker avgjort av byrådet",
              "organ_kort": "Byrådet"},
             fake_sak(2, 0, mote=mote("2026-09-23")), fake_sak(3, 0, mote=mote("2026-09-10"))]
    assert [m["slug"] for m in bygg._meetings(saker)] == [mote("2026-09-23"), mote("2026-09-23", BYRADET),
                                                          mote("2026-09-10")]


def test_status_box_only_for_rare_telling_statuses():
    for tekst in ("Innstillingen falt", "Forslaget falt", "Sendt tilbake – ikke avgjort ennå",
                  "Utsatt – ikke avgjort ennå", "Ikke behandlet"):
        assert bygg.status_boks(tekst), tekst
    for tekst in ("Under behandling", "Venter på protokoll", "Tatt til orientering", "Valg/fritak",
                  "Overlevert – behandles senere", "", None):
        assert not bygg.status_boks(tekst), tekst


def test_rendered_front_page(site):
    site, _ = site
    forside = (site / "bergen/index.html").read_text()
    assert "<title>Siste 14 dager · Bergen · Tingposten</title>" in forside
    # the bars link to the tema pages for the period, never to anchors on the page; Annet last
    bars = re.search(r'<ul class="stolper".*?</ul>', forside, re.S).group(0)
    hrefs = re.findall(r'href="([^"]+)"', bars)
    assert set(hrefs) == {"../bergen/tema/annet/", "../bergen/tema/okonomi/", "../bergen/tema/skole/"}
    assert hrefs[-1] == "../bergen/tema/annet/"
    assert all((site / "bergen" / h.removeprefix("../bergen/") / "index.html").exists() for h in hrefs)
    assert 'href="#tema-' not in forside
    year = (site / "bergen/i-ar/index.html").read_text()
    assert 'href="../../bergen/i-ar/tema/annet/"><span class="stolpe-navn">' in year
    # a sak without a card keeps the kommune's title, set as a plain line
    assert re.search(r'<h4 class="flis-tittel raatittel"><a [^>]*>Sak med høy score men uten kort</a>', forside)
    assert re.search(r'<h2 class="lead-tittel"><a', forside)  # the lead has a card: a headline
    # the rare status is boxed (239: sent back), the Artikkel cue is not a link
    assert '<li class="faktum faktum-status"><span class="vh">Status: </span>Sendt tilbake' in forside
    assert "min lesing" not in lead_html(forside)
    assert not re.search(r'<a[^>]*>[^<]*Artikkel', forside)
    # the tile's count link sits at its foot
    assert re.search(r'<p class="flis-fot"><a class="pil" href="../bergen/tema/annet/">Alle 3 vedtak<span class="vh"> om annet</span></a></p>', forside)


def test_front_page_css(site):
    site, _ = site
    css = (site / "static/style.css").read_text()
    # Source Serif italic isn't loaded for a few one-liners: they are roman or Bodoni italic
    for sel in (".periode-merk", ".flis-tom", ".tomt", ".kommer"):
        rule = re.search(r"(?m)^" + re.escape(sel) + r" \{[^}]*\}", css).group(0)
        assert "italic" not in rule or "var(--didone)" in rule, rule
    assert "--sterk: #c8c5bc" in css                        # softer strong rules in dark mode
    assert ".lead .nokkeltall { margin-top: auto; }" not in css  # white space falls below the lead
    assert ".sek .fakta { align-content: flex-start; }" in css


def test_standard_line_follows_the_stories(tmp_path, data_dir):
    site = tmp_path / "s"
    bygg.build(site, data_dir, idag=date(2026, 10, 1))
    # 14 days: every story is from bystyret 23 September
    assert "Fra bystyret 23. september 2026 der ikke annet står" in (site / "bergen/index.html").read_text()
    # the year: 2 of the 3 stories still are, so the line stays; the January sak shows its meeting
    aar = (site / "bergen/i-ar/index.html").read_text()
    assert "Fra bystyret 23. september 2026 der ikke annet står" in aar
    assert 'faktum-mote"><span>Bystyret, <time datetime="2026-01-28">' in aar
