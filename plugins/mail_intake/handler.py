"""
Mail-Intake-Handler.

Flow:
1. Brevo POSTet JSON an /webhook/_global/mail_intake/incoming
2. Tenant aus to-Adresse extrahieren
3. Spam-Check
4. Gemini-Extraction: name, anliegen, datum, uhrzeit, klar?
5. Wenn klar -> kalender.book_appointment
6. Auto-Reply an Kunden mit DSGVO-Hinweis
7. Telegram-Push an Tenant
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.plugin_system import BasePlugin
from plugins.mail_intake.manifest import MANIFEST
from plugins.telegram_notify.handler import TelegramNotifier

logger = logging.getLogger(__name__)

BREVO_API_BASE = "https://api.brevo.com/v3"
HTTP_TIMEOUT = 15.0
SPAM_SCORE_THRESHOLD = 5.0


# ---------- Tenant-Routing ----------

async def resolve_tenant_from_recipient(recipient: str) -> Tenant | None:
    """
    'dietz@reply.gewerbeagent.de' -> Tenant mit slug='dietz'

    Fallback: bei 'test@...' oder unbekanntem Tenant -> None.
    """
    match = re.match(r"^([a-z0-9_-]+)@", recipient.lower().strip())
    if not match:
        return None
    slug = match.group(1)

    # Spezialfall: 'test'-Adressen werden auf _global geroutet (fuers Debug)
    if slug == "test":
        slug = "_global"

    async with AsyncSessionLocal() as s:
        result = await s.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()


# ---------- Global Mail-Intake-Config laden ----------

async def load_global_config() -> dict | None:
    """Holt mail_intake-Config aus Tenant _global."""
    async with AsyncSessionLocal() as s:
        result = await s.execute(select(Tenant).where(Tenant.slug == "_global"))
        global_tenant = result.scalar_one_or_none()
        if not global_tenant:
            return None
        result = await s.execute(
            select(ToolConfig).where(
                ToolConfig.tenant_id == global_tenant.id,
                ToolConfig.tool_name == "mail_intake",
            )
        )
        tc = result.scalar_one_or_none()
        if not tc or not tc.enabled:
            return None
        return {**MANIFEST.default_config, **(tc.config or {})}


# ---------- Konversations-Memory ----------

async def find_conversation(
    tenant_id: uuid.UUID,
    kunde_email: str,
    in_reply_to: str | None = None,
):
    """
    Findet eine bestehende Konversation:
    - Bevorzugt: per In-Reply-To Header (last_message_id match)
    - Fallback: per (tenant_id, kunde_email) — nimmt die aktuellste offene
    Returns EmailConversation oder None.
    """
    from core.models import EmailConversation, STATE_CLOSED

    async with AsyncSessionLocal() as s:
        if in_reply_to:
            result = await s.execute(
                select(EmailConversation).where(
                    EmailConversation.last_message_id == in_reply_to
                )
            )
            conv = result.scalar_one_or_none()
            if conv:
                return conv

        # Fallback: per Email + Tenant, nicht-closed, neueste
        result = await s.execute(
            select(EmailConversation)
            .where(
                EmailConversation.tenant_id == tenant_id,
                EmailConversation.kunde_email == kunde_email.lower(),
                EmailConversation.state != STATE_CLOSED,
            )
            .order_by(EmailConversation.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def upsert_conversation(
    tenant_id: uuid.UUID,
    kunde_email: str,
    kunde_name: str | None = None,
    gcal_event_id: str | None = None,
    termin_datum=None,
    last_message_id: str | None = None,
    last_subject: str | None = None,
    state: str | None = None,
    proposed_slots=None,
    existing=None,
):
    """
    Erstellt eine neue Konversation oder aktualisiert eine bestehende
    (wenn `existing` uebergeben wird).
    """
    from core.models import EmailConversation, STATE_AWAITING_CONFIRMATION

    async with AsyncSessionLocal() as s:
        if existing is not None:
            # Re-Fetch in dieser Session (existing kommt aus anderer Session)
            result = await s.execute(
                select(EmailConversation).where(EmailConversation.id == existing.id)
            )
            conv = result.scalar_one_or_none()
        else:
            conv = None

        if conv is None:
            conv = EmailConversation(
                tenant_id=tenant_id,
                kunde_email=kunde_email.lower(),
                kunde_name=kunde_name,
                state=state or STATE_AWAITING_CONFIRMATION,
            )
            s.add(conv)

        # Update-Felder (nur wenn explizit gegeben, None heisst "nicht aendern")
        if kunde_name is not None:
            conv.kunde_name = kunde_name
        if gcal_event_id is not None:
            conv.gcal_event_id = gcal_event_id
        if termin_datum is not None:
            conv.termin_datum = termin_datum
        if last_message_id is not None:
            conv.last_message_id = last_message_id
        if last_subject is not None:
            conv.last_subject = last_subject[:500]
        if state is not None:
            conv.state = state
        if proposed_slots is not None:
            conv.proposed_slots = proposed_slots

        await s.commit()
        await s.refresh(conv)
        return conv


async def close_conversation(conv_id: uuid.UUID) -> None:
    """Markiert Konversation als closed. Wird nicht geloescht (Cleanup-Job)."""
    from core.models import EmailConversation, STATE_CLOSED

    async with AsyncSessionLocal() as s:
        result = await s.execute(
            select(EmailConversation).where(EmailConversation.id == conv_id)
        )
        conv = result.scalar_one_or_none()
        if conv:
            conv.state = STATE_CLOSED
            await s.commit()


# ---------- Brevo Outbound (Auto-Reply) ----------

async def send_reply_via_brevo(
    api_key: str,
    sender_name: str,
    sender_email: str,
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    in_reply_to: str | None = None,
    reply_to_email: str | None = None,
) -> bool:
    """Schickt Mail ueber Brevo Outbound API. silent fail bei Fehler."""
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    headers = {}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
        headers["References"] = in_reply_to
    if reply_to_email:
        # Brevo erwartet replyTo als top-level field
        payload["replyTo"] = {"email": reply_to_email, "name": sender_name}
    if headers:
        payload["headers"] = headers

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{BREVO_API_BASE}/smtp/email",
                headers={
                    "accept": "application/json",
                    "api-key": api_key,
                    "content-type": "application/json",
                },
                json=payload,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Auto-Reply gesendet an {to_email}")
                return True
            logger.warning(
                f"Brevo Outbound fehlgeschlagen: {resp.status_code} {resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.exception(f"Brevo Outbound Exception: {e}")
        return False


# ---------- Gemini-Extraction ----------

async def extract_termin_aus_mail(
    sender_name: str,
    subject: str,
    body: str,
    proposed_slots: list | None = None,
) -> dict[str, Any]:
    """
    Nutzt Gemini um Mail-Inhalt zu strukturieren.

    Returns:
        {
            "name": str,
            "anliegen": str,
            "wunschtermin_datum": str | None,  # "29.04.2026" Format
            "wunschtermin_uhrzeit": str | None,  # "14:00" Format
            "telefon": str | None,
            "klar_genug_zum_buchen": bool,
            "begruendung": str,
        }
    """
    # Lokal-Import um Plugin-Loading nicht zu blockieren wenn Gemini-Setup fehlt
    try:
        from core.ai import call_gemini  # type: ignore
    except ImportError:
        # Kein Gemini-Client vorhanden -> Fallback: alles unklar
        logger.warning("Kein Gemini-Client gefunden, Fallback auf manuelle Eskalation")
        return {
            "name": sender_name or "Unbekannt",
            "anliegen": subject or "Mail-Anfrage",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "klar_genug_zum_buchen": False,
            "begruendung": "Gemini nicht verfuegbar",
        }

    prompt = f"""Du analysierst eine eingehende E-Mail an einen Handwerksbetrieb.
