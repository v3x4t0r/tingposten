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

W = 320  # viewBox width; drawn 1:1 in the 320 px rail and at phone width
MAP_W = 240  # the locator map needs less room than the bars
FONT = 12
CHARS_PER_LINE = 46  # conservative for 12 px system sans in ~300 px
LABEL = 13  # vote diagram: proposal labels and counts
SMALL = 12  # vote diagram: party lists and the rest line — never smaller

# Seats in bystyret: only used to scale bars when the protokoll doesn't say how
# many were present (votes in competing rounds sum to 67, e.g. 242/26, 260/26).
# Never drawn as a number.
HOUSE = {"Bergen bystyre": 67}

PARTY = {
    "AP": "Ap", "FRP": "FrP", "KRF": "KrF", "SP": "Sp", "H": "H", "SV": "SV",
    "V": "V", "MDG": "MDG", "R": "R", "INP": "INP", "BL": "BL", "PP": "PP",
}
MAX_BARS = 4
MAX_ADOPTED = 2  # bars for adopted votes; the rest of the budget always goes to proposals that fell

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
    """The vote that decided the sak as one bar split in for and against, the
    other proposals as bars under it, tilleggsforslag in their own group,
    stated counts only.

    The split bar is the adopted proposal (the main innstilling vote when
    several points were voted on): the votes for grow from the left on a track
    the size of the house, the votes against from the right. Against is the
    attendance minus the votes for when the protokoll states it, else the
    count of the competing main proposal (innstilling or alternativforslag)
    that fell when the two counts fill the house — the same reading as the
    «Avstemning» fact. Alternatives are often voted on in sequence, so any
    other pair is two votes, and then only the votes for are shown. The
    parties against are named only when the protokoll names them: when the
    competing proposal's count is the against-count.

    Neutral by construction: for and against differ only by the --tp-for/
    --tp-falt ink step, never party colours; proposals that fell always get
    bars (up to MAX_BARS, at most MAX_ADOPTED adopted), ordered by their count,
    every label in the same weight. Adopted parts with the same count and the
    same parties are one bar ("punkt 1, 2 og 4"); proposals from the same
    proposer that fell with the same count and parties likewise. A party the
    protokoll counts in part ("3V") is "3 fra V", and a bare "V" in another
    vote of the same sak is then "resten av V". None when every vote with a
    count is unanimous or there is only a single count with nothing to set it
    against, or for Eventuelt (several unrelated proposals in one protokoll).
    """
    if (protokoll.get("tittel") or "").lower().startswith("eventuelt"):
        return None
    main_votes, tillegg = _deciding(protokoll.get("voteringer") or [])
    unanimous = [v for v in main_votes if v["enstemmig"]]
    main_votes = _rest_of_party([v for v in main_votes if not v["enstemmig"] and v["stemmer"]])
    tillegg = _rest_of_party([v for v in tillegg if not v["enstemmig"] and v["stemmer"]])
    organ = protokoll.get("organ") or "bystyret"
    groups = _groups(main_votes)
    split = _split(groups, protokoll.get("til_stede"), HOUSE.get(organ))
    if (not split or not split["mot"]) and len(main_votes) + len(tillegg) < 2:
        return None
    counts = [v["stemmer"] for v in main_votes + tillegg]
    house = max(protokoll.get("til_stede") or HOUSE.get(organ) or 0, *counts, (split["for"] + (split["mot"] or 0)) if split else 0)

    parts, y, summary = [], 0, ""
    if split:
        y = _split_bar(parts, split, house, y)
        if split["mot"]:
            summary = f"{split['for']} mot {split['mot']}. "
        groups = [g for g in groups if g is not split["main"] and g is not split["motpart"]]
    elif unanimous and tillegg and not main_votes:
        y = _line(parts, f"{_merged_name(unanimous)} ble enstemmig vedtatt.", y)
    shown, hidden = _pick(groups, MAX_BARS - (1 if split else 0), MAX_ADOPTED - (1 if split else 0))
    y = _bar_section(parts, shown, hidden, house, y)
    if tillegg:
        y += 4
        parts.append(_text(0, y + SMALL, "TILLEGGSFORSLAG", weight=700, size=SMALL, spaced=True))
        y += SMALL + 2
        y = _bar_section(parts, *_pick(_groups(tillegg), MAX_BARS, MAX_ADOPTED), house, y)
    y -= 10  # the last gap is the figure's bottom margin

    said = [_said(g) for g in _order(_groups(main_votes))]
    if tillegg:
        said.append("tilleggsforslag: " + "; ".join(_said(g) for g in _order(_groups(tillegg))))
    alt = f"Avstemning i {organ}: {summary}" + "; ".join(said) + "."
    return {"svg": _svg("Slik ble det stemt", alt, parts, y), "alt": alt, "kilde": "Protokollen, votering",
            # the same two numbers as data, for the facts under a story
            "stemmer": {"for": split["for"], "mot": split["mot"]} if split else None}


