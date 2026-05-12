"""
Telegram-Plugin: Push-Notifications + Empfang von Telegram-Updates.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    ALLE_KATEGORIEN,
    KATEGORIE_LABELS,
    TenantKalkulation,
    ALLE_KALK_KATEGORIEN,
    KALK_KATEGORIE_LABELS,
    KALK_SOURCE_MANUAL,
    KALK_SOURCE_EXCEL,
    STATE_KALK_KATEGORIE,
    STATE_KALK_NAME,
    STATE_KALK_FORMEL,
    STATE_KALK_EINHEIT,
    STATE_KALK_BESCHREIBUNG,
    STATE_KALK_LOESCHEN,
    STATE_KALK_EXCEL_WAITING,
    STATE_KALK_EXCEL_CONFIRM,
    STATE_BELEG_CONFIRMING,
    STATE_BELEG_WAITING_PHOTO,
    STATE_LEXWARE_SETUP_TOKEN,
    STATE_VIZ_WAITING_DESCRIPTION,
    STATE_VIZ_WAITING_KUNDE,
    STATE_VIZ_WAITING_PHOTO,
    STATE_VIZ_POST_ACTION,
    STATE_VIZ_POST_MAIL_EMAIL,
    STATE_VIZ_POST_DRIVE_KUNDE,
    STATE_WISSEN_KATEGORIE,
    STATE_WISSEN_LOESCHEN,
    STATE_WISSEN_TEXT,
    STATE_RECHNUNG_WAITING_INPUT,
    STATE_RECHNUNG_CONFIRMING,
    STATE_RECHNUNG_AWAITING_MAIL,
    STATE_AUFNAHME_WAITING_AUDIO,
    STATE_AUFNAHME_PREVIEWING,
    STATE_ANGEBOT_WAITING_INPUT,
    STATE_ANGEBOT_PREVIEWING,
    STATE_ANGEBOT_AWAITING_INSTRUCTIONS,
    STATE_ANGEBOT_AWAITING_MAIL,
    STATE_ANGEBOT_AWAITING_KUNDE_NAME,
    STATE_MATERIAL_NEU_NAME,
    STATE_MATERIAL_NEU_LINK,
    STATE_MATERIAL_NEU_LIEFERANT,
    STATE_MATERIAL_NEU_PREVIEWING,
    STATE_BESTELLEN_MENGE,
    STATE_ARCHIV_WAITING_FILES,
    STATE_LEISTUNG_WAITING_NAME,
    STATE_LEISTUNG_WAITING_PREIS,
    STATE_LEISTUNG_WAITING_BESCHREIBUNG,
    STATE_LEISTUNG_PREVIEWING,
    STATE_WERKSTATT_WAITING_ADDRESS,
    STATE_WERKSTATT_CONFIRMING,
    STATE_MITARBEITER_NEU_NAME,
    STATE_MITARBEITER_NEU_SKILLS,
    STATE_KALENDER_PROVIDER_CHOICE,
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
    RECHNUNG_STATUS_MAIL_QUEUED,
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
from core.models.angebot import (
    Angebot,
    ANGEBOT_STATUS_ERSTELLT,
    ANGEBOT_STATUS_IN_LEXWARE,
    ANGEBOT_STATUS_MAIL_SENT,
    ANGEBOT_STATUS_MAIL_QUEUED,
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
    ANGEBOT_STATUS_ABGEBROCHEN,
    AUFTRAG_LIFECYCLE,
    AUFTRAG_LIFECYCLE_LABELS,
)
from core.models.angebot_position import AngebotPosition
from core.security import decrypt, encrypt
from core.ai import (
    extract_rechnung_from_audio,
    extract_rechnung_from_text,
    analyse_kundengespraech_from_audio,
)
from core.ai.gemini import (
    extract_angebot_from_text,
    extract_angebot_from_audio,
    generate_angebot_anschreiben,
    generate_angebot_anschreiben_from_audio,
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

# Telegram limitiert eine Bot-Message auf 4096 Zeichen. Wir nehmen 3900
# als sichere Schwelle (Reserve fuer Markup-Overhead).
TELEGRAM_MAX_MESSAGE_LEN = 3900


def _split_message_safely(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Teilt eine zu lange Message in mehrere Stuecke.

    Splittet bevorzugt an Block-Grenzen (\\n\\n), Fallback auf Zeilen-
    Grenzen, Worst-Case auf max_len-Bytes. Bewusst NICHT mitten in
    HTML-Tags — wir splitten nur an Whitespace damit `<b>...</b>`
    geschlossen bleiben (wir haengen Tags nicht ueber Splits).
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len  # hart abschneiden — sollte praktisch nie passieren
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_to_chat(chat_id, text, bot_token=None):
    """Sendet Text an einen Telegram-Chat. Bei > 3900 Zeichen wird
    automatisch in mehrere Messages aufgeteilt — sonst antwortet die
    Bot-API mit HTTP 400 "message is too long"."""
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    text_str = str(text)
    chunks = _split_message_safely(text_str)
    if len(chunks) == 1:
        return await TelegramNotifier._send_raw(bot_token, str(chat_id), chunks[0])
    ok_all = True
    for chunk in chunks:
        ok = await TelegramNotifier._send_raw(bot_token, str(chat_id), chunk)
        ok_all = ok_all and bool(ok)
    return ok_all

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
        # Bug 2026-05-12: ein Inhaber kann sich mit derselben Telegram-App
        # erst als Default-Employee aktivieren und danach als neu angelegter
        # Mitarbeiter — `uq_emp_telegram_chat_id` knallt sonst beim Commit.
        # Loesung: alle FREMDEN Employees mit dieser chat_id (im selben oder
        # einem anderen Tenant) entkoppeln, bevor wir uns selbst zuweisen.
        if emp.telegram_chat_id != chat_id:
            stale = (await s.execute(
                select(Employee).where(
                    Employee.telegram_chat_id == chat_id,
                    Employee.id != emp.id,
                )
            )).scalars().all()
            for old in stale:
                logger.info(
                    f"Loese alte chat_id-Bindung an Employee {old.id} "
                    f"(slug={old.slug}, tenant={old.tenant_id}) bevor "
                    f"sie an {emp.slug} uebergeht"
                )
                old.telegram_chat_id = None
            if stale:
                # Vor dem Reassign muss die NULL-Zuweisung in der DB
                # ankommen, sonst greift uq_emp_telegram_chat_id immer noch.
                await s.flush()
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
        # Empfohlener naechster Schritt: Kalender + Heimat-Adresse einrichten
        reply += (
            "<b>Naechster Schritt:</b>\n"
            "  • /kalender_verbinden — Google- oder Outlook-Kalender verknuepfen\n"
            "  • /werkstatt — Heimat-Adresse fuer Smart-Termine setzen\n\n"
        )
        reply += "Mit /help sehen Sie alle verfuegbaren Befehle."
        return reply

async def _handle_help_command(chat_id=None):
    """Zeigt die Befehlsliste — gefiltert nach aktiven Features.

    Layout: pro Befehl eine Zeile mit Kurzbeschreibung. Vollstaendig
    (alle dispatchbaren Commands sind drin) aber minimalistisch
    (1 Satz pro Befehl, keine Wall-of-Text).

    Wenn der Tenant z.B. das Drive-Feature nicht hat, blendet /help den
    KUNDEN-ARCHIV-Block komplett aus. Tenants im Basis-Paket sehen
    keinen Lexware-, Material- oder Visualisierungs-Block.

    chat_id=None = Fallback (zeigt alles, fuer Test/Doku).
    """
    # Aktive Features fuer diesen Tenant ermitteln. Wenn der Chat noch
    # keinem Tenant zugeordnet ist (z.B. Erstkontakt vor /start),
    # zeigen wir nur die immer-verfuegbaren Befehle.
    enabled_features: frozenset[str] | None = None
    if chat_id is not None:
        tenant = await _get_tenant_by_chat(chat_id)
        if tenant is not None:
            from core.features import enabled_features_for_tenant
            try:
                enabled_features = await enabled_features_for_tenant(tenant.id)
            except Exception as e:
                logger.warning(f"_handle_help_command: feature-load failed: {e}")
                enabled_features = None

    def _is_on(feature_key: str) -> bool:
        # Bei fehlendem Tenant-Mapping -> alles anzeigen (Default-Hilfe)
        if enabled_features is None:
            return True
        return feature_key in enabled_features

    lines: list[str] = ["<b>📋 Befehle</b>"]
    locked_block_count = 0  # fuer kontext-sensitiven Footer-Hinweis

    def _block(
        emoji: str, title: str, items: list[tuple[str, str]],
        *, locked: bool = False,
    ) -> None:
        """items = [(cmd, kurzbeschreibung), ...]. Eine Zeile pro Eintrag.

        Wenn locked=True: Block-Header bekommt 🔒-Suffix, Befehle
        bleiben aber sichtbar mit Beschreibung — der User soll wissen
        was es gibt. Beim Tippen eines locked Befehls greift dann das
        Feature-Gate mit Upgrade-Hinweis.

        Wichtig: KEIN <code>-Wrap um den Befehl — Telegram macht
        Slash-Commands sonst nicht klickbar. Wir nehmen <b> fuer
        visuelle Unterscheidung. Argument-Hint (z.B. '[name]') steht
        ausserhalb von <b>, sonst hebt der Bold-Block die Argumente
        ein und Telegram interpretiert nur das erste Wort als Command
        — wir wollen aber dass der Link das ganze '/cmd' umfasst.
        """
        nonlocal locked_block_count
        if not items:
            return
        lines.append("")
        lock_marker = "  🔒" if locked else ""
        if locked:
            locked_block_count += 1
        lines.append(f"{emoji} <b>{title}</b>{lock_marker}")
        for cmd, desc in items:
            # Command + Args trennen damit nur der echte Slash-Command
            # in <b> steht und Telegram ihn als clickable parsed.
            cmd_word, _, cmd_args = cmd.partition(" ")
            cmd_args_html = f" {cmd_args}" if cmd_args else ""
            lines.append(f"<b>{cmd_word}</b>{cmd_args_html} — {desc}")

    # --- Workflow: Kundengespraeche (voice + kalender + always-on kunde) ---
    # Voice-Init-Befehle haben den expliziten Angebot-Hinweis — wir
    # zeigen sie auch wenn das Feature nicht aktiv ist, damit der User
    # sieht dass die Funktionalitaet existiert (mit 🔒-Marker).
    kunden_items_active: list[tuple[str, str]] = []
    kunden_items_locked: list[tuple[str, str]] = []

    voice_items = [
        ("/aufnahme",
         "Sprachnachricht zum Kundengespraech schicken — Bot "
         "transkribiert, extrahiert Kunde + Anliegen und legt "
         "optional ein Lexware-Angebot mit Preisen direkt an."),
        ("/anrufe",
         "Letzte eingehende Anrufe mit KI-Zusammenfassung und "
         "erkannten Kunden-Daten."),
    ]
    if _is_on("voice_init"):
        kunden_items_active.extend(voice_items)
    else:
        kunden_items_locked.extend(voice_items)

    if _is_on("kalender"):
        kunden_items_active.append((
            "/briefing",
            "Heutige Termine als Liste mit Uhrzeit + Kunde. Tap auf "
            "den /briefing_xxxx-Befehl pro Eintrag zeigt Briefing, "
            "TODOs, Notizen und den Drive-Ordner.",
        ))
    # /kunde ist always_on (kunde_lookup-Feature)
    kunden_items_active.append((
        "/kunde [name | email]",
        "Kundensuche: voller Name (z.B. <i>Anna Mueller</i>) oder "
        "Mail-Adresse (<i>anna@example.com</i>). Zeigt Gespraeche, "
        "Angebote, Lexware-Kontakte und Drive-Ordner.",
    ))
    _block("📞", "Kundengespraeche", kunden_items_active)
    if kunden_items_locked:
        _block(
            "📞", "Kundengespraeche — Telefon-Annahme",
            kunden_items_locked, locked=True,
        )

    # --- Buchhaltung (lexware) ---
    if _is_on("lexware"):
        _block("💰", "Buchhaltung — Belege & Rechnungen", [
            ("/angebot",
             "Angebot per Text oder Sprache diktieren — Bot wendet "
             "deine /kalkulation-Formeln an, legt das Angebot in Lexware "
             "an, schickt PDF per Mail an den Kunden und bereitet eine "
             "passende Rechnung als Lexware-Draft vor."),
            ("/auftraege",
             "Uebersicht laufender Projekte — pro Auftrag siehst du den "
             "Lifecycle (Angenommen, Arbeit laeuft, Fertig). Bei 🏁 Fertig "
             "wird die Rechnung automatisch finalisiert und per Mail "
             "rausgeschickt."),
            ("/beleg",
             "Beleg-Foto oder PDF schicken — wird als Buchungsbeleg "
             "in Lexware angelegt."),
            ("/belege_anzeigen",
             "Letzte 10 Belege mit Datum, Betrag und Sync-Status."),
            ("/rechnung",
             "Rechnung per Text oder Sprache diktieren — Bot baut "
             "einen Lexware-Draft zum Abnicken."),
            ("/rechnungen_anzeigen",
             "Offene + bezahlte Rechnungen mit Bezahl-Status."),
            ("/rechnung_pruefen",
             "Bezahl-Status der offenen Rechnungen sofort gegen "
             "Lexware abgleichen."),
            ("/leistungen",
             "Leistungskatalog (Stundensaetze, Pauschalen, Pakete) "
             "anzeigen."),
            ("/leistung [name]",
             "Detail einer Leistung: Preis, Einheit, Beschreibung."),
            ("/leistung_neu",
             "Neue Leistung anlegen (Wizard: Name → Preis → "
             "Beschreibung)."),
            ("/leistung_loeschen [name]",
             "Leistung aus dem Katalog entfernen."),
            ("/lexware_setup",
             "Lexware-API-Token hinterlegen (einmalig pro Tenant)."),
            ("/lexware_status",
             "Lexware-Verbindung + letzte Sync-Zeit pruefen."),
        ])

    # --- Material ---
    if _is_on("material"):
        _block("🛒", "Material-Bestellungen", [
            ("/material",
             "Verbrauchsmaterial-Katalog mit Inline-Buttons fuer "
             "Schnellbestellung."),
            ("/material [name]",
             "Artikel-Detail: Lieferant, Preis, Bestell-Link."),
            ("/material_neu",
             "Neuen Artikel anlegen (Wizard: Name → Lieferant → "
             "Link)."),
            ("/bestellen [name]",
             "Quick-Order mit Mengen-Eingabe; Bestellung wird "
             "gespeichert."),
            ("/bestellungen",
             "Letzte Bestellungen mit Datum und Status."),
        ])

    # --- Wissensbasis ---
    if _is_on("wissensbasis"):
        _block("📚", "Wissensbasis", [
            ("/wissen",
             "Eintrag anlegen — Kategorien: Anfahrt, Leistungen, "
             "FAQ, Allgemein."),
            ("/wissen_anzeigen",
             "Alle Eintraege gruppiert nach Kategorie."),
            ("/wissen_loeschen",
             "Einen Eintrag entfernen."),
        ])

    # --- Kalkulations-Engine ---
    if _is_on("kalkulation"):
        _block("🧮", "Kalkulation (fuer Angebote)", [
            ("/kalkulation",
             "Neue Formel anlegen — z.B. m²-Preis, Pauschale, "
             "Aufschlag-%."),
            ("/kalkulation_anzeigen",
             "Alle Formeln gruppiert nach Kategorie."),
            ("/kalkulation_loeschen",
             "Eine Formel entfernen."),
            ("/kalkulation_excel",
             "Formel-Set aus .xlsx-Datei importieren."),
        ])

    # --- Anfrage-Formular ---
    if _is_on("anfrage_formular"):
        _block("📋", "Web-Anfrageformular", [
            ("/formular",
             "Felder bearbeiten — hinzufuegen, loeschen, "
             "Live-Vorschau."),
            ("/formular_anzeigen",
             "Aktuelles Schema mit allen Feldern + Pflichtangaben."),
            ("/formular_zuruecksetzen",
             "Auf Tischler- oder Allgemein-Default zuruecksetzen."),
        ])

    # --- Visualisierung (immer anzeigen, mit Lock wenn inaktiv) ---
    viz_items = [
        ("/visualisierung",
         "Foto + Text-Beschreibung schicken → photorealistisches "
         "KI-Rendering. Danach an Kunden mailen, ins "
         "Drive-Archiv legen oder verwerfen."),
    ]
    _block("🎨", "Visualisierung", viz_items, locked=not _is_on("visualisierung"))

    # --- Kunden-Archiv (Drive, immer anzeigen mit Lock wenn inaktiv) ---
    drive_items = [
        ("/drive_verbinden",
         "Google-Drive mit dem Tenant verknuepfen (OAuth-Flow)."),
        ("/drive_status",
         "Drive-Verbindung + verknuepfter Account."),
        ("/archiv [kunde]",
         "Datei-Upload starten — Fotos und PDFs landen "
         "automatisch im Kunden-Ordner."),
        ("/fertig",
         "Archiv-Upload abschliessen (nur im laufenden "
         "/archiv-Wizard)."),
    ]
    _block(
        "☁️", "Kunden-Archiv (Google Drive)",
        drive_items, locked=not _is_on("drive_archiv"),
    )

    # --- Mail-Inbox (mail_intake) ---
    if _is_on("mail_intake"):
        _block("📨", "Mail-Inbox (Outlook)", [
            ("/microsoft_setup",
             "Outlook-Postfach via Microsoft-OAuth verbinden."),
            ("/microsoft_status",
             "Konfigurierter Account + letzter Polling-Zyklus."),
            ("/microsoft_check",
             "Inbox sofort einmal abrufen (statt 2-min-Cron zu "
             "warten)."),
        ])

    # --- Setup (Kalender + Standort + Mitarbeiter) ---
    setup_items: list[tuple[str, str]] = []
    if _is_on("kalender"):
        setup_items.append((
            "/kalender_verbinden",
            "Google- oder Outlook-Kalender via OAuth verknuepfen.",
        ))
        setup_items.append((
            "/kalender_status",
            "Welcher Kalender ist mit welchem Account verknuepft.",
        ))
    if _is_on("werkstatt"):
        setup_items.append((
            "/werkstatt",
            "Heimat-Adresse setzen — Basis fuer Fahrtzeit-aware "
            "Termin-Vorschlaege.",
        ))
        setup_items.append((
            "/werkstatt_status",
            "Aktuell hinterlegte Adresse + Koordinaten.",
        ))
    if _is_on("mitarbeiter"):
        setup_items.append((
            "/mitarbeiter",
            "Mitarbeiter anlegen — eigener Telegram-Chat, eigener "
            "Kalender, eigene Skills.",
        ))
    _block("⚙️", "Setup", setup_items)

    # --- Sonstiges (always-on) ---
    _block("ℹ️", "Sonstiges", [
        ("/paket",
         "Aktuelles Paket + Liste der aktivierten Features."),
        ("/status",
         "Tenant-Slug und Aktivierungs-Status."),
        ("/abbrechen",
         "Laufenden Wizard oder State sofort beenden."),
        ("/help",
         "Diese Befehlsuebersicht."),
    ])

    lines.append("")
    if locked_block_count > 0:
        # Footer mit Lock-Erklaerung nur wenn auch wirklich Locks gerendert
        # wurden — sonst verwirrt's mehr als es hilft.
        lines.append("<i>🔒 = nicht in deinem Paket — Upgrade via "
                     "svenj05@gmx.de. Aktuelle Features: /paket</i>")
    else:
        lines.append("<i>Alle Features aktiv. Kontakt: svenj05@gmx.de</i>")
    return "\n".join(lines)


# =====================================================================
# Feature-Gate fuer Telegram-Befehle
# =====================================================================
# Vor jedem Command-Dispatch wird _check_feature_gate aufgerufen.
# Liefert eine Lock-Message wenn das Feature im Paket nicht enthalten
# ist; sonst None und der normale Dispatch laeuft.
#
# Always-on-Features (telegram_bot, kunde_lookup) passieren immer.
# Befehle die in keiner Feature-Definition stehen (z.B. /status, /start
# wenn nicht ueber telegram_bot.always_on abgedeckt) passieren auch —
# Default-Allow fuer unbekannte Befehle (alte Befehle die noch nicht im
# Catalog sind muessen weiter funktionieren).


async def _check_feature_gate(text: str, chat_id) -> str | None:
    """Returnt die Lock-Message wenn der Befehl ein deaktiviertes
    Feature anspricht. Sonst None — Dispatch laeuft normal.
    """
    from core.features import is_feature_enabled, FEATURES
    from core.features.catalog import COMMAND_TO_FEATURE

    # Erstes Wort aus dem Text extrahieren ("/archiv Mueller" -> "/archiv")
    cmd_word = text.split(maxsplit=1)[0]
    feature_key = COMMAND_TO_FEATURE.get(cmd_word)
    if feature_key is None:
        # Befehl ist nicht im Catalog -> kein Gate (z.B. /status, /skip)
        return None

    feature = FEATURES.get(feature_key)
    if feature is None or feature.always_on:
        # always_on-Features sind immer offen
        return None

    # Tenant ermitteln. Wenn nicht zugeordnet (Erstkontakt), kein Gate —
    # /start handled das.
    tenant = await _get_tenant_by_chat(chat_id)
    if tenant is None:
        return None

    if await is_feature_enabled(tenant.id, feature_key):
        return None  # Feature aktiv → normaler Dispatch

    # Feature gesperrt — Lock-Message
    return _feature_locked_message(feature)


def _feature_locked_message(feature) -> str:
    """Klartext-Antwort wenn ein gesperrtes Feature angefragt wird."""
    return (
        f"🔒 <b>{_h_safe(feature.label)}</b> ist nicht in deinem Paket.\n"
        f"Übersicht: /paket  ·  Upgrade: svenj05@gmx.de"
    )


async def _handle_paket_command(chat_id) -> str:
    """Zeigt aktuelles Paket + aktive/inaktive Features."""
    from core.features import enabled_features_for_tenant, FEATURES
    from core.features.catalog import (
        PACKAGES, PACKAGE_LABELS, PACKAGE_CUSTOM,
    )

    tenant = await _get_tenant_by_chat(chat_id)
    if tenant is None:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet. "
            "Bitte zuerst /start ausfuehren."
        )

    enabled = await enabled_features_for_tenant(tenant.id)
    package_label = PACKAGE_LABELS.get(
        tenant.package_tier, f"📦 {tenant.package_tier}"
    )

    # Apple-Style: kompakte zweispaltige Liste — ein Symbol + Name pro
    # Zeile, sortiert nach Label. Aktiv und Inaktiv direkt in einer
    # Übersicht ohne Block-Header.
    feature_lines: list[str] = []
    sorted_features = sorted(
        (f for f in FEATURES.values() if not f.always_on),
        key=lambda f: f.label,
    )
    for f in sorted_features:
        icon = "✅" if f.key in enabled else "·"
        feature_lines.append(f"{icon}  {_h_safe(f.label)}")

    n_on = sum(1 for f in sorted_features if f.key in enabled)
    n_total = len(sorted_features)

    msg_parts = [
        f"<b>{package_label}</b>",
        f"<i>{_h_safe(tenant.company_name)}</i>",
        f"<i>{n_on} von {n_total} Features aktiv</i>",
        "",
        "\n".join(feature_lines),
        "",
    ]
    if tenant.package_tier == PACKAGE_CUSTOM:
        msg_parts.append("<i>Custom-Paket. Aenderungen via Admin.</i>")
    else:
        msg_parts.append("<i>Upgrade: svenj05@gmx.de</i>")
    return "\n".join(msg_parts)

async def _collect_setup_status(tenant, employee) -> list[tuple[str, str, str | None]]:
    """Pro relevantem Feature: ist die Verbindung konfiguriert?

    Zeigt nur Features die im Paket aktiv sind — sonst wuerde ein
    Basis-Paket-User ueber fehlendes Drive meckern obwohl er es gar
    nicht hat. Returns Liste von (icon, label, hint_command_or_None).
    """
    from core.features import enabled_features_for_tenant
    from core.security.oauth_token_lookup import find_oauth_token
    enabled = await enabled_features_for_tenant(tenant.id)
    items: list[tuple[str, str, str | None]] = []

    # Kalender — Provider absichtlich nicht im Label (Google/Microsoft
    # beide moeglich, Sven will keinen Tech-Namen wo's mehrere gibt).
    if "kalender" in enabled:
        if employee.calendar_provider:
            items.append(("✅", "Kalender", None))
        else:
            items.append(("❌", "Kalender", "/kalender_verbinden"))

    # Mail-Postfach — bisher nur Outlook, kuenftig vllt. Gmail.
    if "mail_intake" in enabled:
        ms = await find_oauth_token(tenant.id, "microsoft")
        items.append(
            ("✅", "Mail-Postfach", None) if ms
            else ("❌", "Mail-Postfach", "/microsoft_setup")
        )

    # Kunden-Archiv (Cloud-Speicher) — bisher Google Drive.
    if "drive_archiv" in enabled:
        try:
            from core.integrations.google_drive import is_drive_configured
            tok = await find_oauth_token(tenant.id, "google", employee.id)
            if tok and is_drive_configured(tok):
                items.append(("✅", "Kunden-Archiv", None))
            elif tok:
                items.append(("⚠️", "Kunden-Archiv (Scope fehlt)", "/drive_verbinden"))
            else:
                items.append(("❌", "Kunden-Archiv", "/drive_verbinden"))
        except Exception:
            logger.exception("drive_archiv-Status-Check fehlgeschlagen")
            items.append(("•", "Kunden-Archiv (Status unklar)", None))

    # Lexware — Token in ToolConfig
    if "lexware" in enabled:
        try:
            async with AsyncSessionLocal() as s:
                row = (await s.execute(
                    select(ToolConfig).where(
                        ToolConfig.tenant_id == tenant.id,
                        ToolConfig.tool_name == LEXWARE_TOOL_NAME,
                    )
                )).scalar_one_or_none()
            has_token = bool(
                row and row.enabled and (row.config or {}).get("encrypted_token")
            )
            items.append(
                ("✅", "Buchhaltung", None) if has_token
                else ("❌", "Buchhaltung", "/lexware_setup")
            )
        except Exception:
            logger.exception("lexware-Status-Check fehlgeschlagen")
            items.append(("•", "Buchhaltung (Status unklar)", None))

    # Werkstatt-/Heimat-Adresse
    if "werkstatt" in enabled:
        label = "Werkstatt-Adresse" if employee.is_default else "Heimat-Adresse"
        if employee.heimat_strasse and employee.heimat_ort:
            items.append(("✅", label, None))
        else:
            items.append(("❌", label, "/werkstatt"))

    return items


async def _handle_status_command(chat_id):
    """Persoenliche Statusuebersicht — wer bin ich, was ist verbunden,
    laeuft gerade ein Wizard.

    Soll auf den ersten Blick zeigen: 'wo stehe ich, wo muss ich noch
    klicken'. Bisher war der Output kryptisch ('demo · onboarding') —
    jetzt: Identitaet, Paket, Setup-Check, Wizard-Zustand.
    """
    from core.features.catalog import PACKAGE_LABELS

    res = await _get_current_employee(chat_id)
    if res is None:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst /start ausfuehren oder QR-Code scannen."
        )
    tenant, employee = res

    # Paket-Label aus catalog (faellt auf raw-Wert zurueck).
    package_label = PACKAGE_LABELS.get(tenant.package_tier, tenant.package_tier)
    # Tenant-Status menschen-lesbar.
    status_icon = {
        "active": "✅",
        "onboarding": "🕒",
        "suspended": "⏸",
        "cancelled": "🚫",
    }.get(tenant.status, "•")
    role_label = "Inhaber" if employee.is_default else "Mitarbeiter"

    lines: list[str] = [
        f"👤 <b>{_h_safe(employee.name)}</b>  ·  {role_label}",
        f"🏢 <b>{_h_safe(tenant.company_name)}</b>",
        f"Slug: <code>{_h_safe(tenant.slug)}</code>  ·  "
        f"Paket: <b>{_h_safe(package_label)}</b>  ·  "
        f"Status: {status_icon} {_h_safe(tenant.status)}",
        "",
    ]

    setup = await _collect_setup_status(tenant, employee)
    if setup:
        lines.append("<b>🔌 Verbindungen</b>")
        for icon, label, hint in setup:
            line = f"{icon}  {label}"
            if hint:
                line += f" — {hint}"
            lines.append(line)
        lines.append("")

    # Aktiver Wizard?
    state = None
    try:
        state = await _load_state(chat_id)
    except Exception:
        logger.exception("State-Check fuer /status fehlgeschlagen")

    lines.append("<b>📋 Bot-Zustand</b>")
    lines.append(f"Telegram-Chat: <code>{chat_id}</code>")
    if state:
        lines.append(
            f"⏳ Aktiver Vorgang: <code>{_h_safe(state.state_key)}</code> "
            "— mit /abbrechen beenden"
        )
    else:
        lines.append("<i>Kein laufender Wizard.</i>")

    return "\n".join(lines)

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


async def _apply_kalkulationen(tenant_id, extracted: dict) -> dict:
    """
    Hybrid-Berechnung: Wenn Gemini fuer eine Position eine `kalkulation`
    mit regel_name + Variablen-Werten geliefert hat, suchen wir die
    passende Regel in der DB und berechnen den Preis deterministisch
    in Python aus Formel + Variablen.

    Schreibt das Ergebnis in `position["preis_brutto_eur"]` und legt
    `position["kalkulation"]["wert"]` fuer die Anzeige ab. Wenn die
    Berechnung fehlschlaegt (Regel nicht gefunden, Variable fehlt),
    bleibt der Original-Preis stehen und wir loggen die Warnung.
    """
    if not extracted or not isinstance(extracted, dict):
        return extracted
    positionen = extracted.get("positionen") or []
    if not positionen:
        return extracted

    # Frueh aussteigen falls keine Position eine Regel referenziert
    if not any(
        isinstance(p.get("kalkulation"), dict) and p["kalkulation"].get("regel_name")
        for p in positionen
    ):
        return extracted

    # Regeln des Tenants laden (alle aktiven; Match per name case-insensitive)
    async with AsyncSessionLocal() as s:
        regeln = (await s.execute(
            select(TenantKalkulation)
            .where(TenantKalkulation.tenant_id == tenant_id)
            .where(TenantKalkulation.aktiv.is_(True))
        )).scalars().all()
    by_name = {r.name.strip().lower(): r for r in regeln}

    from core.ai.kalkulation import (
        FormelError,
        safe_eval_formel,
    )

    new_gesamt = 0.0
    has_changes = False
    for pos in positionen:
        kalk = pos.get("kalkulation")
        if not isinstance(kalk, dict):
            continue
        regel_name = (kalk.get("regel_name") or "").strip().lower()
        if not regel_name or regel_name not in by_name:
            logger.info(
                "Kalkulation: Regel %r nicht gefunden, skip Position %r",
                regel_name,
                pos.get("name"),
            )
            continue
        regel = by_name[regel_name]
        vars_dict = kalk.get("variablen") or {}
        if not isinstance(vars_dict, dict):
            continue
        try:
            wert = safe_eval_formel(regel.formel, vars_dict)
        except FormelError as exc:
            logger.warning(
                "Kalkulation '%s' fehlgeschlagen (%s) - Original-Preis bleibt",
                regel.name, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Kalkulation '%s' Ausnahme: %s - Original-Preis bleibt",
                regel.name, exc, exc_info=True,
            )
            continue

        # Position aktualisieren - Preis pro Einheit auf den Formel-Wert
        # setzen, Menge auf 1, damit menge*preis = berechneter Wert.
        pos["preis_brutto_eur"] = round(wert, 2)
        pos["menge"] = 1.0
        if not pos.get("einheit") or pos.get("einheit") == "Stueck":
            pos["einheit"] = regel.einheit or "Pauschal"
        # Kalkulations-Hinweis fuer die Preview-Anzeige zurueckschreiben
        kalk["wert"] = round(wert, 2)
        kalk["formel"] = regel.formel
        has_changes = True
        logger.info(
            "Kalkulation '%s' angewandt: %s -> %.2f EUR (Position '%s')",
            regel.name, vars_dict, wert, pos.get("name"),
        )
        new_gesamt += wert

    if has_changes:
        # Gesamtbetrag neu errechnen aus allen Positionen
        try:
            extracted["gesamtbetrag_brutto_eur"] = round(
                sum(
                    float(p.get("menge") or 1) * float(p.get("preis_brutto_eur") or 0)
                    for p in positionen
                ),
                2,
            )
        except (TypeError, ValueError):
            pass
    return extracted


# ===========================================================================
# Kalkulation-Wizard (mathematische Formeln fuers Angebot)
# ===========================================================================
#
# Schwester-Konzept zu /wissen, aber mit numerischen Formeln statt Freitext.
# - /kalkulation                : Wizard Kategorie -> Name -> Formel -> Einheit
# - /kalkulation_anzeigen       : Liste, gruppiert nach Kategorie
# - /kalkulation_loeschen       : Eintrag entfernen
# - /kalkulation_excel          : Excel-Datei hochladen (Formeln werden importiert)
#
# Beim Aufnahme-Wizard und Voice-Call werden alle aktiven Formeln des
# Tenants in den Gemini-Prompt eingespeist; der eigentliche Preis wird
# anschliessend deterministisch in Python berechnet (siehe core.ai.kalkulation).

KALK_FORMEL_MAX_LEN = 1000
KALK_NAME_MAX_LEN = 100
KALK_BESCHREIBUNG_MAX_LEN = 500
KALK_EXCEL_MAX_BYTES = 4 * 1024 * 1024  # 4 MB - Excel-Files sind selten groesser


async def _handle_kalkulation_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return ("Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
                "Bitte zuerst Aktivierungs-QR-Code scannen.")
    await _save_state(chat_id, STATE_KALK_KATEGORIE, {})
    msg = "<b>🧮 Neue Kalkulationsregel</b>\n\n"
    msg += "Welche Kategorie passt?\n\n"
    for i, key in enumerate(ALLE_KALK_KATEGORIEN, start=1):
        label = KALK_KATEGORIE_LABELS.get(key, key)
        msg += f"{i}) {label}\n"
    msg += (
        f"\nAntworten Sie mit der Nummer (1-{len(ALLE_KALK_KATEGORIEN)})"
        " oder /abbrechen.\n\n"
        "💡 Mit /kalkulation_excel koennen Sie auch eine .xlsx-Datei "
        "hochladen, dann werden alle Formeln automatisch uebernommen."
    )
    return msg


async def _handle_kalk_kategorie_input(chat_id, text):
    text = text.strip()
    if not text.isdigit():
        return (
            f"Bitte mit einer Nummer von 1 bis {len(ALLE_KALK_KATEGORIEN)}"
            " antworten oder /abbrechen."
        )
    idx = int(text) - 1
    if idx < 0 or idx >= len(ALLE_KALK_KATEGORIEN):
        return (
            f"Nur Nummern 1 bis {len(ALLE_KALK_KATEGORIEN)} sind gueltig."
            " Bitte erneut waehlen."
        )
    kategorie = ALLE_KALK_KATEGORIEN[idx]
    label = KALK_KATEGORIE_LABELS.get(kategorie, kategorie)
    await _save_state(chat_id, STATE_KALK_NAME, {"kategorie": kategorie})
    msg = f"Kategorie: <b>{label}</b>\n\n"
    msg += "Wie heisst diese Regel? (Kurzer Name)\n\n"
    msg += "<i>Beispiele:</i>\n"
    msg += "<i>- Anfahrtspauschale</i>\n"
    msg += "<i>- Notfall-Zuschlag</i>\n"
    msg += "<i>- Material-Aufschlag</i>\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_kalk_name_input(chat_id, text, state_data):
    name = text.strip()
    if len(name) < 3:
        return "Der Name ist zu kurz (mind. 3 Zeichen). Bitte nochmal."
    if len(name) > KALK_NAME_MAX_LEN:
        return f"Der Name ist zu lang (max {KALK_NAME_MAX_LEN}). Bitte kuerzen."
    state_data = dict(state_data or {})
    state_data["name"] = name
    await _save_state(chat_id, STATE_KALK_FORMEL, state_data)
    msg = f"<b>{name}</b>\n\n"
    msg += "Wie wird der Wert berechnet? Schreibe die Formel als Text.\n\n"
    msg += "<b>Erlaubt:</b>\n"
    msg += "- Variablen (kleinbuchstaben mit Unterstrich, z.B. <code>entfernung_km</code>)\n"
    msg += "- Operatoren: + − × (oder *) ÷ (oder /), ** fuer Potenz\n"
    msg += "- Funktionen: <code>min</code>, <code>max</code>, <code>round</code>, <code>abs</code>, <code>ceil</code>, <code>floor</code>\n"
    msg += "- Bedingung: <code>x if bedingung else y</code>\n\n"
    msg += "<b>Beispiele:</b>\n"
    msg += "<code>entfernung_km * 0.50</code>\n"
    msg += "<code>max(50, stunden * 75)</code>\n"
    msg += "<code>einkaufspreis * 1.30 + 5</code>\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_kalk_formel_input(chat_id, text, state_data):
    # Formel-Engine erst hier importieren, damit der Modul-Import nicht
    # zur Startup-Zeit knallt falls jemand die Datei kopiert.
    from core.ai.kalkulation import FormelError, parse_variables

    formel = text.strip()
    if len(formel) > KALK_FORMEL_MAX_LEN:
        return f"Die Formel ist zu lang (max {KALK_FORMEL_MAX_LEN} Zeichen)."
    # Komfort: × und ÷ akzeptieren wir und uebersetzen
    formel_norm = formel.replace("×", "*").replace("·", "*").replace("÷", "/")
    try:
        variablen = parse_variables(formel_norm)
    except FormelError as exc:
        return (
            f"❌ Formel ungueltig: {exc}\n\n"
            "Bitte korrigieren oder /abbrechen.\n"
            "Tipp: Variablen mit kleinbuchstaben + Unterstrich, z.B."
            " <code>entfernung_km</code>."
        )

    state_data = dict(state_data or {})
    state_data["formel"] = formel_norm
    state_data["variablen"] = variablen
    await _save_state(chat_id, STATE_KALK_EINHEIT, state_data)

    if variablen:
        var_text = ", ".join(f"<code>{v}</code>" for v in variablen)
    else:
        var_text = "<i>(keine - konstanter Wert)</i>"
    msg = "✅ Formel verstanden.\n\n"
    msg += f"Variablen: {var_text}\n\n"
    msg += (
        "Welche Einheit hat das Ergebnis? Z.B. <code>EUR</code>, "
        "<code>EUR/Stunde</code>, <code>%</code>.\n\n"
        "Antworte mit <code>-</code> wenn keine Einheit."
    )
    return msg


async def _handle_kalk_einheit_input(chat_id, text, state_data):
    einheit = text.strip()
    if einheit == "-" or einheit == "":
        einheit = None
    elif len(einheit) > 50:
        return "Einheit ist zu lang (max 50 Zeichen)."
    state_data = dict(state_data or {})
    state_data["einheit"] = einheit
    await _save_state(chat_id, STATE_KALK_BESCHREIBUNG, state_data)
    msg = "Wann soll diese Regel greifen?\n\n"
    msg += "Schreibe einen kurzen Satz fuer die KI, z.B.\n"
    msg += "<i>'Bei jedem Auftrag mit Anfahrt zu Kundenadresse anwenden.'</i>\n\n"
    msg += f"Mit <code>-</code> ueberspringen (max {KALK_BESCHREIBUNG_MAX_LEN} Zeichen)."
    return msg


async def _handle_kalk_beschreibung_input(chat_id, text, state_data):
    beschreibung = text.strip()
    if beschreibung == "-" or beschreibung == "":
        beschreibung = None
    elif len(beschreibung) > KALK_BESCHREIBUNG_MAX_LEN:
        return (
            f"Beschreibung zu lang (max {KALK_BESCHREIBUNG_MAX_LEN} Zeichen)."
            " Bitte kuerzen."
        )
    data = dict(state_data or {})
    kategorie = data.get("kategorie")
    name = data.get("name")
    formel = data.get("formel")
    variablen = data.get("variablen") or []
    einheit = data.get("einheit")
    if not kategorie or not name or not formel:
        await _clear_state(chat_id)
        return "Etwas ging schief. Bitte mit /kalkulation neu starten."

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. Bitte erneut /start ausfuehren."

    async with AsyncSessionLocal() as s:
        entry = TenantKalkulation(
            tenant_id=tenant.id,
            kategorie=kategorie,
            name=name,
            formel=formel,
            variablen=variablen,
            einheit=einheit,
            beschreibung=beschreibung,
            source=KALK_SOURCE_MANUAL,
        )
        s.add(entry)
        await s.commit()

    await _clear_state(chat_id)
    label = KALK_KATEGORIE_LABELS.get(kategorie, kategorie)
    msg = f"✅ Gespeichert in <b>{label}</b>:\n\n"
    msg += f"<b>{name}</b>\n"
    msg += f"<code>{formel}</code>"
    if einheit:
        msg += f"  →  {einheit}"
    msg += "\n\n"
    msg += "Diese Regel wird ab jetzt bei jedem neuen Angebot beachtet.\n\n"
    msg += "/kalkulation - weitere Regel anlegen\n"
    msg += "/kalkulation_anzeigen - alle ansehen"
    return msg


async def _handle_kalkulation_anzeigen(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKalkulation)
            .where(TenantKalkulation.tenant_id == tenant.id)
            .where(TenantKalkulation.aktiv.is_(True))
            .order_by(
                TenantKalkulation.kategorie,
                TenantKalkulation.sortierung,
                TenantKalkulation.created_at,
            )
        )).scalars().all()
    if not entries:
        return (
            "Noch keine Kalkulations-Regeln vorhanden.\n\n"
            "Mit /kalkulation legst Du Deine erste Regel an, "
            "oder mit /kalkulation_excel laedst Du eine Excel-Datei hoch."
        )
    by_kat: dict[str, list[TenantKalkulation]] = {}
    for e in entries:
        by_kat.setdefault(e.kategorie, []).append(e)
    msg = f"<b>🧮 Kalkulationsregeln · {tenant.company_name}</b>\n\n"
    total = 0
    for kat in ALLE_KALK_KATEGORIEN:
        if kat not in by_kat:
            continue
        label = KALK_KATEGORIE_LABELS.get(kat, kat)
        msg += f"<b>{label}</b>\n"
        for e in by_kat[kat]:
            tail = f"  →  {e.einheit}" if e.einheit else ""
            src_tag = "  (Excel)" if e.source == KALK_SOURCE_EXCEL else ""
            msg += f"  • <b>{e.name}</b>{src_tag}\n"
            msg += f"    <code>{e.formel}</code>{tail}\n"
            total += 1
        msg += "\n"
    msg += f"<i>Insgesamt {total} Regel(n).</i>\n"
    msg += "Mit /kalkulation_loeschen kannst Du Eintraege entfernen."
    return msg


async def _handle_kalkulation_loeschen_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKalkulation)
            .where(TenantKalkulation.tenant_id == tenant.id)
            .where(TenantKalkulation.aktiv.is_(True))
            .order_by(
                TenantKalkulation.kategorie,
                TenantKalkulation.created_at,
            )
        )).scalars().all()
    if not entries:
        return "Es gibt keine Regeln zum Loeschen."
    id_map = {}
    msg = "<b>Welche Regel loeschen?</b>\n\n"
    for i, e in enumerate(entries, start=1):
        id_map[str(i)] = str(e.id)
        label = KALK_KATEGORIE_LABELS.get(e.kategorie, e.kategorie)
        msg += f"{i}) [{label}] <b>{e.name}</b>: <code>{e.formel}</code>\n"
    msg += "\nAntworte mit der Nummer oder /abbrechen."
    await _save_state(chat_id, STATE_KALK_LOESCHEN, {"id_map": id_map})
    return msg


async def _handle_kalk_loeschen_input(chat_id, text, state_data):
    text = text.strip()
    id_map = (state_data or {}).get("id_map") or {}
    if text not in id_map:
        return "Ungueltige Nummer. Bitte eine angezeigte Nummer eingeben oder /abbrechen."
    entry_id = id_map[text]
    async with AsyncSessionLocal() as s:
        entry = (await s.execute(
            select(TenantKalkulation).where(
                TenantKalkulation.id == uuid.UUID(entry_id)
            )
        )).scalar_one_or_none()
        if entry is None:
            await _clear_state(chat_id)
            return "Regel nicht mehr vorhanden (eventuell schon geloescht)."
        name = entry.name
        await s.delete(entry)
        await s.commit()
    await _clear_state(chat_id)
    return f"✅ Regel <b>{name}</b> geloescht."


async def _handle_kalkulation_excel_command(chat_id):
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist keinem Betrieb zugeordnet."
    await _save_state(chat_id, STATE_KALK_EXCEL_WAITING, {})
    msg = "<b>📊 Excel-Datei hochladen</b>\n\n"
    msg += "Schicke jetzt eine <b>.xlsx</b>-Datei mit Deinen Formeln.\n\n"
    msg += "Ich lese alle Zellen mit Formeln (z.B. <code>=B2*0,5</code>) "
    msg += "und uebernehme sie als Kalkulationsregeln. Den Namen lese ich "
    msg += "aus der Beschriftung links bzw. oberhalb der Zelle.\n\n"
    msg += "Unterstuetzt: arithmetische Formeln, WENN/IF, MIN, MAX, RUNDEN.\n"
    msg += "Nicht unterstuetzt: SVERWEIS, INDIREKT, INDEX, Zellbereiche (B2:B5).\n\n"
    msg += "/abbrechen um abzubrechen."
    return msg


async def _handle_kalk_excel_received(chat_id, document, bot_token):
    """Verarbeitet eine via Telegram hochgeladene .xlsx-Datei."""
    if not document:
        return ("Bitte schicke eine <b>.xlsx</b>-Datei (kein Foto).")

    file_name = (document.get("file_name") or "").lower()
    file_size = int(document.get("file_size") or 0)
    file_id = document.get("file_id")

    if not file_id:
        return "Datei ohne ID empfangen - bitte nochmal."
    if not file_name.endswith(".xlsx"):
        return (
            "Nur <b>.xlsx</b>-Dateien werden unterstuetzt. "
            "Bitte als Excel-2007+-Format speichern."
        )
    if file_size > KALK_EXCEL_MAX_BYTES:
        return (
            f"Datei zu gross ({file_size // 1024} KB). "
            f"Maximum: {KALK_EXCEL_MAX_BYTES // 1024} KB."
        )

    # Datei laden
    file_path = await _telegram_get_file_path(bot_token, file_id)
    if not file_path:
        return "Konnte die Datei nicht von Telegram abholen."
    try:
        file_bytes = await _telegram_download_file(bot_token, file_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Excel-Download fehlgeschlagen: %s", exc)
        return "Download fehlgeschlagen - bitte nochmal."

    # Parsen
    from core.integrations.excel_kalkulation import (
        ExcelImportError,
        extract_formulas_from_xlsx,
    )

    try:
        result = extract_formulas_from_xlsx(file_bytes)
    except ExcelImportError as exc:
        await _clear_state(chat_id)
        return f"❌ Excel-Datei nicht lesbar: {exc}"

    if not result.eintraege:
        warn_text = ("\n\n" + "\n".join(result.warnungen[:5])) if result.warnungen else ""
        await _clear_state(chat_id)
        return (
            "⚠️ Keine importierbaren Formeln gefunden.\n\n"
            f"Sheets gelesen: {', '.join(result.sheets_gelesen)}"
            f"{warn_text}"
        )

    # State auf "Confirm" setzen mit den gefundenen Eintraegen
    eintraege_serial = [
        {
            "name": e.name,
            "formel": e.formel,
            "variablen": e.variablen,
            "raw_excel": e.raw_excel,
            "cell": e.cell,
            "sheet": e.sheet,
        }
        for e in result.eintraege
    ]
    await _save_state(
        chat_id,
        STATE_KALK_EXCEL_CONFIRM,
        {
            "eintraege": eintraege_serial,
            "filename": document.get("file_name"),
            "warnungen": result.warnungen[:10],
        },
    )

    msg = f"<b>📊 Gefunden: {len(result.eintraege)} Formel(n)</b>\n\n"
    for i, e in enumerate(result.eintraege[:15], start=1):
        msg += f"{i}) <b>{e.name}</b>\n"
        msg += f"   <code>{e.formel}</code>\n"
    if len(result.eintraege) > 15:
        msg += f"\n... und {len(result.eintraege) - 15} weitere.\n"
    if result.warnungen:
        msg += "\n<b>⚠️ Hinweise:</b>\n"
        for w in result.warnungen[:5]:
            msg += f"• {w}\n"
        if len(result.warnungen) > 5:
            msg += f"... und {len(result.warnungen) - 5} weitere Warnungen.\n"
    msg += "\nAlle als Kategorie <b>'Sonstiges'</b> uebernehmen?\n"
    msg += "Antworte mit <b>ja</b> zum Speichern oder /abbrechen."
    return msg


async def _handle_kalk_excel_confirm_input(chat_id, text, state_data):
    if text.strip().lower() not in {"ja", "j", "yes", "ok"}:
        return ("Antworte mit <b>ja</b> um die Formeln zu speichern, "
                "oder /abbrechen um zu verwerfen.")

    data = state_data or {}
    eintraege = data.get("eintraege") or []
    filename = data.get("filename")
    if not eintraege:
        await _clear_state(chat_id)
        return "Keine Eintraege im State - bitte nochmal /kalkulation_excel."

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. Bitte /start erneut ausfuehren."

    saved = 0
    async with AsyncSessionLocal() as s:
        for entry in eintraege:
            s.add(TenantKalkulation(
                tenant_id=tenant.id,
                kategorie="sonstiges",
                name=entry.get("name") or "ohne Namen",
                formel=entry.get("formel") or "",
                variablen=entry.get("variablen") or [],
                source=KALK_SOURCE_EXCEL,
                excel_filename=filename,
                beschreibung=(
                    f"Aus {filename} ({entry.get('sheet')}!{entry.get('cell')}, "
                    f"Original: {entry.get('raw_excel')})"
                ),
            ))
            saved += 1
        await s.commit()

    await _clear_state(chat_id)
    msg = f"✅ <b>{saved} Formel(n) gespeichert.</b>\n\n"
    msg += "Mit /kalkulation_anzeigen siehst Du alle Regeln. "
    msg += "Mit /kalkulation_loeschen kannst Du einzelne entfernen.\n\n"
    msg += "💡 Du kannst die Kategorien spaeter pro Eintrag anpassen, "
    msg += "indem Du sie loeschst und mit /kalkulation neu anlegst."
    return msg


# ===========================================================================
# Ende Kalkulation-Wizard
# ===========================================================================


# Befehle, die ein laufender Wizard nicht abschalten darf — sonst
# kann der User aus dem Wizard nicht antworten. Alles ANDERE wird beim
# naechsten Slash-Befehl als impliziter Reset behandelt.
_WIZARD_SAFE_COMMANDS = frozenset({
    "/abbrechen", "/cancel", "/reset", "/skip", "/fertig", "/ja", "/nein",
})


async def _safe_clear_state(chat_id):
    """State loeschen, aber niemals selbst crashen — fuer Recovery-Paths.

    Wenn die DB streikt, soll der Recovery-Handler trotzdem zu Ende
    kommen und dem User eine Antwort schicken. Nur loggen, nicht rethrow.
    """
    try:
        await _clear_state(chat_id)
    except Exception:
        logger.exception(f"State-Clear fuer chat_id={chat_id} fehlgeschlagen")


async def _send_safe(chat_id, text):
    """Send mit Try/Except — fuer Recovery-Paths, nie rethrow."""
    try:
        await _send_to_chat(chat_id, text)
    except Exception:
        logger.exception(f"Recovery-Send an chat_id={chat_id} fehlgeschlagen")


def _extract_chat_id(payload) -> int | None:
    """Versucht aus einem beliebigen Telegram-Update die chat_id zu holen.

    Wird vom outer try/except benutzt — wenn der Hauptcode crasht,
    muessen wir trotzdem wissen WEM wir die Recovery-Antwort schicken.
    """
    try:
        msg = payload.get("message") or payload.get("edited_message")
        if msg:
            return (msg.get("chat") or {}).get("id")
        cq = payload.get("callback_query") or {}
        return ((cq.get("message") or {}).get("chat") or {}).get("id")
    except Exception:
        return None


async def process_telegram_update(payload):
    """Top-Level-Webhook-Dispatcher mit Recovery-Guarantees.

    Drei Defensive-Layer fuer den Sven-Stuck-Case (siehe Bug 2026-05-12,
    /werkstatt -> /Werkstatt -> /start demo__marco-jantos -> 500):

    1) Universal `/abbrechen` (auch /cancel, /reset) wird VOR allem
       anderen abgefangen — auch wenn ein Photo/Voice oder Feature-Gate
       sonst dazwischen kaeme. Liefert garantiert eine Antwort.
    2) Outer try/except: jeder Crash im Dispatch fuehrt zu State-Clear
       + freundlicher Recovery-Message statt 500. So bleibt der Bot
       benutzbar selbst wenn ein einzelner Handler buggy ist.
    3) Slash-Befehle werden case-insensitiv normalisiert (kein 'War
       das jetzt /Werkstatt oder /werkstatt'-Hänger mehr).
    """
    chat_id_for_recovery = _extract_chat_id(payload)

    # Layer 1: Universal /abbrechen — vor Photo/Feature-Gate/Allem.
    # Match auf den ersten Token, damit auch "/abbrechen jetzt", "/cancel
    # bitte" o.ae. durchgehen. Aliase: /cancel /reset /stop. Auch via
    # Callback-Query: ein Inline-Button mit data="/abbrechen" greift.
    try:
        # Text aus Message ODER Callback-Query holen.
        msg = payload.get("message") or payload.get("edited_message") or {}
        text_raw = (msg.get("text") or "").strip().lower()
        cq_text = (payload.get("callback_query") or {}).get("data") or ""
        cancel_token = (text_raw or cq_text.strip().lower()).split(maxsplit=1)
        first = cancel_token[0] if cancel_token else ""
        if first in ("/abbrechen", "/cancel", "/reset", "/stop"):
            chat_id = (
                (msg.get("chat") or {}).get("id")
                or (((payload.get("callback_query") or {}).get("message") or {})
                    .get("chat") or {}).get("id")
            )
            if chat_id:
                # State-Existenz ist nur fuer Wording — wenn der Check
                # crasht, gehen wir defensiv von "lief was" aus und
                # senden trotzdem die Abbrechen-Bestaetigung.
                state_existed = True
                try:
                    state_existed = (await _load_state(chat_id)) is not None
                except Exception:
                    logger.exception(
                        "State-Load im /abbrechen-Pfad fehlgeschlagen — "
                        "ignoriere und sende Abbrechen-Bestaetigung trotzdem"
                    )
                await _safe_clear_state(chat_id)
                if state_existed:
                    msg_out = (
                        "✅ <b>Abgebrochen.</b> Alle offenen Vorgaenge "
                        "zurueckgesetzt.\n\nMit /help sehen Sie alle Befehle."
                    )
                else:
                    msg_out = "Es laeuft gerade keine Aktion. /help zeigt was ich kann."
                await _send_safe(chat_id, msg_out)
                logger.info(
                    f"/abbrechen erfolgreich (chat_id={chat_id}, "
                    f"state_existed={state_existed})"
                )
                return {"ok": True}
    except Exception:
        logger.exception(
            "Fehler im /abbrechen-Early-Path — eskalier zum Recovery"
        )
        # Letzter Notnagel: ChatID retten und Abbrechen-Antwort senden,
        # auch wenn die Logik oben crashte. /abbrechen MUSS durchgehen.
        try:
            recov_chat = _extract_chat_id(payload)
            if recov_chat:
                await _safe_clear_state(recov_chat)
                await _send_safe(
                    recov_chat,
                    "✅ <b>Abgebrochen.</b> (Notfall-Reset) Mit /help "
                    "sehen Sie alle Befehle.",
                )
                return {"ok": True}
        except Exception:
            logger.exception("Auch der Notfall-Abbrechen-Pfad ist gescheitert")

    # Layer 2 + 3: Inner-Dispatch mit Crash-Recovery.
    try:
        return await _dispatch_update(payload)
    except Exception:
        logger.exception(
            f"Telegram-Handler-Crash (chat_id={chat_id_for_recovery}); "
            "State wird zurueckgesetzt und User benachrichtigt."
        )
        if chat_id_for_recovery:
            await _safe_clear_state(chat_id_for_recovery)
            await _send_safe(
                chat_id_for_recovery,
                "⚠️ Da ist intern etwas schiefgelaufen. "
                "Ich habe den Vorgang zurueckgesetzt — bitte erneut versuchen.\n\n"
                "Mit /help siehst du alle Befehle. "
                "Falls es wieder passiert, bitte beim Betreiber melden.",
            )
        # Webhook NIE 500 zurueckgeben — Telegram retried sonst endlos.
        return {"ok": True}


async def _dispatch_update(payload):
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
        elif cq_data and cq_data.startswith("angebot:"):
            await _handle_angebot_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("auftrag:"):
            await _handle_auftrag_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("leistung:"):
            await _handle_leistung_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("formular:"):
            await _handle_formular_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("kal:"):
            await _handle_kalender_callback(cq_chat_id, cq_data, cq_id, bot_token)
        elif cq_data and cq_data.startswith("viz:"):
            await _handle_viz_callback(cq_chat_id, cq_data, cq_id, bot_token)
        else:
            # Unbekannte Callback-Daten - nur bestätigen
            await _answer_callback_query(cq_id, "Unbekannte Aktion", bot_token)
        return {"ok": True}

    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        return {"ok": True}
    text = (msg.get("text") or "").strip()
    # Slash-Befehle case-insensitiv: /Werkstatt = /werkstatt = /WERKSTATT.
    # Argumente bleiben unveraendert — "/kunde Müller" wird "/kunde Müller",
    # nicht "/kunde müller". Sonst zerstoeren wir Adress- und Namens-Inputs.
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        parts[0] = parts[0].lower()
        text = " ".join(parts) if len(parts) > 1 else parts[0]
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
        if state and state.state_key in (
            STATE_VIZ_WAITING_PHOTO,
            STATE_BELEG_WAITING_PHOTO,
            STATE_KALK_EXCEL_WAITING,
        ):
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

        if state and state.state_key == STATE_KALK_EXCEL_WAITING:
            reply = await _handle_kalk_excel_received(chat_id, document, bot_token)
            await _send_to_chat(chat_id, reply)
            return {"ok": True}

        if state and state.state_key == STATE_ARCHIV_WAITING_FILES:
            if not bot_token:
                bot_token = await _load_global_bot_token()
                if not bot_token:
                    await _send_to_chat(
                        chat_id,
                        "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                    )
                    return {"ok": True}
            reply = await _handle_archiv_file_received(
                chat_id, photo_array, document, bot_token, state.state_data,
            )
            if reply:
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
        # ----- Angebot-Wizard: Voice-Note empfangen -----
        if state and state.state_key == STATE_ANGEBOT_WAITING_INPUT:
            bot_token = await _load_global_bot_token()
            if not bot_token:
                await _send_to_chat(
                    chat_id,
                    "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                )
                return {"ok": True}
            await _send_to_chat(chat_id, "⏳ Verstehe deine Aufnahme…")
            reply = await _handle_angebot_input_received(
                chat_id, voice_dict=voice, bot_token=bot_token,
            )
            if reply:
                await _send_to_chat(chat_id, reply)
            return {"ok": True}
        # ----- Angebot-Wizard: Voice-Anweisungen fuers Anschreiben -----
        if state and state.state_key == STATE_ANGEBOT_AWAITING_INSTRUCTIONS:
            bot_token = await _load_global_bot_token()
            if not bot_token:
                await _send_to_chat(
                    chat_id,
                    "Bot-Konfiguration fehlt - bitte beim Betreiber melden.",
                )
                return {"ok": True}
            await _send_to_chat(chat_id, "⏳ Schreibe das Anschreiben…")
            reply = await _handle_angebot_instructions_received(
                chat_id, voice_dict=voice, bot_token=bot_token,
            )
            if reply:
                await _send_to_chat(chat_id, reply)
            return {"ok": True}
        # Voice-Note ohne aktiven Wizard - ignorieren
        logger.info("Voice ohne passenden State ignoriert")
        return {"ok": True}

    # ----- Auto-Reset alter Wizard-States bei neuem Slash-Befehl -----
    # Wenn der User mitten im Wizard einen ANDEREN Slash-Befehl tippt,
    # ist seine Absicht klar: alten Vorgang aufgeben, neuen anfangen.
    # Sonst staut sich der State zu (Sven-Hänger 2026-05-12).
    # Ausnahmen: /abbrechen wird oben schon abgefangen; /skip /fertig
    # /ja /nein /cancel /reset koennen Wizard-Antworten sein.
    if text.startswith("/"):
        first_token = text.split(maxsplit=1)[0]
        if first_token not in _WIZARD_SAFE_COMMANDS:
            try:
                existing_state = await _load_state(chat_id)
                if existing_state is not None:
                    logger.info(
                        f"Auto-Reset: chat_id={chat_id} hatte State "
                        f"{existing_state.state_key}, wird durch Slash-"
                        f"Befehl {first_token!r} zurueckgesetzt"
                    )
                    await _clear_state(chat_id)
            except Exception:
                logger.exception("Auto-Reset-Check fehlgeschlagen — ignoriert")

    # ----- Feature-Gate -----
    # Vor dem Befehls-Dispatch pruefen ob das angefragte Feature im
    # Paket des Tenants ist. Always-on-Befehle (/help, /start, /paket)
    # passieren unbedrueckt — sonst koennte ein Tenant ohne Paket nie
    # /paket lesen.
    if text.startswith("/"):
        locked_msg = await _check_feature_gate(text, chat_id)
        if locked_msg is not None:
            await _send_to_chat(chat_id, locked_msg)
            return {"ok": True}

    # ----- Text-Pfad -----
    if text == "/abbrechen":
        reply = await _handle_abbrechen(chat_id)
    elif text.startswith("/start"):
        await _clear_state(chat_id)
        reply = await _handle_start_command(text, chat_id, from_data)
    elif text == "/help":
        reply = await _handle_help_command(chat_id)
    elif text == "/paket":
        reply = await _handle_paket_command(chat_id)
    elif text == "/status":
        reply = await _handle_status_command(chat_id)
    elif text == "/wissen":
        reply = await _handle_wissen_command(chat_id)
    elif text == "/wissen_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_wissen_anzeigen(chat_id)
    elif text == "/wissen_loeschen":
        reply = await _handle_wissen_loeschen_command(chat_id)
    elif text == "/kalkulation":
        reply = await _handle_kalkulation_command(chat_id)
    elif text == "/kalkulation_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_kalkulation_anzeigen(chat_id)
    elif text == "/kalkulation_loeschen":
        reply = await _handle_kalkulation_loeschen_command(chat_id)
    elif text == "/kalkulation_excel":
        reply = await _handle_kalkulation_excel_command(chat_id)
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
    elif text == "/angebot":
        reply = await _handle_angebot_command(chat_id)
    elif text == "/auftraege":
        await _clear_state(chat_id)
        reply = await _handle_auftraege_command(chat_id)
    elif text.startswith("/auftrag_"):
        # Detail-Ansicht: /auftrag_<8-hex> — sendet Buttons direkt, returnt None
        await _clear_state(chat_id)
        id_prefix = text[len("/auftrag_"):].strip().lower()
        reply_or_none = await _handle_auftrag_show_command(chat_id, id_prefix)
        if reply_or_none is None:
            return {"ok": True}
        reply = reply_or_none
    elif text == "/formular":
        reply = await _handle_formular_command(chat_id)
    elif text == "/formular_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_formular_anzeigen_command(chat_id)
    elif text == "/formular_zuruecksetzen":
        reply = await _handle_formular_zuruecksetzen_command(chat_id)
    elif text == "/material":
        await _clear_state(chat_id)
        reply = await _handle_material_list_command(chat_id)
    elif text == "/material neu" or text == "/material_neu":
        reply = await _handle_material_neu_command(chat_id)
    elif text.startswith("/material "):
        await _clear_state(chat_id)
        args = text[len("/material "):].strip()
        reply = await _handle_material_command(chat_id, args)
    elif text == "/bestellen":
        await _clear_state(chat_id)
        reply = await _handle_bestellen_list_command(chat_id)
    elif text.startswith("/bestellen "):
        args = text[len("/bestellen "):].strip()
        reply = await _handle_bestellen_command(chat_id, args)
    elif text == "/bestellungen":
        await _clear_state(chat_id)
        reply = await _handle_bestellungen_list_command(chat_id)
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
        reply_or_none = await _handle_briefing_command(chat_id)
        if reply_or_none is None:
            return {"ok": True}
        reply = reply_or_none
    elif text.startswith("/briefing_"):
        # Detail-Ansicht eines einzelnen Termins per ID-Prefix
        await _clear_state(chat_id)
        id_prefix = text[len("/briefing_"):].strip().lower()
        reply_or_none = await _handle_briefing_show_command(chat_id, id_prefix)
        if reply_or_none is None:
            return {"ok": True}
        reply = reply_or_none
    elif text == "/anrufe":
        await _clear_state(chat_id)
        reply = await _handle_anrufe_command(chat_id)
    elif text.startswith("/kunde"):
        await _clear_state(chat_id)
        # Argumente nach '/kunde' extrahieren
        args = text[len("/kunde"):].strip()
        reply_or_none = await _handle_kunde_command(chat_id, args)
        if reply_or_none is None:
            return {"ok": True}
        reply = reply_or_none
    elif text == "/rechnungen_anzeigen":
        await _clear_state(chat_id)
        reply = await _handle_rechnungen_anzeigen_command(chat_id)
    elif text == "/rechnung_pruefen":
        await _clear_state(chat_id)
        reply = await _handle_rechnung_pruefen_command(chat_id)
    elif text == "/werkstatt":
        reply = await _handle_werkstatt_command(chat_id)
    elif text == "/werkstatt_status":
        reply = await _handle_werkstatt_status_command(chat_id)
    elif text == "/mitarbeiter" or text.startswith("/mitarbeiter "):
        await _clear_state(chat_id)
        reply = await _handle_mitarbeiter_command(chat_id, text)
    elif text == "/kalender_verbinden" or text == "/kalender":
        await _clear_state(chat_id)
        # Send-and-return weil Wizard direkt per Inline-Buttons antwortet.
        await _handle_kalender_verbinden_command(chat_id)
        return {"ok": True}
    elif text == "/kalender_status":
        reply = await _handle_kalender_status_command(chat_id)
    elif text == "/drive_verbinden":
        await _clear_state(chat_id)
        reply_or_none = await _handle_drive_verbinden_command(chat_id)
        if reply_or_none is None:
            return {"ok": True}
        reply = reply_or_none
    elif text == "/drive_status":
        reply = await _handle_drive_status_command(chat_id)
    elif text == "/archiv":
        await _clear_state(chat_id)
        reply = await _handle_archiv_list_command(chat_id)
    elif text.startswith("/archiv "):
        args = text[len("/archiv "):].strip()
        reply = await _handle_archiv_command(chat_id, args)
    elif text == "/fertig":
        # /fertig ist nur sinnvoll im Archiv-Wizard. Sonst freundlicher Hinweis.
        state = await _load_state(chat_id)
        if state and state.state_key == STATE_ARCHIV_WAITING_FILES:
            reply_or_none = await _handle_archiv_fertig_command(
                chat_id, state.state_data,
            )
            if reply_or_none is None:
                # Inline-Button wurde schon gesendet
                return {"ok": True}
            reply = reply_or_none
        else:
            reply = (
                "/fertig hat hier keine Wirkung. Im /archiv-Wizard schliesst "
                "es den Upload ab."
            )
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
        elif state.state_key == STATE_KALK_KATEGORIE:
            reply = await _handle_kalk_kategorie_input(chat_id, text)
        elif state.state_key == STATE_KALK_NAME:
            reply = await _handle_kalk_name_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_KALK_FORMEL:
            reply = await _handle_kalk_formel_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_KALK_EINHEIT:
            reply = await _handle_kalk_einheit_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_KALK_BESCHREIBUNG:
            reply = await _handle_kalk_beschreibung_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_KALK_LOESCHEN:
            reply = await _handle_kalk_loeschen_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_KALK_EXCEL_WAITING:
            reply = "Bitte schicke eine .xlsx-Datei (kein Text). Oder /abbrechen."
        elif state.state_key == STATE_KALK_EXCEL_CONFIRM:
            reply = await _handle_kalk_excel_confirm_input(chat_id, text, state.state_data)
        elif state.state_key == STATE_VIZ_WAITING_PHOTO:
            reply = "Bitte schicken Sie ein Foto (kein Text). Oder /abbrechen."
        elif state.state_key == STATE_VIZ_POST_ACTION:
            reply = (
                "Bitte einen der Buttons oben antippen "
                "(Mail / Drive / Fertig) oder /abbrechen schicken."
            )
        elif state.state_key == STATE_VIZ_POST_MAIL_EMAIL:
            reply_or_none = await _handle_viz_mail_email_input(
                chat_id, text, state.state_data,
            )
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_VIZ_POST_DRIVE_KUNDE:
            reply_or_none = await _handle_viz_drive_kunde_input(
                chat_id, text, state.state_data,
            )
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_VIZ_WAITING_DESCRIPTION:
            reply_or_none = await _handle_viz_description_input(
                chat_id, text, state.state_data,
            )
            if reply_or_none is None:
                # Inline-Buttons schon gesendet (Bild + Post-Action-Wahl)
                return {"ok": True}
            reply = reply_or_none
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
        elif state.state_key == STATE_ANGEBOT_WAITING_INPUT:
            # Text-Eingabe waehrend Angebot-Wizard (Voice geht weiter oben).
            await _send_to_chat(chat_id, "⏳ Verstehe deine Eingabe…")
            reply_or_none = await _handle_angebot_input_received(
                chat_id, text=text,
            )
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_ANGEBOT_PREVIEWING:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_ANGEBOT_AWAITING_INSTRUCTIONS:
            # Text-Anweisungen fuers Anschreiben (Voice oben)
            await _send_to_chat(chat_id, "⏳ Schreibe das Anschreiben…")
            reply_or_none = await _handle_angebot_instructions_received(
                chat_id, text=text,
            )
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
        elif state.state_key == STATE_ANGEBOT_AWAITING_KUNDE_NAME:
            # User reicht vollen Namen nach
            reply_or_none = await _handle_angebot_kunde_name_input(chat_id, text)
            if reply_or_none is None:
                return {"ok": True}
            reply = reply_or_none
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
        elif state.state_key == STATE_MATERIAL_NEU_NAME:
            reply = await _handle_material_neu_name_input(chat_id, text)
        elif state.state_key == STATE_MATERIAL_NEU_LINK:
            reply = await _handle_material_neu_link_input(
                chat_id, text, state.state_data,
            )
        elif state.state_key == STATE_MATERIAL_NEU_LIEFERANT:
            reply = await _handle_material_neu_lieferant_input(
                chat_id, text, state.state_data,
            )
        elif state.state_key == STATE_MATERIAL_NEU_PREVIEWING:
            reply = "Bitte einen der Buttons oben antippen oder /abbrechen schicken."
        elif state.state_key == STATE_BESTELLEN_MENGE:
            reply = await _handle_bestellen_menge_input(
                chat_id, text, state.state_data,
            )
        elif state.state_key == STATE_ARCHIV_WAITING_FILES:
            kunde = (state.state_data or {}).get("kunde_name") or "diesen Kunden"
            reply = (
                f"Schick mir Bilder, PDFs oder andere Dokumente fuer "
                f"<b>{_h_safe(kunde)}</b>. Mit <b>/fertig</b> abschliessen "
                f"oder /abbrechen."
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
        return "Erst /start ausfuehren — Chat ist keinem Betrieb zugeordnet."
    await _save_state(chat_id, STATE_VIZ_WAITING_PHOTO, {})
    return (
        "<b>🎨 Visualisierung</b>\n"
        "Schick ein Foto der Stelle (Treppe, Bad, Wand, ...)."
    )


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

    return (
        f"Foto erhalten ({len(image_bytes) // 1024} KB).\n\n"
        "<b>Was soll dort hin?</b>\n"
        "<i>z.B. Helle Eichentreppe mit Edelstahl-Gelaender</i>"
    )


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

    # Post-Action-Buttons: Mail / Drive / Fertig.
    # State STATE_VIZ_POST_ACTION laesst /abbrechen funktionieren und
    # speichert viz_id + Bild-Bytes-Verfuegbarkeit (Bild kommt aus DB
    # bei Bedarf, kein Re-Download).
    await _save_state(
        chat_id,
        STATE_VIZ_POST_ACTION,
        {"viz_id": viz_id, "prompt": text},
    )
    await _send_with_inline_buttons(
        chat_id,
        "Was soll mit dem Bild passieren?",
        [
            [
                {"text": "📧 Per Mail senden", "callback_data": f"viz:mail:{viz_id}"},
                {"text": "☁️ Im Drive ablegen", "callback_data": f"viz:drive:{viz_id}"},
            ],
            [
                {"text": "✅ Fertig (nichts mehr)", "callback_data": f"viz:done:{viz_id}"},
            ],
        ],
    )
    return None  # type: ignore[return-value]


# =====================================================================
# Visualisierung — Post-Action (Mail + Drive)
# =====================================================================
# Nach dem Bild-Versand bekommt der User Inline-Buttons zum
# weiterverarbeiten:
#   📧 Per Mail senden  → State STATE_VIZ_POST_MAIL_EMAIL
#                         User schickt Email-Adresse
#                         Bot mailt das Bild als Anhang via Brevo
#   ☁️ Im Drive ablegen → State STATE_VIZ_POST_DRIVE_KUNDE
#                         User schickt Kunden-Name
#                         Bot uploadet via google_drive-Helper
#   ✅ Fertig             clear_state — kein Versand
#
# Beide Aktionen koennen nacheinander ausgefuehrt werden: nach Mail
# bekommt der User wieder die Buttons (Drive ist dann noch sinnvoll),
# umgekehrt analog.


async def _handle_viz_callback(chat_id, callback_data, callback_query_id, bot_token):
    """Verarbeitet Klicks auf die viz:-Inline-Buttons."""
    parts = callback_data.split(":", 2)
    if len(parts) != 3:
        await _answer_callback_query(callback_query_id, "Falsches Format", bot_token)
        return
    _, action, viz_id = parts

    if action == "done":
        await _answer_callback_query(callback_query_id, "Fertig", bot_token)
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            "Mit /visualisierung kannst du eine weitere Visualisierung starten.",
        )
        return

    if action == "mail":
        await _save_state(
            chat_id, STATE_VIZ_POST_MAIL_EMAIL, {"viz_id": viz_id},
        )
        await _answer_callback_query(callback_query_id, "Email eingeben", bot_token)
        await _send_to_chat(
            chat_id,
            "<b>📧 Per Mail senden</b>\n\n"
            "An welche <b>Email-Adresse</b> soll das Bild gehen?\n\n"
            "<i>Beispiel: kunde@beispiel.de</i>\n\n"
            "/abbrechen um stattdessen nichts zu schicken.",
        )
        return

    if action == "drive":
        await _save_state(
            chat_id, STATE_VIZ_POST_DRIVE_KUNDE, {"viz_id": viz_id},
        )
        await _answer_callback_query(callback_query_id, "Kunden-Name eingeben", bot_token)
        await _send_to_chat(
            chat_id,
            "<b>☁️ Im Drive ablegen</b>\n\n"
            "Fuer welchen <b>Kunden</b> soll das Bild archiviert werden?\n"
            "Schick mir einfach den Kunden-Namen.\n\n"
            "<i>Beispiel: Mueller</i>\n\n"
            "/abbrechen um stattdessen nicht zu archivieren.",
        )
        return

    await _answer_callback_query(callback_query_id, "Unbekannte Aktion", bot_token)


async def _show_viz_post_action_buttons(chat_id, viz_id, prompt_text):
    """Zeigt die Inline-Buttons erneut (z.B. nach erfolgreicher Aktion).

    Damit kann der User Mail UND Drive nacheinander machen.
    """
    await _save_state(
        chat_id, STATE_VIZ_POST_ACTION,
        {"viz_id": viz_id, "prompt": prompt_text},
    )
    await _send_with_inline_buttons(
        chat_id,
        "Was soll noch passieren?",
        [
            [
                {"text": "📧 Per Mail senden", "callback_data": f"viz:mail:{viz_id}"},
                {"text": "☁️ Im Drive ablegen", "callback_data": f"viz:drive:{viz_id}"},
            ],
            [
                {"text": "✅ Fertig (nichts mehr)", "callback_data": f"viz:done:{viz_id}"},
            ],
        ],
    )


async def _load_viz_image(viz_id_str: str) -> tuple[bytes | None, str | None]:
    """Liest result_image_data + prompt aus der DB. Failsafe."""
    import uuid as _uuid
    try:
        vid = _uuid.UUID(viz_id_str)
    except (ValueError, TypeError):
        return None, None
    async with AsyncSessionLocal() as s:
        viz = (await s.execute(
            select(Visualisierung).where(Visualisierung.id == vid)
        )).scalar_one_or_none()
        if viz is None or not viz.result_image_data:
            return None, None
        return viz.result_image_data, viz.prompt or ""


async def _viz_queue_for_retry(
    *,
    tenant_id,
    viz_id: str,
    recipient_email: str,
    subject: str,
    html_body: str,
    image_bytes: bytes,
    from_name: str,
    reply_to: str,
    reply_to_name: str,
    last_error: str,
) -> None:
    """Beta-1 B1-4: Visualisierungs-Mail in failed_mail_queue + Status.

    Failsafe — Caller-Branch ist schon im Exception-Handler.
    """
    try:
        from core.integrations.mail_retry_cron import enqueue_failed_mail
        from core.models import (
            MAIL_TYPE_VISUALISIERUNG, VIZ_STATUS_MAIL_QUEUED,
        )
        # Status auf 'mail_queued' setzen damit Tenant es in /briefing /
        # Admin-UI sieht
        try:
            import uuid as _uuid_local
            async with AsyncSessionLocal() as s:
                viz = (await s.execute(
                    select(Visualisierung)
                    .where(Visualisierung.id == _uuid_local.UUID(viz_id))
                )).scalar_one_or_none()
                if viz:
                    viz.status = VIZ_STATUS_MAIL_QUEUED
                    viz.kunde_email = recipient_email
                    await s.commit()
        except Exception as exc:
            logger.warning(f"viz-status auf MAIL_QUEUED setzen ignored: {exc}")

        # Queue-Insert
        await enqueue_failed_mail(
            tenant_id=tenant_id,
            mail_type=MAIL_TYPE_VISUALISIERUNG,
            recipient_email=recipient_email,
            subject=subject,
            html_body=html_body,
            attachments=[{
                "filename": "visualisierung.jpg",
                "mime_type": "image/jpeg",
                "content_bytes": image_bytes,
            }],
            from_name=from_name,
            reply_to=reply_to,
            reply_to_name=reply_to_name,
            viz_id=viz_id,
            last_error=last_error[:500],
        )
    except Exception as exc:
        logger.exception(f"_viz_queue_for_retry crashed: {exc}")


async def _handle_viz_mail_email_input(chat_id, text, state_data):
    """User schickt Email-Adresse → wir mailen das Bild als Anhang."""
    email = (text or "").strip().lower()
    # Sehr einfache Email-Validierung
    if "@" not in email or "." not in email.split("@", 1)[1] or " " in email:
        return (
            "Das sieht nicht wie eine Email-Adresse aus. "
            "Bitte erneut oder /abbrechen."
        )
    if len(email) > 254:
        return "Email-Adresse ist zu lang."

    viz_id = (state_data or {}).get("viz_id") or ""
    image_bytes, prompt = await _load_viz_image(viz_id)
    if image_bytes is None:
        await _clear_state(chat_id)
        return (
            "Bild nicht mehr verfuegbar. "
            "Bitte mit /visualisierung neu starten."
        )

    tenant = await _get_tenant_by_chat(chat_id)
    if tenant is None:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden — bitte /start ausfuehren."

    # Brevo-Config aus globalem Tenant laden (gleicher Pattern wie Rechnung)
    async with AsyncSessionLocal() as s:
        tc = (await s.execute(
            select(ToolConfig)
            .join(Tenant, ToolConfig.tenant_id == Tenant.id)
            .where(
                Tenant.slug == GLOBAL_TENANT_SLUG,
                ToolConfig.tool_name == "mail_intake",
            )
        )).scalar_one_or_none()
        if not tc or not tc.config:
            await _clear_state(chat_id)
            return (
                "Mail-Konfiguration fehlt — bitte den Betreiber kontaktieren."
            )
        cfg = tc.config or {}
        brevo_api_key = cfg.get("brevo_api_key")
        sender_email = cfg.get("sender_email")
        sender_name = cfg.get("sender_name") or tenant.company_name

    if not all([brevo_api_key, sender_email]):
        await _clear_state(chat_id)
        return "Mail-Konfiguration unvollstaendig — bitte Betreiber kontaktieren."

    # Tenant-Kontaktdaten
    tenant_company = tenant.company_name
    tenant_contact_name = tenant.contact_name
    tenant_contact_email = tenant.contact_email

    await _send_to_chat(chat_id, "<i>Bild wird versendet...</i>")

    # Mail bauen + senden
    from core.integrations.brevo import (
        BrevoMailer, MailRecipient, MailAttachment, BrevoError,
    )

    subject = f"Visualisierung von {tenant_company}"
    intro = (
        f"<p>Hallo,</p>"
        f"<p>anbei die Visualisierung wie wir besprochen haben.</p>"
    )
    if prompt:
        intro += f"<p><i>Beschreibung: {_h_safe(prompt)}</i></p>"
    intro += (
        f"<p>Bei Fragen koennen Sie diese Mail einfach beantworten.</p>"
        f"<p>Mit freundlichen Gruessen<br>"
        f"{_h_safe(tenant_contact_name)}<br>"
        f"{_h_safe(tenant_company)}</p>"
    )

    mailer = BrevoMailer(api_key=brevo_api_key)
    filename = "visualisierung.jpg"

    try:
        result = await mailer.send(
            sender_email=sender_email,
            sender_name=sender_name,
            to=MailRecipient(email=email),
            subject=subject,
            html_body=intro,
            reply_to_email=tenant_contact_email,
            reply_to_name=tenant_contact_name,
            attachments=[MailAttachment(
                filename=filename,
                content_bytes=image_bytes,
                content_type="image/jpeg",
            )],
            tenant_id=str(tenant.id),
        )
        msg_id = result.get("messageId", "?")
        logger.info(f"Visualisierung-Mail an {email} gesendet: {msg_id}")
        # Status auf 'sent' setzen (war vorher implizit nirgendwo gesetzt
        # — die Wizard-Logik hat die Viz nach Erfolg nie auf SENT
        # gehoben. Beta-1 macht das jetzt explizit damit /briefing &
        # Admin-UI konsistent sind).
        try:
            from core.models import VIZ_STATUS_SENT
            async with AsyncSessionLocal() as s:
                import uuid as _uuid_for_viz
                viz = (await s.execute(
                    select(Visualisierung).where(Visualisierung.id == _uuid_for_viz.UUID(viz_id))
                )).scalar_one_or_none()
                if viz:
                    viz.status = VIZ_STATUS_SENT
                    viz.kunde_email = email
                    await s.commit()
        except Exception as exc_status:
            logger.exception(f"viz-status auf SENT setzen failed (egal): {exc_status}")
    except BrevoError as e:
        # Beta-1 B1-4: in Retry-Queue legen statt verlorengehen lassen
        logger.exception(f"Visualisierung-Mail fehlgeschlagen — in Queue: {e}")
        await _viz_queue_for_retry(
            tenant_id=tenant.id, viz_id=viz_id, recipient_email=email,
            subject=subject, html_body=intro, image_bytes=image_bytes,
            from_name=sender_name, reply_to=tenant_contact_email,
            reply_to_name=tenant_contact_name, last_error=str(e),
        )
        await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
        return (
            "⚠️ Mail-Versand verzoegert (HTTP "
            f"{e.status_code or '?'}). Wird automatisch in 5 Min "
            "wiederholt — kein erneutes Versenden noetig."
        )
    except Exception as e:
        logger.exception(f"Visualisierung-Mail unbekannter Fehler: {e}")
        # Auch hier in die Queue — bei unbekanntem Fehler genauso wert
        # nachzuversuchen
        await _viz_queue_for_retry(
            tenant_id=tenant.id, viz_id=viz_id, recipient_email=email,
            subject=subject, html_body=intro, image_bytes=image_bytes,
            from_name=sender_name, reply_to=tenant_contact_email,
            reply_to_name=tenant_contact_name, last_error=str(e),
        )
        await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
        return (
            "⚠️ Mailversand fehlgeschlagen. Wird automatisch wiederholt."
        )

    # Erfolgs-Bestaetigung + Buttons fuer naechste Aktion (Drive)
    await _send_to_chat(
        chat_id,
        f"✅ <b>Mail an {_h_safe(email)} gesendet.</b>",
    )
    await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
    return None  # type: ignore[return-value]


async def _handle_viz_drive_kunde_input(chat_id, text, state_data):
    """User schickt Kunden-Name → Bild ins Drive-Archiv des Kunden uploaden."""
    kunde_name = (text or "").strip()
    if len(kunde_name) < 2:
        return (
            "Kunden-Name ist zu kurz. Bitte erneut oder /abbrechen."
        )
    if len(kunde_name) > 200:
        return "Kunden-Name ist zu lang (max 200 Zeichen)."

    viz_id = (state_data or {}).get("viz_id") or ""
    image_bytes, prompt = await _load_viz_image(viz_id)
    if image_bytes is None:
        await _clear_state(chat_id)
        return (
            "Bild nicht mehr verfuegbar. "
            "Bitte mit /visualisierung neu starten."
        )

    res = await _get_current_employee(chat_id)
    if res is None:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden — bitte /start ausfuehren."
    tenant, emp = res

    # Drive-Verbindung pruefen
    from core.security.oauth_token_lookup import find_oauth_token
    from core.integrations.google_drive import (
        is_drive_configured, upload_file_to_kunde_folder,
    )
    tok = await find_oauth_token(tenant.id, "google", emp.id)
    if not tok or not is_drive_configured(tok):
        # Buttons zurueck damit User stattdessen Mail probieren kann
        await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
        return (
            "⚠️ <b>Drive ist noch nicht verbunden.</b>\n\n"
            "Bitte einmal /drive_verbinden ausfuehren — danach klappt "
            "auch das Archivieren direkt aus der Visualisierung."
        )

    await _send_to_chat(chat_id, "<i>Bild wird in Drive abgelegt...</i>")

    # Filename aus Prompt + Timestamp ableiten
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_prompt = (prompt or "visualisierung")[:40]
    # Filename-sichere Zeichen
    safe_prompt = "".join(
        c if (c.isalnum() or c in "-_") else "_" for c in safe_prompt
    )
    filename = f"viz_{ts}_{safe_prompt}.jpg"

    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tenant.id,
            kunde_name=kunde_name,
            file_bytes=image_bytes,
            filename=filename,
            mime_type="image/jpeg",
            employee_id=emp.id,
        )
    except ValueError as e:
        logger.warning(f"Drive-Upload aus Viz fehlgeschlagen: {e}")
        await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
        return (
            f"⚠️ <b>Drive-Upload fehlgeschlagen</b>\n\n"
            f"{_h_safe(str(e))}\n\n"
            "Bitte ggf. /drive_verbinden ausfuehren."
        )
    except Exception as e:
        err = str(e)
        logger.exception(f"Drive-Upload aus Viz: {err[:200]}")
        await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
        if "quotaExceeded" in err or "storageQuotaExceeded" in err:
            hint = "Drive-Speicher voll. Bitte aufraeumen oder Plan upgraden."
        else:
            hint = "Unerwarteter Drive-Fehler. Bitte erneut versuchen."
        return f"⚠️ <b>Upload fehlgeschlagen</b>\n\n{hint}"

    folder_url = result.get("kunde_folder_url") or ""
    upload_count = result.get("upload_count", 0)

    # Erfolgs-Nachricht mit Drive-Link-Button
    success_msg = (
        f"✅ <b>{_h_safe(filename)}</b> abgelegt "
        f"(insgesamt {upload_count} fuer {_h_safe(kunde_name)})"
    )
    if folder_url:
        await _send_with_inline_buttons(
            chat_id, success_msg,
            [[{"text": f"📁 Drive-Ordner: {kunde_name}", "url": folder_url}]],
        )
    else:
        await _send_to_chat(chat_id, success_msg)

    # Buttons wieder zeigen — User kann jetzt noch Mail schicken
    await _show_viz_post_action_buttons(chat_id, viz_id, prompt or "")
    return None  # type: ignore[return-value]


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
            "\n\n<i>ℹ️ Lexware ist schon verbunden — ein neuer Schluessel "
            "ueberschreibt den alten.</i>"
        )

    await _save_state(chat_id, STATE_LEXWARE_SETUP_TOKEN, {})
    msg = (
        "🧾 <b>Lexware verbinden</b>\n\n"
        "Damit ich Angebote und Rechnungen fuer dich in Lexware "
        "anlegen kann, brauche ich einmal deinen API-Schluessel.\n\n"
        "<b>So bekommst du ihn — 3 Schritte:</b>\n\n"
        "1️⃣ Diesen Link in deinem Browser oeffnen:\n"
        "👉 <a href=\"https://app.lexware.de/permalink/profile/api-keys\">"
        "app.lexware.de/permalink/profile/api-keys</a>\n"
        "<i>(Falls dein Lexware-Login fragt: einloggen wie immer.)</i>\n\n"
        "2️⃣ Auf den blauen Button "
        "<b>«Neuen API-Schluessel erstellen»</b> klicken.\n"
        "<i>Bei Bezeichnung kannst du <b>Gewerbeagent</b> eintragen.</i>\n\n"
        "3️⃣ Der Schluessel erscheint einmalig — auf <b>«Kopieren»</b> "
        "tippen und hier in den Chat einfuegen ✏️\n\n"
        "<i>⚠️ Den Schluessel siehst du nur einmal — also direkt hier "
        "einkleben oder kurz notieren.</i>\n\n"
        "<i>🔒 Bei mir wird der Schluessel verschluesselt gespeichert. "
        "Du kannst ihn jederzeit in Lexware widerrufen.</i>\n\n"
        f"Mit /abbrechen verwirfst du den Vorgang.{bereits_eingerichtet}"
    )
    return msg


async def _handle_lexware_setup_token_input(chat_id, text):
    """User schickt den API-Schluessel als Text."""
    api_key = (text or "").strip()
    # Plausi: Lexware-Keys sind alphanumerische Strings (~48+ Zeichen).
    # Sven-feedback-freundliche Fehler statt technischer Zahlen.
    if len(api_key) < 20:
        return (
            "🤔 Das sieht zu kurz aus fuer einen Lexware-Schluessel.\n\n"
            "Hast du den ganzen Schluessel kopiert? Er ist normalerweise "
            "ein langer Buchstaben-Zahlen-Mix.\n\n"
            "Nochmal probieren oder /abbrechen."
        )
    if len(api_key) > 200:
        return (
            "🤔 Das ist viel zu lang fuer einen API-Schluessel.\n\n"
            "Bitte nur den Schluessel einfuegen — keinen Begleittext.\n\n"
            "Nochmal probieren oder /abbrechen."
        )
    if " " in api_key or "\n" in api_key:
        return (
            "🤔 Da sind Leerzeichen oder Zeilenumbrueche drin.\n\n"
            "Bitte den Schluessel <b>am Stueck</b> kopieren und einfuegen — "
            "manche Browser haengen Whitespace mit dran.\n\n"
            "Nochmal probieren oder /abbrechen."
        )

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Chat ist keinem Betrieb zugeordnet — erst /start ausfuehren."

    # Live-Test: Health-Check gegen Lexware
    try:
        provider = LexwareProvider(api_key=api_key)
        profile = await provider.health_check()
    except AccountingError as e:
        await _clear_state(chat_id)
        if e.status_code == 401:
            return (
                "🔒 <b>Lexware sagt: Schluessel ungueltig.</b>\n\n"
                "Moegliche Gruende:\n"
                "  • Schluessel ist abgelaufen oder wurde geloescht\n"
                "  • Beim Kopieren ist ein Zeichen verlorengegangen\n\n"
                "Loesung: einen neuen Schluessel erstellen unter\n"
                "👉 <a href=\"https://app.lexware.de/permalink/profile/api-keys\">"
                "app.lexware.de/permalink/profile/api-keys</a>\n\n"
                "Dann nochmal /lexware_setup."
            )
        return (
            f"⚠️ Lexware antwortet gerade nicht (Fehler {e.status_code}).\n\n"
            "Vielleicht eine kurze Stoerung — in ein paar Minuten "
            "nochmal /lexware_setup probieren."
        )
    except Exception as e:
        logger.exception(f"Lexware-Setup unerwarteter Fehler: {e}")
        await _clear_state(chat_id)
        return (
            "⚠️ Verbindung zu Lexware hat nicht geklappt.\n\n"
            "Internet kurz pruefen und nochmal /lexware_setup probieren. "
            "Falls es immer wieder schiefgeht: bei svenj05@gmx.de melden."
        )

    # Verschluesselt speichern
    encrypted = encrypt(api_key)
    organization_id = profile.get("organizationId") if isinstance(profile, dict) else None
    await _save_lexware_config(tenant.id, encrypted, organization_id)
    await _clear_state(chat_id)

    msg = (
        "✅ <b>Lexware ist verbunden!</b>\n\n"
        "Du kannst jetzt:\n"
        "  • <b>/angebot</b> — Angebot diktieren, automatisch in Lexware "
        "anlegen + per Mail an den Kunden\n"
        "  • <b>/beleg</b> — Foto eines Belegs schicken, kommt direkt "
        "in Lexware\n"
        "  • <b>/rechnung</b> — Rechnung diktieren, Lexware-Draft zum "
        "Abnicken\n"
        "  • <b>/auftraege</b> — laufende Projekte mit Status + "
        "Auto-Rechnungs-Versand bei Beendet\n\n"
        "Status spaeter pruefen mit <b>/lexware_status</b>."
    )
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
            # Unterstuetzt sowohl callback_data (interner State) als auch
            # url (oeffnet Webseite — z.B. fuer Bestell-Buttons).
            if btn.get("url"):
                kb_row.append({"text": btn["text"], "url": btn["url"]})
            else:
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


async def _drive_link_section(tenant_id, kunde_name) -> tuple[str, str | None]:
    """Bereite den Drive-Link-Block fuer /briefing + /kunde vor.

    Returns: (text_block, folder_url | None)
      - folder_url=None: noch kein Ordner, text_block ist Hinweis
      - folder_url=str: Ordner vorhanden, text_block ist HTML-<a>-Link
    Failsafe: bei Fehlern leerer Text + None.
    """
    try:
        from core.integrations.google_drive import get_kunde_folder_link
        url = await get_kunde_folder_link(tenant_id, kunde_name)
    except Exception as e:
        logger.debug(f"_drive_link_section failed (egal): {e}")
        return "", None

    if url:
        return f"\n\n📁 <a href=\"{url}\">Drive-Ordner oeffnen</a>", url

    # Kein Ordner -> Hinweis (kein Button, nur Text-Tipp)
    return (
        f"\n\n📁 <i>Noch kein Drive-Ordner — mit "
        f"<code>/archiv {_h_safe(kunde_name)}</code> Bilder/PDFs ablegen.</i>",
        None,
    )


async def _send_kundengespraech_with_drive(
    chat_id, header: str, gespraech, tenant_id,
) -> str | None:
    """Hilfsfunktion: formatiert Briefing + Drive-Link, sendet ggf. mit Button.

    Returns:
        - str (final message) wenn Caller via _send_to_chat senden soll
        - None wenn schon mit Inline-Button gesendet (Caller short-circuit)
    """
    base = header + _format_kundengespraech_full(gespraech)
    drive_block, folder_url = await _drive_link_section(
        tenant_id, gespraech.kunde_name or "",
    )
    msg = base + drive_block

    if folder_url:
        sent = await _send_with_inline_buttons(
            chat_id, msg,
            [[{
                "text": f"📁 Drive-Ordner: {gespraech.kunde_name or 'Kunde'}",
                "url": folder_url,
            }]],
        )
        if sent:
            return None
        # Inline-Button-Send fehlgeschlagen -> Plain-Text-Fallback
    return msg


async def _fetch_calendar_events_for_day(tenant, employee, target_date):
    """Holt Kalender-Events fuer den Tag vom GEWAEHLTEN Provider
    (employee.calendar_provider). Funktioniert symmetrisch fuer Outlook
    UND Google — je nachdem was der Handwerker via /kalender_verbinden
    ausgewaehlt hat.

    Returns:
        (events_list, provider_label)
        events_list: Liste von Event-dicts wie list_events_for_day liefert
        provider_label: "Outlook" | "Google" | None (None = nicht verbunden)

    Failsafe: API-Fehler -> leere Liste, der Caller faellt auf
    Kundengespraech-Termine zurueck.
    """
    if not employee or not employee.calendar_provider:
        return [], None

    provider = employee.calendar_provider
    try:
        if provider == "microsoft":
            from core.integrations.microsoft_calendar import list_events_for_day
            evs = await list_events_for_day(
                tenant.id, target_date, employee_id=employee.id,
            )
            return evs, "Outlook"
        if provider == "google":
            from core.integrations.google_calendar import list_events_for_day
            evs = await list_events_for_day(
                tenant.id, target_date, employee_id=employee.id,
            )
            return evs, "Google"
    except Exception as exc:
        logger.exception(f"Calendar-Fetch ({provider}) gescheitert: {exc}")

    # Unknown Provider oder Fehler — leer aber Provider-Label fuer
    # Diagnostik mitgeben
    label = "Outlook" if provider == "microsoft" else (
        "Google" if provider == "google" else provider
    )
    return [], label


def _match_kundengespraech_for_subject(subject: str, gespraeche: list) -> object | None:
    """Verbindet einen Kalender-Event-Subject mit einem Kundengespraech
    per Substring-Match auf kunde_name.

    Beispiel: Subject "Termin Müller — Parkett" matched ein Gespraech
    mit kunde_name "Frau Mueller". Case-insensitiv, normalisiert.
    """
    if not subject or not gespraeche:
        return None
    subject_lower = subject.lower()
    # 1) exact substring kunde_name in subject
    for g in gespraeche:
        if not g.kunde_name:
            continue
        if g.kunde_name.lower() in subject_lower:
            return g
    # 2) Token-Match: irgendein Wort aus kunde_name (>= 3 Buchstaben) im subject
    for g in gespraeche:
        if not g.kunde_name:
            continue
        for token in g.kunde_name.split():
            t = token.lower()
            if len(t) >= 3 and t in subject_lower:
                return g
    return None


async def _handle_briefing_command(chat_id):
    """Zeigt alle Termine fuer HEUTE als Liste — aus dem verbundenen
    Kalender (Google/Outlook) UND aus Kundengespraechen mit termin_datum
    heute, dedupliziert und nach Uhrzeit sortiert.

    Pro Termin: Uhrzeit, Subject (oder Kundenname), Ort, klickbarer
    Detail-Befehl fuer das matchende Kundengespraech wenn vorhanden.
    Sonst zeigt der Eintrag nur die Kalender-Daten — der User kann
    via /kunde <name> manuell mehr Infos holen.

    Wenn nichts heute: zeigt naechste 7 Tage aus den Kundengespraechen
    als Trost-Block. Falls auch das leer: juengstes Gespraech.

    Phase-4-Multi-Mitarbeiter: Default-Employee sieht alle Termine,
    Nicht-Default nur seine zugewiesenen.
    """
    from datetime import datetime, timezone, timedelta

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo("Europe/Berlin")
    except Exception:
        local_tz = timezone.utc
    now_local = datetime.now(local_tz)
    today_date = now_local.date()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    week_end = today_start + timedelta(days=7)

    # 1) Kundengespraeche mit termin_datum heute / kommende Woche
    async with AsyncSessionLocal() as s:
        base = (
            select(Kundengespraech)
            .where(
                Kundengespraech.tenant_id == tenant.id,
                Kundengespraech.termin_datum.is_not(None),
            )
            .order_by(Kundengespraech.termin_datum.asc())
        )
        if not current_emp.is_default:
            base = base.where(
                Kundengespraech.assigned_employee_id == current_emp.id
            )
        gespraeche_heute = (await s.execute(
            base.where(
                Kundengespraech.termin_datum >= today_start,
                Kundengespraech.termin_datum < today_end,
            )
        )).scalars().all()
        gespraeche_kommend = (await s.execute(
            base.where(
                Kundengespraech.termin_datum >= today_end,
                Kundengespraech.termin_datum < week_end,
            ).limit(5)
        )).scalars().all()

    # 2) Kalender-Events heute (gewaehlter Provider — Outlook oder Google)
    cal_events, provider_label = await _fetch_calendar_events_for_day(
        tenant, current_emp, today_date,
    )

    # 3) Merge: Eintraege bauen mit Quelle "cal" oder "ges"
    # Vermeide Duplikate: wenn Kalender-Event und Gespraech aufs gleiche
    # Zeitfenster + matching Name fallen, zeige nur den Kalender-Eintrag
    # mit verlinktem Gespraech fuer die Details.
    matched_g_ids: set = set()
    eintraege: list[dict] = []
    for ev in cal_events:
        s_dt = ev["start_dt"]
        # naive → mit lokaler tz versehen damit strftime mit %H:%M passt
        if s_dt.tzinfo is None:
            s_dt_local = s_dt.replace(tzinfo=local_tz)
        else:
            s_dt_local = s_dt.astimezone(local_tz)
        matched_g = _match_kundengespraech_for_subject(
            ev.get("subject", ""), gespraeche_heute,
        )
        if matched_g:
            matched_g_ids.add(matched_g.id)
        eintraege.append({
            "uhr": s_dt_local.strftime("%H:%M"),
            "sort_key": s_dt_local,
            "title": ev.get("subject") or "(ohne Titel)",
            "ort": ev.get("location") or "",
            "preview": ev.get("body_preview") or "",
            "matched_g": matched_g,
            "source": "cal",
        })
    for g in gespraeche_heute:
        if g.id in matched_g_ids:
            continue  # schon im Kalender-Eintrag verlinkt
        s_dt_local = g.termin_datum.astimezone(local_tz)
        eintraege.append({
            "uhr": s_dt_local.strftime("%H:%M"),
            "sort_key": s_dt_local,
            "title": g.kunde_name or "(unbekannter Kunde)",
            "ort": g.termin_ort or "",
            "preview": g.briefing_kurz or "",
            "matched_g": g,
            "source": "ges",
        })
    eintraege.sort(key=lambda e: e["sort_key"])

    cal_source_label = ""
    if provider_label and cal_events:
        cal_source_label = f" <i>(aus {provider_label}-Kalender)</i>"

    if eintraege:
        datum_str = today_start.strftime("%A, %d.%m.%Y")
        lines = [f"🔔 <b>Termine heute</b> — {datum_str}{cal_source_label}\n"]
        for e in eintraege:
            ort_suffix = f"  ·  {_h_safe(e['ort'])}" if e["ort"] else ""
            lines.append(
                f"<b>{e['uhr']}</b>  ·  <b>{_h_safe(e['title'])}</b>{ort_suffix}"
            )
            if e["matched_g"]:
                lines.append(
                    f"  Details: /briefing_{str(e['matched_g'].id)[:8]}"
                )
            elif e["source"] == "cal":
                # Kalender-Event ohne gemerktes Gespraech — Tipp:
                # Kunden-Lookup ueber /kunde
                hint_name = (e["title"] or "").split(" — ")[0].split("(")[0].strip()
                if hint_name:
                    lines.append(f"  Kunden-Lookup: /kunde {hint_name}")
            preview = e["preview"]
            if preview:
                if len(preview) > 140:
                    preview = preview[:138] + "..."
                lines.append(f"  <i>{_h_safe(preview)}</i>")
            lines.append("")
        lines.append(
            "<i>Tap auf /briefing_xxxxxxxx fuer Briefing + TODOs + "
            "Drive-Ordner. Bei Kalender-Eintraegen ohne Gespraech: "
            "/kunde &lt;Name&gt; fuer manuelle Suche.</i>"
        )
        if gespraeche_kommend:
            lines.append("")
            lines.append(f"<i>📅 Naechste Tage:</i>")
            for g in gespraeche_kommend:
                d = g.termin_datum.astimezone(local_tz).strftime("%a %d.%m %H:%M")
                lines.append(
                    f"  · {d} — <b>{_h_safe(g.kunde_name)}</b>  "
                    f"/briefing_{str(g.id)[:8]}"
                )
        return "\n".join(lines)

    # Kein Termin heute — Hinweis-Block
    lines = ["📅 <b>Heute kein Termin.</b>"]
    if provider_label:
        lines.append(
            f"<i>(Geprueft: {provider_label}-Kalender — keine Events heute)</i>\n"
        )
    else:
        lines.append(
            "<i>Kalender ist nicht verbunden. Mit /kalender_verbinden "
            "Google oder Outlook anbinden — dann zeigt /briefing auch "
            "die echten Termine.</i>\n"
        )
    if gespraeche_kommend:
        lines.append(f"<i>Naechste Tage aus /aufnahme:</i>")
        for g in gespraeche_kommend:
            d = g.termin_datum.astimezone(local_tz).strftime("%a %d.%m %H:%M")
            lines.append(
                f"  · {d} — <b>{_h_safe(g.kunde_name)}</b>  "
                f"/briefing_{str(g.id)[:8]}"
            )
        return "\n".join(lines)

    # Nichts in Sicht — juengstes Gespraech als Fallback
    async with AsyncSessionLocal() as s:
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
            f"\n\nNoch kein Kundengespraech{scope} erfasst.\n"
            "Mit /aufnahme das erste anlegen."
        )

    return await _send_kundengespraech_with_drive(
        chat_id,
        "\n\n<b>📋 Letztes Gespraech</b>\n",
        latest, tenant.id,
    )


async def _handle_briefing_show_command(chat_id, id_prefix: str):
    """/briefing_<8hex> — Detail-Ansicht eines Gespraechs mit Drive-Link.

    Klickbar aus dem /briefing-Listing. Findet das Gespraech per UUID-
    Prefix-Match (8 Hex-Chars sind genug fuer eindeutige Auswahl).
    """
    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    from sqlalchemy import cast, String as SAString
    async with AsyncSessionLocal() as s:
        stmt = (
            select(Kundengespraech)
            .where(
                Kundengespraech.tenant_id == tenant.id,
                cast(Kundengespraech.id, SAString).like(f"{id_prefix}%"),
            )
            .limit(2)
        )
        if not current_emp.is_default:
            stmt = stmt.where(
                Kundengespraech.assigned_employee_id == current_emp.id,
            )
        rows = (await s.execute(stmt)).scalars().all()

    if not rows:
        return (
            f"Kein Gespraech mit ID-Prefix <code>{_h_safe(id_prefix)}</code> "
            "gefunden. /briefing zeigt die heutigen Termine."
        )
    if len(rows) > 1:
        return "Mehrdeutiger Prefix — bitte /briefing erneut aufrufen."

    return await _send_kundengespraech_with_drive(
        chat_id, "", rows[0], tenant.id,
    )


async def _handle_kunde_command(chat_id, args):
    """/kunde [Name|Email] - alle Gespraeche zu einem Kunden.

    args = String nach '/kunde ' — entweder vollstaendiger Name
           (z.B. 'Anna Mueller'), Namens-Token ('Mueller') oder
           E-Mail-Adresse ('anna@example.com'). Email-Modus wird
           automatisch erkannt wenn '@' im Argument vorkommt.

    Sucht in:
      1. Kundengespraeche (Name-Match auf kunde_name)
      2. Angebote (Name + Email-Match) — fuegt 'Kunde nur als Angebots-
         Eintrag bekannt'-Resultate hinzu
      3. Lexware-Kontakte — globaler Adressbuch-Fallback
    """
    from sqlalchemy import select, func, or_

    if not args or len(args.strip()) < 2:
        return (
            "Bitte einen Kunden-Namen oder eine Mail-Adresse angeben.\n\n"
            "Beispiele: <code>/kunde Anna Mueller</code> oder "
            "<code>/kunde anna@example.com</code>"
        )

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, current_emp = res

    raw = args.strip()
    suchbegriff = raw.lower()
    is_email_search = "@" in raw

    async with AsyncSessionLocal() as s:
        # 1) Kundengespraeche per Name-Match (Email speichert das Modell
        # selbst nicht — wir haben nur kunde_name dort)
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

        # 2) Angebote per Name ODER Email — wenn Email-Suche, primaer
        # auf kunde_email; sonst auf kunde_name. Liefert nur Angebote
        # die nicht schon ueber Kundengespraech.angebot_id verlinkt sind.
        existing_angebot_ids = {
            g.angebot_id for g in gespraeche if g.angebot_id
        }
        angebot_stmt = select(Angebot).where(Angebot.tenant_id == tenant.id)
        if is_email_search:
            angebot_stmt = angebot_stmt.where(
                func.lower(Angebot.kunde_email).contains(suchbegriff)
            )
        else:
            angebot_stmt = angebot_stmt.where(or_(
                func.lower(Angebot.kunde_name).contains(suchbegriff),
                func.lower(Angebot.kunde_email).contains(suchbegriff),
            ))
        angebot_stmt = angebot_stmt.order_by(
            Angebot.created_at.desc()
        ).limit(10)
        angebote = (await s.execute(angebot_stmt)).scalars().all()
        angebote = [a for a in angebote if a.id not in existing_angebot_ids]

    # 3) Lexware-Kontakte (globales Adressbuch — Pattern-Match auf Name
    # ODER Email, beides via gleichem endpoint).
    lexware_hits: list = []
    try:
        provider = await _get_lexware_provider_for_tenant(tenant)
        if provider is not None and len(raw) >= 3:
            lexware_hits = await provider.search_contacts(
                raw, customer_only=True, limit=5,
            )
    except Exception:
        logger.exception("Lexware-Contact-Lookup fuer /kunde fehlgeschlagen")

    # ----- Ausgabe -----
    def _angebot_line(a) -> str:
        bits = []
        if a.kunde_email:
            bits.append(f"📧 {_h_safe(a.kunde_email)}")
        if a.gesamtbetrag_brutto_eur is not None:
            bits.append(f"💶 {float(a.gesamtbetrag_brutto_eur):.2f}€")
        if a.created_at:
            bits.append(a.created_at.strftime("%d.%m.%Y"))
        bits.append(f"/auftrag_{str(a.id)[:8]}")
        return (
            f"  · <b>{_h_safe(a.kunde_name)}</b>  "
            + "  ·  ".join(bits)
        )

    def _lexware_line(c) -> str:
        bits = []
        if getattr(c, "email", None):
            bits.append(f"📧 {_h_safe(c.email)}")
        if getattr(c, "city", None):
            bits.append(_h_safe(c.city))
        if getattr(c, "role", None):
            bits.append(c.role)
        return (
            f"  · <b>{_h_safe(c.name or '(ohne Name)')}</b>  "
            + ("  ·  ".join(bits) if bits else "")
        )

    # Wenn nur Lexware/Angebot-Treffer aber kein Gespraech → spezieller
    # Branch damit wir wenigstens irgendwas Brauchbares zeigen.
    if not gespraeche:
        if angebote or lexware_hits:
            lines = [
                f"<b>🔎 Kunde '{_h_safe(raw)}'</b>"
                + (" (Mail-Suche)" if is_email_search else "")
                + "  ·  keine Gespraeche, aber gefunden in:",
                "",
            ]
            if angebote:
                lines.append("<b>📋 Angebote / Auftraege</b>")
                for a in angebote[:5]:
                    lines.append(_angebot_line(a))
                lines.append("")
            if lexware_hits:
                lines.append("<b>📇 Lexware-Kontakte</b>")
                for c in lexware_hits[:5]:
                    lines.append(_lexware_line(c))
                lines.append("")
            return "\n".join(lines)
        # Auch ohne Gespraech / Angebot / Lexware: Drive-Ordner-Check
        drive_block, _ = await _drive_link_section(tenant.id, suchbegriff)
        if drive_block:
            return (
                f"Keine Treffer zu <i>{_h_safe(raw)}</i> gefunden."
                f"{drive_block}"
            )
        return f"Keine Treffer zu <i>{_h_safe(raw)}</i> gefunden."

    # Genau ein Gespraech → volle Anzeige (+ ggf. Angebot/Lexware-Block
    # zusaetzlich anhaengen)
    if len(gespraeche) == 1 and not angebote and not lexware_hits:
        return await _send_kundengespraech_with_drive(
            chat_id, "", gespraeche[0], tenant.id,
        )

    # Mehrere Gespraeche ODER mit zusaetzlichen Angebots-/Lexware-Treffern
    header = f"<b>📋 Treffer zu '{_h_safe(raw)}'</b>"
    if is_email_search:
        header += " (Mail-Suche)"
    msg = f"{header}\n\n"

    if gespraeche:
        msg += f"<b>📞 Gespraeche</b> ({len(gespraeche)})\n"
        for i, g in enumerate(gespraeche[:5], 1):
            msg += f"<b>{i}. {_format_kundengespraech_short(g)}</b>\n"
            if g.briefing_kurz:
                briefing = g.briefing_kurz
                if len(briefing) > 200:
                    briefing = briefing[:180] + "..."
                msg += f"<i>{briefing}</i>\n"
            msg += f"  Details: /briefing_{str(g.id)[:8]}\n\n"
        if len(gespraeche) > 5:
            msg += f"<i>... und {len(gespraeche) - 5} weitere</i>\n\n"

    if angebote:
        msg += f"<b>📋 Angebote / Auftraege</b> ({len(angebote)})\n"
        for a in angebote[:5]:
            msg += _angebot_line(a) + "\n"
        msg += "\n"

    if lexware_hits:
        msg += f"<b>📇 Lexware-Kontakte</b> ({len(lexware_hits)})\n"
        for c in lexware_hits[:5]:
            msg += _lexware_line(c) + "\n"
        msg += "\n"

    # Drive-Link nur wenn der Suchbegriff genau einem Kunden-Folder
    # entspricht (alle 5 Gespraech-Treffer haben den gleichen kunde_name).
    distinct_names = {g.kunde_name for g in gespraeche}
    if len(distinct_names) == 1:
        kunde_name = next(iter(distinct_names))
        drive_block, folder_url = await _drive_link_section(
            tenant.id, kunde_name,
        )
        msg += drive_block
        if folder_url:
            sent = await _send_with_inline_buttons(
                chat_id, msg,
                [[{
                    "text": f"📁 Drive-Ordner: {kunde_name}",
                    "url": folder_url,
                }]],
            )
            if sent:
                return None  # type: ignore[return-value]
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
    from urllib.parse import urlencode

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, emp = res

    public_url = settings.public_url.rstrip("/")
    qs = urlencode({
        "tenant": tenant.slug,
        "provider": "microsoft",
        "employee": emp.slug,
    })
    setup_url = f"{public_url}/oauth/start?{qs}"

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
            # Hinweis falls Preis aus Kalkulationsregel berechnet wurde
            kalk = p.get("kalkulation")
            if isinstance(kalk, dict) and kalk.get("formel"):
                vars_str = ", ".join(
                    f"{k}={v}" for k, v in (kalk.get("variablen") or {}).items()
                )
                msg += f"     🧮 <i>{kalk.get('regel_name')}: {kalk['formel']}"
                if vars_str:
                    msg += f"  ({vars_str})"
                msg += "</i>\n"
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

    # Gemini-Analyse - mit Kalkulationsregeln des Tenants als Kontext.
    # Gemini fuellt pro Position das `kalkulation`-Feld; wir berechnen den
    # finalen Preis gleich darunter deterministisch in Python.
    try:
        extracted = await analyse_kundengespraech_from_audio(
            audio_bytes,
            mime_type=audio_mime,
            tenant_id=tenant.id,
        )
    except Exception as e:
        logger.error(f"analyse_kundengespraech fehler: {e}", exc_info=True)
        await _clear_state(chat_id)
        return f"❌ Fehler bei Analyse: {e}\n\nBitte erneut versuchen."

    # Hybrid-Berechnung: Preise aus Kalkulationsregeln einsetzen
    extracted = await _apply_kalkulationen(tenant.id, extracted)

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


# =====================================================================
# /angebot-Pipeline: Text/Voice → Gemini → Kalkulation → Lexware (Quotation
# finalisiert) → Mail-Versand an Kunde → Auto-Invoice-Draft in Lexware
# =====================================================================

async def _send_with_keyboard(chat_id, text, keyboard_dict, bot_token=None):
    """sendMessage mit reply_markup. Text wird ggf. auto-gesplittet —
    Keyboard kommt nur ans LETZTE Stueck (sonst wuerde Telegram die
    Buttons in jedem Chunk anzeigen)."""
    if bot_token is None:
        bot_token = await _load_global_bot_token()
        if bot_token is None:
            return False
    chunks = _split_message_safely(str(text))
    ok_all = True
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if is_last and keyboard_dict:
            payload["reply_markup"] = keyboard_dict
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        f"_send_with_keyboard {resp.status_code}: {resp.text[:200]}"
                    )
                    ok_all = False
            except Exception as exc:
                logger.exception(f"_send_with_keyboard crashed: {exc}")
                ok_all = False
    return ok_all


def _is_full_kunde_name(name: str | None) -> bool:
    """True wenn der Kundenname als 'voll' gilt: min. 2 Tokens mit je
    min. 2 Buchstaben. Anredeworte (Herr, Frau, Familie) zaehlen als
    Token nur wenn ein echter Name danach kommt.

    Beispiele:
      "Müller"             → False (nur ein Name)
      "Frau Müller"        → True (2 Tokens, jedes >=2 Buchstaben)
      "F. Müller"          → False ("F." hat nur 1 Buchstabe)
      "Anna Müller"        → True
      "Bauunternehmen Schmidt GmbH" → True
      "(unbekannt)"        → False
      ""                   → False
    """
    if not name:
        return False
    cleaned = name.strip()
    if cleaned.lower().startswith("(") or cleaned.lower() in (
        "unbekannt", "kunde", "kunde unbekannt", "n/a", "nn",
    ):
        return False
    tokens = [t for t in cleaned.split() if len(t.strip(".,;:-")) >= 2]
    return len(tokens) >= 2


async def _send_angebot_full_name_prompt(chat_id, current_name: str | None):
    """Bot fragt den User nach dem vollen Namen — wird sowohl beim
    Erst-Insert wie auch bei einer expliziten Korrektur genutzt."""
    msg = (
        "⚠️ <b>Kein vollstaendiger Name erkannt</b>"
    )
    if current_name:
        msg += f" — habe verstanden: <i>{_h_safe(current_name)}</i>"
    msg += (
        ".\n\n"
        "Bitte schick mir den <b>vollstaendigen Namen</b> "
        "(Vor- und Nachname, z.B. <i>Anna Mueller</i> oder "
        "<i>Bauunternehmen Schmidt GmbH</i>).\n\n"
        "Mit /abbrechen verwirfst du den Vorgang."
    )
    await _send_to_chat(chat_id, msg)


async def _handle_angebot_command(chat_id):
    """Startet den /angebot-Wizard. Akzeptiert danach Text ODER Voice."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst /start ausfuehren."
        )
    provider = await _get_lexware_provider_for_tenant(tenant)
    if provider is None:
        return (
            "🔒 <b>Lexware ist nicht eingerichtet.</b>\n"
            "Erst /lexware_setup ausfuehren, dann /angebot."
        )
    await _save_state(chat_id, STATE_ANGEBOT_WAITING_INPUT, {})
    return (
        "📋 <b>Neues Angebot</b>\n\n"
        "Diktiere oder schreibe was du anbieten willst — z.B.:\n"
        "<i>«Angebot fuer Frau Mueller, Hauptstr 5 Trier, mueller@example.com:\n"
        "100 qm Parkett verlegen plus Anfahrt»</i>\n\n"
        "Ich nutze deine /kalkulation-Formeln fuer die Preise und lege das "
        "Angebot direkt in Lexware an, sende es per Mail an den Kunden und "
        "bereite eine passende Rechnung als Lexware-Draft vor.\n\n"
        "Mit /abbrechen beenden."
    )


