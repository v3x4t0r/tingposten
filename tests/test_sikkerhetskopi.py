"""deploy/sikkerhetskopi.sh: a checked copy of the comments database, on the SD card and a
second disk, never into an unmounted folder."""
import sqlite3
import subprocess
from pathlib import Path

SKRIPT = Path(__file__).parent.parent / "deploy" / "sikkerhetskopi.sh"


def kjor(repo: Path, **miljo) -> subprocess.CompletedProcess:
    (repo / "deploy").mkdir(exist_ok=True)
    kopi = repo / "deploy" / "sikkerhetskopi.sh"
    kopi.write_text(SKRIPT.read_text())
    env = {"PATH": "/usr/bin:/bin", **miljo}
    return subprocess.run(["bash", str(kopi)], cwd=repo, env=env, capture_output=True, text=True, timeout=60)


def logg(repo: Path) -> str:
    return "".join(p.read_text() for p in (repo / "data/logs/sikkerhetskopi").glob("*.log"))


def test_copies_the_database_checked_and_private(tmp_path):
    db = tmp_path / "data/debatt/debatt.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("create table kommentar (tekst)")
    con.execute("insert into kommentar values ('hei')")
    con.commit()                      # still open, as the comment server keeps it
    r = kjor(tmp_path)
    con.close()
    assert r.returncode == 0, logg(tmp_path)
    [kopi] = (tmp_path / "data/backup/debatt").glob("debatt-*.db")
    assert sqlite3.connect(kopi).execute("select tekst from kommentar").fetchall() == [("hei",)]
    assert kopi.stat().st_mode & 0o077 == 0 and (kopi.parent.stat().st_mode & 0o077) == 0
    assert "done" in logg(tmp_path)


def test_an_unmounted_second_disk_fails_and_writes_nothing_there(tmp_path):
    db = tmp_path / "data/debatt/debatt.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).execute("create table t (x)").connection.commit()
    mal = tmp_path / "ikke-montert" / "tingposten-backup"
    r = kjor(tmp_path, TINGPOSTEN_BACKUP_KOPI=str(mal))
    assert r.returncode == 1
    assert not mal.exists() and "ikke montert" in logg(tmp_path)
    assert list((tmp_path / "data/backup/debatt").glob("debatt-*.db"))   # the SD card copy is still made


def test_no_database_yet_is_fine(tmp_path):
    assert kjor(tmp_path).returncode == 0
