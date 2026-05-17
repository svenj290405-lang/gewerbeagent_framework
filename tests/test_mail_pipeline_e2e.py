"""End-to-End-Tests fuer die Mail-Pipeline (_process_one_mail).

Deckt die wichtigsten Pfade des Mail-Intake-State-Machines ab:
- STORNO mit gefundenen Events -> cancel + storno_reply
- STORNO ohne gefundenen Events -> storno_ohne_termin + auto_reply
- Neu-Buchung klar + Slot frei -> book_appointment + Bestaetigungs-Reply
- Neu-Buchung klar + Slot belegt -> Slot-Alternativen + Slot-Vorschlags-Reply
- Slot-Wahl bei PROPOSING_SLOTS-Konversation -> cancel old + book new
- Unklare Mail -> Eskalations-Reply
- Spam-Filter dropt die Mail bevor irgendwas passiert
- Unbekannter Tenant -> tenant_not_found

Externe Abhaengigkeiten (Brevo, Gemini, kalender-Plugin, DB) werden
gemockt — die Tests verifizieren die Verzweigungs-Logik im Handler,
nicht die unterliegenden Services.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.mail_intake import handler as mh


# =====================================================================
# Test-Doubles
# =====================================================================

def _make_tenant(slug: str = "demo"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        company_name=f"{slug.capitalize()} GmbH",
        branche="Handwerk",
    )


def _make_mail_item(
    *,
    sender_email: str = "kunde@example.de",
    sender_name: str = "Max Kunde",
    subject: str = "Terminanfrage",
    body: str = "Hallo, ich braeuchte einen Termin.",
    recipient: str = "demo@reply.gewerbeagent.de",
    message_id: str = "<msg-1@example.de>",
    in_reply_to: str | None = None,
    spam_score: float = 0.0,
) -> dict:
    return {
        "From": {"Address": sender_email, "Name": sender_name},
        "Subject": subject,
        "ExtractedMarkdownMessage": body,
        "MessageId": message_id,
        "InReplyTo": in_reply_to,
        "Spam": {"Score": spam_score},
        "Recipients": [recipient],
        "To": [{"Address": recipient}],
    }


class _FakeKalender:
    """Mock-Plugin mit konfigurierbaren on_webhook-Responses pro endpoint."""

    def __init__(
        self,
        *,
        find_events_result: dict | None = None,
        check_availability_result: dict | None = None,
        find_free_slots_result: dict | None = None,
        book_appointment_result: dict | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self.find_events_result = find_events_result or {
            "erfolg": True, "anzahl": 0, "termine": [],
        }
        self.check_availability_result = check_availability_result or {
            "verfuegbar": True,
        }
        self.find_free_slots_result = find_free_slots_result or {
            "erfolg": True, "slots": [], "smart_routing": {},
        }
        self.book_appointment_result = book_appointment_result or {
            "erfolg": True, "event_id": "evt-new",
        }

    async def on_webhook(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        if endpoint == "find_events":
            return self.find_events_result
        if endpoint == "check_availability":
            return self.check_availability_result
        if endpoint == "find_free_slots":
            return self.find_free_slots_result
        if endpoint == "book_appointment":
            return self.book_appointment_result
        if endpoint == "cancel_appointment":
            return {"erfolg": True}
        return {}


def _make_plugin():
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return mh.Plugin(context)


def _patch_pipeline(
    monkeypatch,
    *,
    tenant,
    kalender,
    extracted: dict,
    conv=None,
    classification: str = "TERMINANFRAGE",
    sent_replies: list | None = None,
    telegram_calls: list | None = None,
    upserts: list | None = None,
):
    """Wired alle externen Abhaengigkeiten von _process_one_mail.

    sent_replies/telegram_calls/upserts (falls uebergeben) werden mit
    den Aufrufen befuellt — so koennen die Tests verifizieren was
    rausgegangen ist.
    """
    if sent_replies is None:
        sent_replies = []
    if telegram_calls is None:
        telegram_calls = []
    if upserts is None:
        upserts = []

    # Tenant + Global-Config
    monkeypatch.setattr(
        mh, "resolve_tenant_from_recipient",
        AsyncMock(return_value=tenant),
    )
    monkeypatch.setattr(
        mh, "load_global_config",
        AsyncMock(return_value={
            "brevo_api_key": "fake-key",
            "sender_name": "Gewerbeagent",
            "sender_email": "noreply@gewerbeagent.de",
            "inbound_domain": "reply.gewerbeagent.de",
        }),
    )

    # Konversation
    monkeypatch.setattr(
        mh, "find_conversation",
        AsyncMock(return_value=conv),
    )

    async def _fake_upsert(**kwargs):
        upserts.append(kwargs)
        existing = kwargs.get("existing")
        if existing is not None:
            for k, v in kwargs.items():
                if k in ("existing", "tenant_id", "kunde_email"):
                    continue
                if v is not None:
                    setattr(existing, k, v)
            return existing
        return SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=kwargs.get("tenant_id"),
            kunde_email=kwargs.get("kunde_email"),
            kunde_name=kwargs.get("kunde_name"),
            state=kwargs.get("state"),
            gcal_event_id=kwargs.get("gcal_event_id"),
            proposed_slots=kwargs.get("proposed_slots"),
            termin_datum=kwargs.get("termin_datum"),
            last_q_reply=kwargs.get("last_q_reply"),
            last_user_message=kwargs.get("last_user_message"),
        )

    monkeypatch.setattr(mh, "upsert_conversation", _fake_upsert)

    # Gemini-Mocks (subject-classification + extraction)
    import core.ai.gemini as gem
    monkeypatch.setattr(
        gem, "classify_mail_subject",
        AsyncMock(return_value={
            "classification": classification,
            "confidence": "high",
            "reason": "Test-Mock",
        }),
    )
    monkeypatch.setattr(
        mh, "extract_termin_aus_mail",
        AsyncMock(return_value=extracted),
    )
    monkeypatch.setattr(
        mh, "humanize_termin_bestaetigung", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        mh, "humanize_eingangsbestaetigung", AsyncMock(return_value=None),
    )

    # Skill-Routing (no-op)
    import core.routing as routing
    monkeypatch.setattr(
        routing, "choose_employee",
        AsyncMock(return_value=None),
    )

    # Kalender-Plugin-Lookup
    import core.plugin_system as ps
    monkeypatch.setattr(
        ps, "get_plugin_for_tenant",
        AsyncMock(return_value=kalender),
    )

    # Brevo-Outbound: erfassen statt senden
    async def _fake_send(**kwargs):
        sent_replies.append(kwargs)
        return True

    monkeypatch.setattr(mh, "send_reply_via_brevo", _fake_send)

    # Telegram-Push: erfassen statt senden
    from plugins.telegram_notify.handler import TelegramNotifier

    async def _fake_tg(tenant_id, text, *, employee_id=None):
        telegram_calls.append({
            "tenant_id": tenant_id,
            "text": text,
            "employee_id": employee_id,
        })
        return True

    monkeypatch.setattr(TelegramNotifier, "send_for_tenant", _fake_tg)

    return {
        "sent_replies": sent_replies,
        "telegram_calls": telegram_calls,
        "upserts": upserts,
    }


# =====================================================================
# STORNO-Flow
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_storno_with_found_events(monkeypatch):
    """Storno-Mail + find_events findet Termine -> alle werden gecancelt
    + Storno-Bestaetigung an Kunden + Telegram-Push an Tenant."""
    tenant = _make_tenant()
    kalender = _FakeKalender(find_events_result={
        "erfolg": True, "anzahl": 2,
        "termine": [{"event_id": "evt-1"}, {"event_id": "evt-2"}],
    })
    sent: list = []
    tg: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": False,
            "begruendung": "STORNO: Kunde sagt ab",
            "anliegen": "Storno",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "kunde_adresse": None,
        },
        sent_replies=sent,
        telegram_calls=tg,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        subject="Termin absagen", body="Ich muss leider absagen",
    ))

    assert result["status"] == "storniert"
    assert set(result["event_ids"]) == {"evt-1", "evt-2"}

    cancelled = [p["event_id"] for ep, p in kalender.calls
                 if ep == "cancel_appointment"]
    assert set(cancelled) == {"evt-1", "evt-2"}

    # Storno-Reply ist raus
    assert len(sent) == 1
    assert "entfernt" in sent[0]["html_body"]

    # Telegram-Push mit action=storniert
    assert len(tg) == 1


@pytest.mark.asyncio
async def test_pipeline_storno_without_events(monkeypatch):
    """Storno-Mail aber find_events leer + kein conv -> storno_ohne_termin
    + Standard-Auto-Reply (kein book, kein cancel)."""
    tenant = _make_tenant()
    kalender = _FakeKalender()  # find_events leer
    sent: list = []
    tg: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": False,
            "begruendung": "STORNO: Reine Absage",
            "anliegen": "Storno",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "kunde_adresse": None,
        },
        sent_replies=sent, telegram_calls=tg,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        subject="Storno", body="Brauche den Termin doch nicht",
    ))

    assert result["status"] == "storno_ohne_termin"
    cancelled = [c for c in kalender.calls if c[0] == "cancel_appointment"]
    assert cancelled == []
    # Trotzdem Auto-Reply + Telegram-Push
    assert len(sent) == 1
    assert len(tg) == 1


# =====================================================================
# Neu-Buchung-Flow
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_neu_klar_slot_frei_books(monkeypatch):
    """klar + kein conv + slot verfuegbar -> book_appointment +
    Bestaetigungs-Reply."""
    tenant = _make_tenant()
    kalender = _FakeKalender(
        check_availability_result={"verfuegbar": True},
        book_appointment_result={"erfolg": True, "event_id": "evt-fresh"},
    )
    sent: list = []
    tg: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": True,
            "begruendung": "Klar",
            "anliegen": "Heizungs-Check",
            "wunschtermin_datum": "22.05.2026",
            "wunschtermin_uhrzeit": "14:00",
            "telefon": "+49 30 1234",
            "kunde_adresse": "Teststr 1, 10115 Berlin",
        },
        sent_replies=sent, telegram_calls=tg,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        subject="Heizung", body="Bitte am 22.05.2026 um 14:00",
    ))

    assert result["status"] == "processed"
    assert result["action"] == "neu_gebucht"
    assert result["booking"]["erfolg"] is True
    assert result["booking"]["event_id"] == "evt-fresh"

    # check_availability + book_appointment wurden gerufen
    eps = [c[0] for c in kalender.calls]
    assert "check_availability" in eps
    assert "book_appointment" in eps
    book_payload = next(p for ep, p in kalender.calls if ep == "book_appointment")
    assert book_payload["datum"] == "22.05.2026"
    assert book_payload["uhrzeit"] == "14:00"
    # idempotency_key = MessageId (verhindert Doppelbuchung bei Re-Polling)
    assert book_payload["idempotency_key"] == "<msg-1@example.de>"

    # Reply ist Bestaetigung mit Datum
    assert len(sent) == 1
    assert "22.05.2026" in sent[0]["html_body"]


@pytest.mark.asyncio
async def test_pipeline_neu_klar_slot_belegt_proposes_slots(monkeypatch):
    """klar + Slot belegt -> find_free_slots + Slot-Vorschlags-Reply."""
    tenant = _make_tenant()
    kalender = _FakeKalender(
        check_availability_result={"verfuegbar": False},
        find_free_slots_result={
            "erfolg": True,
            "slots": [
                {"wochentag": "Donnerstag", "datum": "22.05.2026", "uhrzeit": "09:00"},
                {"wochentag": "Donnerstag", "datum": "22.05.2026", "uhrzeit": "11:00"},
            ],
            "smart_routing": {},
        },
    )
    sent: list = []
    tg: list = []
    upserts: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": True,
            "begruendung": "Klar",
            "anliegen": "Beratung",
            "wunschtermin_datum": "22.05.2026",
            "wunschtermin_uhrzeit": "14:00",
            "telefon": None,
            "kunde_adresse": None,
        },
        sent_replies=sent, telegram_calls=tg, upserts=upserts,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item())

    assert result["status"] == "slots_proposed"
    assert result["slots_count"] == 2

    # KEIN Book (Slot belegt!)
    booked = [c for c in kalender.calls if c[0] == "book_appointment"]
    assert booked == []

    # Reply enthaelt die vorgeschlagenen Slots
    assert len(sent) == 1
    assert "09:00" in sent[0]["html_body"]
    assert "11:00" in sent[0]["html_body"]

    # Conversation wurde in STATE_PROPOSING_SLOTS gespeichert
    proposing = [u for u in upserts
                 if u.get("state") == "proposing_slots"
                 and u.get("proposed_slots")]
    assert len(proposing) == 1


# =====================================================================
# Slot-Wahl bei bestehender Konversation
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_slot_choice_cancels_old_and_books_new(monkeypatch):
    """Konversation in PROPOSING_SLOTS + Kunde waehlt Slot 1 ->
    alten Termin (falls vorhanden) canceln + neuen buchen."""
    tenant = _make_tenant()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        kunde_email="kunde@example.de",
        kunde_name="Max",
        state="proposing_slots",
        gcal_event_id="evt-old",
        proposed_slots=[
            {"datum": "22.05.2026", "uhrzeit": "09:00"},
            {"datum": "22.05.2026", "uhrzeit": "11:00"},
        ],
        termin_datum=None,
        last_q_reply=None,
        last_user_message=None,
        assigned_employee_id=None,
    )
    kalender = _FakeKalender(
        book_appointment_result={"erfolg": True, "event_id": "evt-chosen"},
    )
    sent: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender, conv=conv,
        extracted={
            "klar_genug_zum_buchen": True,
            "begruendung": "Klar",
            "anliegen": "Slot bestaetigt",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "kunde_adresse": None,
            "gewaehlter_slot_index": 1,
        },
        sent_replies=sent,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        subject="Re: Termin-Vorschlaege",
        body="Der zweite Termin passt mir",
    ))

    assert result["status"] == "processed"
    assert result["action"] == "slot_gewaehlt"

    # Alter Termin gecancelt + neuer mit gewaehlter Slot-Zeit gebucht
    cancelled = [p["event_id"] for ep, p in kalender.calls
                 if ep == "cancel_appointment"]
    assert "evt-old" in cancelled

    book_payload = next(p for ep, p in kalender.calls if ep == "book_appointment")
    assert book_payload["datum"] == "22.05.2026"
    assert book_payload["uhrzeit"] == "11:00"  # Slot-Index 1


# =====================================================================
# Eskalations-Flow (unklar)
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_unklar_sends_eskalation_reply(monkeypatch):
    """klar=False, kein Storno, keine Verschiebung -> Eskalations-Reply
    ohne Buchung, ohne Slot-Vorschlaege."""
    tenant = _make_tenant()
    kalender = _FakeKalender()
    sent: list = []
    tg: list = []
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": False,
            "begruendung": "Wann passt es Ihnen?",
            "anliegen": "Unklar",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "kunde_adresse": None,
        },
        sent_replies=sent, telegram_calls=tg,
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        subject="Frage", body="Wann koennten Sie kommen?",
    ))

    assert result["status"] == "processed"
    assert result["klar"] is False
    assert result["booking"] is None

    # Kein book / cancel
    eps = [c[0] for c in kalender.calls]
    assert "book_appointment" not in eps
    assert "cancel_appointment" not in eps

    # Eskalations-Reply + Telegram
    assert len(sent) == 1
    assert len(tg) == 1


# =====================================================================
# Edge-Cases
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_spam_dropped_before_processing(monkeypatch):
    """Hoher Spam-Score -> Mail wird verworfen, kein Tenant-Lookup."""
    # Reolver darf nicht gerufen werden — wenn doch, fail.
    resolver = AsyncMock(side_effect=AssertionError("Resolver darf nicht gerufen werden"))
    monkeypatch.setattr(mh, "resolve_tenant_from_recipient", resolver)
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(spam_score=99.0))
    assert result["status"] == "spam_dropped"
    assert result["spam_score"] == 99.0


@pytest.mark.asyncio
async def test_pipeline_unknown_tenant_returns_not_found(monkeypatch):
    """Unbekannter Recipient -> tenant_not_found, kein Processing."""
    monkeypatch.setattr(
        mh, "resolve_tenant_from_recipient",
        AsyncMock(return_value=None),
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item(
        recipient="unbekannt@reply.gewerbeagent.de",
    ))
    assert result["status"] == "tenant_not_found"


@pytest.mark.asyncio
async def test_pipeline_missing_global_config_returns_error(monkeypatch):
    """Wenn _global mail_intake-Config fehlt -> config_missing."""
    tenant = _make_tenant()
    monkeypatch.setattr(
        mh, "resolve_tenant_from_recipient",
        AsyncMock(return_value=tenant),
    )
    monkeypatch.setattr(
        mh, "load_global_config",
        AsyncMock(return_value=None),
    )
    # Klassifikator wird vor load_global_config gerufen
    import core.ai.gemini as gem
    monkeypatch.setattr(
        gem, "classify_mail_subject",
        AsyncMock(return_value={"classification": "TERMINANFRAGE",
                                "confidence": "high", "reason": ""}),
    )
    plugin = _make_plugin()
    result = await plugin._process_one_mail(_make_mail_item())
    assert result["status"] == "config_missing"


# =====================================================================
# on_webhook-Level (Webhook-Secret + Items-Loop)
# =====================================================================

@pytest.mark.asyncio
async def test_on_webhook_rejects_wrong_secret(monkeypatch):
    """Falsches X-Webhook-Secret -> PermissionError, keine Mail-Verarbeitung."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "brevo_webhook_secret",
                        "expected-secret", raising=False)
    plugin = _make_plugin()
    with pytest.raises(PermissionError):
        await plugin.on_webhook(
            "incoming",
            {"items": [_make_mail_item()]},
            headers={"x-webhook-secret": "wrong"},
        )