def _format_angebot_preview(extracted: dict, *, anschreiben: str | None = None) -> str:
    """Markdown-Vorschau fuer den Confirm-Step.

    Wenn `anschreiben` gesetzt ist, wird es in einem eigenen Block oben
    angezeigt — sonst Hinweis dass das Default-Anschreiben genutzt wird.
    """
    kunde = extracted.get("kunde_name") or "(unbekannt)"
    email = extracted.get("kunde_email")
    strasse = extracted.get("kunde_strasse")
    plz = extracted.get("kunde_plz")
    ort = extracted.get("kunde_ort")

    lines = [f"📋 <b>Angebot fuer {_h_safe(kunde)}</b>"]
    if email:
        lines.append(f"📧 {_h_safe(email)}")
    else:
        lines.append(
            "📧 <i>(keine Mail erkannt — wird in Lexware-Kontakten gesucht)</i>"
        )
    addr_parts = [p for p in [strasse, f"{plz or ''} {ort or ''}".strip()] if p]
    if addr_parts:
        lines.append("📍 " + ", ".join(_h_safe(p) for p in addr_parts))
    lines.append("")

    positionen = extracted.get("positionen") or []
    gesamt = Decimal("0")
    for i, pos in enumerate(positionen, 1):
        name = pos.get("name") or "(ohne Name)"
        menge = float(pos.get("menge") or 1)
        einheit = pos.get("einheit") or "Stueck"
        preis = float(pos.get("preis_brutto_eur") or 0)
        zeile_total = Decimal(str(round(menge * preis, 2)))
        gesamt += zeile_total
        kalk = pos.get("kalkulation") or {}
        kalk_hint = ""
        if isinstance(kalk, dict) and kalk.get("regel_name"):
            kalk_hint = f"  <i>(Formel: {_h_safe(kalk['regel_name'])})</i>"
        lines.append(
            f"<b>{i}.</b> {_h_safe(name)} — {menge:g} {_h_safe(einheit)} × "
            f"{preis:.2f}€ = <b>{float(zeile_total):.2f}€</b>{kalk_hint}"
        )
        if pos.get("beschreibung"):
            lines.append(f"   <i>{_h_safe(pos['beschreibung'])}</i>")

    lines.append("")
    lines.append(f"<b>Gesamt brutto: {float(gesamt):.2f} €</b>")

    if anschreiben:
        lines.append("")
        lines.append("✏️ <b>Anschreiben (personalisiert)</b>")
        lines.append(f"<i>{_h_safe(anschreiben)}</i>")
    else:
        lines.append("")
        lines.append(
            "<i>Anschreiben: Standard. Mit ✏️ Anschreiben anpassen "
            "personalisieren (Tonangabe, was rein soll, ...).</i>"
        )

    missing = extracted.get("missing_fields") or []
    if missing:
        lines.append("")
        lines.append(
            f"⚠️ <i>Unklare Felder: {', '.join(str(m) for m in missing)}</i>"
        )

    return "\n".join(lines)


