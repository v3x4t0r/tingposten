"""A Stortinget sak as an item the model steps can handle, and the documents they cite.

Four documents, each as text with "--- side N ---" markers like Bergen's PDFs:
- "innstilling": the committee's recommendation, the PDF from www.stortinget.no
  (pdf/<sesjon>_<sakid>_innstilling.pdf; its printed page numbers match the PDF's).
- "vedtak": what Stortinget adopted, one page per numbered decision (stortingsvedtak)
  and the law text of a lovvedtak. The adopted text can differ from the innstilling
  (200236: «innen utgangen av 2026» became «innen første halvår 2027»).
- "forslag": a representantforslag (Dokument 8) itself, the PDF from www.stortinget.no: the
  proposers' own reasons and proposed decisions (owner, 2.11: like Bergen's attachments).
- "votering": one page per vote: what was voted on, the result, the parties on each
  side (counted from the per-seat results) and the proposal's text.
Every page keeps the link a reader follows to check it (doc_url).
"""

import html
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .. import config
from ..kildetekst import dokumenttekst
from .api import tid, url
from .innhenter import BEHANDLET, lagret, vedtaksdato

TYPER = {1: "budsjett", 2: "alminneligsak", 3: "lovsak"}
DOKUMENTGRUPPER = {0: "ikke_spesifisert", 1: "proposisjon", 2: "melding", 3: "redegjorelse",
                   4: "representantforslag", 5: "grunnlovsforslag", 6: "dokumentserien",
                   7: "innstillingssaker", 8: "innberetning"}
# The API's party ids → as the parties write themselves (Ap for "A").
PARTI = {"A": "Ap"}
STEMME = {1: "ikke til stede", 2: "for", 3: "mot"}
DOKUMENTER = ("innstilling", "vedtak", "votering", "forslag")

# Formal saker with nothing for a reader: elections to Stortinget's own bodies, reports
# from its delegations, the list of saker left over at the end of a session, the split of
# the budget between the committees, ministers' statements (redegjørelser, which are
# only "vedlagt protokollen"). The throne speech debate stays: it can end in requests to
# the government.
STOY = re.compile(r"^(valg av|innberetning|saker som stortinget ikke har behandlet|rapport til stortinget fra "
                  r"stortingets delegasjon|referat|melding fra stortingets presidentskap|innstilling fra valgkomiteen"
                  r"|endringer i de faste komiteers sammensetning|den norske nobelkomit|fordeling til komiteene"
                  r"|redegjørelse)", re.IGNORECASE)


def parti(pid: str | None) -> str:
    return PARTI.get(pid or "", pid or "uavhengig")


def er_stoy(sak: dict) -> bool:
    return DOKUMENTGRUPPER.get(sak.get("dokumentgruppe")) == "innberetning" or bool(
        STOY.match(sak.get("korttittel") or sak.get("tittel") or ""))


# --- items ---------------------------------------------------------------------------------

def item(lagret_sak: dict) -> dict:
    """The fields the pipeline and the site use, from a stored sak (innhenter.hent_sak)."""
    sak = lagret_sak["sak"]
    sesjon, sakid = sak.get("sak_sesjon"), int(sak["id"])
    dato = vedtaksdato(lagret_sak)
    innst = innstilling_ref(sak)
    return {
        "kilde": "stortinget",
        "id": f"stortinget:{sakid}",
        "sakid": sakid,
        "sesjon": sesjon,
        "utvalgId": "stortinget",
        "utvalgsNavn": "Stortinget",
        "moteDato": dato.date().isoformat() if dato else None,
        "vedtatt_tid": dato.isoformat() if dato else None,
        "tittel": " ".join((sak.get("tittel") or "").split()),
        "korttittel": " ".join((sak.get("korttittel") or "").split()),
        "type": TYPER.get(sak.get("type"), str(sak.get("type"))),
        "dokumentgruppe": DOKUMENTGRUPPER.get(sak.get("dokumentgruppe"), str(sak.get("dokumentgruppe"))),
        "komite": (sak.get("komite") or {}).get("navn"),
        "henvisning": sak.get("henvisning"),
        "referansenr": (innst or {}).get("lenke_tekst") or f"Sak {sak.get('sak_nummer') or sakid} ({sesjon})",
        "kortvedtak": sak.get("kortvedtak"),
        "url": f"{config.STORTINGET_WWW}/no/Saker-og-publikasjoner/Saker/Sak/?p={sakid}",
        "behandlet": sak.get("status") == BEHANDLET,
        "stoy": er_stoy(sak),
    }


def key(item: dict) -> str:
    return f"{item['sesjon']}/{item['sakid']}"


