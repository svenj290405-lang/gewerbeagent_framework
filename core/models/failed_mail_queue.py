"""FailedMailQueue — Retry-Queue fuer fehlgeschlagene Mail-Versendungen.

Wenn ein Mail-Versand an Brevo failt (HTTP 5xx, Timeout, Account-
Suspendierung), legt der Caller eine Zeile hier ab statt sofort den
Status der Rechnung/Visualisierung/Reply auf 'error' zu setzen.

Der Background-Cron `mail_retry_cron.py` arbeitet die Queue ab mit
Exponential-Backoff (5min → 30min → 2h → dead). Nach 3 Tries auf
'dead' setzen, Sven + Tenant alerten.

Bei Erfolg setzt der Cron die Rechnung wieder auf 'mail_sent'
(oder den Visualisierungs-Status auf 'mailed', etc.) — die Recovery-
Pfad-Logik ist im mail_retry_cron pro `mail_type`.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# Mail-Status-Konstanten
FAILED_MAIL_PENDING = "pending"
FAILED_MAIL_SENT = "sent"
FAILED_MAIL_DEAD = "dead"

# Mail-Type-Konstanten (matchen Caller-Kontexte)
MAIL_TYPE_RECHNUNG = "rechnung"
MAIL_TYPE_VISUALISIERUNG = "visualisierung"
MAIL_TYPE_ANGEBOT = "angebot"
MAIL_TYPE_REPLY = "reply"

# Backoff-Plan: nach welchen Sekunden re-versucht werden soll.
# Listen-Index = attempt_count (0 = erster Retry, 1 = zweiter, ...)
# Nach Liste-Ende: dead.
RETRY_BACKOFF_SECONDS = [
    5 * 60,        # 1. Retry nach 5 min
    30 * 60,       # 2. Retry nach 30 min
    2 * 60 * 60,   # 3. Retry nach 2 h
]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)


class FailedMailQueue(Base):
    """Retry-Queue fuer fehlgeschlagene Mails."""
    __tablename__ = "failed_mail_queue"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rechnung_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rechnungen.id", ondelete="SET NULL"),
        nullable=True,
    )

    mail_type: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Payload: subject, html_body, attachments (Base64-eingebettet)
    # Schema (lock'ed by mail_retry_cron):
    #   {
    #     "subject": str,
    #     "html_body": str,
    #     "from_name": Optional[str],
    #     "attachments": [
    #       {"filename": str, "mime_type": str, "data_base64": str}
    #     ],
    #     "reply_to": Optional[str],   # fuer Reply-Threading
    #   }
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict,
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FAILED_MAIL_PENDING,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )
