"""Gemeinsame Benachrichtigungsschicht fuer Inhaber und Mitarbeiter.

Einziger Kanal ist Web-Push in die PWA. Telegram wurde am 2026-08-21
vollstaendig entfernt (Art. 28 / Drittland-Transfer, siehe
LEGAL/Subprozessoren-Liste.md) — es gibt keinen Parallelkanal mehr.

**DSGVO-Kern:** ``title``/``body`` gehen an FCM/APNs/Mozilla und muessen
deshalb frei von Endkunden-PII sein — kein Name, keine Mailadresse, kein
Betreff, kein Anliegen. Die Details holt die App nach dem Login vom
EU-Server.

``url`` muss die Hash-Form ``/app#<screen>`` haben — siehe push_notifier.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from core.integrations.push_notifier import (
    send_push_to_employee,
    send_push_to_tenant,
)

logger = logging.getLogger(__name__)


async def notify_employee(
    tenant_id: uuid.UUID,
    employee_id: Optional[uuid.UUID],
    *,
    title: str,
    body: str,
    url: str = "/app",
    tag: Optional[str] = None,
) -> int:
    """Benachrichtigt einen einzelnen Mitarbeiter per Web-Push.

    Liefert die Anzahl erfolgreicher Zustellungen (ein Mitarbeiter kann
    mehrere Geraete abonniert haben). Fehler werden geschluckt: der
    Aufrufer soll durch eine fehlgeschlagene Benachrichtigung nie
    abbrechen.
    """
    if employee_id is not None:
        try:
            return await send_push_to_employee(
                employee_id, title=title, body=body, url=url, tag=tag,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("notify: Push-Versand employee=%s: %s", employee_id, e)
            return 0

    # Kein Mitarbeiter aufgeloest (z.B. Voice ohne Routing) — dann geht
    # der Push an den ganzen Betrieb, damit niemand leer ausgeht.
    try:
        return await send_push_to_tenant(
            tenant_id, title=title, body=body, url=url, tag=tag,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("notify: Push-Fanout tenant=%s: %s", tenant_id, e)
        return 0


async def notify_tenant(
    tenant_id: uuid.UUID,
    *,
    title: str,
    body: str,
    url: str = "/app",
    tag: Optional[str] = None,
    employee_id: Optional[uuid.UUID] = None,
    inhaber_only: bool = False,
) -> int:
    """Benachrichtigt einen Betrieb per Web-Push.

    Ist ``employee_id`` gesetzt, geht der Push gezielt an diesen Mitarbeiter
    (sonst an alle bzw. bei ``inhaber_only`` nur an die Default-Employees).
    """
    try:
        if employee_id is not None:
            return await send_push_to_employee(
                employee_id, title=title, body=body, url=url, tag=tag,
            )
        return await send_push_to_tenant(
            tenant_id, title=title, body=body, url=url, tag=tag,
            inhaber_only=inhaber_only,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("notify: Push-Versand tenant=%s: %s", tenant_id, e)
        return 0