Extrahiere die folgenden Felder als JSON. Wenn ein Feld nicht klar erkennbar ist, gib null zurueck.

ABSENDER: {sender_name}
BETREFF: {subject}
INHALT:
{body[:2000]}

Antworte AUSSCHLIESSLICH mit gueltigem JSON in diesem Format:
{{
  "name": "Vor- und Nachname des Absenders falls erkennbar, sonst Email-Name",
  "anliegen": "kurze Beschreibung was der Kunde will (max 80 Zeichen)",
  "wunschtermin_datum": "DD.MM.YYYY oder null",
  "wunschtermin_uhrzeit": "HH:MM oder null",
  "telefon": "+49... oder null",
  "klar_genug_zum_buchen": true oder false,
  "begruendung": "warum klar oder unklar (max 120 Zeichen)",
  "gewaehlter_slot_index": null
}}

WICHTIG fuer klar_genug_zum_buchen=true:
- Es muss ein konkretes Datum genannt sein (Mittwoch, 29.04., 'naechsten Montag', usw.)
- Es muss eine konkrete Uhrzeit genannt sein (10 Uhr, 14:30, vormittags=10:00)
- Das Anliegen muss klar sein

Bei "wann passt es Ihnen?", "rufen Sie mich an", "ich melde mich nochmal" oder vagen Anfragen -> klar_genug_zum_buchen=false.

WICHTIG: Bei JEDER Form von Verschiebung, Umbuchung, Stornierung oder Aenderung
eines bestehenden Termins -> klar_genug_zum_buchen=false und in begruendung
unterscheide:

  - "STORNO": Reine Absage ohne neuen Wunschtermin. Trigger:
    "absagen", "stornieren", "muss leider absagen", "schaffe es doch nicht",
    "nicht mehr noetig", "hat sich erledigt", "doch keinen Termin", "abbrechen",
    "Termin loeschen", "krankheitsbedingt absagen".
    -> begruendung beginnt mit "STORNO: ..."

  - "VERSCHIEBUNG": Aenderung mit oder ohne neuen Wunschtermin. Trigger:
    "verschieben", "umbuchen", "verlegen", "passt doch nicht, koennen wir...",
    "anders", "umlegen", "stattdessen", "anstatt", "frueher machen",
    "spaeter machen", "anderer Tag".
    -> begruendung beginnt mit "VERSCHIEBUNG: ..."

  - Wenn unklar zwischen den beiden: STORNO wenn KEIN neuer Wunschtermin
    erkennbar ist, sonst VERSCHIEBUNG.

