"""Utgave: the printed edition ("papirutgave") of one meeting, as a print-ready PDF.

Built from exactly what the site shows (bygg.load): the edited, fact-checked
cards and articles, the mechanical facts and the figures. Never the models'
raw output. What goes in print, for one meeting:

- no sak whose protokoll doesn't cover the meeting yet (venter_protokoll);
- the best saker (the site's front-page order) as full stories: headline,
  ingress, and then, for a sak with an article, the article as the running
  text (a dateline, the site's fixed sideheads) with Kort fortalt as a boxed
  fact box beside or after it; for a sak without one, Kort fortalt as labelled
  paragraphs; the figures (vote chart, map, amount);
- every other sak with a summary in short form (headline, dek, key facts), in
  agenda order;
- saker without a summary only as plain lines in the agenda listing, and the
  valg and fritak group compactly.

Paper can't link, so every story ends with the pages its text rests on (the
site's kildelinje: "Kilde: Saksframlegg s. 5, 12 · Protokoll s. 3"), a short
address (tingposten.no/s/bs-266-26, see bygg.kortlenker) and a QR code to the
sak page.

The layout is explicit, page by page; nothing is fragmented across pages:
1. every block (a story in each shape it may take, a short story, a box) is
   rendered and measured in Chromium at the widths it can have;
2. a packer places whole blocks on pages: full stories as bands across the
   page, then the short stories in columns, then the back page. A full story
   that doesn't fit becomes a short one; a fact-checked sentence is never cut;
3. the pages are rendered, every box is checked for overflow, and only then is
   the PDF written. A box that overflows anyway makes the packer try again with
   that block counted taller; after a few tries the build fails.

Ink only, like the site: black text, greys for areas, no party colours and no
colour for winners or losers. The fonts are the site's (SIL OFL), made static
with fontTools so Chromium embeds them as TrueType rather than Type 3.

Needs the optional extra: uv run --extra utgave tingposten utgave …
"""

from __future__ import annotations

import hashlib
import html
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from itertools import combinations
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import bygg, config

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
MAL = HERE / "templates" / "utgave"
FONTER = HERE / "static" / "fonts"
PX_PER_MM = 96 / 25.4
UTGAVE_NR = 1  # "Prøveutgave nr. 1"

# The screen copy's paper: a warm newsprint tone (the print files stay white:
# ink on white paper, and a printed tint can't reach the sheet's edge at home).
SKJERM_PAPIR = "#f1ece0"

# Print colours: black for every glyph (small grey type breaks up in a halftone
# screen), greys only for areas (bars, map, tint). The figures read --tp-*.
FARGER = {
    "--tp-for": "#333333",
    "--tp-falt": "#8c8c8c",
    "--tp-spor": "#d9d9d9",
    "--tp-kart": "#d4d4d4",
    "--tp-flate": "#ffffff",
    "--tp-fg": "#000000",
    "--tp-muted": "#000000",
}


@dataclass(frozen=True)
class Format:
    navn: str
    bredde: float           # page, mm
    hoyde: float
    topp: float             # margins, mm
    bunn: float
    inner: float
    ytre: float
    spalter: int
    mellom: float           # column gap, mm
    sider: tuple[int, ...]  # page counts to aim for, fewest first (multiples of 4)
    folio: float            # height of the running head on inside pages, mm
    css: dict = field(default_factory=dict)  # type sizes, as CSS custom properties
    hele_andel: float = 0.75  # full stories wanted: this share of the saker with a background article

    @property
    def satsbredde(self) -> float:
        return self.bredde - self.inner - self.ytre

    @property
    def satshoyde(self) -> float:
        return self.hoyde - self.topp - self.bunn

    @property
    def spalte(self) -> float:
        return (self.satsbredde - (self.spalter - 1) * self.mellom) / self.spalter

    def spenn(self, k: int) -> float:
        """Width of k columns and the gaps between them, mm."""
        return k * self.spalte + (k - 1) * self.mellom


FORMATER = {
    # The common Nordic tabloid: 280 × 400 mm, five columns, newsprint.
    "tabloid": Format(
        navn="tabloid", bredde=280, hoyde=400, topp=13, bunn=14, inner=14, ytre=14,
        spalter=5, mellom=4.6, sider=(4, 8, 12, 16), folio=11,
        css={"--brod": "9.2pt", "--brod-linje": "11.6pt", "--ingress": "11.8pt", "--ingress-liten": "10.4pt",
             "--h-topp": "31pt", "--h-5": "25pt", "--h-3": "21pt", "--h-2": "18pt", "--h-kort": "13pt",
             "--h-lead": "46pt", "--h-front": "16.5pt", "--stikk": "7.2pt", "--liten": "7pt", "--qr": "13mm",
             "--qr-kort": "11.5mm", "--navn": "102pt"},
    ),
    # A4 booklet, stapled: folded A3 sheets, three columns.
    "a4": Format(
        navn="a4", bredde=210, hoyde=297, topp=11, bunn=12, inner=14, ytre=12,
        spalter=3, mellom=5, sider=(4, 8, 12, 16, 20), folio=9,
        css={"--brod": "9.5pt", "--brod-linje": "12.1pt", "--ingress": "11pt", "--ingress-liten": "10.2pt",
             "--h-topp": "23pt", "--h-5": "20pt", "--h-3": "20pt", "--h-2": "16.5pt", "--h-kort": "12.5pt",
             "--h-lead": "34pt", "--h-front": "17pt", "--stikk": "7pt", "--liten": "6.9pt", "--qr": "13mm",
             "--qr-kort": "11.5mm", "--navn": "66pt"},
    ),
    # A5 booklet: A4 sheets folded in the middle, stapled by hand (home inkjet).
    # The inner margin is wider: the fold and the staples take some of it.
    "a5": Format(
        navn="a5", bredde=148, hoyde=210, topp=10, bunn=11, inner=13, ytre=10,
        spalter=2, mellom=5, sider=(4, 8, 12, 16, 20, 24), folio=7.5,
        css={"--brod": "9pt", "--brod-linje": "11.5pt", "--ingress": "10.4pt", "--ingress-liten": "9.8pt",
             "--h-topp": "17.5pt", "--h-5": "16pt", "--h-3": "16pt", "--h-2": "16pt", "--h-kort": "12pt",
             "--h-lead": "23pt", "--h-front": "13pt", "--stikk": "6.6pt", "--liten": "6.6pt", "--qr": "12mm",
             "--qr-kort": "11mm", "--navn": "58pt"},
        hele_andel=0.4,
    ),
}

# Figures need room for their text: below these widths (mm) the vote chart's
# 12–13 px labels would print under 7 pt, too small for newsprint. A4 and A5
# are for laser and inkjet on smooth paper, where 6.5 pt sans still reads well:
# there a vote chart may fill one column. Drawn widths are capped likewise.
FIGUR_MIN = {"stemmer": 66.0, "belop": 66.0, "kart": 38.0}
FIGUR_MIN_FORMAT = {"a4": {"stemmer": 56.0, "belop": 56.0}, "a5": {"stemmer": 56.0, "belop": 56.0}}
FIGUR_MAKS = {"stemmer": 68.0, "belop": 68.0, "kart": 46.0}
FIGUR_MAKS_FORMAT = {"a4": {"stemmer": 60.0, "belop": 60.0, "kart": 42.0},
                     "a5": {"stemmer": 60.0, "belop": 60.0, "kart": 40.0}}


def figur_min(fmt: "Format", typ: str) -> float:
    return FIGUR_MIN_FORMAT.get(fmt.navn, {}).get(typ, FIGUR_MIN[typ])


def figur_maks(fmt: "Format", typ: str) -> float:
    return FIGUR_MAKS_FORMAT.get(fmt.navn, {}).get(typ, FIGUR_MAKS[typ])


# ---------------------------------------------------------------- content ----

class UtgaveFeil(RuntimeError):
    """The edition can't be made as asked (no such meeting, or it doesn't fit)."""


def velg(saker: list[dict], mote: str) -> dict:
    """What goes in print from one meeting ("571424-2026-09-23"), and in what order.

    fulle: candidates for full stories, the site's front-page order (bygg.front_page:
    score, then date and agenda order); korte: every sak with a summary in agenda
    order (the packer takes the full ones out); liste: saker without a summary
    (a plain line each); valg: valg and fritak. A sak waiting for its protokoll
    is left out entirely and only counted (venter)."""
    alle = [s for s in saker if s["mote"] == mote]
    if not alle:
        raise UtgaveFeil(f"fant ikke møtet {mote}")
    venter = [s for s in alle if s.get("venter_protokoll")]
    trykk = sorted((s for s in alle if not s.get("venter_protokoll")), key=lambda s: s["seq"])
    if not trykk:
        raise UtgaveFeil(f"ingen saker fra {mote} er klare for trykk (venter på protokoll)")
    valg = [s for s in trykk if bygg.er_valg(s)]
    saker_ = [s for s in trykk if not bygg.er_valg(s)]
    med = [s for s in saker_ if s["har_sammendrag"]]
    front = bygg.front_page(med) if med else {"lead": None, "sekundaer": [], "liste": []}
    rekke = [s for s in [front["lead"], *front["sekundaer"], *front["liste"]] if s]
    rekke += sorted((s for s in med if s not in rekke), key=lambda s: (-s["score"], s["seq"]))
    moter = bygg._meetings(trykk)
    return {
        "mote": {**moter[0], "slug": mote, "antall": len(alle)},
        "saker": trykk,
        "fulle": rekke,
        "korte": med,
        "liste": [s for s in saker_ if not s["har_sammendrag"]],
        "valg": valg,
        "venter": venter,
        "utfall": bygg.utfall(trykk),
    }


def kilder(s: dict, full: bool) -> list[dict]:
    """Every page the printed text rests on: the dek's, and for a full story also
    the article paragraphs', each Kort fortalt field's, the Status row's and
    the figures'. A story split in parts (deler) has each part's own; a lead
    that jumps from the front lists its whole text's pages inside, since the
    front page prints no sources. Grouped per document by bygg.kildegrupper."""
    del_ = s.get("_del")
    hode = "artikkel" if s.get("artikkel") else "kort"  # the part with the head (headline, dek)
    out = list(s.get("dek_kilder") or []) if del_ in (None, hode) else []
    if full:
        if del_ in (None, "artikkel"):
            for a in s.get("artikkel") or []:
                out += a.get("kilder") or []
        if del_ in (None, "kort"):
            for f in s.get("felt") or []:
                out += f.get("kilder") or []
            if s.get("status_rad"):
                out += s["status_rad"].get("kilder") or []
            out += s.get("_ekstra_kilder") or []
        if del_ in (None, "kort", "figurer"):
            for fig in s.get("figurer") or []:
                out += fig.get("kilder") or []
    return out


def deler(s: dict, niva: int) -> list[dict]:
    """A full story too long for one page, as parts placed one after another
    (on one page, or carried to the top of the next). Split between sections
    only, never inside one. With an article: level 1 = the story (headline,
    ingress, facts, the article's paragraphs) and then Kort fortalt with the
    figures as its own block; level 2 = the story, the figures, Kort fortalt.
    Without one: level 2 = the card without figures, then the figures."""
    if niva == 0:
        return [s]
    figs = [f for f in s.get("figurer") or [] if f.get("type") in FIGUR_MIN]
    artikkel = bool(s.get("artikkel"))
    if niva == 1:
        rekke = ("artikkel", "kort") if artikkel else ()
    else:
        rekke = tuple(d for d, med in (("artikkel", artikkel), ("figurer", bool(figs)), ("kort", True)) if med)
    if len(rekke) < 2:
        return [s]
    ut = []
    for d in rekke:
        del_ = {**s, "_del": d, "_rekke": rekke}
        if d == "artikkel" or (d == "kort" and "figurer" in rekke):
            del_["figurer"] = [] if d == "artikkel" else [f for f in s.get("figurer") or [] if f.get("type") not in FIGUR_MIN]
        ut.append(del_)
    return ut