def _angebot_keyboard(angebot_id: str) -> dict:
    """Inline-Keyboard fuer die Preview — drei Buttons in 2 Reihen.

    Reihe 1: Erstellen + Versenden (Hauptaktion, prominent oben)
    Reihe 2: Anschreiben personalisieren | Verwerfen
    """
    return {
        "inline_keyboard": [
            [{"text": "✅ Erstellen + Versenden",
              "callback_data": f"angebot:confirm:{angebot_id}"}],
            [
                {"text": "✏️ Anschreiben anpassen",
                 "callback_data": f"angebot:anschreiben:{angebot_id}"},
                {"text": "❌ Verwerfen",
                 "callback_data": f"angebot:cancel:{angebot_id}"},
            ],
        ]
    }


async def _resend_angebot_preview(chat_id, angebot_id_str: str):
    """Sendet die aktuelle Preview erneut — mit Anschreiben falls in DB."""
    state = await _load_state(chat_id)
    extracted = (state.state_data or {}).get("extracted") if state else None
    if not extracted:
        await _send_to_chat(
            chat_id,
            "Angebots-Daten nicht mehr im Speicher. Bitte /angebot neu starten."
        )
        return
    # Anschreiben aus DB nachladen
    anschreiben = None
    try:
        import uuid as _uuid
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == _uuid.UUID(angebot_id_str))
            )).scalar_one_or_none()
            if ang and ang.introduction_text:
                anschreiben = ang.introduction_text
    except Exception:
        logger.exception("Anschreiben-Reload fehlgeschlagen — egal, weiter")

    preview = _format_angebot_preview(extracted, anschreiben=anschreiben)
    keyboard = _angebot_keyboard(angebot_id_str)
    msg = (
        preview
        + "\n\n<i>Bestaetigen erstellt das Angebot in Lexware, schickt das "
        "PDF per Mail an den Kunden und legt eine passende Rechnung als "
        "Lexware-Draft an.</i>"
    )
    await _send_with_keyboard(chat_id, msg, keyboard)


