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
    STATE_AUFNAHME_WAITING_AUDIO,
    STATE_AUFNAHME_PREVIEWING,
    STATE_LEISTUNG_WAITING_NAME,
    STATE_LEISTUNG_WAITING_PREIS,
    STATE_LEISTUNG_WAITING_BESCHREIBUNG,
    STATE_LEISTUNG_PREVIEWING,
    STATE_WERKSTATT_WAITING_ADDRESS,
    STATE_WERKSTATT_CONFIRMING,
    STATE_MITARBEITER_NEU_NAME,
    STATE_MITARBEITER_NEU_SKILLS,
    STATE_FORMULAR_TYP_WAEHLEN,
    STATE_FORMULAR_HAUPTMENU,
    STATE_FORMULAR_NEU_NAME,
    STATE_FORMULAR_NEU_LABEL,
    STATE_FORMULAR_NEU_TYP,
    STATE_FORMULAR_NEU_OPTIONEN,
    STATE_FORMULAR_NEU_REQUIRED,
    STATE_FORMULAR_LOESCHEN,
    STATE_FORMULAR_RESET_CONFIRM,
    Beleg,
    BELEG_SOURCE_TELEGRAM,
    BELEG_STATUS_ERROR,
    BELEG_STATUS_PENDING,
    BELEG_STATUS_UPLOADED,
    BELEG_STATUS_UPLOADING,
    Rechnung,
    RechnungPosition,
    Kundengespraech,
    RECHNUNG_INPUT_TEXT,
    RECHNUNG_INPUT_VOICE,
    RECHNUNG_STATUS_BEZAHLT,
    RECHNUNG_STATUS_CANCELLED,
    RECHNUNG_STATUS_CREATING,
    RECHNUNG_STATUS_DRAFTED,
    RECHNUNG_STATUS_ERROR,
    RECHNUNG_STATUS_EXTRACTING,
    RECHNUNG_STATUS_MAIL_SENT,
    RECHNUNG_STATUS_PREVIEWING,
    Tenant,
    TenantKnowledge,
    TelegramState,
    TenantAnfrageSchema,
    ANFRAGE_TYP_TISCHLER,
    ANFRAGE_TYP_ALLGEMEIN,
    ToolConfig,
    VIZ_STATUS_DONE,
    VIZ_STATUS_FAILED,
    VIZ_STATUS_GENERATING,
    VIZ_STATUS_PENDING,
    Visualisierung,
    TenantLeistung,
)
from core.security import decrypt, encrypt
from core.ai import (
    extract_rechnung_from_audio,
    extract_rechnung_from_text,
    analyse_kundengespraech_from_audio,
)
from core.integrations.lexware import LexwareProvider
from core.integrations.openrouteservice import (
    geocode_address as ors_geocode_address,
    is_configured as ors_is_configured,
)
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
    async def send_for_tenant(tenant_id, text, *, employee_id=None):
        """Push an einen Tenant.

        Wenn employee_id gesetzt: Push an den Telegram-Chat dieses
        Mitarbeiters (Multi-User, Phase 2). Sonst: Push an den
        Default-Employee — oder als Fallback an die Chat-ID aus
        tool_configs (Legacy, falls noch kein Backfill gelaufen ist).
        """
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

                # Chat-ID-Aufloesung: Employee > Default-Employee > Legacy-Config
                chat_id = await _resolve_chat_id_for_push(
                    session, tenant_id, employee_id, fallback=cfg.get("chat_id", ""),
                )
                if not bot_token or not chat_id:
                    return False
            return await TelegramNotifier._send_raw(bot_token, chat_id, text)
        except Exception as e:
            logger.exception(f"Telegram-Versand fehlgeschlagen: {e}")
            return False

    @staticmethod
    async def broadcast_to_tenant(tenant_id, text):
        """Push an ALLE aktiven Mitarbeiter eines Tenants.

        Fuer tenant-weite Notifications (z.B. 18:00-Bezahl-Push,
        Anfragen-Eingang, generelle Status-Meldungen). Failsafe:
        Versand pro Mitarbeiter wird einzeln versucht; ein einzelner
        Fehler stoppt nicht die anderen.
        """
        from core.models.employee import get_employees_for_tenant
        try:
            employees = await get_employees_for_tenant(tenant_id, active_only=True)
        except Exception as e:
            logger.exception(f"broadcast_to_tenant: employees-Lookup failed: {e}")
            return 0
        sent = 0
        for emp in employees:
            if not emp.telegram_chat_id:
                continue
            ok = await TelegramNotifier.send_for_tenant(
                tenant_id, text, employee_id=emp.id,
            )
            if ok:
                sent += 1
        return sent

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
    """Tenant zu Chat-ID. Drop-in fuer alle 50+ Aufrufstellen.

    Phase 2 der Multi-Mitarbeiter-Erweiterung
    (`das-machen-wir-gleich-foamy-frost.md`):
    Sucht intern erst employees.telegram_chat_id (Multi-User), faellt
    auf tenants.telegram_chat_id zurueck (Legacy + Default-Employee).
    Return-Typ unveraendert (Tenant | None) — alle bestehenden Caller
    funktionieren ohne Aenderung.
    """
    from core.models.employee import get_employee_by_telegram_chat
    res = await get_employee_by_telegram_chat(chat_id)
    return res[0] if res else None


async def _get_current_employee(chat_id):
    """(Tenant, Employee) zu Chat-ID, oder None.

    Fuer personalisierte Befehle die wissen muessen WER gerade tippt —
    z.B. /briefing (zeigt nur eigene Termine), /werkstatt (setzt
    eigene Heimat), /mitarbeiter (Inhaber-only). Default-Employee
    bei Legacy-Chats (chat_id steht nur am Tenant, nicht am Employee).
    """
    from core.models.employee import get_employee_by_telegram_chat
    return await get_employee_by_telegram_chat(chat_id)


async def _resolve_chat_id_for_push(session, tenant_id, employee_id, *, fallback):
    """Chat-ID-Aufloesung mit 3-stufigem Fallback.

    1. Wenn employee_id gesetzt: dessen telegram_chat_id (oder None
       wenn Mitarbeiter noch nicht onboarded ist)
    2. Sonst: telegram_chat_id des Default-Employee
    3. Sonst: Legacy-Wert aus tool_configs.telegram_notify (chat_id-Key)
    """
    from core.models.employee import Employee
    if employee_id is not None:
        emp = (await session.execute(
            select(Employee).where(Employee.id == employee_id)
        )).scalar_one_or_none()
        if emp and emp.telegram_chat_id:
            return str(emp.telegram_chat_id)
        # employee_id war gesetzt aber Mitarbeiter hat keine Chat-ID →
        # NICHT auf Default zurueckfallen (sonst kriegt der Inhaber
        # Notifications die einem anderen gehoert haetten).
        return None
    # Kein employee_id → Default-Employee
    default_emp = (await session.execute(
        select(Employee).where(
            Employee.tenant_id == tenant_id,
            Employee.is_default.is_(True),
        )
    )).scalar_one_or_none()
    if default_emp and default_emp.telegram_chat_id:
        return str(default_emp.telegram_chat_id)
    # Legacy-Fallback (vor Backfill / unkonfigurierte Tenants)
    return fallback or None

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
    raw = parts[1].strip().lower()
    # Format: "<tenant_slug>" (Owner-Onboarding, Default-Employee)
    # oder:   "<tenant_slug>__<employee_slug>" (Mitarbeiter-Onboarding)
    if "__" in raw:
        tenant_slug, _, employee_slug = raw.partition("__")
    else:
        tenant_slug, employee_slug = raw, "default"
    for token in (tenant_slug, employee_slug):
        if not token or not token.replace("-", "").replace("_", "").isalnum():
            return "Aktivierungs-Link ungueltig. Bitte verwenden Sie den QR-Code."

    from core.models.employee import Employee
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        )).scalar_one_or_none()
        if tenant is None:
            return f"Aktivierungs-Link ungueltig (Tenant {tenant_slug} nicht gefunden)."
        if tenant_slug == GLOBAL_TENANT_SLUG:
            return "Dieser Aktivierungs-Link ist nicht fuer Endkunden bestimmt."

        # Employee finden (oder beim Default-Slug 'default' garantiert
        # da dank Phase-0-Backfill)
        emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id,
                Employee.slug == employee_slug,
            )
        )).scalar_one_or_none()
        if emp is None:
            return (
                f"Mitarbeiter-Slug '{employee_slug}' nicht gefunden. "
                "Der Inhaber muss diesen Mitarbeiter erst anlegen "
                "(/mitarbeiter neu)."
            )

        # Falls schon eine andere Chat-ID hier haengt: warnen + ueberschreiben
        if emp.telegram_chat_id and emp.telegram_chat_id != chat_id:
            logger.warning(
                f"Mitarbeiter {tenant_slug}/{employee_slug}: "
                f"Chat-ID-Wechsel von {emp.telegram_chat_id} zu {chat_id}"
            )
        # Chat-ID auf Mitarbeiter setzen
        emp.telegram_chat_id = chat_id
        # Fuer Default-Employee zusaetzlich tenant.telegram_chat_id mitspiegeln
        # (Backward-Compat fuer Code-Pfade die noch nicht employee-aware sind)
        if emp.is_default:
            tenant.telegram_chat_id = chat_id
        await s.commit()

        first_name = (from_data.get("first_name") or "").strip() or "dort"
        reply = f"Willkommen, {first_name}!\n\n"
        if emp.is_default:
            reply += f"Ihr Telegram ist jetzt mit <b>{tenant.company_name}</b> verbunden.\n\n"
        else:
            reply += (
                f"Sie sind als Mitarbeiter <b>{emp.name}</b> "
                f"bei <b>{tenant.company_name}</b> angemeldet.\n\n"
            )
        reply += "Ab jetzt erhalten Sie hier:\n"
        reply += "- Push-Nachrichten zu Ihren Anrufen und Mails\n"
        reply += "- Bestaetigungen ueber gebuchte Termine\n"
        reply += "- Hinweise wenn Q nicht weiterkommt\n\n"
        reply += "Mit /help sehen Sie alle verfuegbaren Befehle."
        return reply

