"""Fetch Stortinget's decided saker and their votes, store them, log what changed.

Store (config.STORTINGET_DATA_DIR):
  saker/<sesjon>.json             the session's sak list, raw
  sak/<sakid>.json                {"sak": the sak, "voteringer": [each vote with "resultat"
                                  (party and vote per seat, no names) and "forslag"], "hentet"}
  stortingsvedtak/<sesjon>.json   the session's numbered decisions (not the law texts)
  utvalg.json                     {"saker": [sakid, …]}: test saker outside the sessions in scope
  endringer.jsonl                 one line per change: ny, vedtatt, endret

In scope: saker with status «behandlet» from config.STORTINGET_FRA_SESJON on, plus
utvalg.json. A sak is fetched again when the list's sist_oppdatert_dato changes.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .api import Client, tid

log = logging.getLogger(__name__)

BEHANDLET = 1  # sak status in the JSON (XML: "behandlet")


def sesjoner_fra(fra: str, innevaerende: str) -> list[str]:
    """Session ids from `fra` to the current one: "2026-2027", "2027-2028", …"""
    start, slutt = int(fra[:4]), int(innevaerende[:4])
    return [f"{y}-{y + 1}" for y in range(start, slutt + 1)]


def utvalg(data_dir: Path) -> list[int]:
    path = data_dir / "utvalg.json"
    return [int(s) for s in json.loads(path.read_text()).get("saker", [])] if path.exists() else []


def lagret(data_dir: Path, sakid: int) -> dict | None:
    path = data_dir / "sak" / f"{sakid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def hent(data_dir: Path = config.STORTINGET_DATA_DIR, client: Client | None = None,
         saker: list[int] | None = None) -> dict:
    """One run. `saker`: only these sakids (each also joins utvalg.json); otherwise the
    sessions in scope plus utvalg.json. Returns counts for the log."""
    client = client or Client()
    teller = {"saker": 0, "hentet": 0, "uendret": 0}
    endringer: list[dict] = []
    endrede_sesjoner: set[str] = set()

    if saker:
        _legg_i_utvalg(data_dir, saker)
        aktuelle = {int(s): None for s in saker}
    else:
        innevaerende = client.innevaerende_sesjon()
        aktuelle = {}
        for sesjon in sesjoner_fra(config.STORTINGET_FRA_SESJON, innevaerende):
            liste = client.saker(sesjon)
            _skriv(data_dir / "saker" / f"{sesjon}.json", liste)
            for s in liste:
                if s.get("status") == BEHANDLET:
                    aktuelle[int(s["id"])] = s.get("sist_oppdatert_dato")
        for sakid in utvalg(data_dir):
            aktuelle.setdefault(sakid, None)

    for sakid, oppdatert in aktuelle.items():
        teller["saker"] += 1
        for_ = lagret(data_dir, sakid)
        if for_ and (oppdatert is None or for_.get("sist_oppdatert_dato") == oppdatert) and not saker:
            teller["uendret"] += 1
            continue
        ny = hent_sak(client, sakid)
        ny["sist_oppdatert_dato"] = oppdatert
        if for_ and "forslagstillere" in for_:
            ny["forslagstillere"] = for_["forslagstillere"]
        _skriv(data_dir / "sak" / f"{sakid}.json", ny)
        teller["hentet"] += 1
        endrede_sesjoner.add(ny["sak"].get("sak_sesjon") or "")
        endringer += _endringer(for_, ny)

    for sesjon in sorted(s for s in endrede_sesjoner if s):
        _skriv(data_dir / "stortingsvedtak" / f"{sesjon}.json", client.stortingsvedtak(sesjon))
    teller["forslagstillere"] = legg_til_forslagstillere(data_dir, client)

    if endringer:
        with open(data_dir / "endringer.jsonl", "a") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in endringer)
    teller["endringer"] = len(endringer)
    teller["foresporsler"] = client.antall
    log.info("stortinget hent: %s", teller)
    return teller


def hent_sak(client: Client, sakid: int) -> dict:
    """A sak with its votes: per seat only party and vote (1 = ikke til stede, 2 = for,
    3 = mot); names stay at Stortinget."""
    sak = client.sak(sakid)
    voteringer = []
    for v in client.voteringer(sakid):
        vid = v["votering_id"]
        resultat = [{"parti": (r.get("representant") or {}).get("parti", {}).get("id"), "votering": r.get("votering")}
                    for r in (client.voteringsresultat(vid) if v.get("personlig_votering") else [])]
        forslag = client.voteringsforslag(vid)
        voteringer.append({**_uten_person(v), "resultat": resultat, "forslag": forslag})
    return {"sak": sak, "voteringer": voteringer, "hentet": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def legg_til_forslagstillere(data_dir: Path, client: Client) -> int:
    """A representantforslag's proposers, as parties ({"Sp": 5}), from the session's sak list
    (the sak itself doesn't carry them). A session outside the scope is fetched once."""
    lister: dict[str, dict] = {}
    antall = 0
    for path in sorted((data_dir / "sak").glob("*.json")):
        lagret_sak = json.loads(path.read_text())
        if "forslagstillere" in lagret_sak:
            continue
        sesjon = lagret_sak["sak"].get("sak_sesjon")
        if sesjon not in lister:
            fil = data_dir / "saker" / f"{sesjon}.json"
            if not fil.exists():
                _skriv(fil, client.saker(sesjon))
            lister[sesjon] = {int(s["id"]): s for s in json.loads(fil.read_text())}
        fra_liste = lister[sesjon].get(int(lagret_sak["sak"]["id"])) or {}
        partier: dict[str, int] = {}
        for r in fra_liste.get("forslagstiller_liste") or []:
            p = (r.get("parti") or {}).get("id") or "uavhengig"
            partier[p] = partier.get(p, 0) + 1
        lagret_sak["forslagstillere"] = partier
        _skriv(path, lagret_sak)
        antall += 1
    return antall


def _uten_person(v: dict) -> dict:
    """The president of the sitting is a person; keep only the party."""
    v = dict(v)
    if p := v.pop("president", None):
        v["president_parti"] = (p.get("parti") or {}).get("id")
    return v


def _endringer(for_: dict | None, ny: dict) -> list[dict]:
    sak = ny["sak"]
    base = {"id": sak.get("id"), "sesjon": sak.get("sak_sesjon"), "tittel": sak.get("korttittel") or sak.get("tittel"),
            "tid": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if for_ is None:
        return [{"type": "ny", **base}] + ([{"type": "vedtatt", **base}] if sak.get("status") == BEHANDLET else [])
    gammel = for_["sak"]
    if gammel.get("status") != BEHANDLET and sak.get("status") == BEHANDLET:
        return [{"type": "vedtatt", **base}]
    felt = sorted(k for k in sak.keys() | gammel.keys() if k != "respons_dato_tid" and sak.get(k) != gammel.get(k))
    if len(ny["voteringer"]) != len(for_.get("voteringer", [])):
        felt.append("voteringer")
    return [{"type": "endret", **base, "felt": felt}] if felt else []


def _legg_i_utvalg(data_dir: Path, saker: list[int]) -> None:
    alle = sorted(set(utvalg(data_dir)) | {int(s) for s in saker})
    _skriv(data_dir / "utvalg.json", {"saker": alle})


def vedtaksdato(lagret_sak: dict) -> datetime | None:
    """When Stortinget decided: the last vote; without votes, the last dated saksgang event."""
    tider = [t for v in lagret_sak.get("voteringer", []) if (t := tid(v.get("votering_tid")))]
    if tider:
        return max(tider)
    hendelser = [t for steg in ((lagret_sak["sak"].get("saksgang") or {}).get("saksgang_steg_liste") or [])
                 for h in steg.get("saksgang_hendelse_liste") or [] if (t := tid(h.get("dato")))]
    return max(hendelser) if hendelser else None


def _skriv(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)
