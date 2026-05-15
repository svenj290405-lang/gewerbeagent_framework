"""
voice_init Plugin: Conversation-Initiation-Webhook fuer ElevenLabs.
"""
from __future__ import annotations

import logging
from typing import Any

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
        if expected:
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
        if endpoint == "buche_termin":
            return await self._handle_buche_termin(payload)
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
            f"checke_kalender: tenant={tenant_slug} wunsch={datum} {uhrzeit} "
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


    async def _handle_buche_termin(self, payload):
        """Webhook von ElevenLabs wenn Q das Tool 'buche_termin' aufruft.

        Erwartet payload:
          {
            "slot_id": "20.05.2026|14:00|90",   # aus checke_kalender
            "employee_id": "...",                # aus checke_kalender.routing
            "anliegen": "Kuechenmontage",
            "kunde_name": "Frau Mueller",
            "kunde_telefon": "+49 ...",          # optional
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
        logger.info(
            f"buche_termin: tenant={tenant_slug} slot={slot_id} "
            f"emp={employee_id} name={name!r} erfolg={result.get('erfolg')}"
        )
        return result


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
                anliegen=anliegen,
            )

        # Tenant per Telegram informieren — alle User-Inputs HTML-escapen,
        # weil parse_mode=HTML in Telegram. Sonst koennte ein Anrufer mit
        # praepariertem Namen/Anliegen in fremde Bot-Antworten injizieren.
        if tenant_telegram:
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
            await self._push_to_tenant(tenant_telegram, msg)

        return {
            "success": True,
            "contact_id": str(contact.contact_id),
            "action": action,
            "message": f"Kontakt {action}",
            "mail": mail_status,
        }

    async def _send_anfrage_mail(
        self, *, tenant_id, tenant_telegram, company_name, contact_name,
        contact_email, contact_phone, branche, kunde_name, kunde_email,
        anliegen,
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
            bot_token = (tc.config or {}).get("bot_token")
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

