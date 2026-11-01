"""Thin client for Bergen kommune's Public 360 (bk360) JSON API.

Deliberately slow: one request every REQUEST_DELAY_S seconds, a couple of
long backoffs, then give up. Missing a day is fine; getting blocked is not.
"""

import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)


class Blocked(RuntimeError):
    """The server refused us repeatedly. Stop the run, try again tomorrow."""


class Client:
    def __init__(self, delay_s: float = config.REQUEST_DELAY_S):
        self.delay_s = delay_s
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self._last = 0.0

    def get_json(self, path: str, **params):
        url = f"{config.API_BASE}/{path}"
        for attempt in range(len(config.BACKOFF_S) + 1):
            wait = self._last + self.delay_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=config.TIMEOUT_S)
                if r.status_code == 200:
                    return r.json()
                reason = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = type(e).__name__
            if attempt < len(config.BACKOFF_S):
                backoff = config.BACKOFF_S[attempt]
                log.warning("%s for %s (%s); waiting %ss", reason, path, params, backoff)
                time.sleep(backoff)
        raise Blocked(f"{path} {params}: {reason}")

    def utvalg(self) -> list[dict]:
        return self.get_json("utvalgForPolitiskeSaker")

    def agendapunktfiler(self, dokumentnummer: str) -> list[dict]:
        """A sak's attachments (metadata only; robots.txt allows this, not fil/*)."""
        return self.get_json("agendapunktfiler", dokumentnummer=dokumentnummer) or []

    def agendapunkter(self, utvalg_id: str, year: int) -> tuple[list[dict], bool]:
        """All agenda items for one organ and year, and whether we got all of them."""
        items, start, total = [], 0, None
        while total is None or start < total:
            page = self.get_json(
                "agendapunkter",
                start=start,
                rows=config.PAGE_SIZE,
                utvalgId=utvalg_id,
                year=year,
            )
            total = page["numTotal"]
            if not page["items"]:
                break
            items.extend(page["items"])
            start += len(page["items"])
        # New items arriving mid-paging shift the list down, giving duplicates.
        unique = {i["id"]: i for i in items}
        complete = len(unique) == total
        if not complete:
            log.warning("utvalg %s/%s: got %d unique of %d", utvalg_id, year, len(unique), total)
        return list(unique.values()), complete
