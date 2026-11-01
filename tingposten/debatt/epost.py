"""The login e-mail, sent from freetime.no through the same SMTP service as Freetime (Scaleway TEM, 1.11).

Settings come from the environment (the server's own env file, never the
pipeline's .env): DEBATT_SMTP_HOST, DEBATT_SMTP_PORT (465 = TLS, otherwise STARTTLS, e.g. 587 or 2587),
DEBATT_SMTP_BRUKER, DEBATT_SMTP_PASSORD, DEBATT_AVSENDER. DEBATT_EPOST=konsoll
writes the link to the log instead, for testing on one's own machine.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate

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
    msg["Date"] = formatdate(localtime=True)  # smtplib adds none, and some providers count that as spam
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(TEKST.format(lenke=lenke, regler=f"{config.SITE_URL}om/debattregler/",
                                 utgiver=f"utgitt av {config.UTGIVER}" if config.UTGIVER else "tingposten.no"))
    return msg


def _melding(til: str, avsender: str, emne: str, tekst: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = emne
    msg["From"] = formataddr(("Tingposten", avsender))
    msg["To"] = til
    msg["Message-ID"] = make_msgid(domain=avsender.rsplit("@", 1)[-1])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(tekst)
    return msg


def melding_godkjenning(til: str, avsender: str, tittel: str, tekst: str, lenke: str) -> EmailMessage:
    """The editor's version of a debattinnlegg, for the writer to approve."""
    return _melding(til, avsender, f"Debattinnlegget ditt: {tittel}", f"""Hei!

Redaktøren har gått gjennom debattinnlegget ditt og gjort noen endringer. Les den nye
versjonen her, og godkjenn den hvis du er enig:

{lenke}

Lenken virker i 14 dager. Er du ikke enig i endringene, svarer du bare på denne e-posten.

Versjonen som er sendt deg:

{tittel}

{tekst}

Tingposten
""")


def melding_publisert(til: str, avsender: str, tittel: str, lenke: str) -> EmailMessage:
    return _melding(til, avsender, f"Publisert: {tittel}", f"""Hei!

Debattinnlegget ditt er publisert på Tingposten:

{lenke}

Takk for at du skrev. Leserne kan kommentere innlegget under teksten.

Tingposten
""")


def smtp_fra_miljo(miljo=None):
    """send(msg) through the SMTP settings in the environment, or None when they are missing."""
    miljo = os.environ if miljo is None else miljo
    vert = miljo.get("DEBATT_SMTP_HOST", "smtp.domeneshop.no")
    port = int(miljo.get("DEBATT_SMTP_PORT", "587"))
    bruker, passord = miljo.get("DEBATT_SMTP_BRUKER"), miljo.get("DEBATT_SMTP_PASSORD")
    if not (bruker and passord):
        return None

    def send(msg: EmailMessage) -> None:
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


def sender_fra_miljo():
    """send(til, lenke) as the environment says, or a console sender for local tests."""
    if os.environ.get("DEBATT_EPOST") == "konsoll":
        def konsoll(til: str, lenke: str) -> None:
            log.warning("innloggingslenke til %s: %s", til, lenke)
        return konsoll
    avsender = os.environ.get("DEBATT_AVSENDER") or config.KONTAKT_EPOST
    send_msg = smtp_fra_miljo()
    if not (send_msg and avsender):
        raise RuntimeError("DEBATT_SMTP_BRUKER, DEBATT_SMTP_PASSORD and DEBATT_AVSENDER must be set "
                           "(or DEBATT_EPOST=konsoll for local tests)")

    def send(til: str, lenke: str) -> None:
        send_msg(melding(til, lenke, avsender))
    return send