def _bar_section(parts: list[str], shown: list[dict], hidden: list[dict], house: int, y: float) -> float:
    top = y
    y = _bars(parts, shown, house, y)
    if shown:
        # baseline: one hairline the bars grow from, along the bars and their labels
        parts.insert(0, f'<line x1="0.5" y1="{top:.0f}" x2="0.5" y2="{y - 12:.0f}" stroke="var(--tp-kart, #e1e0d9)" stroke-width="1"/>')
    if hidden:
        y = _line(parts, _rest(hidden), y)
    return y


def _line(parts: list[str], text: str, y: float) -> float:
    """A muted small-print sentence, wrapped, with the section gap under it."""
    for line in _wrap(text, _chars(SMALL), no_orphan=True):
        y += SMALL + 4
        parts.append(_text(0, y, line, muted=True, size=SMALL))
    return y + 16


_NUMBERED = re.compile(r"(\d+)\s*([A-ZÆØÅ]+)")


def _rest_of_party(votes: list[dict]) -> list[dict]:
    """"3V" in one vote and a bare "V" in another: the bare one is the rest of
    V, which is how the protokoll means it. Marked "REST V" for _party."""
    numbered = {m[2] for v in votes for t in v["partier"] if (m := _NUMBERED.fullmatch(t.strip()))}
    if not numbered:
        return votes
    return [{**v, "partier": [f"REST {t.strip()}" if t.strip() in numbered else t for t in v["partier"]]} for v in votes]


def _split(groups: list[dict], til_stede: int | None, seats: int | None) -> dict | None:
    """The deciding vote as for/against: {main, for, mot, motpart, partier_mot}.

    main: the adopted group covering most votes (protokoll order at a tie).
    mot: attendance minus the votes for when stated, else the largest competing
    main proposal that fell when the two fill the house (members present must
    vote; any other pair is two separate votes), else None: only the votes for
    are known. partier_mot only when that competing proposal's count is the
    against-count (then the protokoll names them); None otherwise.
    """
    adopted = sorted((g for g in groups if g["won"]), key=lambda g: (-len(g["votes"]), g["first"]))
    if not adopted:
        return None
    main = adopted[0]
    rivals = sorted((g for g in groups if not g["won"] and _is_main(g)), key=lambda g: (-g["stemmer"], g["first"]))
    rival = rivals[0] if rivals else None
    if til_stede:
        mot = til_stede - main["stemmer"]
        if mot <= 0:
            return None
    elif rival and seats and main["stemmer"] + rival["stemmer"] == seats:
        mot = rival["stemmer"]
    else:
        mot = None
    named = rival if rival and mot and rival["stemmer"] == mot else None
    return {"main": main, "for": main["stemmer"], "mot": mot, "motpart": named,
            "partier_mot": named["partier"] if named else None}


def _is_main(g: dict) -> bool:
    return bool(re.match(r"innstilling|alternativ", _name(g["votes"][0]), re.I))