async def _handle_help_command():
    msg = "<b>📋 Verfuegbare Befehle</b>\n\n"

    msg += "<b>📷 BELEGE</b>\n"
    msg += "/beleg - Foto/PDF an Lexware schicken\n"
    msg += "/belege_anzeigen - letzte 10 hochgeladene\n\n"

    msg += "<b>💰 RECHNUNGEN</b>\n"
    msg += "/rechnung - neue anlegen (Text oder Sprache)\n"
    msg += "/rechnungen_anzeigen - letzte 10\n\n"

    msg += "<b>📞 KUNDENGESPRAECHE</b>\n"
    msg += "/aufnahme - Gespraech aufnehmen + Lexware-Angebot\n"
    msg += "/briefing - naechster Termin mit Briefing\n"
    msg += "/anrufe - letzte 10 Gespraeche\n"
    msg += "/kunde [Name] - alle Gespraeche zu einem Kunden\n\n"

    msg += "<b>📚 WISSENSBASIS</b>\n"
    msg += "/wissen - neuen Eintrag anlegen\n"
    msg += "/wissen_anzeigen - alle ansehen\n"
    msg += "/wissen_loeschen - Eintrag entfernen\n\n"

    msg += "<b>📋 ANFRAGE-FORMULAR</b>\n"
    msg += "/formular - Felder bearbeiten (Wizard)\n"
    msg += "/formular_anzeigen - aktuelle Felder ansehen\n"
    msg += "/formular_zuruecksetzen - auf Standard zuruecksetzen\n\n"

    msg += "<b>🎨 VISUALISIERUNG</b>\n"
    msg += "/visualisierung - Foto + KI-Rendering\n\n"

    msg += "<b>⚙️ SETUP</b>\n"
    msg += "/lexware_setup - Lexware verbinden\n"
    msg += "/lexware_status - Verbindung pruefen\n"
    msg += "/werkstatt - Werkstatt-Adresse einrichten (fuer Smart-Termine)\n"
    msg += "/werkstatt_status - aktuelle Heimat-Adresse anzeigen\n"
    msg += "/mitarbeiter - Mitarbeiter-Liste anzeigen\n"
    msg += "/mitarbeiter neu - Mitarbeiter anlegen (nur Inhaber)\n"
    msg += "/start - Bot mit Betrieb verbinden\n"
    msg += "/status - Agent-Status pruefen\n"
    msg += "/abbrechen - laufende Aktion abbrechen\n"
    msg += "/help - Diese Liste\n\n"

    msg += "<i>Bei Fragen: hallo@gewerbeagent.de</i>"
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
        elif cq_data and cq_data.startswith("aufnahme:"):
            await _handle_aufnahme_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("leistung:"):
            await _handle_leistung_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("formular:"):
            await _handle_formular_callback(cq_chat_id, cq_data, cq_id, bot_token)
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
        # ----- Aufnahme-Wizard: Voice-Note empfangen -----
        if state and state.state_key == STATE_AUFNAHME_WAITING_AUDIO:
            bot_token = await _load_global_bot_token()
            if not bot_token:
                await _send_to_chat(
                    chat_id,
                    "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                )
                return {"ok": True}
            reply = await _handle_aufnahme_audio_received(
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
    elif text == "/microsoft_setup":
        reply = await _handle_microsoft_setup_command(chat_id)
    elif text == "/microsoft_status":
        reply = await _handle_microsoft_status_command(chat_id)
    elif text == "/microsoft_check":
        reply = await _handle_microsoft_check_command(chat_id)
    elif text == "/microsoft_test":
        reply = await _handle_microsoft_test_command(chat_id)
    elif text == "/aufnahme":
        reply = await _handle_aufnahme_command(chat_id)
    elif text == "/formular":
        reply = await _handle_formular_command(chat_id)
    elif text == "/formular_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_formular_anzeigen_command(chat_id)
    elif text == "/formular_zuruecksetzen":
        reply = await _handle_formular_zuruecksetzen_command(chat_id)
    elif text == "/leistungen":
        await _clear_state(chat_id)
        reply = await _handle_leistungen_command(chat_id)
    elif text == "/leistung" or text == "/leistung neu" or text == "/leistung_neu":
        # Ohne Argumente -> Wizard starten
        reply = await _handle_leistung_neu_command(chat_id)
    elif text.startswith("/leistung_loeschen"):
        await _clear_state(chat_id)
        args = text[len("/leistung_loeschen"):].strip()
        reply = await _handle_leistung_loeschen_command(chat_id, args)
    elif text.startswith("/leistung"):
        # Mit Argumenten -> Detail anzeigen
        await _clear_state(chat_id)
        args = text[len("/leistung"):].strip()
        reply = await _handle_leistung_show_command(chat_id, args)
    elif text == "/briefing":
        await _clear_state(chat_id)
        reply = await _handle_briefing_command(chat_id)
    elif text == "/anrufe":
        await _clear_state(chat_id)
        reply = await _handle_anrufe_command(chat_id)
    elif text.startswith("/kunde"):
        await _clear_state(chat_id)
        # Argumente nach '/kunde' extrahieren
        args = text[len("/kunde"):].strip()
        reply = await _handle_kunde_command(chat_id, args)
    elif text == "/rechnungen_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_rechnungen_anzeigen_command(chat_id)
    elif text == "/werkstatt":
        reply = await _handle_werkstatt_command(chat_id)
    elif text == "/werkstatt_status":
        reply = await _handle_werkstatt_status_command(chat_id)
    elif text == "/mitarbeiter" or text.startswith("/mitarbeiter "):
        await _clear_state(chat_id)
        reply = await _handle_mitarbeiter_command(chat_id, text)
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
        elif state.state_key == STATE_AUFNAHME_WAITING_AUDIO:
            reply = "Bitte sende eine Sprachnachricht (kein Text). Oder /abbrechen."
        elif state.state_key == STATE_AUFNAHME_PREVIEWING:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_LEISTUNG_WAITING_NAME:
            reply = await _handle_leistung_name_input(chat_id, text)
        elif state.state_key == STATE_LEISTUNG_WAITING_PREIS:
            reply = await _handle_leistung_preis_input(chat_id, text)
        elif state.state_key == STATE_LEISTUNG_WAITING_BESCHREIBUNG:
            reply_or_none = await _handle_leistung_beschreibung_input(chat_id, text)
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_LEISTUNG_PREVIEWING:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_WERKSTATT_WAITING_ADDRESS:
            reply = await _handle_werkstatt_address_input(chat_id, text)
        elif state.state_key == STATE_WERKSTATT_CONFIRMING:
            reply = await _handle_werkstatt_confirm_input(
                chat_id, text, state.state_data,
            )
        elif state.state_key == STATE_MITARBEITER_NEU_NAME:
            reply = await _handle_mitarbeiter_neu_name_input(chat_id, text)
        elif state.state_key == STATE_MITARBEITER_NEU_SKILLS:
            reply = await _handle_mitarbeiter_neu_skills_input(
                chat_id, text, state.state_data,
            )
        elif state.state_key == STATE_RECHNUNG_AWAITING_MAIL:
            stage = (state.state_data or {}).get("stage")
            if stage == "awaiting_mail_address":
                reply_or_none = await _handle_rechnung_mail_address_input(
                    chat_id, text, state.state_data
                )
                if reply_or_none is None:
                    return {"ok": True}
                reply = reply_or_none
            else:
                reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_FORMULAR_TYP_WAEHLEN:
            reply = await _handle_formular_typ_input(chat_id, text)
        elif state.state_key == STATE_FORMULAR_HAUPTMENU:
            reply_or_none = await _handle_formular_hauptmenu_input(chat_id, text, state.state_data)
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_FORMULAR_NEU_NAME:
            reply = await _handle_formular_neu_name_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_NEU_LABEL:
            reply = await _handle_formular_neu_label_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_NEU_TYP:
            reply = await _handle_formular_neu_typ_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_NEU_OPTIONEN:
            reply = await _handle_formular_neu_optionen_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_NEU_REQUIRED:
            reply = await _handle_formular_neu_required_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_LOESCHEN:
            reply = await _handle_formular_loeschen_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_FORMULAR_RESET_CONFIRM:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
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

# Aufnahme-Konstanten - lange Kundengespraeche
AUFNAHME_MAX_AUDIO_BYTES = 50_000_000  # 50 MB (~30 Min Voice-Note)

RECHNUNG_VOICE_MAX_SECONDS = 120     # 2 Minuten max


def _format_rechnung_preview(extracted: dict, confidence_warning: str = "") -> str:
    """Baut die Vorschau-Nachricht aus extrahierten Daten (mit Positionen-Liste)."""
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

    positionen = extracted.get("positionen") or []
    gesamt = extracted.get("gesamtbetrag_brutto_eur")

    msg = "<b>Rechnung-Vorschau</b>\n\n"
    msg += f"• <b>Kunde:</b>     {kn}\n"
    msg += f"• <b>Anschrift:</b> {addr_str}\n"
    msg += "\n<b>Positionen:</b>\n"

    if not positionen:
        msg += "<i>(keine erkannt)</i>\n"
    else:
        for i, p in enumerate(positionen, 1):
            menge = p.get("menge") or 1
            einheit = p.get("einheit") or "Stueck"
            preis = p.get("preis_brutto_eur") or 0
            name = p.get("name") or "?"
            beschreibung = p.get("beschreibung")
            position_total = float(menge) * float(preis)
            if abs(float(menge) - 1.0) < 0.01 and einheit == "Stueck":
                msg += f"  <b>{i}.</b> {name}: <b>{position_total:.2f} €</b>\n"
            else:
                msg += f"  <b>{i}.</b> {name}: {menge} {einheit} \u00d7 {float(preis):.2f} € = <b>{position_total:.2f} €</b>\n"
            if beschreibung:
                msg += f"     <i>{beschreibung}</i>\n"

    if gesamt is not None:
        msg += f"\n<b>Gesamt brutto: {float(gesamt):.2f} €</b>\n"

    missing = extracted.get("missing_fields") or []
    if missing:
        msg += f"\n⚠️ Unklar: {', '.join(missing)}\n"
    if confidence_warning:
        msg += f"\n⚠️ {confidence_warning}\n"

    msg += (
        "\n<i>Hinweis: Anschrift in Lexware ggf. vervollstaendigen, "
        "bevor du finalisierst.</i>"
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
    # Erste Position als 'leistung_titel/betrag' fuer Rueckwaertskompatibilitaet
    positionen_list = extracted.get("positionen") or []
    first_pos = positionen_list[0] if positionen_list else {}
    gesamt = extracted.get("gesamtbetrag_brutto_eur")

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
            leistung_titel=first_pos.get("name"),
            leistung_beschreibung=first_pos.get("beschreibung"),
            betrag_brutto_eur=gesamt,
            status=RECHNUNG_STATUS_PREVIEWING,
        )
        s.add(rg)
        await s.commit()
        await s.refresh(rg)
        rechnung_id = rg.id

        # Positionen in rechnung_positionen speichern
        for i, p in enumerate(positionen_list, start=1):
            pos = RechnungPosition(
                rechnung_id=rechnung_id,
                position_nr=i,
                name=p.get("name") or "Position",
                beschreibung=p.get("beschreibung"),
                menge=p.get("menge") or 1,
                einheit=p.get("einheit") or "Stueck",
                preis_brutto_eur=p.get("preis_brutto_eur") or 0,
                mwst_prozent=p.get("mwst_prozent") or 19,
            )
            s.add(pos)
        await s.commit()
        logger.info(
            f"Rechnung {rechnung_id} angelegt mit {len(positionen_list)} Positionen, "
            f"Gesamt {gesamt}"
        )

    # Vorschau-Nachricht
    confidence = extracted.get("extraction_confidence", "low")
    confidence_warning = ""
    if confidence == "low":
        confidence_warning = "Niedrige Erkennungs-Konfidenz - bitte sorgfaeltig pruefen."
    elif confidence == "medium":
        confidence_warning = "Mittlere Erkennungs-Konfidenz - bitte sorgfaeltig pruefen."

    # Pflichtfelder pruefen - bei wesentlich Fehlendem, kein Best.-Button
    positionen_check = extracted.get("positionen") or []
    has_minimum = bool(
        extracted.get("kunde_name")
        and len(positionen_check) >= 1
        and (extracted.get("gesamtbetrag_brutto_eur") is not None)
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
            [{"text": "✅ In Lexware anlegen", "callback_data": f"rg:confirm:{rechnung_id}"}],
            [
                {"text": "\u270f️ Neu eingeben", "callback_data": f"rg:retry:{rechnung_id}"},
                {"text": "❌ Abbrechen", "callback_data": f"rg:cancel:{rechnung_id}"},
            ],
        ]
    else:
        # Keine Confirm-Option weil Pflichtfelder fehlen
        buttons = [
            [{"text": "\u270f️ Neu eingeben", "callback_data": f"rg:retry:{rechnung_id}"}],
            [{"text": "❌ Abbrechen", "callback_data": f"rg:cancel:{rechnung_id}"}],
        ]

    if bot_token is None:
        bot_token = await _load_global_bot_token()
    await _send_with_inline_buttons(chat_id, preview_text, buttons, bot_token=bot_token)
    return None  # Schon mit Buttons gesendet




# ==============================================================
# /aufnahme - Kundengespraech-Aufnahme-Wizard
# ==============================================================

# =====================================================================
# Briefing-Befehle: /briefing, /kunde X, /anrufe
# =====================================================================

def _format_kundengespraech_short(g) -> str:
    """Eine Zeile: Datum Kunde Status."""
    ts = g.gespraech_datum.strftime("%d.%m %H:%M") if g.gespraech_datum else "-"
    status_emoji = {
        "erfasst": "📋",
        "mit_angebot": "💰",
        "archiviert": "📦",
    }.get(g.status, "•")
    return f"{status_emoji} {ts} <b>{g.kunde_name}</b>"


def _format_kundengespraech_full(g) -> str:
    """Vollstaendige Anzeige eines Kundengespraechs (fuer /briefing + /kunde)."""
    msg = f"<b>📋 {g.kunde_name}</b>\n"
    if g.gespraech_datum:
        msg += f"<i>Gespraech am {g.gespraech_datum.strftime('%d.%m.%Y %H:%M')}</i>\n"

    if g.termin_datum:
        msg += f"\n<b>📅 Termin:</b> {g.termin_datum.strftime('%d.%m.%Y %H:%M')}"
        if g.termin_ort:
            msg += f" @ {g.termin_ort}"
        msg += "\n"

    if g.briefing_kurz:
        msg += f"\n<b>📝 Briefing:</b>\n<i>{g.briefing_kurz}</i>\n"

    if g.todos:
        msg += "\n<b>✅ TODOs:</b>\n"
        for todo in g.todos[:8]:
            msg += f"  • {todo}\n"

    if g.notizen_lang:
        # Volle Notizen nur wenn nicht zu lang
        notizen = g.notizen_lang
        if len(notizen) > 600:
            notizen = notizen[:580] + "..."
        msg += f"\n<b>📓 Notizen:</b>\n<i>{notizen}</i>\n"

    if g.status == "mit_angebot":
        msg += "\n💰 <i>Angebot wurde erstellt</i>"

    return msg


async def _handle_briefing_command(chat_id):
    """Zeigt naechsten anstehenden Termin mit Briefing.

    Faellt zurueck auf juengstes Gespraech wenn kein zukuenftiger Termin.

    Phase-4-Multi-Mitarbeiter: Default-Employee (Inhaber) sieht alle
    Termine; Nicht-Default-Mitarbeiter sieht nur seine zugewiesenen
    (assigned_employee_id == eigene id).
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, and_, or_

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as s:
        # Erst: zukuenftiger Termin
        stmt = (
            select(Kundengespraech)
            .where(
                Kundengespraech.tenant_id == tenant.id,
                Kundengespraech.termin_datum.is_not(None),
                Kundengespraech.termin_datum >= now,
            )
            .order_by(Kundengespraech.termin_datum.asc())
            .limit(1)
        )
        if not current_emp.is_default:
            stmt = stmt.where(
                Kundengespraech.assigned_employee_id == current_emp.id
            )
        future = (await s.execute(stmt)).scalar_one_or_none()

        if future:
            msg = "<b>🔔 Naechster Termin</b>\n\n"
            msg += _format_kundengespraech_full(future)
            return msg

        # Kein zukuenftiger Termin -> juengstes Gespraech zeigen
        stmt2 = (
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant.id)
            .order_by(Kundengespraech.gespraech_datum.desc())
            .limit(1)
        )
        if not current_emp.is_default:
            stmt2 = stmt2.where(
                Kundengespraech.assigned_employee_id == current_emp.id
            )
        latest = (await s.execute(stmt2)).scalar_one_or_none()

        if not latest:
            scope = "" if current_emp.is_default else " (auf dich zugewiesen)"
            return (
                f"Noch kein Kundengespraech{scope} erfasst.\n\n"
                "Mit /aufnahme das erste anlegen."
            )

        msg = "<b>📋 Letztes Gespraech</b>\n"
        msg += "<i>(Kein anstehender Termin)</i>\n\n"
        msg += _format_kundengespraech_full(latest)
        return msg


async def _handle_kunde_command(chat_id, args):
    """/kunde [Name] - alle Gespraeche zu einem Kunden.

    args = String nach '/kunde ' (z.B. 'Mueller' oder 'Frau Mueller')
    """
    from sqlalchemy import select, func

    if not args or len(args.strip()) < 2:
        return (
            "Bitte einen Kunden-Namen angeben.\n\n"
            "Beispiel: <code>/kunde Mueller</code>"
        )

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    suchbegriff = args.strip().lower()

    async with AsyncSessionLocal() as s:
        stmt = (
            select(Kundengespraech)
            .where(
                Kundengespraech.tenant_id == tenant.id,
                func.lower(Kundengespraech.kunde_name).contains(suchbegriff),
            )
            .order_by(Kundengespraech.gespraech_datum.desc())
            .limit(10)
        )
        if not current_emp.is_default:
            stmt = stmt.where(
                Kundengespraech.assigned_employee_id == current_emp.id
            )
        gespraeche = (await s.execute(stmt)).scalars().all()

    if not gespraeche:
        return f"Keine Gespraeche zu <i>{suchbegriff}</i> gefunden."

    if len(gespraeche) == 1:
        # Nur eins -> volle Anzeige
        return _format_kundengespraech_full(gespraeche[0])

    # Mehrere -> Liste mit Briefings
    msg = f"<b>📋 Gespraeche zu '{suchbegriff}'</b> ({len(gespraeche)})\n\n"
    for i, g in enumerate(gespraeche[:5], 1):
        msg += f"<b>{i}. {_format_kundengespraech_short(g)}</b>\n"
        if g.briefing_kurz:
            briefing = g.briefing_kurz
            if len(briefing) > 200:
                briefing = briefing[:180] + "..."
            msg += f"<i>{briefing}</i>\n\n"
    if len(gespraeche) > 5:
        msg += f"<i>... und {len(gespraeche) - 5} weitere</i>\n"
    return msg


async def _handle_anrufe_command(chat_id):
    """Zeigt die letzten 10 Kundengespraeche.

    Phase-4-Filter: Default-Employee sieht alle, andere nur eigene
    (assigned_employee_id == current_emp.id).
    """
    from sqlalchemy import select

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    async with AsyncSessionLocal() as s:
        stmt = (
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == tenant.id)
            .order_by(Kundengespraech.gespraech_datum.desc())
            .limit(10)
        )
        if not current_emp.is_default:
            stmt = stmt.where(
                Kundengespraech.assigned_employee_id == current_emp.id
            )
        gespraeche = (await s.execute(stmt)).scalars().all()

    if not gespraeche:
        scope = "" if current_emp.is_default else " (auf dich zugewiesen)"
        return (
            f"Noch kein Kundengespraech{scope} erfasst.\n\n"
            "Mit /aufnahme das erste anlegen."
        )

    title_scope = "" if current_emp.is_default else " (deine)"
    msg = f"<b>📞 Letzte {len(gespraeche)} Gespraeche{title_scope}</b>\n\n"
    for i, g in enumerate(gespraeche, 1):
        msg += f"{_format_kundengespraech_short(g)}"
        if g.termin_datum:
            msg += f" → 📅 {g.termin_datum.strftime('%d.%m %H:%M')}"
        msg += "\n"

    msg += "\nDetails mit <code>/kunde [Name]</code>"
    return msg


# =====================================================================
# Wissensbasis: tenant_leistungen pflegen
# /leistungen, /leistung neu, /leistung [Name], /leistung_loeschen [Name]
# =====================================================================

# Erlaubte Einheiten (Voice-freundlich)
ERLAUBTE_EINHEITEN = (
    "Stueck", "Stunde", "Tag", "Pauschal",
    "Meter", "lfm", "qm", "kg", "Liter", "Set",
)


def _einheit_normalisieren(text: str) -> str | None:
    """Erkennt Einheit aus Tenant-Eingabe.

    'pro stunde' -> 'Stunde', 'pauschal' -> 'Pauschal', '/lfm' -> 'lfm'
    Gibt None zurueck wenn unklar.
    """
    if not text:
        return None
    t = text.lower().strip().lstrip("/")
    mapping = {
        "stunde": "Stunde", "stunden": "Stunde", "std": "Stunde", "h": "Stunde",
        "stueck": "Stueck", "stk": "Stueck", "stck": "Stueck", "stück": "Stueck",
        "tag": "Tag", "tage": "Tag",
        "pauschal": "Pauschal", "pausch": "Pauschal", "psch": "Pauschal",
        "meter": "Meter", "m": "Meter",
        "lfm": "lfm", "laufmeter": "lfm", "laufender meter": "lfm",
        "qm": "qm", "quadratmeter": "qm", "m2": "qm", "m²": "qm",
        "kg": "kg", "kilo": "kg",
        "liter": "Liter", "l": "Liter",
        "set": "Set",
    }
    for key, val in mapping.items():
        if t == key or t.endswith(" " + key) or t.startswith(key + " "):
            return val
    return None


def _parse_preis_eingabe(text: str) -> dict | None:
    """Parst Tenant-Eingabe wie '75 Euro pro Stunde' oder '50 pauschal'.

    Returns: {'preis_eur': float, 'einheit': str} oder None bei Fehler.
    """
    import re
    if not text:
        return None
    t = text.lower().strip()

    # Preis: erste Zahl finden (mit Komma oder Punkt)
    preis_match = re.search(r"(\d+[,.]?\d*)", t)
    if not preis_match:
        return None
    preis_str = preis_match.group(1).replace(",", ".")
    try:
        preis = float(preis_str)
    except ValueError:
        return None
    if preis <= 0:
        return None

    # Einheit: nach dem Preis suchen
    rest = t[preis_match.end():].strip()
    # Schluesselworte ignorieren
    for ignore in ("euro", "eur", "€", "pro", "/", "je", "brutto", "netto"):
        rest = rest.replace(ignore, " ")
    rest = " ".join(rest.split())  # Whitespace normalisieren
    einheit = _einheit_normalisieren(rest) if rest else None
    if not einheit:
        return None

    return {"preis_eur": preis, "einheit": einheit}


def _format_leistung_short(l) -> str:
    """Eine Zeile fuer Liste: 'Moebelmontage - 75,00€/Stunde'."""
    preis = f"{float(l.preis_eur):.2f}€".replace(".", ",")
    return f"<b>{l.name}</b> — {preis}/{l.einheit}"


def _format_leistung_full(l) -> str:
    """Vollanzeige einer Leistung."""
    preis = f"{float(l.preis_eur):.2f}€".replace(".", ",")
    msg = f"<b>📐 {l.name}</b>\n"
    msg += f"<i>{preis} pro {l.einheit} (brutto, {l.mwst_prozent}% MwSt)</i>\n"
    if l.standard_beschreibung:
        msg += f"\n{l.standard_beschreibung}\n"
    if not l.aktiv:
        msg += "\n⚠️ <i>Inaktiv (geloescht)</i>"
    return msg


# ----- Befehl: /leistungen -----

async def _handle_leistungen_command(chat_id):
    """Liste der aktiven Leistungen."""
    from sqlalchemy import select

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    async with AsyncSessionLocal() as s:
        leistungen = (await s.execute(
            select(TenantLeistung)
            .where(
                TenantLeistung.tenant_id == tenant.id,
                TenantLeistung.aktiv == True,  # noqa: E712
            )
            .order_by(TenantLeistung.sortierung.asc(), TenantLeistung.name.asc())
        )).scalars().all()

    if not leistungen:
        msg = "<b>📚 Wissensbasis</b>\n\n"
        msg += "Du hast noch keine Leistungen hinterlegt.\n\n"
        msg += "<i>Das ist OK</i> — du kannst /aufnahme weiter nutzen, "
        msg += "Q schaut sich die Preise dann aus dem Gespraech ab.\n\n"
        msg += "<b>Wenn du oft die gleichen Leistungen anbietest</b>, hilft es Q "
        msg += "wenn du sie hier hinterlegst.\n\n"
        msg += "<b>Beispiele aus verschiedenen Gewerken:</b>\n"
        msg += "• Tischler: <i>Moebelmontage 75€/Stunde</i>\n"
        msg += "• Sanitaer: <i>Heizungswartung 120€ pauschal</i>\n"
        msg += "• Elektriker: <i>Steckdose montieren 35€/Stueck</i>\n\n"
        msg += "<b>Tipp:</b> Pflege deine 3-5 wichtigsten Leistungen ein. "
        msg += "Mehr ist nicht noetig.\n\n"
        msg += "Anlegen mit: /leistung neu"
        return msg

    msg = f"<b>📚 Deine Leistungen ({len(leistungen)})</b>\n\n"
    for i, l in enumerate(leistungen, 1):
        msg += f"{i}. {_format_leistung_short(l)}\n"
    msg += "\n"
    msg += "Neue anlegen: /leistung neu\n"
    msg += "Details: /leistung [Name]\n"
    msg += "Loeschen: /leistung_loeschen [Name]"
    return msg


# ----- Befehl: /leistung neu -----

async def _handle_leistung_neu_command(chat_id):
    """Startet den Anlege-Wizard."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    await _save_state(chat_id, STATE_LEISTUNG_WAITING_NAME, {})
    msg = "<b>➕ Neue Leistung anlegen</b>\n\n"
    msg += "Wie heisst die Leistung?\n\n"
    msg += "<b>Beispiele:</b>\n"
    msg += "• <i>Moebelmontage</i>\n"
    msg += "• <i>Heizungswartung</i>\n"
    msg += "• <i>Anfahrt Trier</i>\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_leistung_name_input(chat_id, text: str):
    """Schritt 1: Name erhalten."""
    name = (text or "").strip()
    if not name or len(name) < 2:
        return "Name ist zu kurz. Bitte Name eingeben (mind. 2 Zeichen) oder /abbrechen."
    if len(name) > 200:
        return "Name ist zu lang (max 200 Zeichen). Bitte kuerzer."

    await _save_state(chat_id, STATE_LEISTUNG_WAITING_PREIS, {"name": name})
    msg = f"<b>{name}</b> — was kostet das?\n\n"
    msg += "<b>Format:</b> <i>Preis Einheit</i>\n\n"
    msg += "<b>Beispiele:</b>\n"
    msg += "• <i>75 Euro pro Stunde</i>\n"
    msg += "• <i>150 pro lfm</i>\n"
    msg += "• <i>50 pauschal</i>\n"
    msg += "• <i>35 pro Stueck</i>\n\n"
    msg += "Erlaubte Einheiten: Stunde, Stueck, Tag, Pauschal, Meter, lfm, qm, kg, Liter, Set"
    return msg


async def _handle_leistung_preis_input(chat_id, text: str):
    """Schritt 2: Preis + Einheit erhalten."""
    state = await _load_state(chat_id)
    if not state or not state.state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /leistung neu erneut starten."

    name = (state.state_data or {}).get("name") or ""
    parsed = _parse_preis_eingabe(text)
    if not parsed:
        return (
            "Konnte den Preis nicht erkennen.\n\n"
            "Bitte Format: <i>75 Euro pro Stunde</i> oder <i>50 pauschal</i>\n\n"
            "Erlaubte Einheiten: Stunde, Stueck, Tag, Pauschal, Meter, lfm, qm, kg, Liter, Set\n\n"
            "Oder /abbrechen."
        )

    state.state_data["preis_eur"] = parsed["preis_eur"]
    state.state_data["einheit"] = parsed["einheit"]
    await _save_state(chat_id, STATE_LEISTUNG_WAITING_BESCHREIBUNG, state.state_data)

    preis_str = f"{parsed['preis_eur']:.2f}€".replace(".", ",")
    msg = f"<b>{name}</b> — {preis_str}/{parsed['einheit']}\n\n"
    msg += "Eine Standardbeschreibung fuer Angebote? "
    msg += "Wird in Lexware-Angeboten als Detailtext genutzt.\n\n"
    msg += "<b>Beispiele:</b>\n"
    msg += "• <i>Fachgerechte Moebelmontage inkl. Aufbau und Justierung</i>\n"
    msg += "• <i>Wartung der Heizungsanlage gem. Hersteller-Vorgabe</i>\n\n"
    msg += "Oder /skip um leer zu lassen."
    return msg


async def _handle_leistung_beschreibung_input(chat_id, text: str):
    """Schritt 3: Beschreibung (oder /skip)."""
    state = await _load_state(chat_id)
    if not state or not state.state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /leistung neu erneut starten."

    beschreibung = None
    t = (text or "").strip()
    if t.lower() not in ("/skip", "skip", "-", "nein", ""):
        if len(t) > 1000:
            return "Beschreibung zu lang (max 1000 Zeichen). Bitte kuerzer oder /skip."
        beschreibung = t

    data = state.state_data
    data["standard_beschreibung"] = beschreibung
    await _save_state(chat_id, STATE_LEISTUNG_PREVIEWING, data)

    # Vorschau mit Buttons
    name = data["name"]
    preis = data["preis_eur"]
    einheit = data["einheit"]
    preis_str = f"{preis:.2f}€".replace(".", ",")

    msg = "<b>🔍 Vorschau</b>\n\n"
    msg += f"<b>{name}</b>\n"
    msg += f"<i>{preis_str} pro {einheit} (19% MwSt)</i>\n"
    if beschreibung:
        msg += f"\n{beschreibung}\n"

    keyboard = [[
        {"text": "✅ Anlegen", "callback_data": "leistung:save"},
        {"text": "❌ Verwerfen", "callback_data": "leistung:cancel"},
    ]]
    await _send_with_inline_buttons(chat_id, msg, keyboard)
    return None


async def _handle_leistung_callback(chat_id, callback_data, callback_query_id, bot_token):
    """Save/Cancel beim Anlegen."""
    parts = callback_data.split(":")
    if len(parts) < 2:
        await _answer_callback_query(callback_query_id, "Ungueltig", bot_token)
        return
    action = parts[1]

    if action == "cancel":
        await _clear_state(chat_id)
        await _answer_callback_query(callback_query_id, "Verworfen", bot_token)
        await _send_to_chat(chat_id, "🗑 Anlage abgebrochen.")
        return

    if action != "save":
        await _answer_callback_query(callback_query_id, "Unbekannt", bot_token)
        return

    state = await _load_state(chat_id)
    if not state or not state.state_data:
        await _answer_callback_query(callback_query_id, "Session abgelaufen", bot_token)
        await _clear_state(chat_id)
        return

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _answer_callback_query(callback_query_id, "Tenant fehlt", bot_token)
        await _clear_state(chat_id)
        return

    data = state.state_data
    from decimal import Decimal as _Dec

    async with AsyncSessionLocal() as s:
        leistung = TenantLeistung(
            tenant_id=tenant.id,
            name=data["name"][:200],
            preis_eur=_Dec(str(data["preis_eur"])),
            einheit=data["einheit"][:50],
            mwst_prozent=19,
            standard_beschreibung=data.get("standard_beschreibung"),
            aktiv=True,
        )
        s.add(leistung)
        await s.commit()
        leistung_id = leistung.id

    logger.info(
        "TenantLeistung angelegt: id=%s tenant=%s name=%r preis=%s/%s",
        leistung_id, tenant.id, data["name"], data["preis_eur"], data["einheit"],
    )

    await _clear_state(chat_id)
    await _answer_callback_query(callback_query_id, "Angelegt!", bot_token)
    await _send_to_chat(
        chat_id,
        f"✅ <b>{data['name']}</b> wurde angelegt.\n\n"
        "Q wird jetzt diese Leistung kennen wenn du sie im Gespraech erwaehnst.\n\n"
        "Weitere anlegen: /leistung neu\nUebersicht: /leistungen"
    )


# ----- Befehl: /leistung [Name] (Detail) -----

async def _handle_leistung_show_command(chat_id, args: str):
    """Detail einer Leistung anzeigen via Name-Match."""
    from sqlalchemy import select, func

    if not args or len(args.strip()) < 2:
        return (
            "Bitte einen Leistungs-Namen angeben.\n\n"
            "Beispiel: <code>/leistung Moebelmontage</code>\n\n"
            "Liste: /leistungen"
        )

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    suchbegriff = args.strip().lower()

    async with AsyncSessionLocal() as s:
        leistungen = (await s.execute(
            select(TenantLeistung)
            .where(
                TenantLeistung.tenant_id == tenant.id,
                func.lower(TenantLeistung.name).contains(suchbegriff),
            )
            .order_by(TenantLeistung.aktiv.desc(), TenantLeistung.name.asc())
            .limit(5)
        )).scalars().all()

    if not leistungen:
        return f"Keine Leistung gefunden zu <i>{suchbegriff}</i>.\n\nListe: /leistungen"

    if len(leistungen) == 1:
        return _format_leistung_full(leistungen[0])

    msg = f"<b>{len(leistungen)} Treffer</b> fuer <i>{suchbegriff}</i>:\n\n"
    for i, l in enumerate(leistungen, 1):
        msg += f"{i}. {_format_leistung_short(l)}"
        if not l.aktiv:
            msg += " <i>(inaktiv)</i>"
        msg += "\n"
    msg += "\nGenauer eingrenzen: <code>/leistung [genauer Name]</code>"
    return msg


# ----- Befehl: /leistung_loeschen [Name] -----

async def _handle_leistung_loeschen_command(chat_id, args: str):
    """Soft-delete: aktiv = FALSE."""
    from sqlalchemy import select, func

    if not args or len(args.strip()) < 2:
        return (
            "Bitte den Leistungs-Namen angeben.\n\n"
            "Beispiel: <code>/leistung_loeschen Moebelmontage</code>"
        )

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    suchbegriff = args.strip().lower()

    async with AsyncSessionLocal() as s:
        # Nur AKTIVE Leistungen finden
        leistungen = (await s.execute(
            select(TenantLeistung)
            .where(
                TenantLeistung.tenant_id == tenant.id,
                TenantLeistung.aktiv == True,  # noqa: E712
                func.lower(TenantLeistung.name).contains(suchbegriff),
            )
            .limit(5)
        )).scalars().all()

        if not leistungen:
            return f"Keine aktive Leistung gefunden zu <i>{suchbegriff}</i>."

        if len(leistungen) > 1:
            namen = ", ".join(l.name for l in leistungen)
            return (
                f"Mehrere Treffer gefunden ({len(leistungen)}): {namen}\n\n"
                "Bitte genauer eingrenzen, z.B. <code>/leistung_loeschen [genauer Name]</code>"
            )

        # Genau 1 -> deaktivieren
        l = leistungen[0]
        l.aktiv = False
        await s.commit()
        name = l.name

    logger.info("TenantLeistung deaktiviert: tenant=%s name=%r", tenant.id, name)
    return f"✅ <b>{name}</b> wurde deaktiviert.\n\nListe: /leistungen"


# =====================================================================
# Microsoft 365 Mail-Integration
# /microsoft_setup, /microsoft_status, /microsoft_test
# =====================================================================

async def _handle_microsoft_setup_command(chat_id):
    """Generiert OAuth-URL und schickt sie als klickbaren Link."""
    from config.settings import settings

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    public_url = settings.public_url.rstrip("/")
    setup_url = f"{public_url}/oauth/start?tenant={tenant.slug}&provider=microsoft"

    msg = "<b>Microsoft 365 Mail-Anbindung</b>\n\n"
    msg += "Klick den Link um deinen Microsoft-Account zu verbinden:\n\n"
    msg += f'<a href="{setup_url}">Microsoft-Account verbinden</a>\n\n'
    msg += "<i>Du wirst zu Microsoft weitergeleitet. "
    msg += "Logg dich mit der gewuenschten Mail-Adresse ein und "
    msg += "bestaetige dass Gewerbeagent <b>Mails in deinem Namen senden</b> darf.</i>\n\n"
    msg += "<b>Was Gewerbeagent darf:</b>\n"
    msg += "  Mails in deinem Namen senden\n"
    msg += "  Profil-Info lesen\n\n"
    msg += "<b>Was Gewerbeagent NICHT darf:</b>\n"
    msg += "  Deine Mails lesen\n"
    msg += "  Mails verschieben oder loeschen\n\n"
    msg += "Status pruefen mit /microsoft_status\n"
    msg += "Test-Mail mit /microsoft_test"
    return msg


async def _handle_microsoft_status_command(chat_id):
    """Zeigt Microsoft-Verbindungsstatus."""
    from core.integrations.microsoft import get_microsoft_status

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    status = await get_microsoft_status(tenant.id)
    if not status["connected"]:
        return (
            "<b>Microsoft 365: nicht verbunden</b>\n\n"
            "Anbindung starten mit /microsoft_setup"
        )

    msg = "<b>Microsoft 365: verbunden</b>\n\n"
    msg += f"Account: <b>{status['account_email']}</b>\n"
    if status.get("expires_at"):
        try:
            expires_str = status["expires_at"].strftime("%d.%m.%Y %H:%M")
            msg += f"Token gueltig bis: {expires_str}\n"
        except Exception:
            pass
    msg += f"Berechtigungen: {status.get('scopes', '?')}\n\n"
    msg += "Test-Mail senden mit /microsoft_test\n"
    msg += "Neu verbinden (anderer Account) mit /microsoft_setup"
    return msg


async def _handle_microsoft_check_command(chat_id):
    """Polled die Outlook-Inbox des Tenants und klassifiziert ungelesene Mails."""
    from core.integrations.microsoft_inbox import poll_microsoft_inbox

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    result = await poll_microsoft_inbox(tenant.id)

    if result.get("error"):
        return f"Fehler beim Abruf: {result['error']}"

    n = result.get("checked", 0)
    if n == 0:
        return "Keine ungelesenen Mails in deiner Outlook-Inbox."

    classified = result.get("classified", {})
    msgs = result.get("messages", [])

    msg = f"<b>Outlook-Inbox: {n} ungelesene Mail(s)</b>\n\n"
    msg += "<b>Verteilung:</b>\n"
    for cls, count in classified.items():
        msg += f"  {cls}: {count}\n"

    msg += "\n<b>Details (max 10):</b>\n"
    for m in msgs[:10]:
        cls_emoji = {
            "RELEVANT_KUNDE": "K",
            "RELEVANT_GESCHAEFT": "G",
            "NICHT_RELEVANT": "N",
            "PRIVAT": "P",
            "UNSICHER": "?",
        }.get(m["classification"], "?")
        msg += (
            f"\n<b>[{cls_emoji}] {m['classification']}</b> ({m['confidence']})\n"
            f"Von: {m['sender']}\n"
            f"Betreff: {m['subject']}\n"
            f"Grund: {m['reason'][:100]}\n"
        )
        # Bei RELEVANT_KUNDE: Verarbeitungs-Status anzeigen
        pr = m.get("process_result")
        if pr is not None:
            if pr.get("success"):
                msg += f"  Q hat geantwortet + Mail verschoben\n"
            elif pr.get("error"):
                msg += f"  Fehler: {pr['error'][:80]}\n"

    return msg


async def _handle_microsoft_test_command(chat_id):
    """Schickt eine Test-Mail an die Tenant-eigene Adresse."""
    from core.integrations.microsoft import (
        send_mail_as_user,
        get_microsoft_status,
        MicrosoftNotConnectedError,
    )

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    status = await get_microsoft_status(tenant.id)
    if not status["connected"]:
        return (
            "Microsoft 365 nicht verbunden.\n\n"
            "Erst /microsoft_setup verwenden."
        )

    to_email = status["account_email"]
    if not to_email:
        return "Kann Empfaenger nicht ermitteln. Bitte /microsoft_setup neu starten."

    body_html = (
        "<p>Hallo,</p>"
        "<p>diese Test-Mail wurde von <b>Gewerbeagent</b> in deinem Namen gesendet.</p>"
        "<p>Wenn du diese Mail in deinem Posteingang siehst, "
        "funktioniert die Microsoft-365-Anbindung korrekt.</p>"
        "<p>Viele Gruesse<br>Q (dein digitaler Assistent)</p>"
    )

    try:
        ok = await send_mail_as_user(
            tenant_id=tenant.id,
            to_email=to_email,
            subject="Test-Mail von Gewerbeagent",
            body_html=body_html,
        )
    except MicrosoftNotConnectedError:
        return "Microsoft-Account ist nicht (mehr) verbunden. /microsoft_setup neu."
    except Exception as e:
        logger.exception("microsoft_test fehler: %s", e)
        return f"Fehler beim Senden: {e}"

    if not ok:
        return (
            "<b>Test-Mail konnte nicht gesendet werden.</b>\n\n"
            "Pruefe /microsoft_status. "
            "Oder /microsoft_setup neu durchlaufen."
        )

    return (
        f"Test-Mail an <b>{to_email}</b> gesendet.\n\n"
        "Schau in deine Inbox (kann bis zu 1 Min dauern)."
    )


async def _handle_aufnahme_command(chat_id):
    """Startet den /aufnahme-Wizard fuer Kundengespraeche."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )

    await _save_state(chat_id, STATE_AUFNAHME_WAITING_AUDIO, {})
    msg = "<b>📞 Kundengespraech aufnehmen</b>\n\n"
    msg += "Sende mir jetzt eine Sprachnachricht mit dem Kundengespraech.\n\n"
    msg += "<b>Wichtig:</b> Vorher Zustimmung des Kunden einholen!\n\n"
    msg += "Ich werde:\n"
    msg += "• Das Gespraech transkribieren\n"
    msg += "• Kunden-Daten + Anliegen extrahieren\n"
    msg += "• Ein Briefing fuer dich speichern (fuer den Termin)\n"
    msg += "• Optional: ein Lexware-Angebots-Draft erstellen\n\n"
    msg += "Maximal 30 Min Aufnahme.\n"
    msg += "/abbrechen um abzubrechen."
    return msg


def _format_aufnahme_preview(extracted: dict) -> str:
    """Formatiert die Vorschau eines analysierten Kundengespraechs."""
    msg = "<b>📋 Kundengespraech analysiert</b>\n\n"

    # Kunde
    kunde = extracted.get("kunde_name") or "<i>(unbekannt)</i>"
    msg += f"<b>Kunde:</b> {kunde}\n"

    parts = []
    if extracted.get("kunde_strasse"):
        parts.append(extracted["kunde_strasse"])
    if extracted.get("kunde_plz") or extracted.get("kunde_ort"):
        parts.append(
            f"{extracted.get('kunde_plz') or ''} {extracted.get('kunde_ort') or ''}".strip()
        )
    if parts:
        msg += f"<i>{', '.join(parts)}</i>\n"
    if extracted.get("kunde_telefon"):
        msg += f"📞 {extracted['kunde_telefon']}\n"
    if extracted.get("kunde_email"):
        msg += f"✉️ {extracted['kunde_email']}\n"

    # Briefing kurz
    briefing = extracted.get("briefing_kurz")
    if briefing:
        msg += f"\n<b>📝 Briefing:</b>\n<i>{briefing}</i>\n"

    # Termin
    termin = extracted.get("termin_datum")
    if termin:
        msg += f"\n<b>📅 Termin:</b> {termin}"
        if extracted.get("termin_ort"):
            msg += f" @ {extracted['termin_ort']}"
        msg += "\n"

    # Positionen (falls Preise im Gespraech genannt)
    positionen = extracted.get("positionen") or []
    if positionen:
        msg += "\n<b>💰 Positionen (aus Gespraech):</b>\n"
        gesamt = 0.0
        ohne_preis = 0
        for i, p in enumerate(positionen, 1):
            menge = p.get("menge") or 1.0
            einheit = p.get("einheit") or "Stueck"
            preis = p.get("preis_brutto_eur")
            name = p.get("name") or "(?)"
            if preis is not None:
                summe = float(menge) * float(preis)
                gesamt += summe
                msg += f"  {i}. {name}: {menge} {einheit} × {preis:.2f}€ = <b>{summe:.2f}€</b>\n"
            else:
                ohne_preis += 1
                msg += f"  {i}. {name}: {menge} {einheit} (Preis offen)\n"
        if gesamt > 0:
            msg += f"  <b>Summe (genannte Preise): {gesamt:.2f}€</b>\n"
        if ohne_preis > 0:
            msg += f"  <i>{ohne_preis} Position(en) ohne Preis</i>\n"
    else:
        msg += "\n<i>Keine Positionen im Gespraech erkannt.</i>\n"

    # TODOs
    todos = extracted.get("todos") or []
    if todos:
        msg += "\n<b>✅ TODOs fuer dich:</b>\n"
        for todo in todos[:8]:  # max 8 zeigen
            msg += f"  • {todo}\n"

    # Confidence + Missing
    conf = extracted.get("extraction_confidence") or "low"
    if conf == "low":
        msg += "\n⚠️ <i>Niedrige Confidence - bitte Daten genau pruefen!</i>"
    elif conf == "medium":
        msg += "\n<i>Mittlere Confidence - bitte Vorschau pruefen.</i>"

    missing = extracted.get("missing_fields") or []
    if missing:
        msg += f"\n<i>Fehlende Felder: {', '.join(missing)}</i>"

    return msg


async def _handle_aufnahme_audio_received(chat_id, voice_dict, bot_token=None):
    """Tenant hat Audio-Aufnahme geschickt. Gemini analysiert + DB-Speicherung + Vorschau."""
    import uuid as _uuid
    from datetime import datetime as _datetime, timezone as _timezone

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return None

    # Sofort Feedback
    await _send_to_chat(chat_id, "<i>🎧 Hoere mir das Gespraech an... Das kann 30-60 Sek dauern.</i>")

    # Audio downloaden
    if bot_token is None:
        bot_token = await _load_global_bot_token()
    audio_bytes, audio_mime = await _telegram_download_voice(bot_token, voice_dict)
    if not audio_bytes:
        await _clear_state(chat_id)
        return "Konnte die Sprachnachricht nicht laden. Bitte erneut versuchen."
    if len(audio_bytes) > AUFNAHME_MAX_AUDIO_BYTES:
        await _clear_state(chat_id)
        return (
            f"Die Aufnahme ist zu lang ({len(audio_bytes) // 1024 // 1024} MB).\n"
            f"Maximum: {AUFNAHME_MAX_AUDIO_BYTES // 1024 // 1024} MB (~30 Min).\n"
            "Bitte ggf. in mehrere Aufnahmen aufteilen."
        )

    # Audio-Dauer aus voice_dict (Telegram liefert "duration" in Sekunden)
    audio_dauer = voice_dict.get("duration")

    # Gemini-Analyse
    try:
        extracted = await analyse_kundengespraech_from_audio(audio_bytes, mime_type=audio_mime)
    except Exception as e:
        logger.error(f"analyse_kundengespraech fehler: {e}", exc_info=True)
        await _clear_state(chat_id)
        return f"❌ Fehler bei Analyse: {e}\n\nBitte erneut versuchen."

    # Pflicht: kunde_name muss da sein
    if not extracted.get("kunde_name"):
        await _clear_state(chat_id)
        return (
            "❌ Konnte keinen Kundennamen aus der Aufnahme extrahieren.\n\n"
            "Bitte erneut aufnehmen und sicherstellen dass der Kundenname genannt wird."
        )

    # In DB speichern
    async with AsyncSessionLocal() as session:
        gespraech = Kundengespraech(
            tenant_id=tenant.id,
            kunde_name=extracted["kunde_name"][:300],
            audio_dauer_sekunden=audio_dauer,
            raw_transcript=extracted.get("transcript"),
            briefing_kurz=extracted.get("briefing_kurz"),
            notizen_lang=extracted.get("notizen_lang"),
            todos=extracted.get("todos") or [],
            termin_ort=extracted.get("termin_ort"),
            confidence=extracted.get("extraction_confidence"),
            status="erfasst",
        )
        # Termin-Datum parsen falls vorhanden
        termin_str = extracted.get("termin_datum")
        if termin_str:
            try:
                # Versuche verschiedene Formate
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        dt = _datetime.strptime(termin_str[:19], fmt)
                        gespraech.termin_datum = dt.replace(tzinfo=_timezone.utc)
                        break
                    except ValueError:
                        continue
            except Exception as e:
                logger.warning(f"Konnte termin_datum nicht parsen: {termin_str!r} | {e}")

        session.add(gespraech)
        await session.commit()
        gespraech_id = gespraech.id

    logger.info(
        "Kundengespraech gespeichert: id=%s tenant=%s kunde=%r positionen=%d todos=%d",
        gespraech_id, tenant.id, extracted.get("kunde_name"),
        len(extracted.get("positionen") or []),
        len(extracted.get("todos") or []),
    )

    # State auf PREVIEWING setzen, ID + extracted im state_data fuer Callback
    await _save_state(
        chat_id,
        STATE_AUFNAHME_PREVIEWING,
        {
            "gespraech_id": str(gespraech_id),
            "extracted": extracted,
        },
    )

    # Vorschau senden mit Buttons
    preview = _format_aufnahme_preview(extracted)

    has_positionen_mit_preis = any(
        p.get("preis_brutto_eur") is not None
        for p in (extracted.get("positionen") or [])
    )

    if has_positionen_mit_preis:
        keyboard = [
            [
                {"text": "✅ Mit Lexware-Angebot", "callback_data": f"aufnahme:angebot:{gespraech_id}"},
            ],
            [
                {"text": "📋 Nur speichern", "callback_data": f"aufnahme:speichern:{gespraech_id}"},
                {"text": "❌ Verwerfen", "callback_data": f"aufnahme:verwerfen:{gespraech_id}"},
            ],
        ]
    else:
        # Kein Preis genannt -> nur speichern oder verwerfen
        keyboard = [
            [
                {"text": "📋 Speichern (kein Angebot)", "callback_data": f"aufnahme:speichern:{gespraech_id}"},
                {"text": "❌ Verwerfen", "callback_data": f"aufnahme:verwerfen:{gespraech_id}"},
            ],
        ]

    await _send_with_inline_buttons(chat_id, preview, keyboard)
    return None  # Nachricht ist schon gesendet


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

    if action == "start_mail":
        # User klickt 'Per Mail senden' nach Lexware-Draft
        await _handle_rechnung_start_mail(chat_id, rechnung_id, bot_token)
        return

    if action == "confirm_mail":
        # User klickt 'Senden' nach Mail-Adress-Bestaetigung
        await _handle_rechnung_send_mail_now(chat_id, rechnung_id, bot_token)
        return

    if action == "redo_mail":
        # User will andere Mail-Adresse eingeben
        await _save_state(
            chat_id,
            STATE_RECHNUNG_AWAITING_MAIL,
            {"rechnung_id": str(rechnung_id), "stage": "awaiting_mail_address"},
        )
        await _send_to_chat(
            chat_id,
            "OK, an welche Mail-Adresse soll ich die Rechnung schicken?\n\n"
            "<i>Tippe die Adresse, oder schreibe &quot;weiss nicht&quot; um die Kunden-Daten zum Anrufen zu sehen.</i>",
        )
        return

    await _send_to_chat(chat_id, f"Unbekannte Aktion: {action}")



async def _handle_aufnahme_callback(chat_id, callback_data, callback_query_id, bot_token):
    """Verarbeitet Callbacks von Aufnahme-Buttons.

    Format: aufnahme:<action>:<gespraech_id>
    Actions:
      - angebot   = Lexware-Angebots-Draft erstellen + verknuepfen
      - speichern = Nur in DB lassen, kein Angebot
      - verwerfen = DB-Eintrag loeschen
    """
    import uuid as _uuid
    from sqlalchemy import select

    parts = callback_data.split(":")
    if len(parts) != 3:
        await _answer_callback_query(callback_query_id, "Ungueltige Aktion", bot_token)
        return

    _, action, gespraech_id_str = parts
    try:
        gespraech_id = _uuid.UUID(gespraech_id_str)
    except ValueError:
        await _answer_callback_query(callback_query_id, "Ungueltige Aktion", bot_token)
        return

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _answer_callback_query(callback_query_id, "Tenant nicht gefunden", bot_token)
        return

    async with AsyncSessionLocal() as session:
        # Gespraech laden
        result = await session.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gespraech_id,
                Kundengespraech.tenant_id == tenant.id,
            )
        )
        gespraech = result.scalar_one_or_none()
        if not gespraech:
            await _answer_callback_query(callback_query_id, "Gespraech nicht gefunden", bot_token)
            await _clear_state(chat_id)
            return

        # === Action: VERWERFEN ===
        if action == "verwerfen":
            await session.delete(gespraech)
            await session.commit()
            await _answer_callback_query(callback_query_id, "Verworfen", bot_token)
            await _clear_state(chat_id)
            await _send_to_chat(chat_id, "🗑 Gespraech verworfen.")
            return

        # === Action: SPEICHERN (nur DB, kein Angebot) ===
        if action == "speichern":
            gespraech.status = "erfasst"
            await session.commit()
            await _answer_callback_query(callback_query_id, "Gespeichert", bot_token)
            await _clear_state(chat_id)
            briefing = gespraech.briefing_kurz or "<i>(kein Briefing)</i>"
            await _send_to_chat(
                chat_id,
                f"✅ Gespraech mit <b>{gespraech.kunde_name}</b> gespeichert.\n\n"
                f"<b>Briefing:</b> <i>{briefing}</i>\n\n"
                f"Spaeter abrufbar mit /briefing oder /kunde {gespraech.kunde_name.split()[0] if gespraech.kunde_name else ''}"
            )
            return

        # === Action: ANGEBOT (Lexware-Draft erstellen + verknuepfen) ===
        if action == "angebot":
            # Lexware-Provider laden
            provider = await _get_lexware_provider_for_tenant(tenant)
            if not provider:
                await _answer_callback_query(callback_query_id, "Lexware nicht verbunden", bot_token)
                await _send_to_chat(
                    chat_id,
                    "❌ Lexware ist nicht verbunden.\n"
                    "Bitte /lexware_setup ausfuehren. Gespraech bleibt gespeichert."
                )
                gespraech.status = "erfasst"
                await session.commit()
                await _clear_state(chat_id)
                return

            # extracted aus state_data laden
            state = await _load_state(chat_id)
            if not state or not state.state_data:
                await _answer_callback_query(callback_query_id, "Session abgelaufen", bot_token)
                await _clear_state(chat_id)
                return
            extracted = state.state_data.get("extracted") or {}
            positionen = extracted.get("positionen") or []
            positionen_mit_preis = [p for p in positionen if p.get("preis_brutto_eur") is not None]
            if not positionen_mit_preis:
                await _answer_callback_query(callback_query_id, "Keine Positionen mit Preis", bot_token)
                await _send_to_chat(
                    chat_id,
                    "❌ Keine Positionen mit Preisen gefunden. Angebot kann nicht erstellt werden.\n"
                    "Gespraech bleibt gespeichert."
                )
                gespraech.status = "erfasst"
                await session.commit()
                await _clear_state(chat_id)
                return

            # Sofortiges Feedback
            await _answer_callback_query(callback_query_id, "Erstelle Angebot...", bot_token)
            await _send_to_chat(chat_id, "<i>📝 Lege Angebot in Lexware an...</i>")

            # Angebot in DB anlegen
            from core.models import Angebot, AngebotPosition
            from decimal import Decimal as _Dec

            angebot = Angebot(
                tenant_id=tenant.id,
                quelle="telegram_voice",
                raw_input=extracted.get("transcript", "")[:5000] if extracted.get("transcript") else None,
                kunde_name=gespraech.kunde_name,
                kunde_strasse=extracted.get("kunde_strasse"),
                kunde_plz=extracted.get("kunde_plz"),
                kunde_ort=extracted.get("kunde_ort"),
                introduction_text=None,  # spaeter Gemini-generiert
                remark_text=None,
                status="erstellt",
                confidence=extracted.get("extraction_confidence"),
            )
            session.add(angebot)
            await session.flush()  # ID generieren

            gesamt = _Dec("0")
            for i, p in enumerate(positionen_mit_preis, 1):
                menge = _Dec(str(p.get("menge") or 1))
                preis = _Dec(str(p.get("preis_brutto_eur") or 0))
                pos = AngebotPosition(
                    angebot_id=angebot.id,
                    position_nr=i,
                    name=(p.get("name") or "")[:500],
                    beschreibung=p.get("beschreibung"),
                    menge=menge,
                    einheit=(p.get("einheit") or "Stueck")[:50],
                    preis_brutto_eur=preis,
                    mwst_prozent=int(p.get("mwst_prozent") or 19),
                )
                session.add(pos)
                gesamt += menge * preis
            angebot.gesamtbetrag_brutto_eur = gesamt

            # Lexware-Draft erstellen
            from core.integrations.accounting_base import InvoiceLineItem

            line_items = [
                InvoiceLineItem(
                    name=(p.get("name") or "")[:200],
                    quantity=float(p.get("menge") or 1),
                    unit_name=(p.get("einheit") or "Stueck"),
                    unit_price_gross=float(p.get("preis_brutto_eur") or 0),
                    description=p.get("beschreibung"),
                    tax_rate_percent=int(p.get("mwst_prozent") or 19),
                )
                for p in positionen_mit_preis
            ]

            one_time_address = {
                "name": gespraech.kunde_name,
                "countryCode": "DE",
            }
            if extracted.get("kunde_strasse"):
                one_time_address["street"] = extracted["kunde_strasse"]
            if extracted.get("kunde_plz"):
                one_time_address["zip"] = extracted["kunde_plz"]
            if extracted.get("kunde_ort"):
                one_time_address["city"] = extracted["kunde_ort"]

            try:
                quotation = await provider.create_quotation_draft(
                    line_items=line_items,
                    one_time_address=one_time_address,
                    title=f"Angebot {gespraech.kunde_name}",
                    introduction=f"Sehr geehrte/r {gespraech.kunde_name},\n\nvielen Dank fuer Ihre Anfrage. Wir freuen uns, Ihnen folgendes Angebot zu unterbreiten.",
                    remark="Die Preise verstehen sich inkl. gesetzlicher MwSt.\n\nWir freuen uns auf Ihren Auftrag!",
                    tax_type="gross",
                )
            except Exception as e:
                logger.error(f"Lexware create_quotation fehler: {e}", exc_info=True)
                await session.rollback()
                gespraech.status = "erfasst"
                await session.commit()
                await _clear_state(chat_id)
                await _send_to_chat(chat_id, f"❌ Lexware-Fehler: {e}\n\nGespraech bleibt gespeichert.")
                return

            # Angebot mit Lexware-IDs aktualisieren
            angebot.lexware_quotation_id = quotation.quotation_id
            angebot.status = "in_lexware"
            gespraech.angebot_id = angebot.id
            gespraech.status = "mit_angebot"
            await session.commit()

            await _clear_state(chat_id)
            await _send_to_chat(
                chat_id,
                f"✅ <b>Angebot erstellt!</b>\n\n"
                f"Kunde: {gespraech.kunde_name}\n"
                f"Gesamt: {float(gesamt):.2f}€\n\n"
                f"<a href=\"{quotation.deeplink_view}\">→ In Lexware oeffnen</a>\n\n"
                f"<i>Bitte in Lexware pruefen, ggf. anpassen, dann versenden.</i>"
            )
            return

    await _answer_callback_query(callback_query_id, "Unbekannte Aktion", bot_token)


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

    # LineItems bauen: alle Positionen aus rechnung_positionen-Tabelle laden
    line_items = []
    async with AsyncSessionLocal() as s:
        positionen_db = (await s.execute(
            select(RechnungPosition)
            .where(RechnungPosition.rechnung_id == rechnung_id)
            .order_by(RechnungPosition.position_nr.asc())
        )).scalars().all()

    for p in positionen_db:
        line_items.append(InvoiceLineItem(
            name=p.name,
            quantity=float(p.menge),
            unit_name=p.einheit,
            unit_price_gross=float(p.preis_brutto_eur),
            description=p.beschreibung,
            tax_rate_percent=int(p.mwst_prozent),
        ))

    # Fallback falls keine Positionen in DB: alte Single-Line-Logik
    if not line_items:
        line_items.append(InvoiceLineItem(
            name=leistung_titel or "Leistung",
            quantity=1,
            unit_name="Stueck",
            unit_price_gross=betrag,
            description=leistung_beschreibung,
            tax_rate_percent=19,
        ))

    logger.info(
        f"Lexware-Invoice fuer rechnung={rechnung_id}: {len(line_items)} Positionen"
    )

    # Schritt 1: Kontakt suchen oder anlegen
    await _send_to_chat(chat_id, "<i>Lege Kunde in Lexware an...</i>")

    contact_id = None
    is_company = bool(kunde_name and any(
        kw in (kunde_name or "").lower()
        for kw in ("gmbh", "ag", "kg", "ohg", "ug", "gbr", "e.k.", "ev", "verein", "bauunternehmen", "firma")
    ))

    try:
        existing = await provider.search_contacts(kunde_name or "", customer_only=True)
        match = None
        if kunde_name:
            for cand in existing:
                if cand.name.strip().lower() == kunde_name.strip().lower():
                    if not kunde_ort or (cand.city and cand.city.lower() == kunde_ort.lower()):
                        match = cand
                        break

        if match:
            contact_id = match.contact_id
            logger.info(f"Lexware-Kontakt match: {match.name} -> {contact_id}")
        else:
            new_contact = await provider.create_customer_contact(
                name=kunde_name or "Kunde",
                street=kunde_strasse,
                zip_code=kunde_plz,
                city=kunde_ort,
                is_company=is_company,
            )
            contact_id = new_contact.contact_id
            logger.info(f"Lexware-Kontakt angelegt: {new_contact.name} -> {contact_id}")
    except Exception as e:
        logger.warning(f"Contact-Handling fehlgeschlagen, fallback auf one-time-address: {e}")
        contact_id = None

    if contact_id is not None:
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.lexware_contact_id = contact_id
                await s.commit()

    # Schritt 2: Rechnungs-Draft anlegen
    await _send_to_chat(chat_id, "<i>Lege Rechnungs-Entwurf in Lexware an...</i>")

    one_time_address = None
    if contact_id is None:
        one_time_address = {
            "name": kunde_name or "Kunde",
            "countryCode": "DE",
        }
        if kunde_strasse:
            one_time_address["street"] = kunde_strasse
        if kunde_plz:
            one_time_address["zip"] = kunde_plz
        if kunde_ort:
            one_time_address["city"] = kunde_ort

    try:
        draft = await provider.create_invoice_draft(
            line_items=line_items,
            contact_id=contact_id,
            one_time_address=one_time_address,
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

    # Erfolgs-Nachricht mit Folge-Buttons (jetzt mit Mail-Versand-Option)
    msg = "<b>Entwurf in Lexware angelegt.</b>\n\n"
    msg += f'<a href="{draft.deeplink_view}">In Lexware oeffnen und pruefen</a>\n\n'
    msg += (
        "<i>Empfohlen: erst in Lexware oeffnen, Anschrift pruefen "
        "und finalisieren. Dann hier &quot;Per Mail senden&quot; klicken.</i>"
    )

    buttons = [
        [{"text": "✋ Per Mail senden", "callback_data": f"rg:start_mail:{rechnung_id}"}],
        [{"text": "Erstmal nur Entwurf, fertig", "callback_data": f"rg:finish:{rechnung_id}"}],
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
        betrag = f"{float(rg.betrag_brutto_eur):.0f}€" if rg.betrag_brutto_eur is not None else "?"

        if rg.status == RECHNUNG_STATUS_BEZAHLT:
            paid_str = rg.bezahlt_am.strftime("%d.%m.") if rg.bezahlt_am else "?"
            if rg.lexware_invoice_id:
                link = LexwareProvider.invoice_deeplink_view(rg.lexware_invoice_id)
                lines.append(
                    f'• {ts} {kunde} {betrag} ✅ bezahlt {paid_str} '
                    f'<a href="{link}">Lexware</a>'
                )
            else:
                lines.append(f'• {ts} {kunde} {betrag} ✅ bezahlt {paid_str}')
        elif rg.status == RECHNUNG_STATUS_MAIL_SENT:
            # Versendet, noch nicht bezahlt — zusaetzlich Lexware-Cache zeigen
            if rg.lexware_voucher_status == "voided":
                marker = "🚫 storniert"
            elif rg.last_paid_check_at:
                check_str = rg.last_paid_check_at.strftime("%d.%m. %H:%M")
                marker = f"⏳ offen (geprueft {check_str})"
            else:
                marker = "⏳ offen"
            link = (
                LexwareProvider.invoice_deeplink_view(rg.lexware_invoice_id)
                if rg.lexware_invoice_id else None
            )
            tail = f' <a href="{link}">Lexware</a>' if link else ""
            lines.append(f'• {ts} {kunde} {betrag} {marker}{tail}')
        elif rg.status == RECHNUNG_STATUS_DRAFTED and rg.lexware_invoice_id:
            link = LexwareProvider.invoice_deeplink_view(rg.lexware_invoice_id)
            lines.append(f'• {ts} {kunde} {betrag} <a href="{link}">in Lexware</a>')
        elif rg.status == RECHNUNG_STATUS_ERROR:
            err = (rg.error_message or "?")[:50]
            lines.append(f'• {ts} {kunde} {betrag} <i>Fehler: {err}</i>')
        elif rg.status == RECHNUNG_STATUS_CANCELLED:
            lines.append(f'• {ts} {kunde} {betrag} (abgebrochen)')
        else:
            lines.append(f'• {ts} {kunde} {betrag} Status: {rg.status}')
    return "\n".join(lines)




# =====================================================================
# Werkstatt-Setup-Wizard
# (Heimat-Adresse fuer Smart-Termin-Routing — verwendet von Kalender-
#  Plugin um Fahrtzeiten zwischen Terminen einzurechnen)
# =====================================================================

YES_TRIGGERS = ("ja", "j", "yes", "y", "ok", "speichern", "passt", "stimmt")
NO_TRIGGERS = ("nein", "n", "no", "abbrechen", "nochmal", "neu", "falsch")


def _format_werkstatt_status(employee, *, label="Werkstatt-Adresse") -> str:
    """Liefert Anzeige-Text der Heimat-Daten eines Mitarbeiters.

    Phase-3-Multi-Mitarbeiter: zeigt employee.heimat_* statt
    tenant.heimat_*. Der Wizard arbeitet ab jetzt pro Mitarbeiter.
    """
    if not employee.heimat_strasse and not employee.heimat_ort:
        return (
            f"<b>{label}</b>\n\n"
            "Noch keine Heimat-Adresse hinterlegt.\n\n"
            "Mit /werkstatt eintragen — wird gebraucht damit "
            "Q bei Termin-Vorschlaegen die Fahrtzeiten zwischen "
            "Kunden einrechnen kann."
        )
    addr_parts = []
    if employee.heimat_strasse:
        addr_parts.append(employee.heimat_strasse)
    if employee.heimat_plz or employee.heimat_ort:
        addr_parts.append(
            f"{employee.heimat_plz or ''} {employee.heimat_ort or ''}".strip()
        )
    addr = ", ".join(addr_parts)
    geo = ""
    if employee.heimat_lat is not None and employee.heimat_lon is not None:
        geo = (
            f"\n📍 Geo: {employee.heimat_lat}, {employee.heimat_lon}"
            f" (fuer Routing geocoded)"
        )
    owner = " 👑" if employee.is_default else ""
    msg = f"<b>{label} — {employee.name}{owner}</b>\n\n"
    msg += f"📍 {addr}{geo}\n\n"
    msg += f"⏱ Puffer pro Termin: {employee.fahrtzeit_puffer_min} Min\n\n"
    msg += "Mit /werkstatt aendern."
    return msg


async def _update_employee_werkstatt(
    employee_id, *, strasse, plz, ort, lat, lon, mirror_to_tenant_id=None,
):
    """UPDATE auf employees — heimat_*-Felder setzen.

    Wenn mirror_to_tenant_id gesetzt (= Default-Employee): zusaetzlich
    tenant.heimat_* spiegeln, damit Code-Pfade die noch nicht employee-
    aware sind (rechnung_paid_summary, voice_init etc.) konsistent
    bleiben. Spaeter werden diese ebenfalls migriert.
    """
    from sqlalchemy import update
    from decimal import Decimal
    from core.models.employee import Employee
    lat_dec = Decimal(str(round(lat, 6))) if lat is not None else None
    lon_dec = Decimal(str(round(lon, 6))) if lon is not None else None
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(Employee).where(Employee.id == employee_id).values(
                heimat_strasse=strasse,
                heimat_plz=plz,
                heimat_ort=ort,
                heimat_lat=lat_dec,
                heimat_lon=lon_dec,
            )
        )
        if mirror_to_tenant_id is not None:
            await s.execute(
                update(Tenant).where(Tenant.id == mirror_to_tenant_id).values(
                    heimat_strasse=strasse,
                    heimat_plz=plz,
                    heimat_ort=ort,
                    heimat_lat=lat_dec,
                    heimat_lon=lon_dec,
                )
            )
        await s.commit()


async def _handle_werkstatt_command(chat_id):
    """Startet den Werkstatt-Setup-Wizard fuer den AKTUELLEN Mitarbeiter.

    Phase-3-Multi-Mitarbeiter: jeder Mitarbeiter pflegt seine eigene
    Heimat. Default-Employee (Inhaber) spiegelt zusaetzlich auf
    tenant.heimat_* damit Legacy-Code-Pfade konsistent bleiben.
    """
    res = await _get_current_employee(chat_id)
    if res is None:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst den Aktivierungs-QR-Code scannen."
        )
    tenant, employee = res

    has_address = bool(employee.heimat_strasse and employee.heimat_ort)

    await _save_state(chat_id, STATE_WERKSTATT_WAITING_ADDRESS, {})

    label = "Heimat-Adresse" if not employee.is_default else "Werkstatt-Adresse"

    if has_address:
        msg = _format_werkstatt_status(employee, label=label) + "\n\n"
        msg += (
            "<b>Neue Adresse?</b>\n"
            "Schicke einfach die komplette Adresse, z.B.:\n"
            "<code>Hauptstr. 5, 54290 Trier</code>\n\n"
            "Oder /abbrechen wenn alles passt."
        )
        return msg

    msg = f"<b>{label} einrichten — {employee.name}</b>\n\n"
    if employee.is_default:
        msg += (
            "Q braucht deine Werkstatt-Adresse um bei Termin-Vorschlaegen "
            "die Fahrtzeit von einem Kunden zum naechsten einzurechnen — "
            "damit du nicht im Stress hetzen musst.\n\n"
        )
    else:
        msg += (
            "Wenn du morgens von zuhause direkt zum ersten Kunden faehrst, "
            "trage hier deine Heim-Adresse ein. Q rechnet dann bei deinen "
            "Termin-Vorschlaegen die Anfahrt von dort statt von der Werkstatt.\n\n"
        )
    if not ors_is_configured():
        msg += (
            "<i>⚠ Hinweis: Smart-Routing-API noch nicht aktiviert "
            "(Betreiber muss OPENROUTESERVICE_API_KEY setzen). "
            "Adresse wird trotzdem schon gespeichert, "
            "Routing greift sobald der Key da ist.</i>\n\n"
        )
    msg += (
        "<b>Schicke die komplette Adresse</b>, z.B.:\n"
        "<code>Hauptstr. 5, 54290 Trier</code>\n\n"
        "Oder /abbrechen."
    )
    return msg


async def _handle_werkstatt_status_command(chat_id):
    """/werkstatt_status — nur Anzeige, keinen Wizard starten."""
    await _clear_state(chat_id)
    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    _, employee = res
    label = "Heimat-Adresse" if not employee.is_default else "Werkstatt-Adresse"
    return _format_werkstatt_status(employee, label=label)


async def _handle_werkstatt_address_input(chat_id, text):
    """User hat Adresse getippt. Geocoden + Bestaetigung anbieten."""
    text = (text or "").strip()
    if len(text) < 6:
        return (
            "Adresse zu kurz. Bitte schicke etwas wie:\n"
            "<code>Hauptstr. 5, 54290 Trier</code>"
        )

    res = await _get_current_employee(chat_id)
    if res is None:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden."
    tenant, employee = res

    # 1. Versuche zu geocoden
    point = await ors_geocode_address(text)
    if point is None:
        # Kein Geocoding moeglich (kein Key oder Adresse nicht gefunden).
        # Wir lassen den Tenant trotzdem bestaetigen — die Adresse wird
        # als Text gespeichert, Routing greift sobald Geo-Daten da sind.
        # Adresse aufteilen mit grobem Heuristik-Parser.
        parsed = _heuristic_parse_address(text)
        await _save_state(
            chat_id, STATE_WERKSTATT_CONFIRMING,
            {
                "raw": text,
                "strasse": parsed["strasse"],
                "plz": parsed["plz"],
                "ort": parsed["ort"],
                "lat": None, "lon": None,
            },
        )
        msg = "<b>Adresse aufgenommen</b>\n\n"
        if parsed["strasse"]:
            msg += f"Strasse: {parsed['strasse']}\n"
        if parsed["plz"] or parsed["ort"]:
            msg += f"Ort: {parsed['plz']} {parsed['ort']}\n"
        msg += (
            "\n<i>⚠ Die Geo-Koordinaten konnten nicht ermittelt werden "
            "(Routing-API nicht verfuegbar oder Adresse nicht eindeutig). "
            "Wird nachgereicht sobald moeglich.</i>\n\n"
        )
        msg += "Mit <b>JA</b> speichern, mit <b>NEIN</b> nochmal eintippen."
        return msg

    # 2. Adresse aufteilen, Geo merken
    parsed = _heuristic_parse_address(text)
    await _save_state(
        chat_id, STATE_WERKSTATT_CONFIRMING,
        {
            "raw": text,
            "strasse": parsed["strasse"],
            "plz": parsed["plz"],
            "ort": parsed["ort"],
            "lat": point.lat,
            "lon": point.lon,
        },
    )
    msg = "<b>Adresse gefunden</b>\n\n"
    msg += f"📍 {text}\n"
    if parsed["strasse"]:
        msg += f"Strasse: {parsed['strasse']}\n"
    if parsed["plz"] or parsed["ort"]:
        msg += f"Ort: {parsed['plz']} {parsed['ort']}\n"
    msg += f"\n📌 Geo: {point.lat:.5f}, {point.lon:.5f}\n"
    msg += (
        f"<a href=\"https://www.openstreetmap.org/?mlat={point.lat}"
        f"&mlon={point.lon}#map=16/{point.lat}/{point.lon}\">"
        f"Auf Karte ansehen</a>\n\n"
    )
    msg += "Mit <b>JA</b> speichern, mit <b>NEIN</b> nochmal eintippen."
    return msg


async def _handle_werkstatt_confirm_input(chat_id, text, state_data):
    """Mitarbeiter tippt JA / NEIN."""
    t = (text or "").strip().lower()
    state_data = state_data or {}

    if t in YES_TRIGGERS or t.startswith("ja"):
        res = await _get_current_employee(chat_id)
        if res is None:
            await _clear_state(chat_id)
            return "Tenant nicht gefunden."
        tenant, employee = res
        # Default-Employee spiegelt zusaetzlich auf tenant.heimat_*
        # damit Legacy-Code weiter konsistent liest.
        mirror = tenant.id if employee.is_default else None
        await _update_employee_werkstatt(
            employee.id,
            strasse=state_data.get("strasse"),
            plz=state_data.get("plz"),
            ort=state_data.get("ort"),
            lat=state_data.get("lat"),
            lon=state_data.get("lon"),
            mirror_to_tenant_id=mirror,
        )
        await _clear_state(chat_id)
        label = "Werkstatt" if employee.is_default else "Heimat"
        msg = f"✅ <b>{label} gespeichert ({employee.name})</b>\n\n"
        if state_data.get("lat") is None:
            msg += (
                "<i>Hinweis: Geo-Koordinaten fehlen noch. "
                "Sobald Routing-API verfuegbar ist, holen wir sie "
                "automatisch nach.</i>\n\n"
            )
        msg += "Q rechnet ab jetzt bei Termin-Vorschlaegen die Fahrtzeit ein."
        return msg

    if t in NO_TRIGGERS or t.startswith("nein") or t == "/abbrechen":
        await _save_state(chat_id, STATE_WERKSTATT_WAITING_ADDRESS, {})
        return (
            "OK — schicke die Adresse nochmal:\n"
            "<code>Hauptstr. 5, 54290 Trier</code>\n\n"
            "Oder /abbrechen ganz raus."
        )

    return (
        "Bitte mit <b>JA</b> bestaetigen oder <b>NEIN</b> abbrechen.\n"
        "Aktuell vorgemerkt: " + (state_data.get("raw") or "—")
    )


def _heuristic_parse_address(text: str) -> dict:
    """Sehr grober DE-Adress-Parser. Gibt {strasse, plz, ort} zurueck.

    Erwartet 'Strasse + Hausnr, [PLZ] Ort' im typischen Format. Wenn
    das nicht klappt, packen wir alles in 'strasse' und lassen plz/ort
    None — das ist OK fuer Anzeige + Routing geht via Geo, nicht via PLZ.
    """
    import re
    s = (text or "").strip()
    # Versuche "Strasse Hausnr, PLZ Ort" oder "Strasse Hausnr PLZ Ort"
    parts = [p.strip() for p in s.split(",") if p.strip()]
    plz = None
    ort = None
    strasse = None

    if len(parts) >= 2:
        strasse = parts[0]
        # In den Rest: PLZ + Ort suchen
        rest = " ".join(parts[1:])
        m = re.match(r"^(\d{5})\s+(.+)$", rest)
        if m:
            plz = m.group(1)
            ort = m.group(2).strip()
        else:
            ort = rest
    else:
        # 1-Teile: "Hauptstr 5 54290 Trier"
        m = re.search(r"\b(\d{5})\s+([A-Za-zÄÖÜäöüß\s\-]+)$", s)
        if m:
            plz = m.group(1)
            ort = m.group(2).strip()
            strasse = s[: m.start()].strip().rstrip(",")
        else:
            strasse = s

    return {"strasse": strasse, "plz": plz, "ort": ort}




# =====================================================================
# Rechnung Mail-Versand-Wizard (Phase A2)
# =====================================================================

import re as _re

EMAIL_REGEX = _re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Trigger fuer "weiss nicht / hab keine"
NO_MAIL_TRIGGERS = (
    "weiss nicht", "weiss ich nicht", "hab keine", "kenne ich nicht",
    "keine ahnung", "nein", "nope", "?", "??",
    "ich habe sie nicht", "noch nicht", "muss erst fragen",
)


def _looks_like_no_mail(text: str) -> bool:
    """User sagt 'weiss nicht' oder aehnlich."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if t in NO_MAIL_TRIGGERS:
        return True
    for trig in NO_MAIL_TRIGGERS:
        if trig in t and len(t) < 60:
            return True
    return False


def _format_kunde_info_for_phone(rg) -> str:
    """Zeigt dem Tenant alle Kunden-Daten zum manuell Anrufen."""
    msg = "<b>Kunden-Daten zum Anrufen:</b>\n\n"
    if rg.kunde_name:
        msg += f"• <b>Name:</b> {rg.kunde_name}\n"
    addr_parts = []
    if rg.kunde_strasse:
        addr_parts.append(rg.kunde_strasse)
    if rg.kunde_plz or rg.kunde_ort:
        addr_parts.append(f"{rg.kunde_plz or ''} {rg.kunde_ort or ''}".strip())
    if addr_parts:
        msg += f"• <b>Adresse:</b> {', '.join(addr_parts)}\n"
    if rg.leistung_titel:
        leistung = rg.leistung_titel
        if rg.leistung_beschreibung:
            leistung += f" ({rg.leistung_beschreibung})"
        msg += f"• <b>Leistung:</b> {leistung}\n"
    if rg.betrag_brutto_eur is not None:
        msg += f"• <b>Betrag:</b> {float(rg.betrag_brutto_eur):.2f} € brutto\n"
    if rg.lexware_voucher_number:
        msg += f"• <b>Rechnungsnr.:</b> {rg.lexware_voucher_number}\n"
    return msg


async def _handle_rechnung_start_mail(chat_id, rechnung_id, bot_token):
    """User klickt 'Per Mail senden'. Wir pruefen Lexware-Status + holen Mail-Adresse-Vorschlag."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _send_to_chat(chat_id, "Tenant nicht gefunden.")
        return

    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        await _send_to_chat(chat_id, "Lexware nicht verbunden.")
        return

    # Rechnung aus DB laden
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if not rg:
            await _send_to_chat(chat_id, "Rechnung nicht gefunden.")
            return
        if not rg.lexware_invoice_id:
            await _send_to_chat(
                chat_id,
                "Diese Rechnung hat noch keinen Lexware-Eintrag. Erst /rechnung anlegen.",
            )
            return
        rg_data = {
            "id": rg.id,
            "lexware_invoice_id": rg.lexware_invoice_id,
            "lexware_contact_id": rg.lexware_contact_id,
            "kunde_name": rg.kunde_name,
            "kunde_email": rg.kunde_email,
        }

    # Lexware-Status pruefen: noch Draft?
    await _send_to_chat(chat_id, "<i>Pruefe Status in Lexware...</i>")
    try:
        invoice = await provider.get_invoice(rg_data["lexware_invoice_id"])
    except Exception as e:
        logger.exception(f"get_invoice fehlgeschlagen: {e}")
        await _send_to_chat(
            chat_id,
            "Konnte Status in Lexware nicht abrufen. Bitte spaeter erneut versuchen.",
        )
        return

    voucher_status = invoice.get("voucherStatus", "unknown")
    voucher_number = invoice.get("voucherNumber")

    # Voucher-Number falls neu da, in DB speichern
    if voucher_number:
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg and not rg.lexware_voucher_number:
                rg.lexware_voucher_number = voucher_number
                await s.commit()

    # Wenn noch draft -> blocken
    if voucher_status == "draft":
        deeplink = LexwareProvider.invoice_deeplink_view(rg_data["lexware_invoice_id"])
        msg = "<b>Rechnung ist noch im Entwurf-Status.</b>\n\n"
        msg += "PDF kann erst geladen werden, wenn du die Rechnung in Lexware finalisiert hast.\n\n"
        msg += f'<a href="{deeplink}">In Lexware oeffnen</a>\n\n'
        msg += "Klicke dort auf <b>Finalisieren</b> (oder &quot;Festschreiben&quot;), "
        msg += "danach hier nochmal auf &quot;Per Mail senden&quot; klicken."
        buttons = [
            [{"text": "\u270b Per Mail senden (nochmal probieren)", "callback_data": f"rg:start_mail:{rechnung_id}"}],
            [{"text": "Erstmal nur Entwurf, fertig", "callback_data": f"rg:finish:{rechnung_id}"}],
        ]
        await _send_with_inline_buttons(chat_id, msg, buttons, bot_token=bot_token)
        return

    # Status OK -> Mail-Adresse-Vorschlag bauen
    suggested_email = None
    suggested_source = None

    # 1. Wenn schon mal in DB gespeichert
    if rg_data["kunde_email"]:
        suggested_email = rg_data["kunde_email"]
        suggested_source = "fruehere_eingabe"

    # 2. Wenn nicht: aus Lexware-Kontakt holen
    if not suggested_email and rg_data["lexware_contact_id"]:
        try:
            contact = await provider.get_contact(rg_data["lexware_contact_id"])
            if contact and contact.email:
                suggested_email = contact.email
                suggested_source = "lexware_kontakt"
        except Exception as e:
            logger.warning(f"Konnte Kontakt nicht laden: {e}")

    # State setzen
    await _save_state(
        chat_id,
        STATE_RECHNUNG_AWAITING_MAIL,
        {"rechnung_id": str(rechnung_id), "stage": "awaiting_mail_address"},
    )

    msg = f"<b>Rechnung {voucher_number or ''} per Mail an Kunde senden</b>\n\n"
    if suggested_email:
        if suggested_source == "lexware_kontakt":
            msg += f"In Lexware ist diese Mail hinterlegt:\n<code>{suggested_email}</code>\n\n"
        else:
            msg += f"Letzte verwendete Mail:\n<code>{suggested_email}</code>\n\n"
        msg += "Soll ich an diese Adresse senden?\n\n"
        msg += "<i>Oder tippe einfach eine andere Mail-Adresse hier rein.</i>"
        buttons = [
            [{"text": f"✅ Senden an {suggested_email}", "callback_data": f"rg:confirm_mail:{rechnung_id}"}],
            [{"text": "❌ Abbrechen", "callback_data": f"rg:finish:{rechnung_id}"}],
        ]
        await _save_state(
            chat_id,
            STATE_RECHNUNG_AWAITING_MAIL,
            {
                "rechnung_id": str(rechnung_id),
                "stage": "confirming_mail",
                "suggested_email": suggested_email,
            },
        )
        await _send_with_inline_buttons(chat_id, msg, buttons, bot_token=bot_token)
    else:
        msg += "An welche Mail-Adresse soll ich die Rechnung schicken?\n\n"
        msg += "<i>Tippe einfach die Mail-Adresse ein. "
        msg += "Falls du sie nicht zur Hand hast, schreibe &quot;weiss nicht&quot; - "
        msg += "ich gebe dir dann die Kunden-Daten zum Anrufen.</i>"
        await _send_to_chat(chat_id, msg)


async def _handle_rechnung_mail_address_input(chat_id, text, state_data):
    """User tippt Mail-Adresse (oder 'weiss nicht')."""
    rechnung_id_str = (state_data or {}).get("rechnung_id")
    if not rechnung_id_str:
        await _clear_state(chat_id)
        return "Kontext verloren. Bitte mit /rechnung neu starten."

    import uuid as _uuid
    try:
        rechnung_id = _uuid.UUID(rechnung_id_str)
    except Exception:
        await _clear_state(chat_id)
        return "Ungueltiger Kontext. Bitte mit /rechnung neu starten."

    text = (text or "").strip()

    # Variante 1: User sagt "weiss nicht"
    if _looks_like_no_mail(text):
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
        if not rg:
            await _clear_state(chat_id)
            return "Rechnung nicht gefunden."
        msg = _format_kunde_info_for_phone(rg)
        msg += "\n<i>Wenn du die Mail-Adresse hast, einfach hier eintippen. "
        msg += "Oder /abbrechen wenn du heute nicht mehr willst.</i>"
        return msg

    # Variante 2: User tippt eine Mail
    if not EMAIL_REGEX.match(text):
        return (
            "Das sieht nicht wie eine Mail-Adresse aus. "
            "Bitte im Format <code>name@firma.de</code> eintippen, "
            "oder schreibe &quot;weiss nicht&quot; um die Kunden-Daten zu sehen."
        )

    # Mail in DB merken
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if not rg:
            await _clear_state(chat_id)
            return "Rechnung nicht gefunden."
        rg.kunde_email = text
        await s.commit()

    # State auf "confirming"
    await _save_state(
        chat_id,
        STATE_RECHNUNG_AWAITING_MAIL,
        {
            "rechnung_id": str(rechnung_id),
            "stage": "confirming_mail",
            "suggested_email": text,
        },
    )

    bot_token = await _load_global_bot_token()
    msg = f"<b>Senden an</b>\n<code>{text}</code>\n\n"
    msg += "Soll ich die Rechnung jetzt an diese Adresse schicken?\n\n"
    msg += "<i>Du selbst bekommst eine Kopie an deine hinterlegte E-Mail-Adresse.</i>"
    buttons = [
        [{"text": "✅ Ja, senden", "callback_data": f"rg:confirm_mail:{rechnung_id}"}],
        [{"text": "\u270f️ Andere Adresse", "callback_data": f"rg:redo_mail:{rechnung_id}"}],
        [{"text": "❌ Abbrechen", "callback_data": f"rg:finish:{rechnung_id}"}],
    ]
    await _send_with_inline_buttons(chat_id, msg, buttons, bot_token=bot_token)
    return None  # mit Buttons schon gesendet


async def _handle_rechnung_send_mail_now(chat_id, rechnung_id, bot_token):
    """User hat 'Senden'-Button geklickt. Wir laden PDF + schicken Mail."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _send_to_chat(chat_id, "Tenant nicht gefunden.")
        return

    provider = await _get_lexware_provider_for_tenant(tenant)
    if not provider:
        await _send_to_chat(chat_id, "Lexware nicht verbunden.")
        return

    # Rechnung + alle relevanten Daten holen
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if not rg:
            await _send_to_chat(chat_id, "Rechnung nicht gefunden.")
            return
        if not rg.kunde_email:
            await _send_to_chat(chat_id, "Keine Mail-Adresse hinterlegt.")
            return
        rg_data = {
            "id": rg.id,
            "lexware_invoice_id": rg.lexware_invoice_id,
            "lexware_contact_id": rg.lexware_contact_id,
            "voucher_number": rg.lexware_voucher_number,
            "kunde_name": rg.kunde_name or "",
            "kunde_email": rg.kunde_email,
            "betrag": float(rg.betrag_brutto_eur or 0),
        }

    # Tenant-Daten fuer Sender + BCC + Reply-To
    async with AsyncSessionLocal() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.id == tenant.id)
        )).scalar_one_or_none()
        tenant_data = {
            "company_name": t.company_name,
            "contact_name": t.contact_name,
            "contact_email": t.contact_email,
        }

    # Brevo-Config
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig)
            .join(Tenant, ToolConfig.tenant_id == Tenant.id)
            .where(Tenant.slug == GLOBAL_TENANT_SLUG, ToolConfig.tool_name == "mail_intake")
        )).scalar_one_or_none()
        if not tc:
            await _send_to_chat(chat_id, "Mail-Konfiguration fehlt - Betreiber kontaktieren.")
            return
        cfg = tc.config or {}
        brevo_api_key = cfg.get("brevo_api_key")
        sender_email = cfg.get("sender_email")
        sender_name = cfg.get("sender_name")

    if not all([brevo_api_key, sender_email, sender_name]):
        await _send_to_chat(chat_id, "Mail-Konfiguration unvollstaendig.")
        return

    # PDF von Lexware holen
    await _send_to_chat(chat_id, "<i>Lade PDF von Lexware...</i>")
    try:
        pdf_bytes = await provider.download_invoice_pdf(rg_data["lexware_invoice_id"])
    except AccountingError as e:
        if e.status_code == 409:
            deeplink = LexwareProvider.invoice_deeplink_view(rg_data["lexware_invoice_id"])
            await _send_to_chat(
                chat_id,
                "Rechnung ist in Lexware noch nicht finalisiert.\n\n"
                f'<a href="{deeplink}">In Lexware oeffnen + Finalisieren klicken</a>\n\n'
                "Danach hier wieder /rechnungen_anzeigen und Senden klicken.",
            )
        else:
            await _send_to_chat(chat_id, f"Lexware-Fehler beim PDF-Download (HTTP {e.status_code}).")
        return
    except Exception as e:
        logger.exception(f"download_invoice_pdf fehlgeschlagen: {e}")
        await _send_to_chat(chat_id, "PDF-Download fehlgeschlagen. Bitte spaeter erneut.")
        return

    if not pdf_bytes or len(pdf_bytes) < 100:
        await _send_to_chat(chat_id, "Lexware hat ein leeres PDF zurueckgegeben.")
        return

    # Mail-Body bauen
    rg_nummer_str = f" {rg_data['voucher_number']}" if rg_data["voucher_number"] else ""
    subject = f"Rechnung{rg_nummer_str} von {tenant_data['company_name']}"
    html_body = (
        f"<p>Sehr geehrte Damen und Herren,</p>"
        f"<p>vielen Dank fuer Ihren Auftrag.</p>"
        f"<p>Anbei finden Sie unsere Rechnung{rg_nummer_str} im Anhang als PDF.</p>"
        f"<p>Bei Fragen koennen Sie diese Mail einfach beantworten.</p>"
        f"<p>Mit freundlichen Gruessen<br>"
        f"{tenant_data['contact_name']}<br>"
        f"{tenant_data['company_name']}</p>"
    )

    # Send via Brevo
    from core.integrations.brevo import BrevoMailer, MailRecipient, MailAttachment, BrevoError

    mailer = BrevoMailer(api_key=brevo_api_key)
    pdf_filename = f"Rechnung{('_' + rg_data['voucher_number']) if rg_data['voucher_number'] else ''}.pdf"

    try:
        # Mail an Kunden
        result = await mailer.send(
            sender_email=sender_email,
            sender_name=tenant_data["company_name"],
            to=MailRecipient(email=rg_data["kunde_email"], name=rg_data["kunde_name"]),
            subject=subject,
            html_body=html_body,
            reply_to_email=tenant_data["contact_email"],
            reply_to_name=tenant_data["contact_name"],
            attachments=[MailAttachment(
                filename=pdf_filename,
                content_bytes=pdf_bytes,
                content_type="application/pdf",
            )],
        )
        logger.info(f"Rechnungs-Mail an Kunde gesendet: {result.get('messageId')}")

        # Kopie an Tenant
        try:
            copy_subject = f"[Kopie] Rechnung an {rg_data['kunde_email']} versendet"
            copy_body = (
                f"<p>Hallo {tenant_data['contact_name']},</p>"
                f"<p>zur Info: Du hast soeben folgende Rechnung an deinen Kunden geschickt:</p>"
                f"<ul>"
                f"<li>Empfaenger: {rg_data['kunde_email']}</li>"
                f"<li>Rechnung: {rg_data['voucher_number'] or '(noch keine Nummer)'}</li>"
                f"<li>Betrag: {rg_data['betrag']:.2f} € brutto</li>"
                f"</ul>"
                f"<p>Das PDF ist auch an diese Mail angehaengt.</p>"
                f"<p>Gewerbeagent</p>"
            )
            await mailer.send(
                sender_email=sender_email,
                sender_name="Gewerbeagent",
                to=MailRecipient(email=tenant_data["contact_email"], name=tenant_data["contact_name"]),
                subject=copy_subject,
                html_body=copy_body,
                attachments=[MailAttachment(
                    filename=pdf_filename,
                    content_bytes=pdf_bytes,
                    content_type="application/pdf",
                )],
            )
            logger.info(f"Rechnungs-Kopie an Tenant gesendet: {tenant_data['contact_email']}")
        except Exception as e:
            logger.warning(f"Tenant-Kopie fehlgeschlagen (nicht kritisch): {e}")

    except BrevoError as e:
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.status = RECHNUNG_STATUS_ERROR
                rg.error_message = f"Brevo: {str(e)[:400]}"
                await s.commit()
        await _clear_state(chat_id)
        await _send_to_chat(chat_id, f"Mailversand fehlgeschlagen (HTTP {e.status_code}). Bitte spaeter erneut.")
        return
    except Exception as e:
        logger.exception(f"Mailversand unerwartet fehlgeschlagen: {e}")
        await _clear_state(chat_id)
        await _send_to_chat(chat_id, "Mailversand fehlgeschlagen. Bitte spaeter erneut.")
        return

    # Status auf mail_sent
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung).where(Rechnung.id == rechnung_id)
        )).scalar_one_or_none()
        if rg:
            rg.status = RECHNUNG_STATUS_MAIL_SENT
            rg.mail_sent_to = rg_data["kunde_email"]
            rg.mail_sent_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()

    # Lexware-Kontakt mit Mail aktualisieren (Lern-Effekt)
    if rg_data["lexware_contact_id"]:
        try:
            await provider.update_contact_email(
                rg_data["lexware_contact_id"],
                rg_data["kunde_email"],
            )
        except Exception as e:
            logger.warning(f"Lexware update_contact_email fehlgeschlagen (nicht kritisch): {e}")

    await _clear_state(chat_id)
    msg = f"<b>Rechnung versendet.</b>\n\n"
    msg += f"\u2709️ An: {rg_data['kunde_email']}\n"
    msg += f"\u2709️ Kopie: {tenant_data['contact_email']}\n\n"
    msg += "Mit /rechnung kannst du die naechste anlegen."
    await _send_to_chat(chat_id, msg)



