"""Påvirkningsscore: how much a decision matters to residents, 0–100.

Every point is explainable: signals() reads plain facts off the documents,
score() weighs them with WEIGHTS and returns the three biggest reasons as
short badges for the card ("866 MNOK · Fana · 36–31"). Two signals come from
a model step and are optional (data/bergen/signaler/<key>.json); without
them the remaining weights are scaled up. The weights are meant to be
published, so they are kept readable.

First calibration (23.09.2026, one person, 34 saker): the mechanical signals
alone ranked against the user (Spearman −0.29). The user rates what residents
would want to know, so the model signals now carry half the weight.
"""

import json
import math
import re
from pathlib import Path

from . import config, pdf
from .saksframlegg import find_amounts, saksbydeler
from .status import IKKE_AVGJORT, vedtaksstatus

# Points each signal can give at full strength. Signals that are missing
# (None) are left out and the rest scaled up, so the score stays 0–100.
# Set by principle, not fitted to one evening of ratings.
WEIGHTS = {
    # Model signals (1–5 from the classifier): half the score, because the
    # first calibration showed facts on paper don't say what residents care about.
    "angar": 30,             # "Angår mange / bør innbyggerne vite?": how many it concerns, directly or not
    "konkret": 20,           # "Konkret endring i hverdagen": does something change for people now
    # Provisional: political engagement in the meeting itself. Each proposal
    # that fell is a party taking a different position on record.
    "debatt": 10,            # proposals that fell, votes held, interpellasjon
    # Facts read off the documents: what the decision binds and how widely.
    "belop": 8,              # money committed by the vedtak itself (log scale, 1 MNOK → 1 mrd.)
    "rekkevidde": 8,         # how much of the city: one property → one bydel → the whole city
    "juridisk_vekt": 8,      # plans and regulations bind for years; an election to a seat doesn't
    "uenighet": 5,           # contested in the vote; a 36–31 split counts more than 60–5
    "innstilling_falt": 4,   # the voted innstilling (often a committee's) lost, in whole or part
    "varighet": 4,           # long-lived (plan, strategy, agreement, sale) vs one-off grant
    "endelighet": 3,         # final decision (bystyret) vs a recommendation to another organ
}

# A sak that isn't settled yet keeps this share of its score, and its first
# badge says so; it comes back with full weight when it is decided.
IKKE_AVGJORT_FAKTOR = 0.3

# A signal must be at least this strong (0..1) before it is shown as a badge,
# so weak contributions ("rapport", a 60–5 vote) don't pose as reasons.
BADGE_MIN = {
    "belop": 0.01,
    "rekkevidde": 0.35,       # at least one named bydel
    "juridisk_vekt": 0.5,
    "uenighet": 0.5,          # a close vote, not a lopsided one
    "innstilling_falt": 0.5,
    "varighet": 0.9,
    "endelighet": 0.0,
    "angar": 0.75,            # verdi 4 or 5
    "konkret": 0.75,          # verdi 4 or 5
    "debatt": 0.5,
}

# Seats, used only to judge how close a vote was when the protokoll states
# the votes for but not against. Never shown as a number.
SEATS = {"571424": 67}

# Final decision vs recommendation, per organ (plan: "Endelighet").
ENDELIGHET = {
    "571424": 1.0,  # Bergen bystyre
    "900102": 0.8,  # Byrådet – saker avgjort av byrådet (final, but within delegated limits)
    "900101": 0.4,  # Byrådet – innstilling til bystyret (bystyret decides later)
}

BYDELER = ("Arna", "Bergenhus", "Fana", "Fyllingsdalen", "Laksevåg", "Ytrebygda", "Årstad", "Åsane")