def _split_bar(parts: list[str], split: dict, house: int, y: float) -> float:
    """Label, the two counts, one bar (for from the left, against from the
    right, the gap is the rest of the house), then who voted for and against."""
    g, plot, h = split["main"], W, 18
    for i, line in enumerate(_wrap(f"Vedtatt: {g['navn']}", _chars(LABEL), no_orphan=True)):
        y += LABEL + 4
        parts.append(_text(0, y, line, size=LABEL, lead="Vedtatt:" if i == 0 else None))
    y += 8
    parts.append(_text(0, y + LABEL, str(split["for"]), weight=600, size=LABEL))
    if split["mot"]:
        parts.append(_text(plot, y + LABEL, str(split["mot"]), weight=600, size=LABEL, anchor="end"))
    y += LABEL + 6
    w_for = max(4.0, split["for"] / house * plot)
    w_mot = max(4.0, split["mot"] / house * plot) if split["mot"] else 0
    gap = plot - w_for - w_mot
    if w_mot and gap < 2:  # no one between: a hairline of paper keeps the two apart
        w_for, w_mot = w_for - 1 + gap / 2, w_mot - 1 + gap / 2
    parts.append(f'<rect x="0" y="{y:.1f}" width="{plot}" height="{h}" rx="4" fill="var(--tp-spor, #cde2fb)">'
                 f"<title>{house} til stede</title></rect>" if split["mot"] or house > split["for"] else "")
    parts.append(f'<path d="{_bar(0, y, w_for, h)}" fill="var(--tp-for, #2a78d6)">'
                 f"<title>{escape(_said(g, parties=False))}</title></path>")
    if w_mot:
        mot_title = _said(split["motpart"], parties=False) if split["motpart"] else f"{split['mot']} stemte ikke for"
        parts.append(f'<path d="{_bar_end(plot - w_mot, y, w_mot, h)}" fill="var(--tp-falt, #898781)">'
                     f"<title>{escape(mot_title)}</title></path>")
    y += h + 2
    for_line = "For: " + _parties(g["partier"])
    if split["partier_mot"] is not None:
        why = f" (stemte for {_lower(split['motpart']['navn'])})" if split["motpart"] else ""
        mot_line = "Mot: " + _parties(split["partier_mot"]) + why
    elif split["mot"]:
        mot_line = "Mot: partiene er ikke oppgitt i protokollen"
    else:
        mot_line = "Mot: antallet er ikke oppgitt i protokollen"
    for text, lead in ((for_line, "For:"), (mot_line, "Mot:")):
        for i, line in enumerate(_wrap(text, _chars(SMALL), no_orphan=True)):
            y += SMALL + 4
            parts.append(_text(0, y, line, muted=True, size=SMALL, lead=lead if i == 0 else None))
    return y + 20


def _bars(parts: list[str], shown: list[dict], house: int, y: float) -> float:
    """One bar per group with its label, count and the parties for."""
    x0, plot = 0, W - 34  # room for the count at the bar tip
    for g in shown:
        lead = "Vedtatt:" if g["won"] else "Falt:"
        for i, line in enumerate(_wrap(f"{lead} {g['navn']}", _chars(LABEL), no_orphan=True)):
            y += LABEL + 4
            parts.append(_text(x0, y, line, size=LABEL, lead=lead if i == 0 else None))
        y += 6
        w = max(4.0, g["stemmer"] / house * plot)
        colour = "var(--tp-for, #2a78d6)" if g["won"] else "var(--tp-falt, #898781)"
        parts.append(
            f'<path d="{_bar(x0, y, w, 14)}" fill="{colour}">'
            f"<title>{escape(_said(g, parties=False))}</title></path>"
        )
        parts.append(_text(x0 + w + 6, y + 11.5, str(g["stemmer"]), weight=600, size=LABEL))
        y += 14
        for line in _wrap("For: " + _parties(g["partier"]), _chars(SMALL), no_orphan=True):
            y += SMALL + 4
            parts.append(_text(x0, y, line, muted=True, size=SMALL))
        y += 16
    return y


def _deciding(votes: list[dict]) -> tuple[list[dict], list[dict]]:
    """(votes on the sak itself, tilleggsforslag).

    The sak itself: innstilling, alternatives, postponement. Merknader and
    protokolltilførsler never count. Tilleggsforslag are voted on by
    themselves and go in their own group — unless there is no innstilling at
    all (interpellasjoner), where they are the decision.
    """
    # The raw text counts too: "Innstillingen fikk ingen stemmer, og var dermed
    # falt. Alternativforslag …" means there was an innstilling.
    has_innstilling = any(v["forslag"].lower().startswith("innstilling") or _name(v).lower().startswith("innstilling")
                          for v in votes)
    main, tillegg = [], []
    for v in votes:
        f = _name(v).lower()
        if f.startswith(("merknad", "protokoll")):
            continue
        (tillegg if f.startswith("tillegg") and has_innstilling else main).append(v)
    return main, tillegg