def saker(data_dir: Path = config.STORTINGET_DATA_DIR, fra: str | None = None) -> list[dict]:
    """Decided, non-formal saker on disk (decided on or after `fra`), oldest first."""
    out = []
    for path in sorted((data_dir / "sak").glob("*.json")):
        i = item(json.loads(path.read_text()))
        if i["behandlet"] and not i["stoy"] and i["moteDato"] and (not fra or i["moteDato"] >= fra):
            out.append(i)
    return sorted(out, key=lambda i: (i["moteDato"], i["sakid"]))


# --- the innstilling PDF and the lovvedtak --------------------------------------------------

def _referanser(sak: dict) -> list[dict]:
    return sak.get("publikasjon_referanse_liste") or []


def innstilling_ref(sak: dict) -> dict | None:
    return next((r for r in _referanser(sak) if (r.get("eksport_id") or "").startswith("inns-")), None)


def lovvedtak_ref(sak: dict) -> dict | None:
    return next((r for r in _referanser(sak) if (r.get("eksport_id") or "").startswith("vedtak-")), None)


def forslag_ref(sak: dict) -> dict | None:
    return next((r for r in _referanser(sak) if (r.get("eksport_id") or "").startswith("dok8-")), None)


def forslag_pdf_url(sak: dict) -> str | None:
    ref = forslag_ref(sak)
    if not ref:
        return None
    return (f"{config.STORTINGET_WWW}/globalassets/pdf/representantforslag/"
            f"{sak.get('sak_sesjon')}/{ref['eksport_id']}.pdf")


def pdf_url(sak: dict, dok: str) -> str | None:
    return {"innstilling": innstilling_pdf_url, "forslag": forslag_pdf_url}[dok](sak)


def innstilling_pdf_url(sak: dict) -> str | None:
    ref = innstilling_ref(sak)
    if not ref:
        return None
    return (f"{config.STORTINGET_WWW}/globalassets/pdf/innstillinger/stortinget/"
            f"{sak.get('sak_sesjon')}/{ref['eksport_id']}.pdf")


def pdf_fil(data_dir: Path, item: dict, dok: str = "innstilling") -> Path:
    return data_dir / "pdf" / f"{item['sesjon']}_{item['sakid']}_{dok}.pdf"


def pub_fil(data_dir: Path, eksport_id: str) -> Path:
    return data_dir / "pub" / f"{eksport_id}.xml"


def hent_dokumenter(items: list[dict], data_dir: Path = config.STORTINGET_DATA_DIR, client=None) -> dict:
    """Download what the saker still lack: the innstilling and Dokument 8 PDFs, the lovvedtak XML."""
    from .api import Client

    client = client or Client()
    teller = Counter()
    for i in items:
        sak = lagret(data_dir, i["sakid"])["sak"]
        for dok in ("innstilling", "forslag"):
            if (u := pdf_url(sak, dok)) and not (p := pdf_fil(data_dir, i, dok)).exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(client.bytes(u))
                teller[dok] += 1
        if (ref := lovvedtak_ref(sak)) and not (p := pub_fil(data_dir, ref["eksport_id"])).exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(client.publikasjon(ref["eksport_id"]))
            teller["lovvedtak"] += 1
    teller["foresporsler"] = client.antall
    return dict(teller)


# --- the documents as pages -----------------------------------------------------------------

def _tekst_fra_html(s: str | None) -> str:
    s = re.sub(r"<\s*/?\s*(p|div|br|li)[^>]*>", "\n", s or "", flags=re.IGNORECASE)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return "\n".join(" ".join(line.split()) for line in s.splitlines() if line.strip())


def lov_tekst(xml: str) -> str:
    """The law text of a lovvedtak publication, headings and paragraphs on their own lines."""
    rot = ET.fromstring(xml)
    linjer = []
    for el in rot.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("TilStortinget", "Tittel", "Stikktittel", "A"):
            tekst = " ".join("".join(el.itertext()).split())
            if tekst and (tag != "A" or not linjer or linjer[-1] != tekst):
                linjer.append(tekst)
    return "\n".join(linjer)


def _deler(tekst: str, maks: int = 3500) -> list[str]:
    """Long text split at line breaks into pages of about `maks` characters."""
    sider, del_ = [], []
    for linje in tekst.splitlines():
        if del_ and sum(len(x) + 1 for x in del_) + len(linje) > maks:
            sider.append("\n".join(del_))
            del_ = []
        del_.append(linje)
    if del_:
        sider.append("\n".join(del_))
    return sider


