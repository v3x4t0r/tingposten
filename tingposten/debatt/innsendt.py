"""Debattinnlegg sent through the form on the Debatt page, from submission to publication.

    vurderes        sent in; the control panel's AI check (innleggsjekk.py) has not looked yet
    sortert_bort    the AI check found obvious rubbish (spam, gibberish, no op-ed at all):
                    folded away in the panel for 30 days, in case it got one wrong
    til_redaktor    in the editor's queue, with the AI's summary and notes
    sendt_skribent  the editor changed the text and e-mailed it to the writer to approve
    godkjent        the writer approved the editor's version
    publisert       written to data/bergen/innlegg/ by the panel (innlegg.py), then built
    avvist          the editor said no

Used by the comment server (send_inn, for_godkjenning, godkjenn) and by the control
panel on the Pi (the rest). The editor sees the writer's name, never the e-mail address;
the panel only uses it to send the approval and "published" e-mails.
"""

import hashlib
import json
import secrets
from datetime import timedelta

from .db import Db, iso, na, siden
from .tjeneste import Bruker, Feil

MIN_TEGN, MAKS_TEGN = 150, 6000     # the page asks for at most 4000; a little room for the editor to cut
PER_DOGN = 2
FORSOK = 3                           # AI check failures before it goes to the editor unchecked
GODKJENN_GYLDIG = timedelta(days=14)
SORTERT_BORT_DAGER = 30


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tekst(verdi, navn: str, maks: int, minst: int = 0) -> str:
    if not isinstance(verdi, str):
        verdi = ""
    linjer = [" ".join(x.split()) for x in verdi.replace("\r\n", "\n").split("\n")]
    ren = "\n".join(linjer).strip()
    while "\n\n\n" in ren:
        ren = ren.replace("\n\n\n", "\n\n")
    if len(ren) < minst:
        raise Feil(400, f"{navn} er for kort.")
    if len(ren) > maks:
        raise Feil(400, f"{navn} er for lang (høyst {maks} tegn).")
    return ren


def send_inn(db: Db, b: Bruker | None, data: dict) -> dict:
    """A logged-in reader sends a debattinnlegg. It waits for the AI check."""
    if b is None:
        raise Feil(401, "Logg inn for å sende et debattinnlegg.")
    if b.sperret:
        raise Feil(403, "Kontoen er sperret.")
    if not isinstance(data, dict):
        raise Feil(400, "Ugyldig.")
    navn = _tekst(data.get("navn"), "Navnet", 80, 3).replace("\n", " ")
    if len(navn.split()) < 2:
        raise Feil(400, "Debattinnlegg trykkes med fullt navn: skriv fornavn og etternavn.")
    om = _tekst(data.get("om"), "«Hvem du er»", 80).replace("\n", " ")
    tittel = _tekst(data.get("tittel"), "Tittelen", 120, 5).replace("\n", " ")
    tekst = _tekst(data.get("tekst"), "Teksten", MAKS_TEGN, MIN_TEGN)
    sak = data.get("sak") if isinstance(data.get("sak"), str) and len(data.get("sak")) <= 80 else None
    if db.en("SELECT COUNT(*) AS n FROM innsendt WHERE bruker = ? AND opprettet > ?", b.id,
             siden(days=1))["n"] >= PER_DOGN:
        raise Feil(429, f"Du har sendt {PER_DOGN} innlegg siste døgn. Prøv igjen i morgen.")
    with db.tx() as c:
        cur = c.execute("INSERT INTO innsendt (bruker, navn, om, tittel, tekst, sak, status, opprettet) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'vurderes', ?)", (b.id, navn, om, tittel, tekst, sak, iso(na())))
    return {"id": cur.lastrowid, "mottatt": True}


# --- the AI check (control panel) ---------------------------------------------------------

def til_vurdering(db: Db) -> list[dict]:
    return [dict(r) for r in db.les("SELECT id, navn, om, tittel, tekst, sak FROM innsendt "
                                    "WHERE status = 'vurderes' ORDER BY id")]


def sett_ki(db: Db, iid: int, svar: dict, modell: str) -> str:
    status = "sortert_bort" if svar.get("utfall") == "sorter_bort" else "til_redaktor"
    with db.tx() as c:
        c.execute("UPDATE innsendt SET status = ?, ki = ?, endret = ? WHERE id = ? AND status = 'vurderes'",
                  (status, json.dumps({**svar, "modell": modell}, ensure_ascii=False), iso(na()), iid))
    return status