Auch bei Antwort-Mails (Subject startet mit "Re:") immer extra vorsichtig pruefen
ob es eine Aenderung des bestehenden Termins ist. Im Zweifel: klar=false.

Heutiges Datum: {datetime.now().strftime('%A, %d.%m.%Y')}
"""

    # Wenn Slots vorgeschlagen wurden, erweitere den Prompt
    if proposed_slots:
        slot_lines = "\n".join([
            f"  [{i}] {s['wochentag']} {s['datum']} um {s['uhrzeit']} Uhr"
            for i, s in enumerate(proposed_slots)
        ])
        prompt += f"""

KONTEXT: Dem Kunden wurden vorher folgende Termin-Slots vorgeschlagen:
{slot_lines}

Wenn die Mail einen dieser Slots auswaehlt (auch wenn er nur die Uhrzeit nennt
oder "der erste"/"der letzte"/"Donnerstag" sagt), setze das Feld
"gewaehlter_slot_index" auf die Index-Nummer (0, 1, 2, ...).

Wenn der Kunde keinen der Slots will und einen ANDEREN Termin nennt, lass
gewaehlter_slot_index = null und behandle es als neuen Termin-Wunsch.

Wenn unklar: gewaehlter_slot_index = null, klar_genug_zum_buchen = false.

Erweitere das JSON um:
  "gewaehlter_slot_index": int oder null
