"""The test panel: invited testers rate published saker, on tingposten.no/bergen/testpanel/.

The earlier panel was an artifact on claude.ai, and testers without Contributor access
got «permission denied» (owner, 1.11.2026, issue #17). Now the comment server stores
the ratings:

- A tester is a name and an invite link (…/bergen/testpanel/#t=<token>) the editor
  makes in the control panel. No account, no e-mail. Only the token's hash is stored,
  and the editor can close a link.
- Per sak a tester rates: should residents know about this (1–5), does something
  concrete change in everyday life now (1–5), forside / kort / støy, and is the card
  clear (ja / delvis / nei), with an optional comment.
- The saker are those of the meetings the editor picked, or the newest meetings. bygg
  writes every published card of the last months to data/debatt/testpanel.json, which
  is read again when it changes.
- The other testers' ratings of a sak are shown only once the tester has rated it
  (both scores and the pile), so nobody is steered. Comments are shown without names.
- Part 2 (owner, 1.11.2026: in his own first round the piles and the scores didn't
  agree, and he corrected some once it was pointed out): when every sak is rated, funn()
  points out answers that don't fit together, and the tester changes them or keeps them
  («Behold svaret»). Every answer is logged with its part (testlogg), so the first
  answers survive the review.
"""

import hashlib
import json
import secrets
from pathlib import Path

from .db import Db, iso, na
from .tjeneste import Feil

BUNKER = ("forside", "kort", "stoy")
KLAR = ("ja", "delvis", "nei")
MAKS_KOMMENTAR = 1000
NYESTE_PER_ORGAN = 2       # without the editor's choice: the two newest meetings of each organ
BUNKE_RANG = {"forside": 2, "kort": 1, "stoy": 0}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Saker:
    """data/debatt/testpanel.json (bygg.skriv_testpanel), read again when it changes."""

    def __init__(self, path: Path):
        self.path, self._mtime, self._data = Path(path), None, {"moter": []}

    def moter(self) -> list[dict]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return []
        if mtime != self._mtime:
            try:
                self._data, self._mtime = json.loads(self.path.read_text()), mtime
            except ValueError:
                pass
        return self._data.get("moter", [])


# --- testers (the control panel) -------------------------------------------------------

def ny_tester(db: Db, navn: str) -> tuple[int, str]:
    navn = " ".join(str(navn or "").split())[:60]
    if not navn:
        raise Feil(400, "Skriv et navn.")
    token = secrets.token_urlsafe(24)
    with db.tx() as c:
        cur = c.execute("INSERT INTO testere (navn, token_hash, opprettet) VALUES (?, ?, ?)", (navn, _hash(token), iso(na())))
    return cur.lastrowid, token


def steng(db: Db, tid: int, stengt: bool = True) -> None:
    with db.tx() as c:
        c.execute("UPDATE testere SET stengt = ? WHERE id = ?", (iso(na()) if stengt else None, tid))


def ny_lenke(db: Db, tid: int) -> str:
    """A new token for a tester (the old link stops working)."""
    token = secrets.token_urlsafe(24)
    with db.tx() as c:
        c.execute("UPDATE testere SET token_hash = ?, stengt = NULL WHERE id = ?", (_hash(token), tid))
    return token


def testere(db: Db) -> list[dict]:
    return [dict(r) for r in db.les(
        "SELECT t.id, t.navn, t.opprettet, t.stengt, t.sist, COUNT(v.sak) AS vurdert FROM testere t "
        "LEFT JOIN testvurderinger v ON v.tester = t.id GROUP BY t.id ORDER BY t.id")]


def tester(db: Db, token) -> dict | None:
    if not isinstance(token, str) or not 16 <= len(token) <= 100:
        return None
    r = db.en("SELECT id, navn FROM testere WHERE token_hash = ? AND stengt IS NULL", _hash(token))
    return dict(r) if r else None


# --- which meetings ----------------------------------------------------------------------

def valgte(db: Db) -> list[str]:
    return [r["mote"] for r in db.les("SELECT mote FROM testpanel_moter ORDER BY mote")]


def velg(db: Db, moter: list[str]) -> None:
    if not isinstance(moter, list) or not all(isinstance(m, str) and len(m) <= 40 for m in moter):
        raise Feil(400, "Ugyldig møteliste.")
    with db.tx() as c:
        c.execute("DELETE FROM testpanel_moter")
        c.executemany("INSERT OR IGNORE INTO testpanel_moter (mote) VALUES (?)", [(m,) for m in moter])


