"""
Telegram-Plugin: Push-Notifications + Empfang von Telegram-Updates.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    ALLE_KATEGORIEN,
    KATEGORIE_LABELS,
    STATE_BELEG_CONFIRMING,
    STATE_BELEG_WAITING_PHOTO,
    STATE_LEXWARE_SETUP_TOKEN,
    STATE_VIZ_WAITING_DESCRIPTION,
    STATE_VIZ_WAITING_KUNDE,
    STATE_VIZ_WAITING_PHOTO,
    STATE_WISSEN_KATEGORIE,
    STATE_WISSEN_LOESCHEN,
    STATE_WISSEN_TEXT,
    STATE_RECHNUNG_WAITING_INPUT,
    STATE_RECHNUNG_CONFIRMING,
    STATE_RECHNUNG_AWAITING_MAIL,
    Beleg,
    BELEG_SOURCE_TELEGRAM,
    BELEG_STATUS_ERROR,
    BELEG_STATUS_PENDING,
    BELEG_STATUS_UPLOADED,
    BELEG_STATUS_UPLOADING,
    Rechnung,
    RECHNUNG_INPUT_TEXT,
    RECHNUNG_INPUT_VOICE,
    RECHNUNG_STATUS_CANCELLED,
    RECHNUNG_STATUS_CREATING,
    RECHNUNG_STATUS_DRAFTED,
    RECHNUNG_STATUS_ERROR,
    RECHNUNG_STATUS_EXTRACTING,
    RECHNUNG_STATUS_PREVIEWING,
    Tenant,
    TenantKnowledge,
    TelegramState,
    ToolConfig,
    VIZ_STATUS_DONE,
    VIZ_STATUS_FAILED,
    VIZ_STATUS_GENERATING,
    VIZ_STATUS_PENDING,
    Visualisierung,
)
from core.security import decrypt, encrypt
from core.ai import extract_rechnung_from_audio, extract_rechnung_from_text
from core.integrations.lexware import LexwareProvider
from core.integrations.accounting_base import (
    AccountingError,
    InvoiceLineItem,
)
from core.plugin_system import BasePlugin
from plugins.telegram_notify.manifest import MANIFEST

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_SECONDS = 10.0
GLOBAL_TENANT_SLUG = "_global"
TELEGRAM_BOT_TOOL_NAME = "telegram_bot"
STATE_TTL_MINUTES = 30
WISSEN_MAX_LEN = 2000

class TelegramNotifier:
    @staticmethod
    async def send_for_tenant(tenant_id, text):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(ToolConfig).where(
                        ToolConfig.tenant_id == tenant_id,
                        ToolConfig.tool_name == "telegram_notify",
                    )
                )
                tc = result.scalar_one_or_none()
                if tc is None or not tc.enabled:
                    return False
                cfg = {**MANIFEST.default_config, **(tc.config or {})}
                bot_token = cfg.get("bot_token", "")
                chat_id = cfg.get("chat_id", "")
                if not bot_token or not chat_id:
                    return False
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Telegram-Versand fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def send_admin(bot_token, chat_id, text):
        if not bot_token or not chat_id:
            return False
        try:
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Admin-Telegram fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def _send_raw(bot_token, chat_id, text):
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(f"Telegram-API antwortete {resp.status_code}")
                return False
            return True

async def _load_state(chat_id):
    async with AsyncSessionLocal() as s:
        st = (await s.execute(
            select(TelegramState).where(TelegramState.chat_id == chat_id)
        )).scalar_one_or_none()
        if st is None:
            return None
        if st.expires_at and st.expires_at < dt.datetime.now(dt.timezone.utc):
            await s.delete(st)
            await s.commit()
            return None
        s.expunge(st)
        return st

async def _save_state(chat_id, state_key, state_data=None):
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=STATE_TTL_MINUTES)
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(TelegramState).where(TelegramState.chat_id == chat_id)
        )).scalar_one_or_none()
        if existing:
            existing.state_key = state_key
            existing.state_data = state_data or {}
            existing.expires_at = expires
        else:
            ns = TelegramState(
                chat_id=chat_id,
                state_key=state_key,
                state_data=state_data or {},
                expires_at=expires,
            )
            s.add(ns)
        await s.commit()

async def _clear_state(chat_id):
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(TelegramState).where(TelegramState.chat_id == chat_id)
        )).scalar_one_or_none()
        if existing:
            await s.delete(existing)
            await s.commit()

async def _get_tenant_by_chat(chat_id):
    async with AsyncSessionLocal() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.telegram_chat_id == chat_id)
        )).scalar_one_or_none()
        if t:
            s.expunge(t)
        return t

async def _load_global_bot_token():
    async with AsyncSessionLocal() as s:
        gt = (await s.execute(
            select(Tenant).where(Tenant.slug == GLOBAL_TENANT_SLUG)
        )).scalar_one_or_none()
        if not gt:
            return None
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == gt.id,
                ToolConfig.tool_name == TELEGRAM_BOT_TOOL_NAME,
            )
        )).scalar_one_or_none()
        if not tc or not tc.enabled:
            return None
        return (tc.config or {}).get("bot_token") or None

async def _send_to_chat(chat_id, text, bot_token=None):
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    return await TelegramNotifier._send_raw(bot_token, str(chat_id), text)

async def _handle_start_command(text, chat_id, from_data):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        msg = "Hallo! Dies ist der <b>Gewerbeagent-Bot</b>.\n\n"
        msg += "Falls Sie sich gerade einrichten, scannen Sie bitte den QR-Code, "
        msg += "den Sie von uns erhalten haben."
        return msg
    tenant_slug = parts[1].strip().lower()
    if not tenant_slug.replace("-", "").replace("_", "").isalnum():
        return "Aktivierungs-Link ungueltig. Bitte verwenden Sie den QR-Code."
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if tenant is None:
            return f"Aktivierungs-Link ungueltig (Tenant {tenant_slug} nicht gefunden)."
        if tenant_slug == GLOBAL_TENANT_SLUG:
            return "Dieser Aktivierungs-Link ist nicht fuer Endkunden bestimmt."
        if tenant.telegram_chat_id and tenant.telegram_chat_id != chat_id:
            logger.warning(f"Tenant {tenant_slug}: Chat-ID-Wechsel zu {chat_id}")
        tenant.telegram_chat_id = chat_id
        await s.commit()
        first_name = (from_data.get("first_name") or "").strip() or "dort"
        reply = f"Willkommen, {first_name}!\n\n"
        reply += f"Ihr Telegram ist jetzt mit <b>{tenant.company_name}</b> verbunden.\n\n"
        reply += "Ab jetzt erhalten Sie hier:\n"
        reply += "- Push-Nachrichten zu neuen Anrufen und Mails\n"
        reply += "- Bestaetigungen ueber gebuchte Termine\n"
        reply += "- Hinweise wenn Q nicht weiterkommt\n\n"
        reply += "Mit /help sehen Sie alle verfuegbaren Befehle."
        return reply

async def _handle_help_command():
    msg = "<b>Verfuegbare Befehle</b>\n\n"
    msg += "<b>Wissensbasis</b>\n"
    msg += "/wissen - neuen Eintrag anlegen\n"
    msg += "/wissen_anzeigen - alle Eintraege ansehen\n"
    msg += "/wissen_loeschen - Eintrag entfernen\n\n"
    msg += "<b>Visualisierung</b>\n"
    msg += "/visualisierung - Foto schicken, KI rendert was rein soll\n\n"
    msg += "<b>Lexware (Buchhaltung):</b>\n"
    msg += "/lexware_setup - Lexware verbinden\n"
    msg += "/lexware_status - Verbindung pruefen\n"
    msg += "/beleg - Beleg-Foto/PDF an Lexware schicken\n"
    msg += "/belege_anzeigen - letzte hochgeladene Belege\n"
    msg += "/rechnung - neue Rechnung anlegen (Text oder Sprache)\n"
    msg += "/rechnungen_anzeigen - letzte Rechnungen\n\n"
    msg += "<b>Allgemein</b>\n"
    msg += "/start - Bot mit Ihrem Betrieb verbinden\n"
    msg += "/status - Ist Ihr Agent aktiv?\n"
    msg += "/abbrechen - laufende Aktion abbrechen\n"
    msg += "/help - Diese Liste\n\n"
    msg += "Bei Fragen: hallo@gewerbeagent.de"
    return msg

async def _handle_status_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch <b>keinem Betrieb zugeordnet</b>.\n\nBitte scannen Sie den Aktivierungs-QR-Code."
    return f"<b>{tenant.company_name}</b>\nStatus: {tenant.status}\nSlug: {tenant.slug}"

async def _handle_unknown():
    return "Diesen Befehl kenne ich noch nicht.\n\nMit /help sehen Sie was ich kann."

async def _handle_abbrechen(chat_id):
    state = await _load_state(chat_id)
    await _clear_state(chat_id)
    if state:
        return "Abgebrochen. Mit /help sehen Sie alle Befehle."
    return "Es laeuft gerade keine Aktion. /help zeigt was ich kann."

async def _handle_wissen_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet.\nBitte zuerst Aktivierungs-QR-Code scannen."
    await _save_state(chat_id, STATE_WISSEN_KATEGORIE, {})
    msg = "<b>Was moechten Sie hinzufuegen?</b>\n\n"
    for i, key in enumerate(ALLE_KATEGORIEN, start=1):
        label = KATEGORIE_LABELS.get(key, key)
        msg += f"{i}) {label}\n"
    msg += f"\nAntworten Sie mit der Nummer (1-{len(ALLE_KATEGORIEN)}) oder /abbrechen."
    return msg

async def _handle_wissen_kategorie_input(chat_id, text):
    text = text.strip()
    if not text.isdigit():
        return f"Bitte antworten Sie mit einer Nummer von 1 bis {len(ALLE_KATEGORIEN)} oder /abbrechen."
    idx = int(text) - 1
    if idx < 0 or idx >= len(ALLE_KATEGORIEN):
        return f"Nur Nummern 1 bis {len(ALLE_KATEGORIEN)} sind gueltig. Bitte erneut waehlen."
    kategorie = ALLE_KATEGORIEN[idx]
    label = KATEGORIE_LABELS.get(kategorie, kategorie)
    await _save_state(chat_id, STATE_WISSEN_TEXT, {"kategorie": kategorie})
    msg = f"Kategorie: <b>{label}</b>\n\n"
    msg += "Was sollen wir uns merken? Schreiben Sie einfach den Inhalt in einer Nachricht.\n\n"
    msg += "<i>Beispiel: Wir verarbeiten Eiche, Buche und Kiefer. Tropenhoelzer nicht.</i>\n\n"
    msg += "/abbrechen um den Vorgang abzubrechen."
    return msg

async def _handle_wissen_text_input(chat_id, text, state_data):
    kategorie = (state_data or {}).get("kategorie")
    if not kategorie:
        await _clear_state(chat_id)
        return "Etwas ging schief. Bitte starten Sie mit /wissen erneut."
    text = text.strip()
    if len(text) < 5:
        return "Das ist sehr kurz. Bitte schreiben Sie etwas mehr Inhalt (mindestens 5 Zeichen) oder /abbrechen."
    if len(text) > WISSEN_MAX_LEN:
        return f"Das ist zu lang ({len(text)} Zeichen). Maximum {WISSEN_MAX_LEN} Zeichen pro Eintrag."
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. Bitte erneut /start ausfuehren."
    async with AsyncSessionLocal() as s:
        entry = TenantKnowledge(
            tenant_id=tenant.id,
            kategorie=kategorie,
            text=text,
        )
        s.add(entry)
        await s.commit()
    await _clear_state(chat_id)
    label = KATEGORIE_LABELS.get(kategorie, kategorie)
    msg = f"Gespeichert unter <b>{label}</b>.\n\n"
    msg += "/wissen - weiteren Eintrag anlegen\n"
    msg += "/wissen_anzeigen - alle Eintraege ansehen"
    return msg

async def _handle_wissen_anzeigen(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == tenant.id)
            .order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at)
        )).scalars().all()
    if not entries:
        return "Noch keine Wissens-Eintraege vorhanden.\n\nMit /wissen koennen Sie den ersten anlegen."
    by_kat = {}
    for e in entries:
        by_kat.setdefault(e.kategorie, []).append(e)
    msg = f"<b>Wissensbasis von {tenant.company_name}</b>\n\n"
    total = 0
    for kat in ALLE_KATEGORIEN:
        if kat not in by_kat:
            continue
        label = KATEGORIE_LABELS.get(kat, kat)
        msg += f"<b>{label}</b>\n"
        for e in by_kat[kat]:
            short = e.text if len(e.text) <= 200 else e.text[:200] + "..."
            msg += f"  - {short}\n"
            total += 1
        msg += "\n"
    msg += f"<i>Insgesamt {total} Eintraege.</i>\n"
    msg += "Mit /wissen_loeschen koennen Sie Eintraege entfernen."
    return msg

async def _handle_wissen_loeschen_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == tenant.id)
            .order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at)
        )).scalars().all()
    if not entries:
        return "Es gibt keine Eintraege zum Loeschen."
    id_map = {}
    msg = "<b>Welchen Eintrag loeschen?</b>\n\n"
    for i, e in enumerate(entries, start=1):
        id_map[str(i)] = str(e.id)
        label = KATEGORIE_LABELS.get(e.kategorie, e.kategorie)
        short = e.text if len(e.text) <= 100 else e.text[:100] + "..."
        msg += f"{i}) [{label}] {short}\n"
    msg += "\nAntworten Sie mit der Nummer oder /abbrechen."
    await _save_state(chat_id, STATE_WISSEN_LOESCHEN, {"id_map": id_map})
    return msg

async def _handle_wissen_loeschen_input(chat_id, text, state_data):
    text = text.strip()
    id_map = (state_data or {}).get("id_map") or {}
    if text not in id_map:
        return "Ungueltige Nummer. Bitte eine der angezeigten Nummern eingeben oder /abbrechen."
    entry_id = id_map[text]
    async with AsyncSessionLocal() as s:
        entry = (await s.execute(
            select(TenantKnowledge).where(TenantKnowledge.id == uuid.UUID(entry_id))
        )).scalar_one_or_none()
        if entry is None:
            await _clear_state(chat_id)
            return "Eintrag nicht mehr vorhanden (eventuell schon geloescht)."
        label = KATEGORIE_LABELS.get(entry.kategorie, entry.kategorie)
        await s.delete(entry)
        await s.commit()
    await _clear_state(chat_id)
    return f"Eintrag in <b>{label}</b> geloescht."

async def process_telegram_update(payload):
    # Callback-Query (Button-Klick) hat eigene Top-Level-Struktur
    callback_query = payload.get("callback_query")
    if callback_query:
        cq_id = callback_query.get("id")
        cq_data = callback_query.get("data") or ""
        cq_message = callback_query.get("message") or {}
        cq_chat_id = (cq_message.get("chat") or {}).get("id")
        if not cq_chat_id or not cq_data:
            return {"ok": True}
        bot_token = await _load_global_bot_token()
        logger.info(f"Callback-Query: chat={cq_chat_id} data={cq_data!r}")
        if cq_data.startswith("rg:"):
            await _handle_rechnung_callback(cq_chat_id, cq_data, cq_id, bot_token)
        else:
            # Unbekannte Callback-Daten - nur bestätigen
            await _answer_callback_query(cq_id, "Unbekannte Aktion", bot_token)
        return {"ok": True}

    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        return {"ok": True}
    text = (msg.get("text") or "").strip()
    photo_array = msg.get("photo") or []
    document = msg.get("document") or None
    voice = msg.get("voice") or None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_data = msg.get("from") or {}
    if not chat_id:
        return {"ok": True}
    logger.info(
        f"Telegram in: chat_id={chat_id} text={text[:100]!r} "
        f"has_photo={bool(photo_array)}"
    )

    # ----- Photo/Document-Pfad: nur wenn State darauf wartet -----
    if (photo_array or document) and not text:
        state = await _load_state(chat_id)
        bot_token = None
        if state and state.state_key in (STATE_VIZ_WAITING_PHOTO, STATE_BELEG_WAITING_PHOTO):
            bot_token = await _load_global_bot_token()
            if not bot_token:
                await _send_to_chat(
                    chat_id,
                    "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                )
                return {"ok": True}

        if state and state.state_key == STATE_VIZ_WAITING_PHOTO:
            if not photo_array:
                await _send_to_chat(
                    chat_id,
                    "Bitte ein Foto schicken (kein Dokument). Oder /abbrechen.",
                )
                return {"ok": True}
            reply = await _handle_viz_photo_received(chat_id, photo_array, bot_token)
            await _send_to_chat(chat_id, reply)
            return {"ok": True}

        if state and state.state_key == STATE_BELEG_WAITING_PHOTO:
            reply = await _handle_beleg_photo_received(
                chat_id, photo_array, document, bot_token
            )
            await _send_to_chat(chat_id, reply)
            return {"ok": True}

        # Foto/Dokument ohne aktiven State - ignorieren
        logger.info("Photo/Document ohne passenden State ignoriert")
        return {"ok": True}

    # ----- Voice-Pfad: nur wenn State darauf wartet -----
    if voice and not text:
        state = await _load_state(chat_id)
        if state and state.state_key == STATE_RECHNUNG_WAITING_INPUT:
            bot_token = await _load_global_bot_token()
            if not bot_token:
                await _send_to_chat(
                    chat_id,
                    "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                )
                return {"ok": True}
            reply = await _handle_rechnung_input_received(
                chat_id, voice_dict=voice, bot_token=bot_token
            )
            if reply:
                await _send_to_chat(chat_id, reply)
            return {"ok": True}
        # Voice-Note ohne aktiven Wizard - ignorieren
        logger.info("Voice ohne passenden State ignoriert")
        return {"ok": True}

    # ----- Text-Pfad -----
    if text == "/abbrechen":
        reply = await _handle_abbrechen(chat_id)
    elif text.startswith("/start"):
        await _clear_state(chat_id)
        reply = await _handle_start_command(text, chat_id, from_data)
    elif text == "/help":
        reply = await _handle_help_command()
    elif text == "/status":
        reply = await _handle_status_command(chat_id)
    elif text == "/wissen":
        reply = await _handle_wissen_command(chat_id)
    elif text == "/wissen_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_wissen_anzeigen(chat_id)
    elif text == "/wissen_loeschen":
        reply = await _handle_wissen_loeschen_command(chat_id)
    elif text == "/visualisierung":
        reply = await _handle_visualisierung_command(chat_id)
    elif text == "/lexware_setup":
        reply = await _handle_lexware_setup_command(chat_id)
    elif text == "/lexware_status":
        reply = await _handle_lexware_status_command(chat_id)
    elif text == "/beleg":
        reply = await _handle_beleg_command(chat_id)
    elif text == "/belege_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_belege_anzeigen_command(chat_id)
    elif text == "/rechnung":
        reply = await _handle_rechnung_command(chat_id)
    elif text == "/rechnungen_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_rechnungen_anzeigen_command(chat_id)
    elif text.startswith("/"):
        reply = await _handle_unknown()
    else:
        state = await _load_state(chat_id)
        if state is None:
            return {"ok": True}
        if state.state_key == STATE_WISSEN_KATEGORIE:
            reply = await _handle_wissen_kategorie_input(chat_id, text)
        elif state.state_key == STATE_WISSEN_TEXT:
            reply = await _handle_wissen_text_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_WISSEN_LOESCHEN:
            reply = await _handle_wissen_loeschen_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_VIZ_WAITING_PHOTO:
            reply = "Bitte schicken Sie ein Foto (kein Text). Oder /abbrechen."
        elif state.state_key == STATE_VIZ_WAITING_DESCRIPTION:
            reply = await _handle_viz_description_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_LEXWARE_SETUP_TOKEN:
            reply = await _handle_lexware_setup_token_input(chat_id, text)
        elif state.state_key == STATE_BELEG_WAITING_PHOTO:
            reply = "Bitte ein Foto oder PDF des Belegs schicken (kein Text). Oder /abbrechen."
        elif state.state_key == STATE_RECHNUNG_WAITING_INPUT:
            # Text-Eingabe waehrend Rechnungs-Wizard
            reply_or_none = await _handle_rechnung_input_received(chat_id, text=text)
            if reply_or_none is None:
                # Antwort wurde schon mit Buttons gesendet
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_RECHNUNG_CONFIRMING:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_RECHNUNG_AWAITING_MAIL:
            reply = "Bitte den \"Fertig\"-Button oben antippen oder /abbrechen schicken."
        else:
            await _clear_state(chat_id)
            return {"ok": True}
    await _send_to_chat(chat_id, reply)
    return {"ok": True}

# =====================================================================
# Visualisierung-Wizard (Phase 1: Telegram-Roundtrip)
# =====================================================================

VIZ_PROMPT_BOILERPLATE = (
    "Make this a photo-realistic professional rendering. "
    "Keep the existing room, walls, floor, ceiling, and proportions exactly intact. "
    "Use natural lighting and realistic shadows that match the scene. "
    "High quality craftsmanship aesthetic."
)


async def _telegram_get_file_path(bot_token, file_id):
    """Holt von Telegram den file_path zu einer file_id."""
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, params={"file_id": file_id})
        if resp.status_code != 200:
            logger.warning(f"getFile fehlgeschlagen: {resp.status_code}")
            return None
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"getFile not ok: {data}")
            return None
        return data["result"].get("file_path")


async def _telegram_download_file(bot_token, file_path):
    """Laedt eine Datei von Telegram-Servern als Bytes."""
    url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(f"file download fehlgeschlagen: {resp.status_code}")
            return None
        return resp.content


async def _send_photo_to_chat(chat_id, image_bytes, caption=None, bot_token=None):
    """Schickt ein Bild an einen Chat via sendPhoto."""
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendPhoto"
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, data=data, files=files)
        if resp.status_code != 200:
            logger.warning(f"sendPhoto fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
            return False
        return True


async def _handle_visualisierung_command(chat_id):
    """Startet den Visualisierungs-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )
    await _save_state(chat_id, STATE_VIZ_WAITING_PHOTO, {})
    msg = "<b>Visualisierung erstellen</b>\n\n"
    msg += "Schicken Sie mir bitte das <b>Foto</b> der Stelle, "
    msg += "wo z.B. eine Treppe, ein Moebel oder eine Kueche hinkommen soll.\n\n"
    msg += "<i>Tipp: Foto direkt aus der Telegram-Kamera oder aus der Galerie.</i>\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_viz_photo_received(chat_id, photo_array, bot_token):
    """User schickt Photo waehrend Wizard auf STATE_VIZ_WAITING_PHOTO steht."""
    if not photo_array:
        return "Ich habe das Foto nicht verarbeiten koennen. Versuchen Sie es nochmal."

    # Telegram schickt mehrere Aufloesungen, wir nehmen die groesste
    largest = max(photo_array, key=lambda p: p.get("file_size", 0) or 0)
    file_id = largest.get("file_id")
    if not file_id:
        return "Foto-ID fehlt - bitte erneut senden."

    file_path = await _telegram_get_file_path(bot_token, file_id)
    if not file_path:
        return "Konnte Foto nicht von Telegram laden. Bitte erneut senden."

    image_bytes = await _telegram_download_file(bot_token, file_path)
    if not image_bytes:
        return "Foto-Download fehlgeschlagen. Bitte erneut senden."

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden - bitte /start ausfuehren."

    # Visualisierung anlegen mit dem Bild
    async with AsyncSessionLocal() as s:
        viz = Visualisierung(
            tenant_id=tenant.id,
            chat_id=chat_id,
            original_image_data=image_bytes,
            status=VIZ_STATUS_PENDING,
        )
        s.add(viz)
        await s.commit()
        await s.refresh(viz)
        viz_id = str(viz.id)

    await _save_state(
        chat_id,
        STATE_VIZ_WAITING_DESCRIPTION,
        {"viz_id": viz_id},
    )

    msg = f"Foto erhalten ({len(image_bytes) // 1024} KB).\n\n"
    msg += "<b>Was soll dort hin?</b> Beschreiben Sie es kurz:\n"
    msg += "- Material (z.B. Eiche, Buche, Edelstahl)\n"
    msg += "- Stil (z.B. modern, klassisch)\n"
    msg += "- Details (z.B. 14 Stufen, Glasgelaender)\n\n"
    msg += "<i>Beispiel: Helle Eichentreppe geradlaeufig mit Edelstahl-Gelaender.</i>\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_viz_description_input(chat_id, text, state_data):
    """User schickt Beschreibung -> wir generieren das Bild."""
    text = text.strip()
    if len(text) < 5:
        return "Bitte etwas mehr Details (mindestens 5 Zeichen)."
    if len(text) > 500:
        return "Bitte kuerzer fassen (max 500 Zeichen)."

    viz_id = (state_data or {}).get("viz_id")
    if not viz_id:
        await _clear_state(chat_id)
        return "Kontext verloren. Bitte mit /visualisierung neu starten."

    # Sofort Feedback senden
    await _send_to_chat(
        chat_id,
        "<i>Ich rendere das Bild... das dauert ungefaehr 10-20 Sekunden.</i>",
    )

    import uuid as _uuid
    from core.ai import generate_image_from_image

    full_prompt = f"{text}. {VIZ_PROMPT_BOILERPLATE}"

    # Visualisierung holen + auf generating setzen
    async with AsyncSessionLocal() as s:
        viz = (await s.execute(
            select(Visualisierung).where(Visualisierung.id == _uuid.UUID(viz_id))
        )).scalar_one_or_none()
        if not viz:
            await _clear_state(chat_id)
            return "Visualisierungs-Auftrag nicht mehr vorhanden."
        viz.prompt = text
        viz.status = VIZ_STATUS_GENERATING
        await s.commit()
        original_bytes = viz.original_image_data

    # Bild generieren
    result_bytes = await generate_image_from_image(
        image_bytes=original_bytes,
        prompt=full_prompt,
        mime_type="image/jpeg",
    )

    # Status updaten
    async with AsyncSessionLocal() as s:
        viz = (await s.execute(
            select(Visualisierung).where(Visualisierung.id == _uuid.UUID(viz_id))
        )).scalar_one_or_none()
        if not viz:
            return "Visualisierungs-Auftrag verschwunden waehrend Generierung."

        if result_bytes:
            viz.result_image_data = result_bytes
            viz.status = VIZ_STATUS_DONE
            viz.completed_at = dt.datetime.now(dt.timezone.utc)
        else:
            viz.status = VIZ_STATUS_FAILED
            viz.error_message = "Modell hat kein Bild zurueckgegeben (evtl. Sicherheits-Block)"
        await s.commit()

    if not result_bytes:
        await _clear_state(chat_id)
        return (
            "Leider konnte ich kein Bild generieren. "
            "Moegliche Gruende: Sicherheits-Block durch das Modell, "
            "oder das Foto ist fuer die KI unklar.\n\n"
            "Versuchen Sie /visualisierung erneut mit einem anderen Foto oder Beschreibung."
        )

    # Bild an Chat schicken
    sent = await _send_photo_to_chat(
        chat_id,
        result_bytes,
        caption=f"<b>Visualisierung fertig.</b>\n<i>{text}</i>",
    )
    if not sent:
        await _clear_state(chat_id)
        return "Bild generiert, aber Versand an Telegram fehlgeschlagen."

    # Wizard zu Ende - kein Mail-Versand in Phase 1
    await _clear_state(chat_id)
    return (
        "Mit /visualisierung koennen Sie eine weitere Visualisierung starten."
    )


