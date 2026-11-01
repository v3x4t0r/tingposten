"""The comment server's HTTP side (Starlette, run by uvicorn behind Tailscale Funnel).

Readers' browsers call it from tingposten.no pages, so it lives on another site
(…ts.net): sessions are bearer tokens the page keeps, not cookies, which Safari
and Firefox would block as third-party. CORS allows only the site's own origins.
Request bodies are capped; every error a reader can fix comes back as
{"feil": "<Norwegian message>"} with a fitting status code.

  POST   /api/innlogging              {epost, tilbake}   send a login link
  POST   /api/innlogging/bekreft      {token}            → {okt, navn, tilbake}
  POST   /api/utlogging               {alle}
  GET    /api/meg                     the account (name, created, quiz passed for these rules)
  GET    /api/quiz                    the quiz questions (no answers)
  POST   /api/quiz                    {svar: {id: bool}} → right or wrong and why; all right = may write
  POST   /api/meg                     {navn}             choose a display name
  DELETE /api/meg                     delete the account and everything in it
  GET    /api/meg/eksport             everything stored about the reader (JSON)
  GET    /api/sak/{sak:path}/kommentarer?sortering=best|topp|nye
  GET    /api/mest-kommentert?periode=7|30|aar   the most commented saker (the Debatt page)
  POST   /api/kommentar               {sak, tekst, forelder}
  GET    /api/kommentar/{id}          (the author polls this while it is judged)
  PUT    /api/kommentar/{id}          {tekst}
  DELETE /api/kommentar/{id}
  POST   /api/kommentar/{id}/stem     {verdi: 1 | -1 | 0}
  POST   /api/kommentar/{id}/rapporter {grunn}
  POST   /api/kommentar/{id}/manuell  ask the editor to look at a stopped comment
"""

import ipaddress
import json
import logging
import os
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .regler import REGLER_VERSJON
from .tjeneste import Feil, Tjeneste

log = logging.getLogger(__name__)

MAKS_KROPP = 8_000  # bytes; a comment is at most 3000 characters
IP_GRENSE = 60      # requests that write, per IP and 10 minutes (memory only, never stored)
IP_GRENSE_LENKER = 5  # login e-mails asked for, per IP and 10 minutes


class IpTeller:
    """Requests per address over a sliding window, kept in memory only, for at most
    `maks` addresses (the least recently seen are forgotten first)."""

    def __init__(self, grense: int = IP_GRENSE, vindu_s: int = 600, maks: int = 10_000):
        self.grense, self.vindu, self.maks = grense, vindu_s, maks
        self._tider: OrderedDict[str, deque] = OrderedDict()

    def for_mange(self, ip: str) -> bool:
        na = time.monotonic()
        q = self._tider.pop(ip, None) or deque()
        while q and q[0] < na - self.vindu:
            q.popleft()
        q.append(na)
        self._tider[ip] = q  # now the most recent
        while len(self._tider) > self.maks:
            self._tider.popitem(last=False)
        return len(q) > self.grense


