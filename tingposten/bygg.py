"""Bygg: the public static site, from edited cards, metadata and the score.

Reads data/bergen/: agendapunkter/ (metadata), redigert/ (cards after the
fact-checker and the delete-only editor) and pdf/ (for the score). Writes
plain HTML to site/: no JavaScript, no external assets, relative links only,
so the same files work under v3x4t0r.github.io/tingposten/ and at the root
of tingposten.no. Source links go to Bergen kommune's own PDFs.

Laid out like a newspaper: front pages with a lead story, a row of four,
a two-column list (15 stories in all) and a tile per topic (tema); one page
per sak with the full card and, for the top saker, a background article.
Every sentence shown is from the edited card or the edited article; the dek
is the card's first sentence. The small facts under a story (where, the
vote, the amount) are mechanical values read off the documents, chosen by
one fixed rule in fakta().
"""

import html
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
from markupsafe import Markup, escape

from . import config, grafikk, pdf, score
from .klassifiserer import TEMA
from .saksframlegg import saksbydeler
from . import status as status_mod
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
MONTHS_SHORT = ("jan.", "feb.", "mars", "apr.", "mai", "juni", "juli", "aug.", "sep.", "okt.", "nov.", "des.")
WEEKDAYS = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
SECONDARY = 4       # the row of four under the lead
LISTE = 10          # "Flere vedtak": the two-column list after the four (1 + 4 + 10 = 15 stories)
FLIS = 3            # headlines per topic tile ("Alle temaer")
PANEL_MOTER = 6     # meetings listed in "Perioden i tall" before "og N møter til"
RELATED = 6         # "Fra samme møte" on a sak page
# Short organ names for kickers and breadcrumbs.
ORGAN_KORT = {"571424": "Bystyret", "900101": "Byrådet (innstilling)", "900102": "Byrådet"}
DOK_NAVN = {"saksframlegg": "Saksframlegg", "protokoll": "Protokoll"}
DOK_BESTEMT = {"saksframlegg": "saksframlegget", "protokoll": "protokollen"}
ORD_PER_MINUTT = 200  # reading time of a background article
# Facts under a story (fakta()): proposals that fell are counted from two, as
# the score's "N forslag falt"; an amount from 1 mill. kr, where the score's
# money scale starts.
FAKTA_FORSLAG_FALT = 2
FAKTA_BELOP_KRONER = 1_000_000
VIEWS = {           # front-page views: path, days back (None = this year), label
    "": (14, "Siste 14 dager"),
    "30-dager/": (30, "Siste 30 dager"),
    "i-ar/": (None, "I år"),
}
# Fallback when the classifier hasn't given a sak a tema yet: first match on the
# title wins, so the more specific topics come first.
TEMA_ORD = (
    ("styring", ("fritak fra verv", "valg av", "nyvalg", "uttreden", "lovlighetskontroll", "kontrollutvalg",
                 "byrådet", "byrådsmodell", "styreform", "representantforslag", "eventuelt", "konstituering")),
    ("skole", ("skole", "barnehage", "elev", "lærer")),
    ("helse", ("helse", "eldre", "omsorg", "rehabilitering", "sykehjem", "bofellesskap", "legevakt", "rus",
               "psykisk", "sosial")),
    # Not "vei"/"vegen": street names end that way in most planning titles.
    ("samferdsel", ("bybane", "tunnel", "bompenge", "bomstasjon", "kollektiv", "sykkel", "havn", "e39", "e16",
                    "vossebanen", "jernbane", "trasé", "parkering")),
    ("miljo", ("klima", "energi", "kjernekraft", "miljø", "natur", "avfall", "avløp", "påslipp", "vannforsyning")),
    ("kultur", ("kultur", "idrett", "kino", "musikk", "bibliotek", "frivillig", "museum")),
    ("byutvikling", ("regulering", "områdeplan", "gnr", "bnr", "bolig", "eiendom", "forkjøpsrett", "byggesak",
                     "utbygging", "tomt")),
    ("okonomi", ("budsjett", "økonomi", "eierskap", "tilskudd", "revisjon", "investering", "bevilgning",
                 "regnskap", "selskap", " as ", "opsjonsavtale")),
    ("styring", ("høring", "interpellasjon")),
)

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
    "angar": ("Hvor mange det angår", "Hvor mange saken til slutt angår, direkte eller indirekte. Vurderes av en språkmodell."),
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
    idag_tekst = nb_weekday_date(idag)
    write("index.html", "landing.html", side="landing", idag_tekst=idag_tekst)
    for slug, (days, label) in VIEWS.items():
        window = _window(saker, idag, days)
        front = front_page(window)
        write(f"bergen/{slug}index.html", "forside.html", side="forside", visning=slug, periode=label,
              idag_tekst=idag_tekst, **front)
        write(f"bergen/{slug}alle/index.html", "alle.html", side="alle", visning=slug, periode=label,
              moter=_meetings(window), antall=len(window))
        temaer = tema_counts(window)
        for t in temaer:
            in_tema = [s_ for s_ in window if s_["tema"] == t["tema"]]
            write(f"bergen/{slug}tema/{t['tema']}/index.html", "tema.html", side="tema", visning=slug,
                  periode=label, tema=t["tema"], navn=t["navn"], moter=_meetings(in_tema), antall=len(in_tema),
                  temaer=[x for x in temaer if x["tema"] != t["tema"]])
        if slug == "i-ar/":
            # A sak page's breadcrumb links to its tema for the year, when the sak is in it.
            for s_ in window:
                s_["tema_side"] = f"bergen/i-ar/tema/{s_['tema']}/"
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

    shutil.copytree(HERE / "static", tmp / "static")
    (tmp / ".nojekyll").write_text("")
    if getattr(config, "SKJUL_FOR_SOK", False):
        (tmp / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
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


def figur(sak: dict, kind: str) -> dict | None:
    """The sak's figure of one type (kart, stemmer, belop), if any."""
    return next((f for f in sak["figurer"] if f.get("type") == kind), None)


def _sak(item: dict, ranked: dict, data_dir: Path, parsers: "_Parsers | None" = None) -> dict:
    parsers = parsers or _Parsers()
    card_path = data_dir / "redigert" / f"{pdf.key(item)}.json"
    card = json.loads(card_path.read_text()) if card_path.exists() else None
    klar = _protokoll_klar(item, data_dir, parsers)
    usable = bool(card) and not card.get("avvist") and klar
    dato = date.fromisoformat(item["moteDato"])
    uid = item["utvalgId"]
    slug = f"{uid}-{item['moteDato']}"
    anchor = f"sak-{pdf.tag(item)}"
    status = ranked.get("status") or {}
    felt = _fields(item, card) if usable else []
    dek, dek_kilder, felt = _dek(felt)
    side = f"bergen/sak/{pdf.key(item)}/"
    tema = _tema(item, data_dir)
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
        "felt": felt,
        "dek": dek,
        "dek_kilder": dek_kilder,
        "har_sammendrag": bool(dek or felt),
        "tema": tema,
        "tema_navn": TEMA[tema],
        "tema_side": None,
        "artikkel": _article(item, data_dir) if usable else [],
        "figurer": (_figures(item, data_dir, parsers) or list(card.get("figurer", []))) if usable else [],
        "pdf": {k: u for k, _, _ in pdf.KINDS if (u := pdf.url(item, k))},
        "mote": slug,
        "anker": anchor,
        "side": side,
        "lenke": side,
        "venter_protokoll": not klar,
    }
    sak["har_artikkel"] = bool(sak["artikkel"])
    sak["lesetid"] = lesetid(sak["artikkel"])
    sak["fremhevet"] = sak["score"] >= _terskel()
    sak["meld_feil"] = meld_feil(sak["nr"], sak["tittel"], _site_url() + sak["lenke"])
    sak.update(_facts(item, ranked.get("signaler") or {}, data_dir, parsers))
    sak["fakta"] = fakta(sak)
    return sak