# Case types by title/vedtak wording, first match wins: (pattern, label, legal weight, durability).
# Procedural matches come first so "lovlighetskontroll av … budsjett" isn't read as a budget.
SAKSTYPER = (
    (r"fritak|uttreden|varamedlem|settemedlem|nyvalg|\bvalg av (?!trasé)", "valg til verv", 0.0, 0.0),
    (r"overlevering av representantforslag", "representantforslag", 0.0, 0.0),
    (r"interpellasjon", "interpellasjon", 0.1, 0.2),
    (r"\beventuelt\b", "eventuelt", 0.2, 0.3),
    (r"lovlighetskontroll", "lovlighetskontroll", 0.2, 0.3),
    (r"innbyggerforslag", "innbyggerforslag", 0.2, 0.3),
    (r"oppheving av (eldre|gamle)", "opprydding i planer", 0.3, 0.5),
    (r"\bkommuneplan\b|\bøkonomiplan\b|\bbudsjett\b|\bforskrift\b|\bvedtekter\b", "budsjett/forskrift", 1.0, 1.0),
    (r"kommunedelplan|valg av trasé|områderegulering", "kommunedelplan", 0.8, 1.0),
    (r"reguleringsplan|detaljregulering|midlertidig forbud|midlertidig forbod", "reguleringsplan", 0.7, 1.0),
    (r"strategi|retningslinjer|prinsipper|fordelingsmodell", "strategi", 0.5, 0.7),
    (r"opsjonsavtale|\bsalg\b|aksje|eierskap(?!skontroll)", "eierskap/salg", 0.5, 0.9),
    (r"avtale", "avtale", 0.5, 0.8),
    (r"gjennomføringsvedtak|investeringsramme|totalrehabilitering", "investering", 0.5, 0.6),
    (r"høringsuttalelse", "høringsuttalelse", 0.3, 0.2),
    (r"forvaltningsrevisjon|eierskapskontroll|sluttmelding|økonomiske perspektiver|rullering", "rapport", 0.3, 0.2),
    (r"tilskudd|bevilgning", "tilskudd", 0.4, 0.3),
)
DEFAULT_TYPE = ("annet", 0.4, 0.4)

WHOLE_CITY = re.compile(r"hele byen|hele bergen|alle bydeler|byomfattende|bergensskolen", re.I)
# "… for Bergen kommune" in a title names the organisation, often for internal
# matters (property, procurement): kommune-wide, but not the same as city-wide.
WHOLE_KOMMUNE = re.compile(r"bergen kommune", re.I)
NARROW = re.compile(r"\bgnr\.?\s*\d+.*\bbnr\b", re.I)
NO_DECISION = re.compile(r"ikke fattet vedtak|ikke behandlet", re.I)
MERKNAD = re.compile(r"^\s*merknad(er)?\b", re.I | re.M)


def signals(item: dict, protokoll: dict, saksframlegg: dict, modell: dict | None = None) -> dict:
    """Plain facts for the score. `modell` is the classifier's signal file, if any."""
    modell = modell or {}
    tittel = item.get("tittel", "")
    vedtak = _binding_part(protokoll.get("vedtak", ""))
    decided = bool(vedtak) and not NO_DECISION.search(vedtak)

    label, vekt, varig = _sakstype(tittel, vedtak)
    amounts = find_amounts(re.sub(r"\([^()]*\)", " ", vedtak)) if decided else []
    belop = max(amounts, key=lambda b: b["kroner"], default=None)

    bydeler = saksbydeler(item, saksframlegg)[0] if saksframlegg.get("bydel_treff") is not None else \
        [b for b in BYDELER if b in saksframlegg.get("bydeler", [])]
    hele_byen = decided and (
        len(saksframlegg.get("bydeler") or []) == len(BYDELER)
        or bool(WHOLE_CITY.search(tittel))
        or label == "budsjett/forskrift"
        or "økonomiske perspektiver" in tittel.lower()
    )

    split, closeness = _split(protokoll, SEATS.get(item.get("utvalgId")))
    falt, delvis = _innstilling_falt(protokoll)
    votes = protokoll.get("voteringer") or []
    return {
        "status": vedtaksstatus(item, protokoll)["kode"],
        "vedtak_fattet": decided,
        "sakstype": label,
        "juridisk_vekt": vekt if decided else 0.0,
        "varighet": varig if decided else 0.0,
        "belop_kroner": belop["kroner"] if belop else None,
        "bydeler": bydeler,
        "hele_byen": hele_byen,
        "hele_kommunen": decided and not hele_byen and bool(WHOLE_KOMMUNE.search(tittel)),
        "smal": bool(NARROW.search(tittel)),
        "enstemmig": bool(protokoll.get("enstemmig")),
        "har_votering": bool(protokoll.get("voteringer")),
        "split": split,
        "uenighet": closeness,
        "innstilling_falt": falt,
        "innstilling_delvis_falt": delvis,
        "endelighet": ENDELIGHET.get(item.get("utvalgId"), 0.5),
        "utvalgId": item.get("utvalgId"),
        "forslag_falt": sum(v["resultat"] == "falt" for v in votes),
        "voteringer": len(votes),
        "interpellasjon": item.get("kategori") == "Interpellasjon" or "interpellasjon" in tittel.lower(),
        "angar": _verdi(modell.get("angar")),
        "konkret": _verdi(modell.get("konkret")),
    }


