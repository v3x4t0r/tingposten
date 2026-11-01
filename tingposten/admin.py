"""Admin: a small control panel for the Pi, on the home network.

Shows how the site is doing, which meetings wait for their protokoll and
when it is likely to come, and starts a run when someone asks for one:
deploy/run-daily.sh --manuell (fetch the agenda, download the missing PDFs
with the same polite script a person runs by hand, write and check the
cards, build, publish) or --sjekk (fetch the agenda only). Nothing is
fetched or published unless a button is pressed, and every run shares the
daily run's lock, log and kill switch (STOP).

Off the loopback interface the panel needs a password (TINGPOSTEN_ADMIN_PASSORD
in .env, HTTP Basic auth). Requests must name a local host or an IP address
(no DNS rebinding), and a POST needs a custom header, which a page on another
site can't send without a CORS preflight that is never answered.
"""

import ast
import base64
import fcntl
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import statistics
import subprocess
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pypdf import PdfReader

from . import config, pdf, pipeline
from .bygg import MONTHS, ORGAN_KORT, WEEKDAYS
from .llm import read_json

log = logging.getLogger(__name__)

PASSORD_ENV = "TINGPOSTEN_ADMIN_PASSORD"
SJEKK_PAUSE = timedelta(minutes=30)  # run-daily.sh skips hent this soon after the last one
PDF_SEKUNDER = 6                     # per file at the download script's pace (5 s pause + the file)
MODUSER = ("manuell", "sjekk", "publiser")
SIDE = Path(__file__).parent / "templates" / "admin.html"
STATIC = Path(__file__).parent / "static"
STATIC_TYPER = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
                ".ico": "image/x-icon"}
LOGG_LINJER = 80
_START = re.compile(r"^(\S+) start(?: \((\w+)\))?$")
_STEG = re.compile(r"^\S+ == (\S+)")


# Meetings and the protokoll

def _organer() -> list[str]:
    """The organs whose protokoll the pipeline reads (not the recommending ones)."""
    return [uid for uid in config.TIER1 if uid not in config.INNSTILLENDE]


def _items(data_dir: Path, uid: str) -> list[dict]:
    return list((read_json(data_dir / "agendapunkter" / f"{uid}.json") or {}).values())


def _dag(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day}. {MONTHS[d.month - 1]}"


def _dager(n: int) -> str:
    return "1 dag" if n == 1 else f"{n} dager"


def _etter(lo: int, hi: int | None = None) -> str:
    """'samme dag som møtet', '2 dager etter møtet', '1–5 dager etter møtet', …"""
    hi = lo if hi is None else hi
    if hi == 0:
        return "samme dag som møtet"
    if lo == hi:
        return f"{_dager(lo)} etter møtet"
    if lo == 0:
        return f"fra samme dag til {_dager(hi)} etter møtet"
    return f"{lo}–{hi} dager etter møtet"


def far_protokoll(item: dict) -> bool:
    """Saker that get a public protokoll: not noise, not interpellasjoner (they
    have no decision), not saker exempt from public access."""
    return not (pdf.is_noise(item) or item.get("kategori") == "Interpellasjon"
                or "unntatt offentlighet" in (item.get("tittel") or "").lower())


_laget_cache: dict[tuple[str, int], date | None] = {}


