"""Bygg: the public static site, from edited cards, metadata and the score.

Reads data/bergen/: agendapunkter/ (metadata), redigert/ (cards after the
fact-checker and the delete-only editor) and pdf/ (for the score). Writes
plain HTML to site/: no JavaScript, no external assets, relative links only,
so the same files work under v3x4t0r.github.io/tingposten/ and at the root
of tingposten.no. Source links go to Bergen kommune's own PDFs.

Laid out like a newspaper: front pages with a lead story, a few secondary
stories and headline lists; one page per sak with the full card. Every
sentence shown is from the edited card; the dek is its first sentence.
"""

import json
import logging
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import config, grafikk, pdf, score
from .status import IKKE_AVGJORT

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
FIELDS = (
    ("hva_endres", "Hva endres"),
    ("hvem_pavirkes", "Hvem påvirkes"),
    ("hvorfor", "Hvorfor"),
    ("status", "Status"),
    ("nar_og_hvor", "Når og hvor"),
)
MONTHS = ("januar", "februar", "mars", "april", "mai", "juni", "juli",
          "august", "september", "oktober", "november", "desember")
WEEKDAYS = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
SECONDARY = 4       # stories in the grid under the lead
RELATED = 6         # "Flere saker fra samme møte" on a sak page
# Short organ names for kickers and breadcrumbs.
ORGAN_KORT = {"571424": "Bystyret", "900101": "Byrådet (innstilling)", "900102": "Byrådet"}
FRONT_FIGURE = ("kart", "stemmer")  # front pages show at most one figure, in this preference
VIEWS = {           # front-page views: path, days back (None = this year), label
    "": (14, "Siste 14 dager"),
    "30-dager/": (30, "Siste 30 dager"),
    "i-ar/": (None, "I år"),
}
# Words that end with a full stop without ending the sentence.
ABBREVIATIONS = {"ca", "mill", "mrd", "nr", "kr", "jf", "pkt", "bl.a", "f.eks", "dvs", "mv", "osv",
                 "m.fl", "m.m", "inkl", "ekskl", "evt", "St", "gnr", "bnr", "kap", "jfr", "tlf", "Fv", "fv"}

# Published explanation of each score signal (plan: "vekter publisert").
SIGNALS = {
    "belop": ("Beløp", "Penger vedtaket selv binder, på en logaritmisk skala fra 1 million til 1 milliard kroner."),
    "rekkevidde": ("Rekkevidde", "Hvor stor del av byen saken gjelder: én eiendom, én bydel eller hele byen."),
    "juridisk_vekt": ("Juridisk vekt", "Planer, budsjett og forskrifter binder i mange år. Valg til verv gjør det ikke."),
    "uenighet": ("Uenighet", "Hvor jevn avstemningen var. 36–31 teller mer enn 60–5."),
    "innstilling_falt": ("Innstillingen falt", "Flertallet stemte ned hele eller deler av forslaget som ble lagt fram."),
    "varighet": ("Varighet", "Langvarige vedtak som planer, strategier, avtaler og salg, mot engangstiltak."),
    "endelighet": ("Endelighet", "Endelig vedtak, eller bare en innstilling til et annet organ."),
    "hverdag": ("Hverdag", "Om en vanlig innbygger merker vedtaket i hverdagen. Vurderes av en språkmodell."),
    "angar": ("Angår mange", "Hvor mange saken til slutt angår, direkte eller indirekte. Vurderes av en språkmodell."),
    "konkret": ("Konkret endring", "Om noe konkret endrer seg for innbyggerne nå. Vurderes av en språkmodell."),
    "debatt": ("Debatt", "Hvor omstridt saken var i bystyret: forslag som falt og antall avstemninger."),
}


