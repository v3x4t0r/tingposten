"""Attachments (vedlegg.py): listed once from Bergen's API, read by the cheap model,
only the chosen pages go to the writer and the fact-checker, and the site links them."""
import os
import time

import pymupdf
import pytest

from tingposten import admin, bygg, config, kildetekst, llm, pdf, pipeline, skribent, vedlegg

from test_pipeline import item, store

API_FILER = [
    {"kildeId": "15432074", "name": "Svar fra Bystyrets kontor", "alternativt_navn": "Svar-fra-Bystyrets-kontor",
     "filNavn": "15432074_12_1.pdf", "relasjonsType": "Vedlegg", "tilgangskode": "U", "offisiellt": True, "seq": 1,
     "storrelse": 260128, "utvalgsNavn": "Forretningsutvalget"},
    {"kildeId": "14881386", "name": "Svar på representantforslag", "alternativt_navn": "Svar-pa-representantforslag",
     "filNavn": "14881386_24_1.pdf", "relasjonsType": "Vedlegg", "tilgangskode": "U", "offisiellt": True, "seq": 0,
     "storrelse": 256208, "utvalgsNavn": "Forretningsutvalget"},
    {"kildeId": "1", "name": "Unntatt", "filNavn": "1.pdf", "relasjonsType": "Vedlegg", "tilgangskode": "UO", "seq": 2},
    {"kildeId": "2", "name": "Kartbilde", "filNavn": "2.jpg", "relasjonsType": "Vedlegg", "tilgangskode": "U", "seq": 3},
    {"kildeId": "3", "name": "Hoveddokument", "filNavn": "3.pdf", "relasjonsType": "Hoveddokument", "seq": 4},
]


class Klient:
    def __init__(self):
        self.spurt = []

    def agendapunktfiler(self, dokumentnummer):
        self.spurt.append(dokumentnummer)
        return API_FILER


def _sak(**kw):
    return item("358/26", moteDato="2026-11-03", dokumentnummer="2026/27798-2", **kw)


def _pdf_med(path, sider):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for tekst in sider:
        doc.new_page().insert_text((72, 72), tekst)
    doc.save(path)


@pytest.fixture
def fra(monkeypatch):
    monkeypatch.setattr(config, "VEDLEGG_FRA", "2026-11-01")


def test_attachments_are_listed_once_filtered_and_numbered(tmp_path, fra):
    ny, gammel = _sak(), item("300/26", moteDato="2026-10-27", dokumentnummer="2026/1-1")
    klient = Klient()
    assert vedlegg.hent_lister([ny, gammel], tmp_path, client=klient) == {"listet": 1, "vedlegg": 2}
    assert klient.spurt == ["2026/27798-2"]          # the old sak stays frozen
    liste = pdf.vedlegg_liste(ny, tmp_path)
    assert [(v["nr"], v["navn"]) for v in liste] == [(1, "Svar på representantforslag"), (2, "Svar fra Bystyrets kontor")]
    assert liste[0]["url"].endswith("/fil/bk360/14881386/Svar-pa-representantforslag")
    vedlegg.hent_lister([ny], tmp_path, client=klient)
    assert klient.spurt == ["2026/27798-2"]          # never asked twice
    # a named older sak is listed anyway (358/26 on 1.11)
    vedlegg.hent_lister([gammel], tmp_path, client=klient, saker={pdf.key(gammel)})
    assert klient.spurt[-1] == "2026/1-1"


def test_attachments_join_the_download_script(tmp_path, fra):
    sak = _sak()
    store(tmp_path, [sak])
    vedlegg.hent_lister([sak], tmp_path, client=Klient())
    filer = {t["filename"]: t for t in pipeline.pdf_som_mangler([sak], tmp_path)}
    assert "571424/2026-11-03/358-26_vedlegg-1.pdf" in filer
    assert filer["571424/2026-11-03/358-26_vedlegg-2.pdf"]["kind"] == "vedlegg"
    assert "vedlegg-2.pdf" in pdf.build_script(list(filer.values()))