def _groups(votes: list[dict]) -> list[dict]:
    """Votes that read as one bar: same outcome, same count, same parties for.

    Adopted votes merge regardless of proposer (typically points of the
    innstilling voted on separately); fallen votes only when the proposer is
    the same too, so a bar never blurs whose alternative it was.
    """
    groups: list[dict] = []
    for i, v in enumerate(votes):
        won = v["resultat"] == "vedtatt"
        key = (won, v["stemmer"], tuple(sorted(v["partier"])))
        g = next((g for g in groups if g["key"] == key and (won or _from(g["votes"][0]) == _from(v))), None)
        if g:
            g["votes"].append(v)
        else:
            groups.append({"key": key, "votes": [v], "first": i, "won": won,
                           "stemmer": v["stemmer"], "partier": v["partier"]})
    for g in groups:
        g["navn"] = _merged_name(g["votes"])
    return groups


def _pick(groups: list[dict], max_bars: int = MAX_BARS, max_adopted: int = MAX_ADOPTED) -> tuple[list[dict], list[dict]]:
    """The groups that get bars (in drawing order), and the votes left for the rest line.

    Adopted: the group covering most votes first, then protokoll order (the
    main innstilling vote comes first). Fallen: the largest first; at equal
    counts a real alternative before a postponement.
    """
    adopted = sorted((g for g in groups if g["won"]), key=lambda g: (-len(g["votes"]), g["first"]))
    fell = sorted((g for g in groups if not g["won"]),
                  key=lambda g: (-g["stemmer"], _name(g["votes"][0]).lower().startswith("utsettelse"), g["first"]))
    n_fell = min(len(fell), max_bars - min(len(adopted), max_adopted))
    n_won = min(len(adopted), max_bars - n_fell)
    shown = _order(adopted[:n_won] + fell[:n_fell])
    hidden = [v for g in _order(adopted[n_won:] + fell[n_fell:]) for v in g["votes"]]
    return shown, hidden


def _order(groups: list[dict]) -> list[dict]:
    """By count, the way the votes fell, ties in protokoll order; never by outcome."""
    return sorted(groups, key=lambda g: (-g["stemmer"], g["first"]))


def _said(g: dict, parties: bool = True) -> str:
    each = " hver" if len(g["votes"]) > 1 else ""
    who = f" ({_parties(g['partier'])})" if parties else ""
    return f"{g['navn']} fikk {_stemmer([g['stemmer']])}{each}{who} og {'ble vedtatt' if g['won'] else 'falt'}"


def _rest(votes: list[dict]) -> str:
    """The votes without a bar, each named with its count:
    'Vedtatt også: innstillingens punkt 4 (58 stemmer). Falt også: alternativforslag fra R (3 stemmer).'"""
    won = [v for v in votes if v["resultat"] == "vedtatt"]
    fell = [v for v in votes if v["resultat"] != "vedtatt"]
    said = []
    for lead, vs in (("Vedtatt også: ", won), ("Falt også: ", fell)):
        if vs:
            said.append(lead + _join([f"{_lower(_proposal(v))} ({_stemmer([v['stemmer']])})" for v in vs]) + ".")
    return " ".join(said)


def _stemmer(counts: list[int]) -> str:
    return _join([str(n) for n in counts]) + (" stemme" if counts == [1] else " stemmer")


def _proposal(v: dict) -> str:
    return _name(v) + _from(v)


def _name(v: dict) -> str:
    """The proposal as the protokoll names it, tidied: no run-in preamble, en dashes."""
    name = " ".join(v["forslag"].split())
    # The parser can run the previous sentence in ("Ingen motsatte seg å votere
    # over forslagene. Alternativforslag"): keep the last sentence.
    name = re.split(r"(?<=[a-zæøå)]\.)\s+(?=[A-ZÆØÅ])", name)[-1]
    return _dashes(name.rstrip(" ,."))