@pytest.mark.asyncio
async def test_on_webhook_unknown_endpoint_returns_error(monkeypatch):
    """Unbekannter Endpunkt -> Error-Dict, keine Verarbeitung."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "brevo_webhook_secret",
                        "", raising=False)
    plugin = _make_plugin()
    result = await plugin.on_webhook("foobar", {"items": []})
    assert "error" in result


@pytest.mark.asyncio
async def test_on_webhook_processes_multiple_items(monkeypatch):
    """Brevo schickt {"items": [...]} mit mehreren Mails -> alle landen
    in results, eine fehlerhafte stoppt die anderen nicht."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "brevo_webhook_secret",
                        "", raising=False)
    tenant = _make_tenant()
    kalender = _FakeKalender()
    _patch_pipeline(
        monkeypatch, tenant=tenant, kalender=kalender,
        extracted={
            "klar_genug_zum_buchen": False,
            "begruendung": "Unklar",
            "anliegen": "Mail",
            "wunschtermin_datum": None,
            "wunschtermin_uhrzeit": None,
            "telefon": None,
            "kunde_adresse": None,
        },
    )
    plugin = _make_plugin()
    result = await plugin.on_webhook("incoming", {
        "items": [
            _make_mail_item(message_id="<a@x>"),
            _make_mail_item(message_id="<b@x>"),
        ],
    })
    assert result["ok"] is True
    assert result["processed"] == 2
    assert len(result["results"]) == 2