def del_av(s: dict, del_: str, niva: int = 1) -> dict:
    """One part of a split story (see deler)."""
    return next(d for d in deler(s, niva) if d.get("_del") == del_)


def _nabo(x: dict, steg: int) -> str | None:
    rekke = x.get("_rekke") or ()
    if not rekke:
        return None
    i = rekke.index(x["_del"]) + steg
    return rekke[i] if 0 <= i < len(rekke) else None


def kildelinje(s: dict, full: bool) -> str:
    """"Saksframlegg s. 1, 3, 8 · Protokoll s. 2" (plain text)."""
    grupper = bygg.kildegrupper(kilder(s, full))
    return " · ".join(f"{g['navn']} s. {', '.join(str(k['side']) for k in g['sider'])}" for g in grupper)


def felt(s: dict) -> list[dict]:
    """Kort fortalt for print: the five fields (for a card without a written
    ingress, "Hva endres" without its first sentence, which is then the dek),
    the Status row from the protokoll when the card has none, empty fields
    left out."""
    out = [f for f in s.get("felt") or [] if (f.get("tekst") or "").strip()]
    if s.get("status_rad") and not any(f["key"] == "status" for f in out):
        status = {"key": "status", "label": "Status", "tekst": s["status_rad"]["tekst"],
                  "kilder": s["status_rad"].get("kilder") or []}
        orden = [k for k, _ in bygg.FIELDS]
        out = sorted([*out, status], key=lambda f: orden.index(f["key"]))
    return out


def enheter(s: dict) -> list[dict]:
    """The units of a story's running text, which a jump from the front page
    counts in whole: the article's paragraphs when the sak has one, otherwise
    the Kort fortalt fields (felt). Never split inside a unit."""
    return list(s.get("artikkel") or []) if s.get("artikkel") else felt(s)


def fakta(s: dict) -> list[dict]:
    """The key facts (bygg.fakta), without the web's reading-time cue and the
    status, which the kicker shows."""
    return [f for f in s.get("fakta") or [] if f["type"] not in ("bakgrunn", "status")]


def kort_visning(s: dict) -> str:
    """The short address as printed: "tingposten.no/s/bs-266-26"."""
    url = urlsplit(s["kortlenke"])
    return (url.netloc.removeprefix("www.") + url.path).rstrip("/")


ERKLAERING_DELER = ("Nøytralitet", "Personvern")  # the statement's sections that read the same on paper
OM_SIDE_DELER = ("Nøytralitet", "Personvern", "Kilder og gjenbruk")


def erklaering_deler(titler: tuple[str, ...] = ERKLAERING_DELER) -> list[dict]:
    """Sections of the editorial statement as the Om page shows it (erklaering.html,
    approved by the editor), word for word: [{"tittel", "avsnitt": [text, …]}]."""
    tekst = bygg._env().get_template("erklaering.html").render()
    biter = re.split(r"<h3>(.*?)</h3>", tekst)
    ut = []
    for tittel, kropp in zip(biter[1::2], biter[2::2]):
        if tittel in titler:
            avsnitt = [" ".join(html.unescape(re.sub(r"<[^>]+>", "", a)).split())
                       for a in re.findall(r"<p>(.*?)</p>", kropp, re.S)]
            ut.append({"tittel": tittel, "avsnitt": [a for a in avsnitt if a]})
    return sorted(ut, key=lambda d: titler.index(d["tittel"]))


def adresse(s: dict) -> Markup:
    """The short address for print, breakable only after "/s/" (never at a hyphen)."""
    tekst = kort_visning(s)
    hode, sep, hale = tekst.rpartition("/")
    if not sep:
        return Markup('<span class="nb">%s</span>') % tekst
    return Markup('<span class="nb">%s/</span><wbr><span class="nb">%s</span>') % (hode, hale)


@lru_cache(maxsize=4096)
def qr_svg(url: str) -> Markup:
    """A QR code for the full https address, as inline SVG in black (segno, ECC M)."""
    import segno

    qr = segno.make_qr(url, error="m")
    svg = qr.svg_inline(scale=1, border=0, omitsize=True, dark="#000")
    svg = svg.replace('class="segno"', 'class="qr" role="img" aria-label="QR-kode"', 1)
    return Markup(svg)


def figur_svg(svg: str) -> Markup:
    """A figure's SVG for print: the --tp-* colours (and their screen fallbacks)
    replaced by the print greys, so nothing depends on a stylesheet's tokens.

    Labels drawn with a halo (the map's bydel names: a white stroke under the
    text) are left out: Chromium can only put stroked text in a PDF as a Type 3
    font, which print shops' preflight rejects. The caption names the bydeler."""
    def bytt(m):
        return FARGER.get(m.group(1), "#000000")
    svg = re.sub(r"var\((--tp-[a-z]+)(?:,\s*[^)]*)?\)", bytt, svg)
    font = _etikettfont()
    svg = re.sub(r"<text\b([^>]*\bstroke=[^>]*)>(.*?)</text>", lambda m: _etikett_som_sti(m, font), svg, flags=re.S)
    return Markup(svg)


def _etikettfont() -> Path | None:
    """The bold Franklin instance (fonter()), for drawing map labels as outlines."""
    funnet = sorted(font_mappe().glob("TPFranklin-wght700-normal-*.ttf"))
    return funnet[0] if funnet else None


@lru_cache(maxsize=4)
def _glyfer(sti: str):
    from fontTools.ttLib import TTFont

    f = TTFont(sti)
    return f.getGlyphSet(), f.getBestCmap(), f["head"].unitsPerEm, f["hmtx"]


def _etikett_som_sti(m: re.Match, font: Path | None) -> str:
    """A map label with a halo (the bydel's name over the map, as on the site) as
    a vector path: Chromium can only put stroked *text* in a PDF as a Type 3
    font, which print shops' preflight rejects; a stroked path is plain vector
    art. Without the font file the label is left out (the caption names the bydel)."""
    if font is None:
        return ""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    attr = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
    tekst = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
    glyfer, cmap, upm, hmtx = _glyfer(str(font))
    k = float(attr.get("font-size", 12)) / upm
    navn = [cmap.get(ord(c)) for c in tekst]
    bredde = sum(hmtx[n][0] for n in navn if n) * k
    x, y = float(attr.get("x", 0)), float(attr.get("y", 0))
    x -= {"middle": bredde / 2, "end": bredde}.get(attr.get("text-anchor"), 0)
    d = []
    for n in navn:
        if n:
            pen = SVGPathPen(glyfer)
            glyfer[n].draw(TransformPen(pen, (k, 0, 0, -k, x, y)))
            d.append(pen.getCommands())
            x += hmtx[n][0] * k
    return (f'<path class="etikett-sti" d="{" ".join(d)}" fill="{attr.get("fill", "#000000")}" '
            f'stroke="{attr.get("stroke", "#ffffff")}" stroke-width="{attr.get("stroke-width", "3")}" '
            f'stroke-linejoin="round" style="paint-order:stroke"/>')


def svg_storrelse(svg: str) -> tuple[float, float]:
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    return (float(m.group(1)), float(m.group(2))) if m else (320.0, 200.0)


# ------------------------------------------------------------------ fonts ----

# The site's variable fonts as static instances: (file, CSS family, style, axes).
INSTANSER = (
    ("libre-bodoni-latin-wght-normal.woff2", "TP Bodoni", "normal", {"wght": 700}),
    ("libre-bodoni-latin-wght-normal.woff2", "TP Bodoni", "normal", {"wght": 600}),
    ("libre-bodoni-latin-wght-normal.woff2", "TP Bodoni", "normal", {"wght": 400}),
    ("libre-bodoni-latin-wght-italic.woff2", "TP Bodoni", "italic", {"wght": 400}),
    ("libre-franklin-latin-wght-normal.woff2", "TP Franklin", "normal", {"wght": 400}),
    ("libre-franklin-latin-wght-normal.woff2", "TP Franklin", "normal", {"wght": 500}),
    ("libre-franklin-latin-wght-normal.woff2", "TP Franklin", "normal", {"wght": 600}),
    ("libre-franklin-latin-wght-normal.woff2", "TP Franklin", "normal", {"wght": 700}),
    ("libre-franklin-latin-wght-italic.woff2", "TP Franklin", "italic", {"wght": 400}),
    # Body text at the small optical size: sturdier serifs and hairlines on newsprint.
    ("source-serif-4-latin-opsz-normal.woff2", "TP Serif", "normal", {"wght": 400, "opsz": 10}),
    ("source-serif-4-latin-opsz-normal.woff2", "TP Serif", "normal", {"wght": 600, "opsz": 10}),
    ("source-serif-4-latin-opsz-normal.woff2", "TP Serif", "normal", {"wght": 700, "opsz": 10}),
    ("source-serif-4-latin-opsz-italic.woff2", "TP Serif", "italic", {"wght": 400, "opsz": 10}),
    ("source-serif-4-latin-opsz-italic.woff2", "TP Serif", "italic", {"wght": 600, "opsz": 10}),
    # Deks at a larger optical size.
    ("source-serif-4-latin-opsz-normal.woff2", "TP Serif Display", "normal", {"wght": 400, "opsz": 16}),
    ("source-serif-4-latin-opsz-italic.woff2", "TP Serif Display", "italic", {"wght": 400, "opsz": 16}),
)


def font_mappe() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tingposten" / "fonter"


def fonter(mappe: Path | None = None) -> str:
    """@font-face rules for the print fonts, made once and cached on disk.

    The OFL allows embedding in documents; the instances are only a step on the
    way into the PDF and are kept in the user's cache, not distributed."""
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    mappe = mappe or font_mappe()
    mappe.mkdir(parents=True, exist_ok=True)
    regler = []
    for fil, familie, stil, akser in INSTANSER:
        kilde = FONTER / fil
        nokkel = hashlib.sha1(kilde.read_bytes() + repr(sorted(akser.items())).encode()).hexdigest()[:10]
        navn = re.sub(r"[^A-Za-z]", "", familie) + "-" + "-".join(f"{k}{v}" for k, v in sorted(akser.items()))
        ut = mappe / f"{navn}-{stil}-{nokkel}.ttf"
        if not ut.exists():
            font = TTFont(kilde)
            statisk = instancer.instantiateVariableFont(font, akser)
            ps = f"{navn}{'-Italic' if stil == 'italic' else ''}"
            for rec in statisk["name"].names:
                if rec.nameID in (1, 16):
                    rec.string = familie
                elif rec.nameID in (4,):
                    rec.string = f"{familie} {ps}"
                elif rec.nameID == 6:
                    rec.string = ps
            statisk.flavor = None
            tmp = ut.with_suffix(".tmp")
            statisk.save(tmp)
            tmp.rename(ut)
        regler.append(f'@font-face {{ font-family: "{familie}"; font-style: {stil}; font-weight: {akser["wght"]}; '
                      f'src: url("{ut.as_uri()}") format("truetype"); }}')
    navnetrekk = FONTER / "unifrakturmaguntia-latin-400-normal.woff2"
    regler.append(f'@font-face {{ font-family: "TP Navn"; font-weight: 400; src: url("{navnetrekk.as_uri()}") format("woff2"); }}')
    return "\n".join(regler)


# --------------------------------------------------------------- Chromium ----

