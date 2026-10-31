"""Try the comment section on your own machine: `tingposten debatt-lokal`.

Builds the site with comments switched on into data/debatt/lokal/site, serves it on
127.0.0.1, and runs the comment server next to it with the real moderator chain
(config.DEBATT_MODERATOR; Jev needs TYPESAFE_API_KEY in .env). Login links are
printed in this terminal instead of e-mailed. The control panel runs too, for the
editor queue, with its run buttons switched off. Everything lives in data/debatt/lokal/
(its own database), so nothing touches the Pi or real readers. Nothing is published.
"""

import functools
import logging
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


class _IngenKjoring:
    """The control panel's run starter, switched off: in a local test its buttons would
    otherwise fetch, write and publish the real site."""

    def __init__(self):
        from ..admin import Kjorer

        self._kjorer = Kjorer()

    def __getattr__(self, navn):
        return getattr(self._kjorer, navn)

    def start(self, modus: str) -> tuple[int, str]:
        return 403, "Av i den lokale testen: knappene her publiserer ikke."


def kjor(port_side: int = 8091, port_api: int = 8790, moderator: tuple[str, ...] | None = None,
         mappe: Path | None = None, port_panel: int = 8787) -> None:
    import uvicorn

    from .. import admin, bygg
    from . import moderering, server
    from .db import Db
    from .tjeneste import Tjeneste

    mappe = mappe or config.DEBATT_DIR / "lokal"
    side_url, api_url = f"http://127.0.0.1:{port_side}/", f"http://127.0.0.1:{port_api}/"

    # The build reads these from config; only this process sees the change.
    config.SITE_URL, config.DEBATT = side_url, api_url
    log.info("bygger siden med kommentarer på (tar et halvt minutt) …")
    bygg.build(mappe / "site", config.DATA_DIR, debatt_dir=mappe)

    handler = functools.partial(_Stille, directory=str(mappe / "site"))
    statisk = ThreadingHTTPServer(("127.0.0.1", port_side), handler)
    threading.Thread(target=statisk.serve_forever, name="side", daemon=True).start()

    def send_lenke(epost: str, lenke: str) -> None:
        print(f"\n  Innloggingslenke for {epost}:\n  {lenke}\n", flush=True)

    panel = admin.make_server("127.0.0.1", port_panel, None, kjorer=_IngenKjoring(),
                              debatt=admin.DebattKo(mappe / "debatt.db"))
    threading.Thread(target=panel.serve_forever, name="panel", daemon=True).start()

    # Testing means writing a lot from one new account: the spam limits are for the real site.
    from . import tjeneste as tjeneste_modul

    tjeneste_modul.NYE_KONTO_PER_DOGN = tjeneste_modul.KOMMENTARER_PER_DOGN = 1000
    tjeneste_modul.KOMMENTAR_PAUSE_S = 2
    tjeneste_modul.NAVNEFORSOK_PER_TIME = 100

    kjede = moderering.kjede(moderator)
    tjeneste = Tjeneste(Db(mappe / "debatt.db"), kjede, server.Saker(mappe / "saker.json"), send_lenke, side_url,
                        vis_lenke=True)
    app = server.lag_app(tjeneste, [side_url.rstrip("/")])
    print(f"""
  Tingposten med kommentarer, lokalt:  {side_url}bergen/
  Moderator: {kjede.name}
  Redaktørkøen (kontrollpanelet, knappene for kjøring er av):  http://127.0.0.1:{port_panel}/
  Innloggingslenken vises på siden og her, og grensene mot spam er slått av (bare i denne lokale testen).
  Ctrl+C for å stoppe.
""", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port_api, log_level="warning", server_header=False)
    finally:
        statisk.shutdown()
        panel.shutdown()


class _Stille(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)
