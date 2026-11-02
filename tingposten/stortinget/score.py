"""How high a Stortinget sak ranks: 0–100, with up to three reasons shown as badges.

The model's two signals carry most of the weight (Bergen's calibration: facts on
paper don't say what people care about); the rest is read off the votes and the
kind of decision. Ranked only within Stortinget; mixing with a kommune's saker is
a later decision (issue #25).
"""

import json
from pathlib import Path

from .. import config
from .dokumenter import key
from .innhenter import lagret

WEIGHTS = {
    "angar": 40,          # "Angår mange / bør folk vite?"
    "konkret": 30,        # "Konkret endring i hverdagen"
    "juridisk_vekt": 10,  # a law or the budget binds; a request to the government less; a report least
    "uenighet": 10,       # the decisive vote was close: 54–47 counts more than 95–6
    "debatt": 10,         # proposals that fell: parties taking a different position on record
}
BADGE_MIN = {"angar": 0.75, "konkret": 0.75, "juridisk_vekt": 0.8, "uenighet": 0.6, "debatt": 0.5}


def signaler(item: dict, data_dir: Path = config.STORTINGET_DATA_DIR) -> dict:
    lagret_sak = lagret(data_dir, item["sakid"]) or {"voteringer": []}
    tellbare = [v for v in lagret_sak["voteringer"] if (v.get("antall_for") or 0) + (v.get("antall_mot") or 0) > 0]
    # The decisive vote: the adopted one with the most votes cast (the committee's proposal
    # or the law as a whole); if none was adopted, the largest one.
    vedtatt = [v for v in tellbare if v.get("vedtatt")]
    avgjorende = max(vedtatt or tellbare, key=lambda v: v["antall_for"] + v["antall_mot"], default=None)
    modell = _read(Path(data_dir) / "signaler" / f"{key(item)}.json") or {}
    return {
        "type": item.get("type"),
        "dokumentgruppe": item.get("dokumentgruppe"),
        "for": avgjorende.get("antall_for") if avgjorende else None,
        "mot": avgjorende.get("antall_mot") if avgjorende else None,
        "forslag_falt": sum(1 for v in lagret_sak["voteringer"] if not v.get("vedtatt") and "forslag" in
                            (v.get("votering_tema") or "").lower()),
        "angar": _verdi(modell.get("angar")),
        "konkret": _verdi(modell.get("konkret")),
    }


def _parts(sig: dict) -> dict:
    vekt = {"lovsak": 1.0, "budsjett": 0.9}.get(sig.get("type"), 0.0)
    if not vekt:
        vekt = 0.5 if sig.get("dokumentgruppe") in ("proposisjon", "representantforslag") else 0.2
    f, m = sig.get("for"), sig.get("mot")
    uenighet = (1 - abs(f - m) / (f + m)) if f is not None and m is not None and f + m else 0.0
    return {
        "angar": (sig["angar"] - 1) / 4 if sig.get("angar") else None,
        "konkret": (sig["konkret"] - 1) / 4 if sig.get("konkret") else None,
        "juridisk_vekt": vekt,
        "uenighet": round(uenighet, 3),
        "debatt": round(1 - 0.75 ** sig.get("forslag_falt", 0), 3),
    }


def score(sig: dict) -> tuple[int, list[str]]:
    used = {k: v for k, v in _parts(sig).items() if v is not None}
    total = sum(WEIGHTS[k] for k in used)
    points = {k: WEIGHTS[k] * v for k, v in used.items()}
    value = round(100 * sum(points.values()) / total) if total else 0
    reasons = []
    for k in sorted(points, key=points.get, reverse=True):
        if points[k] <= 0 or used[k] < BADGE_MIN[k]:
            continue
        if badge := _badge(k, sig):
            reasons.append(badge)
        if len(reasons) == 3:
            break
    return value, reasons


def _badge(k: str, sig: dict) -> str:
    if k == "angar":
        return "angår mange"
    if k == "konkret":
        return "konkret endring"
    if k == "juridisk_vekt":
        return {"lovsak": "lovendring", "budsjett": "statsbudsjettet"}.get(sig.get("type"), "")
    if k == "uenighet":
        return f"{sig['for']} mot {sig['mot']}"
    if k == "debatt":
        n = sig.get("forslag_falt", 0)
        return f"{n} forslag falt" if n >= 2 else ""
    return ""


def rank(items: list[dict], data_dir: Path = config.STORTINGET_DATA_DIR) -> list[dict]:
    out = []
    for i in items:
        sig = signaler(i, data_dir)
        value, reasons = score(sig)
        out.append({"nr": i["referansenr"], "key": key(i), "tittel": i["tittel"], "score": value,
                    "grunner": reasons, "signaler": sig, "status": {"kode": "vedtatt"}})
    return sorted(out, key=lambda r: (-r["score"], r["nr"]))


def _verdi(signal) -> int | None:
    v = (signal or {}).get("verdi") if isinstance(signal, dict) else None
    return v if isinstance(v, int) and 1 <= v <= 5 else None


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None