# =====================================================================
# Lexware-Setup-Wizard
# =====================================================================

LEXWARE_TOOL_NAME = "lexware"


async def _get_lexware_config(tenant_id):
    """Holt die Lexware-ToolConfig fuer einen Tenant. None wenn nicht eingerichtet."""
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant_id,
                ToolConfig.tool_name == LEXWARE_TOOL_NAME,
            )
        )).scalar_one_or_none()
        if tc:
            s.expunge(tc)
        return tc


async def _save_lexware_config(tenant_id, encrypted_api_key, organization_id=None):
    """Legt oder aktualisiert die Lexware-ToolConfig."""
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == tenant_id,
                ToolConfig.tool_name == LEXWARE_TOOL_NAME,
            )
        )).scalar_one_or_none()
        if tc is None:
            tc = ToolConfig(
                tenant_id=tenant_id,
                tool_name=LEXWARE_TOOL_NAME,
                enabled=True,
                config={},
            )
            s.add(tc)
        cfg = dict(tc.config or {})
        cfg["encrypted_api_key"] = encrypted_api_key
        if organization_id:
            cfg["organization_id"] = organization_id
        tc.config = cfg
        tc.enabled = True
        await s.commit()


async def _get_lexware_provider_for_tenant(tenant):
    """Baut einen LexwareProvider aus der ToolConfig des Tenants. None wenn nicht eingerichtet."""
    if not tenant:
        return None
    tc = await _get_lexware_config(tenant.id)
    if not tc or not tc.enabled:
        return None
    cfg = tc.config or {}
    encrypted = cfg.get("encrypted_api_key")
    if not encrypted:
        return None
    try:
        api_key = decrypt(encrypted)
    except Exception as e:
        logger.warning(f"Lexware-API-Key Entschluesselung fehlgeschlagen: {e}")
        return None
    if not api_key:
        return None
    return LexwareProvider(api_key=api_key)