def _ip(request: Request) -> str:
    """The reader's address, as one key for the limits. Behind Funnel the peer is
    tailscaled on loopback, which appends the real address last to X-Forwarded-For
    (earlier values come from the client and can't be trusted). IPv6 addresses
    count per /64, since one connection usually has a whole /64 to pick from."""
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1") and (fwd := request.headers.get("x-forwarded-for")):
        peer = fwd.split(",")[-1].strip()
    try:
        adr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if adr.version == 6:
        if adr.ipv4_mapped:
            return str(adr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{adr}/64", strict=False))
    return str(adr)


def lag_app(tjeneste: Tjeneste, opprinnelser: list[str], start_ko: bool = True) -> Starlette:
    teller = IpTeller(IP_GRENSE)
    lenketeller = IpTeller(IP_GRENSE_LENKER)

    async def kropp(request: Request) -> dict:
        if int(request.headers.get("content-length") or 0) > MAKS_KROPP:
            raise Feil(413, "For stor forespørsel.")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise Feil(415, "Forventet JSON.")
        data = b""
        async for bit in request.stream():
            data += bit
            if len(data) > MAKS_KROPP:
                raise Feil(413, "For stor forespørsel.")
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            raise Feil(400, "Ugyldig JSON.") from None
        if not isinstance(body, dict):
            raise Feil(400, "Ugyldig JSON.")
        return body

    def okt(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else None

    def innlogget(request: Request):
        b = tjeneste.bruker(okt(request))
        if b is None:
            raise Feil(401, "Du må logge inn.")
        return b

    def svar(data, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    def endepunkt(fn, skriver: bool = False):
        async def handler(request: Request):
            try:
                # Reading (and polling a comment that is being judged) never counts against the limit.
                if skriver and request.method != "GET" and teller.for_mange(_ip(request)):
                    raise Feil(429, "For mange forespørsler. Vent litt.")
                return await fn(request)
            except Feil as e:
                return svar({"feil": e.melding}, e.kode)
        return handler

    def kid(request: Request) -> int:
        try:
            return int(request.path_params["id"])
        except ValueError:
            raise Feil(404, "Fant ikke kommentaren.") from None

    async def be_om_lenke(request):
        if lenketeller.for_mange(_ip(request)):
            raise Feil(429, "Du har bedt om mange lenker. Vent litt.")
        body = await kropp(request)
        lenke = await _i_traad(tjeneste.be_om_lenke, body.get("epost"), body.get("tilbake"))
        return svar({"sendt": True, **({"lenke": lenke} if lenke else {})})

    async def bekreft(request):
        return svar(tjeneste.bekreft((await kropp(request)).get("token")))

    async def logg_ut(request):
        body = await kropp(request)
        if token := okt(request):
            tjeneste.logg_ut(token, alle=body.get("alle") is True)
        return svar({"ok": True})

    async def meg(request):
        b = innlogget(request)
        if request.method == "GET":
            return svar({"navn": b.navn, "opprettet": b.opprettet, "quiz": b.quiz == REGLER_VERSJON})
        if request.method == "DELETE":
            tjeneste.db.slett_bruker(b.id)
            return svar({"slettet": True})
        navn = await _i_traad(tjeneste.sett_navn, b, (await kropp(request)).get("navn"))
        return svar({"navn": navn})

    async def quiz(request):
        if request.method == "GET":
            return JSONResponse(tjeneste.quiz(), headers={"Cache-Control": "public, max-age=300"})
        return svar(tjeneste.svar_quiz(innlogget(request), (await kropp(request)).get("svar")))

    async def eksport(request):
        return svar(tjeneste.db.eksport(innlogget(request).id))

    async def trad(request):
        b = tjeneste.bruker(okt(request))
        data = tjeneste.trad(request.path_params["sak"], b, request.query_params.get("sortering", "best"))
        headers = {"Cache-Control": "no-store" if b else "public, max-age=15"}
        return JSONResponse(data, headers=headers)

    async def mest_kommentert(request):
        data = tjeneste.mest_kommentert(request.query_params.get("periode", "7"))
        return JSONResponse(data, headers={"Cache-Control": "public, max-age=60"})

    async def ny(request):
        b = innlogget(request)
        body = await kropp(request)
        return svar(tjeneste.ny_kommentar(b, body.get("sak"), body.get("tekst"), body.get("forelder")), 201)

    async def kommentar(request):
        if request.method == "GET":
            return svar(tjeneste.kommentar(kid(request), tjeneste.bruker(okt(request))))
        b = innlogget(request)
        if request.method == "DELETE":
            tjeneste.slett(b, kid(request))
            return svar({"slettet": True})
        return svar(tjeneste.endre(b, kid(request), (await kropp(request)).get("tekst")))

    async def stem(request):
        b = innlogget(request)
        return svar(tjeneste.stem(b, kid(request), (await kropp(request)).get("verdi")))

    async def rapporter(request):
        b = innlogget(request)
        tjeneste.rapporter(b, kid(request), (await kropp(request)).get("grunn"))
        return svar({"rapportert": True})

    async def manuell(request):
        return svar(tjeneste.be_om_manuell(innlogget(request), kid(request)))

    async def helse(request):
        return svar({"ok": True})

    ruter = [
        Route("/api/helse", endepunkt(helse)),
        Route("/api/innlogging", endepunkt(be_om_lenke, True), methods=["POST"]),
        Route("/api/innlogging/bekreft", endepunkt(bekreft, True), methods=["POST"]),
        Route("/api/utlogging", endepunkt(logg_ut, True), methods=["POST"]),
        Route("/api/meg", endepunkt(meg, True), methods=["GET", "POST", "DELETE"]),
        Route("/api/meg/eksport", endepunkt(eksport)),
        Route("/api/quiz", endepunkt(quiz, True), methods=["GET", "POST"]),
        Route("/api/sak/{sak:path}/kommentarer", endepunkt(trad)),
        Route("/api/mest-kommentert", endepunkt(mest_kommentert)),
        Route("/api/kommentar", endepunkt(ny, True), methods=["POST"]),
        Route("/api/kommentar/{id}", endepunkt(kommentar, True), methods=["GET", "PUT", "DELETE"]),
        Route("/api/kommentar/{id}/stem", endepunkt(stem, True), methods=["POST"]),
        Route("/api/kommentar/{id}/rapporter", endepunkt(rapporter, True), methods=["POST"]),
        Route("/api/kommentar/{id}/manuell", endepunkt(manuell, True), methods=["POST"]),
    ]

    async def sikkerhet(request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        return response

    @asynccontextmanager
    async def lifespan(app):
        if start_ko:
            tjeneste.start_ko()
        yield

    return Starlette(routes=ruter, lifespan=lifespan, max_body_size=MAKS_KROPP, middleware=[
        Middleware(CORSMiddleware, allow_origins=opprinnelser, allow_methods=["GET", "POST", "PUT", "DELETE"],
                   allow_headers=["Authorization", "Content-Type"], max_age=3600),
        Middleware(BaseHTTPMiddleware, dispatch=sikkerhet),
    ])


async def _i_traad(fn, *args):
    """Run a blocking call (SMTP, the moderator) off the event loop."""
    return await run_in_threadpool(fn, *args)


class Saker:
    """The sak index bygg writes (data/debatt/saker.json: key → tittel, ingress, lenke),
    read again whenever the file changes, so a new build needs no restart."""

    def __init__(self, path):
        self.path, self._mtime, self._data = path, None, {}

    def get(self, key, default=None):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return default
        if mtime != self._mtime:
            try:
                self._data, self._mtime = json.loads(self.path.read_text()), mtime
            except ValueError:
                log.warning("could not read %s; keeping the previous index", self.path)
        return self._data.get(key, default)


def opprinnelser_fra_config() -> list[str]:
    """The site's own origins (tingposten.no and the statichost name), plus extra ones for tests."""
    from .. import config

    ut = [config.SITE_URL.rstrip("/"), f"https://{config.STATICHOST_SITE}.statichost.page"]
    ut += [o.strip().rstrip("/") for o in os.environ.get("DEBATT_EKSTRA_OPPRINNELSER", "").split(",") if o.strip()]
    return ut


def app_fra_config() -> Starlette:
    from .. import config
    from . import epost, moderering
    from .db import Db

    tjeneste = Tjeneste(Db(config.DEBATT_DIR / "debatt.db"), moderering.kjede(),
                        Saker(config.DEBATT_DIR / "saker.json"), epost.sender_fra_miljo(), config.SITE_URL)
    return lag_app(tjeneste, opprinnelser_fra_config())
