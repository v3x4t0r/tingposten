from tingposten import pdf

SAK = {
    "id": "a",
    "utvalgId": "571424",
    "moteDato": "2026-09-23",
    "seq": 5,
    "kategori": "Saker til behandling",
    "tittel": "Reguleringsplan for Eksempelveien",
    "referansenr": "372/26",
    "saksnr": "2026/1",
    "innstillingFilId": "111",
    "innstillingAlternativBeskrivelse": "Framstilling-reguleringsplan",
    "vedtakFilId": "222",
    "vedtakAlternativBeskrivelse": "Protokoll-reguleringsplan",
}
INNKALLING = {
    "id": "b",
    "moteDato": "2026-09-23",
    "seq": 1,
    "kategori": "Innkalling",
    "tittel": "Innkalling Bergen bystyre 23.09.2026",
    "innstillingFilId": "999",
    "innstillingAlternativBeskrivelse": "Framstilling-innkalling",
    "vedtakFilId": None,
}


def test_pdf_url_with_and_without_slug():
    assert pdf.pdf_url("111", "slug").endswith("/fil/bk360/111/slug")
    assert pdf.pdf_url("111", None).endswith("/fil/bk360/111")


def test_is_noise():
    assert pdf.is_noise(INNKALLING)
    assert not pdf.is_noise(SAK)


def test_targets_skips_noise_and_missing_files():
    t = pdf.targets([SAK, INNKALLING])
    assert [x["kind"] for x in t] == ["saksframlegg", "protokoll"]
    assert t[0]["filename"] == "571424/2026-09-23/372-26_saksframlegg.pdf"
    assert t[0]["url"].endswith("/fil/bk360/111/Framstilling-reguleringsplan")


def test_targets_all_includes_noise_but_only_existing_files():
    t = pdf.targets([SAK, INNKALLING], drop_noise=False)
    # sorted by seq: innkalling (seq 1, saksframlegg only) before the sak (seq 5)
    assert [x["kind"] for x in t] == ["saksframlegg", "saksframlegg", "protokoll"]


def test_build_script_is_polite_and_quotes_urls():
    script = pdf.build_script(pdf.targets([SAK]))
    assert script.startswith("#!/usr/bin/env bash")
    assert "sleep 5" in script
    assert "--user-agent=" in script
    assert "Tingposten/0.1" in script
    # URL passed as a quoted argument, not interpolated into flags
    assert "'https://www.bergen.kommune.no/politikere-utvalg/api/fil/bk360/111/Framstilling-reguleringsplan'" in script


def test_build_script_resumes_and_stops_on_refusal(tmp_path):
    import subprocess
    script = pdf.build_script(pdf.targets([SAK]), dest=str(tmp_path / "pdf"))
    assert 'DEST="${1:-' + str(tmp_path / "pdf") + '}"' in script
    assert '[ -s "$DEST/$3" ]' in script and ".part" in script and "trap " in script
    # both files already on disk → nothing is fetched (no wget on PATH is needed)
    for kind in ("saksframlegg", "protokoll"):
        f = tmp_path / "pdf" / f"571424/2026-09-23/372-26_{kind}.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF")
    path = tmp_path / "s.sh"
    path.write_text(script)
    done = subprocess.run(["bash", str(path)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 0 and done.stdout == ""
