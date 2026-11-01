"""Being found (synlighet.py): robots.txt, sitemap, llms.txt, JSON-LD and IndexNow."""
import json
import re
from datetime import date

from tingposten import bygg, config, synlighet

from test_bygg import BYSTYRET, data_dir, fake_rank, sak_page  # noqa: F401 (fixtures)
from test_debatt_bygg import _innlegg

URL = "https://tingposten.no/"


def _ld(html):
    return [json.loads(x) for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _bygg(monkeypatch, tmp_path, data_dir):  # noqa: F811
    monkeypatch.setattr(config, "SITE_URL", URL)
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", False)
    monkeypatch.setattr(config, "INDEXNOW_NOKKEL", "abc123abc123")
    site = tmp_path / "site"
    bygg.build(site, data_dir, idag=date(2026, 10, 1), debatt_dir=tmp_path / "debatt")
    return site


def test_robots_sitemap_and_llms_txt(monkeypatch, tmp_path, data_dir):  # noqa: F811
    site = _bygg(monkeypatch, tmp_path, data_dir)
    robots = (site / "robots.txt").read_text()
    assert "User-agent: *\nAllow: /\n" in robots and f"Sitemap: {URL}sitemap.xml" in robots
    kart = (site / "sitemap.xml").read_text()
    sak = f"{URL}bergen/sak/{BYSTYRET}/2026-09-23/238-26/"
    assert f"<loc>{sak}</loc><lastmod>" in kart and f"<loc>{URL}bergen/</loc><lastmod>2026-10-01</lastmod>" in kart
    assert f"<loc>{URL}om/</loc>" in kart and f"<loc>{URL}bergen/moter/</loc>" in kart
    assert "logg-inn" not in kart and "404" not in kart and f"<loc>{URL}</loc>" not in kart
    llms = (site / "llms.txt").read_text()
    assert llms.startswith("# Tingposten\n\n> ") and f"]({sak})" in llms and "Bergen kommune" in llms
    assert (site / "abc123abc123.txt").read_text() == "abc123abc123\n"


def test_sak_without_summary_stays_out_of_search(monkeypatch, tmp_path, data_dir):  # noqa: F811
    site = _bygg(monkeypatch, tmp_path, data_dir)
    side = sak_page(site, "241/26").read_text()    # the editor rejected the card: only title and links
    assert '<meta name="robots" content="noindex' in side and not _ld(side)
    assert "241-26" not in (site / "sitemap.xml").read_text()


def test_sak_is_a_news_article_about_bergen(monkeypatch, tmp_path, data_dir):  # noqa: F811
    site = _bygg(monkeypatch, tmp_path, data_dir)
    side = sak_page(site, "238/26").read_text()
    assert "<title>" in side and " · Bergen kommune · Tingposten</title>" in side
    graf = _ld(side)[0]["@graph"]
    artikkel = next(g for g in graf if g["@type"] == "NewsArticle")
    assert artikkel["datePublished"] == "2026-09-23" and artikkel["headline"] and artikkel["description"]
    assert artikkel["publisher"]["name"] == "Tingposten" and artikkel["contentLocation"]["name"] == "Bergen"
    assert all(u.startswith("https://") for u in artikkel["isBasedOn"]) and artikkel["isBasedOn"]
    smuler = next(g for g in graf if g["@type"] == "BreadcrumbList")
    assert smuler["itemListElement"][0]["item"] == f"{URL}bergen/"
    forside = (site / "bergen" / "index.html").read_text()
    assert {g["@type"] for g in _ld(forside)[0]["@graph"]} == {"NewsMediaOrganization", "WebSite"}


def test_innlegg_is_an_opinion_piece_with_its_author(monkeypatch, tmp_path, data_dir):  # noqa: F811
    _innlegg(data_dir)
    site = _bygg(monkeypatch, tmp_path, data_dir)
    side = (site / "bergen/debatt/2026-09-30-bybanen-ma-til-asane-na/index.html").read_text()
    stykke = _ld(side)[0]["@graph"][0]
    assert stykke["@type"] == "OpinionNewsArticle" and stykke["author"] == {"@type": "Person", "name": "Kari Nordmann"}
    assert "</script>" not in json.dumps(stykke)


def test_json_ld_cannot_close_its_script_tag():
    s = {"har_sammendrag": True, "lenke": "bergen/sak/x/", "overskrift": "A </script><b>", "dek": "d", "tittel": "t",
         "dato": date(2026, 1, 1), "endret": date(2026, 1, 2), "tema_navn": "Skole", "organ": "Bystyret", "organ_kort": "Bystyret", "organ_kort": "Bystyret",
         "pdf": {}, "tema_side": None}
    html = str(synlighet.sak(URL, s))
    assert html.count("</script>") == 1 and "<\\/script>" in html


def test_indexnow_sends_recent_pages_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SITE_URL", URL)
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", False)
    monkeypatch.setattr(config, "INDEXNOW_NOKKEL", "abc123abc123")
    (tmp_path / "sitemap.xml").write_text(synlighet.sitemap(URL, [
        ("bergen/", date(2026, 10, 1)), ("bergen/sak/gammel/", date(2026, 9, 1)),
        ("bergen/sak/ny/", date(2026, 9, 30)), ("om/", None)]))

    class Okt:
        def __init__(self):
            self.sendt = []

        def post(self, url, json, timeout):
            self.sendt.append((url, json))
            return type("R", (), {"status_code": 200, "text": ""})()

    okt = Okt()
    assert synlighet.varsle_indexnow(tmp_path, idag=date(2026, 10, 1), session=okt) == 2
    url, body = okt.sendt[0]
    assert url == synlighet.INDEXNOW and body["host"] == "tingposten.no"
    assert body["keyLocation"] == f"{URL}abc123abc123.txt"
    assert body["urlList"] == [f"{URL}bergen/", f"{URL}bergen/sak/ny/"]
    monkeypatch.setattr(config, "SKJUL_FOR_SOK", True)
    assert synlighet.varsle_indexnow(tmp_path, session=okt) == 0 and len(okt.sendt) == 1
