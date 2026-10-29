import json

from tingposten import cli, config


def test_pdf_urler_mangler_writes_one_script_for_what_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = {"kategori": "Saker til behandling", "innstillingFilId": "8", "vedtakFilId": "9", "seq": 1}
    items = {
        "a": {**base, "id": "a", "utvalgId": "571424", "moteDato": "2026-09-23", "referansenr": "1/26", "tittel": "A"},
        "b": {**base, "id": "b", "utvalgId": "571424", "moteDato": "2026-10-27", "referansenr": "2/26", "tittel": "B"},
        "c": {**base, "id": "c", "utvalgId": "571424", "moteDato": "2026-10-27", "referansenr": "3/26", "tittel": "C",
              "vedtakFilId": None},  # not decided yet → not needed
    }
    (tmp_path / "agendapunkter").mkdir()
    (tmp_path / "agendapunkter" / "571424.json").write_text(json.dumps(items))
    for kind in ("saksframlegg", "protokoll"):  # sak a is already downloaded
        f = tmp_path / "pdf" / f"571424/2026-09-23/1-26_{kind}.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF")
    script = tmp_path / "last-ned-mangler.sh"
    assert cli.main(["pdf-urler", "--mangler", "--fra", "2026-01-01", "--script", str(script)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[1] for line in out] == ["571424/2026-10-27/2-26_saksframlegg.pdf",
                                                    "571424/2026-10-27/2-26_protokoll.pdf"]
    assert script.exists() and "Last ned 2 PDF-er" in script.read_text()
    # once everything is on disk the old script is removed
    (tmp_path / "pdf" / "571424/2026-10-27").mkdir(parents=True)
    for kind in ("saksframlegg", "protokoll"):
        (tmp_path / "pdf" / f"571424/2026-10-27/2-26_{kind}.pdf").write_bytes(b"%PDF")
    assert cli.main(["pdf-urler", "--mangler", "--fra", "2026-01-01", "--script", str(script)]) == 0
    assert not script.exists()


def test_pdf_urler_needs_utvalg_without_mangler(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert cli.main(["pdf-urler"]) == 1