async def _handle_angebot_input_received(
    chat_id, *, text: str | None = None, voice_dict: dict | None = None,
    bot_token: str | None = None,
):
    """Verarbeitet Text- oder Voice-Input fuer ein neues Angebot.

    1. Gemini-Extraction (mit Kalkulationsregeln im Prompt)
    2. _apply_kalkulationen (deterministische Formel-Anwendung)
    3. Angebot + AngebotPositions in DB anlegen
    4. Preview senden mit Confirm-Buttons
    """
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. /start ausfuehren."

    # 1) Extraction
    try:
        if voice_dict is not None:
            if not bot_token:
                bot_token = await _load_global_bot_token()
            file_id = voice_dict.get("file_id")
            file_path = await _telegram_get_file_path(bot_token, file_id)
            audio_bytes = await _telegram_download_file(bot_token, file_path)
            mime = voice_dict.get("mime_type") or "audio/ogg"
            extracted = await extract_angebot_from_audio(
                audio_bytes, mime_type=mime, tenant_id=tenant.id,
            )
            raw_input_text = f"[Voice {len(audio_bytes)} bytes, mime={mime}]"
        else:
            extracted = await extract_angebot_from_text(
                text or "", tenant_id=tenant.id,
            )
            raw_input_text = (text or "")[:5000]
    except Exception as exc:
        logger.exception(f"Angebot-Extraction fehlgeschlagen: {exc}")
        await _clear_state(chat_id)
        return (
            "❌ Konnte die Eingabe nicht verstehen.\n"
            "Bitte mit /angebot erneut starten und Kunde + Leistungen klar nennen."
        )

    # 2) Kalkulation anwenden — gleiche Funktion wie /aufnahme nutzt
    extracted = await _apply_kalkulationen(tenant.id, extracted)

    # 3) Mindestens 1 Position mit Preis Pflicht
    positionen = extracted.get("positionen") or []
    if not positionen or not any(
        p.get("preis_brutto_eur") for p in positionen
    ):
        await _clear_state(chat_id)
        return (
            "❌ Konnte keine Positionen mit Preis erkennen.\n\n"
            "Bitte erwaehne Leistungen explizit — z.B. <i>«Parkett verlegen, "
            "100qm»</i>. Wenn du Kalkulations-Formeln nutzt, in /kalkulation "
            "anzeigen mit /kalkulation_anzeigen.\n\n"
            "Mit /angebot neu starten."
        )

    # 3b) Pflicht: voller Kunden-Name. Wenn nicht erkannt → State
    # AWAITING_KUNDE_NAME, der naechste Text-Input vom User wird als
    # vollstaendiger Name uebernommen und der Flow geht hier weiter.
    if not _is_full_kunde_name(extracted.get("kunde_name")):
        await _save_state(chat_id, STATE_ANGEBOT_AWAITING_KUNDE_NAME, {
            "extracted": extracted,
            "raw_input": raw_input_text,
            "source": "telegram_voice" if voice_dict is not None else "telegram_text",
        })
        await _send_angebot_full_name_prompt(chat_id, extracted.get("kunde_name"))
        return None  # auf User-Antwort warten

    # 4) DB-Insert + Preview anzeigen — extrahierte Helper-Funktion
    # damit der STATE_ANGEBOT_AWAITING_KUNDE_NAME-Handler nach der
    # Name-Korrektur direkt hier wieder einsteigen kann.
    await _persist_angebot_and_show_preview(
        chat_id, tenant, extracted, raw_input_text,
        source="telegram_voice" if voice_dict is not None else "telegram_text",
        bot_token=bot_token,
    )
    return None


