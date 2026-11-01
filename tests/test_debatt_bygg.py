"""The comment section on the static site: only when config.DEBATT is set."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from tingposten import bygg, config
from tingposten.debatt.regler import REGLER

from test_bygg import BYSTYRET, Links, data_dir, fake_rank, sak_page  # noqa: F401 (fixtures)

API = "https://debatt.tail1234.ts.net"
STATIC = Path(bygg.__file__).parent / "static"


@pytest.fixture
def med_debatt(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "DEBATT", API + "/")
    monkeypatch.setattr(config, "DEBATT_MODERATOR", ("hailo", "claude"))
    site, debatt_dir = tmp_path / "site", tmp_path / "debatt"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=debatt_dir)
    return site, debatt_dir


def test_switched_off_builds_no_comments(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "DEBATT", None)
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=tmp_path / "debatt")
    side = sak_page(site, "238/26").read_text()
    assert 'id="debatt"' not in side and "debatt.js" not in side
    assert not (site / "bergen" / "logg-inn").exists() and not (site / "om" / "debattregler").exists()
    assert not (tmp_path / "debatt").exists()
    assert "Tingposten følger ikke med på enkeltlesere." in (site / "om" / "index.html").read_text()


def test_sak_page_gets_the_comment_section(med_debatt):
    site, _ = med_debatt
    side = sak_page(site, "238/26").read_text()
    sec = re.search(r'<section class="debatt lag" id="debatt"[^>]*>', side).group(0)
    assert f'data-debatt-sak="{BYSTYRET}/2026-09-23/238-26"' in sec and f'data-api="{API}"' in sec
    assert f'data-side="https://tingposten.no/bergen/sak/{BYSTYRET}/2026-09-23/238-26/"' in sec
    assert '<script src="../../../../../static/debatt.js" defer></script>' in side
    # Comments come after the sak and before "Fra samme møte".
    assert side.index('id="debatt"') > side.index("</article>")
    if "relaterte-tittel" in side:
        assert side.index('id="debatt"') < side.index("relaterte-tittel")


def test_login_and_rules_pages(med_debatt):
    site, _ = med_debatt
    inn = (site / "bergen" / "logg-inn" / "index.html").read_text()
    assert "data-debatt-innlogging" in inn and f'data-api="{API}"' in inn and 'content="noindex' in inn
    regler = (site / "om" / "debattregler" / "index.html").read_text()
    for key, (navn, _tekst) in REGLER.items():
        if key != "ok":
            assert f"<dt>{navn}</dt>" in regler
    assert "TypeSafe" not in regler and "Anthropic (Claude)" in regler  # says where the text goes
    om = (site / "om" / "index.html").read_text()
    assert "<h3>Kommentarer</h3>" in om and 'href="debattregler/"' in om


def test_sak_index_for_the_server(med_debatt):
    site, debatt_dir = med_debatt
    indeks = json.loads((debatt_dir / "saker.json").read_text())
    key = f"{BYSTYRET}/2026-09-23/238-26"
    assert indeks[key]["tittel"] == "Klarsignal til å bygge Eksempelhallen"
    assert indeks[key]["lenke"] == f"https://tingposten.no/bergen/sak/{key}/"
    assert all((site / "bergen" / "sak" / k / "index.html").exists() for k in indeks)


def test_new_pages_links_resolve(med_debatt):
    site, _ = med_debatt
    for page in (site / "bergen" / "logg-inn" / "index.html", site / "om" / "debattregler" / "index.html",
                 sak_page(site, "238/26"), site / "om" / "index.html"):
        p = Links()
        p.feed(page.read_text())
        for ref in p.refs:
            if re.match(r"^[a-z]+:", ref) or ref.startswith("#"):
                continue
            target = (page.parent / ref.split("#")[0]).resolve()
            if ref.split("#")[0].endswith("/") or target.is_dir():
                target = target / "index.html"
            assert target.exists(), (page.relative_to(site), ref)


def test_script_never_turns_reader_text_into_html():
    js = (STATIC / "debatt.js").read_text()
    for farlig in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert farlig not in js, farlig
    # Sessions are bearer tokens, never cookies.
    assert "document.cookie" not in js and "credentials" not in js