def _facts(item: dict, sig: dict, data_dir: Path, parsers: "_Parsers") -> dict:
    """Mechanical facts about the sak, from the same parsed documents as the
    score (score.signals) and the figures (grafikk): numbers, never prose."""
    sf = parsers.saksframlegg(data_dir / "pdf" / pdf.filename(item, "saksframlegg"))
    stemmer = None
    if sig.get("stemmer_for"):
        stemmer = {"for": sig["stemmer_for"], "mot": sig.get("stemmer_mot"),
                   # a close vote, not a lopsided one: the score's own bar for showing it
                   "jevn": sig.get("uenighet", 0.0) >= score.BADGE_MIN["uenighet"]}
    return {
        "stemmer": stemmer,
        "forslag_falt": sig.get("forslag_falt") or 0,
        "innstilling_falt": bool(sig.get("innstilling_falt")),
        # the bydeler the map shows: same function, same saksframlegg
        "bydeler": saksbydeler(item, sf)[0],
        "hele_byen": bool(sig.get("hele_byen")),
        "hele_kommunen": bool(sig.get("hele_kommunen")),
        "belop": sig.get("belop_kroner"),
    }


def fakta(s: dict) -> list[dict]:
    """The small facts under a story, by one fixed rule and in a fixed order:
    status, where, the vote (or how many proposals fell), amount, Bakgrunn.

    "Vedtatt" is the normal case and never a fact: the status is shown only
    when it says something else, or when the innstilling fell. Organ and date
    are not facts here; the templates show them only off the period's
    standard meeting. Every value is a mechanical one: no model text.
    """
    out = []

    def add(kind, etikett, verdi):
        out.append({"type": kind, "etikett": etikett, "verdi": verdi})

    if s["status_klasse"]:
        add("status", "Status", s["status_tekst"])
    elif s["innstilling_falt"]:
        add("status", "Status", "Innstillingen falt")
    if sted := _sted(s):
        add("sted", "Gjelder", sted)
    st = s["stemmer"]
    if st and st["jevn"]:
        add("stemmer", "Avstemning", f"{st['for']}–{st['mot']}" if st["mot"] is not None else f"{st['for']} stemmer for")
    elif s["forslag_falt"] >= FAKTA_FORSLAG_FALT:
        add("falt", "Forslag som falt", str(s["forslag_falt"]))
    belop_figur = figur(s, "belop")
    if s["belop"] and s["belop"] >= FAKTA_BELOP_KRONER:
        add("kr", "Beløp", belop_tekst(s["belop"]))
    elif belop_figur and belop_figur.get("belop"):
        add("kr", "Beløp", belop_figur["belop"]["tekst"])
    if s["har_artikkel"]:
        add("bakgrunn", "Bakgrunn", f"Ca. {s['lesetid']} min lesing")
    return out