async def _persist_angebot_and_show_preview(
    chat_id, tenant, extracted: dict, raw_input_text: str,
    *, source: str, bot_token: str | None = None,
):
    """Macht den DB-Insert + Preview-Render fuer ein Angebot.

    Wird aus _handle_angebot_input_received aufgerufen (Hauptweg) UND
    aus dem STATE_ANGEBOT_AWAITING_KUNDE_NAME-Handler (nach Name-
    Korrektur durch User).
    """
    positionen = extracted.get("positionen") or []
    gesamt = Decimal("0")
    async with AsyncSessionLocal() as s:
        ang = Angebot(
            tenant_id=tenant.id,
            quelle=source,
            raw_input=raw_input_text,
            kunde_name=extracted.get("kunde_name") or "(unbekannt)",
            kunde_strasse=extracted.get("kunde_strasse"),
            kunde_plz=extracted.get("kunde_plz"),
            kunde_ort=extracted.get("kunde_ort"),
            kunde_email=extracted.get("kunde_email"),
            status=ANGEBOT_STATUS_ERSTELLT,
            confidence=extracted.get("extraction_confidence"),
        )
        s.add(ang)
        await s.flush()
        for i, pos in enumerate(positionen, 1):
            menge = Decimal(str(round(float(pos.get("menge") or 1), 4)))
            preis = Decimal(str(round(float(pos.get("preis_brutto_eur") or 0), 2)))
            gesamt += menge * preis
            s.add(AngebotPosition(
                angebot_id=ang.id,
                position_nr=i,
                name=(pos.get("name") or "")[:300],
                beschreibung=pos.get("beschreibung"),
                menge=menge,
                einheit=(pos.get("einheit") or "Stueck")[:50],
                preis_brutto_eur=preis,
                mwst_prozent=int(pos.get("mwst_prozent") or 19),
            ))
        ang.gesamtbetrag_brutto_eur = gesamt
        await s.commit()
        angebot_id = str(ang.id)

    preview = _format_angebot_preview(extracted)
    keyboard = _angebot_keyboard(angebot_id)
    await _save_state(chat_id, STATE_ANGEBOT_PREVIEWING, {
        "angebot_id": angebot_id,
        "extracted": extracted,
    })
    msg = (
        preview
        + "\n\n<i>Bestaetigen erstellt das Angebot in Lexware, schickt das "
        "PDF per Mail an den Kunden und legt eine passende Rechnung als "
        "Lexware-Draft an.</i>"
    )
    bot_token_for_send = bot_token or await _load_global_bot_token()
    await _send_with_keyboard(chat_id, msg, keyboard, bot_token_for_send)