def _laget(path: Path) -> date | None:
    """The day a PDF was made, from its own metadata (local time)."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _laget_cache:
        try:
            made = PdfReader(path).metadata.creation_date
            _laget_cache[key] = (made.astimezone() if made.tzinfo else made).date()
        except Exception:
            _laget_cache[key] = None
    return _laget_cache[key]


def protokoll_tempo(data_dir: Path = config.DATA_DIR) -> dict[str, dict]:
    """Per organ: how many days after the meeting its protokoll was made, from the
    protokoller on disk that cover their meeting (the earliest of up to three per
    meeting). An estimate: the date a file was made is not always the date
    Bergen put it online."""
    out = {}
    for uid in _organer():
        moter = defaultdict(list)
        for i in _items(data_dir, uid):
            if i.get("vedtakFilId") and far_protokoll(i):
                moter[i["moteDato"]].append(i)
        dager = []
        for dato, saker in moter.items():
            laget = [d for i in saker[:3] if pipeline.protokoll_klar(i, data_dir)
                     and (d := _laget(data_dir / "pdf" / pdf.filename(i, "protokoll")))]
            if laget and 0 <= (n := (min(laget) - date.fromisoformat(dato)).days) <= 60:
                dager.append(n)
        if dager:
            out[uid] = {"min": min(dager), "maks": max(dager), "typisk": round(statistics.median(dager)),
                        "moter": len(dager)}
    return out


def _forventet(d: date, tempo: dict | None, idag: date) -> str:
    if not tempo:
        return ""
    vanlig = f"Vanligvis kommer den {_etter(tempo['min'], tempo['maks'])}"
    typisk, senest = d + timedelta(days=tempo["typisk"]), d + timedelta(days=tempo["maks"])
    if typisk > idag:
        return f"{vanlig}: ventet rundt {_dag(typisk)}."
    if typisk == idag:
        return f"{vanlig}: ventet i dag."
    if senest >= idag:
        return f"{vanlig}: ventet senest {_dag(senest)}."
    return f"Det har gått lenger enn vanlig ({_etter(tempo['min'], tempo['maks'])})."


def moter(data_dir: Path = config.DATA_DIR, idag: date | None = None) -> list[dict]:
    """The deciding organs' meetings from the pipeline's window on, newest first,
    each with where it stands: kommende, klar (PDFs ready to download), venter
    (the protokoll doesn't cover the meeting yet), mangler (still none, long
    after the usual time) or ferdig. Meetings with no public saker are left out."""
    idag = idag or date.today()
    fra = pipeline.fra_dato(idag)
    tempo = protokoll_tempo(data_dir)
    nye, pa_nytt = defaultdict(int), defaultdict(int)
    for t in pipeline.pdf_som_mangler(pipeline.saker(data_dir, fra=fra), data_dir):
        (pa_nytt if t.get("oppdater") else nye)[t["filename"].rsplit("/", 1)[0]] += 1
    out = []
    for uid in _organer():
        per_mote = defaultdict(list)
        for i in _items(data_dir, uid):
            if far_protokoll(i) and (i.get("moteDato") or "") >= fra:
                per_mote[i["moteDato"]].append(i)
        t = tempo.get(uid)
        for dato, saker in per_mote.items():
            d, key = date.fromisoformat(dato), f"{uid}/{dato}"
            uten = sum(1 for i in saker if not i.get("vedtakFilId"))
            # Only cards the site shows: a card written from an older protokoll waits.
            redigert = [read_json(data_dir / "redigert" / f"{pdf.key(i)}.json") if pipeline.protokoll_klar(i, data_dir)
                        else None for i in saker]
            m = {
                "organ": ORGAN_KORT.get(uid, uid), "dato": dato, "dag": _dag(d), "saker": len(saker),
                "kort": sum(1 for r in redigert if r and not r.get("avvist")),
                "avvist": sum(1 for r in redigert if r and r.get("avvist")),
                "pdf": nye[key] + pa_nytt[key], "merknad": "",
            }
            if d > idag:
                m["status"] = "kommende"
                if t:
                    m["merknad"] = f"Protokollen kommer vanligvis {_etter(t['typisk'])}."
            elif nye[key]:
                m["status"] = "klar"
                m["merknad"] = f"{nye[key]} nye PDF-er kan lastes ned."
            elif uten or pa_nytt[key]:
                sent = t is not None and idag > d + timedelta(days=t["maks"] + 14)
                m["status"] = "mangler" if sent else "venter"
                if sent:
                    m["merknad"] = f"Ingen protokoll {(idag - d).days} dager etter møtet."
                else:
                    forste = ("Protokollen er ikke lagt ut ennå." if uten == len(saker)
                              else "Protokollen dekker ikke hele møtet ennå.")
                    m["merknad"] = " ".join(filter(None, [forste, _forventet(d, t, idag)]))
            else:
                m["status"] = "ferdig"
            out.append(m)
    return sorted(out, key=lambda m: (m["dato"], m["organ"]), reverse=True)


def neste(moter_: list[dict]) -> dict:
    """The panel's headline: what to do, or which protokoll is due next."""
    klar = [m for m in moter_ if m["status"] == "klar"]
    if klar:
        hvilke = ", ".join(f"{m['organ']} {m['dag']}" for m in reversed(klar))
        return {"type": "klar", "tittel": "Nye dokumenter er klare",
                "tekst": f"{hvilke}. Trykk «Last ned og publiser»."}
    venter = [m for m in moter_ if m["status"] == "venter"]  # newest first
    if venter:
        m = venter[0]
        n = len(venter) - 1
        flere = "" if not n else " Ett møte til venter også." if n == 1 else f" {n} møter til venter også."
        return {"type": "venter", "tittel": f"{m['organ']} {m['dag']} venter på protokoll",
                "tekst": (m["merknad"] or "Protokollen er ikke lagt ut ennå.") + flere}
    kommende = sorted((m for m in moter_ if m["status"] == "kommende"), key=lambda m: m["dato"])
    if kommende:
        m = kommende[0]
        return {"type": "kommende", "tittel": f"Neste møte: {m['organ']} {m['dag']}", "tekst": m["merknad"]}
    return {"type": "ferdig", "tittel": "Ingen møter venter på protokoll", "tekst": ""}


# The site, the machine and the runs

def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _mtime(paths) -> float | None:
    times = [p.stat().st_mtime for p in paths if p.exists()]
    return max(times) if times else None


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _publiserbare(folder: Path) -> int:
    return sum(1 for p in folder.glob("*/*/*.json") if not (read_json(p) or {}).get("avvist"))