def _sted(s: dict) -> str:
    by = s["bydeler"]
    if len(by) == 1:
        return by[0]
    if len(by) == 2:
        return f"{by[0]} og {by[1]}"
    if by:
        return f"{len(by)} bydeler"
    if s["hele_byen"]:
        return "Hele byen"
    if s["hele_kommunen"]:
        return "Hele kommunen"
    return ""


def belop_tekst(kr: float) -> str:
    """866 mill. kr, 3,6 mill. kr, 1,2 mrd. kr (the score's rounding, reader's units)."""
    if kr >= 1e9:
        return f"{kr / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " mrd. kr"
    mill = kr / 1e6
    text = f"{mill:.1f}".rstrip("0").rstrip(".") if mill < 10 else f"{mill:.0f}"
    return text.replace(".", ",") + " mill. kr"


def lesetid(artikkel: list[dict]) -> int:
    """Minutes to read the background article (0 without one)."""
    words = sum(len(a["tekst"].split()) for a in artikkel)
    return max(1, round(words / ORD_PER_MINUTT)) if words else 0


def _protokoll_klar(item: dict, data_dir: Path, parsers: "_Parsers") -> bool:
    """See status.protokoll_klar: until the protokoll covers the meeting, the
    card (written from an older protokoll) waits."""
    path = data_dir / "pdf" / pdf.filename(item, "protokoll")
    return not path.exists() or status_mod.protokoll_klar(item, parsers.protokoll(path))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        log.exception("could not read %s", path)
        return None


def _tema(item: dict, data_dir: Path) -> str:
    signaler = _read_json(data_dir / "signaler" / f"{pdf.key(item)}.json") or {}
    if signaler.get("tema") in TEMA:
        return signaler["tema"]
    return tema_fra_tittel(item.get("tittel", ""))


def tema_fra_tittel(tittel: str) -> str:
    text = f" {tittel.lower()} "
    for tema, words in TEMA_ORD:
        if any(w in text for w in words):
            return tema
    return "annet"