# =====================================================================
# Formular-Editor-Wizard ( /formular  /formular_anzeigen  /formular_zuruecksetzen )
#
# Erlaubt dem Tenant das Anfrage-Formular pro Anfrage-Typ zu pflegen.
# Pattern wie /leistung neu (siehe _handle_leistung_neu_command oben).
#
# Datenmodell: TenantAnfrageSchema (UNIQUE auf tenant_id + anfrage_typ).
# Snapshot-Strategie: kompletter Field-Array wird in fields-JSONB gespeichert.
# Wizard-Start klont die Hardcoded-Defaults wenn noch kein Schema existiert.
# =====================================================================

# Mapping fuer Typ-Auswahl im Wizard (Nummer -> Anfrage-Typ-Konstante)
_FORMULAR_TYP_LABEL = {
    ANFRAGE_TYP_TISCHLER: "Tischlerei (Schrank/Tisch/Massmoebel)",
    ANFRAGE_TYP_ALLGEMEIN: "Allgemeine Anfrage (alle Gewerke)",
}

# Field-Type-Auswahl im Wizard - Reihenfolge fixiert
_FORMULAR_FELDTYP_REIHE = [
    ("text", "Text (1 Zeile)"),
    ("textarea", "Mehrzeiliger Text"),
    ("tel", "Telefonnummer"),
    ("date", "Datum"),
    ("radio", "Auswahl (eine Option)"),
    ("checkbox_multi", "Mehrfachauswahl (Checkboxen)"),
    ("select", "Dropdown"),
    ("masse", "Masse Hoehe/Breite/Tiefe"),
]