def _chromium_kandidater() -> list[Path]:
    """Chromium builds Playwright has installed, newest first: used when the
    installed playwright package expects a build that isn't downloaded."""
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    funnet = list(base.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"))
    funnet += list(base.glob("chromium-*/chrome-linux*/chrome"))

    def bygg_nr(p: Path) -> int:
        m = re.search(r"-(\d+)$", p.parts[-3])
        return int(m.group(1)) if m else 0
    return sorted(funnet, key=bygg_nr, reverse=True)


def chromium_tilgjengelig() -> bool:
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        return False
    return bool(os.environ.get("TINGPOSTEN_CHROMIUM")) or bool(_chromium_kandidater()) or _playwright_har_egen()


def _playwright_har_egen() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


class Nettleser:
    """One headless Chromium for a whole build: measuring, checking, printing."""

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise UtgaveFeil("papirutgaven trenger Playwright: uv run --extra utgave tingposten utgave …") from e
        self._pw = sync_playwright().start()
        sti = os.environ.get("TINGPOSTEN_CHROMIUM")
        try:
            if sti:
                self._b = self._pw.chromium.launch(executable_path=sti)
            else:
                try:
                    self._b = self._pw.chromium.launch()
                except Exception:
                    kandidater = _chromium_kandidater()
                    if not kandidater:
                        raise
                    self._b = self._pw.chromium.launch(executable_path=str(kandidater[0]))
        except Exception as e:
            self._pw.stop()
            raise UtgaveFeil(f"fant ingen Chromium for Playwright ({e}); kjør `uv run --extra utgave playwright "
                             "install chromium` eller sett TINGPOSTEN_CHROMIUM") from e
        self.side = self._b.new_page()
        self.side.emulate_media(media="print")
        return self

    def __exit__(self, *exc):
        self._b.close()
        self._pw.stop()

    def last(self, html_text: str, mappe: Path) -> None:
        """Load a document from a file (so file:// fonts load) and wait for its fonts."""
        mappe.mkdir(parents=True, exist_ok=True)
        fil = mappe / f"side-{hashlib.sha1(html_text.encode()).hexdigest()[:12]}.html"
        fil.write_text(html_text)
        self.sist = fil
        self.side.goto(fil.as_uri())
        # Load every face, used or not yet, so measuring never sees a fallback font.
        status = self.side.evaluate("""() => Promise.allSettled([...document.fonts].map(f => f.load()))
            .then(() => document.fonts.ready).then(() => [...document.fonts].map(f => [f.family, f.status]))""")
        mangler = sorted({fam for fam, st in status if st != "loaded"})
        if mangler or not status:
            raise UtgaveFeil(f"fontene lastet ikke: {mangler or 'ingen @font-face'}")

    def hoyder(self) -> dict[str, float]:
        """Height in mm of every [data-maal] element."""
        # scrollHeight too: what the overflow check will see (a 112 pt nameplate's glyphs reach past its line box).
        # A block with a word wider than its column can't be used in that shape at all: infinitely tall.
        px = self.side.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[data-maal]')]
            .map(e => [e.dataset.maal, [...e.querySelectorAll('p, h2, h3, li')].some(p => p.scrollWidth > p.clientWidth + 0.75)
                       || e.scrollWidth > e.clientWidth + 0.75
                       ? -1 : Math.max(e.getBoundingClientRect().height, e.scrollHeight)]))""")
        return {k: (math.inf if v < 0 else v / PX_PER_MM) for k, v in px.items()}

    def sjekk(self) -> list[dict]:
        """Every box that spills: content taller or wider than its box (a CSS
        column box that overflows makes an extra column to the side), anything
        drawn outside its page's type area, and words wider than their column."""
        return self.side.evaluate(SJEKK_JS)

    def pdf(self, sti: Path, fmt: Format) -> None:
        sti.parent.mkdir(parents=True, exist_ok=True)
        self.side.pdf(path=str(sti), width=f"{fmt.bredde}mm", height=f"{fmt.hoyde}mm", print_background=True,
                      prefer_css_page_size=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})


SJEKK_JS = """() => {
  const ut = [];
  const tol = 0.75;  // px
  for (const boks of document.querySelectorAll('[data-boks]')) {
    const id = boks.dataset.boks;
    if (boks.scrollHeight > boks.clientHeight + tol) ut.push({boks: id, feil: 'høyde', over: boks.scrollHeight - boks.clientHeight});
    if (boks.scrollWidth > boks.clientWidth + tol) ut.push({boks: id, feil: 'bredde', over: boks.scrollWidth - boks.clientWidth});
  }
  for (const side of document.querySelectorAll('.side')) {
    const sats = side.querySelector('.sats');
    if (!sats) continue;
    const r = sats.getBoundingClientRect();
    for (const el of sats.querySelectorAll('[data-blokk]')) {
      const b = el.getBoundingClientRect();
      if (b.bottom > r.bottom + tol || b.right > r.right + tol || b.left < r.left - tol || b.top < r.top - tol)
        ut.push({boks: el.dataset.blokk, feil: 'utenfor satsen', over: Math.max(b.bottom - r.bottom, b.right - r.right)});
    }
  }
  // A word wider than its column (no hyphenation point) sticks out into the gap.
  for (const p of document.querySelectorAll('.side p, .side h2, .side h3, .side li')) {
    if (p.scrollWidth > p.clientWidth + tol) ut.push({boks: (p.closest('[data-blokk]') || {dataset: {}}).dataset.blokk || p.className, feil: 'ord for bredt', over: p.scrollWidth - p.clientWidth, tekst: p.textContent.slice(0, 80), klasse: p.className});
  }
  return ut;
}"""


# ---------------------------------------------------------------- shapes ----

@dataclass(frozen=True)
class Variant:
    """One shape a full story can take: `spalter` columns wide, its text in
    `tekst` columns, figures and key facts in an aside `side` columns wide, or
    (side = 0) in a row above the text."""
    spalter: int
    tekst: int
    side: int
    tittel: str  # CSS custom property for the headline size
    bredde: float = 0.0
    sidebredde: float = 0.0
    figurer: tuple = ()     # (figure, width mm) in the aside or the row above the text
    i_tekst: tuple = ()     # (figure, width mm) in the text columns: a map beside a wide aside

    @property
    def id(self) -> str:
        return f"{self.spalter}.{self.tekst}.{self.side}.{self.tittel.removeprefix('--h-')}"


# (columns wide, text columns, aside columns) per format, widest first.
FORMER = {
    "tabloid": {5: ((5, 0), (4, 1), (3, 2)), 3: ((3, 0), (2, 1)), 2: ((2, 0),)},
    "a4": {3: ((3, 0), (2, 1)), 2: ((2, 0),), 1: ((1, 0),)},
    "a5": {2: ((2, 0), (1, 1)), 1: ((1, 0),)},
}
TITTEL = {
    "tabloid": {5: "--h-5", 3: "--h-3", 2: "--h-2"},
    "a4": {3: "--h-5", 2: "--h-2", 1: "--h-kort"},
    "a5": {2: "--h-5", 1: "--h-kort"},
}


def varianter(s: dict, fmt: Format, spalter: int, topp: bool = False) -> list[Variant]:
    """The shapes a full story can take at this width. A vote chart or an
    amount needs room for its labels (FIGUR_MIN): an aside or a row too narrow
    for one rules that shape out, so a figure is never shrunk unreadable."""
    ut = []
    figs = [f for f in s.get("figurer") or [] if f.get("type") in FIGUR_MIN]
    if s.get("_del") == "figurer":
        bredde = fmt.spenn(spalter)
        if any(bredde < figur_min(fmt, f["type"]) for f in figs):
            return []
        valgt = tuple((f, min(figur_maks(fmt, f["type"]), bredde)) for f in figs)
        return [Variant(spalter, spalter, 0, TITTEL[fmt.navn][spalter], bredde, 0.0, valgt)]
    if s.get("_del") == "artikkel":
        # The story's head and its article, text only: the figures go with Kort fortalt.
        tittel = "--h-topp" if topp and spalter == fmt.spalter else TITTEL[fmt.navn][spalter]
        return [Variant(spalter, spalter, 0, tittel, fmt.spenn(spalter))]
    for tekst, side in FORMER[fmt.navn].get(spalter, ()):
        bredde = fmt.spenn(spalter)
        plass = fmt.spenn(side) if side else bredde
        if any(plass < figur_min(fmt, f["type"]) for f in figs):
            continue
        if side and not (figs or fakta(s)):
            continue  # an empty aside
        if side > 1 and not any(figur_min(fmt, f["type"]) > fmt.spalte for f in figs):
            continue  # a wide aside only for a figure that needs it
        tittel = "--h-topp" if topp and spalter == fmt.spalter else TITTEL[fmt.navn][spalter]
        valgt = tuple((f, min(figur_maks(fmt, f["type"]), plass)) for f in figs)
        i_tekst = ()
        if not side or (len(figs) > 1 and any(figur_min(fmt, f["type"]) > fmt.spenn(side) - fmt.spalte for f in figs)):
            # A locator map fits a text column: it goes with the text, after the body and
            # Kort fortalt (a row above the text, or an aside the vote chart fills, would only grow taller).
            i_tekst = tuple((f, min(figur_maks(fmt, f["type"]), fmt.spalte)) for f in figs if f["type"] == "kart")
            valgt = tuple(x for x in valgt if x[0]["type"] != "kart")
        ut.append(Variant(spalter, tekst, side, tittel, bredde, fmt.spenn(side) if side else 0.0, valgt, i_tekst))
    return ut


# ------------------------------------------------------------- templates ----

def _env(fmt: Format) -> Environment:
    env = bygg._env()
    env.loader = FileSystemLoader([MAL, HERE / "templates"])
    env.globals.update(
        kildelinje=kildelinje, felt=felt, enheter=enheter, fakta=fakta, qr=qr_svg, kort_visning=kort_visning, adresse=adresse, figur_svg=figur_svg,
        figur_av=bygg.figur, mellom=fmt.mellom, nr=UTGAVE_NR, lead_figurer=lambda s, b: lead_figurer(s, b, fmt),
        nettsted=urlsplit(bygg._site_url()).netloc.removeprefix("www."),
    )
    env.filters["minikart_print"] = lambda svg: Markup(str(bygg.minikart(str(figur_svg(svg)))))
    env.filters["vertsnavn"] = lambda url: urlsplit(url).netloc.removeprefix("www.")
    return env


def lead_figurer(s: dict, bredde: float, fmt: Format) -> list[tuple[dict, float]]:
    return [(f, min(figur_maks(fmt, f["type"]), bredde)) for f in s.get("figurer") or []
            if f.get("type") in FIGUR_MIN and bredde >= figur_min(fmt, f["type"])]


class Maler:
    """The block macros, rendered to Markup."""

    def __init__(self, fmt: Format):
        self.fmt = fmt
        self.env = _env(fmt)
        self.m = self.env.get_template("blokker.html").module
        self.dok = self.env.get_template("dokument.html")
        self.css = (MAL / "utgave.css").read_text()

    def __getattr__(self, navn):
        makro = getattr(self.m, navn)
        return lambda *a, **kw: Markup(str(makro(*a, **kw)))

    def dokument(self, mote: dict, fontcss: str, sider=None, maal=None, papir: str | None = None) -> str:
        return self.dok.render(fmt=self.fmt, mote=mote, nr=UTGAVE_NR, fontcss=Markup(fontcss), css=Markup(self.css),
                               luft=LUFT[self.fmt.navn], folio_luft=FOLIO_LUFT[self.fmt.navn], sider=sider or [],
                               maal=maal or [], papir=papir, dato_lang=dato_lang(mote))


def dato_lang(mote: dict) -> str:
    """"Onsdag 23. september 2026": the meeting's day, for the masthead and the folios."""
    return bygg.nb_weekday_date(mote["dato"])


LUFT = {"tabloid": 7.0, "a4": 6.0, "a5": 5.0}  # space between bands, mm (the rule sits in the middle)


# ---------------------------------------------------------------- blocks ----

@dataclass
class Blokk:
    """A measured piece: a story in one shape, a short story, a box."""
    id: str
    html: Markup
    bredde: float
    hoyde: float = 0.0
    sak: dict | None = None
    type: str = ""
    variant: Variant | None = None