def build(site_dir: Path = config.SITE_DIR, data_dir: Path = config.DATA_DIR, idag: date | None = None) -> dict:
    idag = idag or date.today()
    saker = load(data_dir)
    _guard(site_dir)
    tmp = site_dir.parent / f".{site_dir.name}.bygg"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    env = _env()
    pages = 0

    def write(rel: str, template: str, **ctx) -> None:
        nonlocal pages
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        depth = len(Path(rel).parts) - 1
        path.write_text(env.get_template(template).render(root="../" * depth, **ctx))
        pages += 1

    moter = _meetings(saker)
    by_meeting = {m["slug"]: m for m in moter}
    write("index.html", "landing.html", side="landing")
    for slug, (days, label) in VIEWS.items():
        window = _window(saker, idag, days)
        front = front_page(window)
        write(f"bergen/{slug}index.html", "forside.html", side="forside", visning=slug, periode=label,
              idag_tekst=nb_weekday_date(idag), **front)
        write(f"bergen/{slug}alle/index.html", "alle.html", side="forside", visning=slug, periode=label,
              moter=_meetings(window), antall=len(window))
    write("bergen/moter/index.html", "moter.html", side="moter", moter=moter)
    for m in moter:
        write(f"bergen/mote/{m['slug']}/index.html", "mote.html", side="mote", mote=m)
    for s_ in saker:
        meeting = by_meeting[s_["mote"]]
        write(s_["side"] + "index.html", "sak.html", side="sak", s=s_, mote=meeting,
              relaterte=related(s_, meeting))
    write("om/index.html", "om.html", side="om", signaler=_weights(), terskel=_terskel())
    rss = [s_ for s_ in _window(saker, idag, 30)]
    rss.sort(key=lambda x: (x["dato"], x["score"]), reverse=True)
    (tmp / "bergen" / "rss.xml").write_text(env.get_template("rss.xml").render(
        saker=rss, site_url=_site_url(), bygget=_rfc822(idag)))

    (tmp / "static").mkdir()
    shutil.copy(HERE / "static" / "style.css", tmp / "static" / "style.css")
    (tmp / ".nojekyll").write_text("")
    if config.CNAME:
        (tmp / "CNAME").write_text(config.CNAME + "\n")

    if site_dir.exists():
        shutil.rmtree(site_dir)
    tmp.rename(site_dir)
    stats = {"sider": pages, "saker": len(saker), "fremhevet": sum(s["fremhevet"] for s in saker), "moter": len(moter)}
    log.info("built %s: %s", site_dir, stats)
    return stats


def load(data_dir: Path) -> list[dict]:
    """Every decided sak from the Tier 1 organs, with its card and score."""
    parsers = _Parsers()
    saker = []
    for uid in config.TIER1:
        path = data_dir / "agendapunkter" / f"{uid}.json"
        # A recommendation is not a decision; the case appears when the
        # deciding organ has decided it.
        if uid in config.INNSTILLENDE or not path.exists():
            continue
        items = [i for i in json.loads(path.read_text()).values()
                 if i.get("vedtakFilId") and i.get("moteDato") and not pdf.is_noise(i)]
        by_meeting = defaultdict(list)
        for i in items:
            by_meeting[i["moteDato"]].append(i)
        for meeting in by_meeting.values():
            # Case numbers are only unique within one organ's meeting.
            ranked = {r["nr"]: r for r in score.rank(meeting, data_dir / "pdf", data_dir=data_dir,
                                                       parse_protokoll=parsers.protokoll,
                                                       parse_saksframlegg=parsers.saksframlegg)}
            for item in meeting:
                saker.append(_sak(item, ranked.get(_nr(item), {}), data_dir, parsers))
    return saker


def _nr(item: dict) -> str:
    return item.get("referansenr") or pdf.tag(item)


class _Parsers:
    """Parse each PDF once per build: the score and the figures both need it."""

    def __init__(self):
        self._cache = {}

    def protokoll(self, path: Path) -> dict:
        from .protokoll import parse_pdf
        return self._get(parse_pdf, path)

    def saksframlegg(self, path: Path) -> dict:
        from .saksframlegg import parse_pdf
        return self._get(parse_pdf, path)

    def _get(self, fn, path: Path) -> dict:
        key = (fn.__module__, str(path))
        if key not in self._cache:
            try:
                self._cache[key] = fn(path) if path.exists() else {}
            except Exception:
                log.exception("could not parse %s", path)
                self._cache[key] = {}
        return self._cache[key]


def _figures(item: dict, data_dir: Path, parsers: "_Parsers") -> list[dict]:
    pdf_dir = data_dir / "pdf"
    p = parsers.protokoll(pdf_dir / pdf.filename(item, "protokoll"))
    s = parsers.saksframlegg(pdf_dir / pdf.filename(item, "saksframlegg"))
    if not p and not s:
        return []
    try:
        figs = grafikk.figurer(item, p, s)
    except Exception:
        log.exception("figures failed for %s", pdf.key(item))
        return []
    # The same map can appear for two saker on one front page: make SVG ids unique.
    suffix = re.sub(r"[^A-Za-z0-9-]", "-", f"{item.get('utvalgId', '')}-{pdf.tag(item)}")
    return [{**f, "svg": re.sub(r"\b(tpf-[0-9a-f]{8})\b", rf"\1-{suffix}", f["svg"]), "type": _figure_type(f)}
            for f in figs]


