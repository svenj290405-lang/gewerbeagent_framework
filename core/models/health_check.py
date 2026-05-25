"""HealthCheckResult — Ergebnis des taeglichen System-Health-Checks.

Der Daily-Health-Check (core/integrations/daily_health_check.py) laeuft
1x morgens und prueft, ob der Bot/das System noch laeuft (DB erreichbar,
Telegram-Bot erreichbar, Background-Crons leben). Jedes Ergebnis wird hier
persistiert, damit es im Admin-Tool (/admin/health) angezeigt werden kann —
anders als der reine In-Memory-Heartbeat ueberlebt das den Container-Restart.

status: "ok" | "degraded" | "error"
  - ok       = alle Teilpruefungen gruen
  - degraded = mind. eine Teilpruefung rot (z.B. ein Cron tot)
  - error    = Check selbst ist gecrasht (z.B. DB nicht erreichbar)
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


HEALTH_STATUS_OK = "ok"
HEALTH_STATUS_DEGRADED = "degraded"
HEALTH_STATUS_ERROR = "error"


class HealthCheckResult(Base):
    __tablename__ = "health_check_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    db_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    telegram_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    crons_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )

    # Roh-Report (Cron-Details, Fehlertexte) als JSON fuer die Admin-Anzeige.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # True wenn wegen eines Problems eine Alarm-Mail rausging.
    alert_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    def __repr__(self) -> str:
        return f"<HealthCheckResult {self.status} @ {self.checked_at}>"
