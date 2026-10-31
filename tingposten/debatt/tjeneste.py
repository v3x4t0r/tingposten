"""What the comment server does, without HTTP: login by e-mail link, display names,
comments and replies, votes, reports, sorting, and the moderation queue.

The server (server.py) only translates requests to these calls; tests call them
directly. Errors a reader can fix are raised as Feil(kode, melding); the
message is Norwegian and shown as it is.
"""

import hashlib
import logging
import math
import queue
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from .db import Db, iso, na, siden
from .moderering import Kjede, Sak
from .regler import REGLER

log = logging.getLogger(__name__)

LENKE_GYLDIG = timedelta(minutes=15)
OKT_GYLDIG = timedelta(days=30)
MAKS_TEKST = 3000
MIN_TEKST = 2
NAVN_MIN, NAVN_MAKS = 2, 30
LENKER_PER_TIME = 3            # login e-mails per address and hour
KOMMENTAR_PAUSE_S = 30         # between two comments from one account
KOMMENTARER_PER_DOGN = 30
NYE_KONTO_PER_DOGN = 5         # the first 24 hours of an account
RESERVERTE_NAVN = ("tingposten", "redaksjonen", "redaksjon", "redaktør", "redaktor", "redaktøren", "admin",
                   "administrator", "moderator", "bergen kommune", "byrådet", "bystyret", "kommunen")

_EPOST = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_NAVN_TEGN = re.compile(r"^[\w .'\-]+$")


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


class Tjeneste:
    def __init__(self, db: Db, moderator: Kjede, saker: dict[str, dict], send_lenke, site_url: str,
                 umiddelbar_moderering: bool = False):
        """`saker`: key → {"tittel", "ingress", "lenke"} (written by bygg). `send_lenke(epost, lenke)`
        sends the login e-mail. With `umiddelbar_moderering` comments are judged inside the
        call (tests); otherwise one background thread works through the queue."""
        self.db, self.moderator, self.saker = db, moderator, saker
        self.send_lenke, self.site_url = send_lenke, site_url.rstrip("/") + "/"
        self.umiddelbar = umiddelbar_moderering
        self._ko: queue.Queue[int] = queue.Queue()
        self._traad: threading.Thread | None = None

    # --- login -----------------------------------------------------------------

    def be_om_lenke(self, epost, tilbake=None) -> None:
        """Send a login link. Says nothing about whether the address has an account."""
        epost = (epost or "").strip().lower() if isinstance(epost, str) else ""
        if not _EPOST.match(epost) or len(epost) > 254:
            raise Feil(400, "Skriv en gyldig e-postadresse.")
        tilbake = tilbake if isinstance(tilbake, str) and tilbake.startswith(self.site_url) else None
        if self.db.en("SELECT COUNT(*) AS n FROM innlogging WHERE epost = ? AND opprettet > ?",
                      epost, siden(hours=1))["n"] >= LENKER_PER_TIME:
            raise Feil(429, "Du har bedt om mange lenker. Prøv igjen om en time.")
        token = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("DELETE FROM innlogging WHERE utloper < ?", (siden(days=1),))
            c.execute("INSERT INTO innlogging (hash, epost, tilbake, opprettet, utloper) VALUES (?, ?, ?, ?, ?)",
                      (_hash(token), epost, tilbake, iso(na()), iso(na() + LENKE_GYLDIG)))
        try:
            self.send_lenke(epost, f"{self.site_url}bergen/logg-inn/#t={token}")
        except Exception:
            log.exception("could not send a login e-mail")
            raise Feil(503, "Vi fikk ikke sendt e-posten akkurat nå. Prøv igjen litt senere.") from None

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
        return Bruker(rad["id"], rad["navn"], rad["opprettet"], bool(rad["sperret"]))

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
        if navn.casefold() in RESERVERTE_NAVN or any(r in navn.casefold() for r in ("tingposten", "redaksjon")):
            raise Feil(400, "Det navnet kan ikke brukes.")
        svar = self.moderator.avgjor(f"Visningsnavn: {navn}", Sak("visningsnavn", "Et nytt visningsnavn i debatten"))
        if svar.utfall == "avvis":
            raise Feil(400, f"Navnet bryter debattreglene ({REGLER[svar.regel][0].lower()}).")
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM brukere WHERE navn = ? AND id != ?", (navn, b.id)).fetchone():
                raise Feil(409, "Navnet er tatt.")
            c.execute("UPDATE brukere SET navn = ? WHERE id = ?", (navn, b.id))
        return navn

    # --- comments --------------------------------------------------------------

    def _sak(self, key) -> Sak:
        info = self.saker.get(key) if isinstance(key, str) else None
        if info is None:
            raise Feil(404, "Ukjent sak.")
        return Sak(key, info.get("tittel", ""), info.get("ingress", ""))

    def ny_kommentar(self, b: Bruker, sak_key, tekst, forelder=None) -> dict:
        if not b.navn:
            raise Feil(400, "Velg et visningsnavn først.")
        sak = self._sak(sak_key)
        tekst = rens_tekst(tekst)
        if forelder is not None:
            f = self.db.en("SELECT sak, status FROM kommentarer WHERE id = ?", forelder) if type(forelder) is int else None
            if f is None or f["sak"] != sak.key or f["status"] != "publisert":
                raise Feil(400, "Kommentaren du svarer på finnes ikke.")
        self._sjekk_tempo(b)
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO kommentarer (sak, forelder, bruker, tekst, status, opprettet) "
                            "VALUES (?, ?, ?, ?, 'venter', ?)", (sak.key, forelder, b.id, tekst, iso(na())))
            kid = cur.lastrowid
        self._moderer_senere(kid)
        return self.kommentar(kid, b)

    def _sjekk_tempo(self, b: Bruker) -> None:
        if self.db.en("SELECT 1 FROM kommentarer WHERE bruker = ? AND opprettet > ?", b.id,
                      siden(seconds=KOMMENTAR_PAUSE_S)):
            raise Feil(429, "Vent litt før du skriver en ny kommentar.")
        grense = NYE_KONTO_PER_DOGN if b.opprettet > siden(days=1) else KOMMENTARER_PER_DOGN
        if self.db.en("SELECT COUNT(*) AS n FROM kommentarer WHERE bruker = ? AND opprettet > ?", b.id,
                      siden(days=1))["n"] >= grense:
            raise Feil(429, f"Du har skrevet {grense} kommentarer siste døgn. Prøv igjen senere.")

    def endre(self, b: Bruker, kid, tekst) -> dict:
        tekst = rens_tekst(tekst)
        egen = self._egen(b, kid)
        if egen["status"] in ("slettet", "fjernet"):
            raise Feil(400, "Kommentaren kan ikke endres.")
        with self.db.tx() as c:
            c.execute("UPDATE kommentarer SET tekst = ?, status = 'venter', regel = NULL, endret = ? WHERE id = ?",
                      (tekst, iso(na()), kid))
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
        placeholder for a deleted or removed one that has published replies); the
        reader also sees their own waiting and stopped ones."""
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

        def synlig(kid: int) -> bool:
            r, _ = noder[kid]
            if r["status"] == "publisert" or (b is not None and r["bruker"] == b.id and r["status"] != "slettet"):
                return True
            return r["status"] in ("slettet", "fjernet") and any(synlig(x) for x in barn.get(kid, []))

        nokkel = SORTERING[sortering]

        def bygg(forelder: int | None) -> list[dict]:
            ut = []
            for kid in sorted((k for k in barn.get(forelder, []) if synlig(k)),
                              key=lambda k: nokkel({**dict(noder[k][0])}), reverse=True):
                node = noder[kid][1]
                node["svar"] = bygg(kid)
                ut.append(node)
            return ut

        antall = sum(1 for r in rader if r["status"] == "publisert")
        return {"sak": sak.key, "sortering": sortering, "antall": antall, "kommentarer": bygg(None)}

    # --- moderation ---------------------------------------------------------------

    def _moderer_senere(self, kid: int) -> None:
        if self.umiddelbar:
            self.moderer(kid)
        else:
            self._ko.put(kid)

    def moderer(self, kid: int) -> None:
        r = self.db.en("SELECT * FROM kommentarer WHERE id = ?", kid)
        if r is None or r["status"] != "venter":
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

    def start_ko(self) -> None:
        """One worker for the whole server: the Pi judges one comment at a time.
        Comments left waiting by a restart are queued again."""
        for r in self.db.les("SELECT id FROM kommentarer WHERE status = 'venter' ORDER BY id"):
            self._ko.put(r["id"])
        if self._traad is None:
            self._traad = threading.Thread(target=self._arbeid, name="moderering", daemon=True)
            self._traad.start()

    def _arbeid(self) -> None:
        while True:
            kid = self._ko.get()
            try:
                self.moderer(kid)
            except Exception:  # one bad comment must not stop the queue; it stays 'venter'
                log.exception("moderation failed for comment %s", kid)

    # --- editor -------------------------------------------------------------------

    def redaktor_ko(self) -> list[dict]:
        """Comments waiting for the editor and reported ones, oldest first."""
        rader = self.db.les(
            "SELECT k.id, k.sak, k.tekst, k.status, k.regel, k.opprettet, u.navn, "
            "(SELECT COUNT(*) FROM rapporter r WHERE r.kommentar = k.id AND r.behandlet = 0) AS rapporter, "
            "(SELECT data FROM vurderinger v WHERE v.kommentar = k.id ORDER BY v.id DESC LIMIT 1) AS vurdering "
            "FROM kommentarer k LEFT JOIN brukere u ON u.id = k.bruker "
            "WHERE k.status = 'til_redaktor' OR (k.status = 'publisert' AND EXISTS "
            "(SELECT 1 FROM rapporter r WHERE r.kommentar = k.id AND r.behandlet = 0)) ORDER BY k.id")
        return [dict(r) for r in rader]

    def redaktor_avgjor(self, kid: int, utfall: str, regel: str | None = None) -> None:
        """The editor publishes ('publiser') or removes ('fjern', optionally with the rule broken)."""
        if utfall not in ("publiser", "fjern") or (regel is not None and regel not in REGLER):
            raise Feil(400, "Ugyldig avgjørelse.")
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM kommentarer WHERE id = ?", (kid,)).fetchone() is None:
                raise Feil(404, "Fant ikke kommentaren.")
            status = "publisert" if utfall == "publiser" else "fjernet"
            c.execute("UPDATE kommentarer SET status = ?, regel = ? WHERE id = ?",
                      (status, regel if utfall == "fjern" else None, kid))
            c.execute("UPDATE rapporter SET behandlet = 1 WHERE kommentar = ?", (kid,))
            self.db.logg_vurdering(c, kid, utfall, regel, "redaktør", {})

    def sperr(self, kid: int) -> None:
        """Block the author of a comment: no login, no new comments; their sessions end."""
        rad = self.db.en("SELECT bruker FROM kommentarer WHERE id = ?", kid)
        if rad is None or rad["bruker"] is None:
            raise Feil(404, "Fant ikke kommentaren.")
        with self.db.tx() as c:
            c.execute("UPDATE brukere SET sperret = 1 WHERE id = ?", (rad["bruker"],))
            c.execute("DELETE FROM okter WHERE bruker = ?", (rad["bruker"],))
