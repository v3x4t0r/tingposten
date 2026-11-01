"""Sharing: a preview card for Facebook and the rest (Open Graph), and the share links (#2)."""
import re
from datetime import date
from pathlib import Path

from tingposten import bygg, config

from test_bygg import BYSTYRET, data_dir, fake_rank, sak_page  # noqa: F401 (fixtures)
from test_debatt_bygg import _innlegg

STATIC = Path(bygg.__file__).parent / "static"


def _meta(html):
    return dict(re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)"', html))


def test_sak_page_has_a_preview_card_and_share_links(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "SITE_URL", "https://tingposten.no/")
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=tmp_path / "debatt")
    side = sak_page(site, "238/26").read_text()
    m = _meta(side)
    url = f"https://tingposten.no/bergen/sak/{BYSTYRET}/2026-09-23/238-26/"
    assert m["og:url"] == url and m["og:type"] == "article" and m["og:description"]
    assert m["og:image"] == "https://tingposten.no/static/deling.png" and m["twitter:card"] == "summary_large_image"
    assert m["article:published_time"] == "2026-09-23"
    # plain links, no Facebook script; del.js only adds the phone's share menu and «Kopier lenke»
    assert f'href="https://www.facebook.com/sharer/sharer.php?u={url.replace(":", "%3A")}"' in side
    assert 'href="mailto:?subject=' in side and f'data-url="{url}"' in side and "static/del.js" in side
    assert "connect.facebook.net" not in side
    assert (site / "static" / "deling.png").exists()
    # pages that are not articles: the image, but no share row and no script
    forside = (site / "bergen" / "index.html").read_text()
    assert _meta(forside)["og:image"].endswith("/static/deling.png") and "data-del" not in forside
    assert "del.js" not in forside


def test_innlegg_page_can_be_shared(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "SITE_URL", "https://tingposten.no/")
    _innlegg(data_dir)
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=tmp_path / "debatt")
    side = (site / "bergen/debatt/2026-09-30-bybanen-ma-til-asane-na/index.html").read_text()
    m = _meta(side)
    assert m["og:type"] == "article" and m["article:published_time"] == "2026-09-30"
    assert m["og:description"].startswith("Debattinnlegg av Kari Nordmann")
    assert 'data-url="https://tingposten.no/bergen/debatt/2026-09-30-bybanen-ma-til-asane-na/"' in side


def test_share_image_is_the_size_the_tags_say():
    png = (STATIC / "deling.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) == (1200, 630)