def vedtak_sider(item: dict, data_dir: Path = config.STORTINGET_DATA_DIR) -> list[dict]:
    """[{"tekst", "url"}]: each numbered decision, then the lovvedtak's text."""
    sak = lagret(data_dir, item["sakid"])["sak"]
    sider = []
    vedtaksfil = data_dir / "stortingsvedtak" / f"{item['sesjon']}.json"
    vedtak = [v for v in (json.loads(vedtaksfil.read_text()) if vedtaksfil.exists() else [])
              if int(v.get("sak_id") or 0) == item["sakid"]]
    for v in sorted(vedtak, key=lambda v: v.get("stortingsvedtak_nummer") or 0):
        dato = tid(v.get("stortingsvedtak_dato_tid"))
        hode = (f"Stortingsvedtak nr. {v.get('stortingsvedtak_nummer')}"
                + (f" ({(v.get('stortingsvedtak_type') or {}).get('navn')})" if v.get("stortingsvedtak_type") else "")
                + (f", {dato.strftime('%d.%m.%Y')}" if dato else ""))
        sider.append({"tekst": hode + "\n" + _tekst_fra_html(v.get("stortingsvedtak_tekst")),
                      "url": url(v.get("stortingsvedtak_lenke_url")) or item["url"]})
    if (ref := lovvedtak_ref(sak)) and (p := pub_fil(data_dir, ref["eksport_id"])).exists():
        lenke = url(ref.get("lenke_url")) or item["url"]
        for n, del_ in enumerate(_deler(lov_tekst(p.read_text())), 1):
            hode = f"{ref.get('lenke_tekst') or 'Lovvedtak'}" + (f" (del {n})" if n > 1 else "")
            sider.append({"tekst": hode + "\n" + del_, "url": lenke})
    if not sider and sak.get("kortvedtak"):
        # Without numbered decisions (e.g. a proposal that fell): Stortinget's own summary.
        sider.append({"tekst": "Stortingets oppsummering av vedtaket\n" + " ".join(sak["kortvedtak"].split()),
                      "url": item["url"]})
    return sider


def partier(resultat: list[dict]) -> dict[str, Counter]:
    """{"for": Counter(party → seats), "mot": …} from the per-seat results."""
    ut = {"for": Counter(), "mot": Counter()}
    for r in resultat:
        side = STEMME.get(r.get("votering"))
        if side in ut:
            ut[side][parti(r.get("parti"))] += 1
    return ut


def _partiliste(c: Counter) -> str:
    return ", ".join(f"{p} {n}" for p, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))) or "ingen"


def forslagstillere(item: dict, data_dir: Path = config.STORTINGET_DATA_DIR) -> str:
    """"5 representanter fra Sp", "7 fra FrP og 2 fra H"; "" when not known."""
    partier = (lagret(data_dir, item["sakid"]) or {}).get("forslagstillere") or {}
    deler = [f"{n} {'representant' if n == 1 else 'representanter'} fra {parti(p)}"
             for p, n in sorted(partier.items(), key=lambda x: (-x[1], x[0]))]
    return " og ".join([", ".join(deler[:-1]), deler[-1]]) if len(deler) > 1 else "".join(deler)


def votering_sider(item: dict, data_dir: Path = config.STORTINGET_DATA_DIR, kort: bool | None = None) -> list[dict]:
    """One page per vote. When the whole is over MAKS_VOTERING_TEGN (the budget), each proposal's
    text is shortened to FORSLAG_KORT characters; the result, the count and the parties stay."""
    if kort is None:
        hele = votering_sider(item, data_dir, kort=False)
        if sum(len(s["tekst"]) for s in hele) <= MAKS_VOTERING_TEGN:
            return hele
        return votering_sider(item, data_dir, kort=True)
    lagret_sak = lagret(data_dir, item["sakid"])
    def orden(v):
        t = tid(v.get("votering_tid"))
        return (t.timestamp() if t else 0.0, v.get("behandlingsrekkefoelge") or 0)
    voteringer = sorted(lagret_sak.get("voteringer", []), key=orden)
    sider = []
    for n, v in enumerate(voteringer, 1):
        t = tid(v.get("votering_tid"))
        linjer = [f"Votering {n} av {len(voteringer)}" + (f", {t.strftime('%d.%m.%Y')}" if t else "")
                  + f": {' '.join((v.get('votering_tema') or '').split())}"]
        resultat = "vedtatt" if v.get("vedtatt") else "ikke vedtatt"
        if v.get("personlig_votering"):
            linjer.append(f"Resultat: {resultat}, {v.get('antall_for')} for og {v.get('antall_mot')} mot "
                          f"({v.get('antall_ikke_tilstede')} ikke til stede).")
            if v.get("resultat"):
                p = partier(v["resultat"])
                linjer += [f"For: {_partiliste(p['for'])}.", f"Mot: {_partiliste(p['mot'])}."]
        else:
            linjer.append(f"Resultat: {resultat} (uten stemmeknapper; ingen opptelling per parti)"
                          + (f", {v.get('antall_for')} for og {v.get('antall_mot')} mot."
                             if (v.get("antall_for") or 0) > 0 else "."))
        if v.get("fri_votering"):
            linjer.append("Fri votering (partiene stemte ikke samlet).")
        for f in v.get("forslag") or []:
            fra = f.get("forslag_paa_vegne_av_tekst")
            linjer.append(f"Forslaget: {f.get('forslag_betegnelse') or ''}" + (f" (på vegne av {fra})" if fra else ""))
            if tekst := _tekst_fra_html(f.get("forslag_tekst")):
                if kort and len(tekst) > FORSLAG_KORT:
                    tekst = tekst[:FORSLAG_KORT].rsplit(" ", 1)[0] + " … (forkortet; hele teksten står i vedtaket)"
                linjer.append(tekst)
        sider.append({"tekst": "\n".join(linjer), "url": item["url"]})
    return sider


