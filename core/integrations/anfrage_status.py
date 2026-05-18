"""Status-Queries fuer das Formular-Eingang-Cockpit (/formulare).

Drei Buckets, abgeleitet aus den AnfrageToken-Spalten:

  📨 OFFEN       submitted_at IS NULL  AND expires_at > NOW()
  ✅ AUSGEFUELLT submitted_at IS NOT NULL
  ⌛ ABGELAUFEN  submitted_at IS NULL  AND expires_at <= NOW()

Wir lesen NUR token-Spalten — kein JOIN auf anfrage_responses, weil
submitted_at am Token bei Submission gespiegelt wird (siehe submit_anfrage
in anfrage_forms.py). Ein Token kann mehrere Responses haben (re-edit),
aber fuer die Status-Sicht reicht "schon mal eingegangen oder nicht".
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import AnfrageToken

STATUS_OFFEN = "offen"
STATUS_AUSGEFUELLT = "ausgefuellt"
STATUS_ABGELAUFEN = "abgelaufen"

STATUS_ICON = {
    STATUS_OFFEN: "📨",
    STATUS_AUSGEFUELLT: "✅",
    STATUS_ABGELAUFEN: "⌛",
}


@dataclass
class TokenStatusRow:
    token: str
    kunde_name: str | None
    kunde_email: str
    status: str
    created_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    reminder_sent_at: datetime | None


def _classify(token: AnfrageToken, now_utc: datetime) -> str:
    if token.submitted_at is not None:
        return STATUS_AUSGEFUELLT
    if token.expires_at > now_utc:
        return STATUS_OFFEN
    return STATUS_ABGELAUFEN


def _to_row(token: AnfrageToken, now_utc: datetime) -> TokenStatusRow:
    return TokenStatusRow(
        token=token.token,
        kunde_name=token.kunde_name,
        kunde_email=token.kunde_email,
        status=_classify(token, now_utc),
        created_at=token.created_at,
        expires_at=token.expires_at,
        submitted_at=token.submitted_at,
        reminder_sent_at=getattr(token, "reminder_sent_at", None),
    )


async def count_status_for_tenant(tenant_id: uuid.UUID) -> dict[str, int]:
    """Liefert die 3 Bucket-Counts der letzten 30 Tage.

    30-Tage-Fenster damit alte Tokens nicht ewig den Count blaehen —
    fuer die /formulare-Ueberschrift reicht "neuere Aktivitaet".
    Filterung in Python (wenig Rows pro Tenant zu erwarten).
    """
    now_utc = datetime.now(timezone.utc)
    cutoff_ts = now_utc.timestamp() - 30 * 86400
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AnfrageToken).where(AnfrageToken.tenant_id == tenant_id)
        )).scalars().all()
    counts = {STATUS_OFFEN: 0, STATUS_AUSGEFUELLT: 0, STATUS_ABGELAUFEN: 0}
    for r in rows:
        if r.created_at.timestamp() < cutoff_ts:
            continue
        counts[_classify(r, now_utc)] += 1
    return counts


async def list_recent_for_tenant(
    tenant_id: uuid.UUID, *, limit: int = 10, only_open: bool = False,
) -> list[TokenStatusRow]:
    """Letzte N Tokens (created_at DESC), optional nur OFFEN."""
    now_utc = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as s:
        q = (
            select(AnfrageToken)
            .where(AnfrageToken.tenant_id == tenant_id)
            .order_by(AnfrageToken.created_at.desc())
        )
        if only_open:
            q = q.where(
                AnfrageToken.submitted_at.is_(None),
                AnfrageToken.expires_at > now_utc,
            )
        else:
            q = q.limit(limit)
        rows = (await s.execute(q)).scalars().all()
    out = [_to_row(t, now_utc) for t in rows]
    if only_open:
        out = out[:limit]
    return out


REMINDER_LOOKBACK_DAYS = 14


async def find_open_tokens_for_reminder(
    tenant_id: uuid.UUID,
) -> list[AnfrageToken]:
    """Fuer den 24h-Reminder-Cron: Tokens die offen sind, noch keine
    Reminder-Mail bekommen haben, und juenger als REMINDER_LOOKBACK_DAYS
    sind. Caller filtert dann noch ueber den Calendar (matched ein Termin
    in [now+22h, now+26h]?).

    Der 14-Tage-Cutoff hat zwei Gruende:
    1. Performance: ohne Cutoff macht der Cron N*2 Calendar-API-Calls;
       in einem Tenant mit vielen Bestands-Tokens (Tests, alte Anfragen
       ohne Expiry-Refresh) wird das schnell rate-limit-relevant.
    2. Semantik: ein 'morgen findet ein Gespraech statt'-Reminder fuer
       ein Anfrage-Formular das vor 3 Wochen verschickt wurde, klingt
       fuer den Kunden unbeholfen — der wuerde sich fragen ob da was
       schiefgelaufen ist.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=REMINDER_LOOKBACK_DAYS)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(AnfrageToken)
            .where(AnfrageToken.tenant_id == tenant_id)
            .where(AnfrageToken.submitted_at.is_(None))
            .where(AnfrageToken.reminder_sent_at.is_(None))
            .where(AnfrageToken.expires_at > now_utc)
            .where(AnfrageToken.created_at > cutoff)
        )).scalars().all()
        for r in rows:
            s.expunge(r)
    return list(rows)


async def mark_reminder_sent(token_id: uuid.UUID) -> None:
    """Setzt reminder_sent_at = NOW() auf dem Token."""
    async with AsyncSessionLocal() as s:
        tok = (await s.execute(
            select(AnfrageToken).where(AnfrageToken.id == token_id)
            .with_for_update()
        )).scalar_one_or_none()
        if tok is None:
            return
        tok.reminder_sent_at = datetime.now(timezone.utc)
        await s.commit()