def aktuelle(db: Db, alle: list[dict]) -> list[dict]:
    """The meetings testers see: the editor's choice, or the newest of each organ."""
    if valg := set(valgte(db)):
        return [m for m in alle if m["id"] in valg]
    per_organ: dict[str, list[dict]] = {}
    for m in sorted(alle, key=lambda m: m["dato"], reverse=True):
        per_organ.setdefault(m["organ"], []).append(m)
    nyeste = {m["id"] for liste in per_organ.values() for m in liste[:NYESTE_PER_ORGAN]}
    return [m for m in alle if m["id"] in nyeste]


# --- rating (the tester's page) --------------------------------------------------------

def _ferdig(v: dict | None) -> bool:
    return bool(v) and v.get("bor_vite") is not None and v.get("konkret") is not None and v.get("bunke") is not None


def vurder(db: Db, t: dict, alle: list[dict], data: dict) -> dict:
    """Save what the tester set for one sak (each click saves; fields left out are kept)."""
    if not isinstance(data, dict):
        raise Feil(400, "Ugyldig.")
    sak = data.get("sak")
    if sak not in {s["key"] for m in aktuelle(db, alle) for s in m["saker"]}:
        raise Feil(404, "Saken er ikke med i testpanelet.")
    felt = {}
    for navn in ("bor_vite", "konkret"):
        if navn in data:
            v = data[navn]
            if v is not None and (type(v) is not int or not 1 <= v <= 5):
                raise Feil(400, "Velg 1–5.")
            felt[navn] = v
    for navn, lov in (("bunke", BUNKER), ("klar", KLAR)):
        if navn in data:
            if data[navn] is not None and data[navn] not in lov:
                raise Feil(400, "Ugyldig valg.")
            felt[navn] = data[navn]
    del_ = 2 if data.get("del") == 2 else 1
    if data.get("behold") is True:
        felt["bekreftet"] = 1
    if "kommentar" in data:
        k = data["kommentar"]
        if k is not None and not isinstance(k, str):
            raise Feil(400, "Ugyldig kommentar.")
        felt["kommentar"] = (k or "").strip()[:MAKS_KOMMENTAR]
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO testvurderinger (tester, sak, tid) VALUES (?, ?, ?)", (t["id"], sak, iso(na())))
        if felt:
            if any(k in felt for k in ("bor_vite", "konkret", "bunke")) and "bekreftet" not in felt:
                felt["bekreftet"] = 0     # a changed answer is checked again
            c.execute(f"UPDATE testvurderinger SET {', '.join(f'{k} = ?' for k in felt)}, tid = ? WHERE tester = ? AND sak = ?",
                      (*felt.values(), iso(na()), t["id"], sak))
            c.executemany("INSERT INTO testlogg (tester, sak, felt, verdi, del, tid) VALUES (?, ?, ?, ?, ?, ?)",
                          [(t["id"], sak, k, None if v is None else str(v), del_, iso(na())) for k, v in felt.items()])
        c.execute("UPDATE testere SET sist = ? WHERE id = ?", (iso(na()), t["id"]))
    mine = _mine(db, t["id"]).get(sak)
    return {"mine": mine, "andre": _andre(db, t["id"], [sak]).get(sak, {"antall": 0}) if _ferdig(mine) else None}


_FELT = ("bor_vite", "konkret", "bunke", "klar", "kommentar", "bekreftet")


def _mine(db: Db, tid: int) -> dict[str, dict]:
    return {r["sak"]: {k: r[k] for k in _FELT} for r in db.les("SELECT * FROM testvurderinger WHERE tester = ?", tid)}


def _andre(db: Db, tid: int, saker: list[str]) -> dict[str, dict]:
    if not saker:
        return {}
    rader = db.les(f"SELECT * FROM testvurderinger WHERE tester != ? AND sak IN ({','.join('?' * len(saker))})",
                   tid, *saker)
    ut: dict[str, dict] = {}
    for r in rader:
        a = ut.setdefault(r["sak"], {"antall": 0, "bor_vite": [], "konkret": [], "bunker": {}, "klar": {}, "kommentarer": []})
        a["antall"] += 1
        for k in ("bor_vite", "konkret"):
            if r[k] is not None:
                a[k].append(r[k])
        for k, telling in (("bunke", "bunker"), ("klar", "klar")):
            if r[k]:
                a[telling][r[k]] = a[telling].get(r[k], 0) + 1
        if r["kommentar"]:
            a["kommentarer"].append(r["kommentar"])
    for a in ut.values():
        for k in ("bor_vite", "konkret"):
            a[k] = round(sum(a[k]) / len(a[k]), 1) if a[k] else None
    return ut