def score(sig: dict, weights: dict = WEIGHTS) -> tuple[int, list[str]]:
    status = sig.get("status")
    if status == "valg":
        return 0, []
    parts = _parts(sig)
    used = {k: v for k, v in parts.items() if v is not None}
    total = sum(weights[k] for k in used)
    if not total:
        return 0, []
    points = {k: weights[k] * v for k, v in used.items()}
    value = 100 * sum(points.values()) / total
    reasons = []
    if status in IKKE_AVGJORT:
        value *= IKKE_AVGJORT_FAKTOR
        reasons.append("ikke avgjort ennå")
    elif status == "orientering":
        # The user's call (29.10): taking a report to notice decides nothing
        # new, so it ranks like a sak that isn't decided yet.
        value *= IKKE_AVGJORT_FAKTOR
        reasons.append("ingen ny beslutning")
    value = round(value)
    for k in sorted(points, key=points.get, reverse=True):
        if points[k] <= 0:
            break
        if used[k] < BADGE_MIN[k]:
            continue
        badge = _badge(k, sig)
        if badge and badge not in reasons:
            reasons.append(badge)
        if len(reasons) == 3:
            break
    return value, reasons


def rank(items: list[dict], pdf_dir, parse_protokoll=None, parse_saksframlegg=None,
         data_dir: Path = config.DATA_DIR) -> list[dict]:
    if parse_protokoll is None:
        from .protokoll import parse_pdf as parse_protokoll
    if parse_saksframlegg is None:
        from .saksframlegg import parse_pdf as parse_saksframlegg
    out = []
    for item in items:
        if pdf.is_noise(item):
            continue
        p = _parse(parse_protokoll, pdf_dir / pdf.filename(item, "protokoll"))
        s = _parse(parse_saksframlegg, pdf_dir / pdf.filename(item, "saksframlegg"))
        modell = _read_json(Path(data_dir) / "signaler" / f"{pdf.key(item)}.json")
        sig = signals(item, p, s, modell)
        value, reasons = score(sig)
        out.append({
            "nr": item.get("referansenr") or pdf.tag(item),
            "tittel": item.get("tittel", ""),
            "score": value,
            "grunner": reasons,
            "signaler": sig,
            "status": vedtaksstatus(item, p),
            "modell": {k: modell[k] for k in ("angar", "konkret") if k in modell},
        })
    return sorted(out, key=lambda r: (-r["score"], r["nr"]))