def _article(item: dict, data_dir: Path) -> list[dict]:
    """The edited background article's paragraphs, with links to the cited pages."""
    art = _read_json(data_dir / "artikler-redigert" / f"{pdf.key(item)}.json")
    if not art or art.get("avvist"):
        return []
    out = []
    for a in art.get("avsnitt") or []:
        text = " ".join((a.get("tekst") or "").split())
        if not text:
            continue
        sources = []
        for k in a.get("kilder") or []:
            base = pdf.url(item, k.get("dok", "")) if k.get("dok") in ("saksframlegg", "protokoll") else None
            if base and isinstance(k.get("side"), int):
                sources.append({"dok": k["dok"], "side": k["side"], "href": f"{base}#page={k['side']}"})
        out.append({"tekst": text, "kilder": _dedupe(sources)})
    return out


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


def split_first_sentence(text: str, years: bool = False) -> tuple[str, str]:
    """(first sentence, rest). A stop after an abbreviation, an initial or a
    number never ends the sentence ("punkt 1. Bystyret"), except, with
    years=True, after a year or other number of three or more digits
    ("vedtatt i 2023. Tunnelene …")."""
    text = " ".join(text.split())
    i = 0
    while True:
        i = text.find(". ", i)
        if i < 0:
            return text, ""
        word = text[:i].rsplit(" ", 1)[-1]
        nxt = text[i + 2:i + 3]
        number = word[-1:].isdigit() and not (years and re.search(r"\d{3,}$", word))
        initial = len(word) == 1 and word.isupper()  # "Chris Jørgen K. Rødland"
        if word not in ABBREVIATIONS and not number and not initial and nxt.isupper():
            return text[:i + 1], text[i + 2:]
        i += 2


def forste_setning(dek: str) -> str:
    """The dek cut to one sentence for the smaller stories on a front page.
    The text is the edited card's own: cut, never rewritten."""
    return split_first_sentence(dek, years=True)[0]


def front_page(window: list[dict]) -> dict:
    """Lead, the row of four, the list of ten, one tile per tema, and the rest.

    The 15 stories are the fremhevede saker that have a summary, by score;
    when fewer than 15 are over the threshold, the next best saker with a
    summary fill up (a fremhevet sak whose card isn't ready never leads with
    "Sammendrag kommer"). Saker scoring 0 (valg, fritak) are never stories.
    Each tile shows the tema's best saker not already among the stories.
    """
    ordered = sorted(window, key=lambda s: (-s["score"], -s["dato"].toordinal(), s["seq"]))
    antall = 1 + SECONDARY + LISTE
    stories = [s for s in ordered if s["fremhevet"] and s["har_sammendrag"]][:antall]
    stories += [s for s in ordered if s["score"] > 0 and s["har_sammendrag"] and s not in stories][:antall - len(stories)]
    if not stories:
        stories = ordered[:1]
    moter = _meetings(window)
    standard = standard_mote(moter)
    temaer = sorted(tema_counts(window), key=lambda t: -t["antall"])
    fliser, i_fliser = [], 0
    for t in temaer:
        rest = [s for s in ordered if s["tema"] == t["tema"] and s not in stories][:FLIS]
        i_fliser += len(rest)
        fliser.append({**t, "saker": rest})
    return {
        "lead": stories[0] if stories else None,
        "sekundaer": stories[1:1 + SECONDARY],
        "liste": stories[1 + SECONDARY:],
        "fliser": fliser,
        "temaer": temaer,
        "standard": standard,
        "moter": moter,
        "panel_moter": panel_moter(moter, standard),
        "andre": len(window) - len(stories) - i_fliser,
        "antall": len(window),
    }


def standard_mote(moter: list[dict]) -> dict | None:
    """The meeting most of the period's saker come from (the latest on a tie).
    Stories from it don't repeat organ and date; the front page says so once."""
    return max(moter, key=lambda m: len(m["saker"]), default=None)


def panel_moter(moter: list[dict], standard: dict | None) -> list[dict]:
    """Meetings for "Perioden i tall": the standard meeting first, then the
    biggest others, listed by organ and date."""
    if not moter:
        return []
    others = sorted((m for m in moter if m is not standard), key=lambda m: (-len(m["saker"]), -m["dato"].toordinal()))
    picked = others[:PANEL_MOTER - 1]
    picked.sort(key=lambda m: (m["organ_kort"], m["dato"]))
    return [standard, *picked]


