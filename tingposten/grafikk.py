"""Grafikk: small self-made SVG figures for cards.

Every number drawn is a number the documents state (the parsed protokoll or
saksframlegg); nothing is estimated or brought in from outside. Each figure
is {"svg", "alt", "kilde"}: inline SVG with <title>/<desc>, a full-sentence
text alternative, and where it comes from.

Colours are CSS custom properties with light-mode fallbacks, so the page's
light/dark theme applies (values from the dataviz reference palette; the
highlight blue is validated for both modes):

  --tp-for     highlighted mark: adopted proposal, named bydel, the part   #2a78d6 / dark #3987e5
  --tp-falt    de-emphasised mark: proposals that fell                     #898781 / dark #898781
  --tp-spor    meter track (lighter step of the highlight ramp)            #cde2fb / dark #0d366b
  --tp-kart    map base fill for bydeler not named                         #e1e0d9 / dark #383835
  --tp-flate   surface: gaps between areas, text halos                     #fcfcfb / dark #1a1a19
  --tp-fg      primary text                                                currentColor
  --tp-muted   secondary text                                              #52514e / dark #c3c2b7
"""

import hashlib
import json
import math
import re
from html import escape
from pathlib import Path

from .saksframlegg import find_amounts, saksbydeler

W = 320  # viewBox width; readable at 320 px
MAP_W = 240  # the locator map needs less room than the bars
FONT = 12
CHARS_PER_LINE = 46  # conservative for 12 px system sans in ~300 px

# Seats in bystyret: only used to scale bars when the protokoll doesn't say how
# many were present (votes in competing rounds sum to 67, e.g. 242/26, 260/26).
# Never drawn as a number.
HOUSE = {"Bergen bystyre": 67}

PARTY = {
    "AP": "Ap", "FRP": "FrP", "KRF": "KrF", "SP": "Sp", "H": "H", "SV": "SV",
    "V": "V", "MDG": "MDG", "R": "R", "INP": "INP", "BL": "BL", "PP": "PP",
}
MAX_BARS = 4

BYDELER = ("Arna", "Bergenhus", "Fana", "Fyllingsdalen", "Laksevåg", "Ytrebygda", "Årstad", "Åsane")
_GEO = json.loads((Path(__file__).parent / "geodata" / "bergen_bydeler.json").read_text())
_K = math.cos(math.radians(60.39))
# Label nudges in map units where the centroid label would collide or sit off-land.
_NUDGE = {"Bergenhus": (0, -4), "Årstad": (4, 3), "Fyllingsdalen": (-2, 2), "Laksevåg": (-2, -4)}


def figurer(item: dict, protokoll: dict, saksframlegg: dict) -> list[dict]:
    out = [
        stemmediagram(protokoll),
        bydelskart(*saksbydeler(item, saksframlegg)),
        belop_i_kontekst(protokoll, saksframlegg),
    ]
    return [f for f in out if f]


# --- vote diagram -------------------------------------------------------------

def stemmediagram(protokoll: dict) -> dict | None:
    """One bar per proposal in the votes that decided the sak, stated counts only.

    The adopted proposal is highlighted, the rest are muted (emphasis, not a
    for/against pairing the protokoll doesn't state). None when the deciding
    votes were unanimous, when fewer than two proposals have a stated count,
    or for Eventuelt (several unrelated proposals in one protokoll).
    """
    if (protokoll.get("tittel") or "").lower().startswith("eventuelt"):
        return None
    votes = [v for v in _deciding(protokoll.get("voteringer") or []) if not v["enstemmig"] and v["stemmer"]]
    if len(votes) < 2:
        return None
    adopted = sorted((v for v in votes if v["resultat"] == "vedtatt"), key=lambda v: -v["stemmer"])
    fell = sorted((v for v in votes if v["resultat"] != "vedtatt"), key=lambda v: -v["stemmer"])
    shown = (adopted + fell)[:MAX_BARS]
    rest = (adopted + fell)[MAX_BARS:]

    organ = protokoll.get("organ") or "bystyret"
    house = protokoll.get("til_stede") or HOUSE.get(organ) or max(v["stemmer"] for v in votes)
    house = max(house, max(v["stemmer"] for v in votes))
    x0, plot = 0, W - 34  # room for the count at the bar tip

    parts, y = [], 2
    for v in shown:
        won = v["resultat"] == "vedtatt"
        label = f"{_proposal(v)} – {'vedtatt' if won else 'falt'}"
        for line in _wrap(label):
            y += FONT + 3
            parts.append(_text(x0, y, line, weight=600 if won else 400))
        y += 5
        w = max(4.0, v["stemmer"] / house * plot)
        colour = "var(--tp-for, #2a78d6)" if won else "var(--tp-falt, #898781)"
        parts.append(
            f'<path d="{_bar(x0, y, w, 14)}" fill="{colour}">'
            f"<title>{escape(label)}: {v['stemmer']} stemmer</title></path>"
        )
        parts.append(_text(x0 + w + 6, y + 11, str(v["stemmer"]), weight=600))
        y += 14
        for line in _wrap("For: " + _parties(v["partier"])):
            y += FONT + 2
            parts.append(_text(x0, y, line, muted=True, size=11))
        y += 12
    if rest:
        counts = _join([str(v["stemmer"]) for v in rest])
        for line in _wrap(f"{len(rest)} forslag til fikk {counts} stemmer."):
            y += FONT + 2
            parts.append(_text(x0, y, line, muted=True, size=11))
        y += 8
    # baseline: one hairline the bars grow from
    parts.insert(0, f'<line x1="0.5" y1="0" x2="0.5" y2="{y - 8}" stroke="var(--tp-kart, #e1e0d9)" stroke-width="1"/>')

    alt = f"Avstemning i {organ}: " + "; ".join(
        f"{_proposal(v)} fikk {v['stemmer']} stemmer ({_parties(v['partier'])}) og ble {'vedtatt' if v['resultat'] == 'vedtatt' else 'stemt ned'}"
        for v in adopted + fell
    ) + "."
    return {"svg": _svg("Slik ble det stemt", alt, parts, y), "alt": alt, "kilde": "Protokollen, votering"}


