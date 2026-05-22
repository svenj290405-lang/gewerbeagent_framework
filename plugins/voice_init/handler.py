"""
voice_init Plugin: Conversation-Initiation-Webhook fuer ElevenLabs.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import (
    ALLE_KATEGORIEN,
    KATEGORIE_LABELS,
    Tenant,
    TenantKnowledge,
    ToolConfig,
)
from core.models.employee import Employee
from core.routing.employee_router import RoutingDecision, choose_employee
from core.integrations.lexware import LexwareProvider
from core.integrations.accounting_base import AccountingError
from core.security import decrypt
from core.plugin_system import BasePlugin
from plugins.voice_init.manifest import MANIFEST

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Storno-Token-Cache (Voice-Pipeline)
# ----------------------------------------------------------------------
# In-Memory-Cache fuer Stornier-Tokens. Wir verschicken keine event_ids
# direkt an den Anrufer/Agent — sondern kurzlebige, einmalig einloesbare
# Tokens. Damit kann niemand mit erratener event_id einen fremden Termin
# loeschen, und Replay-Versuche scheitern automatisch.
#
# Phase 1: in-process dict ohne Redis. Bei Multi-Worker-Setup waeren
# Tokens an einen einzelnen Worker gebunden — aktuell laeuft das
# Framework single-worker (uvicorn ohne --workers), darum ok. Wenn
# multi-worker kommt: auf Redis umstellen.
STORNIER_TOKEN_TTL_SECONDS = 30 * 60  # 30 Minuten
STORNIER_TOKEN_MAX_ENTRIES = 1000     # safety cap

_STORNIER_TOKENS: dict[str, dict[str, Any]] = {}


def _gc_stornier_tokens() -> None:
    """Garbage-collect abgelaufene Tokens. Wird vor jedem Insert
    aufgerufen — kostet O(n) aber n bleibt klein."""
    now = datetime.now(timezone.utc)
    stale = [
        tok for tok, entry in _STORNIER_TOKENS.items()
        if (now - entry["created_at"]).total_seconds() > STORNIER_TOKEN_TTL_SECONDS
    ]
    for tok in stale:
        _STORNIER_TOKENS.pop(tok, None)
    # Hard cap falls trotzdem zu viele: aelteste rausschmeissen
    if len(_STORNIER_TOKENS) > STORNIER_TOKEN_MAX_ENTRIES:
        sorted_items = sorted(
            _STORNIER_TOKENS.items(), key=lambda kv: kv[1]["created_at"],
        )
        for tok, _ in sorted_items[: len(_STORNIER_TOKENS) - STORNIER_TOKEN_MAX_ENTRIES]:
            _STORNIER_TOKENS.pop(tok, None)


def _create_stornier_token(
    tenant_id: UUID, event_id: str, employee_id: str | None,
) -> str:
    """Generiert einen Stornier-Token fuer ein Event und speichert das Mapping."""
    _gc_stornier_tokens()
    token = secrets.token_urlsafe(16)
    _STORNIER_TOKENS[token] = {
        "tenant_id": str(tenant_id),
        "event_id": event_id,
        "employee_id": employee_id,
        "created_at": datetime.now(timezone.utc),
        "used": False,
    }
    return token


def _consume_stornier_token(
    token: str, expected_tenant_id: UUID,
) -> dict[str, Any] | None:
    """Loest einen Token ein. Returns Token-Daten oder None.

    None-Gruende (alle relevant zum Loggen, nicht zum Mit-User-Sharen):
    - Token unbekannt
    - Token expired (>30 min)
    - Token bereits eingeloest
    - Tenant-Mismatch (Token gehoert anderem Tenant)
    """
    entry = _STORNIER_TOKENS.get(token)
    if entry is None:
        return None
    if entry["used"]:
        return None
    age = (datetime.now(timezone.utc) - entry["created_at"]).total_seconds()
    if age > STORNIER_TOKEN_TTL_SECONDS:
        return None
    if entry["tenant_id"] != str(expected_tenant_id):
        return None
    # Atomar einlosen: erst markieren, dann zurueckgeben
    entry["used"] = True
    return entry


# ----------------------------------------------------------------------
# Terminsuche-Job-Store (asynchrone Voice-Pipeline)
# ----------------------------------------------------------------------
# checke_kalender (Gemini-Skill-Routing + Kalender-API) dauert bis ~9s.
# Damit der Voice-Agent nicht so lange stumm blockiert, laeuft die Suche
# als Hintergrund-Task: der Agent startet sie via `starte_terminsuche`
# (sofortige job_id-Antwort, dann redet er weiter — Webformular-Hinweis
# etc.) und holt das Ergebnis via `hole_terminvorschlaege` ab.
#
# Gleiche single-worker-Begruendung wie beim Stornier-Token-Cache oben:
# in-process dict reicht, weil das Framework single-worker laeuft (uvicorn
# ohne --workers). Bei Multi-Worker waeren Jobs an einen Worker gebunden
# -> dann auf Redis/DB umstellen.
TERMINSUCHE_JOB_TTL_SECONDS = 5 * 60   # nach 5 min raeumen wir auf
TERMINSUCHE_JOB_MAX_ENTRIES = 500      # safety cap

_TERMINSUCHE_JOBS: dict[str, dict[str, Any]] = {}


def _gc_terminsuche_jobs() -> None:
    """GC abgelaufener Terminsuche-Jobs (vor jedem Insert/Lookup)."""
    now = datetime.now(timezone.utc)
    stale = [
        jid for jid, e in _TERMINSUCHE_JOBS.items()
        if (now - e["created_at"]).total_seconds() > TERMINSUCHE_JOB_TTL_SECONDS
    ]
    for jid in stale:
        _TERMINSUCHE_JOBS.pop(jid, None)
    if len(_TERMINSUCHE_JOBS) > TERMINSUCHE_JOB_MAX_ENTRIES:
        sorted_items = sorted(
            _TERMINSUCHE_JOBS.items(), key=lambda kv: kv[1]["created_at"],
        )
        for jid, _ in sorted_items[: len(_TERMINSUCHE_JOBS) - TERMINSUCHE_JOB_MAX_ENTRIES]:
            _TERMINSUCHE_JOBS.pop(jid, None)


def _normalize_phone(num):
    """Bringt eine Telefonnummer in einheitliches Format (+49...).
    ElevenLabs koennte mit oder ohne + senden, mit/ohne Leerzeichen etc."""
    if not num:
        return None
    s = str(num).strip().replace(" ", "").replace("-", "")
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        return "+" + s[2:]
    if s.startswith("0"):
        # Vermutlich deutsche Nummer ohne Laendercode
        return "+49" + s[1:]
    if s.isdigit():
        return "+" + s
    return s


async def _find_tenant_by_phone(phone_number):
    """Findet Tenant anhand der angerufenen Nummer."""
    normalized = _normalize_phone(phone_number)
    if not normalized:
        return None
    async with AsyncSessionLocal() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.voice_phone_number == normalized)
        )).scalar_one_or_none()
        if t:
            s.expunge(t)
        return t


async def _load_knowledge(tenant_id):
    """Holt alle Wissens-Eintraege eines Tenants, gruppiert nach Kategorie."""
    async with AsyncSessionLocal() as s:
        entries = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == tenant_id)
            .order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at)
        )).scalars().all()
    by_kat = {}
    for e in entries:
        by_kat.setdefault(e.kategorie, []).append(e.text)
    return by_kat


def _build_knowledge_block(by_kat):
    """Baut einen lesbaren Wissens-Block fuer den System-Prompt."""
    if not by_kat:
        return "Es liegen noch keine spezifischen Betriebs-Informationen vor."
    parts = []
    for kat in ALLE_KATEGORIEN:
        if kat not in by_kat:
            continue
        label = KATEGORIE_LABELS.get(kat, kat)
        parts.append(f"## {label}")
        for text in by_kat[kat]:
            parts.append(f"- {text}")
        parts.append("")
    return "\n".join(parts).strip()


def _split_wunschzeit(wunschzeit):
    """Zerlegt einen Wunschzeit-String in (datum, uhrzeit).

    ElevenLabs liefert die Wunschzeit als ein Feld — je nach Agent-Prompt
    als ISO ('2026-05-20T14:00'), mit Leerzeichen ('2026-05-20 14:00')
    oder im deutschen Format ('20.05.2026 14:00'). Das kalender-Plugin
    erwartet datum + uhrzeit getrennt und parst beide Datumsformate
    selbst. Fehlt die Uhrzeit, ankern wir auf 09:00 (Tagesanfang).
    """
    s = (wunschzeit or "").strip()
    if "T" in s:
        datum, _, uhrzeit = s.partition("T")
    elif " " in s:
        datum, _, uhrzeit = s.partition(" ")
    else:
        datum, uhrzeit = s, ""
    datum = datum.strip()
    uhrzeit = uhrzeit.strip()[:5] or "09:00"
    return datum, uhrzeit


# Slot-IDs sind bewusst zustandslos: das kalender-Plugin kennt keine
# persistenten Slot-Objekte, Termine sind nur datum+uhrzeit. Wir kodieren
# die Slot-Koordinaten in einen String den der Voice-Agent unveraendert
# von checke_kalender an buche_termin zurueckreicht — kein DB-Lookup noetig.
_SLOT_ID_SEP = "|"


def _encode_slot_id(datum, uhrzeit, dauer_min):
    return f"{datum}{_SLOT_ID_SEP}{uhrzeit}{_SLOT_ID_SEP}{int(dauer_min)}"


def _decode_slot_id(slot_id):
    parts = (slot_id or "").split(_SLOT_ID_SEP)
    if len(parts) != 3:
        raise ValueError(f"Ungueltige slot_id: {slot_id!r}")
    datum, uhrzeit, dauer_raw = parts
    return datum.strip(), uhrzeit.strip(), int(dauer_raw)


# ---------------------------------------------------------------------
# Skill-Routing-Helper (Teil A der Multi-Mitarbeiter-Voice-Anbindung)
# ---------------------------------------------------------------------


def _parse_wunschzeit_for_routing(datum, uhrzeit):
    """Parst datum+uhrzeit zu datetime fuer choose_employee.target_datetime.

    Liefert None bei Parse-Fehler — Router skipt dann den Verfuegbarkeits-
    Filter (Absence + Arbeitstag + Arbeitszeit) statt zu crashen. Wenn das
    Datum echt kaputt ist, liefert kalender.find_free_slots danach eine
    eigene Fehlermeldung.
    """
    try:
        from plugins.kalender.handler import Plugin as _KalenderPlugin
        return _KalenderPlugin._parse_datum_uhrzeit(datum, uhrzeit)
    except Exception:
        return None


async def _ensure_calendar_capable_routing(tenant_id, routing):
    """Faengt Routing-Entscheidungen auf, deren Employee keinen Kalender hat.

    Hintergrund: `choose_employee()` darf — laut Skill-Score allein —
    einen Mitarbeiter ohne `calendar_provider` waehlen. Der Adapter
    fiele auf Google + fehlenden OAuth-Token zurueck und wuerde
    spaeter beim find_free_slots brechen. Hier korrigieren wir vor
    der Buchung auf den Default-Employee (falls der einen Kalender
    hat) mit reason='no-calendar' — der ursprueglich gerouteten
    Slug landet im debug-Block fuer Audit.
    """
    if routing is None:
        return None
    async with AsyncSessionLocal() as s:
        emp = (await s.execute(
            select(Employee).where(Employee.id == routing.employee_id)
        )).scalar_one_or_none()
        if emp is None or emp.calendar_provider:
            return routing
        original_slug = emp.slug
        default_emp = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.is_default.is_(True),
            )
        )).scalar_one_or_none()
        if default_emp is None or not default_emp.calendar_provider:
            # Auch der Default hat keinen Kalender — find_free_slots wird
            # mit Tenant-Setup-Fehler antworten, das Routing lassen wir
            # zur besseren Diagnose stehen.
            return routing
        return RoutingDecision(
            employee_id=default_emp.id,
            employee_name=default_emp.name,
            employee_slug=default_emp.slug,
            reason="no-calendar",
            score=0.0,
            debug={
                "original_routing": {
                    "employee_slug": original_slug,
                    "reason": routing.reason,
                }
            },
        )


def _routing_to_response(routing):
    """RoutingDecision → kompakter Dict fuer die Voice-Tool-Response.

    reason wird angereichert: bei skill-match haengen wir die getroffenen
    Skills an (`skill-match: tischler, holz`), damit der Voice-Agent
    dem Kunden bei Rueckfrage erklaeren kann warum gerade dieser
    Mitarbeiter passt.
    """
    if routing is None:
        return None
    reason = routing.reason
    needed = (
        routing.debug.get("needed_skills")
        if isinstance(routing.debug, dict) else None
    )
    if reason == "skill-match" and needed:
        reason_str = f"skill-match: {', '.join(needed)}"
    else:
        reason_str = reason
    return {
        "employee_id": str(routing.employee_id),
        "employee_slug": routing.employee_slug,
        "employee_name": routing.employee_name,
        "reason": reason_str,
        "score": float(routing.score),
    }


# DSGVO-Pflicht-Ansage (Art. 13 DSGVO; § 201 StGB bei Aufzeichnung). Wird
# als dynamic_variable `datenschutz_hinweis` an ElevenLabs gegeben. Der
# Agent MUSS sie in seiner first_message zu Beginn vorlesen — die Variable
# allein wirkt nicht, das first_message-Template im ElevenLabs-Dashboard
# muss `{{datenschutz_hinweis}}` einbinden.
#
# WICHTIG: Falls ElevenLabs Audio/Transkripte speichert, ist dieser Text
# zu erweitern ("Das Gespraech wird aufgezeichnet") und es ist i.d.R. eine
# Einwilligung noetig — Aufnahme ohne Hinweis ist nach § 201 StGB strafbar.
# Die Retention-Einstellung im ElevenLabs-Dashboard pruefen.
DATENSCHUTZ_HINWEIS = (
    "Kurzer Hinweis vorab: Sie sprechen mit einem digitalen KI-Assistenten. "
    "Ihre Angaben werden zur Bearbeitung Ihres Anliegens verarbeitet. "
    "Informationen zum Datenschutz und zu Ihren Rechten erhalten Sie "
    "jederzeit auf Nachfrage oder auf der Webseite des Betriebs."
)


class Plugin(BasePlugin):
    """voice_init Plugin: liefert Init-Daten fuer ElevenLabs-Conversations."""

    manifest = MANIFEST

    async def on_webhook(self, endpoint, payload, headers=None):
        # Signature-Verifikation: ElevenLabs sendet HMAC-SHA256 ueber den
        # Raw-Body im 'ElevenLabs-Signature'-Header, wenn beim Webhook-Setup
        # ein Secret gesetzt wurde. Ohne Verifikation kann jeder gefakete
        # Anrufe einschmuggeln (Lexware-Kontakte unter falschen Tenants
        # anlegen, Telegram-Pushes ausloesen).
        # Hinweis: wir haben hier nur das geparste Payload, nicht den Raw-
        # Body — strenge HMAC-Verifizierung wuerde einen Raw-Body-Hook im
        # zentralen Dispatcher brauchen. Pragmatischer Mittelweg: Secret
        # als statischer Header-Vergleich gegen 'X-Webhook-Secret'.
        from config.settings import settings
        expected = (settings.elevenlabs_webhook_secret or "").strip()
        # Production-Hard-Veto: ohne gesetztes Secret ist der Webhook offen
        # — jeder koennte gefakete Anrufe einschmuggeln (Termine buchen/
        # stornieren, Lexware-Kontakte anlegen, Telegram-Pushes faken) und
        # via Payload-`tenant_slug` sogar fuer fremde Betriebe. Lieber
        # fail-closed (Voice abgelehnt) als offen. In Dev bleibt es offen
        # fuer lokales Testen.
        if not expected:
            if settings.is_production:
                logger.error(
                    "voice_init: ELEVENLABS_WEBHOOK_SECRET ist in Production "
                    "nicht gesetzt — Webhook wird abgelehnt (fail-closed). "
                    "Secret in .env UND als Custom-Header 'X-Webhook-Secret' "
                    "im ElevenLabs-Tool setzen."
                )
                raise PermissionError("elevenlabs-secret-not-configured")
            logger.warning(
                "voice_init: kein ELEVENLABS_WEBHOOK_SECRET gesetzt — "
                "Signatur-Pruefung uebersprungen (nur Dev erlaubt)."
            )
        else:
            got = (headers or {}).get("x-webhook-secret", "") or (
                headers or {}
            ).get("elevenlabs-signature", "")
            import hmac
            if not hmac.compare_digest(got, expected):
                raise PermissionError("invalid-elevenlabs-secret")

        if endpoint == "initiation":
            return await self._handle_initiation(payload)
        if endpoint == "save_contact":
            return await self._handle_save_contact(payload)
        if endpoint == "checke_kalender":
            return await self._handle_checke_kalender(payload)
        if endpoint == "starte_terminsuche":
            return await self._handle_starte_terminsuche(payload)
        if endpoint == "hole_terminvorschlaege":
            return await self._handle_hole_terminvorschlaege(payload)
        if endpoint == "buche_termin":
            return await self._handle_buche_termin(payload)
        if endpoint == "finde_termine":
            return await self._handle_finde_termine(payload)
        if endpoint == "storniere_termin":
            return await self._handle_storniere_termin(payload)
        if endpoint == "wissensbasis":
            return await self._handle_wissensbasis(payload)
        if endpoint == "call_ended":
            return await self._handle_call_ended(payload)
        return {"error": f"Unbekannter Endpunkt: {endpoint}"}

    async def _handle_initiation(self, payload):
        """
        Wird von ElevenLabs bei jedem eingehenden Anruf aufgerufen.

        Erwartet (vereinfacht):
          { "caller_id": "...", "called_number": "+49...", "agent_id": "..." }
        oder via SIP-Trunk:
          { "call_sid": "...", "to": "+49...", "from": "..." }

        Gibt zurueck:
          {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
              "tenant_company_name": "...",
              "tenant_branche": "...",
              "tenant_knowledge_block": "..."
            }
          }
        """
        # called_number aus den verschiedenen moeglichen Feldern lesen
        called = (
            payload.get("called_number")
            or payload.get("to_number")
            or payload.get("to")
            or payload.get("destination_number")
        )
        caller = (
            payload.get("caller_id")
            or payload.get("from_number")
            or payload.get("from")
            or payload.get("caller_number")
            or "unbekannt"
        )

        logger.info(
            f"voice_init: called={called!r} caller={caller!r}"
        )

        tenant = await _find_tenant_by_phone(called) if called else None

        if tenant is None:
            logger.warning(
                f"voice_init: Kein Tenant fuer called={called!r} gefunden, fallback"
            )
            return {
                "type": "conversation_initiation_client_data",
                "dynamic_variables": {
                    "tenant_company_name": "diesem Handwerksbetrieb",
                    "tenant_branche": "Handwerk",
                    "tenant_knowledge_block": "Es liegen aktuell keine spezifischen Informationen ueber den Betrieb vor.",
                    "datenschutz_hinweis": DATENSCHUTZ_HINWEIS,
                },
            }

        by_kat = await _load_knowledge(tenant.id)
        knowledge_block = _build_knowledge_block(by_kat)

        logger.info(
            f"voice_init: Tenant={tenant.slug} branche={tenant.branche} "
            f"knowledge_entries={sum(len(v) for v in by_kat.values())}"
        )

        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
                "tenant_slug": tenant.slug,
                "tenant_company_name": tenant.company_name or "",
                "tenant_branche": tenant.branche or "Handwerk",
                "tenant_knowledge_block": knowledge_block,
                "datenschutz_hinweis": DATENSCHUTZ_HINWEIS,
            },
        }


    async def _handle_checke_kalender(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'checke_kalender' aufruft.

        Erwartet payload:
          {
            "wunschzeit": "2026-05-20T14:00",
            "dauer_min": 90,            # optional, sonst kalender-Default
            "kunde_adresse": "...",     # optional, aktiviert Smart-Routing
            "anliegen": "Kuechenmontage", # optional, Basis fuer Skill-Routing
            "tenant_slug": "demo"
          }

        Routet zuerst per choose_employee() auf den passenden Mitarbeiter
        (Skill + Verfuegbarkeit + Distanz), delegiert dann an das
        kalender-Plugin (find_free_slots). Jeder Slot bekommt eine
        zustandslose slot_id; die gewaehlte employee_id wird im
        Response-`routing`-Block zurueckgegeben, damit der Voice-Agent
        sie spaeter an buche_termin durchreichen kann.
        """
        from core.plugin_system import get_plugin_for_tenant

        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "nachricht": "tenant_slug fehlt"}

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "nachricht": f"Tenant '{tenant_slug}' nicht gefunden",
            }

        kalender = await get_plugin_for_tenant(tenant_slug, "kalender")
        if kalender is None:
            logger.warning(
                f"checke_kalender: kalender-Plugin fuer Tenant {tenant_slug!r} "
                f"nicht verfuegbar/aktiviert"
            )
            return {
                "erfolg": False,
                "nachricht": "Der Kalender ist fuer diesen Betrieb nicht eingerichtet.",
            }

        # Slot-Suche + Skill-Routing — gemeinsame Logik mit der async-Variante
        # (starte_terminsuche). checke_kalender bleibt der synchrone Pfad
        # (Rueckwaerts-Kompatibilitaet / Fallback).
        return await self._run_terminsuche(tenant, kalender, payload)

    async def _run_terminsuche(self, tenant, kalender, payload):
        """Die eigentliche Termin-Slot-Suche: Skill-Routing + Kalender.

        Geteilt von `checke_kalender` (synchron) und `starte_terminsuche`
        (Hintergrund-Task). Erwartet bereits aufgeloeste `tenant` (ORM,
        Spalten geladen) + `kalender` (Plugin-Instanz). Liefert dieselbe
        Response-Struktur wie checke_kalender frueher.
        """
        datum, uhrzeit = _split_wunschzeit(payload.get("wunschzeit", ""))
        dauer_min = payload.get("dauer_min")
        kunde_adresse = (payload.get("kunde_adresse") or "").strip()
        anliegen = (payload.get("anliegen") or "").strip()

        # Skill-Routing: target_datetime fuer Verfuegbarkeits-Filter
        # (Absence + Arbeitstag + Arbeitszeit). Bei Parse-Fehler None,
        # dann skipt der Router den Filter und matcht nur ueber Skill.
        target_dt = _parse_wunschzeit_for_routing(datum, uhrzeit)
        routing = await choose_employee(
            tenant_id=tenant.id,
            anliegen_text=anliegen,
            kunde_adresse=kunde_adresse if kunde_adresse else None,
            target_datetime=target_dt,
        )
        # A.3: gewaehlten Mitarbeiter gegen calendar_provider validieren.
        routing = await _ensure_calendar_capable_routing(tenant.id, routing)

        kalender_payload = {"datum": datum, "uhrzeit": uhrzeit}
        if dauer_min:
            kalender_payload["dauer_minuten"] = int(dauer_min)
        if kunde_adresse:
            kalender_payload["kunde_adresse"] = kunde_adresse
        if routing is not None:
            kalender_payload["employee_id"] = routing.employee_id

        result = await kalender.on_webhook("find_free_slots", kalender_payload)
        if not result.get("erfolg"):
            return result

        # Dauer fuer die slot_id: explizit uebergeben oder kalender-Default.
        slot_dauer = int(dauer_min) if dauer_min else int(
            kalender.config.get("termin_dauer_minuten", 90)
        )
        slots = []
        for slot in result.get("slots", []):
            slot = dict(slot)
            slot["slot_id"] = _encode_slot_id(
                slot["datum"], slot["uhrzeit"], slot_dauer
            )
            slots.append(slot)

        routing_response = _routing_to_response(routing)
        logger.info(
            f"terminsuche: tenant={tenant.slug} wunsch={datum} {uhrzeit} "
            f"emp={routing.employee_slug if routing else '?'} "
            f"reason={routing.reason if routing else '-'} -> {len(slots)} Slots"
        )
        return {
            "erfolg": True,
            "slots": slots,
            "anzahl": len(slots),
            "smart_routing": result.get("smart_routing"),
            "routing": routing_response,
        }

    async def _handle_starte_terminsuche(self, payload):
        """Startet die Termin-Slot-Suche als Hintergrund-Task und gibt
        SOFORT eine job_id zurueck — damit der Voice-Agent weiterreden kann
        (Webformular-Hinweis, 'ich schaue beim passenden Ansprechpartner'),
        statt ~9s stumm zu blockieren. Ergebnis spaeter via
        hole_terminvorschlaege abholen.

        Payload identisch zu checke_kalender (wunschzeit, dauer_min,
        kunde_adresse, anliegen, tenant_slug). Die schnelle Validierung
        (Tenant + Kalender vorhanden) laeuft synchron, damit Setup-Fehler
        sofort gemeldet werden; nur die langsame Suche (Gemini-Routing +
        Kalender-API) wandert in den Hintergrund.
        """
        from core.plugin_system import get_plugin_for_tenant

        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "nachricht": "tenant_slug fehlt"}

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "nachricht": f"Tenant '{tenant_slug}' nicht gefunden",
            }

        kalender = await get_plugin_for_tenant(tenant_slug, "kalender")
        if kalender is None:
            logger.warning(
                f"starte_terminsuche: kalender-Plugin fuer Tenant "
                f"{tenant_slug!r} nicht verfuegbar/aktiviert"
            )
            return {
                "erfolg": False,
                "nachricht": "Der Kalender ist fuer diesen Betrieb nicht eingerichtet.",
            }

        _gc_terminsuche_jobs()
        job_id = secrets.token_urlsafe(12)
        _TERMINSUCHE_JOBS[job_id] = {
            "created_at": datetime.now(timezone.utc),
            "status": "laeuft",
            "result": None,
        }

        async def _worker():
            try:
                res = await self._run_terminsuche(tenant, kalender, payload)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"starte_terminsuche: Suche fehlgeschlagen job={job_id}"
                )
                res = {
                    "erfolg": False,
                    "nachricht": "Die Terminsuche ist fehlgeschlagen.",
                }
            entry = _TERMINSUCHE_JOBS.get(job_id)
            if entry is not None:  # koennte per TTL-GC weg sein
                entry["result"] = res
                entry["status"] = "fertig"

        # Task-Referenz im Job halten, sonst kann der GC den Task einsammeln
        # bevor er fertig ist (asyncio haelt nur schwache Referenzen).
        _TERMINSUCHE_JOBS[job_id]["task"] = asyncio.create_task(_worker())

        logger.info(
            f"starte_terminsuche: job={job_id} tenant={tenant_slug} gestartet"
        )
        return {
            "erfolg": True,
            "job_id": job_id,
            "status": "laeuft",
            "nachricht": (
                "Ich suche im Hintergrund einen Termin beim passenden "
                "Ansprechpartner."
            ),
        }

    async def _handle_hole_terminvorschlaege(self, payload):
        """Holt das Ergebnis einer via starte_terminsuche gestarteten Suche.

        Payload: {"job_id": "...", "tenant_slug": "..."}

        - Suche laeuft noch -> {"erfolg": True, "status": "laeuft"}
          (der Agent ueberbrueckt dann weiter und fragt gleich nochmal).
        - Fertig -> Slots + Routing (gleiche Struktur wie checke_kalender),
          plus "status": "fertig".
        - Unbekannt/abgelaufen -> {"erfolg": False, "status": "unbekannt"}.
        """
        job_id = (payload.get("job_id") or "").strip()
        if not job_id:
            return {"erfolg": False, "nachricht": "job_id fehlt"}

        _gc_terminsuche_jobs()
        entry = _TERMINSUCHE_JOBS.get(job_id)
        if entry is None:
            return {
                "erfolg": False,
                "status": "unbekannt",
                "nachricht": (
                    "Die Terminsuche ist abgelaufen oder unbekannt. "
                    "Bitte die Suche neu starten."
                ),
            }
        if entry["status"] != "fertig":
            return {"erfolg": True, "status": "laeuft"}

        # Fertig: Ergebnis zurueckgeben (bleibt bis TTL im Cache, damit ein
        # erneutes Nachfragen dasselbe liefert). Task-Objekt nie mit-
        # serialisieren.
        result = dict(entry.get("result") or {})
        result["status"] = "fertig"
        return result


    async def _handle_buche_termin(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'buche_termin' aufruft.

        Erwartet payload:
          {
            "slot_id": "20.05.2026|14:00|90",   # aus checke_kalender
            "employee_id": "...",                # aus checke_kalender.routing
            "anliegen": "Kuechenmontage",
            "kunde_name": "Frau Mueller",
            "kunde_telefon": "+49 ...",          # optional
            "kunde_email": "kunde@...",          # optional (Storno-Lookup)
            "kunde_adresse": "...",              # optional
            "tenant_slug": "demo"
          }

        Hinweis: die urspruengliche Spec sah 'kunde_id' vor. Das
        kalender-Plugin (book_appointment) traegt Name/Telefon/Adresse
        direkt ins Kalender-Event ein — es gibt keine Kunden-Tabelle mit
        IDs. Daher reicht der Voice-Agent die Kundendaten direkt durch.

        Wenn employee_id fehlt (z.B. wenn der Agent das Feld vergisst),
        wird das Skill-Routing on-the-fly aus anliegen+adresse+slot
        rekonstruiert — damit bleibt der Endpunkt robust auch wenn das
        Tool-Manifest im ElevenLabs-Dashboard noch nicht erweitert ist.
        """
        from core.plugin_system import get_plugin_for_tenant
        import uuid as _uuid

        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "nachricht": "tenant_slug fehlt"}

        slot_id = (payload.get("slot_id") or "").strip()
        try:
            datum, uhrzeit, dauer_min = _decode_slot_id(slot_id)
        except ValueError as e:
            logger.warning(f"buche_termin: {e}")
            return {
                "erfolg": False,
                "nachricht": (
                    "Der gewaehlte Termin-Slot ist ungueltig. Bitte zuerst "
                    "freie Termine abfragen."
                ),
            }

        name = (payload.get("kunde_name") or "").strip()
        if not name:
            return {"erfolg": False, "nachricht": "kunde_name fehlt"}

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "nachricht": f"Tenant '{tenant_slug}' nicht gefunden",
            }

        kalender = await get_plugin_for_tenant(tenant_slug, "kalender")
        if kalender is None:
            return {
                "erfolg": False,
                "nachricht": "Der Kalender ist fuer diesen Betrieb nicht eingerichtet.",
            }

        adresse = (payload.get("kunde_adresse") or "").strip()
        anliegen = (payload.get("anliegen") or "").strip()
        telefon = (payload.get("kunde_telefon") or "").strip()

        # kunde_email-Resolution (Clean-Architecture-Source-of-Truth):
        # 1. Server-seitiger Lookup ueber den juengsten AnfrageToken zu
        #    dieser Telefonnummer (created via _handle_save_contact —
        #    selbe Voice-Session-Lebensdauer = 2h)
        # 2. Fallback auf explicit payload-Feld (fuer non-voice-Caller,
        #    z.B. Mail-Pipeline die das Tool direkt anzapft, oder
        #    Legacy-Tests)
        # 3. Sonst: keine Mail im Kalender-Event — Storno-Lookup faellt
        #    spaeter auf Telefon-Suche zurueck (siehe find_events).
        from core.integrations.anfrage_forms import (
            lookup_recent_anfrage_by_phone,
        )
        from core.utils.phone import normalize_phone

        kunde_email_payload = (payload.get("kunde_email") or "").strip().lower()
        kunde_email = ""
        email_source = "none"
        token_id_short = ""
        phone_norm = normalize_phone(telefon) if telefon else ""
        if phone_norm:
            try:
                token = await lookup_recent_anfrage_by_phone(
                    tenant.id, phone_norm,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"buche_termin: lookup_recent_anfrage_by_phone "
                    f"({phone_norm[-4:]}***) crashed: {exc}"
                )
                token = None
            if token and token.kunde_email:
                kunde_email = token.kunde_email
                email_source = "anfrage_token"
                token_id_short = str(token.id)[:8]
        if not kunde_email and kunde_email_payload:
            kunde_email = kunde_email_payload
            email_source = "payload"

        # employee_id: bevorzugt aus dem Payload (Voice-Agent reicht die
        # checke_kalender-Routing-Entscheidung weiter). Fehlt sie, machen
        # wir das Routing neu mit denselben Daten — slot-datum/-uhrzeit
        # dienen als target_datetime fuer den Verfuegbarkeits-Filter.
        raw_emp_id = (payload.get("employee_id") or "").strip()
        routing = None
        employee_id = None
        if raw_emp_id:
            try:
                employee_id = _uuid.UUID(raw_emp_id)
            except (ValueError, TypeError):
                logger.warning(
                    f"buche_termin: ungueltige employee_id {raw_emp_id!r} — "
                    f"reroute via choose_employee"
                )
                employee_id = None
        if employee_id is None:
            target_dt = _parse_wunschzeit_for_routing(datum, uhrzeit)
            routing = await choose_employee(
                tenant_id=tenant.id,
                anliegen_text=anliegen,
                kunde_adresse=adresse if adresse else None,
                target_datetime=target_dt,
            )
            routing = await _ensure_calendar_capable_routing(
                tenant.id, routing,
            )
            employee_id = routing.employee_id if routing else None

        book_payload = {
            "name": name,
            "anliegen": anliegen,
            "datum": datum,
            "uhrzeit": uhrzeit,
            "dauer_minuten": dauer_min,
        }
        if telefon:
            book_payload["telefon"] = telefon
        if kunde_email:
            book_payload["kunde_email"] = kunde_email
        if adresse:
            book_payload["adresse"] = adresse
        if employee_id is not None:
            book_payload["employee_id"] = employee_id
            # Idempotency-Key enthaelt employee_id: derselbe Slot kann von
            # verschiedenen Mitarbeitern unabhaengig gebucht werden (eigene
            # Kalender), und Retries auf denselben (slot, employee)
            # liefern das gecachte Resultat statt doppelt anzulegen.
            book_payload["idempotency_key"] = (
                f"voice-{tenant_slug}-{employee_id}-{slot_id}"
            )

        result = await kalender.on_webhook("book_appointment", book_payload)
        if routing is not None and isinstance(result, dict) and result.get("erfolg"):
            result = {**result, "routing": _routing_to_response(routing)}
        # email_source = anfrage_token | payload | none — fuer Nachverfolg-
        # barkeit. token-id (short) nur geloggt wenn aus Token gezogen.
        email_log = f"email_source={email_source}"
        if token_id_short:
            email_log += f" token={token_id_short}"
        logger.info(
            f"buche_termin: tenant={tenant_slug} slot={slot_id} "
            f"emp={employee_id} name={name!r} erfolg={result.get('erfolg')} "
            f"{email_log}"
        )

        # Teil E.0: Kunden-Drive-Ordner anlegen + Link klickbar ins Event
        # eintragen (failsafe — ohne Drive laeuft die Buchung trotzdem).
        # Gleicher Mechanismus wie im Mail-Flow.
        booked_drive_url = None
        if isinstance(result, dict) and result.get("erfolg"):
            try:
                from core.integrations.google_drive import (
                    get_or_create_kunde_folder,
                )
                _fid, booked_drive_url = await get_or_create_kunde_folder(
                    tenant.id, name, employee_id=employee_id,
                    kunde_email=kunde_email or None,
                    kunde_telefon=telefon or None,
                )
            except Exception as e:
                logger.warning(
                    f"buche_termin: Drive-Kundenordner anlegen "
                    f"fehlgeschlagen (non-fatal): {e}"
                )
                booked_drive_url = None
            if booked_drive_url and result.get("event_id"):
                try:
                    await kalender.on_webhook("attach_drive_url", {
                        "event_id": result.get("event_id"),
                        "drive_url": booked_drive_url,
                        "employee_id": employee_id,
                    })
                except Exception as e:
                    logger.warning(
                        f"buche_termin: attach_drive_url fehlgeschlagen "
                        f"(non-fatal): {e}"
                    )

        # Teil E.1: Bestaetigungs-Mail an Kunde (best-effort, blockiert
        # die Buchungs-Response nicht). Nur wenn Buchung erfolgreich
        # UND wir eine Mail-Adresse haben (telefonisch-only-Kunden
        # kriegen keine Mail).
        if (
            isinstance(result, dict)
            and result.get("erfolg")
            and kunde_email
        ):
            await self._send_buche_confirmation_mail(
                tenant=tenant,
                kunde_email=kunde_email,
                kunde_name=name,
                datum=datum,
                uhrzeit=uhrzeit,
                anliegen=anliegen,
                employee_id=employee_id,
                event_id=result.get("event_id"),
                drive_folder_url=booked_drive_url,
            )

        return result

    async def _send_buche_confirmation_mail(
        self,
        *,
        tenant: Tenant,
        kunde_email: str,
        kunde_name: str,
        datum: str,
        uhrzeit: str,
        anliegen: str,
        employee_id: UUID | None,
        event_id: str | None,
        drive_folder_url: str | None = None,
    ) -> None:
        """Versendet Voice-Buchungs-Bestaetigung + persistiert
        EmailConversation fuer Reply-Threading (E.3).

        Best-effort: fanged alle Fehler ab, weil der Termin schon
        gebucht ist und der Voice-Caller das Erfolgs-Result kriegen
        soll, auch wenn die Mail mal nicht raus geht.
        """
        try:
            from core.integrations.mail_pipeline import (
                send_buche_confirmation, create_conversation,
                record_outbound_q_reply,
            )
            from core.integrations.mail_template import extract_first_name
            from core.models import STATE_BOOKED
            from core.models.employee import Employee
            import datetime as _dt

            employee_name: str | None = None
            if employee_id is not None:
                async with AsyncSessionLocal() as s:
                    r = await s.execute(
                        select(Employee).where(Employee.id == employee_id)
                    )
                    emp = r.scalar_one_or_none()
                    if emp:
                        employee_name = emp.name

            termin_datum = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    termin_datum = _dt.datetime.strptime(datum, fmt).date()
                    break
                except Exception:
                    pass

            kunde_anrede = extract_first_name(kunde_name or "") or ""
            company_name = tenant.company_name or "Handwerksbetrieb"
            contact_phone = getattr(tenant, "contact_phone", "") or ""

            sent_meta = await send_buche_confirmation(
                tenant_id=tenant.id, to_email=kunde_email,
                kunde_anrede=kunde_anrede, company_name=company_name,
                datum_label=datum, uhrzeit=uhrzeit,
                employee_name=employee_name, anliegen=anliegen,
                contact_phone=contact_phone, employee_id=employee_id,
            )
            if not sent_meta.get("success"):
                logger.warning(
                    f"buche-confirmation-mail: send failed "
                    f"tenant={tenant.slug} kunde={kunde_email}: "
                    f"{sent_meta.get('error')}"
                )
                return

            conv = await create_conversation(
                tenant_id=tenant.id, sender_email=kunde_email,
                sender_name=kunde_name, subject=None,
                assigned_employee_id=employee_id,
                gcal_event_id=event_id, termin_datum=termin_datum,
                state=STATE_BOOKED,
            )
            # Kundenordner-URL an der Konv vermerken (Formular-Eingang
            # findet den Ordner darueber wieder).
            if drive_folder_url:
                from core.integrations.mail_pipeline import (
                    set_conversation_drive_url,
                )
                try:
                    await set_conversation_drive_url(conv.id, drive_folder_url)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"buche-confirmation: set_conversation_drive_url "
                        f"fehler (non-fatal): {e}"
                    )
            reply_subject = (
                f"Ihre Terminbestaetigung — {datum} um {uhrzeit} Uhr"
            )
            await record_outbound_q_reply(
                conv.id,
                internet_message_id=sent_meta.get("internet_message_id"),
                microsoft_conversation_id=sent_meta.get("conversation_id"),
                q_reply_text=(
                    f"[Voice-Buchungs-Bestaetigung: {anliegen}]"
                ),
                subject=reply_subject,
            )
            logger.info(
                f"buche-confirmation-mail: sent tenant={tenant.slug} "
                f"kunde={kunde_email} conv_id={conv.id} "
                f"event={(event_id or '')[:20]}"
            )
        except Exception as e:
            logger.exception(
                f"buche-confirmation-mail: crashed (Buchung steht trotzdem): "
                f"{e}"
            )

    async def _handle_finde_termine(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'finde_termine' aufruft.

        Sucht Termine des Anrufers anhand seiner Telefonnummer oder
        Email-Adresse, damit Q sie ihm zur Auswahl vorlesen kann
        (Storno-Phase 1). Pro Treffer erzeugen wir einen kurzlebigen
        Stornier-Token — Q reicht den spaeter an /storniere_termin,
        damit der Agent nie eine event_id direkt sieht (Security:
        keine Replay-/Cross-Tenant-Loeschungen moeglich).

        Erwartet payload:
          {
            "tenant_slug": "demo",
            "kunde_telefon": "+49 ...",   # optional
            "kunde_email": "...",         # optional
            "time_min": "ISO" | null,     # optional, default: heute
            "time_max": "ISO" | null      # optional, default: +30d
          }
        Mindestens eines von kunde_telefon/kunde_email muss gesetzt sein.

        Response (fuer ElevenLabs-Tool):
          {
            "erfolg": True, "anzahl": 2,
            "termine": [
              {
                "stornier_token": "<random>",
                "datum": "22.05.2026", "wochentag": "Do",
                "uhrzeit": "14:00", "anliegen": "...", "ort": "..."
              }, ...
            ]
          }
        """
        from core.plugin_system import get_plugin_for_tenant

        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "nachricht": "tenant_slug fehlt"}

        telefon = (payload.get("kunde_telefon") or "").strip()
        email = (payload.get("kunde_email") or "").strip()
        if not telefon and not email:
            return {
                "erfolg": False,
                "nachricht": "kunde_telefon oder kunde_email erforderlich",
            }

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "nachricht": f"Tenant '{tenant_slug}' nicht gefunden",
            }

        kalender = await get_plugin_for_tenant(tenant_slug, "kalender")
        if kalender is None:
            return {
                "erfolg": False,
                "nachricht": "Kalender ist fuer diesen Betrieb nicht eingerichtet.",
            }

        find_payload: dict[str, Any] = {}
        if telefon:
            find_payload["kunde_telefon"] = telefon
        if email:
            find_payload["kunde_email"] = email
        if payload.get("time_min"):
            find_payload["time_min"] = payload["time_min"]
        if payload.get("time_max"):
            find_payload["time_max"] = payload["time_max"]

        result = await kalender.on_webhook("find_events", find_payload)
        if not result.get("erfolg"):
            return {
                "erfolg": False,
                "nachricht": result.get("nachricht") or "Suche fehlgeschlagen",
            }

        # Pro Treffer Stornier-Token + voice-freundliche Felder bauen
        wochentage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        from dateutil import parser as _p  # type: ignore
        out = []
        for ev in result.get("termine", []):
            try:
                start_dt = _p.isoparse(ev["start_dt"])
            except Exception:  # noqa: BLE001
                continue
            token = _create_stornier_token(
                tenant.id, ev["event_id"], ev.get("employee_id"),
            )
            out.append({
                "stornier_token": token,
                "datum": start_dt.strftime("%d.%m.%Y"),
                "wochentag": wochentage[start_dt.weekday()],
                "uhrzeit": start_dt.strftime("%H:%M"),
                "anliegen": ev.get("summary", ""),
                "ort": ev.get("location", ""),
            })

        logger.info(
            f"finde_termine: tenant={tenant_slug} "
            f"telefon={'set' if telefon else 'none'} "
            f"email={'set' if email else 'none'} "
            f"treffer={len(out)}"
        )
        return {"erfolg": True, "anzahl": len(out), "termine": out}

    async def _handle_storniere_termin(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'storniere_termin' aufruft.

        Loest einen vorher per /finde_termine erzeugten Stornier-Token
        ein und loescht den zugeordneten Termin. Token ist einmalig +
        30 min gueltig + Tenant-gebunden — siehe _consume_stornier_token.

        Erwartet payload:
          {
            "tenant_slug": "demo",
            "stornier_token": "<token aus finde_termine>",
            "kunde_bestaetigung_text": "..."  # optional, fuer Audit-Log
          }
        """
        from core.plugin_system import get_plugin_for_tenant

        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "nachricht": "tenant_slug fehlt"}

        token = (payload.get("stornier_token") or "").strip()
        if not token:
            return {"erfolg": False, "nachricht": "stornier_token fehlt"}

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "nachricht": f"Tenant '{tenant_slug}' nicht gefunden",
            }

        entry = _consume_stornier_token(token, tenant.id)
        if entry is None:
            # Bewusst generische Meldung — kein Hint ob "expired" /
            # "unknown" / "tenant-mismatch", damit Token-Probing nichts
            # ueber das System verraet.
            logger.info(f"storniere_termin: tenant={tenant_slug} ungueltiger Token")
            return {
                "erfolg": False,
                "nachricht": (
                    "Der Stornier-Vorgang ist abgelaufen oder bereits "
                    "erledigt. Bitte den Termin erneut suchen."
                ),
            }

        event_id = entry["event_id"]
        employee_id_str = entry.get("employee_id")

        kalender = await get_plugin_for_tenant(tenant_slug, "kalender")
        if kalender is None:
            return {
                "erfolg": False,
                "nachricht": "Kalender ist fuer diesen Betrieb nicht eingerichtet.",
            }

        cancel_payload: dict[str, Any] = {"event_id": event_id}
        if employee_id_str:
            try:
                cancel_payload["employee_id"] = UUID(employee_id_str)
            except (ValueError, TypeError):
                pass
        result = await kalender.on_webhook("cancel_appointment", cancel_payload)

        # Telegram-Push an den zustaendigen Mitarbeiter
        # (silent fail; loggen aber blockieren nie das Storno-Response).
        try:
            from plugins.telegram_notify.handler import TelegramNotifier
            emp_uuid = None
            if employee_id_str:
                try:
                    emp_uuid = UUID(employee_id_str)
                except (ValueError, TypeError):
                    emp_uuid = None
            bestaetigung = (payload.get("kunde_bestaetigung_text") or "").strip()
            push = (
                "🚫 <b>Termin storniert (telefonisch)</b>\n"
                f"<b>Event:</b> <code>{event_id[:12]}…</code>"
            )
            if bestaetigung:
                push += f"\n<b>Aussage Kunde:</b> {bestaetigung}"
            await TelegramNotifier.send_for_employee(
                tenant.id, push, employee_id=emp_uuid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"storniere_termin telegram-push failed: {exc}")

        logger.info(
            f"storniere_termin: tenant={tenant_slug} event={event_id[:12]}… "
            f"emp={employee_id_str} erfolg={result.get('erfolg')}"
        )

        # Teil E.2: Storno-Bestaetigungs-Mail an Kunden (best-effort).
        # Nur wenn Cancel erfolgreich UND wir die Mail-Adresse aus einer
        # frueheren EmailConversation auflosen koennen (via gcal_event_id).
        # Voice-only Kunden ohne Mail-Adresse zur Buchzeit kriegen keine
        # Mail — Telegram-Push an MA hat aber schon stattgefunden.
        if isinstance(result, dict) and result.get("erfolg"):
            await self._send_voice_storno_confirmation_mail(
                tenant=tenant, event_id=event_id, employee_id=emp_uuid,
            )

        return result

    async def _send_voice_storno_confirmation_mail(
        self,
        *,
        tenant: Tenant,
        event_id: str,
        employee_id: UUID | None,
    ) -> None:
        """Findet die zur event_id zugehoerige EmailConversation, schickt
        die Storno-Bestaetigung und setzt State auf STORNIERT.

        Wenn keine Konversation existiert (voice-only Kunde): no-op.
        Best-effort: fanged alle Fehler ab — Cancel ist schon durch,
        Voice-Caller bekommt sein Erfolgs-Result.
        """
        try:
            from core.integrations.mail_pipeline import (
                find_conversation_by_event_id, send_storno_confirmation,
                record_outbound_q_reply, set_conversation_state,
            )
            from core.integrations.mail_template import extract_first_name
            from core.models import STATE_STORNIERT

            conv = await find_conversation_by_event_id(tenant.id, event_id)
            if conv is None or not conv.kunde_email:
                logger.info(
                    f"voice-storno-mail: keine EmailConversation fuer "
                    f"event={event_id[:20]} — kein Mail-Versand "
                    f"(voice-only Kunde ohne Mail-Adresse)"
                )
                return

            company_name = tenant.company_name or "Handwerksbetrieb"
            kunde_anrede = extract_first_name(conv.kunde_name or "") or ""
            original_subject = conv.last_subject or "Ihre Terminbuchung"

            sent_meta = await send_storno_confirmation(
                tenant_id=tenant.id, to_email=conv.kunde_email,
                kunde_anrede=kunde_anrede, company_name=company_name,
                original_subject=original_subject, cancelled_count=1,
                employee_id=employee_id,
            )
            if not sent_meta.get("success"):
                logger.warning(
                    f"voice-storno-mail: send failed tenant={tenant.slug} "
                    f"kunde={conv.kunde_email}: {sent_meta.get('error')}"
                )
                return

            reply_subject = (
                f"Re: {original_subject}"
                if not original_subject.lower().startswith("re:")
                else original_subject
            )
            await record_outbound_q_reply(
                conv.id,
                internet_message_id=sent_meta.get("internet_message_id"),
                microsoft_conversation_id=sent_meta.get("conversation_id"),
                q_reply_text="[Voice-Storno-Bestaetigung: 1 Termin storniert]",
                subject=reply_subject,
            )
            await set_conversation_state(conv.id, STATE_STORNIERT)
            logger.info(
                f"voice-storno-mail: sent tenant={tenant.slug} "
                f"kunde={conv.kunde_email} conv_id={conv.id}"
            )
        except Exception as e:
            logger.exception(
                f"voice-storno-mail: crashed (Cancel steht trotzdem): {e}"
            )


    async def _handle_save_contact(self, payload):
        """
        Webhook von ElevenLabs wenn Q im Anruf das Tool 'speichere_kontakt' aufruft.

        Erwartet payload:
          {
            "name": "Frau Mueller",
            "phone": "+49 651 1234",
            "email": "..." | null,
            "anliegen": "Moebelmontage" | null,
            "tenant_slug": "demo"
          }

        Sucht/legt Kontakt in Lexware an + pingt Tenant via Telegram.
        """
        name = (payload.get("name") or "").strip()
        phone = (payload.get("phone") or "").strip() or None
        email = (payload.get("email") or "").strip() or None
        anliegen = (payload.get("anliegen") or "").strip() or None
        tenant_slug = (payload.get("tenant_slug") or "").strip()

        if not name or not tenant_slug:
            logger.warning(
                f"save_contact: name oder tenant_slug fehlt: name={name!r} slug={tenant_slug!r}"
            )
            return {"success": False, "error": "name und tenant_slug sind Pflicht"}

        # Schutz gegen Leere-Anrufe: Anrufer hat nichts gesagt, Q hat
        # 'unbekannt'/'(silent)'/'.' als Namen. Kein Lexware-Eintrag,
        # nur leiser Push an Inhaber damit er weiss dass jemand kurz
        # angerufen hat.
        name_clean = name.lower().strip(" .,-_")
        suspicious = (
            len(name) < 2
            or name_clean in {
                "unbekannt", "silent", "no name", "noname",
                "test", "anonym", "anonymous", "(silent)", "n/a",
            }
        )
        if suspicious:
            logger.info(
                f"save_contact: leerer Anruf erkannt name={name!r} - "
                f"kein Lexware-Eintrag, nur Hinweis-Push"
            )
            # Tenant-Telegram nachschlagen
            async with AsyncSessionLocal() as s:
                t = (await s.execute(
                    select(Tenant).where(Tenant.slug == tenant_slug)
                )).scalar_one_or_none()
                tg_chat = t.telegram_chat_id if t else None
            if tg_chat:
                await self._push_to_tenant(
                    tg_chat,
                    f"📞 <b>Kurzer Anruf</b> — Anrufer ohne Anliegen "
                    f"(Name: {name!r}). Kein Lexware-Eintrag angelegt.",
                )
            return {"success": True, "skipped": True, "reason": "suspicious-name"}

        # Tenant laden
        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
            if not tenant:
                logger.warning(f"save_contact: Tenant {tenant_slug!r} nicht gefunden")
                return {"success": False, "error": f"Tenant {tenant_slug} unbekannt"}
            tenant_id = tenant.id
            tenant_telegram = tenant.telegram_chat_id
            tenant_company_name = tenant.company_name
            tenant_contact_name = tenant.contact_name
            tenant_contact_email = tenant.contact_email
            tenant_contact_phone = tenant.contact_phone
            tenant_branche = tenant.branche

        # Lexware-Provider holen
        provider = await self._get_lexware_provider(tenant_id)
        if provider is None:
            logger.warning(f"save_contact: Lexware nicht verbunden fuer Tenant {tenant_slug}")
            await self._push_to_tenant(
                tenant_telegram,
                f"⚠️ <b>Voice-Anruf:</b> Kontakt erfasst, aber Lexware nicht "
                f"verbunden. Bitte /lexware_setup ausfuehren.\n\n"
                f"Daten: {name}, {phone or 'kein Tel.'}, {email or 'keine Mail'}",
            )
            return {"success": True, "message": "Kontakt vorgemerkt, Lexware fehlt"}

        # Smart-Detect Firma vs. Person
        is_company = bool(any(
            kw in name.lower()
            for kw in ("gmbh", "ag", "kg", "ohg", "ug", "gbr", "e.k.", "ev", "verein", "bauunternehmen", "firma")
        ))

        # Upsert in Lexware
        try:
            contact, created = await provider.upsert_customer_contact(
                name=name,
                phone=phone,
                email=email,
                anliegen=anliegen,
                is_company=is_company,
            )
        except AccountingError as e:
            logger.exception(f"save_contact Lexware-Fehler: {e}")
            return {"success": False, "error": f"Lexware-Fehler (HTTP {e.status_code})"}
        except Exception as e:
            logger.exception(f"save_contact unerwartet: {e}")
            return {"success": False, "error": "Interner Fehler"}

        action = "neu angelegt" if created else "aktualisiert"
        logger.info(
            f"save_contact OK: tenant={tenant_slug} contact_id={contact.contact_id} "
            f"name={name!r} action={action}"
        )

        # Anfrage-Formular-Mail an den Kunden (nur wenn eine E-Mail vorliegt).
        # Der Kontakt ist an dieser Stelle bereits in Lexware gespeichert —
        # ein Mail-Fehler darf das nicht ruecksetzen, er loest nur eine
        # Telegram-Warnung an den Inhaber aus.
        mail_status = "skipped-no-email"
        if email:
            mail_status = await self._send_anfrage_mail(
                tenant_id=tenant_id,
                tenant_telegram=tenant_telegram,
                company_name=tenant_company_name,
                contact_name=tenant_contact_name,
                contact_email=tenant_contact_email,
                contact_phone=tenant_contact_phone,
                branche=tenant_branche,
                kunde_name=name,
                kunde_email=email,
                kunde_telefon=phone,
                anliegen=anliegen,
            )

        # Push an den fuer dieses Anliegen passenden Mitarbeiter routen.
        # Hier kein target_datetime — der Anruf ist gerade jetzt, und der
        # Kontakt-Speichern-Pfad bucht noch keinen Termin. choose_employee
        # nutzt Skill-Match + optional Distanz (kunde_adresse ist hier
        # nicht erfasst, save_contact bekommt sie nicht vom Voice-Agent).
        routing = None
        try:
            routing = await choose_employee(
                tenant_id=tenant_id,
                anliegen_text=anliegen or "",
            )
        except Exception as e:
            logger.warning(f"save_contact: choose_employee crashed: {e}")

        # Tenant/Mitarbeiter per Telegram informieren — alle User-Inputs
        # HTML-escapen, weil parse_mode=HTML in Telegram. Sonst koennte ein
        # Anrufer mit praepariertem Namen/Anliegen in fremde Bot-Antworten
        # injizieren.
        from html import escape as _h
        anliegen_str = f"\n<b>Anliegen:</b> {_h(anliegen)}" if anliegen else ""
        phone_str = f"\n<b>Telefon:</b> <code>{_h(phone)}</code>" if phone else ""
        email_str = f"\n<b>Mail:</b> <code>{_h(email)}</code>" if email else ""
        deeplink = f"https://app.lexware.de/permalink/contacts/edit/{contact.contact_id}"
        msg = (
            f"☎️ <b>Neuer Anruf - Kontakt {action}</b>\n\n"
            f"<b>Name:</b> {_h(name)}"
            f"{phone_str}"
            f"{email_str}"
            f"{anliegen_str}\n\n"
            f'<a href="{deeplink}">In Lexware oeffnen</a>'
        )
        from plugins.telegram_notify.handler import TelegramNotifier
        await TelegramNotifier.send_for_employee(
            tenant_id, msg,
            employee_id=routing.employee_id if routing else None,
        )

        return {
            "success": True,
            "contact_id": str(contact.contact_id),
            "action": action,
            "message": f"Kontakt {action}",
            "mail": mail_status,
            "routing": _routing_to_response(routing),
        }

    async def _send_anfrage_mail(
        self, *, tenant_id, tenant_telegram, company_name, contact_name,
        contact_email, contact_phone, branche, kunde_name, kunde_email,
        anliegen, kunde_telefon=None,
    ):
        """Erzeugt einen Anfrage-Token und mailt dem Kunden den Formular-Link.

        Laeuft erst nach erfolgreichem Lexware-Upsert. Bei jedem Fehler
        (Token, Rendering, Versand) bleibt der Kontakt gespeichert; der
        Inhaber bekommt eine Telegram-Warnung zum manuellen Nachfassen.

        Returns einen Status-String fuers Logging/Response:
        'sent' | 'send-failed' | 'error'.
        """
        from html import escape as _h
        try:
            from core.integrations.anfrage_forms import (
                build_anfrage_url, create_anfrage_token,
            )
            from core.integrations.mail_template import (
                build_kunde_reply_html, extract_first_name,
            )
            from core.integrations.microsoft import send_mail_as_user
            from core.models.anfrage import (
                ANFRAGE_TYP_ALLGEMEIN, ANFRAGE_TYP_TISCHLER,
            )

            anfrage_typ = (
                ANFRAGE_TYP_TISCHLER
                if (branche or "").lower() == "tischler"
                else ANFRAGE_TYP_ALLGEMEIN
            )
            token_obj = await create_anfrage_token(
                tenant_id=tenant_id,
                kunde_email=kunde_email,
                kunde_name=kunde_name,
                kunde_telefon=kunde_telefon,
                anfrage_typ=anfrage_typ,
            )
            form_url = build_anfrage_url(token_obj.token)

            reply_text = (
                f"vielen Dank fuer deinen Anruf bei {company_name}. "
                + (
                    f"Du hast dich nach folgendem Anliegen erkundigt: "
                    f"{anliegen}. " if anliegen else ""
                )
                + "Damit wir dir ein passendes Angebot machen koennen, "
                "fuell bitte kurz unser Anfrage-Formular aus."
            )
            body_html = build_kunde_reply_html(
                kunde_anrede_name=extract_first_name(kunde_name),
                kunde_email=kunde_email,
                reply_text=reply_text,
                form_url=form_url,
                company_name=company_name or "",
                contact_name=contact_name or "",
                contact_email=contact_email or "",
                contact_phone=contact_phone or "",
            )
            sent = await send_mail_as_user(
                tenant_id=tenant_id,
                to_email=kunde_email,
                subject=f"Dein Anfrage-Formular fuer {company_name}",
                body_html=body_html,
            )
            if sent:
                logger.info(
                    f"save_contact: Anfrage-Mail gesendet an {kunde_email}"
                )
                return "sent"

            logger.warning(
                f"save_contact: Anfrage-Mail an {kunde_email} fehlgeschlagen"
            )
            await self._push_to_tenant(
                tenant_telegram,
                f"⚠️ <b>Voice-Anruf:</b> Kontakt gespeichert, aber die "
                f"Anfrage-Mail an <code>{_h(kunde_email)}</code> konnte "
                f"nicht gesendet werden. Bitte manuell nachfassen.",
            )
            return "send-failed"
        except Exception as e:
            logger.exception(f"save_contact: Anfrage-Mail-Fehler: {e}")
            try:
                await self._push_to_tenant(
                    tenant_telegram,
                    "⚠️ <b>Voice-Anruf:</b> Kontakt gespeichert, aber beim "
                    "Versand der Anfrage-Mail gab es einen Fehler. Bitte "
                    "manuell nachfassen.",
                )
            except Exception:
                pass
            return "error"


    async def _get_lexware_provider(self, tenant_id):
        """Lexware-Provider fuer Tenant aus tool_configs holen."""
        async with AsyncSessionLocal() as s:
            tc = (await s.execute(
                select(ToolConfig).where(
                    ToolConfig.tenant_id == tenant_id,
                    ToolConfig.tool_name == "lexware",
                )
            )).scalar_one_or_none()
            if not tc:
                tc_global = (await s.execute(
                    select(ToolConfig)
                    .join(Tenant, ToolConfig.tenant_id == Tenant.id)
                    .where(Tenant.slug == "_global", ToolConfig.tool_name == "lexware")
                )).scalar_one_or_none()
                if tc_global:
                    tc = tc_global
            if not tc:
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
            return LexwareProvider(api_key=api_key)


    async def _handle_wissensbasis(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'wissensbasis' aufruft.

        Erlaubt dem Voice-Agent gezielt in der tenant-spezifischen Wissens-
        basis nachzuschlagen — Alternative zum kompletten Knowledge-Block
        im System-Prompt (siehe _handle_initiation). Sinnvoll wenn die
        Wissensbasis waechst und der Prompt zu lang wird, oder wenn der
        Agent explizit signalisieren soll dass er weiss was er nicht weiss.

        Erwartet payload:
          {
            "tenant_slug": "demo",
            "frage": "Was kostet eine Beratung?",  # optional
            "kategorie": "preise"                   # optional, eine der
                                                    # ALLE_KATEGORIEN
          }
        Mindestens eines von frage/kategorie muss gesetzt sein.

        Matching-Logik:
        1. kategorie gesetzt → alle Snippets dieser Kategorie zurueck
        2. nur frage gesetzt → simple Keyword-Suche (lowercase substring
           auf Tokens >=4 Zeichen, deutsche Stopwoerter raus). Bei 0
           Treffern fallback auf Liste verfuegbarer Kategorien — damit
           der Agent dem Anrufer sagen kann was er sonst weiss.

        Response (fuer ElevenLabs-Tool):
          {
            "erfolg": True,
            "antwort": "<voice-freundlicher Text, ~max 800 Zeichen>",
            "anzahl_treffer": 3,
            "kategorien_genutzt": ["preise", "leistungen"]
          }

        Keine Vektor-Suche — laut Model-Doku nur 5-30 Snippets pro Tenant.
        Substring-Match auf Tokens reicht in der Praxis und ist deterministisch
        (wichtig fuer Voice: keine Halluzinationen ueber nicht-existente
        Snippets, weil wir nur exakte Snippet-Texte zurueckgeben).
        """
        tenant_slug = (payload.get("tenant_slug") or "").strip()
        if not tenant_slug:
            return {"erfolg": False, "antwort": "tenant_slug fehlt", "anzahl_treffer": 0}

        frage = (payload.get("frage") or "").strip()
        kategorie = (payload.get("kategorie") or "").strip().lower()
        if not frage and not kategorie:
            return {
                "erfolg": False,
                "antwort": "frage oder kategorie erforderlich",
                "anzahl_treffer": 0,
            }

        async with AsyncSessionLocal() as s:
            tenant = (await s.execute(
                select(Tenant).where(Tenant.slug == tenant_slug)
            )).scalar_one_or_none()
        if tenant is None:
            return {
                "erfolg": False,
                "antwort": f"Betrieb '{tenant_slug}' nicht gefunden",
                "anzahl_treffer": 0,
            }

        by_kat = await _load_knowledge(tenant.id)
        if not by_kat:
            return {
                "erfolg": True,
                "antwort": (
                    "Zu diesem Betrieb sind noch keine Informationen "
                    "hinterlegt."
                ),
                "anzahl_treffer": 0,
                "kategorien_genutzt": [],
            }

        # Strategie 1: explizite Kategorie
        treffer: list[tuple[str, str]] = []  # (kategorie, text)
        if kategorie:
            if kategorie not in ALLE_KATEGORIEN:
                erlaubt = ", ".join(ALLE_KATEGORIEN)
                return {
                    "erfolg": False,
                    "antwort": (
                        f"Kategorie '{kategorie}' unbekannt. Erlaubt: "
                        f"{erlaubt}"
                    ),
                    "anzahl_treffer": 0,
                }
            for text in by_kat.get(kategorie, []):
                treffer.append((kategorie, text))

        # Strategie 2: Keyword-Match wenn frage da ist (und Kategorie
        # entweder leer oder keine Treffer brachte)
        if frage and not treffer:
            stopwords = {
                "der", "die", "das", "den", "dem", "des", "ein", "eine",
                "einen", "einem", "eines", "und", "oder", "aber", "doch",
                "wie", "was", "wer", "wann", "wo", "warum", "welche",
                "welcher", "welches", "ist", "sind", "war", "waren",
                "kann", "koennen", "sollte", "muesste", "ich", "du", "er",
                "sie", "wir", "ihr", "mich", "dich", "uns", "euch",
                "habt", "habe", "haben", "hat", "fuer", "bei", "mit",
                "ohne", "auf", "aus", "von", "zum", "zur", "im", "am",
            }
            tokens = [
                t for t in frage.lower().replace("?", " ").replace(".", " ").split()
                if len(t) >= 4 and t not in stopwords
            ]
            scored: list[tuple[int, str, str]] = []
            for kat, texts in by_kat.items():
                for text in texts:
                    tl = text.lower()
                    score = sum(1 for t in tokens if t in tl)
                    if score > 0:
                        scored.append((score, kat, text))
            scored.sort(key=lambda x: -x[0])
            treffer = [(kat, text) for _, kat, text in scored]

        # Strategie 3: kein Treffer → Kategorien-Uebersicht zurueck
        if not treffer:
            verfuegbar = [
                KATEGORIE_LABELS.get(k, k) for k in ALLE_KATEGORIEN
                if k in by_kat
            ]
            if not verfuegbar:
                antwort = "Dazu liegen keine Informationen vor."
            else:
                antwort = (
                    "Dazu habe ich keinen direkten Eintrag. Ich habe aber "
                    "Informationen zu: " + ", ".join(verfuegbar) + "."
                )
            logger.info(
                f"wissensbasis: tenant={tenant_slug} frage={frage!r} "
                f"kategorie={kategorie!r} treffer=0"
            )
            return {
                "erfolg": True,
                "antwort": antwort,
                "anzahl_treffer": 0,
                "kategorien_genutzt": [],
            }

        # Voice-freundlicher Output: Snippets ohne Markdown,
        # max ~800 Zeichen damit der TTS nicht 30s redet
        MAX_CHARS = 800
        parts: list[str] = []
        kategorien_used: list[str] = []
        total_len = 0
        for kat, text in treffer:
            if kat not in kategorien_used:
                kategorien_used.append(kat)
            chunk = text.strip()
            if total_len + len(chunk) > MAX_CHARS:
                if not parts:
                    parts.append(chunk[: MAX_CHARS - 3] + "...")
                break
            parts.append(chunk)
            total_len += len(chunk) + 2

        antwort = " ".join(parts)
        logger.info(
            f"wissensbasis: tenant={tenant_slug} frage={frage!r} "
            f"kategorie={kategorie!r} treffer={len(treffer)} "
            f"kategorien={kategorien_used} antwort_len={len(antwort)}"
        )
        return {
            "erfolg": True,
            "antwort": antwort,
            "anzahl_treffer": len(treffer),
            "kategorien_genutzt": kategorien_used,
        }


    async def _handle_call_ended(self, payload):
        """Webhook von ElevenLabs nach Anrufende.

        Erwartet:
          {
            "tenant_slug": "demo",
            "called_number": "+49 211 87...",
            "caller_id": "+49 ...",
            "duration_seconds": 142,
            "char_count": 1230,           # falls TTS-Zeichen-Count separat
            "conversation_id": "...",
            "call_outcome": "completed" | "incomplete" | "no_audio",
          }

        Trackt:
          - ElevenLabs TTS chars (falls geliefert)
          - Deepgram seconds (Anruf-Dauer fuer Transcription)
          - Sipgate inbound seconds (kostenfrei in DE)
        """
        tenant_slug = (payload.get("tenant_slug") or "").strip()
        called_number = payload.get("called_number") or payload.get("to_number")
        duration_s = float(payload.get("duration_seconds") or 0)
        char_count = int(payload.get("char_count") or 0)
        outcome = (payload.get("call_outcome") or "completed").lower()

        # Tenant-Lookup: erst via slug, sonst via called_number
        tenant_id = None
        async with AsyncSessionLocal() as s:
            if tenant_slug:
                t = (await s.execute(
                    select(Tenant).where(Tenant.slug == tenant_slug)
                )).scalar_one_or_none()
                if t:
                    tenant_id = t.id
        if tenant_id is None and called_number:
            t = await _find_tenant_by_phone(called_number)
            if t:
                tenant_id = t.id

        if duration_s <= 0:
            logger.info(
                f"call_ended ohne duration_seconds — skip tracking "
                f"(tenant={tenant_slug}, outcome={outcome})"
            )
            return {"success": True, "tracked": False}

        # Failsafe Usage-Tracking
        try:
            from core.billing import (
                track_deepgram_seconds, track_elevenlabs_chars,
                track_api_usage,
            )
            # Deepgram: jede Sekunde wird transcribiert
            await track_deepgram_seconds(
                duration_s, tenant_id=tenant_id,
            )
            # ElevenLabs: Zeichen-Count wenn vorhanden
            if char_count > 0:
                await track_elevenlabs_chars(
                    char_count, tenant_id=tenant_id,
                )
            # Sipgate: inbound, kostenfrei aber wir tracken Volume
            await track_api_usage(
                tenant_id=tenant_id,
                provider="sipgate",
                operation="inbound-de",
                units=duration_s,
                unit="second",
                metadata={
                    "called_number": called_number,
                    "outcome": outcome,
                    "conversation_id": payload.get("conversation_id"),
                },
            )
        except Exception as e:
            logger.warning(f"voice call_ended tracking failed: {e}")

        logger.info(
            f"call_ended tracked: tenant={tenant_slug} "
            f"duration={duration_s}s chars={char_count} outcome={outcome}"
        )
        return {"success": True, "tracked": True}


    async def _push_to_tenant(self, telegram_chat_id, html_message):
        """Schickt Telegram-Nachricht an Tenant. Silent fail bei Fehler."""
        if not telegram_chat_id:
            return False
        async with AsyncSessionLocal() as s:
            tc = (await s.execute(
                select(ToolConfig)
                .join(Tenant, ToolConfig.tenant_id == Tenant.id)
                .where(Tenant.slug == "_global", ToolConfig.tool_name == "telegram_notify")
            )).scalar_one_or_none()
            if not tc:
                return False
            from core.security.encryption import try_decrypt
            bot_token = try_decrypt((tc.config or {}).get("bot_token"))
            if not bot_token:
                return False

        import httpx
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": telegram_chat_id,
            "text": html_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code != 200:
                    logger.warning(
                        f"_push_to_tenant fehlgeschlagen: HTTP {r.status_code}"
                    )
                    return False
        except Exception as e:
            logger.warning(f"_push_to_tenant Exception: {e}")
            return False
        return True

