"""The login e-mail, sent through the freetime.no mail account (SMTP).

Settings come from the environment (the server's own env file, never the
pipeline's .env): DEBATT_SMTP_HOST, DEBATT_SMTP_PORT (465 = TLS, 587 = STARTTLS),
DEBATT_SMTP_BRUKER, DEBATT_SMTP_PASSORD, DEBATT_AVSENDER. DEBATT_EPOST=konsoll
writes the link to the log instead, for testing on one's own machine.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .. import config

log = logging.getLogger(__name__)

EMNE = "Logg inn på Tingposten"
TEKST = """\
Hei!

Noen, forhåpentligvis du, ba om å logge inn på Tingposten for å skrive kommentarer.
Trykk på lenken innen 15 minutter:

{lenke}

Lenken virker én gang. Ba du ikke om dette, kan du se bort fra e-posten: ingen konto
blir laget før lenken brukes.

Debattreglene: {regler}

Tingposten · {utgiver}
"""


def melding(til: str, lenke: str, avsender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = EMNE
    msg["From"] = formataddr(("Tingposten", avsender))
    msg["To"] = til
    msg["Message-ID"] = make_msgid(domain=avsender.rsplit("@", 1)[-1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(TEKST.format(lenke=lenke, regler=f"{config.SITE_URL}om/debattregler/",
                                 utgiver=f"utgitt av {config.UTGIVER}" if config.UTGIVER else "tingposten.no"))
    return msg


def sender_fra_miljo():
    """send(til, lenke) as the environment says, or a console sender for local tests."""
    if os.environ.get("DEBATT_EPOST") == "konsoll":
        def konsoll(til: str, lenke: str) -> None:
            log.warning("innloggingslenke til %s: %s", til, lenke)
        return konsoll
    vert = os.environ.get("DEBATT_SMTP_HOST", "smtp.domeneshop.no")
    port = int(os.environ.get("DEBATT_SMTP_PORT", "587"))
    bruker, passord = os.environ.get("DEBATT_SMTP_BRUKER"), os.environ.get("DEBATT_SMTP_PASSORD")
    avsender = os.environ.get("DEBATT_AVSENDER") or config.KONTAKT_EPOST
    if not (bruker and passord and avsender):
        raise RuntimeError("DEBATT_SMTP_BRUKER, DEBATT_SMTP_PASSORD and DEBATT_AVSENDER must be set "
                           "(or DEBATT_EPOST=konsoll for local tests)")

    def send(til: str, lenke: str) -> None:
        msg = melding(til, lenke, avsender)
        kontekst = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(vert, port, context=kontekst, timeout=30) as s:
                s.login(bruker, passord)
                s.send_message(msg)
        else:
            with smtplib.SMTP(vert, port, timeout=30) as s:
                s.starttls(context=kontekst)
                s.login(bruker, passord)
                s.send_message(msg)
    return send