@dataclass
class Rute:
    """A box on a page, in mm from the top left of the page's content area."""
    id: str
    x: float
    y: float
    b: float
    h: float
    html: Markup
    klasse: str = ""
    blokker: tuple = ()  # the blocks inside, top to bottom (the last one takes the blame for overflow)


@dataclass
class Strek:
    x: float
    y: float
    b: float
    h: float
    vekt: float = 0.5
    stil: str = "solid"


@dataclass
class Side:
    nr: int
    folio: str | None
    ruter: list = field(default_factory=list)
    streker: list = field(default_factory=list)
    saker: list = field(default_factory=list)  # (sak, "full" | "kort" | "lead" | "teaser")
    blokker: list = field(default_factory=list)  # ids of the flow's blocks and boxes on the page
    bunn: float = 0.0   # where the last band ends
    ledig: float = 0.0  # room left (front page)


class Maaling:
    """Measures blocks in Chromium, once each (by id)."""

    def __init__(self, maler: Maler, nett: Nettleser, mote: dict, fontcss: str, arbeid: Path):
        self.maler, self.nett, self.mote, self.fontcss, self.arbeid = maler, nett, mote, fontcss, arbeid
        self.hoyder: dict[str, float] = {}
        self.ekstra: dict[str, float] = {}  # mm added after a block spilled in a check

    @staticmethod
    def nokkel(b: Blokk) -> str:
        """Measured once per content and width: the same id can carry new content
        (the front page's contents box changes with the plan)."""
        return f"{b.id}#{hashlib.sha1(f'{b.bredde:.3f}|{b.html}'.encode()).hexdigest()[:12]}"

    def maal(self, blokker: list[Blokk]) -> list[Blokk]:
        nye = {self.nokkel(b): b for b in blokker if self.nokkel(b) not in self.hoyder}
        if nye:
            html_text = self.maler.dokument(self.mote, self.fontcss,
                                            maal=[{"id": k, "bredde": b.bredde, "html": b.html} for k, b in nye.items()])
            self.nett.last(html_text, self.arbeid)
            self.hoyder.update(self.nett.hoyder())
        for b in blokker:
            b.hoyde = self.hoyder[self.nokkel(b)] + self.ekstra.get(b.id, 0.0) + 0.3  # a hair for rounding
        return blokker


# ---------------------------------------------------------------- packer ----

GAP_KORT = 3.2       # between short stories in a column (their bottom margin), mm
STREKK_MAKS = 5.0    # a gap between bands may grow this much to fill a page, mm
FLYT_MIN = 70.0      # the short stories start under the last full story only with this much room, mm


@dataclass
class Band:
    celler: list  # [(Blokk, spalter)]

    @property
    def hoyde(self) -> float:
        return max(b.hoyde for b, _ in self.celler)


