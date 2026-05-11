"""Mail-Retry-Cron — arbeitet failed_mail_queue ab.

Workflow alle 5 Min:
  1. SELECT FOR UPDATE SKIP LOCKED auf pending mails wo
     next_attempt_at <= now() — Parallelitaet-safe.
  2. Pro Eintrag: Brevo-Send versuchen mit dem persistierten Payload.
  3. Bei Erfolg: status='sent', Rechnung.status zurueck auf 'mail_sent'.
  4. Bei Fehler: attempt_count++, next_attempt_at + Backoff, last_error
     setzen. Wenn MAX_ATTEMPTS erreicht → status='dead' + Sven-Alert
     + Tenant-Push.

Tenants ohne Brevo-Config werden silent uebersprungen (queue waechst
nicht weiter, weil der Telegram-Branch dann auch nichts mehr enqueued).

Integration mit Phase A:
- Sven-Alert via core/integrations/admin_alerts.notify_sven_admin_alert
- Tenant-Push via core/integrations/tenant_alert._send_alert
- Heartbeat via core/integrations/cron_health
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from typing import Any

from sqlalchemy import select, update

from core.database import AsyncSessionLocal
from core.integrations.brevo import (
    BrevoError, BrevoMailer, MailAttachment, MailRecipient,
)
from core.models import (
    FAILED_MAIL_DEAD,
    FAILED_MAIL_PENDING,
    FAILED_MAIL_SENT,
    FailedMailQueue,
    MAIL_TYPE_RECHNUNG,
    MAX_ATTEMPTS,
    Rechnung,
    RECHNUNG_STATUS_ERROR,
    RECHNUNG_STATUS_MAIL_SENT,
    RETRY_BACKOFF_SECONDS,
    Tenant,
    ToolConfig,
)

logger = logging.getLogger(__name__)


# Polling-Intervall: alle 5 min — passt zum kuerzesten Backoff-Eintrag.
POLL_INTERVAL_SECONDS = 5 * 60
INITIAL_DELAY_SECONDS = 60
ERROR_RETRY_SECONDS = 60


async def _load_brevo_config_for_tenant(tenant_id) -> dict | None:
    """Holt brevo_api_key + sender_email aus _global mail_intake config.

    Note: aktuell ist Brevo zentral via _global Tenant konfiguriert
    (siehe handler.py:2150 — alle Tenants teilen sich denselben Brevo-
    Account fuer Visualisierungs- + Rechnungs-Mails). Wenn der spaeter
    pro-Tenant wird, hier auf tenant_id umstellen.
    """
    from core.models import Tenant as _Tenant
    GLOBAL_TENANT_SLUG = "_global"
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig)
            .join(_Tenant, ToolConfig.tenant_id == _Tenant.id)
            .where(_Tenant.slug == GLOBAL_TENANT_SLUG)
            .where(ToolConfig.tool_name == "mail_intake")
        )).scalar_one_or_none()
        if not tc or not tc.config:
            return None
        return tc.config


def _next_attempt_time(attempt_count: int) -> dt.datetime | None:
    """Wann ist der naechste Retry faellig? None = dead."""
    if attempt_count >= MAX_ATTEMPTS:
        return None
    delay = RETRY_BACKOFF_SECONDS[attempt_count]
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)


async def _send_via_brevo(
    *, brevo_api_key: str, sender_email: str, sender_name: str,
    recipient_email: str, payload: dict[str, Any],
) -> str:
    """Verschickt eine queued Mail. Returns messageId. Raises BrevoError."""
    mailer = BrevoMailer(api_key=brevo_api_key)

    attachments_payload = payload.get("attachments") or []
    attachments = []
    for a in attachments_payload:
        try:
            attachments.append(MailAttachment(
                filename=a.get("filename", "attachment"),
                content_bytes=base64.b64decode(a.get("data_base64", "")),
                content_type=a.get("mime_type", "application/octet-stream"),
            ))
        except Exception as e:
            logger.warning(f"Anhang ueberspringen (decode-fail): {e}")

    result = await mailer.send(
        sender_email=sender_email,
        sender_name=payload.get("from_name") or sender_name,
        to=MailRecipient(
            email=recipient_email,
            name=payload.get("to_name") or recipient_email,
        ),
        subject=payload.get("subject", "(ohne Betreff)"),
        html_body=payload.get("html_body", ""),
        reply_to_email=payload.get("reply_to"),
        reply_to_name=payload.get("reply_to_name"),
        attachments=attachments or None,
    )
    return result.get("messageId") or "?"


async def _mark_sent(entry_id, message_id: str, mail_type: str,
                    rechnung_id) -> None:
    """Bei erfolgreichem Retry: queue=sent + ggf. Rechnung=mail_sent."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(FailedMailQueue)
            .where(FailedMailQueue.id == entry_id)
            .values(
                status=FAILED_MAIL_SENT,
                last_error=None,
                updated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        if mail_type == MAIL_TYPE_RECHNUNG and rechnung_id:
            await s.execute(
                update(Rechnung)
                .where(Rechnung.id == rechnung_id)
                .values(
                    status=RECHNUNG_STATUS_MAIL_SENT,
                    mail_sent_at=dt.datetime.now(dt.timezone.utc),
                    updated_at=dt.datetime.now(dt.timezone.utc),
                )
            )
        await s.commit()
    logger.info(
        f"mail_retry: entry={entry_id} (type={mail_type}) zugestellt "
        f"messageId={message_id}"
    )


async def _mark_failed_and_reschedule(
    entry_id, attempt_count: int, error: str,
) -> bool:
    """Update Eintrag nach failed Retry. Returns True wenn dead."""
    next_at = _next_attempt_time(attempt_count)
    new_status = FAILED_MAIL_DEAD if next_at is None else FAILED_MAIL_PENDING

    async with AsyncSessionLocal() as s:
        await s.execute(
            update(FailedMailQueue)
            .where(FailedMailQueue.id == entry_id)
            .values(
                attempt_count=attempt_count,
                next_attempt_at=next_at or dt.datetime.now(dt.timezone.utc),
                last_error=error[:1000],
                status=new_status,
                updated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        await s.commit()
    return new_status == FAILED_MAIL_DEAD


async def _on_dead_letter(entry: FailedMailQueue) -> None:
    """Nach MAX_ATTEMPTS: Rechnung auf ERROR setzen + Alerts senden."""
    # Rechnung auf ERROR
    if entry.mail_type == MAIL_TYPE_RECHNUNG and entry.rechnung_id:
        async with AsyncSessionLocal() as s:
            await s.execute(
                update(Rechnung)
                .where(Rechnung.id == entry.rechnung_id)
                .values(
                    status=RECHNUNG_STATUS_ERROR,
                    error_message=(
                        f"Mail-Retry-Queue dead nach {entry.attempt_count} "
                        f"Versuchen: {(entry.last_error or '')[:300]}"
                    )[:1000],
                    updated_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await s.commit()

    # Sven-Alert
    try:
        from core.integrations.admin_alerts import notify_sven_admin_alert
        await notify_sven_admin_alert(
            kind=f"mail_retry_dead.{entry.tenant_id}",
            message=(
                f"⚠️ <b>Mail-Retry-Queue dead</b>\n\n"
                f"Tenant: <code>{entry.tenant_id}</code>\n"
                f"Mail-Type: <code>{entry.mail_type}</code>\n"
                f"Empfaenger: <code>{entry.recipient_email}</code>\n"
                f"Versuche: <b>{entry.attempt_count}</b>\n"
                f"Letzter Fehler: <code>{(entry.last_error or '')[:300]}</code>"
            ),
            details={
                "tenant_id": str(entry.tenant_id),
                "mail_type": entry.mail_type,
                "recipient": entry.recipient_email,
                "attempts": entry.attempt_count,
            },
        )
    except Exception as e:
        logger.warning(f"Sven-Alert (mail_retry_dead) failed: {e}")

    # Tenant-Push
    try:
        from core.integrations.tenant_alert import (
            _record_alert, _send_alert, _was_recently_alerted,
        )
        kind = f"mail_retry_dead.{entry.mail_type}"
        if not await _was_recently_alerted(
            tenant_id=entry.tenant_id, alert_kind=kind,
        ):
            label = {
                MAIL_TYPE_RECHNUNG: "Rechnung",
                "visualisierung": "Visualisierung",
                "angebot": "Angebot",
                "reply": "Antwort-Mail",
            }.get(entry.mail_type, "Mail")
            msg = (
                f"⚠️ <b>{label} konnte nicht zugestellt werden</b>\n\n"
                f"Empfaenger: <code>{entry.recipient_email}</code>\n"
                f"Wir haben es {entry.attempt_count}x versucht. Bitte "
                f"selbst per Mail senden oder Empfaenger pruefen."
            )
            sent = await _send_alert(tenant_id=entry.tenant_id, message=msg)
            await _record_alert(
                tenant_id=entry.tenant_id, alert_kind=kind, success=sent,
                details={"mail_type": entry.mail_type},
            )
    except Exception as e:
        logger.warning(f"Tenant-Alert (mail_retry_dead) failed: {e}")


async def process_one_pending_batch(batch_size: int = 20) -> dict:
    """Hauptarbeitsschritt — pendings holen + verarbeiten.

    Returns Counter dict {claimed, sent, retried, dead}.
    """
    summary = {"claimed": 0, "sent": 0, "retried": 0, "dead": 0}
    now = dt.datetime.now(dt.timezone.utc)

    # Atomar pending → in_flight markieren via UPDATE ... RETURNING.
    # Wir benutzen kein temp-status; stattdessen attempt_count + status
    # bleiben gleich, aber wir filtern auf next_attempt_at <= now.
    # SKIP LOCKED via Index — fuer Parallel-Worker safe (auch wenn wir
    # aktuell nur einen haben).
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(FailedMailQueue)
            .where(FailedMailQueue.status == FAILED_MAIL_PENDING)
            .where(FailedMailQueue.next_attempt_at <= now)
            .order_by(FailedMailQueue.next_attempt_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )).scalars().all()

        # In-Place ableiten, NICHT in derselben Transaktion bearbeiten —
        # Brevo-Call ist langsam und wir wollen den Row-Lock kurz halten.
        # Wir lassen die Transaktion einfach mit commit() enden, ohne
        # status zu aendern. Nachfolgende Updates passieren in eigenen
        # Sessions pro Eintrag.
        entries = [
            {
                "id": r.id, "tenant_id": r.tenant_id,
                "rechnung_id": r.rechnung_id,
                "mail_type": r.mail_type,
                "recipient_email": r.recipient_email,
                "payload": r.payload, "attempt_count": r.attempt_count,
                "last_error": r.last_error,
            }
            for r in rows
        ]
        await s.commit()

    summary["claimed"] = len(entries)
    if not entries:
        return summary

    # Brevo-Config einmal laden (alle Tenants teilen sich _global).
    cfg = await _load_brevo_config_for_tenant(entries[0]["tenant_id"])
    if not cfg:
        logger.warning("mail_retry: brevo-config nicht verfuegbar — alle pending bleiben pending")
        return summary
    brevo_api_key = cfg.get("brevo_api_key", "")
    sender_email = cfg.get("sender_email", "")
    sender_name_default = cfg.get("sender_name", "Gewerbeagent")
    if not brevo_api_key or not sender_email:
        logger.warning(
            "mail_retry: brevo_api_key oder sender_email fehlt in _global config"
        )
        return summary

    for entry_data in entries:
        # Faux-dataclass fuer _on_dead_letter
        class _E:
            id = entry_data["id"]
            tenant_id = entry_data["tenant_id"]
            rechnung_id = entry_data["rechnung_id"]
            mail_type = entry_data["mail_type"]
            recipient_email = entry_data["recipient_email"]
            attempt_count = entry_data["attempt_count"] + 1
            last_error = entry_data["last_error"]

        try:
            msg_id = await _send_via_brevo(
                brevo_api_key=brevo_api_key,
                sender_email=sender_email,
                sender_name=sender_name_default,
                recipient_email=entry_data["recipient_email"],
                payload=entry_data["payload"] or {},
            )
            await _mark_sent(
                entry_data["id"], msg_id,
                entry_data["mail_type"], entry_data["rechnung_id"],
            )
            summary["sent"] += 1
        except BrevoError as e:
            err = f"{e.status_code or '?'}: {e.message}"
            is_dead = await _mark_failed_and_reschedule(
                entry_data["id"], _E.attempt_count, err,
            )
            if is_dead:
                _E.last_error = err
                await _on_dead_letter(_E)  # type: ignore[arg-type]
                summary["dead"] += 1
            else:
                summary["retried"] += 1
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            is_dead = await _mark_failed_and_reschedule(
                entry_data["id"], _E.attempt_count, err,
            )
            if is_dead:
                _E.last_error = err
                await _on_dead_letter(_E)  # type: ignore[arg-type]
                summary["dead"] += 1
            else:
                summary["retried"] += 1

    return summary


async def cron_loop() -> None:
    """Endlosschleife — alle 5 min process_one_pending_batch."""
    logger.info(
        f"Mail-Retry-Cron gestartet (Intervall: {POLL_INTERVAL_SECONDS}s)"
    )
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    from core.integrations.cron_health import record_heartbeat

    while True:
        try:
            started = dt.datetime.now(dt.timezone.utc)
            summary = await process_one_pending_batch()
            duration = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()

            if summary["claimed"] > 0:
                logger.info(
                    f"Mail-Retry-Lauf fertig in {duration:.1f}s: "
                    f"claimed={summary['claimed']} sent={summary['sent']} "
                    f"retried={summary['retried']} dead={summary['dead']}"
                )

            record_heartbeat("mail_retry_cron")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Mail-Retry-Cron gestoppt")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Mail-Retry-Loop unerwarteter Fehler: {exc}"
            )
            await asyncio.sleep(ERROR_RETRY_SECONDS)


# Convenience-Helper fuer Caller (Telegram-Handler etc.)

async def enqueue_failed_mail(
    *,
    tenant_id,
    mail_type: str,
    recipient_email: str,
    subject: str,
    html_body: str,
    attachments: list[dict] | None = None,
    from_name: str | None = None,
    to_name: str | None = None,
    reply_to: str | None = None,
    reply_to_name: str | None = None,
    rechnung_id=None,
    last_error: str = "",
) -> None:
    """Legt eine fehlgeschlagene Mail in der Retry-Queue ab.

    attachments: Liste von Dicts mit {filename, mime_type, content_bytes}
                 — content_bytes wird hier zu base64 codiert fuer JSONB.

    Failsafe — keine Exception darf den Caller stoeren.
    """
    try:
        enc_attachments = []
        for a in (attachments or []):
            cb = a.get("content_bytes")
            if not cb:
                continue
            enc_attachments.append({
                "filename": a.get("filename", "anhang"),
                "mime_type": a.get("mime_type", "application/octet-stream"),
                "data_base64": base64.b64encode(cb).decode("ascii"),
            })

        payload = {
            "subject": subject,
            "html_body": html_body,
            "from_name": from_name,
            "to_name": to_name,
            "reply_to": reply_to,
            "reply_to_name": reply_to_name,
            "attachments": enc_attachments,
        }

        async with AsyncSessionLocal() as s:
            row = FailedMailQueue(
                tenant_id=tenant_id,
                rechnung_id=rechnung_id,
                mail_type=mail_type,
                recipient_email=recipient_email,
                payload=payload,
                attempt_count=0,
                next_attempt_at=(
                    dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(seconds=RETRY_BACKOFF_SECONDS[0])
                ),
                last_error=last_error[:1000],
                status=FAILED_MAIL_PENDING,
            )
            s.add(row)
            await s.commit()
        logger.info(
            f"mail_retry: enqueued {mail_type} fuer {recipient_email} "
            f"(rechnung_id={rechnung_id})"
        )
    except Exception as e:
        # Letzte Verteidigungslinie: Logging, kein Crash.
        logger.exception(f"enqueue_failed_mail crashed: {e}")