def side(root: Path = config.ROOT, data_dir: Path = config.DATA_DIR, site_dir: Path = config.SITE_DIR) -> dict:
    teller = config.GOATCOUNTER
    return {
        "url": config.SITE_URL,
        "skjult": config.SKJUL_FOR_SOK,
        # The local branch once this machine has published; GitHub's copy before that.
        "publisert": (_git(root, "log", "-1", "--format=%cI", config.PAGES_BRANCH)
                      or _git(root, "log", "-1", "--format=%cI", f"origin/{config.PAGES_BRANCH}")),
        "bygget": _iso(_mtime([site_dir / "index.html"])),
        "saker": sum(1 for _ in site_dir.glob("bergen/sak/*/*/*/index.html")),
        "kort": _publiserbare(data_dir / "redigert"),
        "artikler": _publiserbare(data_dir / "artikler-redigert"),
        "rettelser": sum(1 for _ in (data_dir / "rettelser").glob("**/*.json")),
        "auto_publiser": (root / "PUBLISER").exists(),
        "stopp": (root / "STOP").exists(),
        "besok": teller.rsplit("/count", 1)[0] if teller else None,
    }


def sist_hentet(data_dir: Path = config.DATA_DIR) -> float | None:
    return _mtime((data_dir / "agendapunkter").glob("*.json"))


def maskin(root: Path = config.ROOT) -> dict:
    disk = shutil.disk_usage(root)
    # run-daily.sh puts ~/.local/bin on PATH, so look there too.
    path = os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")])
    return {
        "navn": socket.gethostname(),
        "disk_ledig_gb": round(disk.free / 1e9, 1),
        "disk_brukt": round(100 * disk.used / disk.total),
        "claude": bool(shutil.which("claude", path=path)),
        "codex": bool(shutil.which("codex", path=path)),
        "reserve": config.FAKTASJEKKER_RESERVE,
        "reserve_innsats": config.FAKTASJEKKER_RESERVE_EFFORT,
    }


def laast(lock: Path) -> bool:
    """True while a run holds run-daily.sh's lock."""
    try:
        f = lock.open("rb")
    except OSError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


# What a run did, in words (owner 1.11: «Sjekk nå» seemed to give nothing, and the
# log's «klassifiser: 87 saker» read like 87 new saker). Read from run-daily.sh's log.
_HENT = re.compile(r"INFO (.+?) \d{4}: \d+ items, \{'ny': (\d+), 'vedtatt': (\d+)")
_HENT_HOPPET = "hent: the agenda was fetched less than 30 minutes ago"
_PDF = re.compile(r"(\d+) PDF\(s\) to fetch across (\d+) meeting")
_RESULTAT = re.compile(r"INFO (skriv|artikkel|omskriv)( artikkel)?: (\{.*\})$")
_VEDLEGGSLISTER = re.compile(r"vedleggslister: (\d+) saker, (\d+) vedlegg")
_VEDLEGG_LEST = re.compile(r"INFO vedlegg: \{'antall': \{[^}]*'lest': (\d+)")
_BYGD = re.compile(r"INFO built .*?\{'sider': \d+, 'saker': (\d+), 'fremhevet': (\d+)")
_INDEXNOW = re.compile(r"IndexNow: (\d+) sider meldt")
# Codex (ChatGPT plan) out of quota: «You’ve hit your usage limit. … try again at Nov 4th, 2026 8:38 AM.»
_CODEX_KVOTE = re.compile(r"hit your usage limit.*?try again at ([A-Z][a-z]{2})\w* (\d{1,2})\w*, \d{4},? (\d{1,2}):(\d\d) ([AP]M)")
_MANEDER = dict(zip(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), MONTHS))
_GRENSE = re.compile(r"hit your [\w-]+ limit[^\n\"']*?resets (\d{1,2})(?::(\d\d))?\s?([ap]m)")


def _flertall(n: int, en: str, flere: str) -> str:
    return f"{n} {en if n == 1 else flere}"