class Pakker:
    """Places the edition's blocks on pages (everything after the front page).

    Full stories go in the site's order as bands across the page: one story
    over all columns, or two side by side. The short stories, the agenda
    listing and the boxes follow in columns, starting under the last full
    story when there's room; the back page ends with the colophon band.
    Search: for each number of full stories, the best page breaks (dynamic
    programming over the stories); among those that give the wanted page
    count, the one with the least white space, and with more full stories on a tie."""

    def __init__(self, fmt: Format, maler: Maler, maaling: Maaling, innhold: dict, forste: int):
        self.fmt, self.maler, self.maaling, self.innhold = fmt, maler, maaling, innhold
        self.forste = forste  # full stories from this index on (0, or 1 when the lead is complete on the front)
        self.forside_bokser = ("slik",) if front_former(fmt)["slik"] else ()  # boxes the front page holds
        self.H = fmt.satshoyde - fmt.folio - FOLIO_LUFT[fmt.navn]
        self.luft = LUFT[fmt.navn]
        self._var: dict[tuple, list[Blokk]] = {}

    # -- blocks
    def varianter(self, s: dict, spalter: int, topp: bool = False) -> list[Blokk]:
        del_ = f"{s.get('_del') or 'hel'}{len(s.get('_rekke') or ())}"
        nokkel = (s["kort"], del_, spalter, topp)
        if nokkel not in self._var:
            bl = [Blokk(f"full:{s['kort']}:{del_}:{v.id}", self.maler.full(s, v), v.bredde, sak=s, type="full", variant=v)
                  for v in varianter(s, self.fmt, spalter, topp)]
            self._var[nokkel] = self.maaling.maal(bl)
        return self._var[nokkel]

    def forhandsmaal(self) -> None:
        """Measure every shape of every candidate in one go (one page load)."""
        bl = []
        for s0 in self.innhold["fulle"]:
            for s in [s0] + [d for n in (1, 2) for d in deler(s0, n) if d.get("_del")]:
                del_ = f"{s.get('_del') or 'hel'}{len(s.get('_rekke') or ())}"
                for sp in FORMER[self.fmt.navn]:
                    for topp in ((False, True) if sp == self.fmt.spalter else (False,)):
                        for v in varianter(s, self.fmt, sp, topp):
                            bl.append(Blokk(f"full:{s['kort']}:{del_}:{v.id}", self.maler.full(s, v), v.bredde, sak=s,
                                            type="full", variant=v))
        bl += self.flytblokker([])
        bl += self.flytblokker([], stor=True)
        bl += self.bunnblokker()
        bl += self.fyllbokser()
        self.maaling.maal(bl)
        self._var.clear()

    def fyllbokser(self) -> list[Blokk]:
        """Boxes that may fill the foot of a full-story page, across the page:
        how Tingposten is made, and two sections of the editorial statement."""
        f = self.fmt
        deler = [("slik", self.maler.slik())]
        deler += [(f"erklaering:{d['tittel']}", self.maler.erklaeringsdel(d)) for d in erklaering_deler()]
        spalter = {"tabloid": 4, "a4": 3, "a5": 2}[f.navn]
        return self.maaling.maal([Blokk(bid, Markup(f'<div class="band-boks" style="--boks-spalter: {spalter}">{h}</div>'),
                                        f.satsbredde, type="boks") for bid, h in deler if bid not in self.forside_bokser])

    def valgfrie(self) -> list[Blokk]:
        """Sections of the editorial statement, one column wide, that may fill
        the back page when the inside pages didn't take them."""
        sp = self.fmt.spalte
        return self.maaling.maal([Blokk(f"smal:erklaering:{d['tittel']}",
                                        Markup(f'<div class="blokk blokk-boks">{self.maler.erklaeringsdel(d)}</div>'),
                                        sp, type="boks") for d in erklaering_deler()])

    def flytblokker(self, fulle: list[dict], brukt: tuple = (), stor: bool = False) -> list[Blokk]:
        """The short stories in agenda order (stor: a size up), then the agenda
        listing, then the box on how Tingposten is made (unless a full page took it)."""
        i = self.innhold
        sp = self.fmt.spalte
        hele = {s["kort"] for s in fulle}
        ut = [(f"kort{'-stor' if stor else ''}:{s['kort']}", self.maler.kort(s, stor), s, "kort")
              for s in i["korte"] if s["kort"] not in hele]
        if i["liste"]:
            ut.append(("liste", self.maler.dagsorden("Uten sammendrag", "Saker vi ikke har oppsummert ennå, med "
                                                     "kommunens egen tittel.", i["liste"]), None, "liste"))
        if front_former(self.fmt).get("motetall_inne"):
            ut.append(("motetall", self.maler.motetall(i["mote"], i["utfall"], i["venter"]), None, "boks"))
        if i["valg"]:
            ut.append(("valg", self.maler.dagsorden("Valg og fritak", "", i["valg"]), None, "valg"))
        if "slik" not in brukt and "slik" not in self.forside_bokser:
            ut.append(("slik", self.maler.slik(), None, "boks"))
        blokker = self.maaling.maal([Blokk(bid, Markup(f'<div class="blokk blokk-{t}">{h}</div>'), sp, sak=s_, type=t)
                                     for bid, h, s_, t in ut])
        if stor:
            # A story whose head doesn't fit the column a size up keeps its normal size.
            vanlige = {b.sak["kort"]: b for b in self.flytblokker(fulle, brukt) if b.sak}
            blokker = [vanlige[b.sak["kort"]] if b.sak and b.hoyde == math.inf else b for b in blokker]
        return blokker

    def hode(self) -> Blokk:
        html_text = self.maler.flythode(self.innhold["mote"])
        return self.maaling.maal([Blokk("flythode", html_text, self.fmt.satsbredde, type="hode")])[0]

    def bunnblokker(self) -> list[Blokk]:
        """The back page's closing band: corrections, colophon."""
        f = self.fmt
        kolofon = {"tabloid": 4, "a4": 2, "a5": 1}[f.navn]
        deler = [("rettelser", self.maler.rettelser_boks(), 1), ("kolofon", self.maler.kolofon(), kolofon)]
        return self.maaling.maal([Blokk(n, h, f.spenn(k), type="bunn") for n, h, k in deler])

    # -- full-story pages
    def ordne(self, saker: list[dict], plass: float, n_bands: int) -> list[tuple[list[Band], float]]:
        """Every way to set these stories, in this order, as bands that fit in
        `plass` mm under n_bands bands already on the page: one story across
        the page, or two side by side. (bands, height used incl. the gaps)."""
        f, ut = self.fmt, []
        par = {"tabloid": ((3, 2), (2, 3)), "a4": ((2, 1), (1, 2)), "a5": ((1, 1),)}[f.navn]

        def videre(i, bands, brukt):
            if i >= len(saker):
                ut.append((list(bands), brukt))
                return
            luft = self.luft if (bands or n_bands) else 0.0
            valg = [Band([(b, f.spalter)]) for b in self.varianter(saker[i], f.spalter)]
            if i + 1 < len(saker):
                for a, b in par:
                    for va in self.varianter(saker[i], a):
                        for vb in self.varianter(saker[i + 1], b):
                            if abs(va.hoyde - vb.hoyde) < 0.2 * max(va.hoyde, vb.hoyde):
                                valg.append(Band([(va, a), (vb, b)]))
            for band in valg:
                h = brukt + luft + band.hoyde
                if h <= plass:
                    videre(i + len(band.celler), bands + [band], h)
        videre(0, [], 0.0)
        return ut

    def pakk_fulle(self, kandidater: list[dict], n_sider: int) -> tuple[list[list[Band]], float, float]:
        """n_sider pages of full stories, as an editor would fill them: the best
        story left opens each page across all columns with the big headline;
        under it the combination of the next VINDU stories that fills the page
        best. A story placed ahead of better-ranked ones costs a little
        (HOPP_KOST), so the site's order holds unless the fit is clearly better.
        The last page leaves its foot to the short stories when there's room.
        Returns the pages, the room left on the last one and the white space (mm²)."""
        f, rest, sider, kost = self.fmt, list(kandidater), [], 0.0
        ledig = self.H
        bokser = self.fyllbokser()
        flyt_min = FLYT_MIN + self.hode().hoyde
        while len(sider) < n_sider:
            if not rest:
                raise UtgaveFeil("ikke nok saker til så mange sider")
            siste = len(sider) == n_sider - 1
            beste = None
            for lv in self.varianter(rest[0], f.spalter, topp=True):
                if lv.hoyde > self.H:
                    continue
                topp = Band([(lv, f.spalter)])
                kand = rest[1:1 + VINDU]
                for k in range(0, min(3, len(kand)) + 1):
                    for valgt in combinations(range(len(kand)), k):
                        saker = [kand[i] for i in valgt]
                        # A split story's parts follow each other: on the same page, or the
                        # next part first on the next page (for one story per page, not the last).
                        her = [rest[0]] + saker
                        apne = [x for x in her if _nabo(x, 1) and not any(
                            y["kort"] == x["kort"] and y.get("_del") == _nabo(x, 1) for y in her)]
                        if len(apne) > (0 if siste else 1) or any(_nabo(x, -1) and not any(
                                y["kort"] == x["kort"] and y.get("_del") == _nabo(x, -1) for y in her) for x in saker):
                            continue
                        hopp = sum(i - j for j, i in enumerate(valgt))  # better-ranked stories passed over
                        for bands, brukt in self.ordne(saker, self.H - lv.hoyde, 1):
                            alle = [topp] + bands
                            h = lv.hoyde + brukt
                            if not siste:
                                # A box at the foot when the stories leave room for it.
                                for boks in bokser:
                                    ny = alle + [Band([(boks, f.spalter)])]
                                    hn = h + self.luft + boks.hoyde
                                    if hn <= self.H and self.sidekost(ny, hn) < self.sidekost(alle, h):
                                        alle, h = ny, hn
                                        break
                                c = self.sidekost(alle, h)
                            else:
                                tom = self.H - h
                                c = 0.0 if tom >= flyt_min else tom ** 2
                            c += HOPP_KOST * hopp
                            c += sum(40 + 0.5 * (b.hoyde - min(x.hoyde for x, _ in b.celler)) ** 2
                                     for b in bands if len(b.celler) == 2)
                            if beste is None or c < beste[0]:
                                beste = (c, alle, h, [rest[0]] + saker)
            if beste is None:
                if _nabo(rest[0], -1):
                    raise UtgaveFeil("en fortsatt del får ikke plass")  # half a story: not a plan
                # Too tall for a page in any shape: the whole story goes in short form instead.
                log.debug("%s: %s er for lang for én side og trykkes kort", f.navn, rest[0]["nr_etikett"])
                rest = [x for x in rest if x["kort"] != rest[0]["kort"]]
                if not rest:
                    raise UtgaveFeil("ingen hel sak får plass på en side")
                continue
            c, bands, h, brukt_saker = beste
            sider.append(bands)
            kost += c
            rest = [s_ for s_ in rest if not any(s_ is b for b in brukt_saker)]
            # The next part of a story whose part ends this page opens the next page.
            for b in brukt_saker:
                neste = next((x for x in rest if x["kort"] == b["kort"] and x.get("_del") == _nabo(b, 1)), None)
                if neste is not None:
                    rest.remove(neste)
                    rest.insert(0, neste)
            brukte = {b.id for band in bands for b, _ in band.celler}
            bokser = [b for b in bokser if b.id not in brukte]
            ledig = self.H - h
        return sider, ledig, kost

    def sidekost(self, bands: list[Band], brukt: float) -> float:
        """White space left on a page after the gaps between bands have taken
        what they can (mm²)."""
        tom = self.H - brukt
        strekk = min(tom, STREKK_MAKS * (len(bands) - 1))
        return (tom - strekk) ** 2

    # -- the flow of short stories
    def flyt(self, blokker: list[Blokk], ledig: float | None, bunn: float,
             valgfrie: list[Blokk] = ()) -> tuple[list[dict], float]:
        """Columns of blocks, page by page. ledig: room under the last full story
        (None: start on a fresh page). Each page takes as many blocks as fit in
        balanced columns; the last page keeps `bunn` mm for the closing band and
        may take some of the `valgfrie` boxes (in order) to fill up. Returns the
        pages ([{"start", "hode", "kol_topp", "kolonner", "kol_hoyde", "slutt"}])
        and the white space it leaves (mm²)."""
        f, H, hode = self.fmt, self.H, self.hode()
        sider, kost = [], 0.0
        start = None
        if ledig is not None and ledig >= FLYT_MIN + hode.hoyde:
            start = H - ledig + self.luft
        elif ledig is not None:
            kost += ledig ** 2  # empty room at the foot of the last full page
        rest = list(blokker)
        forste = True
        while True:
            topp = (start or 0.0) if forste else 0.0
            kol_topp = topp + (hode.hoyde + 3.0 if forste else 0.0)
            hoyde = H - kol_topp
            # Finish here if everything left fits over the closing band; fill up with optional boxes.
            plass_slutt = hoyde - bunn - self.luft
            beste = None
            for k in range(len(valgfrie), -1, -1):
                kol = balanser(rest + list(valgfrie[:k]), f.spalter, plass_slutt)
                if kol is not None:
                    tom = plass_slutt - max((kolonnehoyde(c) for c in kol), default=0.0)
                    if beste is None or tom < beste[0]:
                        beste = (tom, kol)
            if beste is not None and (rest or not forste):
                tom, kol = beste
                siste = {"start": topp, "hode": forste and bool(rest), "kol_topp": kol_topp, "kolonner": kol,
                         "kol_hoyde": plass_slutt, "slutt": True}
                if sider and not forste:
                    tom = jevn_ut(sider[-1], siste, f.spalter, tom)
                sider.append(siste)
                return sider, kost + tom ** 2
            # Otherwise as many as fit, balanced, and on to the next page.
            n = 0
            while n < len(rest) and balanser(rest[:n + 1], f.spalter, hoyde) is not None:
                n += 1
            if n == 0:
                if not forste:
                    raise UtgaveFeil(f"en blokk er høyere enn en hel side: {rest[0].id}")
            else:
                kol = balanser(rest[:n], f.spalter, hoyde)
                sider.append({"start": topp, "hode": forste, "kol_topp": kol_topp, "kolonner": kol,
                              "kol_hoyde": hoyde, "slutt": False})
                kost += sum(max(0.0, hoyde - kolonnehoyde(c) - KOLONNE_STREKK * max(len(c) - 1, 0)) ** 2
                            for c in kol) / f.spalter
                rest = rest[n:]
            forste = False
            start = None

    # -- the whole edition after the front page
    def plan(self, maal_sider: tuple[int, ...]) -> dict:
        """The pages after the front, for every number n of full stories (the
        best n by the site's order); among the plans with a wanted page count
        (fewest pages first) the least white space wins, and each extra full
        story weighs a little in its favour."""
        alle = []
        for s_ in self.innhold["fulle"][self.forste:]:
            # The fewest parts that each fit a page (the packer drops a story none of whose splits fit).
            for niva in (0, 1, 2):
                biter = deler(s_, niva)
                if all(min((b.hoyde for t in (True, False) for b in self.varianter(d, self.fmt.spalter, t)),
                           default=math.inf) <= self.H for d in biter):
                    break
            alle += biter
        bunn = max(b.hoyde for b in self.bunnblokker())
        kandidater = []
        for n_sider in range(0, max(maal_sider)):
            if n_sider:
                try:
                    bandsider, ledig, kost_f = self.pakk_fulle(alle, n_sider)
                except UtgaveFeil:
                    continue
            else:
                bandsider, ledig, kost_f = [], None, 0.0
            plassert = [b.sak for side in bandsider for band in side for b, _ in band.celler if b.sak]
            har = {(x["kort"], x.get("_del")) for x in plassert}
            if any((x["kort"], d) not in har for x in plassert for d in x.get("_rekke") or ()):
                continue  # half a split story: not a plan
            fulle = list({x["kort"]: x for x in plassert}.values())
            brukt = tuple(b.id for side in bandsider for band in side for b, _ in band.celler if b.type == "boks")
            valgfrie = [b for b in self.valgfrie() if b.id.removeprefix("smal:") not in brukt]
            for stor in (False, True):
                # The short stories a size up too, for pages they would leave half empty.
                blokker = self.flytblokker(self.innhold["fulle"][:self.forste] + fulle, brukt, stor)
                try:
                    flytsider, kost_flyt = self.flyt(blokker, ledig, bunn, valgfrie)
                except UtgaveFeil:
                    continue  # a block that fits no column (too tall, or a word too wide at this size)
                delt = bool(flytsider) and flytsider[0]["hode"] and flytsider[0]["start"] > 0
                totalt = 1 + len(bandsider) + len(flytsider) - delt
                kandidater.append((totalt, kost_f + kost_flyt, len(fulle), bandsider, flytsider))
        # Enough full stories: at least every one with a background article (the
        # site's top quarter of the meeting), counting the lead on the front.
        onsket = max(3, round(self.fmt.hele_andel * sum(1 for s_ in self.innhold["fulle"] if s_.get("har_artikkel"))))
        onsket -= self.forste
        onsket = min(onsket, max((k[2] for k in kandidater if k[0] in maal_sider or k[0] + 1 in maal_sider),
                                 default=0))
        log.debug("planer (sider, hvitt, hele): %s", [(k[0], round(k[1]), k[2]) for k in kandidater])
        for maal in maal_sider:
            # One page short of the count is made up with an "Om Tingposten" page (ekstra).
            for ekstra in (0, 1):
                passer = [k for k in kandidater if k[0] == maal - ekstra and k[2] >= onsket]
                if passer:
                    # White space in mm²; each extra full story is worth a little of it.
                    _, kost, n, bandsider, flytsider = min(passer, key=lambda k: (k[1] - FULL_VERDI * k[2], -k[2]))
                    log.info("%s: %d sider, %d hele saker etter forsiden, hvitt %.0f%s", self.fmt.navn, maal, n, kost,
                             " (med en side om Tingposten)" if ekstra else "")
                    return {"sider": maal, "bandsider": bandsider, "flytsider": flytsider, "kost": kost,
                            "om_side": bool(ekstra)}
        mulige = sorted({k[0] for k in kandidater})
        raise UtgaveFeil(f"fant ingen ombrekning med {maal_sider} sider (mulige sidetall: {mulige})")

    def om_side(self) -> Blokk:
        """The page that makes up a page count: how Tingposten is made and the
        sections of the editorial statement that read the same on paper."""
        f = self.fmt
        deler = erklaering_deler(OM_SIDE_DELER)
        tekst = Markup(f'<div class="band-boks om-tingposten" style="--boks-spalter: {min(2, f.spalter)}">'
                       f'<h2 class="om-tittel">Om Tingposten</h2>{self.maler.slik()}'
                       + "".join(str(self.maler.erklaeringsdel(d)) for d in deler) + str(self.maler.kolofon()) + "</div>")
        return self.maaling.maal([Blokk("om-tingposten", tekst, f.satsbredde, type="boks")])[0]


VINDU = 8           # stories after the page's lead that may fill the page
HOPP_KOST = 120.0   # cost (mm²) per better-ranked story a filler skips ahead of
FULL_VERDI = 300.0  # how much white space (mm²) one more full story is worth
FOLIO_LUFT = {"tabloid": 5.0, "a4": 4.5, "a5": 3.5}
KOLONNE_STREKK = 5.0  # mm a gap between short stories may grow to justify a column


def jevn_ut(forrige: dict, siste: dict, spalter: int, tom: float) -> float:
    """A nearly empty last page takes the last few blocks of the page before
    (in order), when that leaves the two pages with more even white space.
    Changes both pages in place; returns the last page's white space."""
    def ledig(kol, hoyde):
        return hoyde - max((kolonnehoyde(c) for c in kol), default=0.0)

    for_blokker = [b for c in forrige["kolonner"] for b in c]
    slutt_blokker = [b for c in siste["kolonner"] for b in c]
    beste = (max(ledig(forrige["kolonner"], forrige["kol_hoyde"]), tom), 0, None, None)
    for k in range(1, min(12, len(for_blokker))):
        a = balanser(for_blokker[:-k], spalter, forrige["kol_hoyde"])
        b = balanser(for_blokker[-k:] + slutt_blokker, spalter, siste["kol_hoyde"])
        if a is None or b is None:
            continue
        verst = max(ledig(a, forrige["kol_hoyde"]), ledig(b, siste["kol_hoyde"]))
        if verst < beste[0] - 1.0:
            beste = (verst, k, a, b)
    if beste[1]:
        forrige["kolonner"], siste["kolonner"] = beste[2], beste[3]
        return ledig(beste[3], siste["kol_hoyde"])
    return tom


def kolonnehoyde(k: list[Blokk]) -> float:
    return sum(b.hoyde for b in k) + GAP_KORT * max(len(k) - 1, 0)


