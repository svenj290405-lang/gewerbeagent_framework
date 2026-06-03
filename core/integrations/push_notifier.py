"""Web-Push-Versand (VAPID) an die Inhaber-/Mitarbeiter-PWA.

Loest die Telegram-Pushes ab. **DSGVO-Kern:** der Payload ist bewusst
minimal/inhaltslos — er enthaelt KEINE Endkunden-PII. FCM/APNs/Mozilla
sehen nur einen verschluesselten Blob mit z.B. {"title": "Neue Buchung",
"body": "In der App ansehen", "url": "/app/termine"}. Die eigentlichen
Daten laedt die App erst nach Login vom EU-Server.

``pywebpush`` wird lazy importiert, damit das Modul auch ladbar bleibt
bevor die Dependency im Image ist. Fehlende VAPID-Keys oder fehlende Lib
= Push still deaktiviert (App laeuft weiter, schickt nur nichts).

CLI: ``python -m core.integrations.push_notifier --genkey`` erzeugt ein
VAPID-Schluesselpaar fuer die .env.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import delete, select

from config.settings import settings
from core.database.connection import get_session
from core.models.app_account import PushSubscription

logger = logging.getLogger(__name__)


def push_enabled() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def _vapid_claims() -> dict:
    return {"sub": settings.vapid_subject}


async def send_push_to_employee(
    employee_id: uuid.UUID,
    *,
    title: str,
    body: str,
    url: str = "/app",
    tag: Optional[str] = None,
) -> int:
    """Schickt eine (inhaltslose) Push-Notification an alle Geraete eines
    Employees. Liefert die Anzahl erfolgreich zugestellter Pushes.

    Tote Subscriptions (404/410 vom Push-Service) werden automatisch
    aus der DB entfernt.
    """
    if not push_enabled():
        logger.debug("push disabled (keine VAPID-Keys) — skip")
        return 0

    try:
        from pywebpush import WebPushException, webpush  # lazy
    except Exception as e:  # pragma: no cover - nur bis Image-Rebuild
        logger.warning("pywebpush nicht installiert: %s", e)
        return 0

    async with get_session() as s:
        subs = (await s.execute(
            select(PushSubscription).where(
                PushSubscription.employee_id == employee_id
            )
        )).scalars().all()

    if not subs:
        return 0

    payload = json.dumps({
        "title": title[:120],
        "body": body[:200],
        "url": url,
        "tag": tag or "ga",
    })

    sent = 0
    dead_ids: list[uuid.UUID] = []
    for sub in subs:
        sub_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims=dict(_vapid_claims()),
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                dead_ids.append(sub.id)
            else:
                logger.warning("webpush failed (%s): %s", status, e)
        except Exception as e:  # pragma: no cover
            logger.warning("webpush unerwarteter Fehler: %s", e)

    if dead_ids:
        async with get_session() as s:
            await s.execute(
                delete(PushSubscription).where(PushSubscription.id.in_(dead_ids))
            )
        logger.info("push: %d tote Subscriptions entfernt", len(dead_ids))

    return sent


# =====================================================================
# CLI: VAPID-Keys erzeugen
# =====================================================================

def _genkey() -> None:
    """Erzeugt ein VAPID-Schluesselpaar (EC P-256) und gibt die .env-Zeilen aus.

    Beide Werte als raw URL-safe base64 ohne Padding — das Format, das der
    Browser (``applicationServerKey``) und pywebpush/py_vapid erwarten.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    priv_key = ec.generate_private_key(ec.SECP256R1())
    private_value = priv_key.private_numbers().private_value
    public_point = priv_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )  # 65 Bytes, unkomprimiert

    print("# In .env eintragen:")
    print(f"VAPID_PUBLIC_KEY={b64(public_point)}")
    print(f"VAPID_PRIVATE_KEY={b64(private_value.to_bytes(32, 'big'))}")
    print("VAPID_SUBJECT=mailto:datenschutz@gewerbeagent.de")


if __name__ == "__main__":
    import sys
    if "--genkey" in sys.argv:
        _genkey()
    else:
        print("Nutzung: python -m core.integrations.push_notifier --genkey")