async def _handle_angebot_kunde_name_input(chat_id, text: str | None):
    """User hat den vollen Kundennamen eingegeben — validieren, in
    extracted mergen, dann DB-Insert + Preview."""
    state = await _load_state(chat_id)
    if not state or state.state_key != STATE_ANGEBOT_AWAITING_KUNDE_NAME:
        return "Status verloren — /angebot neu starten."

    data = state.state_data or {}
    extracted = data.get("extracted") or {}
    raw_input_text = data.get("raw_input") or ""
    source = data.get("source") or "telegram_text"

    name = (text or "").strip()
    if not _is_full_kunde_name(name):
        # Erneut fragen — kein Fortschritt ohne vollen Namen
        await _send_angebot_full_name_prompt(chat_id, name or None)
        return None

    extracted["kunde_name"] = name
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. /start ausfuehren."
    await _persist_angebot_and_show_preview(
        chat_id, tenant, extracted, raw_input_text, source=source,
    )
    return None


async def _handle_angebot_callback(chat_id, callback_data, callback_query_id, bot_token):
    """Verarbeitet angebot:<action>:<id>-Callbacks (confirm/cancel/anschreiben)."""
    import uuid as _uuid
    parts = callback_data.split(":", 2)
    if len(parts) != 3:
        await _answer_callback_query(callback_query_id, "Format-Fehler", bot_token)
        return
    _, action, angebot_id_str = parts
    try:
        angebot_uuid = _uuid.UUID(angebot_id_str)
    except (ValueError, AttributeError):
        await _answer_callback_query(callback_query_id, "Ungueltige ID", bot_token)
        return

    if action == "cancel":
        await _answer_callback_query(callback_query_id, "Verworfen", bot_token)
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_uuid)
            )).scalar_one_or_none()
            if ang is not None:
                await s.delete(ang)
                await s.commit()
        await _clear_state(chat_id)
        await _send_to_chat(chat_id, "Angebot verworfen.")
        return

    if action == "confirm":
        await _answer_callback_query(
            callback_query_id, "Erstelle Angebot…", bot_token,
        )
        await _send_to_chat(
            chat_id,
            "⏳ Lege Angebot in Lexware an, versende Mail, erzeuge Rechnung…",
        )
        await _clear_state(chat_id)
        result = await _run_angebot_pipeline(angebot_uuid)
        await _send_to_chat(chat_id, result)
        return

    if action == "anschreiben":
        # State auf "warte auf Anweisungen" setzen — angebot_id mitnehmen
        # damit wir das richtige Angebot im naechsten Step finden.
        # Preview-State bleibt in der DB als Backup falls User abbricht.
        await _answer_callback_query(
            callback_query_id, "Anschreiben anpassen…", bot_token,
        )
        # Aktuellen Preview-State erhalten und die extracted-Daten weitergeben
        prev = await _load_state(chat_id)
        extracted = (prev.state_data or {}).get("extracted") if prev else {}
        await _save_state(chat_id, STATE_ANGEBOT_AWAITING_INSTRUCTIONS, {
            "angebot_id": angebot_id_str,
            "extracted": extracted,
        })
        await _send_to_chat(
            chat_id,
            "✏️ <b>Anschreiben personalisieren</b>\n\n"
            "Schreib oder diktier deine Anweisungen — z.B.:\n"
            "<i>«freundlich und kurz, erwaehne dass wir naechste Woche "
            "Donnerstag Zeit haetten»</i>\n\n"
            "Oder direkt deinen Wunsch-Text — ich baue daraus einen sauberen "
            "Brief-Anfang fuer das Lexware-PDF.\n\n"
            "Mit /abbrechen verwerfen und zur Standard-Variante zurueck.",
        )
        return

    await _answer_callback_query(callback_query_id, "Unbekannte Aktion", bot_token)


async def _handle_angebot_instructions_received(
    chat_id, *, text: str | None = None, voice_dict: dict | None = None,
    bot_token: str | None = None,
):
    """Gemini generiert ein personalisiertes Anschreiben aus den
    Handwerker-Anweisungen, speichert in angebot.introduction_text,
    zeigt aktualisierte Preview."""
    state = await _load_state(chat_id)
    if not state or state.state_key != STATE_ANGEBOT_AWAITING_INSTRUCTIONS:
        return "Status verloren — bitte /angebot neu starten."
    data = state.state_data or {}
    angebot_id_str = data.get("angebot_id")
    extracted = data.get("extracted") or {}
    if not angebot_id_str:
        await _clear_state(chat_id)
        return "Angebots-ID fehlt — bitte /angebot neu starten."

    tenant = await _get_tenant_by_chat(chat_id)

    # Gemini-Generation — Text oder Audio
    try:
        if voice_dict is not None:
            if not bot_token:
                bot_token = await _load_global_bot_token()
            file_id = voice_dict.get("file_id")
            file_path = await _telegram_get_file_path(bot_token, file_id)
            audio_bytes = await _telegram_download_file(bot_token, file_path)
            mime = voice_dict.get("mime_type") or "audio/ogg"
            anschreiben = await generate_angebot_anschreiben_from_audio(
                extracted, audio_bytes, mime_type=mime,
                tenant_id=tenant.id if tenant else None,
            )
        else:
            anschreiben = await generate_angebot_anschreiben(
                extracted, text or "",
                tenant_id=tenant.id if tenant else None,
            )
    except Exception as exc:
        logger.exception(f"Anschreiben-Generation crashed: {exc}")
        anschreiben = ""

    if not anschreiben:
        # Zurueck zur Preview ohne Anschreiben
        await _save_state(chat_id, STATE_ANGEBOT_PREVIEWING, {
            "angebot_id": angebot_id_str,
            "extracted": extracted,
        })
        await _send_to_chat(
            chat_id,
            "⚠️ Konnte kein Anschreiben generieren. Bitte konkreter formulieren "
            "(z.B. <i>«kurz und sachlich, Hinweis dass wir nach Ostern kommen»</i>).\n\n"
            "Du kannst es nochmal versuchen oder direkt Erstellen + Versenden.",
        )
        await _resend_angebot_preview(chat_id, angebot_id_str)
        return None

    # Anschreiben in DB persistieren
    import uuid as _uuid
    try:
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == _uuid.UUID(angebot_id_str))
            )).scalar_one_or_none()
            if ang is not None:
                ang.introduction_text = anschreiben
                await s.commit()
    except Exception:
        logger.exception("Anschreiben-DB-Save fehlgeschlagen")

    # Zurueck zur Preview mit dem neuen Anschreiben sichtbar
    await _save_state(chat_id, STATE_ANGEBOT_PREVIEWING, {
        "angebot_id": angebot_id_str,
        "extracted": extracted,
    })
    await _send_to_chat(chat_id, "✅ Anschreiben angepasst. Hier die Vorschau:")
    await _resend_angebot_preview(chat_id, angebot_id_str)
    return None


async def _run_angebot_pipeline(angebot_id) -> str:
    """Volle Pipeline: Lexware-Quotation finalisiert anlegen → Mail mit
    PDF → Lexware-Invoice-Draft mit gleichen Positionen.

    Jede Stufe wird einzeln gefangen — die anderen laufen weiter und
    der User kriegt einen aussagekraeftigen Status-Report.
    """
    from core.integrations.angebot_mail import send_angebot_to_customer

    # Angebot laden inkl. Positionen + Tenant (brauchen wir fuer Provider)
    async with AsyncSessionLocal() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one_or_none()
        if ang is None:
            return "❌ Angebot nicht gefunden."
        positions = (await s.execute(
            select(AngebotPosition).where(AngebotPosition.angebot_id == angebot_id)
            .order_by(AngebotPosition.position_nr)
        )).scalars().all()
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == ang.tenant_id)
        )).scalar_one()
        kunde_name = ang.kunde_name
        kunde_email = ang.kunde_email
        kunde_strasse = ang.kunde_strasse
        kunde_plz = ang.kunde_plz
        kunde_ort = ang.kunde_ort
        # Personalisiertes Anschreiben falls vorher gesetzt — sonst Default
        custom_intro = ang.introduction_text

    provider = await _get_lexware_provider_for_tenant(tenant)
    if provider is None:
        return "❌ Lexware ist nicht eingerichtet. /lexware_setup ausfuehren."

    # Lexware-LineItems vorbereiten (einmal — fuer Quotation + Invoice)
    line_items = [
        InvoiceLineItem(
            name=p.name,
            quantity=float(p.menge),
            unit_name=p.einheit or "Stueck",
            unit_price_gross=float(p.preis_brutto_eur),
            description=p.beschreibung,
            tax_rate_percent=int(p.mwst_prozent or 19),
        )
        for p in positions
    ]
    one_time_address = {
        "name": kunde_name,
        "countryCode": "DE",
    }
    if kunde_strasse:
        one_time_address["street"] = kunde_strasse
    if kunde_plz:
        one_time_address["zip"] = kunde_plz
    if kunde_ort:
        one_time_address["city"] = kunde_ort

    report: list[str] = [f"📋 <b>Angebot: {_h_safe(kunde_name)}</b>", ""]

    # Stufe 1: Lexware-Quotation FINALISIERT anlegen (Pflicht — sonst
    # kein PDF-Download moeglich, also keine Mail an Kunde).
    intro_text = custom_intro or (
        "Sehr geehrte Damen und Herren,\n\nvielen Dank fuer Ihre Anfrage. "
        "Anbei unser Angebot."
    )
    try:
        quotation = await provider.create_quotation_draft(
            line_items=line_items,
            one_time_address=one_time_address,
            title=f"Angebot {kunde_name}",
            introduction=intro_text,
            remark="Preise inkl. MwSt. Angebot gueltig 30 Tage.",
            tax_type="gross",
            finalize=True,
        )
    except Exception as exc:
        logger.exception(f"Lexware-Quotation-Anlage gescheitert: {exc}")
        return (
            "❌ <b>Angebot in Lexware konnte nicht angelegt werden.</b>\n\n"
            f"Fehler: <code>{_h_safe(str(exc)[:200])}</code>\n\n"
            "Pruefe /lexware_status — ggf. /lexware_setup neu durchlaufen."
        )

    async with AsyncSessionLocal() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one()
        ang.lexware_quotation_id = quotation.quotation_id
        ang.lexware_voucher_number = quotation.voucher_number
        ang.status = ANGEBOT_STATUS_IN_LEXWARE
        await s.commit()

    report.append(
        f"✅ Lexware-Angebot angelegt — "
        f"<a href=\"{quotation.deeplink_view}\">in Lexware oeffnen</a>"
    )

    # Stufe 2a: Email-Fallback aus Lexware-Kontakten wenn nicht
    # im Input erkannt. search_contacts macht Pattern-Match auf den
    # Namen — beste Trefferquote bei min 3 Zeichen.
    if not kunde_email and kunde_name and len(kunde_name.strip()) >= 3:
        try:
            contacts = await provider.search_contacts(
                kunde_name, customer_only=True, limit=5,
            )
        except Exception as exc:
            logger.exception(f"Lexware-Contact-Lookup gescheitert: {exc}")
            contacts = []
        # Erste Mail-Adresse aus den Treffern uebernehmen — bevorzugt
        # Match wo der Name zumindest teilweise im Treffer-Display steht.
        chosen_email = None
        kunde_lower = kunde_name.lower()
        for c in contacts:
            if c.email:
                if any(tok in (c.name or "").lower() for tok in kunde_lower.split()):
                    chosen_email = c.email
                    break
        if not chosen_email:
            for c in contacts:
                if c.email:
                    chosen_email = c.email
                    break
        if chosen_email:
            kunde_email = chosen_email
            report.append(
                f"🔎 Mail-Adresse aus Lexware-Kontakten: <code>{_h_safe(chosen_email)}</code>"
            )
            # In DB persistieren — beim naechsten Mal sofort dabei
            async with AsyncSessionLocal() as s:
                ang2 = (await s.execute(
                    select(Angebot).where(Angebot.id == angebot_id)
                )).scalar_one_or_none()
                if ang2 is not None:
                    ang2.kunde_email = chosen_email
                    await s.commit()

    # Stufe 2b: Mail an Kunden (nur wenn jetzt eine Adresse da ist)
    if kunde_email:
        try:
            mail_result = await send_angebot_to_customer(
                angebot_id=angebot_id, to_email=kunde_email,
            )
        except Exception as exc:
            logger.exception(f"Mail-Versand crashed: {exc}")
            mail_result = {"success": False, "error": str(exc)}

        if mail_result.get("success"):
            report.append(f"✅ Mail mit PDF an <b>{_h_safe(kunde_email)}</b> versendet")
        elif mail_result.get("queued"):
            report.append(
                f"⏳ Mail an {_h_safe(kunde_email)} eingereiht "
                "(wird vom Cron erneut versucht)"
            )
        else:
            err = mail_result.get("error", "unbekannt")
            report.append(
                f"⚠️ Mail an {_h_safe(kunde_email)} nicht versendet — "
                f"<i>{_h_safe(err[:160])}</i>"
            )
    else:
        report.append(
            "ℹ️ Keine Kunden-Mail-Adresse erkannt — Mail nicht versendet. "
            "Du kannst das Angebot manuell aus Lexware verschicken."
        )

    # Stufe 3: Auto-Rechnung als Lexware-Draft mit den gleichen Positionen.
    # Bleibt bewusst im Draft-Status — wird erst beim /auftraege-Schritt
    # "Fertig" finalisiert + per Mail an den Kunden geschickt.
    try:
        invoice = await provider.create_invoice_draft(
            line_items=line_items,
            one_time_address=one_time_address,
            title=f"Rechnung {kunde_name}",
            introduction=(
                f"Sehr geehrte Damen und Herren,\n\nvielen Dank fuer Ihren "
                f"Auftrag. Nachstehend unsere Rechnung."
            ),
            remark="Bitte begleichen Sie den Rechnungsbetrag innerhalb von 14 Tagen.",
            tax_type="gross",
            finalize=False,
        )
    except Exception as exc:
        logger.exception(f"Auto-Rechnung-Draft gescheitert: {exc}")
        report.append(
            f"⚠️ Rechnungs-Draft konnte nicht angelegt werden — "
            f"<i>{_h_safe(str(exc)[:160])}</i>"
        )
    else:
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_id)
            )).scalar_one()
            ang.lexware_invoice_id = invoice.invoice_id
            ang.status = ANGEBOT_STATUS_RECHNUNG_ERSTELLT
            await s.commit()
        report.append(
            f"✅ Rechnungs-Draft in Lexware bereit — "
            f"<a href=\"{invoice.deeplink_edit}\">in Lexware oeffnen</a>"
        )
        report.append(
            "\n<i>Naechste Schritte siehst du in /auftraege — "
            "dort kannst du den Status setzen und am Ende mit einem Tap "
            "die Rechnung rausschicken.</i>"
        )

    return "\n".join(report)


# =====================================================================
# /auftraege — Uebersicht laufender Projekte, Status-Setzen, Rechnungs-
# Versand bei "Fertig"
# =====================================================================

# Stati die als "laufende Auftraege" angezeigt werden — alles ab
# rechnung_erstellt (Angebot raus + Invoice-Draft bereit) bis
# rechnung_gesendet (Rechnung beim Kunden).
_AUFTRAG_ACTIVE_STATI = {
    ANGEBOT_STATUS_RECHNUNG_ERSTELLT,
    ANGEBOT_STATUS_ACCEPTED,
    ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ANGEBOT_STATUS_WORK_DONE,
    ANGEBOT_STATUS_RECHNUNG_GESENDET,
}


def _auftrag_progress_line(status: str) -> str:
    """Visuelle Fortschritts-Anzeige — ✓ fuer erledigte Schritte, ▸ fuer
    den aktuellen, ─ fuer noch offene."""
    if status not in AUFTRAG_LIFECYCLE:
        return AUFTRAG_LIFECYCLE_LABELS.get(status, status)
    current_idx = AUFTRAG_LIFECYCLE.index(status)
    parts = []
    for i, s in enumerate(AUFTRAG_LIFECYCLE):
        label = AUFTRAG_LIFECYCLE_LABELS.get(s, s).split(" ", 1)
        symbol = label[0]  # nur das Emoji als Schritt-Symbol
        if i < current_idx:
            parts.append(f"<s>{symbol}</s>")
        elif i == current_idx:
            parts.append(f"<b>{symbol}</b>")
        else:
            parts.append(f"<i>{symbol}</i>")
    return " → ".join(parts)


async def _handle_auftraege_command(chat_id):
    """Listet laufende Auftraege des Tenants mit aktuellem Lifecycle-Schritt."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet.\n"
            "Bitte zuerst /start ausfuehren."
        )

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tenant.id)
            .where(Angebot.status.in_(list(_AUFTRAG_ACTIVE_STATI)))
            .order_by(Angebot.created_at.desc())
            .limit(20)
        )).scalars().all()

    if not rows:
        return (
            "📂 <b>Keine laufenden Auftraege</b>\n\n"
            "Mit /angebot eins anlegen — sobald das Angebot in Lexware "
            "ist, taucht der Auftrag hier auf."
        )

    lines = ["📂 <b>Laufende Auftraege</b>\n"]
    for ang in rows:
        progress = _auftrag_progress_line(ang.status)
        gesamt = float(ang.gesamtbetrag_brutto_eur or 0)
        created = ang.created_at.strftime("%d.%m.%Y") if ang.created_at else "?"
        lines.append(
            f"<b>{_h_safe(ang.kunde_name)}</b>  ·  "
            f"{gesamt:.2f}€  ·  {created}"
        )
        lines.append(f"  {progress}")
        lines.append(
            f"  <b>Status setzen:</b> /auftrag_{str(ang.id)[:8]}"
        )
        lines.append("")
    lines.append(
        "<i>Tap auf /auftrag_xxxxxxxx fuer Details + Buttons zum "
        "Status-Wechseln. Bei 🏁 Fertig wird die Rechnung automatisch "
        "rausgeschickt.</i>"
    )
    return "\n".join(lines)


async def _handle_auftrag_show_command(chat_id, id_prefix: str):
    """Zeigt einen einzelnen Auftrag (per 8-Char-ID-Prefix) mit Buttons
    zum Status-Setzen."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Tenant nicht gefunden. /start ausfuehren."

    # Suche per Prefix — wir nehmen exakt 8 Hex-Chars
    async with AsyncSessionLocal() as s:
        # Cast id-Spalte zu Text fuer Prefix-Match, ist sauberer als
        # alle Rows zu laden + Python-Filter.
        from sqlalchemy import cast, String as SAString
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tenant.id)
            .where(cast(Angebot.id, SAString).like(f"{id_prefix}%"))
            .limit(2)
        )).scalars().all()

    if not rows:
        return f"Kein Auftrag mit ID-Prefix <code>{_h_safe(id_prefix)}</code> gefunden."
    if len(rows) > 1:
        return "Mehrdeutiger Prefix — bitte mehr Zeichen. /auftraege fuer Uebersicht."

    ang = rows[0]
    progress = _auftrag_progress_line(ang.status)
    label_now = AUFTRAG_LIFECYCLE_LABELS.get(ang.status, ang.status)
    gesamt = float(ang.gesamtbetrag_brutto_eur or 0)

    lines = [
        f"📂 <b>{_h_safe(ang.kunde_name)}</b>",
        f"💶 {gesamt:.2f} € brutto",
        f"📅 angelegt {ang.created_at.strftime('%d.%m.%Y') if ang.created_at else '?'}",
        "",
        f"<b>Aktueller Stand:</b> {label_now}",
        progress,
    ]
    if ang.kunde_email:
        lines.append(f"📧 {_h_safe(ang.kunde_email)}")
    if ang.lexware_quotation_id:
        deeplink_q = LexwareProvider.quotation_deeplink_view(ang.lexware_quotation_id)
        lines.append(f"📋 <a href=\"{deeplink_q}\">Angebot in Lexware</a>")
    if ang.lexware_invoice_id:
        deeplink_i = LexwareProvider.invoice_deeplink_view(ang.lexware_invoice_id)
        lines.append(f"🧾 <a href=\"{deeplink_i}\">Rechnung in Lexware</a>")

    # Buttons je nach aktuellem Status: nur sinnvolle Folge-Schritte zeigen
    aid = str(ang.id)
    btns: list[list[dict]] = []
    if ang.status == ANGEBOT_STATUS_RECHNUNG_ERSTELLT:
        btns.append([
            {"text": "✅ Angenommen",
             "callback_data": f"auftrag:set:{aid}:{ANGEBOT_STATUS_ACCEPTED}"},
            {"text": "❌ Abgebrochen",
             "callback_data": f"auftrag:set:{aid}:{ANGEBOT_STATUS_ABGEBROCHEN}"},
        ])
    if ang.status in (ANGEBOT_STATUS_RECHNUNG_ERSTELLT, ANGEBOT_STATUS_ACCEPTED):
        btns.append([
            {"text": "🔨 Arbeit laeuft",
             "callback_data": f"auftrag:set:{aid}:{ANGEBOT_STATUS_WORK_IN_PROGRESS}"},
        ])
    if ang.status in (
        ANGEBOT_STATUS_ACCEPTED,
        ANGEBOT_STATUS_WORK_IN_PROGRESS,
    ):
        btns.append([
            {"text": "🏁 Fertig — Rechnung raus",
             "callback_data": f"auftrag:fertig:{aid}"},
        ])
    if ang.status == ANGEBOT_STATUS_WORK_DONE:
        # Edge: wenn der vorherige Rechnungs-Versand crashte, nochmal versuchen
        btns.append([
            {"text": "🔄 Rechnung jetzt rausschicken",
             "callback_data": f"auftrag:fertig:{aid}"},
        ])
    if ang.status == ANGEBOT_STATUS_RECHNUNG_GESENDET:
        lines.append("")
        lines.append("✅ <i>Auftrag abgeschlossen. Rechnung ist beim Kunden.</i>")

    msg = "\n".join(lines)
    if btns:
        await _send_with_keyboard(chat_id, msg, {"inline_keyboard": btns})
        return None
    return msg


async def _handle_auftrag_callback(chat_id, callback_data, callback_query_id, bot_token):
    """auftrag:set:<id>:<status>  oder  auftrag:fertig:<id>"""
    import uuid as _uuid
    parts = callback_data.split(":")
    if len(parts) < 3:
        await _answer_callback_query(callback_query_id, "Format-Fehler", bot_token)
        return
    action = parts[1]
    try:
        aid = _uuid.UUID(parts[2])
    except (ValueError, IndexError):
        await _answer_callback_query(callback_query_id, "Ungueltige ID", bot_token)
        return

    if action == "set":
        if len(parts) < 4:
            await _answer_callback_query(callback_query_id, "Status fehlt", bot_token)
            return
        new_status = parts[3]
        if new_status not in AUFTRAG_LIFECYCLE_LABELS:
            await _answer_callback_query(callback_query_id, "Unbekannt", bot_token)
            return
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == aid)
            )).scalar_one_or_none()
            if ang is None:
                await _answer_callback_query(callback_query_id, "Weg", bot_token)
                return
            ang.status = new_status
            if new_status == ANGEBOT_STATUS_ACCEPTED:
                import datetime as _dt
                ang.accepted_at = _dt.datetime.now(_dt.timezone.utc)
            await s.commit()
            kunde_name = ang.kunde_name
        await _answer_callback_query(
            callback_query_id,
            f"Status: {AUFTRAG_LIFECYCLE_LABELS.get(new_status, new_status)}",
            bot_token,
        )
        await _send_to_chat(
            chat_id,
            f"📂 <b>{_h_safe(kunde_name)}</b>: Status gesetzt auf "
            f"{AUFTRAG_LIFECYCLE_LABELS.get(new_status, new_status)}.\n\n"
            f"Mit /auftraege siehst du alle, mit /auftrag_{str(aid)[:8]} "
            f"die naechsten Schritte."
        )
        return

    if action == "fertig":
        # User bestaetigt: Arbeit fertig — Rechnung finalisieren + versenden
        await _answer_callback_query(
            callback_query_id, "Schicke Rechnung raus…", bot_token,
        )
        await _send_to_chat(
            chat_id,
            "🏁 <b>Auftrag fertig</b> — finalisiere Rechnung in Lexware "
            "und sende sie mit Anschreiben an den Kunden…",
        )
        result = await _run_rechnung_versand_pipeline(aid)
        await _send_to_chat(chat_id, result)
        return

    await _answer_callback_query(callback_query_id, "Unbekannt", bot_token)