def balanser(blokker: list[Blokk], spalter: int, hoyde: float) -> list[list[Blokk]] | None:
    """All blocks in order in `spalter` columns of at most `hoyde`, as even as
    possible (the lowest tallest column); None when they don't fit."""
    n = len(blokker)
    if n == 0:
        return [[] for _ in range(spalter)]
    from functools import lru_cache as _cache

    @_cache(maxsize=None)
    def best(i: int, k: int) -> tuple[float, tuple]:
        if i == n:
            return 0.0, ()
        if k == 0:
            return math.inf, ()
        ut = (math.inf, ())
        for j in range(i + 1, n + 1):
            h = kolonnehoyde(blokker[i:j])
            if h > hoyde:
                break
            rest, deling = best(j, k - 1)
            m = max(h, rest)
            if m < ut[0]:
                ut = (m, (j,) + deling)
        return ut

    maks, kutt = best(0, spalter)
    if maks == math.inf:
        return None
    kol, i = [], 0
    for j in kutt:
        kol.append(blokker[i:j])
        i = j
    return kol + [[] for _ in range(spalter - len(kol))]


# ------------------------------------------------------------- the pages ----

FOLIO = {"full": "Hovedsaker", "flyt": "Flere saker"}  # the running head's section name


def sett_inne(pakker: Pakker, plan: dict) -> list[Side]:
    """The pages after the front, as boxes and rules at explicit positions."""
    f, H, luft = pakker.fmt, pakker.H, pakker.luft
    W = f.satsbredde
    sider: list[Side] = []
    nr = 2
    if plan.get("om_side"):
        om = pakker.om_side()
        if om.hoyde > H:
            raise UtgaveFeil("siden om Tingposten er for lang")
        # The page's boxes spread down the whole page.
        html_om = Markup(str(om.html).replace('class="band-boks om-tingposten"',
                                              f'class="band-boks om-tingposten om-hel" data-h="{H:.1f}"', 1))
        sider.append(Side(nr, "Om Tingposten", [Rute(om.id, 0, 0, W, H, html_om, "boks", (om.id,))],
                          blokker=["om-tingposten"]))
        nr += 1
    bandsider = plan["bandsider"]
    for pi, bands in enumerate(bandsider):
        side = Side(nr, FOLIO["full"])
        brukt = sum(b.hoyde for b in bands) + luft * (len(bands) - 1)
        siste = pi == len(bandsider) - 1
        ekstra = 0.0 if siste or len(bands) < 2 else min(STREKK_MAKS, (H - brukt) / (len(bands) - 1))
        y = 0.0
        for bi, band in enumerate(bands):
            if bi:
                side.streker.append(Strek(0, y + (luft + ekstra) / 2, W, 0, 0.6))
                y += luft + ekstra
            x = 0.0
            for ci, (blokk, spalter) in enumerate(band.celler):
                w = f.spenn(spalter)
                if ci:
                    side.streker.append(Strek(x - f.mellom / 2, y, 0, band.hoyde, 0.35))
                side.ruter.append(Rute(blokk.id, x, y, w, band.hoyde, blokk.html, "celle", (blokk.id,)))
                if blokk.sak:
                    side.saker.append((blokk.sak, "full"))
                else:
                    side.blokker.append(blokk.id)
                x += w + f.mellom
            y += band.hoyde
        side.bunn = y  # where the flow may start
        sider.append(side)
        nr += 1
    hode = pakker.hode()
    bunn = pakker.bunnblokker()
    for fi, fs in enumerate(plan["flytsider"]):
        if fi == 0 and fs["start"] > 0 and sider:
            side = sider[-1]
        else:
            side = Side(nr, FOLIO["flyt"])
            sider.append(side)
            nr += 1
        if fs["hode"]:
            side.ruter.append(Rute("flythode", 0, fs["start"], W, hode.hoyde, hode.html, "hode", ("flythode",)))
        # Columns are justified: the gaps between blocks grow a little (at most KOLONNE_STREKK each).
        strekk = [max(0.0, min(KOLONNE_STREKK, (fs["kol_hoyde"] - kolonnehoyde(k) - 0.5) / (len(k) - 1)))
                  if len(k) > 1 else 0.0 for k in fs["kolonner"]]
        hoyest = max((kolonnehoyde(k) + e * max(len(k) - 1, 0) for k, e in zip(fs["kolonner"], strekk)), default=0.0)
        for ci, kol in enumerate(fs["kolonner"]):
            x = ci * (f.spalte + f.mellom)
            if ci and (kol or fs["kolonner"][ci - 1]):
                side.streker.append(Strek(x - f.mellom / 2, fs["kol_topp"], 0, hoyest, 0.35))
            ekstra = strekk[ci]
            innhold = Markup(f'<div class="kolonne" style="--ekstra: {max(ekstra, 0):.2f}mm">'
                             + "".join(str(b.html) for b in kol) + "</div>")
            side.ruter.append(Rute(f"kol-{side.nr}-{ci}", x, fs["kol_topp"], f.spalte, fs["kol_hoyde"], innhold,
                                   "kol", tuple(b.id for b in kol)))
            side.saker += [(b.sak, "kort") for b in kol if b.sak]
            side.blokker += [b.id for b in kol]
        if fs["slutt"]:
            hb = max(b.hoyde for b in bunn)
            y = H - hb
            side.streker.append(Strek(0, y - luft / 2 - 0.6, W, 0, 1.6))  # an Oxford rule: thick over thin
            side.streker.append(Strek(0, y - luft / 2 + 0.5, W, 0, 0.4))
            x = 0.0
            for bi, b in enumerate(bunn):
                if bi:
                    side.streker.append(Strek(x - f.mellom / 2, y, 0, hb, 0.35))
                side.ruter.append(Rute(b.id, x, y, b.bredde, hb, b.html, "bunn", (b.id,)))
                x += b.bredde + f.mellom
            side.blokker += [b.id for b in bunn]
    return sider


def front_former(fmt: Format) -> dict:
    """The front page's grid: columns for the lead (and the teasers under it),
    and the rail beside it (0 = the boxes go in the teaser row)."""
    return {"tabloid": {"lead": 4, "rail": 1, "teasere": 4, "slik": False},
            "a4": {"lead": 3, "rail": 0, "teasere": 2, "slik": False},
            # A5: the meeting in numbers goes inside (before Valg og fritak), the trial note in a strip.
            "a5": {"lead": 2, "rail": 0, "teasere": 1, "slik": False, "motetall_inne": True, "ombord_bred": True,
                   "teaser_kart": False}}[fmt.navn]


def sett_forside(pakker: Pakker, innhold: dict, sider: list[Side], komplett: Variant | None,
                 n_enheter: int | None = None) -> Side | None:
    """The front page: the site's masthead and the folio bar; the lead under a
    banner head (across the page where there is a rail beside the lead), its
    text in columns with as many whole units of its text (enheter: the
    article's paragraphs, or the card's fields) as fit and the jump inside, or
    the complete lead when it fits; teasers for the next stories
    with their page; and the boxes: what's in this edition, the meeting in
    numbers (in the rail, or a strip under the lead), how Tingposten is made
    (tabloid), a word about this being a trial. None when it doesn't fit."""
    f, maler, maaling = pakker.fmt, pakker.maler, pakker.maaling
    form = front_former(f)
    H, W, luft = f.satshoyde, f.satsbredde, pakker.luft
    m = innhold["mote"]
    side_for = {}
    for sd in sider:
        for s_, _ in sd.saker:
            if s_ is not None:
                side_for.setdefault(s_["kort"], sd.nr)
    lead_s = innhold["fulle"][0]
    teaser_saker = innhold["fulle"][1:1 + form["teasere"]]
    bl = f.spenn(form["lead"])

    hode = maaling.maal([Blokk("masthode", maler.masthode(dato_lang(m)), W, type="front"),
                         Blokk("datolinje", maler.datolinje(m, dato_lang(m)), W, type="front")])
    y0 = hode[0].hoyde + 1.5 + hode[1].hoyde + luft * 0.8
    teasere = maaling.maal([Blokk(f"teaser:{s_['kort']}", maler.teaser(s_, side_for.get(s_["kort"], 2),
                                                                    form.get("teaser_kart", True)),
                                  f.spalte, sak=s_, type="teaser") for s_ in teaser_saker])
    rader = innholdsrader(innhold, sider, komplett, f)
    if form["slik"]:
        rader.insert(len([r for r in rader if r["sak"]]), {"tekst": "Slik lages Tingposten", "side": 1, "sak": False})
    boks = {b.id: b for b in maaling.maal(
        [Blokk("i-utgaven", maler.innhold(rader), f.spalte, type="boks"),
         Blokk("motetall", maler.motetall(m, innhold["utfall"], innhold["venter"]), f.spalte, type="boks"),
         Blokk("slik", maler.slik(), f.spalte, type="boks"),
         Blokk("ombord", maler.ombord(UTGAVE_NR), f.spalte, type="boks"),
         Blokk("motetall-stripe", maler.motetall(m, innhold["utfall"], innhold["venter"], True), bl, type="boks"),
         Blokk("ombord-bred", maler.ombord(UTGAVE_NR), W, type="boks")])}
    ombord_bred = boks["ombord-bred"] if form.get("ombord_bred") else None
    if komplett:
        banner = None
        leads = maaling.maal([Blokk(f"lead:{lead_s['kort']}:{komplett.id}", maler.full(lead_s, komplett), bl, sak=lead_s,
                                    type="full")])
        leads[0].n_enheter = len(enheter(lead_s))
    else:
        banner = maaling.maal([Blokk(f"leadhode:{lead_s['kort']}", maler.lead_hode(lead_s), W, sak=lead_s,
                                     type="lead")])[0]
        mulige = range(maks_hopp(lead_s) + 1) if n_enheter is None else [n_enheter]
        leads = maaling.maal([Blokk(f"leadkropp:{lead_s['kort']}:{k}",
                                    maler.lead_kropp(lead_s, side_for.get(lead_s["kort"], 2), bl, k, form["lead"]),
                                    bl, sak=lead_s, type="lead") for k in mulige])
        for b, k in zip(leads, mulige):
            b.n_enheter = k
    y1 = y0 + (banner.hoyde + luft if banner else 0.0)  # under the banner: the lead's text and the rail

    def stabel(ider):
        celle = maaling.maal([Blokk("stabel:" + "+".join(ider), maler.stabel([boks[i].html for i in ider]),
                                    f.spalte, type="boks")])[0]
        celle.blokker = ider
        return celle

    def railhoyde(ider):
        return sum(boks[i].hoyde for i in ider) + luft * 0.7 * max(len(ider) - 1, 0)

    valg = []
    alternativ = [(stripe, kort) for stripe in ((False,) if form.get("motetall_inne") else (False, True))
                  for kort in ((True, False) if form["rail"] else (False,))]
    for stripe, kort_rail in alternativ:
        if form["rail"]:
            alle_bokser = ["i-utgaven"] + ([] if stripe else ["motetall"]) + (["slik"] if form["slik"] else []) + ["ombord"]
            if kort_rail:
                # The rail beside the lead only; the other boxes in the teaser row's fifth column.
                rail = ["i-utgaven"] + ([] if stripe else ["motetall"])
                rad = teasere + [stabel([i for i in alle_bokser if i not in rail])]
            else:
                rail = alle_bokser
                rad = teasere
                if railhoyde(rail) > H - y1:
                    continue
        else:
            # No rail: the boxes stacked in one cell of the row beside the teasers.
            rail = []
            ider = ["i-utgaven"] + ([] if stripe or form.get("motetall_inne") else ["motetall"]) + \
                ([] if ombord_bred else ["ombord"])
            rad = teasere[:f.spalter - 1] + [stabel(ider)]
        ht = max((b.hoyde for b in rad), default=0.0)
        hs = (boks["motetall-stripe"].hoyde + luft) if stripe else 0.0
        plass = H - y1 - luft - ht - hs
        plass -= (ombord_bred.hoyde + luft) if ombord_bred else 0.0
        passer = [b for b in leads if b.hoyde <= plass]
        if kort_rail:
            passer = [b for b in passer if railhoyde(rail) <= b.hoyde + hs]
        if passer:
            valg.append((passer[-1].n_enheter, kort_rail, not stripe, passer[-1], stripe, kort_rail, rail, rad, plass))
    if not valg:
        return None
    # The most of the lead's text on the front; then a rail that ends where the teasers start;
    # then the meeting box in the rail rather than a strip.
    *_, lead, stripe, kort_rail, rail, rad, plass = max(valg, key=lambda v: v[:3])

    hs = (boks["motetall-stripe"].hoyde + luft) if stripe else 0.0
    if banner and lead.n_enheter:
        # Room to spare: the lead's text a size up (still whole units, the same jump).
        stor = maaling.maal([Blokk(f"leadkropp-stor:{lead_s['kort']}:{lead.n_enheter}",
                                   maler.lead_kropp(lead_s, side_for.get(lead_s["kort"], 2), bl, lead.n_enheter,
                                                    form["lead"], True), bl, sak=lead_s, type="lead")])[0]
        rad_h = max((b.hoyde for b in rad), default=0.0)
        if stor.hoyde <= plass and stor.hoyde + 0.5 * rad_h <= plass + rad_h * 0.5 and \
                (not kort_rail or railhoyde(rail) <= stor.hoyde + hs):
            stor.n_enheter = lead.n_enheter
            lead = stor
    yt_teaser = y1 + lead.hoyde + luft + hs + ((ombord_bred.hoyde + luft) if ombord_bred else 0.0)
    store = maaling.maal([Blokk(f"teaser-stor:{s_['kort']}", maler.teaser(s_, side_for.get(s_["kort"], 2),
                                                                         form.get("teaser_kart", True), True),
                                f.spalte, sak=s_, type="teaser") for s_ in teaser_saker])
    if store and max(b.hoyde for b in store) + 2 <= H - yt_teaser and \
            all(max(b.hoyde for b in store) >= c.hoyde for c in rad if c not in teasere):
        rad = store + [c for c in rad if c not in teasere]

    side = Side(1, None)
    y = 0.0
    for b in hode:
        side.ruter.append(Rute(b.id, 0, y, W, b.hoyde, b.html, "front", (b.id,)))
        y += b.hoyde + 1.5
    if banner:
        side.ruter.append(Rute(banner.id, 0, y0, W, banner.hoyde, banner.html, "lead", (banner.id,)))
        if lead.n_enheter or form["rail"]:
            side.streker.append(Strek(0, y1 - luft / 2, W, 0, 0.6))
        else:
            y1 = y0 + banner.hoyde + 1.0  # no text on the front: the pointer follows the deck directly
    side.ruter.append(Rute(lead.id, 0, y1, bl, lead.hoyde, lead.html, "lead", (lead.id,)))
    side.saker.append((lead_s, "full" if komplett else "lead"))
    yt = y1 + lead.hoyde + luft
    if stripe:
        st = boks["motetall-stripe"]
        side.streker.append(Strek(0, yt - luft / 2, bl if rail else W, 0, 0.6))
        side.ruter.append(Rute(st.id, 0, yt, bl, st.hoyde, st.html, "boks", (st.id,)))
        side.blokker.append("motetall")
        yt += st.hoyde + luft
    if rail:
        xr = bl + f.mellom
        bunn = (yt - luft) if kort_rail else H
        side.streker.append(Strek(xr - f.mellom / 2, y1, 0, bunn - y1, 0.35))
        yr = y1
        for i, bid in enumerate(rail):
            b = boks[bid]
            if i == len(rail) - 1 and not kort_rail:
                yr = max(yr, H - b.hoyde)  # the last box sits at the foot of the rail
            side.ruter.append(Rute(b.id, xr, yr, f.spalte, b.hoyde, b.html, "boks", (b.id,)))
            side.blokker.append(b.id)
            yr += b.hoyde + luft * 0.7
    if ombord_bred:
        side.ruter.append(Rute(ombord_bred.id, 0, yt, W, ombord_bred.hoyde, ombord_bred.html, "boks",
                               (ombord_bred.id,)))
        side.blokker.append("ombord")
        yt += ombord_bred.hoyde + luft
    # The teaser row follows; its columns run to the foot of the page.
    ht = H - yt
    side.streker.append(Strek(0, yt - luft / 2, bl if rail and not kort_rail else W, 0, 0.6))
    for i, b in enumerate(rad):
        x = i * (f.spalte + f.mellom)
        if i:
            side.streker.append(Strek(x - f.mellom / 2, yt, 0, ht, 0.35))
        side.ruter.append(Rute(b.id, x, yt, f.spalte, ht, b.html, b.type, (b.id,)))
        if b.sak:
            side.saker.append((b.sak, "teaser"))
        else:
            side.blokker += getattr(b, "blokker", None) or [b.id]
    side.ledig = plass - lead.hoyde
    side.lead_enheter = lead.n_enheter
    return side