def test_cheap_reader_picks_pages_and_writer_gets_only_those(tmp_path, fra):
    sak = _sak()
    vedlegg.hent_lister([sak], tmp_path, client=Klient())
    _pdf_med(tmp_path / "pdf" / pdf.vedlegg_fil(sak, 1), ["Forside", "Byrådet mener åpenhet koster for mye.", "Vedlegg A"])
    _pdf_med(tmp_path / "pdf" / pdf.filename(sak, "protokoll"), ["Vedtak"])
    assert [pdf.key(i) for i in pipeline.todo("vedlegg", [sak], tmp_path)] == [pdf.key(sak)]
    modell = llm.FakeLLM([{"avsender": "kommunal", "sammendrag": "Byrådets svar.",
                           "sider": [{"side": 2, "hva": "begrunnelsen"}, {"side": 99, "hva": "finnes ikke"}]}])
    assert vedlegg.les(sak, tmp_path, modell) == "lest"
    assert "<<<VEDLEGG" in modell.prompts[0] and "Svar på representantforslag" in modell.prompts[0]
    d = vedlegg.digest(sak, tmp_path)["vedlegg"][0]
    assert d["sider"] == [{"side": 2, "hva": "begrunnelsen"}] and d["avsender"] == "kommunal"
    assert pipeline.todo("vedlegg", [sak], tmp_path) == []
    kilder = kildetekst.kilder_for(sak, tmp_path)
    assert "koster for mye" in kilder["vedlegg-1"] and "Forside" not in kilder["vedlegg-1"]
    assert "--- side 2 ---" in kilder["vedlegg-1"] and "vedlegg-2" not in kilder   # not downloaded yet
    schema = kildetekst.med_dokumenter(skribent.CARD_SCHEMA, kilder)
    assert schema["properties"]["kilder"]["items"]["properties"]["dok"]["enum"] == ["saksframlegg", "protokoll", "vedlegg-1"]
    assert skribent.CARD_SCHEMA["properties"]["kilder"]["items"]["properties"]["dok"]["enum"] == ["saksframlegg", "protokoll"]
    blokk = kildetekst.vedlegg_blokker(kilder)
    assert len(blokk) == 1 and blokk[0].startswith("<<<VEDLEGG_1")


def test_digest_makes_only_its_own_card_stale(tmp_path, fra):
    sak, gammel = _sak(), item("300/26", moteDato="2026-10-27")
    for s in (sak, gammel):
        _pdf_med(tmp_path / "pdf" / pdf.filename(s, "protokoll"), ["Vedtak"])
        kort = tmp_path / "kort" / f"{pdf.key(s)}.json"
        kort.parent.mkdir(parents=True, exist_ok=True)
        kort.write_text("{}")
        t = time.time() + 5
        os.utime(kort, (t, t))
    assert pipeline.todo("skriv", [sak, gammel], tmp_path) == []
    digest = tmp_path / "vedleggsdigest" / f"{pdf.key(sak)}.json"
    digest.parent.mkdir(parents=True)
    digest.write_text("{}")
    t = time.time() + 10
    os.utime(digest, (t, t))
    assert [pdf.key(i) for i in pipeline.todo("skriv", [sak, gammel], tmp_path)] == [pdf.key(sak)]


def test_site_links_attachment_pages_and_lists_them(tmp_path, fra):
    sak = _sak()
    vedlegg.hent_lister([sak], tmp_path, client=Klient())
    lenke = bygg._kilde(sak, {"dok": "vedlegg-1", "side": 3}, tmp_path)
    assert lenke["href"].endswith("/fil/bk360/14881386/Svar-pa-representantforslag#page=3")
    assert bygg._kilde(sak, {"dok": "vedlegg-7", "side": 3}, tmp_path) is None
    assert bygg.kildegrupper([lenke])[0]["navn"] == "Vedlegg 1"
    assert bygg.dok_navn("vedlegg-2", bestemt=True) == "vedlegg 2"
    assert pdf.er_dok("vedlegg-1") and not pdf.er_dok("vedlegg-0") and not pdf.er_dok("annet")


def test_the_panel_says_what_the_attachment_steps_did():
    run = ["2026-11-03T08:00:00+01:00 start (manuell)",
           "2026-11-03 08:00:10,000 INFO vedleggslister: 3 saker, 5 vedlegg",
           "2026-11-03 08:05:00,000 INFO vedlegg: {'antall': {'lest': 2, 'ingen vedlegg': 1}, 'feil': []}"]
    p = admin.oppsummer(run)["punkter"]
    assert "Fant 5 vedlegg til 3 nye saker." in p
    assert "Leste vedleggene til 2 saker og plukket ut sidene skribenten trenger." in p
