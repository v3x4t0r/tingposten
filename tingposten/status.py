"""Vedtaksstatus: was anything actually decided, and what kind of outcome is it?

Read off the adopted vedtak text and the votes in the protokoll. The point is
the user's rule from the first calibration: a plan sent back for more work is
not a decision ("Ingenting er vedtatt"), so the card says so and the score
holds it back until the sak returns.
"""

import re

LABELS = {
    "vedtatt": "Vedtatt",
    "sendt_tilbake": "Sendt tilbake – ikke avgjort ennå",
    "utsatt": "Utsatt – ikke avgjort ennå",
    "orientering": "Tatt til orientering",
    "falt": "Forslaget falt",
    "ikke_behandlet": "Ikke behandlet",
    "valg": "Valg/fritak",
    "overlevert": "Overlevert – behandles senere",
    "under_behandling": "Under behandling",
}

# Nothing is settled yet: the sak will come back, or was never dealt with.
IKKE_AVGJORT = {"sendt_tilbake", "utsatt", "ikke_behandlet", "under_behandling", "overlevert"}

VALG = re.compile(r"fritak|uttreden|varamedlem|settemedlem|nyvalg|\bvalg av (?!trasé)", re.I)
OVERLEVERING = re.compile(r"overlevering av representantforslag", re.I)
IKKE_BEHANDLET = re.compile(r"ikke behandlet|overskredet tid|ikkje handsama", re.I)
IKKE_FATTET = re.compile(r"ikke fattet vedtak|ikkje gjort vedtak", re.I)
# "Byrådet bes komme tilbake …" is a follow-up request, not a return.
SENDT_TILBAKE = re.compile(
    r"sendes\s+(?:\w+\s+){0,3}?tilbake|sendes i retur|returneres|tilbakesendes|"
    r"vert sendt attende|sendast attende|ytterligere bearbeiding|ytterlegare bearbeiding",
    re.I,
)
UTSATT = re.compile(r"\butsettes\b|\bvert utsett\b|\butsetjast\b", re.I)
ORIENTERING = re.compile(r"til orientering", re.I)
MERKNAD = re.compile(r"\bMerknad(?:er)?(?:\s+\d+)?\s+(?=fra\b|frå\b|[A-ZÆØÅ])")
# "Daniel Hägglund (FRP) fremsatte på vegne av FRP følgende protokolltilførsel: …"
TILFORSEL = re.compile(r"(?:[A-ZÆØÅ][\wæøåÆØÅ'\-]*\s+){1,6}\([^)]*\)\s+fremsatte\b", re.S)


def vedtaksstatus(item: dict, protokoll: dict) -> dict:
    """{"kode": one of LABELS, "tekst": the Bokmål label for the card}."""
    return _status(_kode(item, protokoll))


def _kode(item: dict, p: dict) -> str:
    tittel = item.get("tittel", "")
    vedtak = " ".join((p.get("vedtak") or "").split())
    votes = p.get("voteringer") or []
    if not (vedtak or votes):
        return "under_behandling"
    if VALG.search(tittel):
        return "valg"
    if OVERLEVERING.search(tittel):
        return "overlevert"
    if IKKE_BEHANDLET.search(vedtak) and not votes:
        return "ikke_behandlet"
    binding = bindende(vedtak)
    if SENDT_TILBAKE.search(binding):
        return "sendt_tilbake"
    if UTSATT.search(binding) or any(
        v["resultat"] == "vedtatt" and v["forslag"].lower().startswith("utsettelse") for v in votes
    ):
        return "utsatt"
    adopted = [v for v in votes if v["resultat"] == "vedtatt" and not v["forslag"].lower().startswith("merknad")]
    if IKKE_FATTET.search(vedtak) or (votes and not adopted):
        return "falt" if any(v["resultat"] == "falt" for v in votes) else "ikke_behandlet"
    if _only_orientering(binding):
        return "orientering"
    return "vedtatt"


def bindende(vedtak: str) -> str:
    """The adopted text without merknader and protokolltilførsler, which don't bind."""
    cut = len(vedtak)
    for pattern in (MERKNAD, TILFORSEL):
        m = pattern.search(vedtak)
        if m:
            cut = min(cut, m.start())
    return vedtak[:cut].strip()


def _only_orientering(binding: str) -> bool:
    """True when every point of the vedtak only takes something 'til orientering'."""
    if not ORIENTERING.search(binding):
        return False
    # Numbered points ("1. … 2. …") or, failing that, sentences.
    points = [x for x in re.split(r"(?:^|\s)\d+\.\s+", binding) if x.strip()]
    if len(points) <= 1:
        points = [x for x in re.split(r"(?<=[.!?])\s+(?=[A-ZÆØÅ])", binding) if x.strip()]
    return all(ORIENTERING.search(x) for x in points)


def _status(kode: str) -> dict:
    return {"kode": kode, "tekst": LABELS[kode]}