# Welche Typen brauchen Optionen?
_FORMULAR_TYPEN_MIT_OPTIONEN = {"radio", "checkbox_multi", "select"}


def _formular_format_field_short(idx: int, f: dict) -> str:
    """Eine Zeile pro Feld fuer Listen/Vorschau."""
    typ = f.get("type", "?")
    label = f.get("label") or f.get("name", "?")
    pflicht = " *" if f.get("required") else ""
    opts_hint = ""
    if typ in _FORMULAR_TYPEN_MIT_OPTIONEN:
        n = len(f.get("options") or [])
        opts_hint = f" ({n} Optionen)"
    return f"{idx}. <b>{label}</b>{pflicht}\n   <i>{typ}{opts_hint}</i>"


def _formular_render_hauptmenu(fields: list[dict], anfrage_typ: str, dirty: bool = False) -> str:
    """Hauptmenue-Text mit Feld-Anzahl + Aktionen.

    dirty=True markiert ungespeicherte Aenderungen mit deutlichem Hinweis.
    """
    typ_label = _FORMULAR_TYP_LABEL.get(anfrage_typ, anfrage_typ)
    msg = f"<b>📋 Formular-Editor: {typ_label}</b>\n\n"
    msg += f"Aktuell <b>{len(fields)} Felder</b> im Formular.\n"
    if dirty:
        msg += "⚠️  <b>Aenderungen sind noch nicht gespeichert!</b>\n"
        msg += "    Erst mit <b>4</b> landet es im Web-Formular.\n"
    msg += "\nWas tun?\n"
    msg += "<b>1</b>) ➕ Feld hinzufuegen\n"
    msg += "<b>2</b>) ➖ Feld entfernen\n"
    msg += "<b>3</b>) 👁  Vorschau\n"
    msg += "<b>4</b>) ✅ Speichern (und im Web aktivieren)\n"
    msg += "<b>5</b>) 🗑  Verwerfen\n\n"
    msg += "Bitte Nummer schicken oder /abbrechen."
    return msg