def _deciding(votes: list[dict]) -> list[dict]:
    """Votes on the sak itself: innstilling, alternatives, proposals, postponement.

    Merknader and protokolltilførsler never count. Tilleggsforslag only count
    when there is no innstilling at all (interpellasjoner), because there they
    are the decision.
    """
    has_innstilling = any(v["forslag"].lower().startswith("innstilling") for v in votes)
    out = []
    for v in votes:
        f = v["forslag"].lower()
        if f.startswith(("merknad", "protokoll")):
            continue
        if f.startswith("tillegg") and has_innstilling:
            continue
        out.append(v)
    return out


def _proposal(v: dict) -> str:
    name = " ".join(v["forslag"].split()).rstrip(" ,.")
    if v.get("fra") and not name.lower().startswith("innstilling"):
        name += " fra " + _join([_party(p) for p in v["fra"]])
    return name


def _party(token: str) -> str:
    token = token.strip()
    if token.upper().startswith("UAV"):
        return "én uavhengig"
    m = re.fullmatch(r"(\d+)\s*([A-ZÆØÅ]+)", token)
    if m:
        return f"{m.group(1)} fra {PARTY.get(m.group(2), m.group(2))}"
    return PARTY.get(token.upper(), token)


def _parties(tokens: list[str]) -> str:
    return _join([_party(t) for t in tokens]) or "ikke oppgitt"


def _join(names: list[str]) -> str:
    names = list(dict.fromkeys(n for n in names if n))
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " og " + names[-1]