def _figure_type(fig: dict) -> str:
    kilde = fig.get("kilde", "")
    if kilde.startswith("Kart"):
        return "kart"
    if kilde.startswith("Protokollen, votering"):
        return "stemmer"
    return "belop"


def front_figure(sak: dict) -> dict | None:
    for kind in FRONT_FIGURE:
        for f in sak["figurer"]:
            if f.get("type") == kind:
                return f
    return None


def _sak(item: dict, ranked: dict, data_dir: Path, parsers: "_Parsers | None" = None) -> dict:
    card_path = data_dir / "redigert" / f"{pdf.key(item)}.json"
    card = json.loads(card_path.read_text()) if card_path.exists() else None
    usable = bool(card) and not card.get("avvist")
    dato = date.fromisoformat(item["moteDato"])
    uid = item["utvalgId"]
    slug = f"{uid}-{item['moteDato']}"
    anchor = f"sak-{pdf.tag(item)}"
    status = ranked.get("status") or {}
    felt = _fields(item, card) if usable else []
    dek, dek_kilder, felt = _dek(felt)
    side = f"bergen/sak/{pdf.key(item)}/"
    sak = {
        "nr": _nr(item),
        "tittel": item.get("tittel", ""),
        "overskrift": (card.get("overskrift") if usable else "") or item.get("tittel", ""),
        "organ": config.TIER1.get(uid, item.get("utvalgsNavn", uid)),
        "organ_kort": ORGAN_KORT.get(uid, config.TIER1.get(uid, uid)),
        "utvalg": uid,
        "dato": dato,
        "dato_tekst": nb_date(dato),
        "seq": item.get("seq") or 0,
        "score": ranked.get("score", 0),
        "grunner": list(ranked.get("grunner", []))[:3],
        "status_tekst": status.get("tekst", ""),
        "status_klasse": _status_class(status),
        "modell": _model_line(ranked.get("modell")),
        "felt": felt,
        "dek": dek,
        "dek_kilder": dek_kilder,
        "har_sammendrag": bool(dek or felt),
        "figurer": (_figures(item, data_dir, parsers or _Parsers()) or list(card.get("figurer", []))) if usable else [],
        "pdf": {k: u for k, _, _ in pdf.KINDS if (u := pdf.url(item, k))},
        "mote": slug,
        "anker": anchor,
        "side": side,
        "lenke": side,
    }
    sak["fremhevet"] = sak["score"] >= _terskel()
    sak["meld_feil"] = meld_feil(sak["nr"], sak["tittel"], _site_url() + sak["lenke"])
    return sak


def _fields(item: dict, card: dict) -> list[dict]:
    felt = card.get("felt") or {}
    out = []
    for key, label in FIELDS:
        text = (felt.get(key) or "").strip()
        if not text:
            continue
        sources = []
        for k in card.get("kilder", []):
            base = pdf.url(item, k.get("dok", "")) if k.get("dok") in ("saksframlegg", "protokoll") else None
            if k.get("felt") == key and base and isinstance(k.get("side"), int):
                sources.append({"dok": k["dok"], "side": k["side"], "href": f"{base}#page={k['side']}"})
        out.append({"key": key, "label": label, "tekst": text, "kilder": _dedupe(sources)})
    return out


def _dedupe(sources: list[dict]) -> list[dict]:
    seen, out = set(), []
    for s in sources:
        if (s["dok"], s["side"]) not in seen:
            seen.add((s["dok"], s["side"]))
            out.append(s)
    return out


