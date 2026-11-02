"""Stortinget's pages on the site: /stortinget/ with the same templates as Bergen.

A sak becomes the same dict Bergen's bygg._sak makes, so forside, alle, tema, mote and
sak render it unchanged; the templates take the source's words from `k`
(bygg.KILDE_STORTINGET). A "møte" is a day Stortinget voted: its votes happen in the
sitting of that day. Only built with `bygg --med-stortinget` until the owner has
tested the copy (config.STORTINGET_PUBLISERT).
"""

import logging
from datetime import date
from pathlib import Path

from .. import bygg, config
from ..typo import rydd_tittel
from . import dokumenter, regler, score
from .dokumenter import key
from .innhenter import lagret

log = logging.getLogger(__name__)

STI = "stortinget/"


def _status(item: dict, data_dir: Path) -> dict:
    """Vedtatt, or «Forslaget falt» for a representantforslag that got no decision of its own."""
    vedtak = [s for s in dokumenter.vedtak_sider(item, data_dir) if s["tekst"].startswith(("Stortingsvedtak", "Lov"))]
    if item.get("dokumentgruppe") == "representantforslag" and not vedtak:
        return {"kode": "falt", "tekst": "Forslaget falt"}
    return {"kode": "vedtatt", "tekst": "Vedtatt"}


def _tema(item: dict, data_dir: Path) -> str:
    s = bygg._read_json(data_dir / "signaler" / f"{key(item)}.json") or {}
    return s["tema"] if s.get("tema") in regler.TEMA else "annet"


def sak(item: dict, ranked: dict, data_dir: Path) -> dict:
    k = key(item)
    card = bygg._read_json(data_dir / "redigert" / f"{k}.json")
    art = bygg._read_json(data_dir / "artikler-redigert" / f"{k}.json")
    usable = bool(card) and not card.get("avvist")
    dato = date.fromisoformat(item["moteDato"])
    status = _status(item, data_dir)
    kort_felt = bygg._fields(item, card, data_dir) if usable else []
    ingress = bygg._ingress(item, card, data_dir) if usable else None
    if ingress:
        dek, dek_kilder, felt = ingress["tekst"], ingress["kilder"], kort_felt
    else:
        dek, dek_kilder, felt = bygg._dek(kort_felt, bygg._setninger(item, data_dir, "hva_endres") if kort_felt else [])
    tema = _tema(item, data_dir)
    side = f"{STI}sak/{k}/"
    sig = ranked.get("signaler") or {}
    stemmer = None
    if sig.get("for") is not None:
        stemmer = {"for": sig["for"], "mot": sig["mot"],
                   "jevn": score._parts(sig)["uenighet"] >= score.BADGE_MIN["uenighet"]}
    sak_json = (lagret(data_dir, item["sakid"]) or {}).get("sak") or {}
    s = {
        "kilde": "stortinget",
        "nr": item["referansenr"],
        "nr_etikett": item["referansenr"].replace("-", "–"),
        "tittel": rydd_tittel(item.get("korttittel") or item.get("tittel", "")),
        "overskrift": (card.get("overskrift") if usable else "") or rydd_tittel(item.get("korttittel") or ""),
        "organ": "Stortinget",
        "organ_kort": "Stortinget",
        "utvalg": "stortinget",
        "dato": dato,
        "dato_tekst": bygg.nb_date(dato),
        "seq": item["sakid"],
        "score": ranked.get("score", 0),
        "grunner": list(ranked.get("grunner", []))[:3],
        "status_kode": status["kode"],
        "status_tekst": status["tekst"],
        "status_klasse": bygg._status_class(status),
        "felt": felt,
        "kort_felt": kort_felt,
        "dek": dek,
        "dek_kilder": dek_kilder,
        "har_ingress": bool(ingress),
        "har_sammendrag": bool(dek or felt),
        "tema": tema,
        "tema_navn": regler.TEMA[tema],
        "tema_side": None,
        "artikkel": bygg._article(item, art, data_dir) if usable else [],
        "figurer": [],
        "pdf": {"innstilling": u} if (u := dokumenter.innstilling_pdf_url(sak_json)) else {},
        "vedlegg": [],
        "mote": f"{dato.isoformat()}",
        "anker": f"sak-{item['sakid']}",
        "key": f"stortinget/{k}",
        "side": side,
        "lenke": side,
        "venter_protokoll": False,
        "rettelser": [],
        "rettet": [],
        "stemmer": stemmer,
        "forslag_falt": sig.get("forslag_falt") or 0,
        "innstilling_falt": False,
        "bydeler": [],
        "hele_byen": False,
        "hele_kommunen": False,
        "belop": None,
        "stortinget_url": item["url"],
    }
    s["har_artikkel"] = bool(s["artikkel"])
    filer = [p for p in (data_dir / "redigert" / f"{k}.json", data_dir / "artikler-redigert" / f"{k}.json")
             if p.exists()]
    s["endret"] = max([dato] + [date.fromtimestamp(p.stat().st_mtime) for p in filer])
    s["lesetid"] = bygg.lesetid(s["artikkel"])
    s["fremhevet"] = s["score"] >= bygg._terskel()
    s["meld_feil"] = bygg.meld_feil(s["nr"], s["tittel"], bygg._site_url() + s["lenke"])
    s["meld_feil_epost"] = bygg.meld_feil_epost(s["nr"], s["tittel"], bygg._site_url() + s["lenke"])
    s["fakta"] = bygg.fakta(s)
    s["status_rad"] = None
    s["kort"] = None
    return s