def maks_hopp(s: dict) -> int:
    """How many units of the lead's text (enheter) the front page may take:
    all of them when something else continues inside (Kort fortalt after an
    article, or figures), otherwise all but one, so the jump never leads to
    an empty page."""
    n = len(enheter(s))
    resten = s.get("artikkel") or [f for f in s.get("figurer") or [] if f.get("type") in FIGUR_MIN]
    return n if resten else max(n - 1, 0)


def innholdsrader(innhold: dict, sider: list[Side], komplett, fmt: Format) -> list[dict]:
    """"I denne utgaven": the full stories with their page, then the sections.
    Before the inside pages are made (sider empty): rows of the same size, to measure the box."""
    maks = {"tabloid": 6, "a4": 5, "a5": 3}[fmt.navn]
    if not sider:
        return [{"tekst": s_["overskrift"], "side": 12, "sak": True} for s_ in innhold["fulle"][1:maks + 1]] + \
            [{"tekst": t, "side": "12–15", "sak": False} for t in
             ("Flere saker fra møtet", "Valg og fritak", "Slik lages Tingposten", "Rettelser og utgiver")]
    rader, korte, valg, slik, slutt = [], [], None, None, None
    for sd in sider:
        for s_, typ in sd.saker:
            if typ == "full" and s_ is not None and not any(r.get("kort") == s_["kort"] for r in rader):
                rader.append({"tekst": s_["overskrift"], "side": sd.nr, "sak": True, "kort": s_["kort"]})
            elif typ == "kort":
                korte.append(sd.nr)
        blokker = sd.blokker
        if "valg" in blokker:
            valg = sd.nr
        if "slik" in blokker:
            slik = sd.nr
        if "kolofon" in blokker:
            slutt = sd.nr
    if len(rader) > maks:
        rader = rader[:maks]
    if korte:
        a, b = min(korte), max(korte)
        rader.append({"tekst": "Flere saker fra møtet", "side": f"{a}–{b}" if a != b else a, "sak": False})
    if valg:
        rader.append({"tekst": "Valg og fritak", "side": valg, "sak": False})
    if slik:
        rader.append({"tekst": "Slik lages Tingposten", "side": slik, "sak": False})
    if slutt:
        rader.append({"tekst": "Rettelser og utgiver", "side": slutt, "sak": False})
    return rader


def front_lead_former(s: dict, fmt: Format) -> list[Variant]:
    """The lead complete on the front: its column count, with the big headline."""
    k = front_former(fmt)["lead"]
    figs = [f_ for f_ in s.get("figurer") or [] if f_.get("type") in FIGUR_MIN]
    ut = []
    for tekst, side in ((k, 0), (k - 1, 1)):
        plass = fmt.spenn(side) if side else fmt.spenn(k)
        if any(plass < figur_min(fmt, f_["type"]) for f_ in figs) or (side and not (figs or fakta(s))):
            continue
        ut.append(Variant(k, tekst, side, "--h-lead", fmt.spenn(k), fmt.spenn(side) if side else 0.0,
                          tuple((f_, min(figur_maks(fmt, f_["type"]), plass)) for f_ in figs)))
    return ut


# ----------------------------------------------------------------- build ----

def lag(mote: str, format_: str, til: Path, *, saker: list[dict] | None = None, data_dir: Path | None = None,
        forhandsvisning: Path | None = None, dpi: int = 110, arbeid: Path | None = None) -> dict:
    """Make the printed edition of one meeting ("571424-2026-09-23") as a PDF.

    Measures, packs, renders and checks; tries again (up to FORSOK times) with
    the measured height of any block that spilled raised by how much it
    spilled. Fails rather than print a page where something doesn't fit.
    forhandsvisning: also write a PNG of every page there."""
    if format_ not in FORMATER:
        raise UtgaveFeil(f"ukjent format {format_!r} (kjenner {', '.join(FORMATER)})")
    fmt = FORMATER[format_]
    if saker is None:
        saker = bygg.load(data_dir or config.DATA_DIR)
    innhold = velg(saker, mote)
    if not innhold["fulle"]:
        raise UtgaveFeil(f"ingen saker med sammendrag fra {mote}: ingenting å trykke")
    maler = Maler(fmt)
    fontcss = fonter()
    til = Path(til)
    if arbeid is None:
        # Pages for measuring and checking (they load the fonts from file://); the last is the edition as printed.
        arbeid = Path(tempfile.gettempdir()) / "tingposten-utgave" / til.stem
        for gammel in arbeid.glob("side-*.html"):
            gammel.unlink()
    with Nettleser() as nett:
        maaling = Maaling(maler, nett, innhold["mote"], fontcss, arbeid)
        for forsok in range(FORSOK):
            sider = ombrekk(fmt, maler, maaling, innhold)
            nett.last(maler.dokument(innhold["mote"], fontcss, sider=sider), arbeid)
            feil = nett.sjekk()
            if not feil:
                break
            log.info("forsøk %d: %d bokser renner over, prøver igjen: %s", forsok + 1, len(feil),
                     ", ".join(f"{e['boks']} ({e['feil']})" for e in feil[:6]))
            ruter = {r.id: r for sd in sider for r in sd.ruter}
            for e in feil:
                if e["feil"] == "ord for bredt":
                    raise UtgaveFeil(f"et ord er bredere enn spalten i {e['boks']} ({e.get('klasse')}: "
                                     f"{e.get('tekst')!r}, {e['over']:.1f} px)")
                rute = ruter.get(e["boks"])
                bid = rute.blokker[-1] if rute and rute.blokker else e["boks"]
                maaling.ekstra[bid] = maaling.ekstra.get(bid, 0.0) + e["over"] / PX_PER_MM + 0.5
        else:
            raise UtgaveFeil(f"fikk ikke alt til å passe etter {FORSOK} forsøk: {feil[:5]}")
        nett.pdf(til, fmt)
        html_fil = nett.sist
        # The same pages on a newsprint tone, for reading on screen.
        skjerm = skjerm_sti(til)
        nett.last(skjermkopi(maler.dokument(innhold["mote"], fontcss, sider=sider, papir=SKJERM_PAPIR)), arbeid)
        if nett.sjekk():
            raise UtgaveFeil("skjermkopien ble ikke lik trykkfilen")
        nett.pdf(skjerm, fmt)
    sideboks(til, fmt)
    sideboks(skjerm, fmt)
    info = sjekk_pdf(til, fmt, len(sider))
    sjekk_pdf(skjerm, fmt, len(sider))
    info["html"] = str(html_fil)
    info["skjerm"] = str(skjerm)
    if fmt.navn == "a5":
        info["hefte"] = str(hefte_sti(til))
        info["ark"] = hefte(til, hefte_sti(til))
        info["enkeltsidig"] = [str(f) for f in enkeltsidig(hefte_sti(til))]
    if forhandsvisning:
        info["png"] = [str(p) for p in png(til, Path(forhandsvisning), dpi)]
        if info.get("hefte"):
            info["png"] += [str(p) for p in png(Path(info["hefte"]), Path(forhandsvisning), dpi)]
        info["png"] += [str(p) for p in png(skjerm, Path(forhandsvisning), dpi)]
    info.update({
        "format": fmt.navn, "pdf": str(til), "sider": len(sider),
        "hele": list(dict.fromkeys(s["nr_etikett"] for sd in sider for s, t in sd.saker if t == "full")),
        "korte": [s["nr_etikett"] for sd in sider for s, t in sd.saker if t == "kort"],
        "valg": [s["nr_etikett"] for s in innhold["valg"]],
        "uten_sammendrag": [s["nr_etikett"] for s in innhold["liste"]],
        "venter": [s["nr_etikett"] for s in innhold["venter"]],
        "innhold": [{"side": sd.nr, "saker": [(s["nr_etikett"], t) for s, t in sd.saker],
                     "blokker": sd.blokker} for sd in sider],
    })
    return info