async def _run_rechnung_versand_pipeline(angebot_id) -> str:
    """Bei 🏁 Fertig: Rechnung in Lexware finalisieren + Mail mit PDF an
    den Kunden. Setzt Status auf rechnung_gesendet wenn alles klappt.

    Strategie: wir legen die Invoice NEU als finalized an (Lexware bietet
    keine 'draft -> open'-Konvertierung an). Der alte Draft kann manuell
    in Lexware geloescht werden — er stoert nicht.
    """
    from core.integrations.angebot_mail import (
        send_rechnung_to_customer,
    )

    async with AsyncSessionLocal() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one_or_none()
        if ang is None:
            return "❌ Auftrag nicht gefunden."
        positions = (await s.execute(
            select(AngebotPosition).where(AngebotPosition.angebot_id == angebot_id)
            .order_by(AngebotPosition.position_nr)
        )).scalars().all()
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == ang.tenant_id)
        )).scalar_one()
        kunde_name = ang.kunde_name
        kunde_email = ang.kunde_email
        kunde_strasse = ang.kunde_strasse
        kunde_plz = ang.kunde_plz
        kunde_ort = ang.kunde_ort
        custom_intro = ang.introduction_text

    provider = await _get_lexware_provider_for_tenant(tenant)
    if provider is None:
        return "❌ Lexware ist nicht eingerichtet."

    line_items = [
        InvoiceLineItem(
            name=p.name,
            quantity=float(p.menge),
            unit_name=p.einheit or "Stueck",
            unit_price_gross=float(p.preis_brutto_eur),
            description=p.beschreibung,
            tax_rate_percent=int(p.mwst_prozent or 19),
        )
        for p in positions
    ]
    one_time_address = {"name": kunde_name, "countryCode": "DE"}
    if kunde_strasse:
        one_time_address["street"] = kunde_strasse
    if kunde_plz:
        one_time_address["zip"] = kunde_plz
    if kunde_ort:
        one_time_address["city"] = kunde_ort

    report = [f"🧾 <b>Rechnung: {_h_safe(kunde_name)}</b>", ""]

    # Stufe 1: Finalisierte Rechnung in Lexware anlegen
    intro_text = (
        f"Sehr geehrte Damen und Herren,\n\nvielen Dank fuer den "
        f"Auftrag. Nachstehend unsere Rechnung."
    )
    if custom_intro:
        # Wir nutzen das gleiche personalisierte Anschreiben wie im
        # Angebot — passt meist auch zur Rechnung ("danke fuer den
        # Auftrag, hier die Abrechnung").
        intro_text = custom_intro
    try:
        invoice = await provider.create_invoice_draft(
            line_items=line_items,
            one_time_address=one_time_address,
            title=f"Rechnung {kunde_name}",
            introduction=intro_text,
            remark="Bitte begleichen Sie den Rechnungsbetrag innerhalb von 14 Tagen.",
            tax_type="gross",
            finalize=True,
        )
    except Exception as exc:
        logger.exception(f"Rechnungs-Finalisierung gescheitert: {exc}")
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_id)
            )).scalar_one()
            ang.status = ANGEBOT_STATUS_WORK_DONE  # bleibt im "Fertig"-Status
            await s.commit()
        return (
            f"❌ Rechnung konnte nicht angelegt werden: "
            f"<code>{_h_safe(str(exc)[:200])}</code>\n\n"
            "Status bleibt 'Fertig' — du kannst es nochmal versuchen."
        )

    # IDs persistieren — alte lexware_invoice_id (Draft) wird ueberschrieben.
    async with AsyncSessionLocal() as s:
        ang = (await s.execute(
            select(Angebot).where(Angebot.id == angebot_id)
        )).scalar_one()
        ang.lexware_invoice_id = invoice.invoice_id
        ang.status = ANGEBOT_STATUS_WORK_DONE
        await s.commit()

    report.append(
        f"✅ Rechnung in Lexware angelegt — "
        f"<a href=\"{invoice.deeplink_view}\">oeffnen</a>"
    )

    # Stufe 2: Email-Fallback aus Lexware-Kontakten
    if not kunde_email and kunde_name and len(kunde_name.strip()) >= 3:
        try:
            contacts = await provider.search_contacts(
                kunde_name, customer_only=True, limit=5,
            )
        except Exception:
            contacts = []
        chosen = None
        kn_low = kunde_name.lower()
        for c in contacts:
            if c.email and any(tok in (c.name or "").lower() for tok in kn_low.split()):
                chosen = c.email
                break
        if not chosen:
            for c in contacts:
                if c.email:
                    chosen = c.email
                    break
        if chosen:
            kunde_email = chosen
            report.append(
                f"🔎 Mail-Adresse aus Lexware-Kontakten: <code>{_h_safe(chosen)}</code>"
            )
            async with AsyncSessionLocal() as s:
                ang2 = (await s.execute(
                    select(Angebot).where(Angebot.id == angebot_id)
                )).scalar_one()
                ang2.kunde_email = chosen
                await s.commit()

    # Stufe 3: Mail an Kunden mit Rechnungs-PDF
    if not kunde_email:
        report.append(
            "⚠️ Keine Kunden-Mail vorhanden — Rechnung manuell aus "
            "Lexware versenden. Status bleibt auf 'Fertig'."
        )
        return "\n".join(report)

    try:
        mail_result = await send_rechnung_to_customer(
            angebot_id=angebot_id, to_email=kunde_email,
        )
    except Exception as exc:
        logger.exception(f"Rechnungs-Mail-Versand crashed: {exc}")
        mail_result = {"success": False, "error": str(exc)}

    if mail_result.get("success"):
        async with AsyncSessionLocal() as s:
            ang = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_id)
            )).scalar_one()
            ang.status = ANGEBOT_STATUS_RECHNUNG_GESENDET
            await s.commit()
        report.append(
            f"✅ Rechnung per Mail an <b>{_h_safe(kunde_email)}</b> versendet"
        )
        report.append("")
        report.append("🎉 <b>Auftrag abgeschlossen.</b>")
    else:
        err = mail_result.get("error", "unbekannt")
        report.append(
            f"⚠️ Mail-Versand fehlgeschlagen — <i>{_h_safe(err[:160])}</i>\n"
            "Status bleibt auf 'Fertig'. Im /auftraege erneut anstossen."
        )

    return "\n".join(report)


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
    """Erstellt die Lexware-Rechnung (Draft) aus den extrahierten Daten.

    Beta-1 B1-7: Race-Schutz mit SELECT FOR UPDATE — verhindert dass
    zwei parallele Bestaetigungs-Klicks zwei Lexware-Vouchers erzeugen.
    """
    async with AsyncSessionLocal() as s:
        rg = (await s.execute(
            select(Rechnung)
            .where(Rechnung.id == rechnung_id)
            .with_for_update()
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

        # Race-Schutz: anderer Klick hat die Rechnung schon im Verlauf
        if rg.status == RECHNUNG_STATUS_CREATING:
            await _send_to_chat(
                chat_id,
                "Rechnung wird gerade angelegt — bitte einen Moment warten.",
            )
            return
        if rg.status in (
            RECHNUNG_STATUS_MAIL_SENT, RECHNUNG_STATUS_MAIL_QUEUED,
            RECHNUNG_STATUS_BEZAHLT,
        ):
            await _send_to_chat(
                chat_id,
                f"Rechnung ist schon im Status '{rg.status}' — nichts zu tun.",
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


async def _handle_rechnung_pruefen_command(chat_id) -> str:
    """Manueller 'Jetzt pruefen'-Befehl: triggert Lexware-Polling sofort
    statt auf den naechsten 30min-Cron-Lauf zu warten.
    """
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    try:
        from core.integrations.rechnung_payment_monitor import (
            check_pending_invoices_for_tenant,
        )
        summary = await check_pending_invoices_for_tenant(tenant.id)
    except Exception as e:
        logger.exception(f"Manueller /rechnung_pruefen failed: {e}")
        return f"Pruefung fehlgeschlagen: {str(e)[:120]}"

    if summary["checked"] == 0:
        return "Keine offenen Rechnungen zum Pruefen."

    parts = [
        f"<b>Pruefung fertig</b>",
        f"• {summary['checked']} Rechnungen geprueft",
    ]
    if summary["paid"] > 0:
        parts.append(f"• 💰 {summary['paid']} neu als bezahlt markiert")
    if summary["errors"] > 0:
        parts.append(f"• ⚠️ {summary['errors']} API-Fehler")
    if summary["no_change"] == summary["checked"] and summary["paid"] == 0:
        parts.append("• Status unveraendert (alle weiter offen)")
    parts.append("\nDetails: /rechnungen_anzeigen")
    return "\n".join(parts)


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
            from core.models.rechnung import LEXWARE_PARTIAL_PAID_STATES
            voucher_lc = (rg.lexware_voucher_status or "").lower()
            if voucher_lc == "voided":
                marker = "🚫 storniert"
            elif voucher_lc == "cancelled":
                marker = "❌ in Lexware geloescht"
            elif voucher_lc in LEXWARE_PARTIAL_PAID_STATES:
                marker = "🟡 teilweise bezahlt"
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
        elif rg.status == "mail_queued":
            # Phase A5: Mail in Retry-Queue, Cron versucht's noch.
            lines.append(
                f'• {ts} {kunde} {betrag} ⏳ Mailversand verzoegert '
                f'(automatischer Retry)'
            )
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
            logger.exception(f"Tenant-Kopie fehlgeschlagen (nicht kritisch): {e}")

    except BrevoError as e:
        # Phase A5: in Retry-Queue legen statt sofort auf ERROR.
        # Status='mail_queued' signalisiert dem Tenant "wir versuchen
        # es im Hintergrund weiter". Cron erledigt 3 Retries; bei
        # endgueltigem Fehler setzt _on_dead_letter dann den Status
        # auf ERROR und schickt Push.
        from core.integrations.mail_retry_cron import enqueue_failed_mail
        from core.models import (
            MAIL_TYPE_RECHNUNG, RECHNUNG_STATUS_MAIL_QUEUED,
        )
        async with AsyncSessionLocal() as s:
            rg = (await s.execute(
                select(Rechnung).where(Rechnung.id == rechnung_id)
            )).scalar_one_or_none()
            if rg:
                rg.status = RECHNUNG_STATUS_MAIL_QUEUED
                rg.error_message = f"Brevo (queued): {str(e)[:300]}"
                await s.commit()
        await enqueue_failed_mail(
            tenant_id=tenant.id,
            mail_type=MAIL_TYPE_RECHNUNG,
            recipient_email=rg_data["kunde_email"],
            subject=subject,
            html_body=html_body,
            attachments=[{
                "filename": pdf_filename,
                "mime_type": "application/pdf",
                "content_bytes": pdf_bytes,
            }],
            from_name=tenant_data["company_name"],
            to_name=rg_data["kunde_name"],
            reply_to=tenant_data["contact_email"],
            reply_to_name=tenant_data["contact_name"],
            rechnung_id=rechnung_id,
            last_error=str(e),
        )
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            f"⚠️ Mail-Versand verzoegert (HTTP {e.status_code}). "
            f"Wird automatisch in 5 Min nochmal versucht. "
            f"Status: /rechnungen_anzeigen",
        )
        return
    except Exception as e:
        logger.exception(f"Mailversand unerwartet fehlgeschlagen: {e}")
        # Auch hier in die Retry-Queue — bei unbekanntem Fehler ist es
        # genauso wert nachzuversuchen.
        try:
            from core.integrations.mail_retry_cron import enqueue_failed_mail
            from core.models import (
                MAIL_TYPE_RECHNUNG, RECHNUNG_STATUS_MAIL_QUEUED,
            )
            async with AsyncSessionLocal() as s:
                rg = (await s.execute(
                    select(Rechnung).where(Rechnung.id == rechnung_id)
                )).scalar_one_or_none()
                if rg:
                    rg.status = RECHNUNG_STATUS_MAIL_QUEUED
                    rg.error_message = f"Unbekannt (queued): {str(e)[:300]}"
                    await s.commit()
            await enqueue_failed_mail(
                tenant_id=tenant.id,
                mail_type=MAIL_TYPE_RECHNUNG,
                recipient_email=rg_data["kunde_email"],
                subject=subject,
                html_body=html_body,
                attachments=[{
                    "filename": pdf_filename,
                    "mime_type": "application/pdf",
                    "content_bytes": pdf_bytes,
                }],
                from_name=tenant_data["company_name"],
                to_name=rg_data["kunde_name"],
                reply_to=tenant_data["contact_email"],
                reply_to_name=tenant_data["contact_name"],
                rechnung_id=rechnung_id,
                last_error=str(e),
            )
        except Exception as inner:
            logger.warning(f"enqueue_failed_mail crashed too: {inner}")
        await _clear_state(chat_id)
        await _send_to_chat(
            chat_id,
            "⚠️ Mailversand fehlgeschlagen. Wird automatisch wiederholt. "
            "Status: /rechnungen_anzeigen",
        )
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
    from core.models import Employee, Tenant
    from config.settings import settings as _settings
    from urllib.parse import urlencode as _urlencode

    async with AsyncSessionLocal() as s:
        emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.slug == slug,
            )
        )).scalar_one_or_none()
        if emp is None:
            return f"Mitarbeiter <b>{slug}</b> nicht gefunden."
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )).scalar_one()

    bot_username = await _get_bot_username()
    # Telegram-Deeplink: Inhaber bekommt einfachen tenant-Slug,
    # weitere Mitarbeiter brauchen das __<emp_slug>-Suffix damit der
    # Bot beim ersten /start die richtige Person zuordnet.
    if bot_username:
        if emp.is_default:
            tg_deeplink = f"https://t.me/{bot_username}?start={tenant.slug}"
        else:
            tg_deeplink = (
                f"https://t.me/{bot_username}?start={tenant.slug}__{emp.slug}"
            )
    else:
        tg_deeplink = "(Bot-Username unbekannt)"

    # OAuth-Deeplink — funktioniert sobald Provider gewaehlt ist
    base = (_settings.public_url or "").rstrip("/")
    oauth_deeplink = ""
    if base and emp.calendar_provider:
        qs = _urlencode({
            "tenant": tenant.slug,
            "provider": emp.calendar_provider,
            "employee": emp.slug,
        })
        oauth_deeplink = f"{base}/oauth/start?{qs}"

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
    cal_label = _kalender_label(emp.calendar_provider)
    parts = [
        f"<b>{emp.name}</b>{flag}{active}",
        f"Slug: <code>{emp.slug}</code>",
        f"E-Mail: {emp.contact_email or '—'}",
        f"Telegram: {chat_str}",
        f"Heimat: {heimat}",
        f"Kalender: {cal_label}",
        f"Skills: {_format_skills(emp.skills)}",
        "",
        f"<b>Telegram-Onboarding:</b>\n<code>{tg_deeplink}</code>",
    ]
    if oauth_deeplink:
        parts.append(
            f"\n<b>OAuth-Connect-Link:</b>\n<code>{oauth_deeplink}</code>"
        )
    elif not emp.calendar_provider:
        parts.append(
            "\n<i>Kein Kalender gewaehlt — Mitarbeiter soll "
            "/kalender_verbinden im eigenen Chat ausfuehren.</i>"
        )
    return "\n".join(parts)


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
# Kalender-Verbinden-Wizard (Outlook + Google)
# =====================================================================


def _kalender_label(provider: str | None) -> str:
    if provider == "google":
        return "Google Calendar"
    if provider == "microsoft":
        return "Microsoft Outlook"
    return "(noch nicht verbunden)"


def _has_oauth_token(emps_provider_pairs, expected_provider: str) -> bool:
    """Helper-Stub — nicht jetzt benutzt aber spaeter fuer Status-Anzeige."""
    return False


async def _handle_kalender_verbinden_command(chat_id) -> None:
    """Wizard-Start: zeigt 2 Inline-Buttons (Google / Microsoft)."""
    res = await _get_current_employee(chat_id)
    if res is None:
        await _send_to_chat(
            chat_id,
            "Dieser Chat ist noch keinem Betrieb zugeordnet. "
            "Bitte zuerst /start ausfuehren.",
        )
        return
    tenant, emp = res

    # State setzen, damit Callback weiss zu welchem Mitarbeiter
    await _save_state(
        chat_id, STATE_KALENDER_PROVIDER_CHOICE,
        {"employee_id": str(emp.id), "tenant_slug": tenant.slug},
    )

    aktuell = _kalender_label(emp.calendar_provider)
    msg = (
        f"<b>📅 Kalender verbinden — {emp.name}</b>\n\n"
        f"Aktuell: {aktuell}\n\n"
        "Welchen Kalender willst du verknuepfen?\n"
        "Du wirst auf die Login-Seite des Anbieters weitergeleitet "
        "und nach dem Login zurueck zum Bot."
    )
    buttons = [[
        {"text": "📅 Google Calendar", "callback_data": f"kal:google:{emp.slug}"},
        {"text": "📧 Microsoft Outlook", "callback_data": f"kal:microsoft:{emp.slug}"},
    ]]
    await _send_with_inline_buttons(chat_id, msg, buttons)


async def _handle_kalender_status_command(chat_id) -> str:
    """Zeigt Status der Kalender-Verbindung des aktuellen Mitarbeiters."""
    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    _, emp = res
    label = _kalender_label(emp.calendar_provider)
    cal_id = emp.calendar_id or "(primaerer Kalender)"
    msg = (
        f"<b>📅 Kalender — {emp.name}</b>\n\n"
        f"Provider: <b>{label}</b>\n"
        f"Kalender-ID: <code>{cal_id}</code>\n\n"
        "Mit /kalender_verbinden aendern."
    )
    return msg


async def _handle_kalender_callback(chat_id, cq_data, cq_id, bot_token) -> None:
    """Verarbeitet Klick auf Inline-Buttons aus /kalender_verbinden.

    cq_data Format: 'kal:<provider>:<employee_slug>'
    Aktion: setzt employee.calendar_provider, generiert OAuth-Deeplink,
    schickt ihn als Folgenachricht.
    """
    parts = cq_data.split(":", 2)
    if len(parts) != 3:
        await _answer_callback_query(cq_id, "Falsches Format", bot_token)
        return
    _, provider, emp_slug = parts
    if provider not in ("google", "microsoft"):
        await _answer_callback_query(cq_id, "Unbekannter Provider", bot_token)
        return

    res = await _get_current_employee(chat_id)
    if res is None:
        await _answer_callback_query(cq_id, "Nicht zugeordnet", bot_token)
        return
    tenant, emp = res
    if emp.slug != emp_slug:
        # Sicherheits-Check: User darf nur seinen eigenen Provider setzen
        await _answer_callback_query(
            cq_id, "Slug-Mismatch — Wizard erneut starten", bot_token,
        )
        return

    # Provider in DB speichern
    from core.models.employee import Employee
    async with AsyncSessionLocal() as s:
        emp_db = (await s.execute(
            select(Employee).where(Employee.id == emp.id)
        )).scalar_one_or_none()
        if emp_db is None:
            await _answer_callback_query(cq_id, "Mitarbeiter weg", bot_token)
            return
        emp_db.calendar_provider = provider
        # calendar_id zuruecksetzen → Default ('primary' bzw. /me/events)
        emp_db.calendar_id = None
        await s.commit()

    await _clear_state(chat_id)
    await _answer_callback_query(cq_id, f"{provider.capitalize()} gewaehlt", bot_token)

    # OAuth-Deeplink generieren — public_url + /oauth/start
    # Phase 1 Multi-OAuth: employee-Slug mitgeben damit Token am
    # Mitarbeiter-Datensatz landet (nicht nur tenant-weit).
    from config.settings import settings
    from urllib.parse import urlencode
    base = (settings.public_url or "").rstrip("/")
    qs = urlencode({
        "tenant": tenant.slug,
        "provider": provider,
        "employee": emp.slug,
    })
    oauth_url = f"{base}/oauth/start?{qs}"

    label = _kalender_label(provider)
    msg = (
        f"✅ <b>{label}</b> als Provider gespeichert.\n\n"
        f"<b>Jetzt einloggen:</b>\n"
        f"<a href=\"{oauth_url}\">{oauth_url}</a>\n\n"
        "Klick den Link, melde dich mit deinem "
        f"{'Google' if provider == 'google' else 'Microsoft'}-Account an, "
        "und erlaube Zugriff auf den Kalender. Danach landet das Token im "
        "System und du kriegst Termine direkt in deinen Kalender.\n\n"
        "<i>Tipp:</i> mit /kalender_status kannst du jederzeit pruefen "
        "welcher Provider aktiv ist."
    )
    await _send_to_chat(chat_id, msg, bot_token=bot_token)


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
        if not expected:
            # Production: hartes Veto — kein Webhook ohne Secret.
            # Dev: nur Warning, damit lokales Probieren ohne Setup geht.
            if settings.is_production:
                logger.error(
                    "Telegram-Webhook ABGELEHNT: TELEGRAM_WEBHOOK_SECRET ist "
                    "in Production zwingend erforderlich",
                )
                raise PermissionError("webhook-secret-missing-in-production")
            logger.warning(
                "Telegram-Webhook ohne Secret (dev) — Production wuerde dies blockieren",
            )
        else:
            got = (headers or {}).get("x-telegram-bot-api-secret-token", "")
            # Constant-Time-Vergleich gegen Timing-Attacks
            import hmac
            if not hmac.compare_digest(got, expected):
                raise PermissionError("invalid-telegram-secret")
        logger.info(f"Telegram-Webhook empfangen: endpoint={endpoint}")
        if endpoint == "incoming":
            return await process_telegram_update(payload)
        return {"ok": True, "note": f"unknown endpoint: {endpoint}"}


# =====================================================================
# Material-Verwaltung + /bestellen
# =====================================================================
# Verbrauchs-Artikel (Schrauben, Klebstoff, Akku, ...) die der Handwerker
# regelmaessig nachbestellt. Tenant pflegt einen Katalog mit Bestell-URL,
# /bestellen <slug> zeigt einen anklickbaren Telegram-Button.
# Bewusst KEINE Mail-Bestellung — Sven wollte URL-only.

async def _handle_material_list_command(chat_id):
    """Zeigt alle aktiven Materialien des Tenants."""
    from core.models.tenant_material import TenantMaterial
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        materialien = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.tenant_id == tenant.id)
            .where(TenantMaterial.aktiv.is_(True))
            .order_by(TenantMaterial.name)
        )).scalars().all()

    if not materialien:
        return (
            "Noch keine Materialien angelegt.\n\n"
            "Mit <b>/material neu</b> das erste anlegen — du gibst Name "
            "und einen Bestell-Link, der Bot zeigt dir den dann auf Wunsch "
            "als anklickbaren Button."
        )

    lines = [f"<b>Materialien ({len(materialien)}):</b>\n"]
    for m in materialien:
        lieferant_label = f" · {m.lieferant_name}" if m.lieferant_name else ""
        lines.append(
            f"• <code>{_h_safe(m.slug)}</code> — {_h_safe(m.name)}{lieferant_label}"
        )
    lines.append("")
    lines.append("Bestellen: <b>/bestellen &lt;slug&gt; [menge]</b>")
    lines.append("Details:   <b>/material &lt;slug&gt;</b>")
    return "\n".join(lines)


async def _handle_material_neu_command(chat_id):
    """Startet den Anlege-Wizard (4 Schritte)."""
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    # Inhaber-Schutz: nur Default-Employee darf neue Materialien anlegen
    ok, err, _, _ = await _ensure_inhaber_or_explain(chat_id)
    if not ok:
        return err

    await _save_state(chat_id, STATE_MATERIAL_NEU_NAME, {})
    return (
        "<b>➕ Neues Material anlegen</b>\n\n"
        "<b>Schritt 1/4 — Wie heisst es?</b>\n\n"
        "Beispiele:\n"
        "• <i>Schrauben Edelstahl 5mm</i>\n"
        "• <i>Akku Bohrer XL18</i>\n"
        "• <i>Silikon transparent</i>\n\n"
        "/abbrechen um abzubrechen."
    )


async def _handle_material_neu_name_input(chat_id, text: str):
    """Schritt 1: Name."""
    name = (text or "").strip()
    if not name or len(name) < 2:
        return "Name ist zu kurz (min. 2 Zeichen). Bitte erneut oder /abbrechen."
    if len(name) > 200:
        return "Name ist zu lang (max 200 Zeichen). Bitte kuerzer."
    await _save_state(
        chat_id, STATE_MATERIAL_NEU_LINK, {"name": name},
    )
    return (
        f"<b>{_h_safe(name)}</b>\n\n"
        "<b>Schritt 2/4 — Bestell-Link</b>\n\n"
        "Schick mir die URL wo du das Material bestellst. Beispiele:\n"
        "• <i>https://www.toolnation.de/p/12345</i>\n"
        "• <i>https://www.amazon.de/dp/B0XXXXXXX</i>\n"
        "• <i>https://www.wuerth.de/...</i>\n\n"
        "Tip: wenn der Shop einen 'Direkt-in-Warenkorb'-Link unterstuetzt "
        "(Amazon: <code>/gp/aws/cart/add.html?ASIN.1=...</code>), kannst du "
        "den hier eintragen. Dann landet der Artikel beim Klick direkt im "
        "Warenkorb und du musst nur noch zur Kasse."
    )


async def _handle_material_neu_link_input(chat_id, text: str, state_data: dict | None):
    """Schritt 2: URL."""
    url = (text or "").strip()
    if not url:
        return "URL fehlt. Bitte einen kompletten Link schicken oder /abbrechen."
    if not (url.startswith("http://") or url.startswith("https://")):
        return (
            "Bitte einen kompletten Link mit <code>http://</code> oder "
            "<code>https://</code> schicken. Oder /abbrechen."
        )
    if len(url) > 2000:
        return "URL ist zu lang (max 2000 Zeichen)."
    data = dict(state_data or {})
    data["bestell_link"] = url
    await _save_state(chat_id, STATE_MATERIAL_NEU_LIEFERANT, data)
    return (
        "<b>Schritt 3/4 — Lieferant (optional)</b>\n\n"
        "Wer ist der Lieferant? Wird im Bestell-Button angezeigt z.B. "
        "<i>'Schrauben jetzt bei Toolnation bestellen'</i>.\n\n"
        "Schick den Namen — oder <b>/skip</b> falls dir das egal ist."
    )


async def _handle_material_neu_lieferant_input(
    chat_id, text: str, state_data: dict | None,
):
    """Schritt 3: Lieferant + Speichern."""
    txt = (text or "").strip()
    data = dict(state_data or {})
    if txt.lower() in {"/skip", "skip", "nein", "-"}:
        data["lieferant_name"] = None
    else:
        if len(txt) > 200:
            return "Lieferant-Name ist zu lang (max 200 Zeichen)."
        data["lieferant_name"] = txt

    # Direkt speichern — keine vierte Bestaetigungs-Stufe noetig wenn die
    # Daten schon validiert sind. Tenant kann mit /material <slug>
    # bearbeiten/loeschen falls nicht passt.
    return await _save_material_from_wizard(chat_id, data)