"""

    try:
        response_text = await call_gemini(prompt)
        logger.info(f"Gemini-Rohantwort (erste 500 Zeichen): {response_text[:500]!r}")

        import json
        cleaned = response_text.strip()

        # Code-Fences entfernen (```json ... ```)
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)

        # Falls Gemini Text VOR oder NACH dem JSON schreibt, isoliere das JSON
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            cleaned = cleaned[first_brace : last_brace + 1]

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as parse_err:
            # Versuche Rescue: kontroll-zeichen entfernen
            logger.warning(f"JSON-Parse fehlgeschlagen, versuche Rescue: {parse_err}")
            import unicodedata
            sanitized = "".join(
                ch for ch in cleaned
                if unicodedata.category(ch)[0] != "C" or ch in ("\n", "\t", " ")
            )
            data = json.loads(sanitized)

        return data
    except Exception as e:
        logger.exception(f"Gemini-Extraction fehlgeschlagen: {e}")
        return {
            "name": sender_name or "Unbekannt",
            "anliegen": subject or "Mail-Anfrage",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "klar_genug_zum_buchen": False,
            "begruendung": f"Extraction-Fehler: {e}",
        }


# ---------- Plugin-Klasse ----------

class Plugin(BasePlugin):
    manifest = MANIFEST

    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if endpoint != "incoming":
            return {"error": f"Unbekannter Endpunkt: {endpoint}"}

        # Brevo schickt {"items": [...]} mit einer oder mehreren Mails
        items = payload.get("items", [])
        if not items:
            logger.warning("Brevo-Webhook ohne items")
            return {"ok": True, "processed": 0}

        results = []
        for item in items:
            try:
                result = await self._process_one_mail(item)
                results.append(result)
            except Exception as e:
                logger.exception(f"Mail-Verarbeitung fehlgeschlagen: {e}")
                results.append({"error": str(e)})

        return {"ok": True, "processed": len(results), "results": results}

    async def _process_one_mail(self, item: dict[str, Any]) -> dict[str, Any]:
        """Verarbeitet eine einzelne Mail aus dem Brevo-Payload."""
        from core.models import (
            STATE_AWAITING_CONFIRMATION,
            STATE_BOOKED,
            STATE_PROPOSING_SLOTS,
        )

        # Daten extrahieren
        from_obj = item.get("From", {})
        sender_email = from_obj.get("Address", "")
        sender_name = from_obj.get("Name", "") or sender_email.split("@")[0]
        subject = item.get("Subject", "(kein Betreff)")
        text_body = item.get("ExtractedMarkdownMessage") or item.get("RawTextBody", "")
        message_id = item.get("MessageId")
        in_reply_to = item.get("InReplyTo")
        spam_score = (item.get("Spam") or {}).get("Score") or item.get("SpamScore") or 0
        recipients = item.get("Recipients", [])
        to_list = item.get("To", [])

        recipient_addr = ""
        if recipients:
            recipient_addr = recipients[0] if isinstance(recipients[0], str) else recipients[0].get("Address", "")
        elif to_list:
            recipient_addr = to_list[0].get("Address", "")

        logger.info(
            f"Mail empfangen: from={sender_email} to={recipient_addr} "
            f"subject='{subject[:50]}' spam={spam_score} in_reply_to={in_reply_to}"
        )

        # 1. Spam-Filter
        if spam_score and spam_score > SPAM_SCORE_THRESHOLD:
            logger.warning(f"Spam-Mail verworfen (score={spam_score}): {sender_email}")
            return {"status": "spam_dropped", "spam_score": spam_score}

        # 2. Tenant-Routing
        tenant = await resolve_tenant_from_recipient(recipient_addr)
        if tenant is None:
            logger.warning(f"Tenant nicht gefunden fuer {recipient_addr}")
            return {"status": "tenant_not_found", "recipient": recipient_addr}

        logger.info(f"Mail-Routing: {recipient_addr} -> tenant '{tenant.slug}'")

        # 3. Globale Konfig laden
        global_cfg = await load_global_config()
        if not global_cfg:
            logger.error("Globale mail_intake-Config fehlt!")
            return {"status": "config_missing"}

        # 4. Bestehende Konversation finden
        conv = await find_conversation(tenant.id, sender_email, in_reply_to)
        if conv:
            logger.info(
                f"Konversation gefunden: id={conv.id} state={conv.state} "
                f"termin={conv.termin_datum}"
            )

        # 5. Gemini-Extraction (mit proposed_slots-Kontext falls vorhanden)
        extracted = await extract_termin_aus_mail(
            sender_name,
            subject,
            text_body,
            proposed_slots=conv.proposed_slots if conv and conv.state == STATE_PROPOSING_SLOTS else None,
        )
        logger.info(f"Extracted: {extracted}")

        klar = extracted.get("klar_genug_zum_buchen", False)
        begruendung = (extracted.get("begruendung") or "").upper()
        ist_storno = begruendung.startswith("STORNO") or "REINE ABSAGE" in begruendung
        ist_verschiebung = (
            ("VERSCHIEBUNG" in begruendung or "STORNO" in begruendung)
            and not ist_storno
        )
        slot_idx = extracted.get("gewaehlter_slot_index")

        # 6. State-Machine
        booking_result = None
        action = "neu"  # fuer Logging/Telegram

        # Fall STORNO: Kunde sagt ab, kein neuer Termin
        if ist_storno:
            if conv and conv.gcal_event_id:
                logger.info(f"Storno: cancel event {conv.gcal_event_id}")
                await self._cancel_via_kalender(tenant, conv.gcal_event_id)
                action = "storniert"
                # Konversation als closed markieren
                conv = await upsert_conversation(
                    tenant_id=tenant.id,
                    kunde_email=sender_email,
                    kunde_name=sender_name,
                    last_message_id=message_id,
                    last_subject=subject,
                    state="closed",
                    existing=conv,
                )
                # Telegram-Push + Auto-Reply
                await self._notify_tenant_telegram(
                    tenant, sender_email, sender_name, subject, extracted, action="storniert"
                )
                await self._send_storno_reply(
                    global_cfg, tenant, sender_email, sender_name, subject, message_id
                )
                return {"status": "storniert", "tenant": tenant.slug, "event_id": conv.gcal_event_id if conv else None}
            else:
                # Kein bestehender Termin gefunden - nur eskalieren
                action = "storno_ohne_termin"
                await self._notify_tenant_telegram(
                    tenant, sender_email, sender_name, subject, extracted, action="storno_ohne_termin"
                )
                await self._send_auto_reply(
                    global_cfg=global_cfg,
                    tenant=tenant,
                    sender_email=sender_email,
                    sender_name=sender_name,
                    original_subject=subject,
                    extracted=extracted,
                    booking_result=None,
                    in_reply_to=message_id,
                )
                return {"status": "storno_ohne_termin", "tenant": tenant.slug}

        # Fall A: Kunde hat einen vorgeschlagenen Slot gewaehlt
        if conv and conv.state == STATE_PROPOSING_SLOTS and slot_idx is not None:
            slots = conv.proposed_slots or []
            if 0 <= slot_idx < len(slots):
                gewaehlter_slot = slots[slot_idx]
                logger.info(f"Kunde waehlt Slot {slot_idx}: {gewaehlter_slot}")
                # Alten Termin canceln (falls vorhanden)
                if conv.gcal_event_id:
                    await self._cancel_via_kalender(tenant, conv.gcal_event_id)
                # Neuen Termin buchen
                booking_result = await self._versuche_buchung(
                    tenant,
                    sender_name,
                    {
                        "anliegen": "Termin (per Mail bestaetigt)",
                        "wunschtermin_datum": gewaehlter_slot["datum"],
                        "wunschtermin_uhrzeit": gewaehlter_slot["uhrzeit"],
                        "telefon": extracted.get("telefon"),
                    },
                )
                action = "slot_gewaehlt"

        # Fall B: Verschiebung mit konkretem Wunsch
        elif ist_verschiebung and klar and conv and conv.gcal_event_id:
            logger.info("Verschiebungs-Wunsch mit konkretem Termin")
            # Erst Slot-Verfuegbarkeit pruefen
            verfuegbar = await self._check_slot(
                tenant,
                extracted["wunschtermin_datum"],
                extracted["wunschtermin_uhrzeit"],
            )
            if verfuegbar:
                # Alten canceln, neuen buchen
                await self._cancel_via_kalender(tenant, conv.gcal_event_id)
                booking_result = await self._versuche_buchung(tenant, sender_name, extracted)
                action = "verschoben"
            else:
                # Wunsch belegt -> Slots vorschlagen
                slots = await self._slot_alternativen(
                    tenant,
                    extracted["wunschtermin_datum"],
                    extracted["wunschtermin_uhrzeit"],
                )
                conv = await upsert_conversation(
                    tenant_id=tenant.id,
                    kunde_email=sender_email,
                    kunde_name=sender_name,
                    last_message_id=message_id,
                    last_subject=subject,
                    state=STATE_PROPOSING_SLOTS,
                    proposed_slots=slots,
                    existing=conv,
                )
                action = "slots_vorgeschlagen"
                # Auto-Reply mit Slot-Vorschlaegen
                await self._send_slot_proposals(global_cfg, tenant, sender_email, sender_name, subject, message_id, extracted, slots)
                await self._notify_tenant_telegram(tenant, sender_email, sender_name, subject, extracted, action="slots_vorgeschlagen")
                return {"status": "slots_proposed", "tenant": tenant.slug, "slots_count": len(slots)}

        # Fall C: Verschiebung vage (kein Wunschtermin) oder ohne bestehenden Termin
        elif ist_verschiebung:
            # Eskalation an Tenant, kein Auto-Buchen
            action = "verschiebung_eskaliert"

        # Fall D: Klare Neue Anfrage
        elif klar and not conv:
            # Erst Slot pruefen
            verfuegbar = await self._check_slot(
                tenant,
                extracted["wunschtermin_datum"],
                extracted["wunschtermin_uhrzeit"],
            )
            if verfuegbar:
                booking_result = await self._versuche_buchung(tenant, sender_name, extracted)
                action = "neu_gebucht"
            else:
                # Slot belegt -> Alternativen
                slots = await self._slot_alternativen(
                    tenant,
                    extracted["wunschtermin_datum"],
                    extracted["wunschtermin_uhrzeit"],
                )
                conv = await upsert_conversation(
                    tenant_id=tenant.id,
                    kunde_email=sender_email,
                    kunde_name=sender_name,
                    last_message_id=message_id,
                    last_subject=subject,
                    state=STATE_PROPOSING_SLOTS,
                    proposed_slots=slots,
                )
                action = "slots_vorgeschlagen"
                await self._send_slot_proposals(global_cfg, tenant, sender_email, sender_name, subject, message_id, extracted, slots)
                await self._notify_tenant_telegram(tenant, sender_email, sender_name, subject, extracted, action="slots_vorgeschlagen")
                return {"status": "slots_proposed", "tenant": tenant.slug, "slots_count": len(slots)}

        # Fall E: Klare Anfrage aber Konversation existiert (Kunde mailt nochmal)
        elif klar and conv:
            # Behandeln wie neue Anfrage, aber alten Termin ggf. ueberschreiben
            verfuegbar = await self._check_slot(
                tenant,
                extracted["wunschtermin_datum"],
                extracted["wunschtermin_uhrzeit"],
            )
            if verfuegbar:
                if conv.gcal_event_id:
                    await self._cancel_via_kalender(tenant, conv.gcal_event_id)
                booking_result = await self._versuche_buchung(tenant, sender_name, extracted)
                action = "neu_gebucht"
            else:
                slots = await self._slot_alternativen(
                    tenant,
                    extracted["wunschtermin_datum"],
                    extracted["wunschtermin_uhrzeit"],
                )
                conv = await upsert_conversation(
                    tenant_id=tenant.id,
                    kunde_email=sender_email,
                    kunde_name=sender_name,
                    last_message_id=message_id,
                    last_subject=subject,
                    state=STATE_PROPOSING_SLOTS,
                    proposed_slots=slots,
                    existing=conv,
                )
                action = "slots_vorgeschlagen"
                await self._send_slot_proposals(global_cfg, tenant, sender_email, sender_name, subject, message_id, extracted, slots)
                await self._notify_tenant_telegram(tenant, sender_email, sender_name, subject, extracted, action="slots_vorgeschlagen")
                return {"status": "slots_proposed", "tenant": tenant.slug, "slots_count": len(slots)}

        # 7. Konversation persistieren (bei Buchung mit event_id)
        if booking_result and booking_result.get("erfolg"):
            from datetime import datetime
            try:
                termin_dt = datetime.strptime(
                    extracted.get("wunschtermin_datum") or
                    (conv.proposed_slots[slot_idx]["datum"] if conv and slot_idx is not None else ""),
                    "%d.%m.%Y"
                ).date()
            except Exception:
                termin_dt = None

            conv = await upsert_conversation(
                tenant_id=tenant.id,
                kunde_email=sender_email,
                kunde_name=sender_name,
                gcal_event_id=booking_result.get("event_id"),
                termin_datum=termin_dt,
                last_message_id=message_id,
                last_subject=subject,
                state=STATE_BOOKED,
                existing=conv,
            )
        elif not booking_result and (action == "verschiebung_eskaliert" or (not klar)):
            # Auch ohne Buchung Konversation tracken (fuer spaetere Replies)
            conv = await upsert_conversation(
                tenant_id=tenant.id,
                kunde_email=sender_email,
                kunde_name=sender_name,
                last_message_id=message_id,
                last_subject=subject,
                state=STATE_AWAITING_CONFIRMATION,
                existing=conv,
            )

        # 8. Telegram-Push an Tenant
        await self._notify_tenant_telegram(tenant, sender_email, sender_name, subject, extracted, action=action)

        # 9. Auto-Reply an Kunden
        await self._send_auto_reply(
            global_cfg=global_cfg,
            tenant=tenant,
            sender_email=sender_email,
            sender_name=sender_name,
            original_subject=subject,
            extracted=extracted,
            booking_result=booking_result,
            in_reply_to=message_id,
        )

        return {
            "status": "processed",
            "tenant": tenant.slug,
            "action": action,
            "klar": klar,
            "booking": booking_result,
        }


    async def _notify_tenant_telegram(
        self,
        tenant: Tenant,
        sender_email: str,
        sender_name: str,
        subject: str,
        extracted: dict,
        action: str = "neu",
    ) -> None:
        klar = extracted.get("klar_genug_zum_buchen", False)
        begruendung = (extracted.get("begruendung") or "").upper()
        ist_verschiebung = "VERSCHIEBUNG" in begruendung or "STORNO" in begruendung

        # Action-basierte Status-Anzeige (Multi-Turn)
        if action == "storniert":
            status_emoji = "X"
            status_text = "<b>Termin storniert</b>"
        elif action == "storno_ohne_termin":
            status_emoji = "!"
            status_text = "<b>STORNO ohne bestehenden Termin - manuell pruefen</b>"
        elif action == "neu_gebucht":
            status_emoji = "📅"
            status_text = "<b>Neuer Termin gebucht</b>"
        elif action == "verschoben":
            status_emoji = "🔄"
            status_text = "<b>Termin verschoben</b>"
        elif action == "slot_gewaehlt":
            status_emoji = "✅"
            status_text = "<b>Kunde hat Slot gewaehlt + gebucht</b>"
        elif action == "slots_vorgeschlagen":
            status_emoji = "📋"
            status_text = "<b>Slots vorgeschlagen, warte auf Bestaetigung</b>"
        elif action == "verschiebung_eskaliert":
            status_emoji = "⚠️"
            status_text = "<b>VERSCHIEBUNG ohne Wunsch - manuell pruefen</b>"
        elif ist_verschiebung:
            status_emoji = "🔄"
            status_text = "<b>VERSCHIEBUNG/STORNO - manuell pruefen</b>"
        elif klar:
            status_emoji = "📧"
            status_text = "klar"
        else:
            status_emoji = "❓"
            status_text = "<b>unklar - manuell pruefen</b>"

        text = (
            f"{status_emoji} <b>Neue Mail-Anfrage ({status_text})</b>\n"
            f"<b>Von:</b> {sender_name} ({sender_email})\n"
            f"<b>Betreff:</b> {subject[:80]}\n"
            f"<b>Anliegen:</b> {extracted.get('anliegen', '?')}\n"
        )
        if extracted.get("wunschtermin_datum"):
            text += f"<b>Wunschtermin:</b> {extracted['wunschtermin_datum']} {extracted.get('wunschtermin_uhrzeit', '')}\n"
        if extracted.get("telefon"):
            text += f"<b>Telefon:</b> {extracted['telefon']}\n"

        await TelegramNotifier.send_for_tenant(tenant.id, text)

    async def _versuche_buchung(
        self,
        tenant: Tenant,
        kunden_name: str,
        extracted: dict,
    ) -> dict | None:
        """Ruft kalender-Plugin direkt auf via dessen on_webhook."""
        try:
            from core.plugin_system import get_plugin_for_tenant
            kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
            if not kalender:
                logger.warning(f"Kalender-Plugin nicht aktiv fuer {tenant.slug}")
                return {"erfolg": False, "nachricht": "Kalender nicht aktiviert"}

            payload = {
                "name": kunden_name,
                "anliegen": extracted.get("anliegen", "Mail-Anfrage"),
                "adresse": "Per Mail nicht angegeben",
                "telefon": extracted.get("telefon"),
                "datum": extracted["wunschtermin_datum"],
                "uhrzeit": extracted["wunschtermin_uhrzeit"],
            }
            return await kalender.on_webhook("book_appointment", payload)
        except Exception as e:
            logger.exception(f"Buchung fehlgeschlagen: {e}")
            return {"erfolg": False, "nachricht": str(e)}

    async def _send_auto_reply(
        self,
        global_cfg: dict,
        tenant: Tenant,
        sender_email: str,
        sender_name: str,
        original_subject: str,
        extracted: dict,
        booking_result: dict | None,
        in_reply_to: str | None,
    ) -> None:
        """Schickt Auto-Reply an den Kunden."""
        klar = extracted.get("klar_genug_zum_buchen", False)
        gebucht = booking_result and booking_result.get("erfolg")

        # Betreff: Re: <original>
        reply_subject = original_subject
        if not reply_subject.lower().startswith("re:"):
            reply_subject = f"Re: {original_subject}"

        if gebucht:
            # Termin wurde gebucht
            html = self._build_html(
                anrede=f"Hallo {sender_name},",
                hauptteil=(
                    f"<p>vielen Dank fuer Ihre Anfrage. Ich habe den Termin "
                    f"am <b>{extracted['wunschtermin_datum']}</b> um "
                    f"<b>{extracted['wunschtermin_uhrzeit']} Uhr</b> fuer Sie eingetragen.</p>"
                    f"<p>Anliegen: {extracted.get('anliegen', '')}</p>"
                    f"<p>Falls der Termin nicht passt, antworten Sie einfach auf diese Mail "
                    f"mit einem Alternativ-Termin oder rufen Sie uns an.</p>"
                ),
                tenant=tenant,
            )
        elif klar and not gebucht:
            # Wunschtermin war klar, aber Buchung fehlgeschlagen (z.B. besetzt)
            grund = (booking_result or {}).get("nachricht", "Termin nicht verfuegbar")
            html = self._build_html(
                anrede=f"Hallo {sender_name},",
                hauptteil=(
                    f"<p>vielen Dank fuer Ihre Anfrage zum {extracted.get('wunschtermin_datum', '')}.</p>"
                    f"<p>Leider konnte ich den Termin nicht direkt buchen ({grund}). "
                    f"Wir melden uns bei Ihnen mit einem Alternativ-Vorschlag.</p>"
                ),
                tenant=tenant,
            )
        else:
            # Anliegen unklar -> Eskalation, kein Termin-Versuch
            begruendung = (extracted.get("begruendung") or "").upper()
            ist_verschiebung = "VERSCHIEBUNG" in begruendung or "STORNO" in begruendung
            if ist_verschiebung:
                html = self._build_html(
                    anrede=f"Hallo {sender_name},",
                    hauptteil=(
                        "<p>vielen Dank fuer Ihre Nachricht. Wir haben Ihre Aenderungs-Wunsch "
                        "erhalten und melden uns zeitnah mit einer Bestaetigung "
                        "oder einem Alternativ-Vorschlag.</p>"
                        "<p>Bei dringenden Aenderungen erreichen Sie uns am besten telefonisch.</p>"
                    ),
                    tenant=tenant,
                )
            else:
                html = self._build_html(
                    anrede=f"Hallo {sender_name},",
                    hauptteil=(
                        "<p>vielen Dank fuer Ihre Nachricht. Wir haben Ihre Anfrage "
                        "erhalten und melden uns zeitnah mit Termin-Vorschlaegen.</p>"
                        "<p>Falls es eilig ist, erreichen Sie uns auch telefonisch.</p>"
                    ),
                    tenant=tenant,
                )

        # Reply-To: Antworten landen auf der Tenant-Inbound-Adresse,
        # nicht auf der noreply-Hauptdomain (die keinen MX-Record hat).
        inbound_domain = global_cfg.get("inbound_domain", "reply.gewerbeagent.de")
        tenant_reply_to = f"{tenant.slug}@{inbound_domain}"

        await send_reply_via_brevo(
            api_key=global_cfg["brevo_api_key"],
            sender_name=global_cfg["sender_name"],
            sender_email=global_cfg["sender_email"],
            to_email=sender_email,
            to_name=sender_name,
            subject=reply_subject,
            html_body=html,
            in_reply_to=in_reply_to,
            reply_to_email=tenant_reply_to,
        )

    async def _check_slot(self, tenant, datum: str, uhrzeit: str) -> bool:
        """Prueft ob Slot frei ist via kalender-Plugin check_availability."""
        from core.plugin_system import get_plugin_for_tenant
        kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
        if not kalender:
            return False
        try:
            res = await kalender.on_webhook(
                "check_availability",
                {"datum": datum, "uhrzeit": uhrzeit},
            )
            return bool(res.get("verfuegbar"))
        except Exception as e:
            logger.exception(f"check_slot fehlgeschlagen: {e}")
            return False

    async def _slot_alternativen(self, tenant, datum: str, uhrzeit: str) -> list:
        """Holt freie Slots ueber kalender.find_free_slots."""
        from core.plugin_system import get_plugin_for_tenant
        kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
        if not kalender:
            return []
        try:
            res = await kalender.on_webhook(
                "find_free_slots",
                {"datum": datum, "uhrzeit": uhrzeit},
            )
            if res.get("erfolg"):
                return res.get("slots", [])
        except Exception as e:
            logger.exception(f"slot_alternativen fehlgeschlagen: {e}")
        return []

    async def _cancel_via_kalender(self, tenant, event_id: str) -> None:
        """Loescht alten Termin ueber kalender.cancel_appointment."""
        from core.plugin_system import get_plugin_for_tenant
        kalender = await get_plugin_for_tenant(tenant.slug, "kalender")
        if not kalender:
            return
        try:
            await kalender.on_webhook("cancel_appointment", {"event_id": event_id})
            logger.info(f"Termin {event_id} geloescht")
        except Exception as e:
            logger.exception(f"cancel fehlgeschlagen: {e}")

    async def _send_slot_proposals(
        self,
        global_cfg: dict,
        tenant,
        sender_email: str,
        sender_name: str,
        original_subject: str,
        in_reply_to: str | None,
        extracted: dict,
        slots: list,
    ) -> None:
        """Schickt Mail mit den vorgeschlagenen Slots."""
        if not slots:
            slot_html = "<p>Leider sind in den naechsten Tagen keine freien Termine verfuegbar. Wir melden uns telefonisch.</p>"
        else:
            slot_lines = "".join([
                f"<li><b>{s['wochentag']} {s['datum']}</b> um <b>{s['uhrzeit']} Uhr</b></li>"
                for s in slots
            ])
            slot_html = f"<ul>{slot_lines}</ul>"

        wunsch = extracted.get("wunschtermin_datum") or "Ihr Wunschtermin"
        hauptteil = (
            f"<p>vielen Dank fuer Ihre Anfrage. Leider ist {wunsch} "
            f"{extracted.get('wunschtermin_uhrzeit', '')} Uhr nicht mehr verfuegbar.</p>"
            f"<p>Wir koennen Ihnen folgende Termine anbieten:</p>"
            f"{slot_html}"
            f"<p>Bitte antworten Sie einfach auf diese Mail mit dem fuer Sie passenden Termin "
            f"(z.B. \"Donnerstag 30.04. um 8 Uhr\" oder \"erster Termin\"), und wir tragen ihn fuer Sie ein.</p>"
        )

        html = self._build_html(anrede=f"Hallo {sender_name},", hauptteil=hauptteil, tenant=tenant)

        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
        inbound_domain = global_cfg.get("inbound_domain", "reply.gewerbeagent.de")
        tenant_reply_to = f"{tenant.slug}@{inbound_domain}"

        await send_reply_via_brevo(
            api_key=global_cfg["brevo_api_key"],
            sender_name=global_cfg["sender_name"],
            sender_email=global_cfg["sender_email"],
            to_email=sender_email,
            to_name=sender_name,
            subject=reply_subject,
            html_body=html,
            in_reply_to=in_reply_to,
            reply_to_email=tenant_reply_to,
        )

    async def _send_storno_reply(
        self,
        global_cfg: dict,
        tenant,
        sender_email: str,
        sender_name: str,
        original_subject: str,
        in_reply_to: str | None,
    ) -> None:
        """Schickt Bestaetigungs-Mail nach erfolgreicher Stornierung."""
        hauptteil = (
            "<p>vielen Dank fuer Ihre Nachricht. Wir haben Ihren Termin "
            "wie gewuenscht aus unserem Kalender entfernt.</p>"
            "<p>Falls Sie zu einem spaeteren Zeitpunkt einen neuen Termin moechten, "
            "antworten Sie einfach auf diese Mail oder melden sich telefonisch.</p>"
        )
        html = self._build_html(anrede=f"Hallo {sender_name},", hauptteil=hauptteil, tenant=tenant)

        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
        inbound_domain = global_cfg.get("inbound_domain", "reply.gewerbeagent.de")
        tenant_reply_to = f"{tenant.slug}@{inbound_domain}"

        await send_reply_via_brevo(
            api_key=global_cfg["brevo_api_key"],
            sender_name=global_cfg["sender_name"],
            sender_email=global_cfg["sender_email"],
            to_email=sender_email,
            to_name=sender_name,
            subject=reply_subject,
            html_body=html,
            in_reply_to=in_reply_to,
            reply_to_email=tenant_reply_to,
        )

    def _build_html(self, anrede: str, hauptteil: str, tenant: Tenant) -> str:
        """Baut HTML-Mail mit DSGVO-Footer."""
        return f"""<!DOCTYPE html>
<html><body style="font-family: Arial, sans-serif; color: #222; max-width: 600px;">
<p>{anrede}</p>
{hauptteil}
<p>Mit freundlichen Gruessen<br>
{tenant.company_name}</p>

<hr style="border: none; border-top: 1px solid #ccc; margin-top: 30px;">
<p style="font-size: 11px; color: #888;">
<b>Hinweis:</b> Diese Nachricht wurde mit KI-Unterstuetzung verfasst.
Ihre Anfrage wird zur Terminvereinbarung verarbeitet.
Mehr Informationen zum Datenschutz finden Sie unter
<a href="https://gewerbeagent.de/datenschutz">gewerbeagent.de/datenschutz</a>.
</p>
</body></html>"""