async def _formular_load_initial_fields(tenant_id, anfrage_typ: str) -> list[dict]:
    """Initial-Snapshot: vorhandenes DB-Schema oder Default-Klon."""
    from core.integrations.anfrage_forms import get_default_schema
    from sqlalchemy import select as _sel
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _sel(TenantAnfrageSchema).where(
                TenantAnfrageSchema.tenant_id == tenant_id,
                TenantAnfrageSchema.anfrage_typ == anfrage_typ,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None and row.fields:
            # Deep copy via list+dict comprehension
            return [dict(f) for f in row.fields]
    default = get_default_schema(anfrage_typ)
    return [dict(f) for f in (default.get("fields") or [])]


async def _handle_formular_command(chat_id):
    """Einstieg: /formular - fragt nach Anfrage-Typ."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    await _save_state(chat_id, STATE_FORMULAR_TYP_WAEHLEN, {})
    msg = "<b>📋 Anfrage-Formular bearbeiten</b>\n\n"
    msg += "Welches Formular willst du anpassen?\n\n"
    msg += f"<b>1</b>) {_FORMULAR_TYP_LABEL[ANFRAGE_TYP_TISCHLER]}\n"
    msg += f"<b>2</b>) {_FORMULAR_TYP_LABEL[ANFRAGE_TYP_ALLGEMEIN]}\n\n"
    msg += "Bitte Nummer schicken oder /abbrechen."
    return msg


async def _handle_formular_typ_input(chat_id, text: str):
    """Schritt 0: Anfrage-Typ-Auswahl."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Dieser Chat ist keinem Betrieb zugeordnet."

    t = (text or "").strip()
    if t == "1":
        anfrage_typ = ANFRAGE_TYP_TISCHLER
    elif t == "2":
        anfrage_typ = ANFRAGE_TYP_ALLGEMEIN
    else:
        return "Bitte <b>1</b> oder <b>2</b> schicken oder /abbrechen."

    fields = await _formular_load_initial_fields(tenant.id, anfrage_typ)
    state_data = {"anfrage_typ": anfrage_typ, "fields": fields}
    await _save_state(chat_id, STATE_FORMULAR_HAUPTMENU, state_data)
    return _formular_render_hauptmenu(fields, anfrage_typ)


async def _handle_formular_hauptmenu_input(chat_id, text: str, state_data: dict):
    """Hauptmenue: 1=neu, 2=entfernen, 3=vorschau, 4=speichern, 5=verwerfen."""
    if not state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    t = (text or "").strip()
    fields = state_data.get("fields") or []
    anfrage_typ = state_data.get("anfrage_typ") or ANFRAGE_TYP_ALLGEMEIN

    if t == "1":
        # Neues Feld - Schritt 1: Name
        await _save_state(chat_id, STATE_FORMULAR_NEU_NAME, state_data)
        msg = "<b>➕ Neues Feld</b>\n\n"
        msg += "Wie soll das Feld <i>technisch</i> heissen? "
        msg += "(Kleinbuchstaben, Zahlen, Unterstriche; max 30 Zeichen)\n\n"
        msg += "<b>Beispiele:</b>\n"
        msg += "• <i>lieferadresse</i>\n"
        msg += "• <i>wandfarbe</i>\n"
        msg += "• <i>raumgroesse</i>\n\n"
        msg += "Oder /abbrechen."
        return msg

    if t == "2":
        if not fields:
            return _formular_render_hauptmenu(fields, anfrage_typ) + "\n\nKein Feld zum Loeschen vorhanden."
        await _save_state(chat_id, STATE_FORMULAR_LOESCHEN, state_data)
        msg = "<b>➖ Feld entfernen</b>\n\nWelches Feld soll raus? Nummer schicken:\n\n"
        for i, f in enumerate(fields, 1):
            msg += _formular_format_field_short(i, f) + "\n"
        msg += "\nOder /abbrechen."
        return msg

    if t == "3":
        # Vorschau
        if not fields:
            preview = "<i>(noch keine Felder)</i>"
        else:
            preview = "\n".join(_formular_format_field_short(i, f) for i, f in enumerate(fields, 1))
        msg = "<b>👁 Vorschau</b>\n\n" + preview + "\n\n"
        if state_data.get("dirty"):
            msg += "⚠️  <b>Noch nicht gespeichert!</b> Erst mit <b>4</b> landet das im Web-Formular.\n"
        msg += "Mit <b>1</b>=hinzufuegen, <b>2</b>=entfernen, <b>4</b>=speichern, <b>5</b>=verwerfen weiter."
        return msg

    if t == "4":
        # Speichern via Inline-Buttons (Bestaetigung + sicherer Save-Pfad)
        from core.integrations.anfrage_forms import validate_schema_fields
        ok, err = validate_schema_fields(fields)
        if not ok:
            return f"Schema ist nicht gueltig: {err}\n\nBitte Felder korrigieren und nochmal speichern."

        msg = f"<b>✅ Speichern?</b>\n\n{len(fields)} Felder werden uebernommen."
        keyboard = [[
            {"text": "✅ Speichern", "callback_data": "formular:save"},
            {"text": "❌ Verwerfen", "callback_data": "formular:cancel"},
        ]]
        await _send_with_inline_buttons(chat_id, msg, keyboard)
        return None

    if t == "5":
        await _clear_state(chat_id)
        return "🗑 Aenderungen verworfen. Bisheriges Formular bleibt aktiv."

    return "Bitte <b>1</b>-<b>5</b> schicken oder /abbrechen."


async def _handle_formular_neu_name_input(chat_id, text: str, state_data: dict):
    """Schritt 1: technischer Feldname."""
    import re
    if not state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    name = (text or "").strip().lower()
    if not name or len(name) < 2:
        return "Name zu kurz. Bitte mind. 2 Zeichen oder /abbrechen."
    if len(name) > 30:
        return "Name zu lang (max 30 Zeichen). Bitte kuerzer."
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return "Nur Kleinbuchstaben, Zahlen, Unterstriche; muss mit Buchstabe anfangen."
    if name in {"name", "email", "token"}:
        return f"'{name}' ist reserviert. Bitte einen anderen Namen waehlen."

    existing_names = {(f.get("name") or "").lower() for f in (state_data.get("fields") or [])}
    if name in existing_names:
        return f"'{name}' ist schon vergeben. Bitte einen anderen Namen waehlen."

    pending = {"name": name}
    state_data["pending_field"] = pending
    await _save_state(chat_id, STATE_FORMULAR_NEU_LABEL, state_data)
    msg = f"<b>{name}</b> — was soll als <i>Anzeige-Label</i> im Formular stehen?\n\n"
    msg += "<b>Beispiele:</b>\n"
    msg += "• <i>An welche Adresse soll geliefert werden?</i>\n"
    msg += "• <i>Welche Wandfarbe haetten Sie gerne?</i>\n\n"
    msg += "Oder /abbrechen."
    return msg


async def _handle_formular_neu_label_input(chat_id, text: str, state_data: dict):
    """Schritt 2: Anzeige-Label."""
    if not state_data or "pending_field" not in state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    label = (text or "").strip()
    if not label or len(label) < 2:
        return "Label zu kurz. Bitte mind. 2 Zeichen oder /abbrechen."
    if len(label) > 200:
        return "Label zu lang (max 200 Zeichen). Bitte kuerzer."

    state_data["pending_field"]["label"] = label
    await _save_state(chat_id, STATE_FORMULAR_NEU_TYP, state_data)

    msg = f"<b>{label}</b>\n\nWelcher Feld-Typ?\n\n"
    for i, (_, lab) in enumerate(_FORMULAR_FELDTYP_REIHE, 1):
        msg += f"<b>{i}</b>) {lab}\n"
    msg += "\nBitte Nummer schicken oder /abbrechen."
    return msg


async def _handle_formular_neu_typ_input(chat_id, text: str, state_data: dict):
    """Schritt 3: Feld-Typ-Auswahl 1-8."""
    if not state_data or "pending_field" not in state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    t = (text or "").strip()
    if not t.isdigit() or not (1 <= int(t) <= len(_FORMULAR_FELDTYP_REIHE)):
        return f"Bitte eine Zahl von 1 bis {len(_FORMULAR_FELDTYP_REIHE)} schicken oder /abbrechen."

    typ_key, _ = _FORMULAR_FELDTYP_REIHE[int(t) - 1]
    state_data["pending_field"]["type"] = typ_key

    if typ_key in _FORMULAR_TYPEN_MIT_OPTIONEN:
        await _save_state(chat_id, STATE_FORMULAR_NEU_OPTIONEN, state_data)
        msg = f"<b>Optionen fuer '{state_data['pending_field']['label']}'</b>\n\n"
        msg += "Bitte alle Auswahlmoeglichkeiten als <b>Komma-getrennte Liste</b> schicken.\n\n"
        msg += "<b>Beispiel:</b> <i>Eiche, Buche, Nussbaum, Lackiert</i>\n\n"
        msg += "Mindestens 2 Optionen, max 12. Oder /abbrechen."
        return msg

    # Kein Optionen-Schritt - direkt zu Required
    await _save_state(chat_id, STATE_FORMULAR_NEU_REQUIRED, state_data)
    return (
        f"Soll das Feld ein <b>Pflichtfeld</b> sein?\n\n"
        f"<b>ja</b> oder <b>nein</b> schicken (oder /abbrechen)."
    )


async def _handle_formular_neu_optionen_input(chat_id, text: str, state_data: dict):
    """Schritt 3.5: Optionen fuer radio/checkbox/select."""
    if not state_data or "pending_field" not in state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    raw = (text or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        return "Mindestens 2 Optionen. Bitte komma-getrennt nochmal schicken oder /abbrechen."
    if len(parts) > 12:
        return "Max 12 Optionen. Bitte kuerzen oder /abbrechen."
    if any(len(p) > 80 for p in parts):
        return "Eine Option ist laenger als 80 Zeichen. Bitte kuerzen."

    state_data["pending_field"]["options"] = parts
    await _save_state(chat_id, STATE_FORMULAR_NEU_REQUIRED, state_data)
    return (
        f"Optionen erfasst ({len(parts)}). Soll das Feld ein <b>Pflichtfeld</b> sein?\n\n"
        f"<b>ja</b> oder <b>nein</b> schicken (oder /abbrechen)."
    )


async def _handle_formular_neu_required_input(chat_id, text: str, state_data: dict):
    """Schritt 4 (final): Pflichtfeld ja/nein, dann Feld dem Snapshot anhaengen."""
    if not state_data or "pending_field" not in state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    t = (text or "").strip().lower()
    if t in ("ja", "j", "y", "yes"):
        required = True
    elif t in ("nein", "n", "no"):
        required = False
    else:
        return "Bitte <b>ja</b> oder <b>nein</b> schicken oder /abbrechen."

    pending = state_data["pending_field"]
    pending["required"] = required

    fields = state_data.get("fields") or []
    fields.append(pending)
    state_data["fields"] = fields
    state_data["dirty"] = True
    state_data.pop("pending_field", None)

    anfrage_typ = state_data.get("anfrage_typ") or ANFRAGE_TYP_ALLGEMEIN
    await _save_state(chat_id, STATE_FORMULAR_HAUPTMENU, state_data)

    msg = (
        f"✅ <b>{pending['label']}</b> in Bearbeitungspuffer aufgenommen.\n"
        f"<i>(noch nicht im Web aktiv – tippe <b>4</b> zum Speichern)</i>\n\n"
    )
    msg += _formular_render_hauptmenu(fields, anfrage_typ, dirty=True)
    return msg


async def _handle_formular_loeschen_input(chat_id, text: str, state_data: dict):
    """Loescht das Feld an Position N (1-basiert) aus dem Snapshot."""
    if not state_data:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /formular erneut starten."

    fields = state_data.get("fields") or []
    anfrage_typ = state_data.get("anfrage_typ") or ANFRAGE_TYP_ALLGEMEIN
    t = (text or "").strip()
    if not t.isdigit():
        return "Bitte eine Zahl schicken oder /abbrechen."
    idx = int(t)
    if idx < 1 or idx > len(fields):
        return f"Index ausserhalb. Bitte 1 bis {len(fields)} schicken."

    removed = fields.pop(idx - 1)
    state_data["fields"] = fields
    state_data["dirty"] = True
    await _save_state(chat_id, STATE_FORMULAR_HAUPTMENU, state_data)
    msg = (
        f"➖ <b>{removed.get('label', removed.get('name', 'Feld'))}</b> aus Bearbeitungspuffer entfernt.\n"
        f"<i>(noch nicht im Web aktiv – tippe <b>4</b> zum Speichern)</i>\n\n"
    )
    msg += _formular_render_hauptmenu(fields, anfrage_typ, dirty=True)
    return msg


async def _handle_formular_anzeigen_command(chat_id):
    """/formular_anzeigen - Read-only-View des aktiven Schemas (DB oder Default)."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    from core.integrations.anfrage_forms import get_schema_for_tenant
    msg = "<b>📋 Aktive Formulare</b>\n\n"
    for typ in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
        schema = await get_schema_for_tenant(tenant.id, typ)
        flds = schema.get("fields") or []
        msg += f"<b>{_FORMULAR_TYP_LABEL[typ]}</b> ({len(flds)} Felder)\n"
        for i, f in enumerate(flds, 1):
            msg += _formular_format_field_short(i, f) + "\n"
        msg += "\n"
    msg += "Mit /formular kannst du Felder anpassen."
    return msg


async def _handle_formular_zuruecksetzen_command(chat_id):
    """/formular_zuruecksetzen - Bestaetigung via Inline-Buttons."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    await _save_state(chat_id, STATE_FORMULAR_RESET_CONFIRM, {})
    msg = "<b>🗑 Formular zuruecksetzen?</b>\n\n"
    msg += "Setzt <i>beide</i> Anfrage-Typen (Tischlerei + Allgemein) auf die "
    msg += "Standard-Felder zurueck. Eigene Anpassungen gehen verloren.\n\n"
    msg += "Welche moechtest du zuruecksetzen?"
    keyboard = [
        [{"text": "Tischlerei", "callback_data": f"formular:reset:{ANFRAGE_TYP_TISCHLER}"}],
        [{"text": "Allgemein", "callback_data": f"formular:reset:{ANFRAGE_TYP_ALLGEMEIN}"}],
        [{"text": "Beide", "callback_data": "formular:reset:both"}],
        [{"text": "❌ Abbrechen", "callback_data": "formular:reset_cancel"}],
    ]
    await _send_with_inline_buttons(chat_id, msg, keyboard)
    return None


async def _handle_formular_callback(chat_id, callback_data, callback_query_id, bot_token):
    """Inline-Button-Dispatcher fuer save/cancel und reset:*."""
    parts = callback_data.split(":")
    if len(parts) < 2:
        await _answer_callback_query(callback_query_id, "Ungueltig", bot_token)
        return
    action = parts[1]

    if action == "cancel":
        await _clear_state(chat_id)
        await _answer_callback_query(callback_query_id, "Verworfen", bot_token)
        await _send_to_chat(chat_id, "🗑 Aenderungen verworfen.")
        return

    if action == "reset_cancel":
        await _clear_state(chat_id)
        await _answer_callback_query(callback_query_id, "Abgebrochen", bot_token)
        await _send_to_chat(chat_id, "🗑 Reset abgebrochen.")
        return

    if action == "save":
        state = await _load_state(chat_id)
        if not state or not state.state_data:
            await _answer_callback_query(callback_query_id, "Session abgelaufen", bot_token)
            await _clear_state(chat_id)
            return
        tenant = await _get_tenant_by_chat(chat_id)
        if not tenant:
            await _answer_callback_query(callback_query_id, "Tenant fehlt", bot_token)
            return
        from core.integrations.anfrage_forms import upsert_tenant_schema
        anfrage_typ = state.state_data.get("anfrage_typ") or ANFRAGE_TYP_ALLGEMEIN
        fields = state.state_data.get("fields") or []
        ok, err = await upsert_tenant_schema(
            tenant_id=tenant.id,
            anfrage_typ=anfrage_typ,
            fields=fields,
        )
        await _clear_state(chat_id)
        if ok:
            await _answer_callback_query(callback_query_id, "Gespeichert!", bot_token)
            label = _FORMULAR_TYP_LABEL.get(anfrage_typ, anfrage_typ)
            await _send_to_chat(
                chat_id,
                f"✅ <b>Gespeichert.</b>\n\nFormular '{label}' hat jetzt {len(fields)} Felder.",
            )
        else:
            await _answer_callback_query(callback_query_id, "Fehler", bot_token)
            await _send_to_chat(chat_id, f"❌ Konnte nicht speichern: {err}")
        return

    if action == "reset":
        if len(parts) < 3:
            await _answer_callback_query(callback_query_id, "Ungueltig", bot_token)
            return
        target = parts[2]
        tenant = await _get_tenant_by_chat(chat_id)
        if not tenant:
            await _answer_callback_query(callback_query_id, "Tenant fehlt", bot_token)
            return
        from core.integrations.anfrage_forms import delete_tenant_schema
        if target == "both":
            n1 = await delete_tenant_schema(tenant.id, ANFRAGE_TYP_TISCHLER)
            n2 = await delete_tenant_schema(tenant.id, ANFRAGE_TYP_ALLGEMEIN)
            removed = int(n1) + int(n2)
        elif target in (ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN):
            removed = 1 if await delete_tenant_schema(tenant.id, target) else 0
        else:
            await _answer_callback_query(callback_query_id, "Ungueltig", bot_token)
            return
        await _clear_state(chat_id)
        await _answer_callback_query(callback_query_id, "Zurueckgesetzt", bot_token)
        if removed:
            await _send_to_chat(
                chat_id,
                f"✅ Zurueckgesetzt. Default-Felder werden wieder verwendet ({removed}× geloescht).",
            )
        else:
            await _send_to_chat(
                chat_id,
                "ℹ️ Es gab kein eigenes Schema - Defaults waren ohnehin aktiv.",
            )
        return

    await _answer_callback_query(callback_query_id, "Unbekannt", bot_token)


# =====================================================================
# Mitarbeiter-Wizard (Phase 4 Multi-Mitarbeiter)
# =====================================================================


def _slugify(name: str) -> str:
    """Erzeugt einen URL-/DB-kompatiblen Slug aus einem Namen.

    'Sven Müller' → 'sven-mueller'. Ohne Sonderzeichen, lowercase,
    Bindestrich-getrennt. Garantiert nicht-leer fuer nicht-leeren Input.
    """
    s = (name or "").strip().lower()
    # Umlaute
    s = (
        s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
         .replace("ß", "ss")
    )
    out = []
    last_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    result = "".join(out).strip("-")
    return result or "mitarbeiter"


async def _get_bot_username(bot_token: str | None = None) -> str | None:
    """Liefert den @-Username des Bots fuer Deep-Links."""
    if bot_token is None:
        bot_token = await _load_global_bot_token()
    if not bot_token:
        return None
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("ok"):
                return None
            return data["result"].get("username")
    except Exception as e:
        logger.warning(f"_get_bot_username failed: {e}")
        return None


def _format_skills(skills: list[str] | None) -> str:
    if not skills:
        return "—"
    return ", ".join(skills)


async def _format_mitarbeiter_list(tenant_id) -> str:
    """Liste aller Mitarbeiter eines Tenants."""
    from core.models import get_employees_for_tenant
    emps = await get_employees_for_tenant(tenant_id, active_only=False)
    if not emps:
        return "Noch keine Mitarbeiter angelegt."
    lines = ["<b>👥 Mitarbeiter</b>", ""]
    for e in emps:
        flag = " 👑" if e.is_default else ""
        active = "" if e.is_active else " <i>(deaktiviert)</i>"
        chat = "✅" if e.telegram_chat_id else "—"
        lines.append(
            f"• <b>{e.name}</b> ({e.slug}){flag}{active}\n"
            f"   Telegram: {chat}  Skills: {_format_skills(e.skills)}"
        )
    lines.append("")
    lines.append("<i>Details:</i> /mitarbeiter &lt;slug&gt;")
    lines.append("<i>Neu anlegen:</i> /mitarbeiter neu")
    return "\n".join(lines)


async def _format_mitarbeiter_detail(tenant_id, slug) -> str:
    """Detail-Anzeige eines Mitarbeiters."""
    from core.database import AsyncSessionLocal
    from core.models import Employee
    async with AsyncSessionLocal() as s:
        emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.slug == slug,
            )
        )).scalar_one_or_none()
        if emp is None:
            return f"Mitarbeiter <b>{slug}</b> nicht gefunden."
    bot_username = await _get_bot_username()
    deeplink = (
        f"https://t.me/{bot_username}?start={slug}"
        if bot_username and emp.is_default
        else (
            f"https://t.me/{bot_username}?start=__SLUG_PLACEHOLDER__"
            if bot_username else "(Bot-Username unbekannt)"
        )
    )
    flag = " 👑 Inhaber" if emp.is_default else ""
    active = "" if emp.is_active else " <b>(deaktiviert)</b>"
    chat_str = (
        f"verbunden (Chat-ID {emp.telegram_chat_id})"
        if emp.telegram_chat_id else "noch nicht verbunden"
    )
    heimat = (
        f"{emp.heimat_strasse}, {emp.heimat_plz} {emp.heimat_ort}"
        if (emp.heimat_strasse or emp.heimat_ort) else "—"
    )
    msg = (
        f"<b>{emp.name}</b>{flag}{active}\n"
        f"Slug: <code>{emp.slug}</code>\n"
        f"E-Mail: {emp.contact_email or '—'}\n"
        f"Telegram: {chat_str}\n"
        f"Heimat: {heimat}\n"
        f"Skills: {_format_skills(emp.skills)}\n"
    )
    return msg