def load(data_dir: Path = config.STORTINGET_DATA_DIR) -> list[dict]:
    from .pipeline import saker

    items = saker(data_dir)
    ranked = {r["key"]: r for r in score.rank(items, data_dir)}
    return [sak(i, ranked.get(key(i), {}), data_dir) for i in items]


def _med_sti(front: dict) -> dict:
    """Links in shared macros (tiles, meeting heads) read `sti` off the dict."""
    for t in front.get("temaer", []) + front.get("fliser", []) + front.get("ogsa", []):
        t["sti"] = STI
    for m in front.get("moter", []):
        m["sti"] = STI
    return front


def bygg_sider(write, env, idag: date, data_dir: Path | None = None) -> dict:
    """Write /stortinget/ with the build's `write`. Returns counts for the build's log."""
    data_dir = data_dir or config.STORTINGET_DATA_DIR
    saker = load(data_dir)
    k = bygg.KILDE_STORTINGET
    felles = {"k": k, "debatt": None}
    moter = [{**m, "sti": STI, "organ_kort": "Stortinget"} for m in bygg._meetings(saker)]
    by_meeting = {m["slug"]: m for m in moter}
    idag_tekst = bygg.nb_weekday_date(idag)
    for slug, (days, label) in bygg.VIEWS.items():
        window = bygg._window(saker, idag, days)
        front = _med_sti(bygg.front_page(window, regler.TEMA))
        write(f"{STI}{slug}index.html", "forside.html", side="forside", visning=slug, periode=label,
              idag_tekst=idag_tekst, endret=idag, **front, **felles)
        aar = days is None
        write(f"{STI}{slug}alle/index.html", "alle.html", side="alle", visning=slug, periode=label,
              moter=[{**m, "sti": STI} for m in bygg._meetings(window)], antall=len(window), aar=aar, **felles)
        temaer = [{**t, "sti": STI} for t in bygg.tema_counts(window, regler.TEMA)]
        for t in temaer:
            in_tema = [s_ for s_ in window if s_["tema"] == t["tema"]]
            write(f"{STI}{slug}tema/{t['tema']}/index.html", "tema.html", side="tema", visning=slug,
                  periode=label, tema=t["tema"], navn=t["navn"],
                  moter=[{**m, "sti": STI} for m in bygg._meetings(in_tema)], antall=len(in_tema),
                  temaer=[x for x in temaer if x["tema"] != t["tema"]], aar=aar, **felles)
        if slug == "i-ar/":
            for s_ in window:
                s_["tema_side"] = f"{STI}i-ar/tema/{s_['tema']}/"
    write(f"{STI}moter/index.html", "moter.html", side="moter", moter=moter, **felles)
    for m in moter:
        write(f"{STI}mote/{m['slug']}/index.html", "mote.html", side="mote", mote=m, **felles)
    for s_ in saker:
        meeting = by_meeting[s_["mote"]]
        write(s_["side"] + "index.html", "sak.html", side="sak", s=s_, mote=meeting,
              relaterte=bygg.related(s_, meeting), noindex=not s_["har_sammendrag"], endret=s_["endret"], **felles)
    rss = sorted(bygg._window(saker, idag, 30), key=lambda x: (x["dato"], x["score"]), reverse=True)
    rss_xml = env.get_template("rss.xml").render(saker=rss, site_url=bygg._site_url(), bygget=bygg._rfc822(idag), k=k)
    return {"saker": len(saker), "moter": len(moter), "rss": rss_xml,
            "fremhevet": sum(s_["fremhevet"] for s_ in saker)}
