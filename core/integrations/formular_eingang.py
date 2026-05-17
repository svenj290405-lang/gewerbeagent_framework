"""Bearbeitungs-Logik fuer eingegangene Formulare (AnfrageResponse).

Trennt die DB-Operationen vom Telegram-Layer:
- list_recent_for_tenant / get_by_short_id / set_status fuer die
  /formulare-Befehle und Inline-Buttons
- find_overdue fuer den Daily-Heartbeat-Cron

Kurz-IDs: Die UUID-Hex hat 32 Zeichen, zu lang fuer Telegram-Befehle
wie /formular_eingang_<id>. Wir benutzen die ersten 8 Hex-Zeichen,
das gibt 16^8 = ~4.3 Mrd Praefixe — pro Tenant ist eine Kollision
weit jenseits realistischer Anfrage-Volumina.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Optional

from sqlalchemy import desc, func, select

from core.database import AsyncSessionLocal
from core.models import (
    AnfrageResponse,
    AnfrageToken,
    FORMULAR_STATUS_OFFEN,
    FORMULAR_STATUS_VALID,
)

logger = logging.getLogger(__name__)


def short_id(response_id: uuid.UUID) -> str:
    """Erste 8 Hex-Zeichen der Response-UUID — fuer /formular_eingang_<id>."""
    return response_id.hex[:8]


async def list_recent_for_tenant(
    tenant_id: uuid.UUID,
    *,
    limit: int = 10,
    only_open: bool = False,
) -> list[tuple[AnfrageResponse, AnfrageToken]]:
    """Letzte N Formular-Antworten fuer einen Tenant.

    only_open=True filtert auf Status 'neu' und 'in_bearbeitung' fuer
    /formulare_offen — die Liste die der Handwerker noch abarbeiten muss.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(AnfrageResponse, AnfrageToken)
            .join(AnfrageToken, AnfrageResponse.token_id == AnfrageToken.id)
            .where(AnfrageToken.tenant_id == tenant_id)
        )
        if only_open:
            stmt = stmt.where(
                AnfrageResponse.bearbeitungs_status.in_(FORMULAR_STATUS_OFFEN)
            )
        stmt = stmt.order_by(desc(AnfrageResponse.submitted_at)).limit(limit)
        rows = (await session.execute(stmt)).all()
        return [(r[0], r[1]) for r in rows]


async def get_by_short_id(
    tenant_id: uuid.UUID, short: str,
) -> Optional[tuple[AnfrageResponse, AnfrageToken]]:
    """Aufloesen einer 8-Hex-Kurz-ID zu (Response, Token).

    Praefix-Match per LIKE damit der User nur den Anfang tippen muss.
    Wenn die Kurz-ID mehr als 1 Treffer hat (extrem unwahrscheinlich,
    Kollision bei 4.3 Mrd Werten) geben wir None zurueck — der User
    soll dann mehr Zeichen tippen statt eine willkuerliche Wahl zu
    bekommen.
    """
    short = (short or "").lower().strip()
    if not short:
        return None
    # Client-side Filter: die letzten 200 Antworten laden und in Python
    # auf Hex-Prefix matchen. Bei <200 Anfragen pro Tenant kostet das
    # nichts; bei groesseren Tenants koennten wir spaeter einen
    # functional Index auf substring(id::text) ergaenzen.
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(AnfrageResponse, AnfrageToken)
            .join(AnfrageToken, AnfrageResponse.token_id == AnfrageToken.id)
            .where(AnfrageToken.tenant_id == tenant_id)
            .order_by(desc(AnfrageResponse.submitted_at))
            .limit(200)
        )).all()
        matches = [(r[0], r[1]) for r in rows if short_id(r[0].id).startswith(short)]
        if len(matches) != 1:
            return None
        return matches[0]


async def set_status(
    response_id: uuid.UUID,
    *,
    status: str,
    employee_id: Optional[uuid.UUID] = None,
) -> bool:
    """Setzt den Bearbeitungs-Status. Returns True wenn der Status
    sich geaendert hat, False wenn er schon so war oder die ID fehlt.

    bearbeitet_at + bearbeitet_by_employee_id werden immer mitgesetzt
    damit man im Audit-Log sieht wer wann den Status zuletzt
    veraendert hat — auch dann wenn der neue Status zufaellig der
    gleiche ist wie der alte (idempotent).
    """
    if status not in FORMULAR_STATUS_VALID:
        logger.warning(f"set_status: ungueltiger Status {status!r}")
        return False
    async with AsyncSessionLocal() as session:
        resp = (await session.execute(
            select(AnfrageResponse).where(AnfrageResponse.id == response_id)
        )).scalar_one_or_none()
        if not resp:
            return False
        changed = resp.bearbeitungs_status != status
        resp.bearbeitungs_status = status
        resp.bearbeitet_at = dt.datetime.now(dt.timezone.utc)
        resp.bearbeitet_by_employee_id = employee_id
        await session.commit()
        return changed


async def count_open(
    tenant_id: uuid.UUID,
    *,
    older_than: Optional[dt.timedelta] = None,
) -> int:
    """Anzahl offener Formulare (status neu/in_bearbeitung).

    older_than=timedelta(hours=12) liefert nur die Antworten, die schon
    laenger als 12h liegen — Heartbeat-Cron nutzt das damit frische
    Anfragen vom heutigen Morgen nicht sofort wieder gepingt werden.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(func.count(AnfrageResponse.id))
            .join(AnfrageToken, AnfrageResponse.token_id == AnfrageToken.id)
            .where(AnfrageToken.tenant_id == tenant_id)
            .where(AnfrageResponse.bearbeitungs_status.in_(FORMULAR_STATUS_OFFEN))
        )
        if older_than is not None:
            cutoff = dt.datetime.now(dt.timezone.utc) - older_than
            stmt = stmt.where(AnfrageResponse.submitted_at < cutoff)
        return int((await session.execute(stmt)).scalar() or 0)


async def find_tenants_with_overdue(
    *, older_than: dt.timedelta = dt.timedelta(hours=12),
) -> dict[uuid.UUID, int]:
    """{tenant_id: anzahl_offener_alter_anfragen} fuer den Heartbeat.

    Nur Tenants mit >= 1 offener Anfrage >= older_than landen im Ergebnis.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - older_than
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(AnfrageToken.tenant_id, func.count(AnfrageResponse.id))
            .join(AnfrageResponse, AnfrageResponse.token_id == AnfrageToken.id)
            .where(AnfrageResponse.bearbeitungs_status.in_(FORMULAR_STATUS_OFFEN))
            .where(AnfrageResponse.submitted_at < cutoff)
            .group_by(AnfrageToken.tenant_id)
        )).all()
        return {tid: int(cnt) for tid, cnt in rows}


async def get_response_for_token(
    token_str: str,
) -> Optional[tuple[AnfrageResponse, AnfrageToken]]:
    """Nach erfolgreichem Submit: die Response holen die gerade angelegt
    wurde, damit der Telegram-Push die response.id fuer Inline-Buttons
    kennt. Pro Token gibt es genau eine Response (token.submitted_at-
    Unique-Constraint via Code, nicht DB)."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(AnfrageResponse, AnfrageToken)
            .join(AnfrageToken, AnfrageResponse.token_id == AnfrageToken.id)
            .where(AnfrageToken.token == token_str)
            .order_by(desc(AnfrageResponse.submitted_at))
            .limit(1)
        )).first()
        if not row:
            return None
        return (row[0], row[1])