def _bar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Square at the baseline, 4 px rounded data-end."""
    r = min(r, w / 2, h / 2)
    return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z")


# --- bydel map ----------------------------------------------------------------

def bydelskart(bydeler: list[str], grunnlag: str = "saksframlegg") -> dict | None:
    named = [b for b in BYDELER if b in set(bydeler)]
    if not named:
        return None
    polys = {name: [[(lon * _K, -lat) for lon, lat in ring] for ring in rings] for name, rings in _GEO["bydeler"].items()}
    xs = [x for rings in polys.values() for r in rings for x, _ in r]
    ys = [y for rings in polys.values() for r in rings for _, y in r]
    pad = 6
    scale = (MAP_W - 2 * pad) / (max(xs) - min(xs))
    h = (max(ys) - min(ys)) * scale + 2 * pad

    def px(p):
        return (p[0] - min(xs)) * scale + pad, (p[1] - min(ys)) * scale + pad

    shapes, labels = [], []
    for name in BYDELER:
        hit = name in named
        d = " ".join("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in map(px, ring)) + "Z" for ring in polys[name])
        fill = "var(--tp-for, #2a78d6)" if hit else "var(--tp-kart, #e1e0d9)"
        shapes.append(
            f'<path d="{d}" fill="{fill}" stroke="var(--tp-flate, #fcfcfb)" stroke-width="2" stroke-linejoin="round">'
            f"<title>{name}{' – nevnt i saken' if hit else ''}</title></path>"
        )
        if not hit:
            continue  # context bydeler carry their name in the hover title only
        cx, cy = px(_centroid(polys[name][0]))
        dx, dy = _NUDGE.get(name, (0, 0))
        labels.append(_text(cx + dx, cy + dy, name, anchor="middle", weight=700,
                             size=11 if len(named) <= 4 else 9.5, halo=True))
    pron = "de" if len(named) > 1 else "den"
    if grunnlag == "tittel":
        why, kilde = f"sakens tittel nevner {'dem' if len(named) > 1 else 'den'}", "Bydel fra sakens tittel."
    else:
        why, kilde = f"{pron} er nevnt flere ganger i saksframlegget", "Bydeler nevnt flere ganger i saksframlegget."
    alt = f"Kart over Bergens åtte bydeler der {_join(named)} er uthevet, fordi {why}."
    return {"svg": _svg("Bydeler nevnt i saken", alt, shapes + labels, h, width=MAP_W),
            "alt": alt, "kilde": f"Kart: © OpenStreetMap-bidragsytere (ODbL). {kilde}"}


def _centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    a /= 2
    return cx / (6 * a), cy / (6 * a)


# --- money in context ---------------------------------------------------------

_INCREASE = re.compile(r"\b(øke[sr]?|økning|økes)\b.*?\bmed\b(?P<part>.*?)\b(?:til|blir)\b(?P<total>.*)", re.I | re.S)
_OF_TOTAL = re.compile(r"(?P<part>.*)\bav\s+(?:totalt|en\s+total\w*|en\s+samlet\w*|samlet\w*)\b(?P<total>.*)", re.I | re.S)


def belop_i_kontekst(protokoll: dict, saksframlegg: dict) -> dict | None:
    """An amount shown against the total it is part of, both stated in one sentence.

    Two patterns: an increase "med A … til/blir B" (A is part of the new total
    B), and "A av totalt/en total … B". The adopted vedtak is searched before
    the saksframlegg. Anything else — before/after comparisons, percentages
    without a stated total — gets no figure.
    """
    sources = [("Vedtak", protokoll.get("vedtak_side") or 1, protokoll.get("vedtak") or "")]
    sources += [("Saksframlegg", s["side"], s["tekst"]) for s in saksframlegg.get("seksjoner") or []]
    for doc, page, text in sources:
        for sentence in _sentences(text):
            for pattern, kind in ((_INCREASE, "økning"), (_OF_TOTAL, "andel")):
                m = pattern.search(sentence)
                if not m:
                    continue
                part = (find_amounts(m.group("part"), page) or [None])[-1 if kind == "andel" else 0]
                total = (find_amounts(m.group("total"), page) or [None])[0]
                if part and total and 0 < part["kroner"] < total["kroner"]:
                    return _meter(part, total, kind, f"{doc} s. {page}", sentence)
    return None


def _meter(part: dict, total: dict, kind: str, kilde: str, sentence: str) -> dict:
    """Meter: the part on a lighter track of the same hue. Labels say only what
    the sentence says; the alt text attributes it to its document, because a
    saksframlegg proposes and the adopted vedtak may differ."""
    x0, plot, y = 0, W - 2, 6
    head = f"Ny sum: {total['tekst']}" if kind == "økning" else f"Totalt: {total['tekst']}"
    sub = f"Herav økning: {part['tekst']}" if kind == "økning" else f"Herav: {part['tekst']}"
    w = max(4.0, part["kroner"] / total["kroner"] * plot)
    # An increase sits at the end of the new sum; a share starts at the baseline.
    px = x0 + plot - w if kind == "økning" else x0
    parts = [
        _text(x0, y + FONT, head, weight=600),
        f'<path d="{_bar(x0, y + 20, plot, 14)}" fill="var(--tp-spor, #cde2fb)"/>',
        f'<path d="{_bar(px, y + 20, w, 14)}" fill="var(--tp-for, #2a78d6)">'
        f"<title>{escape(sub)} av {escape(total['tekst'])}</title></path>",
        _text(x0, y + 54, sub, muted=True, size=11),
    ]
    doc = "Vedtaket" if kilde.startswith("Vedtak") else "Saksframlegget"
    alt = (f"{doc} beskriver en økning på {part['tekst']} til totalt {total['tekst']}."
           if kind == "økning" else f"{doc} oppgir {part['tekst']} av totalt {total['tekst']}.")
    return {"svg": _svg("Beløp i sammenheng", alt, parts, y + 64), "alt": alt, "kilde": kilde,
            # The same two amounts as data, for the facts under a story.
            "belop": {"type": kind, "del": part["tekst"], "total": total["tekst"], "tekst": head}}


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.;:])\s+(?=[A-ZÆØÅ0-9])", " ".join(text.split()))


# --- shared SVG bits ----------------------------------------------------------

def _wrap(text: str, width: int = CHARS_PER_LINE) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])


def _text(x, y, s, weight=400, muted=False, size=FONT, anchor="start", halo=False) -> str:
    fill = "var(--tp-muted, #52514e)" if muted else "var(--tp-fg, currentColor)"
    extra = ' paint-order="stroke" stroke="var(--tp-flate, #fcfcfb)" stroke-width="3" stroke-linejoin="round"' if halo else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{fill}"{extra}>{escape(s)}</text>')


def _svg(title: str, desc: str, body: list[str], height: float, width: int = W) -> str:
    uid = "tpf-" + hashlib.sha1((title + desc).encode()).hexdigest()[:8]
    h = math.ceil(height)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {h}" width="{width}" height="{h}" '
        f'role="img" aria-labelledby="{uid}-t {uid}-d" class="tp-figur" style="max-width:100%;height:auto">'
        f'<title id="{uid}-t">{escape(title)}</title><desc id="{uid}-d">{escape(desc)}</desc>'
        + "".join(body) + "</svg>"
    )