def funn(saker: list[dict], mine: dict[str, dict]) -> dict:
    """Part 2: answers that contradict each other, per sak (kept answers left out). Only
    the tester's own contradictions, phrased as questions; once every sak is rated."""
    vurdert = [s for s in saker if _ferdig(mine.get(s["key"]))]
    if not saker or len(vurdert) < len(saker):
        return {"klar": False, "saker": {}, "forside": None}
    grunner: dict[str, list[str]] = {}

    def legg(key, tekst):
        grunner.setdefault(key, []).append(tekst)

    for s in vurdert:
        v = mine[s["key"]]
        if v["bunke"] == "forside" and v["bor_vite"] <= 2:
            legg(s["key"], f"Du la saken på forsiden, men ga «bør folk vite» {v['bor_vite']}. Er det det du mener?")
        if v["bunke"] == "stoy" and v["bor_vite"] >= 4:
            legg(s["key"], f"Du kalte saken støy, men ga «bør folk vite» {v['bor_vite']}. Er det det du mener?")
        if v["konkret"] >= 4 and v["bor_vite"] <= 2:
            legg(s["key"], f"Du mener mye endrer seg i hverdagen ({v['konkret']}), men at få trenger å vite om det "
                           f"({v['bor_vite']}). Er det det du mener?")
        if s.get("ingen_ny_beslutning") and v["konkret"] >= 3:
            legg(s["key"], f"Ingenting nytt er vedtatt i saken ({s.get('status') or 'ikke avgjort'}), men du ga "
                           f"«endrer hverdagen» {v['konkret']}. Er det det du mener?")
    # Two saker that contradict each other: clearly higher «bør folk vite», lower pile.
    par = []
    for a in vurdert:
        for b in vurdert:
            va, vb = mine[a["key"]], mine[b["key"]]
            if va["bor_vite"] >= vb["bor_vite"] + 2 and BUNKE_RANG[va["bunke"]] < BUNKE_RANG[vb["bunke"]]:
                par.append((va["bor_vite"] - vb["bor_vite"], a, b))
    for _, a, b in sorted(par, key=lambda x: -x[0])[:3]:
        va, vb = mine[a["key"]], mine[b["key"]]
        legg(b["key"], f"«{a['overskrift']}» fikk «bør folk vite» {va['bor_vite']} og ligger som {va['bunke']}, mens "
                       f"denne fikk {vb['bor_vite']} og ligger som {vb['bunke']}. Er det det du mener?")
    for key in [k for k in grunner if mine[k].get("bekreftet")]:
        del grunner[key]
    # No nudge about how many saker belong on forside (owner, 1.11.2026): that is the
    # editor's rule, not the tester's judgement; the share is counted afterwards instead.
    return {"klar": True, "saker": grunner, "forside": None}


def side(db: Db, t: dict, alle: list[dict]) -> dict:
    """Everything the tester's page shows: the meetings, the saker, the tester's own
    ratings, and the others' where the tester has rated."""
    moter = aktuelle(db, alle)
    mine = _mine(db, t["id"])
    ferdige = [k for k, v in mine.items() if _ferdig(v)]
    andre = _andre(db, t["id"], ferdige)
    ut = []
    for m in moter:
        saker = [{**s, "mine": mine.get(s["key"]),
                  "andre": andre.get(s["key"], {"antall": 0}) if s["key"] in ferdige else None}
                 for s in m["saker"]]
        ut.append({k: m[k] for k in ("id", "organ", "dato", "dato_tekst")} | {"saker": saker})
    saker = [s for m in moter for s in m["saker"]]
    return {"navn": t["navn"], "moter": ut, "del2": funn(saker, mine)}


# --- results (the control panel) -------------------------------------------------------

def resultater(db: Db) -> dict:
    """Every rating with the tester's name, for the editor and for calibrating the score."""
    rader = db.les("SELECT t.navn, v.* FROM testvurderinger v JOIN testere t ON t.id = v.tester ORDER BY v.sak, t.id")
    logg = db.les("SELECT t.navn, l.* FROM testlogg l JOIN testere t ON t.id = l.tester ORDER BY l.tid")
    return {"testere": testere(db), "valgte_moter": valgte(db),
            "vurderinger": [{"tester": r["navn"], "sak": r["sak"], **{k: r[k] for k in _FELT}, "tid": r["tid"]}
                            for r in rader],
            "logg": [{"tester": r["navn"], "sak": r["sak"], "felt": r["felt"], "verdi": r["verdi"], "del": r["del"],
                      "tid": r["tid"]} for r in logg]}