async def _ensure_inhaber_or_explain(chat_id) -> tuple[bool, str | None, object | None, object | None]:
    """Gemeinsame Berechtigungs-Pruefung: nur Default-Employee = Inhaber.

    Return: (ok, error_message, tenant, employee)
    """
    res = await _get_current_employee(chat_id)
    if res is None:
        return False, (
            "Dieser Chat ist noch keinem Betrieb zugeordnet. "
            "Bitte zuerst /start ausfuehren."
        ), None, None
    tenant, emp = res
    if not emp.is_default:
        return False, (
            "Nur der Inhaber kann Mitarbeiter verwalten. "
            "Bitte den Inhaber bitten, dies fuer dich zu tun."
        ), tenant, emp
    return True, None, tenant, emp


async def _handle_mitarbeiter_command(chat_id, text):
    """Top-Level-Dispatcher fuer /mitarbeiter*.

    /mitarbeiter                 → Liste
    /mitarbeiter neu             → Wizard-Start (Inhaber-only)
    /mitarbeiter <slug>          → Detail
    /mitarbeiter <slug> deaktivieren / aktivieren
    /mitarbeiter <slug> skills <s1,s2,...>
    """
    res = await _get_current_employee(chat_id)
    if res is None:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet. "
            "Bitte zuerst /start ausfuehren."
        )
    tenant, current_emp = res
    args = text[len("/mitarbeiter"):].strip()

    # Liste
    if not args:
        return await _format_mitarbeiter_list(tenant.id)

    parts = args.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    # Neu-Wizard
    if sub == "neu":
        ok, err, _, _ = await _ensure_inhaber_or_explain(chat_id)
        if not ok:
            return err
        await _save_state(chat_id, STATE_MITARBEITER_NEU_NAME, {})
        return (
            "<b>Neuer Mitarbeiter anlegen</b>\n\n"
            "Schicke mir den Namen des Mitarbeiters (z.B. 'Sven Mueller')."
            "\n\nMit /abbrechen verwirfst du den Vorgang."
        )

    # Sub-Befehle auf einem konkreten Slug
    sub_slug = sub
    sub_args = rest.strip().lower() if rest else ""
    # Wenn nur slug → Detail (kein Inhaber-Check, alle duerfen lesen)
    if not sub_args:
        return await _format_mitarbeiter_detail(tenant.id, sub_slug)

    # Schreib-Operationen sind Inhaber-only
    ok, err, _, _ = await _ensure_inhaber_or_explain(chat_id)
    if not ok:
        return err

    from core.database import AsyncSessionLocal
    from core.models import Employee, ALLE_SKILLS
    async with AsyncSessionLocal() as s:
        emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id, Employee.slug == sub_slug,
            )
        )).scalar_one_or_none()
        if emp is None:
            return f"Mitarbeiter <b>{sub_slug}</b> nicht gefunden."

        if sub_args in ("deaktivieren", "deactivate", "off"):
            if emp.is_default:
                return "Der Inhaber-Account kann nicht deaktiviert werden."
            emp.is_active = False
            await s.commit()
            return f"✅ Mitarbeiter <b>{emp.name}</b> deaktiviert."
        if sub_args in ("aktivieren", "activate", "on"):
            emp.is_active = True
            await s.commit()
            return f"✅ Mitarbeiter <b>{emp.name}</b> aktiviert."
        if sub_args.startswith("skills "):
            raw = sub_args[len("skills "):].strip()
            new_skills = [
                t.strip().lower() for t in raw.split(",") if t.strip()
            ]
            unbekannt = [sk for sk in new_skills if sk not in ALLE_SKILLS]
            if unbekannt:
                return (
                    f"Unbekannte Skills: {', '.join(unbekannt)}.\n"
                    f"Erlaubt: {', '.join(ALLE_SKILLS)}."
                )
            emp.skills = new_skills or None
            await s.commit()
            return (
                f"✅ Skills fuer <b>{emp.name}</b> gesetzt: "
                f"{_format_skills(emp.skills)}"
            )

    return (
        f"Unbekannter Befehl: <code>/mitarbeiter {args}</code>\n\n"
        "Verfuegbar:\n"
        "• /mitarbeiter — Liste\n"
        "• /mitarbeiter neu — anlegen\n"
        "• /mitarbeiter &lt;slug&gt; — Details\n"
        "• /mitarbeiter &lt;slug&gt; aktivieren / deaktivieren\n"
        "• /mitarbeiter &lt;slug&gt; skills heizung,sanitaer"
    )


