"""The comment database: one SQLite file (data/debatt/debatt.db, WAL), standard library only.

Personal data kept here, and nowhere else: a reader's e-mail address, display
name, comments and votes. Login links and sessions are stored only as SHA-256
hashes. Who voted for what is never shown to anyone, the editor included; only
the totals are. Deleting an account removes the e-mail, the name, the votes and
the text of every comment (a comment with replies stays as "[slettet]").
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKJEMA = """
CREATE TABLE IF NOT EXISTS brukere (
  id INTEGER PRIMARY KEY,
  epost TEXT UNIQUE NOT NULL,
  navn TEXT UNIQUE COLLATE NOCASE,
  opprettet TEXT NOT NULL,
  sperret INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS innlogging (
  hash TEXT PRIMARY KEY,
  epost TEXT NOT NULL,
  tilbake TEXT,
  opprettet TEXT NOT NULL,
  utloper TEXT NOT NULL,
  brukt INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS innlogging_epost ON innlogging(epost, opprettet);
CREATE TABLE IF NOT EXISTS okter (
  hash TEXT PRIMARY KEY,
  bruker INTEGER NOT NULL REFERENCES brukere(id) ON DELETE CASCADE,
  opprettet TEXT NOT NULL,
  utloper TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kommentarer (
  id INTEGER PRIMARY KEY,
  sak TEXT NOT NULL,
  forelder INTEGER REFERENCES kommentarer(id),
  bruker INTEGER REFERENCES brukere(id) ON DELETE SET NULL,
  tekst TEXT NOT NULL,
  -- venter | publisert | avvist | til_redaktor | slettet (by the author) | fjernet (by the editor)
  status TEXT NOT NULL,
  regel TEXT,
  opprettet TEXT NOT NULL,
  endret TEXT,
  opp INTEGER NOT NULL DEFAULT 0,
  ned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS kommentarer_sak ON kommentarer(sak, status);
CREATE INDEX IF NOT EXISTS kommentarer_bruker ON kommentarer(bruker, opprettet);
CREATE TABLE IF NOT EXISTS stemmer (
  bruker INTEGER NOT NULL REFERENCES brukere(id) ON DELETE CASCADE,
  kommentar INTEGER NOT NULL REFERENCES kommentarer(id) ON DELETE CASCADE,
  verdi INTEGER NOT NULL CHECK (verdi IN (-1, 1)),
  PRIMARY KEY (bruker, kommentar)
);
-- Every moderation decision, for the editor and for the audit trail.
CREATE TABLE IF NOT EXISTS vurderinger (
  id INTEGER PRIMARY KEY,
  kommentar INTEGER NOT NULL REFERENCES kommentarer(id) ON DELETE CASCADE,
  tid TEXT NOT NULL,
  utfall TEXT NOT NULL,
  regel TEXT,
  hvem TEXT NOT NULL,   -- the moderator chain, or "redaktør"
  data TEXT NOT NULL    -- JSON: every backend's verdict and errors
);
CREATE TABLE IF NOT EXISTS rapporter (
  kommentar INTEGER NOT NULL REFERENCES kommentarer(id) ON DELETE CASCADE,
  bruker INTEGER NOT NULL REFERENCES brukere(id) ON DELETE CASCADE,
  tid TEXT NOT NULL,
  grunn TEXT,
  behandlet INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (kommentar, bruker)
);
"""


def na() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def siden(**kw) -> str:
    return iso(na() - timedelta(**kw))


class Db:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lokal = threading.local()
        self._skriv = threading.Lock()  # one writer at a time; SQLite would serialise anyway
        with self._skriv:
            self._conn().executescript(SKJEMA)  # executescript commits by itself; no BEGIN around it

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._lokal, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                c.execute("PRAGMA journal_mode = WAL")
            self._lokal.c = c
        return c

    @contextmanager
    def tx(self):
        c = self._conn()
        with self._skriv:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def les(self, sql: str, *args) -> list[sqlite3.Row]:
        return self._conn().execute(sql, args).fetchall()

    def en(self, sql: str, *args) -> sqlite3.Row | None:
        return self._conn().execute(sql, args).fetchone()

    # --- users and sessions ---------------------------------------------------

    def bruker_for_epost(self, c: sqlite3.Connection, epost: str) -> sqlite3.Row:
        rad = c.execute("SELECT * FROM brukere WHERE epost = ?", (epost,)).fetchone()
        if rad is None:
            c.execute("INSERT INTO brukere (epost, opprettet) VALUES (?, ?)", (epost, iso(na())))
            rad = c.execute("SELECT * FROM brukere WHERE epost = ?", (epost,)).fetchone()
        return rad

    def slett_bruker(self, bruker: int) -> None:
        """Everything about the reader goes; replies to their comments keep a placeholder."""
        with self.tx() as c:
            for (kid,) in c.execute("SELECT kommentar FROM stemmer WHERE bruker = ?", (bruker,)).fetchall():
                c.execute("DELETE FROM stemmer WHERE bruker = ? AND kommentar = ?", (bruker, kid))
                self._tell_stemmer(c, kid)
            c.execute("UPDATE kommentarer SET tekst = '', status = 'slettet', endret = ? WHERE bruker = ?",
                      (iso(na()), bruker))
            c.execute("DELETE FROM brukere WHERE id = ?", (bruker,))

    def eksport(self, bruker: int) -> dict:
        b = self.en("SELECT epost, navn, opprettet FROM brukere WHERE id = ?", bruker)
        return {
            "konto": dict(b) if b else None,
            "kommentarer": [dict(r) for r in self.les(
                "SELECT id, sak, forelder, tekst, status, regel, opprettet, endret FROM kommentarer "
                "WHERE bruker = ? ORDER BY id", bruker)],
            "stemmer": [dict(r) for r in self.les("SELECT kommentar, verdi FROM stemmer WHERE bruker = ?", bruker)],
            "rapporter": [dict(r) for r in self.les("SELECT kommentar, tid, grunn FROM rapporter WHERE bruker = ?",
                                                    bruker)],
        }

    # --- votes -----------------------------------------------------------------

    def stem(self, bruker: int, kommentar: int, verdi: int) -> tuple[int, int]:
        with self.tx() as c:
            if verdi == 0:
                c.execute("DELETE FROM stemmer WHERE bruker = ? AND kommentar = ?", (bruker, kommentar))
            else:
                c.execute("INSERT INTO stemmer (bruker, kommentar, verdi) VALUES (?, ?, ?) "
                          "ON CONFLICT (bruker, kommentar) DO UPDATE SET verdi = excluded.verdi",
                          (bruker, kommentar, verdi))
            return self._tell_stemmer(c, kommentar)

    @staticmethod
    def _tell_stemmer(c: sqlite3.Connection, kommentar: int) -> tuple[int, int]:
        opp, ned = c.execute("SELECT COALESCE(SUM(verdi = 1), 0), COALESCE(SUM(verdi = -1), 0) FROM stemmer "
                             "WHERE kommentar = ?", (kommentar,)).fetchone()
        c.execute("UPDATE kommentarer SET opp = ?, ned = ? WHERE id = ?", (opp, ned, kommentar))
        return opp, ned

    # --- moderation log ---------------------------------------------------------

    def logg_vurdering(self, c: sqlite3.Connection, kommentar: int, utfall: str, regel: str | None, hvem: str,
                       data: dict) -> None:
        c.execute("INSERT INTO vurderinger (kommentar, tid, utfall, regel, hvem, data) VALUES (?, ?, ?, ?, ?, ?)",
                  (kommentar, iso(na()), utfall, regel, hvem, json.dumps(data, ensure_ascii=False)))
