"""Client for Stortinget's open data (data.stortinget.no) and its PDFs (www.stortinget.no).

Open data under NLOD, but we keep Bergen's manners: one request at a time, a pause
per host (config.STORTINGET_DELAY_S, far below the 100 calls a minute the terms
allow and at www's Crawl-delay), the same User-Agent, long backoffs, and Blocked
when the server keeps refusing.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from .. import config
from ..bk360 import Blocked

log = logging.getLogger(__name__)

NORSK_TID = timezone(timedelta(hours=1))  # only for dates without an offset; the API gives one


class Client:
    def __init__(self, session=None, sleep=time.sleep, clock=time.monotonic, delay_s: dict | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.delay_s = config.STORTINGET_DELAY_S if delay_s is None else delay_s
        self._sleep, self._clock = sleep, clock
        self._last: dict[str, float] = {}
        self.antall = 0  # requests made, for the run's log

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        host = urlsplit(url).hostname or ""
        for attempt in range(len(config.BACKOFF_S) + 1):
            if host in self._last:
                wait = self._last[host] + self.delay_s.get(host, 5.0) - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last[host] = self._clock()
            self.antall += 1
            try:
                r = self.session.get(url, params=params, timeout=config.TIMEOUT_S)
                if r.status_code == 200:
                    return r
                reason = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = type(e).__name__
            if attempt < len(config.BACKOFF_S):
                backoff = config.BACKOFF_S[attempt]
                log.warning("%s for %s (%s); waiting %ss", reason, url, params, backoff)
                self._sleep(backoff)
        raise Blocked(f"{url} {params}: {reason}")

    def json(self, endpoint: str, **params):
        return self._get(f"{config.STORTINGET_API}/{endpoint}", {**params, "format": "json"}).json()

    def tekst(self, endpoint: str, **params) -> str:
        """An XML endpoint (publikasjon has no JSON)."""
        r = self._get(f"{config.STORTINGET_API}/{endpoint}", params)
        r.encoding = r.encoding or "utf-8"
        return r.text

    def bytes(self, url: str) -> bytes:
        return self._get(url).content

    # --- endpoints ---------------------------------------------------------------------

    def innevaerende_sesjon(self) -> str:
        return self.json("sesjoner")["innevaerende_sesjon"]["id"]

    def saker(self, sesjon: str) -> list[dict]:
        return self.json("saker", sesjonid=sesjon).get("saker_liste") or []

    def sak(self, sakid: int) -> dict:
        return self.json("sak", sakid=sakid)

    def voteringer(self, sakid: int) -> list[dict]:
        return self.json("voteringer", sakid=sakid).get("sak_votering_liste") or []

    def voteringsresultat(self, voteringid: int) -> list[dict]:
        return self.json("voteringsresultat", voteringid=voteringid).get("voteringsresultat_liste") or []

    def voteringsforslag(self, voteringid: int) -> list[dict]:
        return self.json("voteringsforslag", voteringid=voteringid).get("voteringsforslag_liste") or []

    def stortingsvedtak(self, sesjon: str) -> list[dict]:
        return self.json("stortingsvedtak", sesjonid=sesjon).get("stortingsvedtak_liste") or []

    def publikasjon(self, publikasjonid: str) -> str:
        return self.tekst("publikasjon", publikasjonid=publikasjonid)


_MS = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")
_NORSK = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?: (\d{2}):(\d{2}):(\d{2}))?")


def tid(verdi) -> datetime | None:
    """The API's dates as aware datetimes: "/Date(1781183647493+0200)/" (JSON) or
    "11.06.2026 00:00:00" (saksgang). "01.01.0001" and year-1 dates mean no date."""
    if not verdi:
        return None
    if m := _MS.fullmatch(str(verdi)):
        ms, off = int(m.group(1)), m.group(2)
        tz = timezone(timedelta(hours=int(off[:3]), minutes=int(off[0] + off[3:]))) if off else NORSK_TID
        d = datetime.fromtimestamp(ms / 1000, tz)
    elif m := _NORSK.fullmatch(str(verdi)):
        dd, mm, yyyy, *hms = m.groups()
        d = datetime(int(yyyy), int(mm), int(dd), *(int(x or 0) for x in hms), tzinfo=NORSK_TID)
    else:
        try:
            d = datetime.fromisoformat(str(verdi))
        except ValueError:
            return None
    return None if d.year < 1900 else d


def url(lenke: str | None) -> str | None:
    """The API's links are "//www.stortinget.no/…" or "http://…"; we link https."""
    if not lenke:
        return None
    if lenke.startswith("//"):
        return "https:" + lenke
    return re.sub(r"^http://(www\.)?stortinget\.no", "https://www.stortinget.no", lenke)