async def _handle_mitarbeiter_neu_name_input(chat_id, text):
    """Wizard Schritt 1: Name → Slug-Vorschlag → State weiter."""
    name = text.strip()
    if len(name) < 2:
        return "Bitte einen vollstaendigen Namen schicken (oder /abbrechen)."
    res = await _get_current_employee(chat_id)
    if res is None:
        await _clear_state(chat_id)
        return "Dieser Chat ist nicht zugeordnet."
    tenant, _ = res

    slug = _slugify(name)
    # Slug-Kollisions-Check + ggf. Suffix anhaengen
    from core.database import AsyncSessionLocal
    from core.models import Employee
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(Employee.slug).where(Employee.tenant_id == tenant.id)
        )).scalars().all()
    final_slug = slug
    suffix = 2
    while final_slug in existing:
        final_slug = f"{slug}-{suffix}"
        suffix += 1

    await _save_state(
        chat_id, STATE_MITARBEITER_NEU_SKILLS,
        {"name": name, "slug": final_slug},
    )
    from core.models import ALLE_SKILLS
    return (
        f"Slug wird <code>{final_slug}</code>.\n\n"
        "<b>Welche Skills hat der Mitarbeiter?</b>\n"
        f"Erlaubt: {', '.join(ALLE_SKILLS)}\n\n"
        "Mehrere mit Komma trennen (z.B. <code>heizung, sanitaer</code>),\n"
        "oder <b>keine</b> tippen wenn keine Spezialisierung."
    )


