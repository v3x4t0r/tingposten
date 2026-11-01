"""The comment database: one SQLite file (data/debatt/debatt.db, WAL), standard library only.

Personal data kept here, and nowhere else: a reader's e-mail address, display
name, comments, votes and debattinnlegg sent through the form. Login links and sessions are stored only as SHA-256
hashes. Who voted for what is never shown to anyone, the editor included; only
the totals are. Deleting an account removes the e-mail (also from unused login
links), the name, the votes, the text of every comment (a comment with replies
stays as "[slettet]") and the moderators' notes on them; secure_delete overwrites
the freed pages.
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
  navn TEXT,
  navn_nokkel TEXT UNIQUE,   -- the name folded for comparison (tjeneste.navnenokkel)
  quiz TEXT,                 -- the rules version whose quiz the reader passed (regler.REGLER_VERSJON)
  opprettet TEXT NOT NULL,
  sperret INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS innlogging (
  hash TEXT PRIMARY KEY,
  epost TEXT NOT NULL,
  epost_nokkel TEXT NOT NULL,  -- the address without +tag, for the rate limit
  tilbake TEXT,
  opprettet TEXT NOT NULL,
  utloper TEXT NOT NULL,
  brukt INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS innlogging_epost ON innlogging(epost_nokkel, opprettet);
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
  endringer INTEGER NOT NULL DEFAULT 0,
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
-- Debattinnlegg sent through the form on the Debatt page (innsendt.py). The AI check runs
-- in the control panel on the Pi; the editor publishes, rejects or sends an edit for approval.
CREATE TABLE IF NOT EXISTS innsendt (
  id INTEGER PRIMARY KEY,
  bruker INTEGER REFERENCES brukere(id) ON DELETE SET NULL,
  navn TEXT NOT NULL,          -- the full name the innlegg is signed with
  om TEXT NOT NULL DEFAULT '',
  tittel TEXT NOT NULL,
  tekst TEXT NOT NULL,
  sak TEXT,
  -- vurderes | sortert_bort | til_redaktor | sendt_skribent | godkjent | publisert | avvist
  status TEXT NOT NULL,
  ki TEXT,                     -- JSON: the AI check's verdict
  forsok INTEGER NOT NULL DEFAULT 0,
  red_tittel TEXT,             -- the editor's version, sent to the writer for approval
  red_tekst TEXT,
  godkjenn_hash TEXT,
  godkjenn_utloper TEXT,
  fil TEXT,                    -- data/bergen/innlegg/<fil> once published
  opprettet TEXT NOT NULL,
  endret TEXT
);
CREATE INDEX IF NOT EXISTS innsendt_status ON innsendt(status, opprettet);
CREATE TABLE IF NOT EXISTS testere (       -- the test panel's raters (testpanel.py): invite link, no account
  id INTEGER PRIMARY KEY,
  navn TEXT NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,           -- sha256 of the link's token; the token itself is never stored
  opprettet TEXT NOT NULL,
  stengt TEXT,
  sist TEXT
);
CREATE TABLE IF NOT EXISTS testvurderinger (
  tester INTEGER NOT NULL REFERENCES testere(id) ON DELETE CASCADE,
  sak TEXT NOT NULL,
  bor_vite INTEGER,                          -- 1–5: should residents know about this?
  konkret INTEGER,                           -- 1–5: does something concrete change in everyday life now?
  bunke TEXT,                                -- forside | kort | stoy
  klar TEXT,                                 -- ja | delvis | nei: is the card clear?
  kommentar TEXT,
  bekreftet INTEGER NOT NULL DEFAULT 0,      -- part 2: the tester kept a flagged answer on purpose
  tid TEXT NOT NULL,
  PRIMARY KEY (tester, sak)
);
CREATE TABLE IF NOT EXISTS testlogg (        -- every answer as given, part 1 or the part 2 review
  tester INTEGER NOT NULL REFERENCES testere(id) ON DELETE CASCADE,
  sak TEXT NOT NULL,
  felt TEXT NOT NULL,
  verdi TEXT,
  del INTEGER NOT NULL,
  tid TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testpanel_moter (mote TEXT PRIMARY KEY);  -- the editor's choice; none = the newest
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
            c.execute("PRAGMA secure_delete = ON")
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
            rad = c.execute("SELECT epost FROM brukere WHERE id = ?", (bruker,)).fetchone()
            if rad:
                c.execute("DELETE FROM innlogging WHERE epost = ?", (rad["epost"],))
            c.execute("UPDATE vurderinger SET data = '{}' WHERE kommentar IN "
                      "(SELECT id FROM kommentarer WHERE bruker = ?)", (bruker,))
            for (kid,) in c.execute("SELECT kommentar FROM stemmer WHERE bruker = ?", (bruker,)).fetchall():
                c.execute("DELETE FROM stemmer WHERE bruker = ? AND kommentar = ?", (bruker, kid))
                self._tell_stemmer(c, kid)
            c.execute("UPDATE kommentarer SET tekst = '', status = 'slettet', endret = ? WHERE bruker = ?",
                      (iso(na()), bruker))
            # Innlegg not yet published go; a published one is the writer's signed text on the site.
            c.execute("DELETE FROM innsendt WHERE bruker = ? AND status != 'publisert'", (bruker,))
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
            "innloggingslenker": [dict(r) for r in self.les(
                "SELECT opprettet, utloper, brukt FROM innlogging WHERE epost = ?", b["epost"])] if b else [],
            "debattinnlegg": [dict(r) for r in self.les(
                "SELECT id, navn, om, tittel, tekst, sak, status, red_tittel, red_tekst, opprettet, endret "
                "FROM innsendt WHERE bruker = ? ORDER BY id", bruker)],
            "vurderinger": [dict(r) for r in self.les(
                "SELECT v.kommentar, v.tid, v.utfall, v.regel, v.hvem, v.data FROM vurderinger v "
                "JOIN kommentarer k ON k.id = v.kommentar WHERE k.bruker = ? ORDER BY v.id", bruker)],
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