async def _save_material_from_wizard(chat_id, data: dict) -> str:
    """Persistiert das neue Material + clearet den State."""
    from core.models.tenant_material import TenantMaterial
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden. Bitte /start erneut machen."

    name = data.get("name") or ""
    bestell_link = data.get("bestell_link") or ""
    lieferant_name = data.get("lieferant_name")

    # Slug + Kollisions-Check
    base_slug = _slugify(name) or "material"
    async with AsyncSessionLocal() as s:
        existing_slugs = {
            r[0] for r in (await s.execute(
                select(TenantMaterial.slug)
                .where(TenantMaterial.tenant_id == tenant.id)
            )).all()
        }
    slug = base_slug
    counter = 2
    while slug in existing_slugs:
        slug = f"{base_slug}-{counter}"
        counter += 1
        if counter > 99:
            await _clear_state(chat_id)
            return "Zu viele Materialien mit diesem Namen. Bitte anders nennen."

    async with AsyncSessionLocal() as s:
        m = TenantMaterial(
            tenant_id=tenant.id,
            slug=slug,
            name=name,
            bestell_link=bestell_link,
            lieferant_name=lieferant_name,
            einheit="Stück",
            standard_menge=1,
            aktiv=True,
        )
        s.add(m)
        await s.commit()

    await _clear_state(chat_id)

    lieferant_line = f"\n<b>Lieferant:</b> {_h_safe(lieferant_name)}" if lieferant_name else ""
    return (
        "<b>✅ Material angelegt</b>\n\n"
        f"<b>Name:</b> {_h_safe(name)}\n"
        f"<b>Slug:</b> <code>{slug}</code>{lieferant_line}\n\n"
        f"Jetzt bestellen mit:\n<b>/bestellen {slug}</b>"
    )


async def _handle_material_command(chat_id, args: str):
    """Detail-View oder Sub-Befehle: /material <slug> [bearbeiten|loeschen|aktivieren|deaktivieren]"""
    from core.models.tenant_material import TenantMaterial, MaterialBestellung
    parts = args.split(None, 1)
    if not parts:
        return await _handle_material_list_command(chat_id)
    slug = parts[0].strip().lower()
    sub = parts[1].strip().lower() if len(parts) > 1 else ""

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.tenant_id == tenant.id)
            .where(TenantMaterial.slug == slug)
        )).scalar_one_or_none()

        if not m:
            return f"Material <code>{_h_safe(slug)}</code> nicht gefunden. Liste mit /material"

        # Sub-Aktionen (Inhaber-only)
        if sub in {"loeschen", "deaktivieren"}:
            ok, err, _, _ = await _ensure_inhaber_or_explain(chat_id)
            if not ok:
                return err
            m.aktiv = False
            await s.commit()
            return f"❌ <b>{_h_safe(m.name)}</b> deaktiviert. Mit /material {slug} aktivieren wieder anschalten."

        if sub == "aktivieren":
            ok, err, _, _ = await _ensure_inhaber_or_explain(chat_id)
            if not ok:
                return err
            m.aktiv = True
            await s.commit()
            return f"✅ <b>{_h_safe(m.name)}</b> aktiviert."

        # Detail-View — letzte 5 Bestellungen
        recent = (await s.execute(
            select(MaterialBestellung)
            .where(MaterialBestellung.material_id == m.id)
            .order_by(MaterialBestellung.created_at.desc())
            .limit(5)
        )).scalars().all()

    parts_msg = [
        f"<b>{_h_safe(m.name)}</b>",
        f"<i>slug:</i> <code>{m.slug}</code>",
    ]
    if m.lieferant_name:
        parts_msg.append(f"<i>Lieferant:</i> {_h_safe(m.lieferant_name)}")
    parts_msg.append(f"<i>Standard-Menge:</i> {m.standard_menge} {m.einheit}")
    parts_msg.append(f"<i>Status:</i> {'aktiv' if m.aktiv else 'deaktiviert'}")
    parts_msg.append(f"\n<b>Bestell-Link:</b>\n{_h_safe(m.bestell_link)}")
    if recent:
        parts_msg.append("\n<b>Letzte Bestellungen:</b>")
        for r in recent:
            ts = r.created_at.strftime("%d.%m. %H:%M") if r.created_at else "-"
            parts_msg.append(f"  • {ts} — {r.menge} {r.einheit}")
    parts_msg.append("")
    parts_msg.append(f"<b>Bestellen:</b> /bestellen {m.slug}")
    if m.aktiv:
        parts_msg.append(f"<b>Deaktivieren:</b> /material {m.slug} deaktivieren")
    else:
        parts_msg.append(f"<b>Aktivieren:</b> /material {m.slug} aktivieren")
    return "\n".join(parts_msg)


# =====================================================================
# /bestellen — Inline-URL-Button auf Bestellseite
# =====================================================================

async def _handle_bestellen_list_command(chat_id):
    """/bestellen ohne Argument: Quick-Liste der aktiven Materialien."""
    from core.models.tenant_material import TenantMaterial
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        materialien = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.tenant_id == tenant.id)
            .where(TenantMaterial.aktiv.is_(True))
            .order_by(TenantMaterial.name)
            .limit(20)
        )).scalars().all()

    if not materialien:
        return (
            "Noch keine Materialien zum Bestellen.\n\n"
            "Mit <b>/material neu</b> das erste anlegen."
        )

    lines = ["<b>🛒 Materialien zum Nachbestellen:</b>\n"]
    for m in materialien:
        lines.append(f"• /bestellen {m.slug} — {_h_safe(m.name)}")
    lines.append("")
    lines.append("Mit Menge: <b>/bestellen &lt;slug&gt; 5</b>")
    return "\n".join(lines)


async def _handle_bestellen_command(chat_id, args: str):
    """/bestellen <slug> [menge] — zeigt URL-Button + loggt die Bestellung."""
    from core.models.tenant_material import (
        TenantMaterial, MaterialBestellung, BESTELL_ART_LINK,
    )

    parts = args.split()
    slug = parts[0].strip().lower() if parts else ""
    if not slug:
        return await _handle_bestellen_list_command(chat_id)

    # Optional menge im Argument
    menge_arg = None
    if len(parts) > 1:
        try:
            menge_arg = int(parts[1])
            if menge_arg <= 0 or menge_arg > 9999:
                return "Menge muss zwischen 1 und 9999 sein."
        except ValueError:
            return f"'{_h_safe(parts[1])}' ist keine Zahl. Format: /bestellen {slug} 5"

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    # Material laden
    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.tenant_id == tenant.id)
            .where(TenantMaterial.slug == slug)
            .where(TenantMaterial.aktiv.is_(True))
        )).scalar_one_or_none()

    if not m:
        return (
            f"Kein aktives Material <code>{_h_safe(slug)}</code> gefunden.\n\n"
            "Liste: /material"
        )

    # Wenn keine Menge angegeben + standard_menge > 1: zurueck-fragen
    if menge_arg is None and m.standard_menge != 1:
        await _save_state(
            chat_id, STATE_BESTELLEN_MENGE,
            {"material_id": str(m.id)},
        )
        return (
            f"<b>{_h_safe(m.name)}</b>\n\n"
            f"Wie viele <b>{m.einheit}</b> bestellen?\n\n"
            f"Standard: {m.standard_menge}\n"
            f"Schick eine Zahl oder <b>/skip</b> für Standard."
        )

    menge = menge_arg if menge_arg is not None else m.standard_menge
    return await _ausloesen_bestellung(chat_id, m, menge)


async def _handle_bestellen_menge_input(
    chat_id, text: str, state_data: dict | None,
):
    """Schritt 2 wenn /bestellen ohne Menge geschickt wurde."""
    from core.models.tenant_material import TenantMaterial

    txt = (text or "").strip().lower()
    data = state_data or {}
    material_id = data.get("material_id")
    if not material_id:
        await _clear_state(chat_id)
        return "Wizard-Session abgelaufen. Bitte /bestellen erneut."

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        await _clear_state(chat_id)
        return "Tenant nicht gefunden."

    async with AsyncSessionLocal() as s:
        m = (await s.execute(
            select(TenantMaterial)
            .where(TenantMaterial.id == material_id)
        )).scalar_one_or_none()
    if not m:
        await _clear_state(chat_id)
        return "Material nicht mehr gefunden."

    if txt in {"/skip", "skip", "-"}:
        menge = m.standard_menge
    else:
        try:
            menge = int(txt)
            if menge <= 0 or menge > 9999:
                return "Menge muss zwischen 1 und 9999 sein. Erneut?"
        except ValueError:
            return f"'{_h_safe(text)}' ist keine Zahl. Bitte Zahl oder /skip."

    await _clear_state(chat_id)
    return await _ausloesen_bestellung(chat_id, m, menge)


async def _ausloesen_bestellung(chat_id, material, menge: int) -> str:
    """Erzeugt den Telegram-URL-Button + loggt die Bestellung in DB."""
    from core.models.tenant_material import MaterialBestellung, BESTELL_ART_LINK

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Tenant nicht gefunden."

    # Optional employee_id ermitteln (wer hat den Befehl gegeben)
    employee_id = None
    try:
        emp = await _get_current_employee(chat_id)
        if emp:
            employee_id = emp[1].id if isinstance(emp, tuple) else None
    except Exception:
        pass

    # Audit-Log einfuegen
    try:
        async with AsyncSessionLocal() as s:
            log = MaterialBestellung(
                tenant_id=tenant.id,
                material_id=material.id,
                employee_id=employee_id,
                material_name=material.name,
                bestell_link=material.bestell_link,
                menge=menge,
                einheit=material.einheit,
                bestell_art=BESTELL_ART_LINK,
            )
            s.add(log)
            await s.commit()
    except Exception as e:
        logger.warning(f"material_bestellung-Log failed: {e}")

    # Button-Text + URL
    lieferant_part = f" bei {_h_safe(material.lieferant_name)}" if material.lieferant_name else ""
    btn_text = f"🛒 {menge} {material.einheit} {material.name[:40]} bestellen{lieferant_part if material.lieferant_name else ''}"
    if len(btn_text) > 64:  # Telegram-Limit fuer Button-Text
        btn_text = f"🛒 {menge}x {material.name[:50]} bestellen"
    if len(btn_text) > 64:
        btn_text = f"🛒 Jetzt bestellen ({menge} {material.einheit})"

    text_msg = (
        f"<b>🛒 Bestellung vorbereitet</b>\n\n"
        f"<b>{_h_safe(material.name)}</b>\n"
        f"Menge: {menge} {material.einheit}\n"
    )
    if material.lieferant_name:
        text_msg += f"Lieferant: {_h_safe(material.lieferant_name)}\n"
    text_msg += "\nKlick den Button zum Bestellen:"

    bot_token = await _load_global_bot_token()
    if bot_token is None:
        # Fallback: Link als Text
        return text_msg + f"\n\n{material.bestell_link}"

    sent = await _send_with_inline_buttons(
        chat_id, text_msg,
        [[{"text": btn_text, "url": material.bestell_link}]],
        bot_token=bot_token,
    )
    if not sent:
        return text_msg + f"\n\n{material.bestell_link}"
    return None  # Button schon gesendet, kein zusaetzlicher Reply


async def _handle_bestellungen_list_command(chat_id):
    """Letzte 20 Bestellungen des Tenants."""
    from core.models.tenant_material import MaterialBestellung
    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    async with AsyncSessionLocal() as s:
        bestellungen = (await s.execute(
            select(MaterialBestellung)
            .where(MaterialBestellung.tenant_id == tenant.id)
            .order_by(MaterialBestellung.created_at.desc())
            .limit(20)
        )).scalars().all()

    if not bestellungen:
        return "Noch keine Bestellungen ausgeloest.\n\nMit /bestellen die erste auswählen."

    lines = [f"<b>Letzte {len(bestellungen)} Bestellungen:</b>\n"]
    for b in bestellungen:
        ts = b.created_at.strftime("%d.%m. %H:%M") if b.created_at else "-"
        lines.append(f"• {ts} — {b.menge} {b.einheit} <b>{_h_safe(b.material_name)}</b>")
    return "\n".join(lines)


def _h_safe(s) -> str:
    """HTML-escape einen String fuer Telegram-HTML-parse_mode."""
    import html as _html
    return _html.escape(str(s or ""))


# =====================================================================
# Google-Drive-Archiv (pro Kunde Ordner, Bilder/PDFs ueber Telegram)
# =====================================================================
# Workflow:
#   /drive_verbinden      → einmaliger OAuth-Re-Auth (Drive-Scope)
#   /drive_status         → Verbindung + Statistik
#   /archiv               → Liste aller Kunden mit Drive-Ordner
#   /archiv <name>        → Wizard-Start: Bot wartet auf Files
#   <Photo/Document/PDF>  → Upload in Kunden-Ordner (lazy create)
#   /fertig               → Wizard schliessen, Drive-Link zeigen
#
# Failsafe-Pattern: alle Drive-Errors fangen wir, schicken die Klartext-
# Meldung im Telegram (statt Stacktraces) und brechen den Wizard nicht
# ab — der User kann es nochmal versuchen.

ARCHIV_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB Telegram-Bot-API-Limit


async def _handle_drive_verbinden_command(chat_id):
    """Schickt OAuth-Deeplink mit Drive-Scope.

    Calendar-Token bleibt gueltig — der naechste OAuth-Roundtrip
    erweitert nur den Scope-Set des Tokens und behaelt den Refresh-Token.
    """
    res = await _get_current_employee(chat_id)
    if res is None:
        return "Erst /start ausfuehren — Chat ist keinem Betrieb zugeordnet."
    tenant, emp = res

    from core.security.oauth_token_lookup import find_oauth_token
    from core.integrations.google_drive import is_drive_configured
    tok = await find_oauth_token(tenant.id, "google", emp.id)
    if tok and is_drive_configured(tok):
        return (
            "✅ <b>Drive ist verbunden.</b>\n"
            "Mit <b>/archiv &lt;kundenname&gt;</b> kannst du loslegen."
        )

    from config.settings import settings
    from urllib.parse import urlencode
    base = (settings.public_url or "").rstrip("/")
    qs = urlencode({
        "tenant": tenant.slug,
        "provider": "google",
        "employee": emp.slug,
    })
    oauth_url = f"{base}/oauth/start?{qs}"

    msg = (
        "<b>☁️ Drive verbinden</b>\n"
        "Einmal-Login. Calendar bleibt verbunden — wir erweitern nur "
        "den Scope.\n\n"
        "<i>Q sieht nur die Ordner die er selbst anlegt. "
        "Private Files bleiben unsichtbar.</i>"
    )
    sent = await _send_with_inline_buttons(
        chat_id, msg,
        [[{"text": "Drive freigeben", "url": oauth_url}]],
    )
    if sent:
        return None  # type: ignore[return-value]
    # Fallback ohne Buttons
    return msg + f"\n\nLink: {oauth_url}"


async def _handle_drive_status_command(chat_id) -> str:
    """Zeigt Drive-Verbindungs-Status + Statistik."""
    from core.security.oauth_token_lookup import find_oauth_token
    from core.integrations.google_drive import (
        is_drive_configured, list_tenant_kunde_drives,
    )

    res = await _get_current_employee(chat_id)
    if res is None:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."
    tenant, emp = res

    tok = await find_oauth_token(tenant.id, "google", emp.id)
    if not tok:
        return (
            "❌ <b>Drive nicht verbunden.</b>\n\n"
            "Mit /drive_verbinden den OAuth-Flow starten."
        )
    if not is_drive_configured(tok):
        return (
            "⚠️ <b>Google ist verbunden, aber ohne Drive-Scope.</b>\n\n"
            "Bitte einmal /drive_verbinden ausfuehren — Calendar bleibt "
            "weiter verbunden."
        )

    folders = await list_tenant_kunde_drives(tenant.id, limit=100)
    total_uploads = sum(int(f.upload_count or 0) for f in folders)
    last_upload = None
    for f in folders:
        if f.last_upload_at and (
            last_upload is None or f.last_upload_at > last_upload
        ):
            last_upload = f.last_upload_at
    last_str = (
        last_upload.strftime("%d.%m.%Y %H:%M") if last_upload else "—"
    )

    msg = (
        "<b>☁️ Drive</b>  ✅\n"
        f"{len(folders)} Ordner  ·  {total_uploads} Dateien  ·  letzter {last_str}\n"
    )
    if not folders:
        msg += (
            "Mit /archiv &lt;name&gt; den ersten anlegen."
        )
    return msg


async def _handle_archiv_list_command(chat_id) -> str:
    """Listet alle Kunden mit Drive-Ordner — sortiert nach last_upload."""
    from core.integrations.google_drive import list_tenant_kunde_drives

    tenant = await _get_tenant_by_chat(chat_id)
    if not tenant:
        return "Dieser Chat ist noch keinem Betrieb zugeordnet."

    folders = await list_tenant_kunde_drives(tenant.id, limit=30)
    if not folders:
        return (
            "<b>📁 Kunden-Archiv (Google Drive)</b>\n\n"
            "Noch keine Kunden mit Drive-Ordner.\n\n"
            "Lege den ersten an mit:\n"
            "<b>/archiv &lt;Kundenname&gt;</b>\n\n"
            "Beispiel: <code>/archiv Mueller</code>\n\n"
            "Falls noch nicht passiert: einmal /drive_verbinden zur "
            "OAuth-Freigabe."
        )

    lines = [
        f"<b>📁 Kunden-Archiv</b> — {len(folders)} Kunden\n",
    ]
    for f in folders[:20]:
        ts = (
            f.last_upload_at.strftime("%d.%m.%y") if f.last_upload_at else "-"
        )
        lines.append(
            f"• <b>{_h_safe(f.kunde_name)}</b> — "
            f"{f.upload_count or 0} Files, letzter: {ts}"
        )
    if len(folders) > 20:
        lines.append(f"\n<i>... und {len(folders) - 20} weitere</i>")
    lines.append("")
    lines.append(
        "Neuen Upload starten: <b>/archiv &lt;name&gt;</b>"
    )
    return "\n".join(lines)


async def _handle_archiv_command(chat_id, args: str) -> str:
    """Wizard-Start: '/archiv Mueller' setzt State + erwartet Files."""
    name = (args or "").strip()
    if not name or len(name) < 2:
        return (
            "Bitte einen Kunden-Namen mitgeben.\n\n"
            "Beispiel: <code>/archiv Mueller</code>"
        )
    if len(name) > 200:
        return "Kunden-Name ist zu lang (max 200 Zeichen)."

    res = await _get_current_employee(chat_id)
    if res is None:
        return (
            "Dieser Chat ist noch keinem Betrieb zugeordnet. "
            "Bitte zuerst /start ausfuehren."
        )
    tenant, emp = res

    # Drive-Verbindung pruefen — sonst direkt freundlicher Hint statt
    # 5-Sekunden-Wartezeit beim ersten Upload-Versuch.
    from core.security.oauth_token_lookup import find_oauth_token
    from core.integrations.google_drive import is_drive_configured
    tok = await find_oauth_token(tenant.id, "google", emp.id)
    if not tok or not is_drive_configured(tok):
        return (
            "⚠️ <b>Drive ist noch nicht verbunden.</b>\n\n"
            "Bitte einmal /drive_verbinden ausfuehren — danach klappt "
            "/archiv direkt."
        )

    # State setzen
    await _save_state(
        chat_id, STATE_ARCHIV_WAITING_FILES,
        {
            "kunde_name": name,
            "employee_id": str(emp.id),
            "tenant_id": str(tenant.id),
            "uploaded": 0,
        },
    )

    return (
        f"<b>📁 {_h_safe(name)}</b>\n"
        "Schick Bilder/PDFs. Mehrere OK.\n"
        "<b>/fertig</b> zum Abschliessen."
    )


async def _handle_archiv_file_received(
    chat_id, photo_array, document, bot_token, state_data,
) -> str:
    """Verarbeitet ein Foto oder Dokument waehrend STATE_ARCHIV_WAITING_FILES.

    Lade die Bytes von Telegram, schiebt sie via google_drive-Helper in
    den Kunden-Ordner. Failsafe — bei Drive-Fehlern sehen wir Klartext
    statt Stacktrace.
    """
    from core.integrations.google_drive import upload_file_to_kunde_folder

    data = state_data or {}
    kunde_name = data.get("kunde_name") or "Unbekannt"
    tenant_id_str = data.get("tenant_id")
    employee_id_str = data.get("employee_id")
    if not tenant_id_str:
        await _clear_state(chat_id)
        return "Wizard-State korrupt. Bitte /archiv erneut starten."

    try:
        tenant_id = uuid.UUID(tenant_id_str)
        employee_id = uuid.UUID(employee_id_str) if employee_id_str else None
    except (ValueError, TypeError):
        await _clear_state(chat_id)
        return "Wizard-State korrupt. Bitte /archiv erneut starten."

    # File-Identifikation: Photo (groesste Variante) oder Document
    file_id = None
    filename = None
    mime_type = None
    file_size = 0

    if photo_array:
        # Photo: nehme groesste Variante (telegram liefert mehrere Resolutions)
        largest = max(
            photo_array, key=lambda p: int(p.get("file_size") or 0),
        )
        file_id = largest.get("file_id")
        file_size = int(largest.get("file_size") or 0)
        # Telegram-Photos haben keinen Dateinamen — wir generieren einen
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"foto_{ts}.jpg"
        mime_type = "image/jpeg"
    elif document:
        file_id = document.get("file_id")
        file_size = int(document.get("file_size") or 0)
        filename = document.get("file_name") or f"datei_{file_id}"
        mime_type = (
            document.get("mime_type") or "application/octet-stream"
        )
    else:
        return "Bitte ein Foto oder Dokument schicken (kein Text)."

    if not file_id:
        return "Datei konnte nicht identifiziert werden — nochmal versuchen."

    if file_size > ARCHIV_MAX_FILE_SIZE:
        return (
            f"Datei zu gross ({file_size // 1024 // 1024} MB). "
            f"Telegram erlaubt max 25 MB pro Datei. Bitte komprimieren "
            f"oder in mehreren Schritten schicken."
        )

    # Telegram-Download
    file_path = await _telegram_get_file_path(bot_token, file_id)
    if not file_path:
        return "Telegram-Download fehlgeschlagen — bitte erneut schicken."
    file_bytes = await _telegram_download_file(bot_token, file_path)
    if not file_bytes:
        return "Telegram-Download fehlgeschlagen — bitte erneut schicken."

    # Drive-Upload
    try:
        result = await upload_file_to_kunde_folder(
            tenant_id=tenant_id,
            kunde_name=kunde_name,
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            employee_id=employee_id,
        )
    except ValueError as e:
        # Token-/Scope-Fehler — sauberer Reset, User soll re-authen
        msg = str(e)
        logger.warning(f"Drive-Upload ValueError: {msg}")
        return (
            f"⚠️ <b>Drive-Upload fehlgeschlagen</b>\n\n"
            f"{_h_safe(msg)}\n\n"
            "Bitte einmal /drive_verbinden — danach /archiv erneut."
        )
    except Exception as e:
        # Quota / Network / API-Error — Wizard NICHT abbrechen
        # damit User naechste Datei probieren kann.
        err = str(e)
        logger.exception(f"Drive-Upload-Fehler: {err[:200]}")
        if "quotaExceeded" in err or "storageQuotaExceeded" in err:
            hint = (
                "Drive-Speicher voll. In Drive aufraumen oder Workspace-"
                "Plan upgraden, dann erneut versuchen."
            )
        elif "403" in err and "rateLimitExceeded" in err:
            hint = "Drive-API-Rate-Limit. In ein paar Sekunden erneut versuchen."
        else:
            hint = (
                "Unerwarteter Drive-Fehler. Bitte erneut versuchen oder "
                "/drive_status pruefen."
            )
        return f"⚠️ <b>Upload fehlgeschlagen</b>\n\n{hint}"

    # Counter im State hochzaehlen
    new_data = dict(data)
    new_data["uploaded"] = int(data.get("uploaded") or 0) + 1
    new_data["folder_url"] = result.get("kunde_folder_url") or ""
    new_data["folder_id"] = result.get("kunde_folder_id") or ""
    await _save_state(chat_id, STATE_ARCHIV_WAITING_FILES, new_data)

    return (
        f"✅ <b>{_h_safe(filename)}</b> abgelegt "
        f"({new_data['uploaded']} fuer {_h_safe(kunde_name)})\n\n"
        "Naechste Datei schicken oder mit <b>/fertig</b> abschliessen."
    )


async def _handle_archiv_fertig_command(chat_id, state_data) -> str:
    """Schliesst den /archiv-Wizard ab. Zeigt Anzahl + Drive-Link."""
    data = state_data or {}
    kunde_name = data.get("kunde_name") or "Unbekannt"
    uploaded = int(data.get("uploaded") or 0)
    folder_url = data.get("folder_url") or ""

    await _clear_state(chat_id)

    if uploaded == 0:
        return (
            f"Wizard fuer <b>{_h_safe(kunde_name)}</b> beendet — "
            f"keine Dateien hochgeladen.\n\n"
            "Mit /archiv erneut starten."
        )

    # Drive-Link via Lookup falls im State noch nicht vorhanden (sollte sein)
    if not folder_url:
        try:
            from core.integrations.google_drive import get_kunde_folder_link
            tenant = await _get_tenant_by_chat(chat_id)
            if tenant:
                folder_url = await get_kunde_folder_link(
                    tenant.id, kunde_name,
                ) or ""
        except Exception as e:
            logger.debug(f"folder_url-lookup im /fertig egal: {e}")

    msg = (
        f"<b>✅ {uploaded} Datei{'en' if uploaded != 1 else ''} archiviert</b>\n\n"
        f"Kunde: <b>{_h_safe(kunde_name)}</b>\n"
    )
    if folder_url:
        msg += f"\n<a href=\"{folder_url}\">📁 Drive-Ordner oeffnen</a>"
        # Plus Inline-Button fuer einfachen Mobile-Klick
        await _send_with_inline_buttons(
            chat_id, msg,
            [[{"text": f"📁 Ordner '{kunde_name}' oeffnen", "url": folder_url}]],
        )
        return None  # type: ignore[return-value]
    return msg
