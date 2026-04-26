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
) -> bool:
    """Schickt Mail ueber Brevo Outbound API. silent fail bei Fehler."""
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if in_reply_to:
        payload["headers"] = {"In-Reply-To": in_reply_to}

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
  "begruendung": "warum klar oder unklar (max 120 Zeichen)"
}}

WICHTIG fuer klar_genug_zum_buchen=true:
- Es muss ein konkretes Datum genannt sein (Mittwoch, 29.04., 'naechsten Montag', usw.)
- Es muss eine konkrete Uhrzeit genannt sein (10 Uhr, 14:30, vormittags=10:00)
- Das Anliegen muss klar sein

Bei "wann passt es Ihnen?", "rufen Sie mich an", "ich melde mich nochmal" oder vagen Anfragen -> klar_genug_zum_buchen=false.

Heutiges Datum: {datetime.now().strftime('%A, %d.%m.%Y')}
"""

    try:
        response_text = await call_gemini(prompt)
        # JSON aus Antwort extrahieren
        import json
        # Gemini gibt manchmal ```json ... ``` zurueck
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        data = json.loads(cleaned)
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
        # Daten extrahieren
        from_obj = item.get("From", {})
        sender_email = from_obj.get("Address", "")
        sender_name = from_obj.get("Name", "") or sender_email.split("@")[0]
        subject = item.get("Subject", "(kein Betreff)")
        text_body = item.get("ExtractedMarkdownMessage") or item.get("RawTextBody", "")
        message_id = item.get("MessageId")
        spam_score = (item.get("Spam") or {}).get("Score") or item.get("SpamScore") or 0
        recipients = item.get("Recipients", [])
        to_list = item.get("To", [])

        # Empfaenger-Adresse: bevorzugt aus Recipients (RCPT TO), sonst To
        recipient_addr = ""
        if recipients:
            recipient_addr = recipients[0] if isinstance(recipients[0], str) else recipients[0].get("Address", "")
        elif to_list:
            recipient_addr = to_list[0].get("Address", "")

        logger.info(
            f"Mail empfangen: from={sender_email} to={recipient_addr} "
            f"subject='{subject[:50]}' spam={spam_score}"
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

        # 3. Globale Konfig laden (Brevo-Credentials)
        global_cfg = await load_global_config()
        if not global_cfg:
            logger.error("Globale mail_intake-Config fehlt!")
            return {"status": "config_missing"}

        # 4. Gemini-Extraction
        extracted = await extract_termin_aus_mail(sender_name, subject, text_body)
        logger.info(f"Extracted: {extracted}")

        # 5. Telegram-Push an Tenant (immer, egal ob klar oder nicht)
        await self._notify_tenant_telegram(tenant, sender_email, sender_name, subject, extracted)

        # 6. Wenn klar -> Buchungsversuch
        booking_result = None
        if extracted.get("klar_genug_zum_buchen"):
            booking_result = await self._versuche_buchung(tenant, sender_name, extracted)

        # 7. Auto-Reply an Kunden
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
            "sender": sender_email,
            "klar": extracted.get("klar_genug_zum_buchen"),
            "booking": booking_result,
        }

    async def _notify_tenant_telegram(
        self,
        tenant: Tenant,
        sender_email: str,
        sender_name: str,
        subject: str,
        extracted: dict,
    ) -> None:
        klar = extracted.get("klar_genug_zum_buchen", False)
        status_emoji = "📧" if klar else "❓"
        status_text = "klar" if klar else "<b>unklar - manuell pruefen</b>"

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
            html = self._build_html(
                anrede=f"Hallo {sender_name},",
                hauptteil=(
                    f"<p>vielen Dank fuer Ihre Nachricht. Wir haben Ihre Anfrage "
                    f"erhalten und melden uns zeitnah mit Termin-Vorschlaegen.</p>"
                    f"<p>Falls es eilig ist, erreichen Sie uns auch telefonisch.</p>"
                ),
                tenant=tenant,
            )

        await send_reply_via_brevo(
            api_key=global_cfg["brevo_api_key"],
            sender_name=global_cfg["sender_name"],
            sender_email=global_cfg["sender_email"],
            to_email=sender_email,
            to_name=sender_name,
            subject=reply_subject,
            html_body=html,
            in_reply_to=in_reply_to,
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