def tema_counts(saker: list[dict]) -> list[dict]:
    """Every tema present, with its number of saker, in TEMA order."""
    counts = defaultdict(int)
    for s in saker:
        counts[s["tema"]] += 1
    return [{"tema": t, "navn": TEMA[t], "antall": counts[t]} for t in TEMA if counts[t]]


def related(sak: dict, meeting: dict) -> list[dict]:
    others = [s for s in meeting["saker"] if s is not sak and s["har_sammendrag"]]
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
            "maned": f"{MONTHS[first['dato'].month - 1].capitalize()} {first['dato'].year}",
            "maned_nr": first["dato"].strftime("%Y-%m"),
        })
    moter.sort(key=lambda m: (m["dato"], m["organ"]), reverse=True)
    return moter


def _window(saker: list[dict], idag: date, days: int | None) -> list[dict]:
    """The period's vedtak. A sak whose meeting is held but whose protokoll
    doesn't cover it yet is left out: its outcome isn't known (meeting page only)."""
    saker = [s for s in saker if not s.get("venter_protokoll")]
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


def nb_date_short(d: date, year: bool = False, short: bool = True) -> str:
    """23. sep. (or 23. september, with or without the year)."""
    return f"{d.day}. {(MONTHS_SHORT if short else MONTHS)[d.month - 1]}" + (f" {d.year}" if year else "")


def tittel_html(text: str) -> Markup:
    """A headline, escaped, with 'Dokken–Lyngbø'-style compounds kept on one line."""
    return Markup(re.sub(r"(\w+–\w+)", r'<span class="nb">\1</span>', str(escape(text))))


def minikart(svg: str) -> Markup:
    """The bydel map as a small decorative locator: the bydel's name is in the
    text beside it, so the SVG drops its title, description and role."""
    svg = re.sub(r'\s(role|aria-labelledby)="[^"]*"', "", svg, count=2)
    svg = re.sub(r"<svg\b", '<svg aria-hidden="true" focusable="false"', svg, count=1)
    svg = re.sub(r"<title id=[^>]*>.*?</title><desc[^>]*>.*?</desc>", "", svg, count=1)
    return Markup(svg)


def figur_tittel(svg: str) -> str:
    m = re.search(r"<title id=[^>]*>([^<]+)</title>", svg)
    return html.unescape(m.group(1)) if m else ""


def kildegrupper(kilder: list[dict]) -> list[dict]:
    """Sources grouped per document, pages sorted: Saksframlegg s. 1, 3, 8."""
    groups: dict[str, dict] = {}
    for k in kilder or []:
        groups.setdefault(k["dok"], {}).setdefault(k["side"], k["href"])
    return [{"dok": dok, "navn": DOK_NAVN.get(dok, dok.capitalize()),
             "sider": [{"side": n, "href": sider[n]} for n in sorted(sider)]} for dok, sider in groups.items()]


def artikkel_dokumenter(artikkel: list[dict]) -> str:
    """"saksframlegget og protokollen": the documents a background article builds on."""
    docs = []
    for a in artikkel:
        for k in a["kilder"]:
            if k["dok"] not in docs:
                docs.append(k["dok"])
    names = [DOK_BESTEMT.get(d, d) for d in docs]
    return " og ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else "".join(names)


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
    env.filters["kort_dato"] = nb_date_short
    env.filters["tittel"] = tittel_html
    env.filters["minikart"] = minikart
    env.filters["figur_tittel"] = figur_tittel
    env.filters["kildegrupper"] = kildegrupper
    env.filters["forste_setning"] = forste_setning
    env.globals["repo"] = config.REPO
    env.globals["figur"] = figur
    env.globals["artikkel_dokumenter"] = artikkel_dokumenter
    env.globals["fonter"] = (HERE / "static" / "fonts.css").exists()
    env.globals["skjult"] = getattr(config, "SKJUL_FOR_SOK", False)
    return env


def _guard(site_dir: Path) -> None:
    """Refuse to replace a directory that isn't a previous build."""
    resolved = site_dir.resolve()
    if resolved in {config.ROOT.resolve(), config.DATA_DIR.resolve(), Path.home().resolve(), Path("/")}:
        raise ValueError(f"refusing to build into {site_dir}")
    if site_dir.exists() and any(site_dir.iterdir()) and not (site_dir / ".nojekyll").exists():
        raise ValueError(f"{site_dir} is not empty and doesn't look like a site build (.nojekyll missing)")
