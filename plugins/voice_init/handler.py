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
        }


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