FORSOK = 5


def ombrekk(fmt: Format, maler: Maler, maaling: Maaling, innhold: dict) -> list[Side]:
    """Every page, front first. The lead goes complete on the front when one of
    its shapes fits there with the teasers and the boxes; otherwise it is a
    large teaser and its full story opens the inside pages."""
    lead = innhold["fulle"][0]
    beste = None
    planer: dict[int, tuple] = {}
    for komplett in front_lead_former(lead, fmt):
        if 1 not in planer:
            pakker = Pakker(fmt, maler, maaling, innhold, 1)
            pakker.forhandsmaal()
            plan = pakker.plan(fmt.sider)
            planer[1] = (pakker, plan, sett_inne(pakker, plan))
        pakker, plan, inne = planer[1]
        front = sett_forside(pakker, innhold, inne, komplett)
        if front is not None:
            nokkel = (len(inne), False, front.ledig)
            if beste is None or nokkel < beste[0]:
                beste = (nokkel, [front, *inne])
    hopp = hoppsider(fmt, maler, maaling, innhold)
    if hopp is not None:
        # Fewest pages first; then a complete lead; then the fuller front page.
        nokkel = (len(hopp) - 1, True, hopp[0].ledig)
        if beste is None or nokkel < beste[0]:
            beste = (nokkel, hopp)
    if beste is None:
        raise UtgaveFeil("forsiden får ikke plass til hovedsaken og henvisningene")
    sider = beste[1]
    if len(sider) % 4:
        raise UtgaveFeil(f"{len(sider)} sider er ikke et multiplum av 4")
    return sider


def hoppsider(fmt: Format, maler: Maler, maaling: Maaling, innhold: dict) -> list[Side] | None:
    """The lead starts on the front page and jumps: the front takes its head,
    deck, facts and as many whole units of its text as fit (the article's
    paragraphs, or the card's fields), ending with "Fortsettes på side N"; the
    story goes on inside under "Fra side 1" with the rest of the text, Kort
    fortalt (after an article), the figures and the sources. Nothing is
    printed twice and no sentence is split. The number of units is chosen
    first (the front page's other blocks don't depend on the inside pages),
    then the inside is packed with the rest of the story first."""
    lead = innhold["fulle"][0]
    pakker = Pakker(fmt, maler, maaling, innhold, 0)
    pakker.forhandsmaal()
    forhand = sett_forside(pakker, innhold, [], None)
    k = forhand.lead_enheter if forhand is not None else 0
    while k >= 0:
        if k:
            resten = {**lead, "_fra": k}
            vist = lead_figurer(lead, fmt.spenn(front_former(fmt)["lead"]), fmt)
            if vist:  # figures printed on the front aren't repeated; their sources are
                resten["figurer"] = [f for f in lead.get("figurer") or [] if f not in [x for x, _ in vist]]
                resten["_ekstra_kilder"] = [kk for f, _ in vist for kk in f.get("kilder") or []]
            inn = {**innhold, "fulle": [resten] + innhold["fulle"][1:]}
        else:
            inn = innhold
        pk = Pakker(fmt, maler, maaling, inn, 0)
        pk.forhandsmaal()
        try:
            plan = pk.plan(fmt.sider)
        except UtgaveFeil:
            k -= 1
            continue
        inne = sett_inne(pk, plan)
        plassert = any(s_["kort"] == lead["kort"] for sd in inne for s_, t in sd.saker if t == "full")
        if k and not plassert:
            k = 0  # the rest of the story didn't find room inside: the front shows head and deck only
            continue
        front = sett_forside(pk, innhold, inne, None, n_enheter=k)
        if front is not None:
            return [front, *inne]
        k -= 1
    return None


PT_PER_MM = 72 / 25.4


def sideboks(sti: Path, fmt: Format) -> None:
    """Exact page boxes: Chromium rounds the page to whole CSS pixels (280 mm
    becomes 280.08 mm). MediaBox and TrimBox are set to the format's size in
    points; what is cut off is a fraction of a point of white margin."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject

    leser = PdfReader(str(sti))
    skriver = PdfWriter(clone_from=leser)
    b, h = fmt.bredde * PT_PER_MM, fmt.hoyde * PT_PER_MM
    for side in skriver.pages:
        topp = float(side.mediabox.top)
        boks = RectangleObject([0, topp - h, b, topp])
        side.mediabox = boks
        side.cropbox = boks
        side.trimbox = boks
    tmp = sti.with_suffix(".tmp.pdf")
    with open(tmp, "wb") as fil:
        skriver.write(fil)
    tmp.replace(sti)


def sjekk_pdf(sti: Path, fmt: Format, sider: int) -> dict:
    """The PDF as printed: page count and size as planned, every font embedded, none as Type 3."""
    import pymupdf

    with pymupdf.open(sti) as doc:
        if doc.page_count != sider:
            raise UtgaveFeil(f"PDF-en har {doc.page_count} sider, ventet {sider}")
        r = doc[0].rect
        mm = (round(r.width * 25.4 / 72, 1), round(r.height * 25.4 / 72, 1))
        if abs(mm[0] - fmt.bredde) > 0.05 or abs(mm[1] - fmt.hoyde) > 0.05:
            raise UtgaveFeil(f"sidestørrelse {mm} mm, ventet {fmt.bredde} × {fmt.hoyde}")
        fonter_ = {}
        for side in doc:
            for f in side.get_fonts():
                fonter_[f[3]] = {"type": f[2], "innebygd": f[1] != "n/a" and bool(f[1])}
    type3 = sorted(n for n, f in fonter_.items() if f["type"] == "Type3")
    if type3:
        raise UtgaveFeil(f"Type 3-fonter i PDF-en: {type3}")
    # Only the edition's own fonts: anything else is a fallback for a glyph they lack.
    fremmede = sorted(n for n in fonter_ if not re.search(r"\+(TP|UnifrakturMaguntia)", n))
    if fremmede:
        raise UtgaveFeil(f"tegn uten egen font (reservefont brukt): {fremmede}")
    return {"fonter": sorted(fonter_), "sidestorrelse_mm": mm}


def png(sti: Path, mappe: Path, dpi: int = 110) -> list[Path]:
    """A PNG of every page (pymupdf), for looking at the edition before printing it."""
    import pymupdf

    mappe.mkdir(parents=True, exist_ok=True)
    ut = []
    with pymupdf.open(sti) as doc:
        for i, side in enumerate(doc, start=1):
            fil = mappe / f"{sti.stem}-s{i:02d}.png"
            side.get_pixmap(dpi=dpi).save(fil)
            ut.append(fil)
    return ut


# ------------------------------------------------------------- A5 booklet ----

def hefte_rekkefolge(n: int) -> list[tuple[int, int]]:
    """The A5 pages (1-based) on each side of the A4 sheets, in print order:
    sheet k (from 0) has pages n−2k and 2k+1 on its front, 2k+2 and n−2k−1 on
    its back, left to right. Printed double-sided, flipped on the short edge,
    the stack folded in the middle reads 1, 2, … n."""
    if n <= 0 or n % 4:
        raise ValueError(f"et hefte trenger et antall sider delelig med 4, ikke {n}")
    ut = []
    for k in range(n // 4):
        ut.append((n - 2 * k, 2 * k + 1))
        ut.append((2 * k + 2, n - 2 * k - 1))
    return ut


def hefte(kilde: Path, til: Path) -> int:
    """The A5 edition imposed for a home printer: A4 landscape sheets with two
    A5 pages side by side in booklet order (hefte_rekkefolge). Returns the
    number of A4 sheets."""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation

    leser = PdfReader(str(kilde))
    n = len(leser.pages)
    a5 = leser.pages[0].mediabox
    b, h = float(a5.width), float(a5.height)
    # Exactly A4 landscape, so "actual size" prints without scaling; the two pages meet at the middle.
    ark_b, ark_h = 297 * PT_PER_MM, 210 * PT_PER_MM
    x0, y0 = ark_b / 2 - b, (ark_h - h) / 2
    skriver = PdfWriter()
    for venstre, hoyre in hefte_rekkefolge(n):
        ark = PageObject.create_blank_page(width=ark_b, height=ark_h)
        for nr, x in ((venstre, x0), (hoyre, ark_b / 2)):
            side = leser.pages[nr - 1]
            ark.merge_transformed_page(side, Transformation().translate(tx=x - float(side.mediabox.left),
                                                                         ty=y0 - float(side.mediabox.bottom)))
        skriver.add_page(ark)
    skriver.add_metadata({"/Title": f"Tingposten, prøveutgave nr. {UTGAVE_NR} (A5-hefte for utskrift)",
                          "/Creator": "Tingposten (utgave.py)"})
    til.parent.mkdir(parents=True, exist_ok=True)
    with open(til, "wb") as fil:
        skriver.write(fil)
    return n // 4


def enkeltsidig(hefte_pdf: Path) -> list[Path]:
    """The imposed booklet split for a printer without duplex: the sheets' fronts
    in one file and their backs in another, both in sheet order (sheet 1, the one
    with the front page, first), plus the backs turned 180° for printers that feed
    the stack the other way round. Print the fronts, put the stack back with sheet
    1 fed first, print the backs. Returns the three paths."""
    from pypdf import PdfReader, PdfWriter

    leser = PdfReader(str(hefte_pdf))
    stier = [hefte_pdf.with_name(f"{hefte_pdf.stem}-{navn}{hefte_pdf.suffix}")
             for navn in ("1-forsider", "2-baksider", "2-baksider-snudd")]
    for sti, start, snu in zip(stier, (0, 1, 1), (False, False, True)):
        skriver = PdfWriter()
        for i in range(start, len(leser.pages), 2):
            side = skriver.add_page(leser.pages[i])
            if snu:
                side.rotate(180)
        skriver.add_metadata({"/Title": f"Tingposten, prøveutgave nr. {UTGAVE_NR} (A5-hefte, {sti.stem.split('-hefte-')[-1]})",
                              "/Creator": "Tingposten (utgave.py)"})
        with open(sti, "wb") as fil:
            skriver.write(fil)
    return stier


def skjerm_sti(til: Path) -> Path:
    """tingposten-2026-09-23-tabloid.pdf → tingposten-2026-09-23-tabloid-skjerm.pdf"""
    return til.with_name(f"{til.stem}-skjerm{til.suffix}")


def skjermkopi(html_text: str) -> str:
    """The figures' paper-coloured strokes and halos on the screen copy's paper tone."""
    hvit = FARGER["--tp-flate"]
    return html_text.replace(f'stroke="{hvit}"', f'stroke="{SKJERM_PAPIR}"').replace(f'fill="{hvit}"',
                                                                                     f'fill="{SKJERM_PAPIR}"')


def hefte_sti(til: Path) -> Path:
    """tingposten-2026-09-23-a5.pdf → tingposten-2026-09-23-a5-hefte.pdf"""
    return til.with_name(f"{til.stem}-hefte{til.suffix}")