async def _handle_lexware_setup_command(chat_id):
    """Startet den Lexware-Setup-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )

    existing = await _get_lexware_config(tenant.id)
    bereits_eingerichtet = ""
    if existing and (existing.config or {}).get("encrypted_api_key"):
        bereits_eingerichtet = (
            "\n\n<i>Hinweis: Lexware ist schon verbunden. "
            "Wenn Sie einen neuen Schluessel eingeben, ueberschreibt das den alten.</i>"
        )

    await _save_state(chat_id, STATE_LEXWARE_SETUP_TOKEN, {})
    msg = "<b>Lexware verbinden</b>\n\n"
    msg += "So gehts:\n"
    msg += "1. Im Browser <b>app.lexware.de</b> oeffnen und einloggen\n"
    msg += "2. <b>Erweiterungen / Apps -> Public API</b> oeffnen\n"
    msg += "3. Auf <b>API-Schluessel erstellen</b> klicken\n"
    msg += "4. Schluessel kopieren und mir hier einfuegen\n\n"
    msg += "<i>Der Schluessel wird verschluesselt gespeichert.</i>\n\n"
    msg += f"/abbrechen um abzubrechen.{bereits_eingerichtet}"
    return msg


async def _handle_lexware_setup_token_input(chat_id, text):
    """User schickt den API-Key als Text."""
    api_key = (text or "").strip()
    # Plausibilisierung: Lexware-Keys sind ~48 Zeichen, alphanumerisch
    if len(api_key) < 20:
        return "Das sieht nicht wie ein API-Schluessel aus. Bitte den vollstaendigen Schluessel einfuegen oder /abbrechen."
    if len(api_key) > 200:
        return "Das ist zu lang fuer einen API-Schluessel. Bitte nochmal pruefen oder /abbrechen."
    if " " in api_key or "\n" in api_key:
        return "Der Schluessel enthaelt Leerzeichen oder Zeilenumbrueche. Bitte sauber kopieren oder /abbrechen."

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden - bitte /start ausfuehren."

    # Live-Test: Health-Check gegen Lexware
    try:
        provider = LexwareProvider(api_key=api_key)
        profile = await provider.health_check()
    except AccountingError as e:
        await _clear_state(chat_id)
        if e.status_code == 401:
            return (
                "Der Schluessel wurde von Lexware abgelehnt (401).\n"
                "Bitte einen neuen Schluessel im Lexware-Profil erstellen "
                "und mit /lexware_setup nochmal versuchen."
            )
        return (
            f"Lexware antwortet mit Fehler (HTTP {e.status_code}).\n"
            "Bitte spaeter erneut /lexware_setup versuchen."
        )
    except Exception as e:
        logger.exception(f"Lexware-Setup unerwarteter Fehler: {e}")
        await _clear_state(chat_id)
        return "Verbindung zu Lexware fehlgeschlagen. Bitte spaeter erneut versuchen."

    # Verschluesselt speichern
    encrypted = encrypt(api_key)
    organization_id = profile.get("organizationId") if isinstance(profile, dict) else None
    await _save_lexware_config(tenant.id, encrypted, organization_id)
    await _clear_state(chat_id)

    features = ", ".join(profile.get("businessFeatures") or []) if isinstance(profile, dict) else "-"
    msg = "<b>Lexware verbunden.</b>\n\n"
    msg += f"Org-ID: <code>{organization_id or '-'}</code>\n"
    msg += f"Features: {features}\n\n"
    msg += "Jetzt koennen Sie mit /beleg Belege hochladen."
    return msg


async def _handle_lexware_status_command(chat_id):
    """Zeigt aktuellen Lexware-Verbindungs-Status."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        return (
            "Lexware ist <b>nicht verbunden</b>.\n\n"
            "Mit /lexware_setup koennen Sie Ihren API-Schluessel hinterlegen."
        )
    try:
        profile = await provider.health_check()
    except Exception as e:
        logger.warning(f"Lexware-Status Health-Check fehlgeschlagen: {e}")
        return (
            "Lexware ist eingerichtet, aber der Verbindungs-Test ist fehlgeschlagen.\n"
            "Eventuell wurde der Schluessel widerrufen. Mit /lexware_setup neu einrichten."
        )
    org = profile.get("organizationId", "-")
    features = ", ".join(profile.get("businessFeatures") or []) or "-"
    return (
        "<b>Lexware ist verbunden.</b>\n\n"
        f"Org-ID: <code>{org}</code>\n"
        f"Features: {features}"
    )