def oppsummer(run: list[str], forrige_bygg: tuple[int, int] | None = None) -> dict:
    """A run's log lines as short sentences for the panel: what came from Bergen
    kommune, what was written, built and published (punkter), and what went wrong
    or needs a person (advarsler)."""
    tekst = "\n".join(run)
    punkter, advarsler = [], []
    nye = [(m.group(1), int(m.group(2)) + int(m.group(3))) for m in _HENT.finditer(tekst)]
    if _HENT_HOPPET in tekst:
        punkter.append("Agendaen var hentet for under 30 minutter siden, så den ble ikke hentet på nytt.")
    elif nye:
        endret = [(organ, n) for organ, n in nye if n]
        punkter.append("Nytt hos Bergen kommune: " + ", ".join(f"{organ} ({n})" for organ, n in endret) + "."
                       if endret else "Ingen nye saker eller vedtak hos Bergen kommune.")
    if m := _PDF.search(tekst):
        n, moter_ = int(m.group(1)), _flertall(int(m.group(2)), "møte", "møter")
        if not n:
            punkter.append("Ingen nye PDF-er å hente.")
        elif "start (manuell)" in run[0] and "PDF download stopped" not in tekst:
            punkter.append(f"Hentet {_flertall(n, 'PDF', 'PDF-er')} fra {moter_}.")
        else:
            punkter.append(f"{_flertall(n, 'PDF', 'PDF-er')} fra {moter_} kan hentes med «Last ned og publiser».")
    if m := _VEDLEGGSLISTER.search(tekst):
        punkter.append(f"Fant {_flertall(int(m.group(2)), 'vedlegg', 'vedlegg')} til "
                       f"{_flertall(int(m.group(1)), 'ny sak', 'nye saker')}.")
    if m := _VEDLEGG_LEST.search(tekst):
        punkter.append(f"Leste vedleggene til {_flertall(int(m.group(1)), 'sak', 'saker')} og plukket ut sidene "
                       "skribenten trenger.")
    # The steps' result lines: «skriv: {'antall': {'skrevet': 84, 'feil': 3}, …}» for new
    # work, «omskriv: {'omskriv': {'antall': {'omskrevet': 4}}, …}» for rewrites.
    nytt, omskrevet, feilet = {"skriv": 0, "artikkel": 0}, {"skriv": 0, "artikkel": 0}, 0
    for linje in run:
        if not (m := _RESULTAT.search(linje)):
            continue
        try:
            resultat = ast.literal_eval(m.group(3))
        except (ValueError, SyntaxError):
            continue
        if m.group(1) == "omskriv":
            antall = next((v.get("antall", {}) for v in resultat.values() if isinstance(v, dict)), {})
            omskrevet["artikkel" if m.group(2) else "skriv"] += sum(
                n for k, n in antall.items() if k.startswith("omskrevet"))
            feilet += antall.get("feil", 0)
        else:
            antall = resultat.get("antall", {})
            nytt[m.group(1)] += sum(n for k, n in antall.items() if k.startswith("skrevet"))
            feilet += antall.get("feil", 0)
    if nytt["skriv"]:
        punkter.append(f"Skrev {_flertall(nytt['skriv'], 'nytt kort', 'nye kort')}.")
    if omskrevet["skriv"]:
        punkter.append(f"Skrev om {_flertall(omskrevet['skriv'], 'avvist kort', 'avviste kort')}.")
    if nytt["artikkel"]:
        punkter.append(f"Skrev {_flertall(nytt['artikkel'], 'bakgrunnsartikkel', 'bakgrunnsartikler')}.")
    if omskrevet["artikkel"]:
        punkter.append(f"Skrev om {_flertall(omskrevet['artikkel'], 'avvist artikkel', 'avviste artikler')}.")
    if "== kjor" in tekst and not any(nytt.values()) and not any(omskrevet.values()) and not feilet:
        punkter.append("Ingen nye kort eller artikler å skrive.")
    bygd = [(int(m.group(1)), int(m.group(2))) for m in _BYGD.finditer(tekst)]
    if bygd:
        saker, fremhevet = bygd[-1]
        endring = ""
        if forrige_bygg and forrige_bygg[1] != fremhevet:
            endring = f" (før {forrige_bygg[1]})"
        punkter.append(f"Siden har {saker} saker, {fremhevet} av dem fremhevet{endring}.")
    if "asked statichost to deploy" in tekst:
        punkter.append("Publisert på tingposten.no.")
    elif "PUBLISER not set" in tekst:
        punkter.append("Bygget, men ikke publisert: daglig publisering er av.")
    elif "STOP found" in tekst:
        advarsler.append("STOP er satt, så ingenting ble publisert.")
    if m := _INDEXNOW.search(tekst):
        punkter.append(f"Meldte {m.group(1)} sider til Bing (IndexNow).")

    if "codex" in tekst and "401 Unauthorized" in tekst:
        advarsler.append("Codex er logget ut på Pi-en, så Claude tok over faktasjekken. Logg inn igjen: "
                         "ssh til Pi-en og kjør «codex login».")
    elif m := _CODEX_KVOTE.search(tekst):
        time = int(m.group(3)) % 12 + (12 if m.group(5) == "PM" else 0)
        dag = f"{int(m.group(2))}. {_MANEDER.get(m.group(1), m.group(1))}"
        advarsler.append(f"Codex har brukt opp kvoten sin til {dag} kl. {time:02d}.{m.group(4)}, så Claude tar "
                         "faktasjekken så lenge. Ingenting å gjøre, med mindre du vil kjøpe mer på "
                         "chatgpt.com/settings/usage.")
    elif "takes over for the rest of this run" in tekst:
        advarsler.append("Faktasjekken byttet til reservemodellen underveis.")
    if feilet:
        punkter.append(f"{_flertall(feilet, 'sak', 'saker')} kunne ikke skrives denne gangen.")
    if "Invalid API key" in tekst:
        advarsler.append("Claude avviste API-nøkkelen. ANTHROPIC_API_KEY må være tom på Pi-en, så abonnementet "
                         "brukes.")
    if m := _GRENSE.search(tekst):
        time = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        advarsler.append(f"Claude-abonnementet nådde grensen sin, som nullstilles kl. {time:02d}.{m.group(2) or '00'}. "
                         "Neste kjøring skriver det som mangler.")
    elif "warning: kjor exit" in tekst:
        advarsler.append("Skrivingen stoppet før den var ferdig. Neste kjøring skriver det som mangler.")
    if "server keeps refusing us" in tekst:
        advarsler.append("Bergen kommune avviste oss. Prøv igjen i morgen.")
    elif "hent failed" in tekst:
        advarsler.append("Agendaen kunne ikke hentes. Se loggen.")
    if "bygg failed" in tekst:
        advarsler.append("Siden kunne ikke bygges, så den ble ikke publisert. Se loggen.")
    if "IndexNow svarte" in tekst:
        advarsler.append("Bing (IndexNow) avviste varselet. Siden er publisert likevel.")
    if "PDF download stopped" in tekst:
        advarsler.append("Nedlastingen av PDF-er stoppet underveis. Neste kjøring tar resten.")
    return {"punkter": punkter, "advarsler": advarsler}