def ki_feilet(db: Db, iid: int, melding: str) -> None:
    """After FORSOK failures the innlegg goes to the editor unchecked, rather than waiting forever."""
    with db.tx() as c:
        c.execute("UPDATE innsendt SET forsok = forsok + 1, endret = ? WHERE id = ?", (iso(na()), iid))
        c.execute("UPDATE innsendt SET status = 'til_redaktor', ki = ? WHERE id = ? AND forsok >= ? "
                  "AND status = 'vurderes'", (json.dumps({"feil": melding[-300:]}, ensure_ascii=False), iid, FORSOK))


# --- the editor (control panel) ------------------------------------------------------------

def _rad(r) -> dict:
    d = {k: r[k] for k in ("id", "navn", "om", "tittel", "tekst", "sak", "status", "red_tittel", "red_tekst",
                           "opprettet", "endret")}
    d["ki"] = json.loads(r["ki"]) if r["ki"] else None
    return d


def ko(db: Db) -> dict:
    """What the panel shows: the queue, writers' approvals, and what the AI folded away."""
    rader = [_rad(r) for r in db.les("SELECT * FROM innsendt WHERE status IN "
                                     "('til_redaktor', 'sendt_skribent', 'godkjent', 'vurderes') ORDER BY id")]
    bort = [_rad(r) for r in db.les("SELECT * FROM innsendt WHERE status = 'sortert_bort' AND opprettet > ? "
                                    "ORDER BY id DESC", siden(days=SORTERT_BORT_DAGER))]
    return {"ko": rader, "sortert_bort": bort}


def hent(db: Db, iid: int) -> dict:
    r = db.en("SELECT * FROM innsendt WHERE id = ?", iid) if type(iid) is int else None
    if r is None:
        raise Feil(404, "Fant ikke innlegget.")
    return _rad(r)


def epost(db: Db, iid: int) -> str | None:
    r = db.en("SELECT u.epost FROM innsendt i JOIN brukere u ON u.id = i.bruker WHERE i.id = ?", iid)
    return r["epost"] if r else None


def til_godkjenning(db: Db, iid: int, tittel: str, tekst: str) -> str:
    """Store the editor's version and return a fresh approval token (only its hash is kept)."""
    rad = hent(db, iid)
    if rad["status"] not in ("til_redaktor", "sendt_skribent", "godkjent"):
        raise Feil(409, "Innlegget kan ikke sendes til godkjenning nå.")
    tittel = _tekst(tittel, "Tittelen", 120, 5).replace("\n", " ")
    tekst = _tekst(tekst, "Teksten", MAKS_TEGN, 1)
    token = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("UPDATE innsendt SET status = 'sendt_skribent', red_tittel = ?, red_tekst = ?, godkjenn_hash = ?, "
                  "godkjenn_utloper = ?, endret = ? WHERE id = ?",
                  (tittel, tekst, _hash(token), iso(na() + GODKJENN_GYLDIG), iso(na()), iid))
    return token


def _etter_token(db: Db, token):
    r = db.en("SELECT * FROM innsendt WHERE godkjenn_hash = ?", _hash(token)) \
        if isinstance(token, str) and 20 <= len(token) <= 100 else None
    if r is None or r["godkjenn_utloper"] < iso(na()):
        raise Feil(404, "Lenken er ugyldig eller utløpt. Svar på e-posten, så ordner vi det.")
    return r


def for_godkjenning(db: Db, token) -> dict:
    """The editor's version, for the writer's approval page."""
    r = _etter_token(db, token)
    return {"tittel": r["red_tittel"], "tekst": r["red_tekst"], "navn": r["navn"], "om": r["om"],
            "godkjent": r["status"] in ("godkjent", "publisert")}


def godkjenn(db: Db, token) -> dict:
    r = _etter_token(db, token)
    if r["status"] == "sendt_skribent":
        with db.tx() as c:
            c.execute("UPDATE innsendt SET status = 'godkjent', endret = ? WHERE id = ?", (iso(na()), r["id"]))
    return {"godkjent": True}


def til_publisering(rad: dict) -> tuple[str, str]:
    """The text to publish: the editor's version when there is one, else the writer's own."""
    return rad["red_tittel"] or rad["tittel"], rad["red_tekst"] or rad["tekst"]


def sett_status(db: Db, iid: int, status: str, fil: str | None = None) -> None:
    assert status in ("publisert", "avvist", "til_redaktor")
    with db.tx() as c:
        c.execute("UPDATE innsendt SET status = ?, fil = COALESCE(?, fil), godkjenn_hash = "
                  "CASE WHEN ? = 'til_redaktor' THEN godkjenn_hash ELSE NULL END, endret = ? WHERE id = ?",
                  (status, fil, status, iso(na()), iid))