async def _handle_mitarbeiter_neu_skills_input(chat_id, text, state_data):
    """Wizard Schritt 2: Skills setzen, Employee anlegen, Deeplink ausgeben."""
    res = await _get_current_employee(chat_id)
    if res is None:
        await _clear_state(chat_id)
        return "Dieser Chat ist nicht zugeordnet."
    tenant, _ = res

    raw = (text or "").strip().lower()
    if raw in ("keine", "none", "-"):
        new_skills = None
    else:
        new_skills = [t.strip() for t in raw.split(",") if t.strip()]
        from core.models import ALLE_SKILLS
        unbekannt = [sk for sk in new_skills if sk not in ALLE_SKILLS]
        if unbekannt:
            return (
                f"Unbekannte Skills: {', '.join(unbekannt)}.\n"
                f"Erlaubt: {', '.join(ALLE_SKILLS)}.\n\n"
                "Bitte korrigieren oder <b>keine</b> tippen."
            )

    name = state_data.get("name", "")
    slug = state_data.get("slug", "")
    if not name or not slug:
        await _clear_state(chat_id)
        return "Wizard-Daten verloren — bitte mit /mitarbeiter neu erneut starten."

    # Anlegen
    from core.database import AsyncSessionLocal
    from core.models import Employee
    async with AsyncSessionLocal() as s:
        emp = Employee(
            tenant_id=tenant.id,
            slug=slug,
            name=name,
            is_default=False,
            is_active=True,
            skills=new_skills,
        )
        s.add(emp)
        try:
            await s.commit()
        except Exception as e:
            await s.rollback()
            await _clear_state(chat_id)
            logger.exception(f"Employee-Insert fehlgeschlagen: {e}")
            return f"Anlegen fehlgeschlagen: {e}"
        await s.refresh(emp)

    await _clear_state(chat_id)

    bot_username = await _get_bot_username()
    deeplink = (
        f"https://t.me/{bot_username}?start={tenant.slug}__{slug}"
        if bot_username else "(Bot-Username unbekannt — pruefe Bot-Konfig)"
    )
    return (
        f"✅ Mitarbeiter <b>{name}</b> angelegt (Slug <code>{slug}</code>).\n\n"
        f"<b>Telegram-Onboarding-Link</b> an den Mitarbeiter weiterleiten:\n"
        f"<code>{deeplink}</code>\n\n"
        "Sobald er den Link oeffnet + /start drueckt, wird er als "
        f"<b>{name}</b> mit dem Bot verbunden.\n\n"
        "Spaeter kann er optional <i>/werkstatt</i> ausfuehren um seine "
        "eigene Heimat-Adresse zu setzen (fuer Smart-Termin-Routing)."
    )


# =====================================================================
# Plugin-Klasse (Webhook-Einstieg)
# =====================================================================


class Plugin(BasePlugin):
    manifest = MANIFEST

    async def on_webhook(self, endpoint, payload, headers=None):
        # Signature-Verifikation: Telegram setzt secret_token im Header
        # 'X-Telegram-Bot-Api-Secret-Token' wenn beim setWebhook-Call ein
        # Secret konfiguriert wurde. Ohne Verifikation kann jeder mit
        # gefakten Updates Befehle ausloesen (/werkstatt-Adresse aendern,
        # Mitarbeiter anlegen, Termine buchen).
        from config.settings import settings
        expected = (settings.telegram_webhook_secret or "").strip()
        if expected:
            got = (headers or {}).get("x-telegram-bot-api-secret-token", "")
            # Constant-Time-Vergleich gegen Timing-Attacks
            import hmac
            if not hmac.compare_digest(got, expected):
                raise PermissionError("invalid-telegram-secret")
        # Wenn KEIN Secret gesetzt ist: Webhook ist offen (Backward-Compat).
        # Das wird in STATUS.md/Doku als deployment-blocker markiert.
        logger.info(f"Telegram-Webhook empfangen: endpoint={endpoint}")
        if endpoint == "incoming":
            return await process_telegram_update(payload)
        return {"ok": True, "note": f"unknown endpoint: {endpoint}"}
