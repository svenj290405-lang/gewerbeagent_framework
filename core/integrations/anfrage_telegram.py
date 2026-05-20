"""Drive-Archiv wenn Kunde Anfrage-Formular abschickt.

Kein Telegram-Push mehr — siehe notify_tenant_anfrage_submitted-Docstring
fuer die Begruendung.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import AnfrageToken, Tenant

logger = logging.getLogger(__name__)

_BERLIN_TZ = ZoneInfo("Europe/Berlin")


def _format_value_plain(value) -> str:
    """Plain-Text-Renderer fuer Antwort-Werte (fuer Drive-.txt-Export).

    Listen aus File-Dicts werden zu '1 Datei(en): foto.jpg' kompakt;
    sonstige Listen werden Comma-joined; Skalare werden zu str().
    """
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and v.get("filename") for v in value):
            names = [v.get("filename", "?") for v in value]
            return f"{len(value)} Datei(en): " + ", ".join(names)
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


def _extract_files(antworten: dict) -> list[dict]:
    """Sammelt alle hochgeladenen Files aus den Antworten."""
    files = []
    for key, value in antworten.items():
        if not isinstance(value, list):
            continue
        for v in value:
            if isinstance(v, dict) and v.get("filename") and v.get("base64"):
                files.append({
                    "field": key,
                    "filename": v["filename"],
                    "content_type": v.get("content_type") or "application/octet-stream",
                    "base64": v["base64"],
                })
    return files


def _build_antworten_text_file(
    *, token_obj: AnfrageToken, antworten: dict, now_berlin: datetime,
) -> tuple[str, bytes]:
    """Baut den Drive-Text-File-Inhalt + Filename aus einer Submission.

    Filename: anfrage_YYYY-MM-DD_HH-MM-SS.txt — sortiert chronologisch
    im Drive-Ordner. Sekunden-Praezision reicht fuer Eindeutigkeit
    (gleichzeitige Submissions desselben Kunden im selben Sekunden-
    fenster sind praktisch ausgeschlossen).
    """
    filename = f"anfrage_{now_berlin.strftime('%Y-%m-%d_%H-%M-%S')}.txt"

    kunde = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    lines = [
        f"Anfrage von: {kunde}",
        f"E-Mail: {token_obj.kunde_email or '—'}",
        f"Eingegangen: {now_berlin.strftime('%Y-%m-%d %H:%M:%S')} Europe/Berlin",
    ]
    if token_obj.original_subject:
        lines.append(f"Original-Betreff: {token_obj.original_subject}")
    lines.append("")
    lines.append("Antworten")
    lines.append("─" * 40)
    for key, value in (antworten or {}).items():
        if not value:
            continue
        label = key.replace("_", " ").title()
        rendered = _format_value_plain(value)
        if "\n" in rendered:
            lines.append(f"{label}:")
            lines.append(rendered)
        else:
            lines.append(f"{label}: {rendered}")
        lines.append("")
    return filename, ("\n".join(lines) + "\n").encode("utf-8")


async def _save_submission_to_drive(
    *, tenant: Tenant, token_obj: AnfrageToken, antworten: dict,
    employee_id, now_berlin: datetime,
) -> str | None:
    """Schreibt Text-Datei + alle Form-Uploads in den Kunden-Drive-Ordner.

    Files bekommen den gleichen Timestamp-Praefix wie die Text-Datei,
    damit alle Artefakte einer Submission im Ordner zusammen stehen:

        anfrage_2026-05-18_15-30-45.txt
        anfrage_2026-05-18_15-30-45__foto_kueche.jpg
        anfrage_2026-05-18_15-30-45__skizze.pdf

    Returns: Drive-Folder-URL bei Erfolg (fuer Anzeige im Telegram-
    Push), None bei Fehler. Eigene Fehler werden geloggt — Drive ist
    Backup, DB-Response bleibt Source of Truth.
    """
    import base64 as _b64
    from core.integrations.google_drive import upload_file_to_kunde_folder

    kunde_name = token_obj.kunde_name or token_obj.kunde_email or "Unbekannt"
    txt_filename, txt_content = _build_antworten_text_file(
        token_obj=token_obj, antworten=antworten, now_berlin=now_berlin,
    )
    prefix = txt_filename.removesuffix(".txt")

    folder_url = None
    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tenant.id,
            kunde_name=kunde_name,
            file_bytes=txt_content,
            filename=txt_filename,
            mime_type="text/plain; charset=utf-8",
            employee_id=employee_id,
        )
        folder_url = result.get("kunde_folder_url")
        logger.info(
            f"Anfrage-Text in Drive abgelegt: tenant={tenant.slug} "
            f"kunde={kunde_name} file={txt_filename}"
        )
    except Exception as e:
        logger.warning(f"Drive-Save (Text) fehlgeschlagen: {e}")
        return None

    # Alle Form-Uploads (Fotos, PDFs) mit gleichem Praefix nachschieben.
    # Per-File-Try damit ein einzelner Fehler nicht den ganzen Batch killt.
    for f in _extract_files(antworten):
        try:
            raw = _b64.b64decode(f["base64"])
        except Exception as e:
            logger.warning(f"Drive-File-Decode fehlgeschlagen {f['filename']}: {e}")
            continue
        try:
            await upload_file_to_kunde_folder(
                tenant_id=tenant.id,
                kunde_name=kunde_name,
                file_bytes=raw,
                filename=f"{prefix}__{f['filename']}"[:200],
                mime_type=f["content_type"],
                employee_id=employee_id,
            )
        except Exception as e:
            logger.warning(
                f"Drive-Save (File {f['filename']}) fehlgeschlagen: {e}"
            )
    return folder_url


def _anliegen_text_from_antworten(antworten: dict) -> str:
    """Aggregiert die Freitext-Antworten zu einem Skill-Routing-Input.

    Listen/Dicts (z.B. Multi-Select, File-Uploads) werden ausgelassen —
    der Skill-Router matcht nur ueber Substring-Vergleich auf
    KEYWORD_TO_SKILL und braucht Freitext, nicht strukturierte Daten.
    """
    parts = []
    for value in (antworten or {}).values():
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


async def notify_tenant_anfrage_submitted(token_str: str, antworten: dict) -> None:
    """Verarbeitet eine Formular-Submission: schreibt alles ins Drive-Archiv.

    Kein Telegram-Push mehr — die Eingaenge sammeln sich im Kunden-Drive-
    Ordner an, der Handwerker pruefen via /formulare (Status-Liste)
    oder direkt im Drive ueber /archiv <kunde>. So fluten viele
    parallele Anfragen nicht mehr den Chat.

    Routing fuer den Drive-Owner (welcher Mitarbeiter Drive-Credentials
    nutzt) — in dieser Reihenfolge:
    1. `AnfrageToken.assigned_employee_id` — sticky.
    2. `choose_employee()` ueber den aggregierten Antwort-Text.
    """
    from core.routing.employee_router import choose_employee

    async with AsyncSessionLocal() as session:
        token_obj = (await session.execute(
            select(AnfrageToken).where(AnfrageToken.token == token_str)
        )).scalar_one_or_none()
        if not token_obj:
            return

        tenant = (await session.execute(
            select(Tenant).where(Tenant.id == token_obj.tenant_id)
        )).scalar_one_or_none()
        if not tenant:
            return

    sticky_emp_id = getattr(token_obj, "assigned_employee_id", None)
    employee_id = sticky_emp_id
    if employee_id is None:
        try:
            routing = await choose_employee(
                tenant_id=tenant.id,
                anliegen_text=_anliegen_text_from_antworten(antworten),
            )
            employee_id = routing.employee_id if routing else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"anfrage_telegram: choose_employee failed: {e}")

    now_berlin = datetime.now(_BERLIN_TZ)
    drive_folder_url = await _save_submission_to_drive(
        tenant=tenant, token_obj=token_obj, antworten=antworten,
        employee_id=employee_id, now_berlin=now_berlin,
    )

    # Dankes-Mail an den Kunden + Konversation fuer Reply-Threading
    # vorbereiten. Damit kann der Kunde direkt mit einem Wunschtermin
    # antworten und die Mail-Pipeline laesst die Termin-Buchung zu
    # (Formular-Status ist jetzt submitted). Best-effort — Drive-Save
    # und Submission-Persistenz haengen nicht davon ab.
    await _send_dank_mail_and_thread(
        tenant=tenant, token_obj=token_obj, employee_id=employee_id,
        drive_folder_url=drive_folder_url,
    )


async def _send_dank_mail_and_thread(
    *, tenant: Tenant, token_obj: AnfrageToken, employee_id,
    drive_folder_url: str | None,
) -> None:
    """Schickt die Dankes-Mail nach Formular-Eingang und legt/aktualisiert
    die EmailConversation, damit Folge-Mails (Wunschtermin) ueber das
    Reply-Threading in den Dialog-Pfad laufen.

    drive_folder_url wird (falls vorhanden) an der Konversation vermerkt,
    damit die spaetere Termin-Buchung den Link in die Kalender-Event-
    Beschreibung schreiben kann.
    """
    kunde_email = (token_obj.kunde_email or "").strip()
    if not kunde_email:
        return
    try:
        from core.integrations.mail_pipeline import (
            send_formular_dank_mail, find_open_conversation,
            create_conversation, record_outbound_q_reply,
            set_conversation_drive_url,
        )
        from core.integrations.mail_template import extract_first_name
        from core.models import STATE_DIALOG, STATE_BOOKED

        kunde_anrede = extract_first_name(token_obj.kunde_name or "") or ""
        company_name = tenant.company_name or "Handwerksbetrieb"
        contact_name = getattr(tenant, "contact_name", "") or ""
        contact_phone = getattr(tenant, "contact_phone", "") or ""

        # Konversation zuerst suchen — daraus ergibt sich, ob schon ein
        # Termin besteht (neuer Flow: erst Termin, dann Formular). Davon
        # haengt der Text der Dankes-Mail ab: bei bestehendem Termin wird
        # NICHT erneut nach einem Wunschtermin gefragt.
        conv = await find_open_conversation(tenant.id, kunde_email)
        termin_besteht = bool(
            conv is not None and (
                getattr(conv, "state", None) == STATE_BOOKED
                or getattr(conv, "gcal_event_id", None)
            )
        )

        sent_meta = await send_formular_dank_mail(
            tenant_id=tenant.id,
            to_email=kunde_email,
            kunde_anrede=kunde_anrede,
            company_name=company_name,
            contact_name=contact_name,
            contact_phone=contact_phone,
            original_subject=token_obj.original_subject,
            employee_id=employee_id,
            termin_besteht=termin_besteht,
        )
        if not sent_meta.get("success"):
            logger.warning(
                f"Dank-Mail nach Formular fehlgeschlagen tenant={tenant.slug} "
                f"kunde={kunde_email}: {sent_meta.get('error')}"
            )
            return

        # Konversation anlegen falls noch keine existiert (reiner Angebots-
        # Pfad ohne vorherige Dialog-/Termin-Konv).
        if conv is None:
            conv = await create_conversation(
                tenant_id=tenant.id,
                sender_email=kunde_email,
                sender_name=token_obj.kunde_name,
                subject=token_obj.original_subject,
                state=STATE_DIALOG,
                assigned_employee_id=employee_id,
            )
        await record_outbound_q_reply(
            conv.id,
            internet_message_id=sent_meta.get("internet_message_id"),
            microsoft_conversation_id=sent_meta.get("conversation_id"),
            q_reply_text="[Dankes-Mail nach Formular-Eingang]",
            subject=token_obj.original_subject,
        )
        if drive_folder_url:
            await set_conversation_drive_url(conv.id, drive_folder_url)
        logger.info(
            f"Dank-Mail nach Formular gesendet tenant={tenant.slug} "
            f"kunde={kunde_email} conv_id={conv.id} "
            f"drive={'yes' if drive_folder_url else 'no'}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"_send_dank_mail_and_thread fehlgeschlagen "
            f"tenant={tenant.slug}: {e}"
        )