def sider(item: dict, dok: str, data_dir: Path = config.STORTINGET_DATA_DIR) -> list[dict]:
    if dok == "vedtak":
        return vedtak_sider(item, data_dir)
    if dok == "votering":
        return votering_sider(item, data_dir)
    raise ValueError(dok)


def tekst(item: dict, dok: str, data_dir: Path = config.STORTINGET_DATA_DIR) -> str:
    """A document as text with "--- side N ---" markers; "" when there is none."""
    if dok in ("innstilling", "forslag"):
        p = pdf_fil(data_dir, item, dok)
        if not p.exists():
            return ""
        t = dokumenttekst(p)
        if dok == "forslag" and (hvem := forslagstillere(item, data_dir)):
            # Stortinget's own record of who proposed it, so the party is never guessed from names.
            t = t.replace("--- side 1 ---\n", f"--- side 1 ---\nFremmet av {hvem} (Stortingets saksopplysninger).\n", 1)
        return t
    return "\n".join(f"--- side {n} ---\n{s['tekst']}" for n, s in enumerate(sider(item, dok, data_dir), 1))


def doc_url(item: dict, dok: str, side: int | None, data_dir: Path = config.STORTINGET_DATA_DIR) -> str:
    """Where a reader checks a citation: the PDF page, the decision, or the sak page."""
    if dok in ("innstilling", "forslag"):
        sak = lagret(data_dir, item["sakid"])["sak"]
        base = pdf_url(sak, dok) or item["url"]
        return f"{base}#page={side}" if side and base.endswith(".pdf") else base
    pages = sider(item, dok, data_dir)
    if side and 1 <= side <= len(pages):
        return pages[side - 1]["url"]
    return item["url"]


MAKS_INNSTILLING_SIDER = 40  # longer documents (the budget) go through the cheap reader's pages
LESES = ("innstilling", "vedtak")  # the documents the cheap reader cuts when they are that long
MAKS_VALGT = {"innstilling": 20, "vedtak": 15}  # pages it may pick in all
MAKS_VOTERING_TEGN = 60_000  # longer vote documents (the budget's hundreds of votes) get short proposal texts
FORSLAG_KORT = 400


def digest_fil(data_dir: Path, item: dict, dok: str) -> Path:
    """The cheap reader's picked pages: innstillingsdigest/, vedtaksdigest/."""
    return data_dir / f"{dok}sdigest" / f"{key(item)}.json"


def for_lang(tekst_: str) -> bool:
    from ..kildetekst import sider as sidetall

    return len(sidetall(tekst_)) > MAKS_INNSTILLING_SIDER


def kilder_for(item: dict, data_dir: Path = config.STORTINGET_DATA_DIR) -> dict[str, str]:
    """{"vedtak", "votering", "innstilling", "forslag"} as text with page markers ("" when missing).
    A long innstilling or vedtak is cut to the pages the cheap reader picked (<dok>sdigest/),
    or, before it has read it, to its first pages."""
    from ..kildetekst import sider as sidetall
    from ..kildetekst import utdrag

    ut = {"votering": tekst(item, "votering", data_dir)}
    for dok in LESES:
        t = tekst(item, dok, data_dir)
        if for_lang(t):
            alle = sidetall(t)
            digest = digest_fil(data_dir, item, dok)
            valgt = [s["side"] for s in (json.loads(digest.read_text()).get("sider", []) if digest.exists() else [])]
            valgt = valgt or alle[:MAKS_VALGT[dok]]
            t = (f"Utdrag: {len(valgt)} av {len(alle)} sider, valgt fordi de gjelder vedtaket, begrunnelsen og "
                 f"hvem som berøres.\n" + utdrag(t, valgt))
        ut[dok] = t
    forslag = tekst(item, "forslag", data_dir)
    if len(fs := sidetall(forslag)) > MAKS_INNSTILLING_SIDER:
        forslag = utdrag(forslag, fs[:MAKS_INNSTILLING_SIDER])
    ut["forslag"] = forslag
    return {d: ut[d] for d in ("vedtak", "votering", "innstilling", "forslag")}
