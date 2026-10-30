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
MODUSER = ("manuell", "sjekk")
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
        "publisert": _git(root, "log", "-1", "--format=%cI", config.PAGES_BRANCH),
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


def siste_kjoring(log_dir: Path) -> dict | None:
    """The last run in run-daily.sh's newest log: mode, start, current step,
    whether it got to the end, and its last lines."""
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
            with (log_dir / "admin.log").open("ab") as out:
                self._proc = subprocess.Popen(self.kommando(modus), cwd=self.root, stdin=subprocess.DEVNULL,
                                              stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            threading.Thread(target=self._proc.wait, daemon=True).start()  # no zombie when it ends
            log.info("admin: started %s run (pid %d)", modus, self._proc.pid)
            return 202, "Startet."


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


def make_server(vert: str = "127.0.0.1", port: int = 8787, passord: str | None = None,
                kjorer: Kjorer | None = None, site_dir: Path = config.SITE_DIR,
                idag: date | None = None) -> ThreadingHTTPServer:
    if not passord and not _loopback(vert):
        raise ValueError(f"set {PASSORD_ENV} in .env before the panel listens on {vert}")
    kjorer = kjorer or Kjorer()
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
            if urlsplit(self.path).path != "/start":
                return self._json(404, {"melding": "Finnes ikke."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                modus = json.loads(self.rfile.read(min(length, 1000)) or b"{}").get("modus")
            except (ValueError, AttributeError):
                modus = None
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

    return ThreadingHTTPServer((vert, port), Handler)