def _forrige_bygg(logs: list[Path], for_kjoringen: list[str]) -> tuple[int, int] | None:
    """(saker, fremhevet) from the last build before this run, for «før 52»."""
    for tekst in ["\n".join(for_kjoringen)] + [p.read_text(errors="replace") for p in reversed(logs[-4:-1])]:
        if bygd := _BYGD.findall(tekst):
            return int(bygd[-1][0]), int(bygd[-1][1])
    return None


def siste_kjoring(log_dir: Path) -> dict | None:
    """The last run in run-daily.sh's newest log: mode, start, current step,
    whether it got to the end, its last lines, and what it did (oppsummer)."""
    logs = sorted(p for p in log_dir.glob("*.log") if re.fullmatch(r"\d{4}-\d\d-\d\d\.log", p.name))
    if not logs:
        return None
    lines = logs[-1].read_text(errors="replace").splitlines()
    starts = [n for n, line in enumerate(lines) if _START.match(line)]
    if not starts:
        return None
    run = lines[starts[-1]:]
    start = _START.match(run[0])
    steg = [m.group(1) for line in run if (m := _STEG.match(line))]
    return {
        "modus": start.group(2) or "daglig",
        "startet": start.group(1),
        "steg": steg[-1] if steg else None,
        "ferdig": any(line.endswith(" done") for line in run),
        "slutt": run[-1].split(" ", 1)[0] if len(run) > 1 else None,
        "logg": run[-LOGG_LINJER:],
        "oppsummering": oppsummer(run, _forrige_bygg(logs, lines[:starts[-1]])),
    }