# =====================================================================
# Beleg-Wizard
# =====================================================================

# Erlaubte MIME-Types fuer Beleg-Upload (Lexware akzeptiert nur diese)
BELEG_ALLOWED_MIMES = {"image/jpeg", "image/png", "application/pdf"}
BELEG_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB - Lexware-Limit


def _hash_bytes(data: bytes) -> str:
    """SHA256-Hex eines Byte-Strings (fuer Idempotenz)."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


async def _handle_beleg_command(chat_id):
    """Startet den Beleg-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )
    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        return (
            "Lexware ist noch nicht verbunden.\n\n"
            "Bitte zuerst mit /lexware_setup den API-Schluessel hinterlegen."
        )
    await _save_state(chat_id, STATE_BELEG_WAITING_PHOTO, {})
    msg = "<b>Beleg an Lexware schicken</b>\n\n"
    msg += "Schicken Sie mir bitte ein <b>Foto</b> oder <b>PDF</b> des Belegs "
    msg += "(z.B. Tankquittung, Material-Rechnung).\n\n"
    msg += "Ich lege ihn unbearbeitet in Lexware ab. "
    msg += "Sie koennen ihn dort dann pruefen und verbuchen.\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_beleg_photo_received(chat_id, photo_array, document, bot_token):
    """User schickt Foto oder PDF im Beleg-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden - bitte /start ausfuehren."

    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        await _clear_state(chat_id)
        return (
            "Lexware ist nicht mehr verbunden.\n"
            "Bitte mit /lexware_setup neu einrichten."
        )

    # Foto oder Dokument? -> file_id + MIME bestimmen
    file_id = None
    mime_type = None
    original_filename = None

    if photo_array:
        # Foto: groesste Aufloesung waehlen, ist immer JPEG
        largest = max(photo_array, key=lambda p: p.get("file_size", 0) or 0)
        file_id = largest.get("file_id")
        mime_type = "image/jpeg"
        original_filename = "telegram_photo.jpg"
    elif document:
        file_id = document.get("file_id")
        mime_type = document.get("mime_type") or "application/octet-stream"
        original_filename = document.get("file_name") or "beleg"

    if not file_id:
        return "Datei-ID fehlt - bitte erneut senden."

    if mime_type not in BELEG_ALLOWED_MIMES:
        await _clear_state(chat_id)
        return (
            f"Dateityp <code>{mime_type}</code> wird von Lexware nicht akzeptiert.\n\n"
            "Erlaubt sind: JPEG-Fotos, PNG-Bilder, PDF-Dateien.\n\n"
            "Bitte das Bild als JPEG/PNG senden oder als PDF exportieren, "
            "dann mit /beleg neu starten."
        )

    # Datei laden
    file_path = await _telegram_get_file_path(bot_token, file_id)
    if not file_path:
        return "Konnte Datei nicht von Telegram laden. Bitte erneut senden."

    file_bytes = await _telegram_download_file(bot_token, file_path)
    if not file_bytes:
        return "Datei-Download fehlgeschlagen. Bitte erneut senden."

    if len(file_bytes) > BELEG_MAX_SIZE_BYTES:
        await _clear_state(chat_id)
        return (
            f"Datei ist zu gross ({len(file_bytes) // 1024 // 1024} MB).\n"
            f"Maximum sind {BELEG_MAX_SIZE_BYTES // 1024 // 1024} MB. "
            "Bitte Foto in niedrigerer Aufloesung oder PDF komprimieren."
        )

    file_hash = _hash_bytes(file_bytes)

    # Idempotenz-Check: gleicher Hash schon mal hochgeladen?
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(Beleg).where(
                Beleg.tenant_id == tenant.id,
                Beleg.file_hash == file_hash,
            )
        )).scalar_one_or_none()

    if existing and existing.status == BELEG_STATUS_UPLOADED and existing.lexware_voucher_id:
        await _clear_state(chat_id)
        deeplink = LexwareProvider.voucher_deeplink(existing.lexware_voucher_id)
        msg = "<b>Diesen Beleg gibts schon in Lexware.</b>\n\n"
        msg += "(Selber Datei-Inhalt wurde frueher schon hochgeladen.)\n\n"
        msg += f'<a href="{deeplink}">In Lexware oeffnen</a>'
        return msg

    # Beleg in DB anlegen (Status pending)
    beleg_id = None
    async with AsyncSessionLocal() as s:
        if existing:
            # Re-Use vom alten Eintrag (z.B. wenn vorheriger Upload Error war)
            beleg = existing
            beleg.file_data = file_bytes
            beleg.file_mime = mime_type
            beleg.file_size = len(file_bytes)
            beleg.original_filename = original_filename
            beleg.chat_id = chat_id
            beleg.status = BELEG_STATUS_UPLOADING
            beleg.upload_attempts = (beleg.upload_attempts or 0) + 1
            beleg.error_message = None
            s.add(beleg)
        else:
            beleg = Beleg(
                tenant_id=tenant.id,
                chat_id=chat_id,
                file_data=file_bytes,
                file_mime=mime_type,
                file_hash=file_hash,
                file_size=len(file_bytes),
                original_filename=original_filename,
                source=BELEG_SOURCE_TELEGRAM,
                status=BELEG_STATUS_UPLOADING,
                upload_attempts=1,
            )
            s.add(beleg)
        await s.commit()
        await s.refresh(beleg)
        beleg_id = beleg.id

    # Sofort Feedback senden
    await _send_to_chat(
        chat_id,
        f"<i>Lade {len(file_bytes) // 1024} KB an Lexware hoch...</i>",
    )

    # Lexware-Upload
    try:
        result = await provider.upload_voucher_file(
            file_bytes=file_bytes,
            mime_type=mime_type,
            filename=original_filename,
        )
    except AccountingError as e:
        async with AsyncSessionLocal() as s:
            beleg = (await s.execute(
                select(Beleg).where(Beleg.id == beleg_id)
            )).scalar_one_or_none()
            if beleg:
                beleg.status = BELEG_STATUS_ERROR
                beleg.error_message = str(e)[:500]
                await s.commit()
        await _clear_state(chat_id)
        return (
            f"Lexware-Upload fehlgeschlagen (HTTP {e.status_code}).\n\n"
            "Sie koennen es mit /beleg erneut versuchen. "
            "Falls es weiterhin scheitert: /lexware_status pruefen."
        )
    except Exception as e:
        logger.exception(f"Lexware-Upload unerwartet fehlgeschlagen: {e}")
        async with AsyncSessionLocal() as s:
            beleg = (await s.execute(
                select(Beleg).where(Beleg.id == beleg_id)
            )).scalar_one_or_none()
            if beleg:
                beleg.status = BELEG_STATUS_ERROR
                beleg.error_message = f"Unerwartet: {str(e)[:400]}"
                await s.commit()
        await _clear_state(chat_id)
        return "Lexware-Upload fehlgeschlagen. Bitte spaeter mit /beleg erneut versuchen."

    # Erfolg in DB festhalten
    async with AsyncSessionLocal() as s:
        beleg = (await s.execute(
            select(Beleg).where(Beleg.id == beleg_id)
        )).scalar_one_or_none()
        if beleg:
            beleg.status = BELEG_STATUS_UPLOADED
            beleg.lexware_file_id = result.file_id
            beleg.lexware_voucher_id = result.voucher_id
            beleg.uploaded_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()

    await _clear_state(chat_id)
    deeplink = LexwareProvider.voucher_deeplink(result.voucher_id) if result.voucher_id else None
    msg = "<b>Beleg an Lexware uebergeben.</b>\n\n"
    if deeplink:
        msg += f'<a href="{deeplink}">In Lexware oeffnen und verbuchen</a>\n\n'
    msg += "<i>Lexware hat den Beleg im Status \"unchecked\" angelegt. "
    msg += "Bitte in Lexware Datum, Lieferant und Betrag pruefen und ergaenzen.</i>\n\n"
    msg += "Mit /beleg koennen Sie den naechsten Beleg schicken."
    return msg


async def _handle_belege_anzeigen_command(chat_id):
    """Zeigt die letzten 10 Belege des Tenants."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    async with AsyncSessionLocal() as s:
        belege = (await s.execute(
            select(Beleg)
            .where(Beleg.tenant_id == tenant.id)
            .order_by(Beleg.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not belege:
        return "Noch keine Belege hochgeladen.\n\nMit /beleg den ersten anlegen."

    lines = ["<b>Letzte Belege:</b>\n"]
    for b in belege:
        ts = b.created_at.strftime("%d.%m %H:%M") if b.created_at else "-"
        size_kb = (b.file_size or 0) // 1024
        if b.status == BELEG_STATUS_UPLOADED and b.lexware_voucher_id:
            link = LexwareProvider.voucher_deeplink(b.lexware_voucher_id)
            lines.append(f'• {ts} ({size_kb} KB) <a href="{link}">in Lexware</a>')
        elif b.status == BELEG_STATUS_ERROR:
            err = (b.error_message or "?")[:80]
            lines.append(f'• {ts} ({size_kb} KB) Fehler: <i>{err}</i>')
        else:
            lines.append(f'• {ts} ({size_kb} KB) Status: {b.status}')
    return "\n".join(lines)




# =====================================================================
# Inline-Buttons-Helper (Telegram-Reply mit Buttons)
# =====================================================================

async def _send_with_inline_buttons(chat_id, text, buttons, bot_token=None):
    """
    Schickt Nachricht mit Inline-Keyboard.
    buttons: list[list[dict]]: jede Reihe ist eine Liste von {text, callback_data}.
    Beispiel:
      [[{"text": "Anlegen", "callback_data": "rechnung:confirm:UUID"}]]
    """
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False

    inline_keyboard = []
    for row in buttons:
        kb_row = []
        for btn in row:
            kb_row.append({
                "text": btn["text"],
                "callback_data": btn["callback_data"],
            })
        inline_keyboard.append(kb_row)

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": inline_keyboard},
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning(f"sendMessage(buttons) fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
            return False
        return True


async def _answer_callback_query(callback_query_id, text=None, bot_token=None):
    """Bestaetigt einen Button-Klick (entfernt das Lade-Symbol in Telegram)."""
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]  # Telegram max 200 chars
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            logger.warning(f"answerCallbackQuery fehlgeschlagen: {e}")
            return False
    return True


# =====================================================================
# Voice-Helper (Telegram Voice-Notes laden)
# =====================================================================

async def _telegram_download_voice(bot_token, voice_dict):
    """
    Telegram-Voice-Note runterladen.
    voice_dict aus message['voice']: {file_id, duration, mime_type, file_size}
    Gibt (audio_bytes, mime_type) zurueck oder (None, None) bei Fehler.
    """
    file_id = voice_dict.get("file_id") if voice_dict else None
    if not file_id:
        return None, None
    file_path = await _telegram_get_file_path(bot_token, file_id)
    if not file_path:
        return None, None
    audio_bytes = await _telegram_download_file(bot_token, file_path)
    if not audio_bytes:
        return None, None
    mime_type = (voice_dict or {}).get("mime_type") or "audio/ogg"
    return audio_bytes, mime_type


# =====================================================================
# Rechnung-Wizard
# =====================================================================

# Limits + Defaults
RECHNUNG_MAX_INPUT_LEN = 1000        # Text-Eingabe Maximum
RECHNUNG_MAX_VOICE_BYTES = 5_000_000 # 5 MB Audio-Maximum
RECHNUNG_VOICE_MAX_SECONDS = 120     # 2 Minuten max


def _format_rechnung_preview(extracted: dict, confidence_warning: str = "") -> str:
    """Baut die Vorschau-Nachricht aus extrahierten Daten."""
    kn = extracted.get("kunde_name") or "<i>(fehlt)</i>"
    ko = extracted.get("kunde_ort") or ""
    ks = extracted.get("kunde_strasse") or ""
    kp = extracted.get("kunde_plz") or ""

    addr_extra = []
    if ks:
        addr_extra.append(ks)
    if kp or ko:
        addr_extra.append(f"{kp} {ko}".strip())
    addr_str = ", ".join(addr_extra) if addr_extra else (ko or "<i>(unbekannt)</i>")

    leistung = extracted.get("leistung_titel") or "<i>(fehlt)</i>"
    leistung_desc = extracted.get("leistung_beschreibung")
    betrag = extracted.get("betrag_brutto_eur")
    betrag_str = f"{betrag:.2f} \u20ac brutto" if betrag is not None else "<i>(fehlt)</i>"

    msg = "<b>Rechnung-Vorschau</b>\n\n"
    msg += "Verstanden:\n"
    msg += f"\u2022 <b>Kunde:</b>      {kn}\n"
    msg += f"\u2022 <b>Anschrift:</b>  {addr_str}\n"
    msg += f"\u2022 <b>Leistung:</b>   {leistung}\n"
    if leistung_desc:
        msg += f"  <i>{leistung_desc}</i>\n"
    msg += f"\u2022 <b>Betrag:</b>     {betrag_str}\n"

    missing = extracted.get("missing_fields") or []
    if missing:
        msg += f"\n\u26a0\ufe0f Unklar: {', '.join(missing)}\n"
    if confidence_warning:
        msg += f"\n\u26a0\ufe0f {confidence_warning}\n"

    msg += (
        "\n<i>Hinweis: Die Anschrift musst du in Lexware ggf. vervollstaendigen, "
        "bevor du die Rechnung finalisierst.</i>"
    )
    return msg


async def _handle_rechnung_command(chat_id):
    """Startet den Rechnung-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )
    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        return (
            "Lexware ist noch nicht verbunden.\n\n"
            "Bitte zuerst mit /lexware_setup den API-Schluessel hinterlegen."
        )

    await _save_state(chat_id, STATE_RECHNUNG_WAITING_INPUT, {})
    msg = "<b>Rechnung erstellen</b>\n\n"
    msg += "Beschreibe was die Rechnung enthaelt - per Text oder per Sprachnachricht.\n\n"
    msg += "<b>Beispiel:</b>\n"
    msg += "<i>\"Rechnung an Frau Mueller in Trier, Moebelmontage Schreibtisch und Regal, 350 Euro brutto\"</i>\n\n"
    msg += "Ich extrahiere Kunde, Leistung und Betrag und zeige dir eine Vorschau bevor irgendwas in Lexware angelegt wird.\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_rechnung_input_received(
    chat_id,
    text=None,
    voice_dict=None,
    bot_token=None,
):
    """
    Tenant hat Text oder Sprachnachricht geschickt.
    Wir rufen Gemini auf, speichern in DB, zeigen Vorschau mit Buttons.
    """
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return None  # Antwort haben wir schon im Helper-Pfad

    # Sofort Feedback senden waehrend Gemini laeuft
    if voice_dict:
        await _send_to_chat(chat_id, "<i>Hoere mir die Sprachnachricht an...</i>")
    else:
        await _send_to_chat(chat_id, "<i>Verarbeite deine Eingabe...</i>")

    # Eingabe normalisieren + an Gemini schicken
    raw_text = None
    audio_bytes = None
    audio_mime = None
    input_type = None

    if voice_dict:
        if bot_token is None:
            bot_token = await _load_global_bot_token()
        audio_bytes, audio_mime = await _telegram_download_voice(bot_token, voice_dict)
        if not audio_bytes:
            await _clear_state(chat_id)
            return "Konnte die Sprachnachricht nicht laden. Bitte erneut versuchen."
        if len(audio_bytes) > RECHNUNG_MAX_VOICE_BYTES:
            await _clear_state(chat_id)
            return "Die Sprachnachricht ist zu lang. Bitte kuerzer fassen oder als Text schicken."
        input_type = RECHNUNG_INPUT_VOICE
    elif text:
        if len(text) > RECHNUNG_MAX_INPUT_LEN:
            await _clear_state(chat_id)
            return f"Eingabe zu lang (max {RECHNUNG_MAX_INPUT_LEN} Zeichen). Bitte kuerzer fassen."
        raw_text = text.strip()
        input_type = RECHNUNG_INPUT_TEXT
    else:
        await _clear_state(chat_id)
        return "Keine Eingabe erkannt. /rechnung erneut starten."

    # Gemini-Extraktion
    try:
        if input_type == RECHNUNG_INPUT_VOICE:
            extracted = await extract_rechnung_from_audio(audio_bytes, mime_type=audio_mime)
        else:
            extracted = await extract_rechnung_from_text(raw_text)
    except Exception as e:
        logger.exception(f"Gemini-Extraktion fehlgeschlagen: {e}")
        await _clear_state(chat_id)
        return "KI-Extraktion fehlgeschlagen. Bitte spaeter erneut versuchen oder /rechnung neu starten."

    # In DB anlegen
    rechnung_id = None
    async with AsyncSessionLocal() as s:
        rg = Rechnung(
            tenant_id=tenant.id,
            chat_id=chat_id,
            input_type=input_type,
            raw_input_text=raw_text,
            transcript=extracted.get("transcript"),
            extracted_data=extracted,
            kunde_name=extracted.get("kunde_name"),
            kunde_ort=extracted.get("kunde_ort"),
            kunde_strasse=extracted.get("kunde_strasse"),
            kunde_plz=extracted.get("kunde_plz"),
            kunde_email=extracted.get("kunde_email"),
            leistung_titel=extracted.get("leistung_titel"),
            leistung_beschreibung=extracted.get("leistung_beschreibung"),
            betrag_brutto_eur=extracted.get("betrag_brutto_eur"),
            status=RECHNUNG_STATUS_PREVIEWING,
        )
        s.add(rg)
        await s.commit()
        await s.refresh(rg)
        rechnung_id = rg.id

    # Vorschau-Nachricht
    confidence = extracted.get("extraction_confidence", "low")
    confidence_warning = ""
    if confidence == "low":
        confidence_warning = "Niedrige Erkennungs-Konfidenz - bitte sorgfaeltig pruefen."
    elif confidence == "medium":
        confidence_warning = "Mittlere Erkennungs-Konfidenz - bitte sorgfaeltig pruefen."

    # Pflichtfelder pruefen - bei wesentlich Fehlendem, kein Best.-Button
    has_minimum = bool(
        extracted.get("kunde_name")
        and extracted.get("leistung_titel")
        and (extracted.get("betrag_brutto_eur") is not None)
    )

    preview_text = _format_rechnung_preview(extracted, confidence_warning)

    # State weiter
    await _save_state(
        chat_id,
        STATE_RECHNUNG_CONFIRMING,
        {"rechnung_id": str(rechnung_id)},
    )

    # Inline-Buttons
    if has_minimum:
        buttons = [
            [{"text": "\u2705 In Lexware anlegen", "callback_data": f"rg:confirm:{rechnung_id}"}],
            [
                {"text": "\u270f\ufe0f Neu eingeben", "callback_data": f"rg:retry:{rechnung_id}"},
                {"text": "\u274c Abbrechen", "callback_data": f"rg:cancel:{rechnung_id}"},
            ],
        ]
    else:
        # Keine Confirm-Option weil Pflichtfelder fehlen
        buttons = [
            [{"text": "\u270f\ufe0f Neu eingeben", "callback_data": f"rg:retry:{rechnung_id}"}],
            [{"text": "\u274c Abbrechen", "callback_data": f"rg:cancel:{rechnung_id}"}],
        ]

    if bot_token is None:
        bot_token = await _load_global_bot_token()
    await _send_with_inline_buttons(chat_id, preview_text, buttons, bot_token=bot_token)
    return None  # Schon mit Buttons gesendet


async def _handle_rechnung_callback(chat_id, callback_data, callback_query_id, bot_token):
    """
    User hat einen Button geklickt.
    callback_data Format: rg:<action>:<rechnung_id>
    actions: confirm, retry, cancel, send_mail, finish
    """
    parts = callback_data.split(":")
    if len(parts) < 3:
        await _answer_callback_query(callback_query_id, "Ungueltige Aktion", bot_token)
        return

    action = parts[1]
    rechnung_id_str = parts[2]
    try:
        import uuid as _uuid
        rechnung_id = _uuid.UUID(rechnung_id_str)
    except Exception:
        await _answer_callback_query(callback_query_id, "Ungueltige ID", bot_token)
        return

    # Schnelle Bestaetigung des Klicks (sonst dreht sich Telegram-Spinner ewig)
    await _answer_callback_query(callback_query_id, bot_token=bot_token)

    if action == "cancel":
        await _mark_rechnung_cancelled(rechnung_id)
        await _clear_state(chat_id)
        await _send_to_chat(chat_id, "Abgebrochen. /rechnung um neu zu starten.")
        return

    if action == "retry":
        await _mark_rechnung_cancelled(rechnung_id)
        await _save_state(chat_id, STATE_RECHNUNG_WAITING_INPUT, {})
        await _send_to_chat(
            chat_id,
            "OK, beschreibe die Rechnung nochmal - per Text oder Sprachnachricht.\n\n/abbrechen um abzubrechen.",
        )
        return

    if action == "confirm":
        await _create_rechnung_in_lexware(chat_id, rechnung_id, bot_token)
        return

    if action == "finish":
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            "Fertig. Die Rechnung liegt als Entwurf in Lexware. /rechnung um eine neue anzulegen.",
        )
        return

    await _send_to_chat(chat_id, f"Unbekannte Aktion: {action}")


async def _mark_rechnung_cancelled(rechnung_id):
    """Setzt Status auf cancelled (Audit-Trail)."""
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if rg and rg.status not in (RECHNUNG_STATUS_DRAFTED,):
            rg.status = RECHNUNG_STATUS_CANCELLED
            await s.commit()


async def _create_rechnung_in_lexware(chat_id, rechnung_id, bot_token):
    """Erstellt die Lexware-Rechnung (Draft) aus den extrahierten Daten."""
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if not rg:
            await _send_to_chat(chat_id, "Rechnung nicht mehr gefunden.")
            return

        if rg.status == RECHNUNG_STATUS_DRAFTED and rg.lexware_invoice_id:
            # Schon angelegt
            link = LexwareProvider.invoice_deeplink_view(rg.lexware_invoice_id)
            await _send_to_chat(
                chat_id,
                f"Rechnung wurde bereits in Lexware angelegt: <a href=\"{link}\">oeffnen</a>",
            )
            return

        rg.status = RECHNUNG_STATUS_CREATING
        await s.commit()
        # Wir lesen nochmal die Felder die wir brauchen
        kunde_name = rg.kunde_name
        kunde_ort = rg.kunde_ort
        kunde_strasse = rg.kunde_strasse
        kunde_plz = rg.kunde_plz
        leistung_titel = rg.leistung_titel
        leistung_beschreibung = rg.leistung_beschreibung
        betrag = float(rg.betrag_brutto_eur) if rg.betrag_brutto_eur is not None else 0.0

    tenant = await _get_tenant_by_chat(chat_id)
    provider = await _get_lexware_provider_for_tenant(tenant) if tenant else None
    if not provider:
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.status = RECHNUNG_STATUS_ERROR
                rg.error_message = "Lexware-Provider nicht verfuegbar"
                await s.commit()
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            "Lexware-Verbindung weg. Bitte mit /lexware_setup neu einrichten.",
        )
        return

    # LineItem bauen
    line_item = InvoiceLineItem(
        name=leistung_titel or "Leistung",
        quantity=1,
        unit_name="Stueck",
        unit_price_gross=betrag,
        description=leistung_beschreibung,
        tax_rate_percent=19,
    )

    # One-time-Address (auch wenn nicht alles gefuellt - Lexware akzeptiert Teil-Adressen)
    address = {
        "name": kunde_name or "Kunde",
        "countryCode": "DE",
    }
    if kunde_strasse:
        address["street"] = kunde_strasse
    if kunde_plz:
        address["zip"] = kunde_plz
    if kunde_ort:
        address["city"] = kunde_ort

    await _send_to_chat(chat_id, "<i>Lege Rechnungs-Entwurf in Lexware an...</i>")

    try:
        draft = await provider.create_invoice_draft(
            line_items=[line_item],
            one_time_address=address,
            title="Rechnung",
            introduction="Vielen Dank fuer Ihren Auftrag.",
            remark="Zahlbar innerhalb 14 Tagen ohne Abzug.",
            tax_type="gross",
        )
    except AccountingError as e:
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.status = RECHNUNG_STATUS_ERROR
                rg.error_message = str(e)[:500]
                await s.commit()
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            f"Lexware-Fehler beim Anlegen (HTTP {e.status_code}). Bitte spaeter erneut versuchen.",
        )
        return
    except Exception as e:
        logger.exception(f"create_invoice_draft fehlgeschlagen: {e}")
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.status = RECHNUNG_STATUS_ERROR
                rg.error_message = f"Unerwartet: {str(e)[:400]}"
                await s.commit()
        await _clear_state(chat_id)
        await _send_to_chat(chat_id, "Unerwarteter Fehler beim Anlegen. Bitte spaeter erneut versuchen.")
        return

    # Erfolg in DB festhalten
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if rg:
            rg.status = RECHNUNG_STATUS_DRAFTED
            rg.lexware_invoice_id = draft.invoice_id
            rg.drafted_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()

    # Erfolgs-Nachricht mit Folge-Buttons
    msg = "<b>Entwurf in Lexware angelegt.</b>\n\n"
    msg += f'<a href="{draft.deeplink_view}">In Lexware oeffnen und pruefen</a>\n\n'
    msg += (
        "<i>Bitte Anschrift vervollstaendigen, ggf. korrigieren und in Lexware "
        "finalisieren. Mailversand mit PDF folgt in einer der naechsten Updates.</i>"
    )

    buttons = [
        [{"text": "Fertig", "callback_data": f"rg:finish:{rechnung_id}"}],
    ]
    await _save_state(chat_id, STATE_RECHNUNG_AWAITING_MAIL, {"rechnung_id": str(rechnung_id)})
    await _send_with_inline_buttons(chat_id, msg, buttons, bot_token=bot_token)


async def _handle_rechnungen_anzeigen_command(chat_id):
    """Zeigt die letzten 10 Rechnungen des Tenants."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    async with AsyncSessionLocal() as s:
        rechnungen = (await s.execute(
            select(Rechnung)
            .where(Rechnung.tenant_id == tenant.id)
            .order_by(Rechnung.created_at.desc())
            .limit(10)
        )).scalars().all()

    if not rechnungen:
        return "Noch keine Rechnungen erstellt.\n\nMit /rechnung die erste anlegen."

    lines = ["<b>Letzte Rechnungen:</b>\n"]
    for rg in rechnungen:
        ts = rg.created_at.strftime("%d.%m %H:%M") if rg.created_at else "-"
        kunde = rg.kunde_name or "?"
        betrag = f"{float(rg.betrag_brutto_eur):.0f}\u20ac" if rg.betrag_brutto_eur is not None else "?"
        if rg.status == RECHNUNG_STATUS_DRAFTED and rg.lexware_invoice_id:
            link = LexwareProvider.invoice_deeplink_view(rg.lexware_invoice_id)
            lines.append(f'\u2022 {ts} {kunde} {betrag} <a href="{link}">in Lexware</a>')
        elif rg.status == RECHNUNG_STATUS_ERROR:
            err = (rg.error_message or "?")[:50]
            lines.append(f'\u2022 {ts} {kunde} {betrag} <i>Fehler: {err}</i>')
        elif rg.status == RECHNUNG_STATUS_CANCELLED:
            lines.append(f'\u2022 {ts} {kunde} {betrag} (abgebrochen)')
        else:
            lines.append(f'\u2022 {ts} {kunde} {betrag} Status: {rg.status}')
    return "\n".join(lines)



class Plugin(BasePlugin):
    manifest = MANIFEST

    async def on_webhook(self, endpoint, payload):
        logger.info(f"Telegram-Webhook empfangen: endpoint={endpoint}")
        if endpoint == "incoming":
            return await process_telegram_update(payload)
        return {"ok": True, "note": f"unknown endpoint: {endpoint}"}
