"""What the comment server does, without HTTP: login by e-mail link, display names,
comments and replies, votes, reports, sorting, and the moderation queue.

The server (server.py) only translates requests to these calls; tests call them
directly. Errors a reader can fix are raised as Feil(kode, melding); the
message is Norwegian and shown as it is.
"""

import hashlib
import json
import logging
import math
import queue
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta

from .db import Db, iso, na, siden
from .moderering import Kjede, Sak
from .regler import QUIZ, REGLER, REGLER_VERSJON

log = logging.getLogger(__name__)

LENKE_GYLDIG = timedelta(minutes=15)
OKT_GYLDIG = timedelta(days=30)
MAKS_TEKST = 3000
MIN_TEKST = 2
NAVN_MIN, NAVN_MAKS = 2, 30
LENKER_PER_TIME = 3            # login e-mails per address (without +tag) and hour
LENKER_PER_TIME_TOTALT = 200   # all login e-mails per hour: protects the mail account's reputation
KOMMENTAR_PAUSE_S = 30         # between two comments (or edits) from one account
KOMMENTARER_PER_DOGN = 30
NYE_KONTO_PER_DOGN = 5         # the first 24 hours of an account
MAKS_ENDRINGER = 5             # edits per comment; each one is judged again
NAVNEFORSOK_PER_TIME = 5
MAKS_DYBDE = 8                 # replies to replies …; deeper threads are refused
# Names a reader can't take or hide inside their own (folded: see navnenokkel).
RESERVERTE_NAVN = ("tingposten", "redaksjon", "redaktor", "redaktoren", "admin", "moderator", "bergenkommune",
                   "kommunen", "byradet", "bystyret", "ordforer")
# Latin look-alikes in other scripts, folded before the reserved names are compared.
_FORVEKSLING = str.maketrans("аеорсухіјӏԁոѕԛԝαεορτυνιкмтвн", "aeopcyxijldnsqwaeoptuvikmtbh")

_EPOST = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_NAVN_TEGN = re.compile(r"^[\w .'\-]+$")


def navnenokkel(navn: str) -> str:
    """A display name folded for comparison: compatibility forms, case, look-alike
    letters from other scripts, Norwegian letters and everything but letters and
    digits. "Тingposten" (Cyrillic T) and "TING-posten" give the same key."""
    n = unicodedata.normalize("NFKC", navn).casefold().translate(_FORVEKSLING)
    n = n.replace("æ", "ae").replace("ø", "o").replace("å", "a")
    n = "".join(ch for ch in unicodedata.normalize("NFKD", n) if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]", "", n)


def _epost_nokkel(epost: str) -> str:
    """The address without a +tag, so victim+1@, victim+2@ … share one rate limit."""
    lokal, _, domene = epost.partition("@")
    return f"{lokal.split('+', 1)[0]}@{domene}"


PERIODER = {"7": 7, "30": 30, "aar": None}   # "Mest kommentert" on the Debatt page


class Feil(Exception):
    def __init__(self, kode: int, melding: str):
        super().__init__(melding)
        self.kode, self.melding = kode, melding


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def wilson(opp: int, ned: int, z: float = 1.96) -> float:
    """Lower bound of the 95 % Wilson interval for the share of up-votes:
    Reddit's 'best' comment order. Few votes → low bound, so one early up-vote
    doesn't beat a comment with fifty up and five down."""
    n = opp + ned
    if n == 0:
        return 0.0
    p = opp / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


SORTERING = {
    "best": lambda k: (wilson(k["opp"], k["ned"]), k["opprettet"]),
    "topp": lambda k: (k["opp"] - k["ned"], k["opprettet"]),
    "nye": lambda k: (k["opprettet"], k["id"]),
}