def _from(v: dict) -> str:
    if v.get("fra") and not _name(v).lower().startswith("innstilling"):
        return " fra " + _join(_names(v["fra"]))
    return ""


_POINT = re.compile(r"(?P<head>.*?)(?P<kw>\b(?:punkt|pkt\.))\s+(?P<spec>\S.*)")


def _merged_name(votes: list[dict]) -> str:
    """One label for several votes: "Innstillingen punkt 1, 2 og 4",
    "Utsettelsesforslag og alternativforslag fra SV"."""
    if len(votes) == 1:
        return _proposal(votes[0])
    froms = {_from(v) for v in votes}
    if len(froms) > 1:
        return _join([_proposal(v) if i == 0 else _lower(_proposal(v)) for i, v in enumerate(votes)])
    names, suffix = [_name(v) for v in votes], froms.pop()
    points = [_POINT.fullmatch(n) for n in names]
    if all(points) and len({(m["head"], m["kw"]) for m in points}) == 1:
        head, kw, specs = points[0]["head"], points[0]["kw"], [m["spec"] for m in points]
        if any("," in s for s in specs):  # "punkt 1, punkt 2 og punkt 4, avsnitt 1"
            return head + _join([f"{kw} {s}" for s in specs]) + suffix
        return f"{head}{kw} {_join(specs)}{suffix}"
    return _join([n if i == 0 else _lower(n) for i, n in enumerate(names)]) + suffix


def _lower(s: str) -> str:
    """Lower-case a leading common noun ("Alternativforslag"), never an abbreviation."""
    return s[0].lower() + s[1:] if len(s) > 1 and s[1].islower() else s


_RANGE = re.compile(r"(?<![^\W\d_])(\d+(?:\.\d+)*[a-z]?|[ivx]{1,4}|[A-Za-z])-(?=(?:\d|(?:[ivx]{1,4}|[A-Za-z])(?![^\W\d_])))")


def _dashes(s: str) -> str:
    """En dash for a spaced hyphen used as a dash and for ranges ("c–d", "4.1–4.3");
    hyphenated words and §-numbers ("§ 12-1") stay as they are."""
    s = s.replace(" - ", " – ")
    return _RANGE.sub(lambda m: m.group(0) if s[:m.start()].rstrip().endswith("§") else m.group(1) + "–", s)


def _party(token: str) -> str:
    token = token.strip()
    if token.startswith("REST "):
        return "resten av " + _party(token[5:])
    if token.upper().startswith("UAV"):
        return "én uavhengig"
    m = re.fullmatch(r"(\d+)\s*([A-ZÆØÅ]+)", token)
    if m:
        return f"{m.group(1)} fra {PARTY.get(m.group(2), m.group(2))}"
    return PARTY.get(token.upper(), token)


_COUNT_WORDS = {2: "to", 3: "tre", 4: "fire"}


def _names(tokens: list[str]) -> list[str]:
    """Party names in protokoll order; independents counted, not repeated."""
    uav = sum(1 for t in tokens if t.strip().upper().startswith("UAV"))
    out, said = [], False
    for t in tokens:
        if t.strip().upper().startswith("UAV"):
            if not said:
                out.append("én uavhengig" if uav == 1 else f"{_COUNT_WORDS.get(uav, uav)} uavhengige")
                said = True
        elif t.strip():
            out.append(_party(t))
    return out


def _parties(tokens: list[str]) -> str:
    return _join(_names(tokens)) or "ikke oppgitt"


def _join(names: list[str]) -> str:
    """'a, b og c' — every item kept, repeats included (11, 11 are two votes)."""
    names = [n for n in names if n]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " og " + names[-1]


