"""Gemeinsame Benachrichtigungsschicht fuer Inhaber und Mitarbeiter.

Waehrend des Telegram-Ausstiegs (Art. 28 / Drittland-Transfer, siehe
LEGAL/Subprozessoren-Liste.md) gehen Benachrichtigungen ueber **beide**
Wege raus: Web-Push in die PWA und — solange ``settings.telegram_enabled``
gesetzt ist — zusaetzlich per Telegram. Damit entsteht kein
Benachrichtigungsloch fuer Mitarbeiter, die noch kein Push-Abo haben.

Sobald alle Mitarbeiter ein Abo haben, reicht ``TELEGRAM_ENABLED=false`` in
der ``.env`` — kein Code-Deploy noetig. Danach faellt der Telegram-Zweig in
genau diesem Modul weg und ``plugins/telegram_notify`` kann geloescht
werden; die Aufrufstellen bleiben unveraendert.

**DSGVO-Kern:** ``title``/``body`` gehen an FCM/APNs/Mozilla und muessen
deshalb frei von Endkunden-PII sein — kein Name, keine Mailadresse, kein
Betreff, kein Anliegen. Die Details holt die App nach dem Login vom
EU-Server. ``telegram_text`` darf reicher sein, weil es historisch so war;
neue Aufrufer sollten auch dort auf PII verzichten.

``url`` muss die Hash-Form ``/app#<screen>`` haben — siehe push_notifier.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from config.settings import settings
from core.integrations.push_notifier import (
    send_push_to_employee,
    send_push_to_tenant,
)

logger = logging.getLogger(__name__)


def telegram_active() -> bool:
    """True solange Telegram als Parallelkanal mitlaeuft."""
    return bool(getattr(settings, "telegram_enabled", False))


async def _telegram_for_employee(
    tenant_id: uuid.UUID,
    employee_id: Optional[uuid.UUID],
    text: str,
    employee_label: Optional[str],
    keyboard: Optional[dict] = None,
) -> bool:
    if not telegram_active() or not text:
        return False
    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        if keyboard is not None:
            # Inline-Tastatur (z.B. Rueckruf abhaken). Web-Push kann das
            # nicht — dort fuehrt der Deeplink in die App, die denselben
            # Vorgang ueber ihre eigene API erledigt.
            return bool(await TelegramNotifier.send_for_employee_with_keyboard(
                tenant_id, text, keyboard, employee_id=employee_id,
            ))
        return bool(await TelegramNotifier.send_for_employee(
            tenant_id, text,
            employee_id=employee_id, employee_label=employee_label,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("notify: Telegram-Versand employee=%s: %s", employee_id, e)
        return False


async def _telegram_for_tenant(
    tenant_id: uuid.UUID,
    text: str,
    employee_id: Optional[uuid.UUID],
) -> bool:
    if not telegram_active() or not text:
        return False
    try:
        from plugins.telegram_notify.handler import TelegramNotifier
        return bool(await TelegramNotifier.send_for_tenant(
            tenant_id, text, employee_id=employee_id,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("notify: Telegram-Versand tenant=%s: %s", tenant_id, e)
        return False


async def notify_employee(
    tenant_id: uuid.UUID,
    employee_id: Optional[uuid.UUID],
    *,
    title: str,
    body: str,
    url: str = "/app",
    tag: Optional[str] = None,
    telegram_text: str = "",
    employee_label: Optional[str] = None,
    telegram_keyboard: Optional[dict] = None,
) -> int:
    """Benachrichtigt einen einzelnen Mitarbeiter auf allen aktiven Kanaelen.

    Liefert die Anzahl erfolgreicher Zustellungen ueber **alle** Kanaele
    zusammen — Aufrufer, die das als bool auswerten ("ist es rausgegangen?"),
    behalten damit ihre bisherige Semantik, auch wenn Web-Push mangels
    VAPID-Keys oder Abo nichts zustellt. Fehler auf einem Kanal blockieren
    den anderen nicht; der Aufrufer soll durch eine fehlgeschlagene
    Benachrichtigung nie abbrechen.
    """
    sent = 0
    if employee_id is not None:
        try:
            sent = await send_push_to_employee(
                employee_id, title=title, body=body, url=url, tag=tag,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("notify: Push-Versand employee=%s: %s", employee_id, e)
    else:
        # Kein Mitarbeiter aufgeloest (z.B. Voice ohne Routing) — dann geht
        # der Push an den ganzen Betrieb, damit niemand leer ausgeht.
        try:
            sent = await send_push_to_tenant(
                tenant_id, title=title, body=body, url=url, tag=tag,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("notify: Push-Fanout tenant=%s: %s", tenant_id, e)

    tg_ok = await _telegram_for_employee(
        tenant_id, employee_id, telegram_text, employee_label,
        keyboard=telegram_keyboard,
    )
    return sent + (1 if tg_ok else 0)


async def notify_tenant(
    tenant_id: uuid.UUID,
    *,
    title: str,
    body: str,
    url: str = "/app",
    tag: Optional[str] = None,
    telegram_text: str = "",
    employee_id: Optional[uuid.UUID] = None,
    inhaber_only: bool = False,
) -> int:
    """Benachrichtigt einen Betrieb auf allen aktiven Kanaelen.

    Ist ``employee_id`` gesetzt, geht der Push gezielt an diesen Mitarbeiter
    (sonst an alle bzw. bei ``inhaber_only`` nur an die Default-Employees).
    Rueckgabe wie bei notify_employee: Summe ueber alle Kanaele.
    """
    sent = 0
    try:
        if employee_id is not None:
            sent = await send_push_to_employee(
                employee_id, title=title, body=body, url=url, tag=tag,
            )
        else:
            sent = await send_push_to_tenant(
                tenant_id, title=title, body=body, url=url, tag=tag,
                inhaber_only=inhaber_only,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("notify: Push-Versand tenant=%s: %s", tenant_id, e)

    tg_ok = await _telegram_for_tenant(tenant_id, telegram_text, employee_id)
    return sent + (1 if tg_ok else 0)