def rens_tekst(tekst) -> str:
    if not isinstance(tekst, str):
        raise Feil(400, "Kommentaren mangler.")
    tekst = unicodedata.normalize("NFC", tekst).replace("\r\n", "\n").replace("\r", "\n")
    tekst = "".join(ch for ch in tekst if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    tekst = re.sub(r"\n{3,}", "\n\n", tekst).strip()
    if len(tekst) < MIN_TEKST:
        raise Feil(400, "Kommentaren er tom.")
    if len(tekst) > MAKS_TEKST:
        raise Feil(400, f"Kommentaren er for lang (høyst {MAKS_TEKST} tegn).")
    return tekst


@dataclass
class Bruker:
    id: int
    navn: str | None
    opprettet: str
    sperret: bool
    quiz: str | None = None  # the rules version whose quiz the reader passed


class Tjeneste:
    def __init__(self, db: Db, moderator: Kjede, saker: dict[str, dict], send_lenke, site_url: str,
                 umiddelbar_moderering: bool = False, vis_lenke: bool = False):
        """`saker`: key → {"tittel", "ingress", "lenke"} (written by bygg). `send_lenke(epost, lenke)`
        sends the login e-mail. With `umiddelbar_moderering` comments are judged inside the
        call (tests); otherwise one background thread works through the queue."""
        self.db, self.moderator, self.saker = db, moderator, saker
        self.send_lenke, self.site_url = send_lenke, site_url.rstrip("/") + "/"
        self.umiddelbar = umiddelbar_moderering
        # Only `tingposten debatt-lokal`: the login link comes back to the page instead of
        # by e-mail. Never on a real server: anyone could log in as any address.
        self.vis_lenke = vis_lenke
        self._ko: queue.Queue[int] = queue.Queue()
        self._i_ko: set[int] = set()
        self._ko_lås = threading.Lock()
        self._traad: threading.Thread | None = None
        self._navneforsok: dict[int, deque] = defaultdict(deque)
        from .. import config

        self.reserverte = RESERVERTE_NAVN + tuple(
            navnenokkel(n) for n in (getattr(config, "ANSVARLIG_REDAKTOR", None), getattr(config, "UTGIVER", None)) if n)

    # --- login -----------------------------------------------------------------

    def be_om_lenke(self, epost, tilbake=None) -> str | None:
        """Send a login link. Says nothing about whether the address has an account.
        Returns the link only in local test mode (vis_lenke)."""
        epost = (epost or "").strip().lower() if isinstance(epost, str) else ""
        if not _EPOST.match(epost) or len(epost) > 254:
            raise Feil(400, "Skriv en gyldig e-postadresse.")
        tilbake = tilbake if isinstance(tilbake, str) and tilbake.startswith(self.site_url) else None
        nokkel = _epost_nokkel(epost)
        token = secrets.token_urlsafe(32)
        with self.db.tx() as c:  # count and insert in one transaction: parallel requests can't slip past
            c.execute("DELETE FROM innlogging WHERE utloper < ?", (siden(days=1),))
            if c.execute("SELECT COUNT(*) FROM innlogging WHERE epost_nokkel = ? AND opprettet > ?",
                         (nokkel, siden(hours=1))).fetchone()[0] >= LENKER_PER_TIME:
                raise Feil(429, "Du har bedt om mange lenker. Prøv igjen om en time.")
            if c.execute("SELECT COUNT(*) FROM innlogging WHERE opprettet > ?",
                         (siden(hours=1),)).fetchone()[0] >= LENKER_PER_TIME_TOTALT:
                log.warning("login e-mail cap reached (%d per hour)", LENKER_PER_TIME_TOTALT)
                raise Feil(503, "Innloggingen er travel akkurat nå. Prøv igjen om en stund.")
            c.execute("INSERT INTO innlogging (hash, epost, epost_nokkel, tilbake, opprettet, utloper) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (_hash(token), epost, nokkel, tilbake, iso(na()), iso(na() + LENKE_GYLDIG)))
        lenke = f"{self.site_url}bergen/logg-inn/#t={token}"
        try:
            self.send_lenke(epost, lenke)
        except Exception:
            log.exception("could not send a login e-mail")
            raise Feil(503, "Vi fikk ikke sendt e-posten akkurat nå. Prøv igjen litt senere.") from None
        return lenke if self.vis_lenke else None

    def bekreft(self, token) -> dict:
        """A login link's token → a session token (kept by the reader's browser)."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise Feil(400, "Lenken er ugyldig.")
        with self.db.tx() as c:
            rad = c.execute("SELECT * FROM innlogging WHERE hash = ?", (_hash(token),)).fetchone()
            if rad is None or rad["brukt"] or rad["utloper"] < iso(na()):
                raise Feil(400, "Lenken er brukt eller utløpt. Be om en ny.")
            c.execute("UPDATE innlogging SET brukt = 1 WHERE hash = ?", (rad["hash"],))
            bruker = self.db.bruker_for_epost(c, rad["epost"])
            if bruker["sperret"]:
                raise Feil(403, "Kontoen er sperret.")
            okt = secrets.token_urlsafe(32)
            c.execute("DELETE FROM okter WHERE utloper < ?", (iso(na()),))
            c.execute("INSERT INTO okter (hash, bruker, opprettet, utloper) VALUES (?, ?, ?, ?)",
                      (_hash(okt), bruker["id"], iso(na()), iso(na() + OKT_GYLDIG)))
        return {"okt": okt, "navn": bruker["navn"], "tilbake": rad["tilbake"]}

    def bruker(self, okt: str | None) -> Bruker | None:
        if not okt:
            return None
        rad = self.db.en("SELECT b.* FROM okter o JOIN brukere b ON b.id = o.bruker WHERE o.hash = ? AND o.utloper > ?",
                         _hash(okt), iso(na()))
        if rad is None or rad["sperret"]:
            return None
        return Bruker(rad["id"], rad["navn"], rad["opprettet"], bool(rad["sperret"]), rad["quiz"])

    def logg_ut(self, okt: str, alle: bool = False) -> None:
        with self.db.tx() as c:
            if alle:
                rad = c.execute("SELECT bruker FROM okter WHERE hash = ?", (_hash(okt),)).fetchone()
                if rad:
                    c.execute("DELETE FROM okter WHERE bruker = ?", (rad["bruker"],))
            else:
                c.execute("DELETE FROM okter WHERE hash = ?", (_hash(okt),))

    def sett_navn(self, b: Bruker, navn) -> str:
        navn = re.sub(r"\s+", " ", unicodedata.normalize("NFC", navn or "")).strip() if isinstance(navn, str) else ""
        if not NAVN_MIN <= len(navn) <= NAVN_MAKS or not _NAVN_TEGN.match(navn):
            raise Feil(400, f"Navnet må ha {NAVN_MIN}–{NAVN_MAKS} tegn: bokstaver, tall, mellomrom, punktum, "
                            "bindestrek eller apostrof.")
        nokkel = navnenokkel(navn)
        if len(nokkel) < NAVN_MIN or any(r in nokkel for r in self.reserverte):
            raise Feil(400, "Det navnet kan ikke brukes.")
        forsok = self._navneforsok[b.id]
        while forsok and forsok[0] < time.monotonic() - 3600:
            forsok.popleft()
        if len(forsok) >= NAVNEFORSOK_PER_TIME:
            raise Feil(429, "Du har prøvd mange navn. Prøv igjen om en time.")
        forsok.append(time.monotonic())
        if self.db.en("SELECT 1 FROM brukere WHERE navn_nokkel = ? AND id != ?", nokkel, b.id):
            raise Feil(409, "Navnet er tatt.")
        # Only a moderator at home sees a display name (it may identify the reader).
        lokal = self.moderator.lokal() if self.moderator else None
        if lokal:
            svar = lokal.avgjor(f"Visningsnavn: {navn}", Sak("visningsnavn", "Et nytt visningsnavn i debatten"))
            if svar.utfall == "avvis":
                raise Feil(400, f"Navnet bryter debattreglene ({REGLER[svar.regel][0].lower()}).")
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM brukere WHERE navn_nokkel = ? AND id != ?", (nokkel, b.id)).fetchone():
                raise Feil(409, "Navnet er tatt.")
            c.execute("UPDATE brukere SET navn = ?, navn_nokkel = ? WHERE id = ?", (navn, nokkel, b.id))
        return navn

    # --- the quiz -----------------------------------------------------------------

    @staticmethod
    def quiz() -> dict:
        """The quiz questions, without the answers."""
        return {"versjon": REGLER_VERSJON, "sporsmal": [{"id": q["id"], "tekst": q["tekst"]} for q in QUIZ]}

    def svar_quiz(self, b: Bruker, svar) -> dict:
        """Check every answer ({id: True for "lov"}); all right passes this rules version.
        The reader sees whether each answer was right and why, and can try again."""
        if not isinstance(svar, dict) or set(svar) != {q["id"] for q in QUIZ} or \
                any(type(v) is not bool for v in svar.values()):
            raise Feil(400, "Svar på alle spørsmålene.")
        resultat = [{"id": q["id"], "riktig": svar[q["id"]] == q["lov"], "lov": q["lov"],
                     "forklaring": q["forklaring"]} for q in QUIZ]
        bestatt = all(r["riktig"] for r in resultat)
        if bestatt:
            with self.db.tx() as c:
                c.execute("UPDATE brukere SET quiz = ? WHERE id = ?", (REGLER_VERSJON, b.id))
        return {"bestatt": bestatt, "svar": resultat}

    def _sjekk_skriverett(self, b: Bruker) -> None:
        if not b.navn:
            raise Feil(400, "Velg et visningsnavn først.")
        if b.quiz != REGLER_VERSJON:
            raise Feil(403, "Ta quizen om debattreglene før du skriver.")

    # --- comments --------------------------------------------------------------

    def _sak(self, key) -> Sak:
        info = self.saker.get(key) if isinstance(key, str) else None
        if info is None:
            raise Feil(404, "Ukjent sak.")
        return Sak(key, info.get("tittel", ""), info.get("ingress", ""))

    def ny_kommentar(self, b: Bruker, sak_key, tekst, forelder=None) -> dict:
        self._sjekk_skriverett(b)
        sak = self._sak(sak_key)
        tekst = rens_tekst(tekst)
        if forelder is not None:
            f = self.db.en("SELECT sak, status FROM kommentarer WHERE id = ?", forelder) if type(forelder) is int else None
            if f is None or f["sak"] != sak.key or f["status"] != "publisert":
                raise Feil(400, "Kommentaren du svarer på finnes ikke.")
            if self._dybde(forelder) >= MAKS_DYBDE:
                raise Feil(400, "Tråden er for lang. Svar lenger opp.")
        self._sjekk_tempo(b)
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO kommentarer (sak, forelder, bruker, tekst, status, opprettet) "
                            "VALUES (?, ?, ?, ?, 'venter', ?)", (sak.key, forelder, b.id, tekst, iso(na())))
            kid = cur.lastrowid
        self._moderer_senere(kid)
        return self.kommentar(kid, b)

    def _dybde(self, kid: int) -> int:
        """How many comments up to the top of the thread (the top comment is 1)."""
        n = 0
        while kid is not None and n <= MAKS_DYBDE:
            rad = self.db.en("SELECT forelder FROM kommentarer WHERE id = ?", kid)
            kid, n = (rad["forelder"] if rad else None), n + 1
        return n

    def _sjekk_tempo(self, b: Bruker) -> None:
        if self.db.en("SELECT 1 FROM kommentarer WHERE bruker = ? AND opprettet > ?", b.id,
                      siden(seconds=KOMMENTAR_PAUSE_S)):
            raise Feil(429, "Vent litt før du skriver en ny kommentar.")
        grense = NYE_KONTO_PER_DOGN if b.opprettet > siden(days=1) else KOMMENTARER_PER_DOGN
        if self.db.en("SELECT COUNT(*) AS n FROM kommentarer WHERE bruker = ? AND opprettet > ?", b.id,
                      siden(days=1))["n"] >= grense:
            raise Feil(429, f"Du har skrevet {grense} kommentarer siste døgn. Prøv igjen senere.")

    def endre(self, b: Bruker, kid, tekst) -> dict:
        self._sjekk_skriverett(b)
        tekst = rens_tekst(tekst)
        egen = self._egen(b, kid)
        if egen["status"] in ("slettet", "fjernet"):
            raise Feil(400, "Kommentaren kan ikke endres.")
        if egen["endringer"] >= MAKS_ENDRINGER:
            raise Feil(400, "Kommentaren er endret mange ganger. Slett den og skriv en ny.")
        if self.db.en("SELECT 1 FROM kommentarer WHERE bruker = ? AND (opprettet > ? OR endret > ?)", b.id,
                      siden(seconds=KOMMENTAR_PAUSE_S), siden(seconds=KOMMENTAR_PAUSE_S)):
            raise Feil(429, "Vent litt før du endrer.")
        with self.db.tx() as c:
            c.execute("UPDATE kommentarer SET tekst = ?, status = 'venter', regel = NULL, endret = ?, "
                      "endringer = endringer + 1 WHERE id = ?", (tekst, iso(na()), kid))
        self._moderer_senere(kid)
        return self.kommentar(kid, b)

    def slett(self, b: Bruker, kid) -> None:
        self._egen(b, kid)
        with self.db.tx() as c:
            c.execute("UPDATE kommentarer SET tekst = '', status = 'slettet', endret = ? WHERE id = ?", (iso(na()), kid))

    def be_om_manuell(self, b: Bruker, kid) -> dict:
        """A stopped comment's author asks the editor to look at it."""
        if self._egen(b, kid)["status"] != "avvist":
            raise Feil(400, "Bare en stoppet kommentar kan sendes til redaktøren.")
        with self.db.tx() as c:
            c.execute("UPDATE kommentarer SET status = 'til_redaktor' WHERE id = ?", (kid,))
            self.db.logg_vurdering(c, kid, "redaktor", None, "leseren", {"grunn": "ba om manuell vurdering"})
        return self.kommentar(kid, b)

    def _egen(self, b: Bruker, kid):
        rad = self.db.en("SELECT * FROM kommentarer WHERE id = ?", kid) if type(kid) is int else None
        if rad is None or rad["bruker"] != b.id:
            raise Feil(404, "Fant ikke kommentaren.")
        return rad

    def stem(self, b: Bruker, kid, verdi) -> dict:
        if verdi not in (-1, 0, 1) or type(verdi) is not int:
            raise Feil(400, "Ugyldig stemme.")
        rad = self.db.en("SELECT bruker, status FROM kommentarer WHERE id = ?", kid) if type(kid) is int else None
        if rad is None or rad["status"] != "publisert":
            raise Feil(404, "Fant ikke kommentaren.")
        if rad["bruker"] == b.id:
            raise Feil(400, "Du kan ikke stemme på din egen kommentar.")
        opp, ned = self.db.stem(b.id, kid, verdi)
        return {"opp": opp, "ned": ned, "poeng": opp - ned, "min_stemme": verdi}

    def rapporter(self, b: Bruker, kid, grunn=None) -> None:
        rad = self.db.en("SELECT status FROM kommentarer WHERE id = ?", kid) if type(kid) is int else None
        if rad is None or rad["status"] != "publisert":
            raise Feil(404, "Fant ikke kommentaren.")
        grunn = grunn.strip()[:500] if isinstance(grunn, str) else None
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO rapporter (kommentar, bruker, tid, grunn) VALUES (?, ?, ?, ?)",
                      (kid, b.id, iso(na()), grunn))

    # --- reading ----------------------------------------------------------------

    def _node(self, r, b: Bruker | None, stemmer: dict[int, int]) -> dict:
        egen = b is not None and r["bruker"] == b.id
        node = {"id": r["id"], "forelder": r["forelder"], "navn": r["navn"] or "", "tid": r["opprettet"],
                "endret": r["endret"], "status": r["status"], "poeng": r["opp"] - r["ned"], "egen": egen,
                "min_stemme": stemmer.get(r["id"], 0), "svar": []}
        if r["status"] in ("slettet", "fjernet"):
            node.update(tekst="", navn="")
        elif r["status"] != "publisert" and not egen:
            # Someone else's comment that waits or was stopped (after an edit), shown only
            # because it has published replies: a placeholder, nothing of its text.
            node.update(tekst="", navn="", status="skjult")
        else:
            node["tekst"] = r["tekst"]
        if egen and r["status"] == "avvist" and r["regel"] in REGLER:
            node["regel"] = {"key": r["regel"], "navn": REGLER[r["regel"]][0], "tekst": REGLER[r["regel"]][1]}
        return node

    def kommentar(self, kid: int, b: Bruker | None) -> dict:
        r = self.db.en("SELECT k.*, u.navn FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker WHERE k.id = ?", kid)
        if r is None or (r["status"] != "publisert" and (b is None or r["bruker"] != b.id)):
            raise Feil(404, "Fant ikke kommentaren.")
        stemmer = {} if b is None else {s["kommentar"]: s["verdi"] for s in self.db.les(
            "SELECT kommentar, verdi FROM stemmer WHERE bruker = ? AND kommentar = ?", b.id, kid)}
        return self._node(r, b, stemmer)

    def trad(self, sak_key, b: Bruker | None, sortering: str = "best") -> dict:
        """The sak's comments as a tree. Others see only published comments (and a
        placeholder for any other comment that has published replies); the reader
        also sees their own waiting and stopped ones."""
        sak = self._sak(sak_key)
        if sortering not in SORTERING:
            sortering = "best"
        rader = self.db.les("SELECT k.*, u.navn FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker "
                            "WHERE k.sak = ? ORDER BY k.id", sak.key)
        stemmer = {} if b is None else {s["kommentar"]: s["verdi"] for s in self.db.les(
            "SELECT s.kommentar, s.verdi FROM stemmer s JOIN kommentarer k ON k.id = s.kommentar "
            "WHERE s.bruker = ? AND k.sak = ?", b.id, sak.key)}
        noder = {r["id"]: (r, self._node(r, b, stemmer)) for r in rader}
        barn: dict[int | None, list[int]] = {}
        for r in rader:
            barn.setdefault(r["forelder"], []).append(r["id"])

        def direkte(kid: int) -> bool:
            r, _ = noder[kid]
            return r["status"] == "publisert" or (b is not None and r["bruker"] == b.id and r["status"] != "slettet")

        # Visible: shown for its own sake, or as a placeholder because a reply below it is.
        # Children first (ids grow down the thread), so each comment is looked at once.
        synlig: dict[int, bool] = {}
        for kid in sorted(noder, reverse=True):
            synlig[kid] = direkte(kid) or any(synlig.get(x, False) for x in barn.get(kid, []))

        nokkel = SORTERING[sortering]

        def bygg(forelder: int | None) -> list[dict]:
            ut = []
            for kid in sorted((k for k in barn.get(forelder, []) if synlig[k]),
                              key=lambda k: nokkel(noder[k][0]), reverse=True):
                node = noder[kid][1]
                node["svar"] = bygg(kid)  # depth is capped at MAKS_DYBDE when a reply is written
                ut.append(node)
            return ut

        antall = sum(1 for r in rader if r["status"] == "publisert")
        return {"sak": sak.key, "sortering": sortering, "antall": antall, "kommentarer": bygg(None)}

    def mest_kommentert(self, periode: str = "7", antall: int = 10) -> dict:
        """The saker (and debattinnlegg) with the most published comments written in the
        period: "7" or "30" days, or "aar" (this year). Only keys the site still has a page
        for are listed, with the title and link from the sak index."""
        if periode not in PERIODER:
            periode = "7"
        dager = PERIODER[periode]
        fra = siden(days=dager) if dager else iso(na().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
        rader = self.db.les("SELECT sak, COUNT(*) AS antall, MAX(opprettet) AS siste FROM kommentarer "
                            "WHERE status = 'publisert' AND opprettet >= ? GROUP BY sak "
                            "ORDER BY antall DESC, siste DESC", fra)
        ut = []
        for r in rader:
            info = self.saker.get(r["sak"])
            if info:
                ut.append({"sak": r["sak"], "tittel": info.get("tittel", ""), "lenke": info.get("lenke", ""),
                           "antall": r["antall"]})
            if len(ut) == antall:
                break
        return {"periode": periode, "saker": ut}

    # --- moderation ---------------------------------------------------------------

    def _moderer_senere(self, kid: int) -> None:
        if self.umiddelbar:
            self.moderer(kid)
            return
        with self._ko_lås:  # a comment edited while it waits is judged once, in its newest form
            if kid in self._i_ko:
                return
            self._i_ko.add(kid)
        self._ko.put(kid)

    def moderer(self, kid: int) -> None:
        r = self.db.en("SELECT k.*, u.sperret FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker "
                       "WHERE k.id = ?", kid)
        if r is None or r["status"] != "venter":
            return
        if r["sperret"]:  # blocked while the comment waited
            with self.db.tx() as c:
                c.execute("UPDATE kommentarer SET status = 'fjernet' WHERE id = ? AND status = 'venter'", (kid,))
            return
        forelder = self.db.en("SELECT tekst FROM kommentarer WHERE id = ?", r["forelder"]) if r["forelder"] else None
        try:
            sak = self._sak(r["sak"])
        except Feil:
            sak = Sak(r["sak"], "")
        svar = self.moderator.avgjor(r["tekst"], sak, forelder["tekst"] if forelder else None)
        status = {"publiser": "publisert", "avvis": "avvist", "redaktor": "til_redaktor"}[svar.utfall]
        with self.db.tx() as c:
            # The author may have edited or deleted it meanwhile: only a still-waiting text is decided.
            oppdatert = c.execute("UPDATE kommentarer SET status = ?, regel = ? WHERE id = ? AND status = 'venter' "
                                  "AND tekst = ?", (status, svar.regel, kid, r["tekst"])).rowcount
            if oppdatert:
                self.db.logg_vurdering(c, kid, svar.utfall, svar.regel, self.moderator.name, {
                    "vurderinger": [v.som_dict() for v in svar.vurderinger], "feil": svar.feil})
        if not oppdatert:  # edited while it was judged: judge the new text
            ny = self.db.en("SELECT status FROM kommentarer WHERE id = ?", kid)
            if ny is not None and ny["status"] == "venter":
                self._moderer_senere(kid)

    def start_ko(self) -> None:
        """One worker for the whole server: the Pi judges one comment at a time.
        Comments left waiting by a restart are queued again."""
        for r in self.db.les("SELECT id FROM kommentarer WHERE status = 'venter' ORDER BY id"):
            self._moderer_senere(r["id"])
        if self._traad is None:
            self._traad = threading.Thread(target=self._arbeid, name="moderering", daemon=True)
            self._traad.start()

    def _arbeid(self) -> None:
        while True:
            kid = self._ko.get()
            with self._ko_lås:
                self._i_ko.discard(kid)
            try:
                self.moderer(kid)
            except Exception:  # one bad comment must not stop the queue; it stays 'venter'
                log.exception("moderation failed for comment %s", kid)

    # --- editor -------------------------------------------------------------------

    def redaktor_ko(self) -> list[dict]:
        """Comments waiting for the editor and reported ones, oldest first, with the
        moderators' last verdict (not the reader's own 'please look' entry) and the
        reasons readers gave in their reports."""
        rader = self.db.les(
            "SELECT k.id, k.sak, k.tekst, k.status, k.regel, k.opprettet, k.endret, u.navn, "
            "(SELECT COUNT(*) FROM rapporter r WHERE r.kommentar = k.id AND r.behandlet = 0) AS rapporter, "
            "(SELECT json_group_array(r.grunn) FROM rapporter r WHERE r.kommentar = k.id AND r.behandlet = 0 "
            " AND r.grunn IS NOT NULL AND r.grunn != '') AS grunner, "
            "(SELECT data FROM vurderinger v WHERE v.kommentar = k.id AND v.hvem NOT IN ('leseren', 'redaktør') "
            " ORDER BY v.id DESC LIMIT 1) AS vurdering, "
            "EXISTS (SELECT 1 FROM vurderinger v WHERE v.kommentar = k.id AND v.hvem = 'leseren') AS ba_om_vurdering "
            "FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker "
            "WHERE k.status = 'til_redaktor' OR (k.status = 'publisert' AND EXISTS "
            "(SELECT 1 FROM rapporter r WHERE r.kommentar = k.id AND r.behandlet = 0)) ORDER BY k.id")
        ut = []
        for r in rader:
            d = dict(r)
            d["grunner"] = json.loads(d["grunner"] or "[]")
            d["ba_om_vurdering"] = bool(d["ba_om_vurdering"])
            ut.append(d)
        return ut

    def nylig_stoppet(self, dager: int = 7) -> list[dict]:
        """Comments removed by the editor or stopped by the model in the last `dager` days,
        newest first, so the editor can see what was stopped and undo a mistake."""
        rader = self.db.les(
            "SELECT k.id, k.sak, k.tekst, k.status, k.regel, k.opprettet, u.navn, "
            "(SELECT v.tid FROM vurderinger v WHERE v.kommentar = k.id ORDER BY v.id DESC LIMIT 1) AS tid, "
            "(SELECT v.hvem FROM vurderinger v WHERE v.kommentar = k.id ORDER BY v.id DESC LIMIT 1) AS hvem, "
            "(SELECT data FROM vurderinger v WHERE v.kommentar = k.id AND v.hvem NOT IN ('leseren', 'redaktør') "
            " ORDER BY v.id DESC LIMIT 1) AS vurdering "
            "FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker "
            "WHERE k.status IN ('fjernet', 'avvist') AND k.tekst != '' ORDER BY k.id DESC")
        grense = siden(days=dager)
        return [dict(r) for r in rader if (r["tid"] or r["opprettet"]) >= grense]

    def redaktor_avgjor(self, kid: int, utfall: str, regel: str | None = None, tekst: str | None = None) -> None:
        """The editor publishes ('publiser') or removes ('fjern', optionally with the rule
        broken). Publishing needs the text the editor read: if the author changed or
        deleted it since, nothing happens (409) and the queue shows the new state. A
        comment the editor removed or the model stopped can be published after all."""
        if utfall not in ("publiser", "fjern") or (regel is not None and regel not in REGLER):
            raise Feil(400, "Ugyldig avgjørelse.")
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM kommentarer WHERE id = ?", (kid,)).fetchone() is None:
                raise Feil(404, "Fant ikke kommentaren.")
            if utfall == "publiser":
                endret = c.execute("UPDATE kommentarer SET status = 'publisert', regel = NULL WHERE id = ? "
                                   "AND status IN ('til_redaktor', 'publisert', 'fjernet', 'avvist') AND tekst = ?",
                                   (kid, tekst)).rowcount
            else:
                endret = c.execute("UPDATE kommentarer SET status = 'fjernet', regel = ? WHERE id = ? "
                                   "AND status != 'slettet'", (regel, kid)).rowcount
            if not endret:
                raise Feil(409, "Kommentaren er endret eller slettet siden du så den. Last inn på nytt.")
            c.execute("UPDATE rapporter SET behandlet = 1 WHERE kommentar = ?", (kid,))
            self.db.logg_vurdering(c, kid, utfall, regel, "redaktør", {})

    def sperr(self, kid: int) -> None:
        """Block the author of a comment: no login, no new comments, their sessions end,
        and what they have waiting is removed. Nothing to block if the account is gone."""
        rad = self.db.en("SELECT bruker FROM kommentarer WHERE id = ?", kid)
        if rad is None:
            raise Feil(404, "Fant ikke kommentaren.")
        if rad["bruker"] is None:
            return
        with self.db.tx() as c:
            c.execute("UPDATE brukere SET sperret = 1 WHERE id = ?", (rad["bruker"],))
            c.execute("DELETE FROM okter WHERE bruker = ?", (rad["bruker"],))
            c.execute("UPDATE kommentarer SET status = 'fjernet' WHERE bruker = ? AND status IN ('venter', 'til_redaktor')",
                      (rad["bruker"],))
