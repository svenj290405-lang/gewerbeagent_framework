"""Wer verschickt die Mail — und taugt diese Adresse fuer Geschaeftspost?

Zwei Fragen, die im ganzen Mail-Verkehr wiederkehren:

**1. Welche Adresse steht im From-Feld?** Graph setzt den Absender selbst
aufs verbundene Postfach; unsere Templates wussten das bisher nicht und
haben ``tenant.contact_email`` in den Footer gerendert. Weichen die beiden
ab (Tippfehler, alte Adresse, Umzug von .com auf .de), zeigt die Mail einen
``mailto:``-Link auf eine ANDERE Adresse als das From-Feld. Das ist erstens
ein Spamfilter-Merkmal (klassisches Phishing-Muster) und zweitens ein
echter Funktionsfehler: eine Kunden-Antwort an die Footer-Adresse landet in
einem Postfach, das der Inbox-Poller gar nicht liest — der Thread reisst
ab, Q sieht die Antwort nie. Darum folgt der Footer dem Postfach.

**2. Ist das ein Freemail-Postfach?** Wer Angebote aus einem kostenlosen
``@outlook.de``/``@gmx.de`` verschickt, sendet ueber eine Shared Domain:
DKIM signiert auf die Domain des Anbieters, nicht auf die des Betriebs. Es
gibt also keine eigene Reputation, die man sich erarbeiten koennte — man
erbt die von Millionen fremder Nutzer. Vor allem bei Erstkontakt (Angebot
an eine Adresse, die noch nie mit dem Postfach gesprochen hat) landen die
Mails dann zuverlaessig im Spam-Ordner, und zwar OHNE Bounce: niemand merkt
es. Deshalb warnen wir in den Verbindungen, statt zu hoffen.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# Kostenlose bzw. geteilte Endkunden-Domains. Bewusst nur die im deutschen
# Handwerk verbreiteten — die Liste soll warnen, nicht vollstaendig sein.
# Falsch-Negative sind harmlos (keine Warnung), Falsch-Positive nerven den
# Betrieb bei jedem Blick in die Einstellungen.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
    "live.com", "live.de", "msn.com",
    "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com",
    "web.de", "t-online.de", "freenet.de", "arcor.de",
    "yahoo.com", "yahoo.de", "ymail.com",
    "aol.com", "aol.de",
    "icloud.com", "me.com", "mac.com",
    "mail.de", "posteo.de", "mailbox.org",
})


def ist_freemail_adresse(adresse: str | None) -> bool:
    """True, wenn die Adresse auf einer geteilten Freemail-Domain liegt."""
    domain = (adresse or "").strip().lower().rpartition("@")[2]
    return bool(domain) and domain in FREEMAIL_DOMAINS


async def mailbox_adresse(
    tid: uuid.UUID, employee_id: uuid.UUID | None = None,
) -> str | None:
    """Die Adresse des Postfachs, aus dem gerade verschickt wird.

    Derselbe Token-Lookup wie im Versand (employee → default-employee →
    legacy-tenant), damit Footer und From garantiert dasselbe Postfach
    meinen. Bei fehlender Verbindung ``None`` — der Aufrufer faellt dann
    auf ``tenant.contact_email`` zurueck.
    """
    try:
        from core.security.oauth_token_lookup import find_oauth_token

        token = await find_oauth_token(tid, "microsoft", employee_id)
        adresse = (getattr(token, "account_email", "") or "").strip()
        return adresse or None
    except Exception as exc:  # noqa: BLE001
        # Nie den Mail-Versand an der Footer-Kosmetik scheitern lassen.
        logger.warning("mailbox_adresse fehlgeschlagen (tenant=%s): %s", tid, exc)
        return None