class Kjorer:
    """Starts deploy/run-daily.sh in the background, one run at a time."""

    def __init__(self, root: Path = config.ROOT, data_dir: Path = config.DATA_DIR, kommando=None):
        self.root, self.data_dir = root, data_dir
        self.kommando = kommando or (lambda modus: ["bash", str(root / "deploy" / "run-daily.sh"), f"--{modus}"])
        self._lock = threading.Lock()
        self._proc = None

    @property
    def lock(self) -> Path:
        return self.root / "data" / "run-daily.lock"

    def pagar(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None) or laast(self.lock)

    def sjekk_mulig(self, na: datetime | None = None) -> datetime | None:
        """When «Sjekk nå» may run again (None: now)."""
        sist = sist_hentet(self.data_dir)
        if sist is None:
            return None
        neste = datetime.fromtimestamp(sist, timezone.utc) + SJEKK_PAUSE
        return neste if neste > (na or datetime.now(timezone.utc)) else None

    def start(self, modus: str) -> tuple[int, str]:
        with self._lock:
            if self.pagar():
                return 409, "En kjøring pågår allerede."
            if modus == "sjekk" and (neste := self.sjekk_mulig()):
                return 429, f"Agendaen ble nettopp hentet. Du kan sjekke igjen kl. {neste.astimezone():%H:%M}."
            log_dir = self.root / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # Without what the panel read from .env at its start, so the run reads .env as it is now.
            env = {k: v for k, v in os.environ.items() if k not in config.FRA_DOTENV}
            with (log_dir / "admin.log").open("ab") as out:
                self._proc = subprocess.Popen(self.kommando(modus), cwd=self.root, stdin=subprocess.DEVNULL, env=env,
                                              stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            threading.Thread(target=self._proc.wait, daemon=True).start()  # no zombie when it ends
            log.info("admin: started %s run (pid %d)", modus, self._proc.pid)
            return 202, "Startet."


_ARBEID: dict = {"tid": None, "verdi": None, "data_dir": None, "regner": False}
_ARBEID_LAAS = threading.Lock()
ARBEID_CACHE = timedelta(minutes=2)
_UKJENT = {"kort": None, "omskriv": None, "artikler": None}


def _tell_arbeid(items: list[dict], data_dir: Path) -> dict:
    try:
        return {
            "kort": len(pipeline.todo("skriv", items, data_dir)),
            "omskriv": len(pipeline.omskriv_kandidater(items, data_dir)),
            "artikler": len(pipeline.todo("artikkel", pipeline.velg_artikler(items, data_dir), data_dir)),
        }
    except Exception:  # the panel must load even if the pipeline's bookkeeping trips
        log.exception("admin: could not count the work waiting")
        return dict(_UKJENT)


def arbeid(items: list[dict], data_dir: Path, vent: bool = False) -> dict:
    """What «Last ned og publiser» would write with the PDFs already here: new cards,
    rejected cards to rewrite, background articles. Counting reads many files (seconds
    on the Pi), so it runs in the background and the panel shows the last count
    (None until the first is done); `vent` counts at once (tests)."""
    na = datetime.now(timezone.utc)
    with _ARBEID_LAAS:
        fersk = _ARBEID["tid"] and na - _ARBEID["tid"] < ARBEID_CACHE and _ARBEID["data_dir"] == data_dir
        if fersk or _ARBEID["regner"]:
            return _ARBEID["verdi"] if _ARBEID["data_dir"] == data_dir and _ARBEID["verdi"] else dict(_UKJENT)
        _ARBEID["regner"] = True

    def regn():
        verdi = _tell_arbeid(items, data_dir)
        with _ARBEID_LAAS:
            _ARBEID.update(tid=datetime.now(timezone.utc), verdi=verdi, data_dir=data_dir, regner=False)

    if vent:
        regn()
        return _ARBEID["verdi"]
    threading.Thread(target=regn, name="arbeid", daemon=True).start()
    return _ARBEID["verdi"] if _ARBEID["data_dir"] == data_dir and _ARBEID["verdi"] else dict(_UKJENT)


def status(kjorer: Kjorer, site_dir: Path = config.SITE_DIR, idag: date | None = None) -> dict:
    root, data_dir = kjorer.root, kjorer.data_dir
    idag = idag or date.today()
    moter_ = moter(data_dir, idag)
    items = pipeline.saker(data_dir, fra=pipeline.fra_dato(idag))
    mangler = pipeline.pdf_som_mangler(items, data_dir)
    neste_sjekk = kjorer.sjekk_mulig()
    return {
        "na": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "neste": neste(moter_),
        "moter": moter_,
        "side": side(root, data_dir, site_dir),
        "gjenstar": {
            "pdf": len(mangler),
            "pdf_pa_nytt": sum(1 for t in mangler if t.get("oppdater")),
            "pdf_minutter": max(1, round(len(mangler) * PDF_SEKUNDER / 60)) if mangler else 0,
            "uten_kort": sum(1 for i in items if not (data_dir / "redigert" / f"{pdf.key(i)}.json").exists()),
            **arbeid(items, data_dir),
        },
        "sist_hentet": _iso(sist_hentet(data_dir)),
        "sjekk_mulig": neste_sjekk.isoformat(timespec="seconds") if neste_sjekk else None,
        "kjoring": {"pagar": kjorer.pagar(), "siste": siste_kjoring(root / "data" / "logs")},
        "maskin": maskin(root),
    }


# The web server

def _loopback(vert: str) -> bool:
    try:
        return ipaddress.ip_address(vert).is_loopback
    except ValueError:
        return vert == "localhost"


def lokal_vert(host: str) -> bool:
    """A Host header the panel answers to: an IP address or a name on the home
    network (no dots, .local, .lan, .home.arpa). Anything else could be a
    public name pointed at the Pi to read the panel from another site."""
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    name = name.rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return bool(name) and ("." not in name or name == "localhost"
                           or name.endswith((".local", ".lan", ".home.arpa", ".localhost")))


class DebattKo:
    """The comment server's editor queue, when there is a comment database: comments the
    moderator was unsure about (or a reader asked about) and reported ones. The panel
    shows the author's display name, never the e-mail address."""

    HANDLINGER = ("publiser", "fjern", "sperr")

    def __init__(self, db_path: Path | None = None):
        self.path = db_path or config.DEBATT_DIR / "debatt.db"
        self._tjeneste = None

    def tjeneste(self):
        if self._tjeneste is None and self.path.exists():
            from .debatt.db import Db
            from .debatt.tjeneste import Tjeneste

            self._tjeneste = Tjeneste(Db(self.path), None, {}, None, config.SITE_URL)
        return self._tjeneste

    def status(self) -> dict:
        t = self.tjeneste()
        if t is None:
            return {"aktiv": False, "ko": [], "stoppet": []}
        return {"aktiv": True, "ko": [self._rad(r) for r in t.redaktor_ko()],
                "stoppet": [self._rad(r) for r in t.nylig_stoppet()]}

    @staticmethod
    def _rad(r: dict) -> dict:
        """One comment for the panel: the moderators' last verdict, the link, no e-mail."""
        r = dict(r)
        data = json.loads(r.pop("vurdering", None) or "{}")
        r["vurderinger"] = [{"modell": v.get("modell"), "regel": v.get("regel"),
                             "sikkerhet": (v.get("sannsynligheter") or {}).get(v.get("regel")),
                             "begrunnelse": v.get("begrunnelse") or ""} for v in data.get("vurderinger", [])]
        r["feil"] = data.get("feil", [])
        r.setdefault("grunner", [])
        r["lenke"] = f"{config.SITE_URL}bergen/sak/{r['sak']}/#k{r['id']}"
        return r

    def handle(self, body: dict) -> tuple[int, dict]:
        from .debatt.regler import REGLER
        from .debatt.tjeneste import Feil

        t = self.tjeneste()
        kid, handling, regel = body.get("id"), body.get("handling"), body.get("regel")
        if t is None or type(kid) is not int or handling not in self.HANDLINGER or (regel and regel not in REGLER):
            return 400, {"melding": "Ugyldig."}
        try:
            if handling == "publiser":
                # Only the text the panel showed: an edit since then gives 409, never a blind publish.
                t.redaktor_avgjor(kid, "publiser", tekst=body.get("tekst"))
            else:
                t.redaktor_avgjor(kid, "fjern", regel or None)
                if handling == "sperr":
                    t.sperr(kid)
        except Feil as e:
            return e.kode, {"melding": e.melding}
        return 200, {"melding": {"publiser": "Publisert.", "fjern": "Fjernet.", "sperr": "Fjernet, og forfatteren er sperret."}[handling]}


def debatt_miljo(root: Path = config.ROOT) -> dict:
    """The comment server's settings (deploy/debatt/debatt.env): the panel sends the approval
    and "published" e-mails with the same SMTP account."""
    path = root / "deploy" / "debatt" / "debatt.env"
    miljo = {}
    if path.exists():
        for linje in path.read_text().splitlines():
            k, sep, v = linje.partition("=")
            if sep and not k.lstrip().startswith("#"):
                miljo[k.strip()] = v.strip()
    return miljo


class InnleggKo:
    """Debattinnlegg sent through the form (debatt/innsendt.py): the AI check in a background
    thread, then the editor's queue. Publishing writes data/bergen/innlegg/ and starts a
    build-and-publish run (run-daily.sh --publiser)."""

    HANDLINGER = ("publiser", "send", "avvis", "hent_tilbake", "ny")
    PAUSE_S = 120

    def __init__(self, debatt: "DebattKo", kjorer: Kjorer, modell=None, sender=None, data_dir: Path = config.DATA_DIR):
        self.debatt, self.kjorer, self.data_dir = debatt, kjorer, data_dir
        self._modell, self._sender = modell, sender
        self._stopp = threading.Event()

    def db(self):
        t = self.debatt.tjeneste()
        return t.db if t else None

    def modell(self):
        if self._modell is None:
            from .debatt import innleggsjekk

            self._modell = innleggsjekk.backend()
        return self._modell

    def sender(self):
        """(send(msg), avsender) from deploy/debatt/debatt.env, or (None, None)."""
        if self._sender is not None:
            return self._sender
        from .debatt import epost

        miljo = debatt_miljo()
        return epost.smtp_fra_miljo(miljo), miljo.get("DEBATT_AVSENDER") or config.KONTAKT_EPOST

    # --- the AI check ---------------------------------------------------------------------

    def sjekk(self) -> int:
        """Check every innlegg that waits for it; returns how many were checked."""
        from .debatt import innleggsjekk, innsendt
        from .llm import LLMError

        db = self.db()
        if db is None:
            return 0
        n = 0
        for rad in innsendt.til_vurdering(db):
            try:
                svar = innleggsjekk.vurder(self.modell(), rad)
            except (LLMError, OSError) as e:
                log.warning("innlegg %d: the AI check failed: %s", rad["id"], e)
                innsendt.ki_feilet(db, rad["id"], str(e))
                continue
            status = innsendt.sett_ki(db, rad["id"], svar, self.modell().name)
            log.info("innlegg %d: %s (%s)", rad["id"], status, svar.get("kategori"))
            n += 1
        return n

    def start(self) -> None:
        def loop():
            while not self._stopp.wait(self.PAUSE_S):
                try:
                    self.sjekk()
                except Exception:  # noqa: BLE001  the panel must keep running
                    log.exception("the innlegg check stopped this round")
        threading.Thread(target=loop, name="innleggsjekk", daemon=True).start()

    # --- the editor -----------------------------------------------------------------------

    def status(self) -> dict:
        from .debatt import innsendt

        db = self.db()
        return {"aktiv": db is not None, **(innsendt.ko(db) if db else {"ko": [], "sortert_bort": []})}

    def _publiser(self, tittel, forfatter, tekst, om="", sak=None) -> tuple[Path, str]:
        from . import innlegg

        path = innlegg.ny(tittel, forfatter, tekst, om=om or "", sak=sak, data_dir=self.data_dir)
        code, melding = self.kjorer.start("publiser")
        bygg = "Bygger og publiserer nå." if code == 202 else f"Lagret; publiseres ved neste kjøring ({melding})"
        return path, bygg

    def handle(self, body: dict) -> tuple[int, dict]:
        from .debatt import epost, innsendt
        from .debatt.tjeneste import Feil

        handling = body.get("handling")
        if handling not in self.HANDLINGER:
            return 400, {"melding": "Ugyldig."}
        try:
            if handling == "ny":  # an innlegg that came by e-mail: the editor pastes it in
                path, bygg = self._publiser(body.get("tittel") or "", body.get("forfatter") or "", body.get("tekst") or "",
                                            body.get("om") or "", body.get("sak") or None)
                return 200, {"melding": f"Lagret som {path.name}. {bygg}"}
            db = self.db()
            if db is None:
                return 400, {"melding": "Ingen kommentardatabase."}
            rad = innsendt.hent(db, body.get("id"))
            if handling == "avvis":
                innsendt.sett_status(db, rad["id"], "avvist")
                return 200, {"melding": "Avvist."}
            if handling == "hent_tilbake":
                innsendt.sett_status(db, rad["id"], "til_redaktor")
                return 200, {"melding": "Lagt i køen."}
            send_msg, avsender = self.sender()
            til = innsendt.epost(db, rad["id"])
            if handling == "send":
                if not (send_msg and til):
                    return 400, {"melding": "Kan ikke sende e-post (mangler SMTP eller skribentens konto)."}
                token = innsendt.til_godkjenning(db, rad["id"], body.get("tittel"), body.get("tekst"))
                ny = innsendt.hent(db, rad["id"])
                lenke = f"{config.SITE_URL}bergen/debatt/godkjenn/#t={token}"
                send_msg(epost.melding_godkjenning(til, avsender, ny["red_tittel"], ny["red_tekst"], lenke))
                return 200, {"melding": "Sendt til skribenten for godkjenning."}
            # publiser: the text the panel showed (the editor's version if any, else the writer's)
            if rad["status"] not in ("til_redaktor", "sendt_skribent", "godkjent"):
                return 409, {"melding": "Innlegget kan ikke publiseres nå."}
            tittel, tekst = innsendt.til_publisering(rad)
            if body.get("tittel") not in (None, tittel) or body.get("tekst") not in (None, tekst):
                return 409, {"melding": "Teksten er endret. Send den til skribenten først, eller last siden på nytt."}
            path, bygg = self._publiser(tittel, rad["navn"], tekst, rad["om"], rad["sak"])
            innsendt.sett_status(db, rad["id"], "publisert", path.name)
            if send_msg and til:
                try:
                    send_msg(epost.melding_publisert(til, avsender, tittel, f"{config.SITE_URL}bergen/debatt/{path.stem}/"))
                except OSError as e:
                    log.warning("innlegg %d: the 'published' e-mail failed: %s", rad["id"], e)
            return 200, {"melding": f"Publisert som {path.name}. {bygg}"}
        except Feil as e:
            return e.kode, {"melding": e.melding}
        except (ValueError, FileExistsError) as e:
            return 400, {"melding": str(e)}
        except OSError as e:
            log.warning("innlegg: %s", e)
            return 502, {"melding": "E-posten kunne ikke sendes. Prøv igjen."}


def make_server(vert: str = "127.0.0.1", port: int = 8787, passord: str | None = None,
                kjorer: Kjorer | None = None, site_dir: Path = config.SITE_DIR,
                idag: date | None = None, debatt: DebattKo | None = None,
                innlegg: InnleggKo | None = None) -> ThreadingHTTPServer:
    if not passord and not _loopback(vert):
        raise ValueError(f"set {PASSORD_ENV} in .env before the panel listens on {vert}")
    kjorer = kjorer or Kjorer()
    debatt = debatt or DebattKo()
    innlegg = innlegg or InnleggKo(debatt, kjorer)
    page = SIDE.read_bytes()
    expected = passord.encode() if passord else b""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self._tillatt():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/status.json":
                self._json(200, status(kjorer, site_dir, idag))
            elif path == "/debatt.json":
                self._json(200, debatt.status())
            elif path == "/innlegg.json":
                self._json(200, innlegg.status())
            elif path.startswith("/static/"):
                self._static(path.removeprefix("/static/"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"Finnes ikke")

        def do_POST(self):
            if not self._tillatt():
                return
            origin = self.headers.get("Origin")
            if self.headers.get("X-Tingposten") != "admin" or (
                    origin and urlsplit(origin).netloc != self.headers.get("Host")):
                return self._json(403, {"melding": "Avvist."})
            sti = urlsplit(self.path).path
            if sti not in ("/start", "/debatt", "/innlegg"):
                return self._json(404, {"melding": "Finnes ikke."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 40_000 if sti == "/innlegg" else 1000)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                body = {}
            if sti == "/debatt":
                code, svar = debatt.handle(body)
                return self._json(code, svar)
            if sti == "/innlegg":
                code, svar = innlegg.handle(body)
                return self._json(code, svar)
            modus = body.get("modus")
            if modus not in MODUSER:
                return self._json(400, {"melding": "Ukjent kjøring."})
            code, melding = kjorer.start(modus)
            self._json(code, {"melding": melding})

        def _tillatt(self) -> bool:
            if not lokal_vert(self.headers.get("Host", "")):
                self._send(403, "text/plain; charset=utf-8", b"Ukjent vertsnavn")
                return False
            if not expected:
                return True
            auth = self.headers.get("Authorization", "")
            given = b""
            if auth.startswith("Basic "):
                try:
                    given = base64.b64decode(auth[6:], validate=True).partition(b":")[2]
                except ValueError:
                    pass
            if hmac.compare_digest(given, expected):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Tingposten admin", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _static(self, name: str) -> None:
            path = (STATIC / name).resolve()
            if STATIC.resolve() not in path.parents or path.suffix not in STATIC_TYPER or not path.is_file():
                return self._send(404, "text/plain; charset=utf-8", b"Finnes ikke")
            self._send(200, STATIC_TYPER[path.suffix], path.read_bytes(), cache="max-age=86400")

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode())

        def _send(self, code: int, ctype: str, body: bytes, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                        "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("admin: " + fmt, *args)

    server = ThreadingHTTPServer((vert, port), Handler)
    server.innlegg = innlegg  # the CLI starts its AI check thread (tests don't)
    return server