def _dek(felt: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """The first sentence of "Hva endres" as the dek; the body keeps the rest.

    The text is the edited, fact-checked card: it is split, never rewritten.
    """
    hva = next((f for f in felt if f["key"] == "hva_endres"), None)
    if not hva:
        return "", [], felt
    first, rest = split_first_sentence(hva["tekst"])
    if rest:
        body = [{**f, "tekst": rest} if f is hva else f for f in felt]
        return first, [], body
    # One sentence only: it all moves to the dek, and so do its sources.
    return first, hva["kilder"], [f for f in felt if f is not hva]


def split_first_sentence(text: str) -> tuple[str, str]:
    text = " ".join(text.split())
    i = 0
    while True:
        i = text.find(". ", i)
        if i < 0:
            return text, ""
        word = text[:i].rsplit(" ", 1)[-1]
        nxt = text[i + 2:i + 3]
        if word not in ABBREVIATIONS and not word[-1:].isdigit() and nxt.isupper():
            return text[:i + 1], text[i + 2:]
        i += 2


def front_page(window: list[dict]) -> dict:
    """Lead, secondary stories, headline list and the rest, newest first on ties.

    The lead and the grid take fremhevede saker that have a summary; a
    fremhevet sak whose card isn't ready goes to the headline list instead of
    leading with "Sammendrag kommer".
    """
    ordered = sorted(window, key=lambda s: (-s["score"], -s["dato"].toordinal(), s["seq"]))
    fremhevet = [s for s in ordered if s["fremhevet"]]
    stories = [s for s in fremhevet if s["har_sammendrag"]]
    if not stories:
        stories = [s for s in ordered if s["har_sammendrag"]][:1] or ordered[:1]
    shown = stories[:1 + SECONDARY]
    flere = [s for s in fremhevet if s not in shown]
    andre = len(window) - len(shown) - len(flere)
    return {
        "lead": shown[0] if shown else None,
        "sekundaer": shown[1:],
        "flere": flere,
        "andre": andre,
        "antall": len(window),
    }


def related(sak: dict, meeting: dict) -> list[dict]:
    others = [s for s in meeting["saker"] if s is not sak]
    return sorted(others, key=lambda s: (-s["score"], s["seq"]))[:RELATED]


def _status_class(status: dict) -> str:
    kode, tekst = status.get("kode", ""), status.get("tekst", "").lower()
    if kode in IKKE_AVGJORT or "ikke avgjort" in tekst:
        return "apen"
    if kode == "falt":
        return "falt"
    if kode in ("orientering", "valg"):
        return "noytral"
    return ""


def _model_line(modell: dict | None) -> str:
    if not modell:
        return ""
    parts = [(modell.get(k) or {}).get("begrunnelse", "").strip() for k in ("angar", "konkret")]
    return " ".join(p for p in parts if p)


def meld_feil(nr: str, tittel: str, url: str) -> str:
    template = getattr(config, "MELD_FEIL", None)
    if template:
        return template.format(nr=quote(nr, safe=""), tittel=quote(tittel, safe=""), url=quote(url, safe=""))
    body = f"Sak: {nr} – {tittel}\nSide: {url}\n\nHva er feil, og hva sier kilden?\n"
    return f"https://github.com/{config.REPO}/issues/new?" + urlencode({"title": f"Feil i sak {nr}", "body": body})


def _meetings(saker: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for s in saker:
        groups[s["mote"]].append(s)
    moter = []
    for slug, items in groups.items():
        items.sort(key=lambda s: s["seq"])
        first = items[0]
        moter.append({
            "slug": slug,
            "organ": first["organ"],
            "organ_kort": first["organ_kort"],
            "dato": first["dato"],
            "dato_tekst": first["dato_tekst"],
            "saker": items,
            "fremhevet": sum(s["fremhevet"] for s in items),
        })
    moter.sort(key=lambda m: (m["dato"], m["organ"]), reverse=True)
    return moter


def _window(saker: list[dict], idag: date, days: int | None) -> list[dict]:
    if days is None:
        return [s for s in saker if s["dato"].year == idag.year and s["dato"] <= idag]
    start = idag - timedelta(days=days)
    return [s for s in saker if start <= s["dato"] <= idag]


def _weights() -> list[dict]:
    total = sum(score.WEIGHTS.values()) or 1
    out = []
    for key, points in score.WEIGHTS.items():
        name, meaning = SIGNALS.get(key, (key.replace("_", " ").capitalize(), ""))
        out.append({"navn": name, "poeng": points, "andel": round(100 * points / total), "forklaring": meaning})
    return out


def _terskel() -> int:
    return getattr(config, "FREMHEVET_TERSKEL", 40)


def _site_url() -> str:
    url = getattr(config, "SITE_URL", "https://v3x4t0r.github.io/tingposten/")
    return url if url.endswith("/") else url + "/"


def nb_date(d: date) -> str:
    return f"{d.day}. {MONTHS[d.month - 1]} {d.year}"


def nb_weekday_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()} {nb_date(d)}"


def _rfc822(d: date) -> str:
    return format_datetime(datetime.combine(d, time(12), tzinfo=timezone.utc))


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["rfc822"] = _rfc822
    env.globals["repo"] = config.REPO
    env.globals["front_figure"] = front_figure
    return env


def _guard(site_dir: Path) -> None:
    """Refuse to replace a directory that isn't a previous build."""
    resolved = site_dir.resolve()
    if resolved in {config.ROOT.resolve(), config.DATA_DIR.resolve(), Path.home().resolve(), Path("/")}:
        raise ValueError(f"refusing to build into {site_dir}")
    if site_dir.exists() and any(site_dir.iterdir()) and not (site_dir / ".nojekyll").exists():
        raise ValueError(f"{site_dir} is not empty and doesn't look like a site build (.nojekyll missing)")
