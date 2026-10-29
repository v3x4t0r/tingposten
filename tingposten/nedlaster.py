"""Nedlaster: fetch missing saksframlegg/protokoll PDFs, only when allowed.

robots.txt disallows /politikere-utvalg/api/fil/*, so nothing is downloaded
until config.ALLOW_PDF_FETCH is set (after Bystyrets kontor has said yes).
Same manners as the agenda client: one request at a time, a pause between
requests, long backoffs, and the run stops when the server keeps refusing.
"""

import logging
import os
import time
from pathlib import Path

import requests

from . import config, pdf
from .bk360 import Blocked

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, delay_s: float = config.REQUEST_DELAY_S, session=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.delay_s = delay_s
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self._sleep, self._clock = sleep, clock
        self._last = None

    def get_bytes(self, url: str) -> bytes:
        """The body of a 200 response. HTTPError for a client error (that one
        file is missing or gone); Blocked after repeated refusals."""
        for attempt in range(len(config.BACKOFF_S) + 1):
            if self._last is not None:
                wait = self._last + self.delay_s - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()
            try:
                r = self.session.get(url, timeout=config.TIMEOUT_S)
                if r.status_code == 200:
                    return r.content
                reason = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
            except (requests.ConnectionError, requests.Timeout) as e:
                reason = type(e).__name__
            if attempt < len(config.BACKOFF_S):
                backoff = config.BACKOFF_S[attempt]
                log.warning("%s for %s; waiting %ss", reason, url, backoff)
                self._sleep(backoff)
        raise Blocked(f"{url}: {reason}")


def mangler(items: list[dict], data_dir: Path = config.DATA_DIR) -> list[tuple[dict, str, str, Path]]:
    """(item, kind, url, path) for every PDF the items should have but don't."""
    out = []
    for item in items:
        for kind in ("protokoll", "saksframlegg"):
            url = pdf.url(item, kind)
            path = data_dir / "pdf" / pdf.filename(item, kind)
            if url and not path.exists():
                out.append((item, kind, url, path))
    return out


def last_ned(items: list[dict], data_dir: Path = config.DATA_DIR, client=None) -> dict:
    todo = mangler(items, data_dir)
    if not config.ALLOW_PDF_FETCH:
        if todo:
            log.info("%d PDF(s) missing; automatic download is off (config.ALLOW_PDF_FETCH)", len(todo))
        return {"av": len(todo)}

    cap = getattr(config, "MAKS_PDF_PER_KJORING", 200)
    client = client or Fetcher()
    counts = {"lastet_ned": 0, "feil": 0, "gjenstar": max(0, len(todo) - cap), "blokkert": False}
    for n, (item, kind, url, path) in enumerate(todo[:cap]):
        try:
            body = client.get_bytes(url)
        except Blocked as e:
            log.error("server keeps refusing, stopping downloads for today: %s", e)
            counts["blokkert"] = True
            counts["gjenstar"] += len(todo[:cap]) - n
            break
        except requests.HTTPError as e:
            log.warning("%s %s: %s", pdf.key(item), kind, e)
            counts["feil"] += 1
            continue
        if not body.startswith(b"%PDF"):
            log.warning("%s %s: response is not a PDF (%d bytes), not saved", pdf.key(item), kind, len(body))
            counts["feil"] += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(body)
        os.replace(tmp, path)
        counts["lastet_ned"] += 1
    log.info("last ned: %s", counts)
    return counts