def _bar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Square at the baseline, 4 px rounded data-end."""
    r = min(r, w / 2, h / 2)
    return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z")


def _bar_end(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Mirror of _bar: rounded data-end on the left, square at the right edge."""
    r = min(r, w / 2, h / 2)
    return (f"M{x + w:.1f},{y:.1f}h-{w - r:.1f}a{r},{r} 0 0 0 -{r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 0 {r},{r}h{w - r:.1f}z")


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
    part, total = {**part, "tekst": _unit(part["tekst"])}, {**total, "tekst": _unit(total["tekst"])}
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
        _text(x0, y + 55, sub, muted=True, size=SMALL),
    ]
    doc = "Vedtaket" if kilde.startswith("Vedtak") else "Saksframlegget"
    alt = (f"{doc} beskriver en økning på {part['tekst']} til totalt {total['tekst']}."
           if kind == "økning" else f"{doc} oppgir {part['tekst']} av totalt {total['tekst']}.")
    return {"svg": _svg("Beløp i sammenheng", alt, parts, y + 65), "alt": alt, "kilde": kilde,
            # The same two amounts as data, for the facts under a story.
            "belop": {"type": kind, "del": part["tekst"], "total": total["tekst"], "tekst": head}}


_MILL = re.compile(r"(?P<n>\d[\d  .]*(?:,\d+)?)(?P<sp>\s+)(?P<u>million|milliard)\b")


def _unit(tekst: str) -> str:
    """'2,6 million kroner' → '2,6 millioner kroner': plural unless exactly 1.
    The number stays as the document wrote it."""
    def fix(m):
        n = float(re.sub(r"[  .]", "", m["n"]).replace(",", "."))
        return m.group(0) if n == 1 else f"{m['n']}{m['sp']}{m['u']}er"
    return _MILL.sub(fix, tekst)


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.;:])\s+(?=[A-ZÆØÅ0-9])", " ".join(text.split()))


# --- shared SVG bits ----------------------------------------------------------

# Never broken across lines: "én uavhengig", "11 fra Ap", "punkt 4".
_GLUE = re.compile(r"\b(?:(?:én|to|tre|fire|\d+) (?:uavhengige?\b|fra \S+)|(?:punkt|pkt\.) \S+)")
NBSP = "\u00a0"


def _wrap(text: str, width: int = CHARS_PER_LINE, no_orphan: bool = False) -> list[str]:
    """Greedy word wrap. `no_orphan`: a last line of one word takes the
    previous line's last word along."""
    text = _GLUE.sub(lambda m: m.group(0).replace(" ", NBSP), " ".join(text.split()))
    lines, line = [], ""
    for word in text.split(" "):
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip(" ")
    lines += [line] if line else []
    if no_orphan and len(lines) > 1 and " " not in lines[-1] and " " in lines[-2]:
        head, moved = lines[-2].rsplit(" ", 1)
        lines[-2:] = [head, f"{moved} {lines[-1]}"]
    return [line.replace(NBSP, " ") for line in lines]


def _chars(size: float) -> int:
    """Characters per line at a font size, conservative for a fallback system
    sans (Libre Franklin is narrower)."""
    return int(W / (size * 0.52))


def _text(x, y, s, weight=400, muted=False, size=FONT, anchor="start", halo=False, lead=None, spaced=False) -> str:
    """One line of text. `lead`: a leading word set in 600 weight ("Vedtatt:");
    `spaced`: a small-caps-like section label."""
    fill = "var(--tp-muted, #52514e)" if muted else "var(--tp-fg, currentColor)"
    # paint-order goes in style: the HTML validator rejects it as an attribute.
    extra = (' stroke="var(--tp-flate, #fcfcfb)" stroke-width="3" stroke-linejoin="round" style="paint-order:stroke"'
             if halo else "") + (' letter-spacing="0.1em"' if spaced else "")
    body = escape(s)
    if lead and s.startswith(lead):
        body = f'<tspan font-weight="600">{escape(lead)}</tspan>{escape(s[len(lead):])}'
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{fill}"{extra}>{body}</text>')


def _svg(title: str, desc: str, body: list[str], height: float, width: int = W) -> str:
    uid = "tpf-" + hashlib.sha1((title + desc).encode()).hexdigest()[:8]
    h = math.ceil(height)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {h}" width="{width}" height="{h}" '
        f'role="img" aria-labelledby="{uid}-t {uid}-d" class="tp-figur" style="max-width:100%;height:auto">'
        f'<title id="{uid}-t">{escape(title)}</title><desc id="{uid}-d">{escape(desc)}</desc>'
        + "".join(body) + "</svg>"
    )