def compare(ranked: list[dict], ratings: dict) -> dict:
    """How well the score agrees with a person's hand ratings."""
    by_nr = {r["nr"]: r for r in ranked}
    rated = {nr: v for nr, v in ratings.items() if nr in by_nr}

    pairs = [(by_nr[nr]["score"], v["merkbar"]) for nr, v in rated.items() if v.get("merkbar")]
    piles = {}
    for pile in ("forside", "kort", "stoy"):
        scores = [by_nr[nr]["score"] for nr, v in rated.items() if v.get("vurdering") == pile]
        piles[pile] = {"antall": len(scores), "snitt": round(sum(scores) / len(scores), 1) if scores else None}

    forside = {nr for nr, v in rated.items() if v.get("vurdering") == "forside"}
    top = {r["nr"] for r in ranked[: len(forside)]}

    def view(nr):
        r = by_nr[nr]
        return {"nr": nr, "tittel": r["tittel"], "score": r["score"], "grunner": r["grunner"],
                "kommentar": rated[nr].get("kommentar", "")}

    stoy = [nr for nr, v in rated.items() if v.get("vurdering") == "stoy"]
    return {
        "spearman": spearman([a for a, _ in pairs], [b for _, b in pairs]),
        "par": len(pairs),
        "hauger": piles,
        "forside_i_topp": {"n": len(forside), "treff": len(forside & top)},
        "forside_men_lav_score": [view(nr) for nr in sorted(forside, key=lambda n: by_nr[n]["score"])[:5]],
        "stoy_men_hoy_score": [view(nr) for nr in sorted(stoy, key=lambda n: -by_nr[n]["score"])[:5]],
    }


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho with average ranks for ties; None if undefined."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if not vx or not vy:
        return None
    return round(cov / math.sqrt(vx * vy), 3)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _parts(sig: dict) -> dict:
    """Each signal as 0..1 (None = unknown, left out of the score)."""
    kr = sig.get("belop_kroner")
    if sig.get("hele_byen"):
        reach = 1.0
    elif sig.get("hele_kommunen"):
        reach = 0.6
    elif sig.get("smal"):
        reach = 0.2
    else:
        n = len(sig.get("bydeler", []))
        reach = (0.0, 0.35, 0.5, 0.6, 0.65, 0.75, 0.8, 0.85, 1.0)[n] if sig.get("vedtak_fattet") else 0.0
    falt = 1.0 if sig.get("innstilling_falt") else 0.6 if sig.get("innstilling_delvis_falt") else 0.0
    return {
        "belop": min(1.0, max(0.0, math.log10(kr / 1e6) / 3)) if kr else 0.0,
        "rekkevidde": reach,
        "juridisk_vekt": sig.get("juridisk_vekt", 0.0),
        "uenighet": sig.get("uenighet", 0.0),
        "innstilling_falt": falt,
        "varighet": sig.get("varighet", 0.0),
        "endelighet": sig.get("endelighet", 0.5) if sig.get("vedtak_fattet") else 0.0,
        "debatt": _debatt(sig),
        "angar": (sig["angar"] - 1) / 4 if sig.get("angar") else None,
        "konkret": (sig["konkret"] - 1) / 4 if sig.get("konkret") else None,
    }


def _debatt(sig: dict) -> float:
    """0..1, saturating: an interpellasjon starts at 0.5, each proposal that
    fell takes a quarter of what's left, each further vote a tenth."""
    falt = sig.get("forslag_falt", 0)
    others = max(0, sig.get("voteringer", 0) - falt - 1)
    rest = (0.5 if sig.get("interpellasjon") else 1.0) * 0.75 ** falt * 0.9 ** others
    return round(1 - rest, 3)


def _badge(key: str, sig: dict) -> str:
    if key == "belop":
        return _kroner(sig["belop_kroner"])
    if key == "rekkevidde":
        if sig.get("hele_byen"):
            return "hele byen"
        if sig.get("hele_kommunen"):
            return "hele kommunen"
        b = sig.get("bydeler", [])
        if len(b) > 2:
            return f"{len(b)} bydeler"
        return " og ".join(b)
    if key == "juridisk_vekt":
        return sig.get("sakstype", "")
    if key == "uenighet":
        return sig.get("split") or ""
    if key == "innstilling_falt":
        return "innstillingen falt" if sig.get("innstilling_falt") else "deler av innstillingen falt"
    if key == "varighet":
        return "langsiktig"
    if key == "endelighet":
        return "" if sig.get("endelighet", 0) >= 1 else "ikke endelig"
    if key == "debatt":
        n = sig.get("forslag_falt", 0)
        if n >= 2:
            return f"{n} forslag falt"
        return "interpellasjon" if sig.get("interpellasjon") else "debattert"
    if key == "angar":
        return "angår mange"
    if key == "konkret":
        return "konkret endring"
    return ""


def _kroner(kr: float) -> str:
    if kr >= 1e9:
        return f"{kr / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " mrd. kr"
    mnok = kr / 1e6
    text = f"{mnok:.1f}".rstrip("0").rstrip(".") if mnok < 10 else f"{mnok:.0f}"
    return text.replace(".", ",") + " MNOK"


def _binding_part(vedtak: str) -> str:
    """The vedtak without merknader, which aren't binding."""
    m = MERKNAD.search(vedtak)
    return (vedtak[: m.start()] if m else vedtak).strip()


def _sakstype(tittel: str, vedtak: str) -> tuple[str, float, float]:
    """The title decides; the start of the vedtak only when the title doesn't."""
    orientering = bool(re.search(r"til orientering", vedtak[:200], re.I))
    for text in (tittel.lower(), vedtak[:300].lower()):
        for pattern, label, vekt, varig in SAKSTYPER:
            if re.search(pattern, text):
                # "Bystyret tar … til orientering" changes nothing by itself.
                return label, min(vekt, 0.3) if orientering else vekt, varig
    if orientering:
        return "til orientering", 0.2, 0.1
    return DEFAULT_TYPE


def _split(p: dict, seats: int | None = None) -> tuple[str | None, float]:
    """Badge and closeness (0..1) of the most contested binding vote."""
    votes = [v for v in p.get("voteringer", []) if not v["forslag"].lower().startswith("merknad")]
    if not votes or p.get("enstemmig"):
        return None, 0.0
    won = [v for v in votes if v["resultat"] == "vedtatt" and v["stemmer"]]
    lost = [v["stemmer"] for v in votes if v["resultat"] == "falt" and v["stemmer"]]
    # Only competing main proposals (innstilling vs alternativ) can be read
    # as one vote "36–31"; a tilleggsforslag is voted on by itself.
    lost_main = [v["stemmer"] for v in votes if v["resultat"] == "falt" and v["stemmer"] and _is_main(v)]
    if not won:
        # Main decision unanimous (or nothing adopted), but proposals fell.
        if lost and any(v["resultat"] == "vedtatt" for v in votes):
            return None, 0.3
        return None, 0.2 if lost else 0.0
    til_stede = p.get("til_stede")
    best = None
    for v in won:
        n = v["stemmer"]
        if til_stede:
            mot, badge = til_stede - n, f"{n}–{til_stede - n}"
        elif lost_main and _is_main(v):
            mot, badge = max(lost_main), f"{n}–{max(lost_main)}"
        elif seats:
            # Against-votes not stated: judge closeness from the seat count,
            # but show only the stated number.
            mot, badge = seats - n, f"{n} stemmer for"
        else:
            continue
        if mot <= 0:
            continue
        c = 1 - abs(n - mot) / (n + mot)
        if best is None or c > best[1]:
            best = (badge, c)
    if best is None:
        return f"{min(v['stemmer'] for v in won)} stemmer for", 0.5
    return best[0], max(0.3, best[1])


def _is_main(vote: dict) -> bool:
    return bool(re.match(r"innstilling|alternativ", vote["forslag"].strip(), re.I))


def _innstilling_falt(p: dict) -> tuple[bool, bool]:
    """(the innstilling as a whole lost, only some of its points fell)."""
    if not p.get("innstilling_falt"):
        return False, False
    votes = p.get("voteringer", [])
    alternative_won = any(
        v["resultat"] == "vedtatt" and v["forslag"].lower().startswith("alternativ") for v in votes
    )
    whole_fell = any(
        v["resultat"] == "falt" and re.fullmatch(r"innstillingen", v["forslag"].strip(), re.I) for v in votes
    )
    if alternative_won or whole_fell:
        return True, False
    return False, True


def _verdi(signal) -> int | None:
    """The 1–5 value of one model signal, or None if absent or malformed."""
    v = signal.get("verdi") if isinstance(signal, dict) else None
    return v if type(v) is int and 1 <= v <= 5 else None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def _parse(fn, path) -> dict:
    if not path.exists():
        return {}
    try:
        return fn(path)
    except Exception:
        return {}
