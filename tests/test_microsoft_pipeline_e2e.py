"""End-to-End-Tests fuer die Microsoft-Mail-Pipeline.

Komplement zu tests/test_mail_pipeline_e2e.py (das die alte Brevo-
Pipeline testet). Hier wird die NEUE Microsoft-Pipeline abgedeckt:
  - poll_microsoft_inbox-Dispatch (neu_anfrage / followup / 3 Intents
    / spam / low-conf)
  - Intent-Handler (_handle_storno_intent, _handle_verschiebung_intent,
    _handle_rechnungsanfrage_intent)
  - mail_pipeline Helper (Konv-Lookup, Persistenz, Telegram-Pushes)
  - classify_mail_subject Intent-Klassifikation + Keyword-Backup
  - Reply-Threading via send_tracked_mail
  - Bounce-Tracking
  - Regression-Tests gegen die Audit-Funde aus Teil A

Externe Abhaengigkeiten (Microsoft Graph, Gemini, Telegram, Postgres,
Kalender-Plugin) werden gemockt. Die Tests verifizieren Verzweigungs-
logik + Persistenz-Calls + Push-Inhalte, nicht die unterliegenden
Services selbst.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.integrations import mail_pipeline
from core.integrations import microsoft_inbox
import core.ai.gemini as gem


# =====================================================================
# Test-Doubles
# =====================================================================

def _make_tenant(
    slug: str = "demo",
    *,
    company_name: str | None = "Tischlerei Dietz",
    contact_phone: str = "+49 211 12345",
    contact_name: str = "Daniel Dietz",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        company_name=company_name,
        branche="Tischler",
        contact_name=contact_name,
        contact_email="info@dietz.de",
        contact_phone=contact_phone,
    )


def _make_conv(
    *,
    tenant_id: uuid.UUID | None = None,
    kunde_email: str = "kunde@example.de",
    kunde_name: str = "Sven Kunde",
    state: str = "awaiting_confirmation",
    last_subject: str | None = "Re: Anfrage",
    last_message_id: str | None = "<q-reply-1@dietz.de>",
    gcal_event_id: str | None = None,
    assigned_employee_id: uuid.UUID | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        kunde_email=kunde_email,
        kunde_name=kunde_name,
        state=state,
        last_subject=last_subject,
        last_message_id=last_message_id,
        gcal_event_id=gcal_event_id,
        assigned_employee_id=assigned_employee_id,
        classification_reason=None,
    )


class _FakeKalender:
    """Mock-Kalender-Plugin mit konfigurierbaren Endpoint-Responses."""

    def __init__(
        self,
        *,
        find_events_result: dict | None = None,
        cancel_appointment_result: dict | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self.find_events_result = find_events_result or {
            "erfolg": True, "anzahl": 0, "termine": [],
        }
        self.cancel_appointment_result = cancel_appointment_result or {
            "erfolg": True,
        }

    async def on_webhook(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        if endpoint == "find_events":
            return self.find_events_result
        if endpoint == "cancel_appointment":
            return self.cancel_appointment_result
        return {}


# =====================================================================
# Pytest-Fixtures: Push-Capture + Mail-Send-Capture
# =====================================================================

@pytest.fixture
def push_capture(monkeypatch):
    """Captured calls zu TelegramNotifier.send_for_tenant."""
    calls: list[dict] = []

    async def fake_send(tenant_id, text, *, employee_id=None):
        calls.append({
            "tenant_id": tenant_id, "text": text,
            "employee_id": employee_id,
        })
        return True

    import plugins.telegram_notify.handler as tnh
    monkeypatch.setattr(tnh.TelegramNotifier, "send_for_tenant", fake_send)
    return calls


@pytest.fixture
def mail_send_capture(monkeypatch):
    """Captured calls zu core.integrations.microsoft.send_tracked_mail
    (das von allen mail_pipeline-Send-Helpers benutzt wird)."""
    calls: list[dict] = []

    async def fake_send_tracked(
        *, tenant_id, to_email, subject, body_html,
        cc=None, attachments=None, employee_id=None,
    ):
        calls.append({
            "tenant_id": tenant_id, "to_email": to_email,
            "subject": subject, "body_html": body_html,
            "employee_id": employee_id,
        })
        return {
            "success": True,
            "message_id": f"ms-msg-{len(calls)}",
            "internet_message_id": f"<sent-{len(calls)}@dietz.de>",
            "conversation_id": f"ms-conv-{len(calls)}",
            "error": None,
        }

    import core.integrations.microsoft as ms
    monkeypatch.setattr(ms, "send_tracked_mail", fake_send_tracked)
    return calls


@pytest.fixture
def persistence_capture(monkeypatch):
    """Mockt mail_pipeline's DB-Funktionen (create_conversation,
    find_*, record_*, mark_*, set_conversation_state) und zeichnet die
    Aufrufe auf — damit die Tests keine echte Postgres-Verbindung
    brauchen.
    """
    captured: dict = {
        "create_conversation": [],
        "find_open_conversation": [],
        "find_conversation_by_event_id": [],
        "find_conversation_by_outbound_message_id": [],
        "record_inbound": [],
        "record_outbound_q_reply": [],
        "mark_delivery_failed": [],
        "set_conversation_state": [],
    }

    async def fake_create(tenant_id, sender_email, sender_name, subject, **kw):
        conv = _make_conv(
            tenant_id=tenant_id,
            kunde_email=sender_email,
            kunde_name=sender_name,
            state=kw.get("state", "awaiting_confirmation"),
            gcal_event_id=kw.get("gcal_event_id"),
            assigned_employee_id=kw.get("assigned_employee_id"),
        )
        captured["create_conversation"].append({
            "tenant_id": tenant_id, "sender_email": sender_email,
            "sender_name": sender_name, "subject": subject, **kw,
            "_returned_conv_id": conv.id,
        })
        return conv

    async def fake_find_open(tenant_id, sender_email, **kw):
        captured["find_open_conversation"].append({
            "tenant_id": tenant_id, "sender_email": sender_email, **kw,
        })
        # Default: nichts gefunden (Neukunde). Tests die existierende
        # Konv. brauchen, ueberschreiben das selber.
        return None

    async def fake_find_event(tenant_id, event_id):
        captured["find_conversation_by_event_id"].append({
            "tenant_id": tenant_id, "event_id": event_id,
        })
        return None

    async def fake_find_outbound(tenant_id, msg_id):
        captured["find_conversation_by_outbound_message_id"].append({
            "tenant_id": tenant_id, "outbound_message_id": msg_id,
        })
        return None

    async def fake_record_inbound(conv_id, **kw):
        captured["record_inbound"].append({"conv_id": conv_id, **kw})

    async def fake_record_outbound(conv_id, **kw):
        captured["record_outbound_q_reply"].append({"conv_id": conv_id, **kw})

    async def fake_mark_delivery_failed(conv_id, *, reason=None):
        captured["mark_delivery_failed"].append({
            "conv_id": conv_id, "reason": reason,
        })

    async def fake_set_state(conv_id, state):
        captured["set_conversation_state"].append({
            "conv_id": conv_id, "state": state,
        })

    monkeypatch.setattr(mail_pipeline, "create_conversation", fake_create)
    monkeypatch.setattr(mail_pipeline, "find_open_conversation", fake_find_open)
    monkeypatch.setattr(
        mail_pipeline, "find_conversation_by_event_id", fake_find_event,
    )
    monkeypatch.setattr(
        mail_pipeline, "find_conversation_by_outbound_message_id",
        fake_find_outbound,
    )
    monkeypatch.setattr(mail_pipeline, "record_inbound", fake_record_inbound)
    monkeypatch.setattr(
        mail_pipeline, "record_outbound_q_reply", fake_record_outbound,
    )
    monkeypatch.setattr(
        mail_pipeline, "mark_delivery_failed", fake_mark_delivery_failed,
    )
    monkeypatch.setattr(
        mail_pipeline, "set_conversation_state", fake_set_state,
    )
    return captured


@pytest.fixture
def kalender_capture(monkeypatch):
    """Mockt get_plugin_for_tenant um einen FakeKalender zu liefern.
    Tests koennen die Instanz pro Test konfigurieren via kalender.find_events_result.
    """
    holder: dict = {"kalender": _FakeKalender()}

    async def fake_get_plugin(tenant_slug, plugin_name):
        if plugin_name == "kalender":
            return holder["kalender"]
        return None

    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", fake_get_plugin)
    return holder


# =====================================================================
# 1. HELPERS: HTML-Render, Keyword-Detection, Header-Parsing
# =====================================================================

def test_buche_confirmation_html_renders_all_fields():
    html = mail_pipeline._build_buche_confirmation_html(
        kunde_anrede="Sven", company_name="Tischlerei Dietz",
        datum_label="20.05.2026", uhrzeit="14:00",
        employee_name="Marco", anliegen="Kuechenmontage",
        contact_phone="+49 211 1234",
    )
    assert "Sven" in html
    assert "20.05.2026" in html
    assert "14:00" in html
    assert "Marco" in html
    assert "Kuechenmontage" in html
    assert "+49 211 1234" in html
    assert "verschieben oder absagen" in html


def test_buche_confirmation_html_handles_missing_employee_and_phone():
    html = mail_pipeline._build_buche_confirmation_html(
        kunde_anrede="", company_name="Dietz", datum_label="20.05.2026",
        uhrzeit="14:00", employee_name=None, anliegen="Test",
        contact_phone="",
    )
    assert "Hallo," in html  # leere Anrede → generic
    assert "Marco" not in html
    assert "Rueckruf-Nummer" not in html


def test_storno_html_one_cancelled_says_storniert():
    html = mail_pipeline._build_storno_html(
        kunde_anrede="Anna", company_name="Dietz",
        cancelled_count=1, original_subject="Termin",
    )
    assert "Anna" in html
    assert "storniert" in html
    assert "keine Rechnung" in html


def test_storno_html_zero_cancelled_uses_fallback():
    html = mail_pipeline._build_storno_html(
        kunde_anrede="", company_name="Dietz",
        cancelled_count=0, original_subject="?",
    )
    assert "keinen bestehenden Termin" in html


def test_verschiebung_html_quotes_first_found_termin():
    html = mail_pipeline._build_verschiebung_html(
        kunde_anrede="Maria", company_name="Dietz",
        found_termine=[
            {"datum": "23.05.2026", "uhrzeit": "14:00"},
            {"datum": "24.05.2026", "uhrzeit": "10:00"},
        ],
    )
    assert "23.05.2026" in html
    assert "Wunsch-Ersatztermin" in html


def test_verschiebung_html_without_termine_rueckfrage():
    html = mail_pipeline._build_verschiebung_html(
        kunde_anrede="Maria", company_name="Dietz", found_termine=[],
    )
    assert "keinen bestehenden Termin" in html
    assert "Wunsch-Ersatztermin" in html


def test_extract_in_reply_to_case_insensitive():
    headers = [
        {"name": "Received", "value": "from mail"},
        {"name": "in-reply-to", "value": "<abc@gmx.de>"},
    ]
    assert (
        mail_pipeline.extract_in_reply_to_from_headers(headers) == "<abc@gmx.de>"
    )


def test_extract_in_reply_to_returns_first_match():
    headers = [
        {"name": "In-Reply-To", "value": "<first@x>"},
        {"name": "In-Reply-To", "value": "<second@x>"},
    ]
    assert mail_pipeline.extract_in_reply_to_from_headers(headers) == "<first@x>"


def test_extract_in_reply_to_no_match():
    assert mail_pipeline.extract_in_reply_to_from_headers([]) is None
    assert mail_pipeline.extract_in_reply_to_from_headers(None) is None
    assert mail_pipeline.extract_in_reply_to_from_headers(
        [{"name": "From", "value": "x@y"}]
    ) is None


def test_intent_keyword_storno():
    assert (
        gem._detect_intent_keywords("Mein Termin", "muss leider absagen")
        == gem.INTENT_TERMIN_STORNIEREN
    )


def test_intent_keyword_verschieben():
    assert (
        gem._detect_intent_keywords("Re: Termin", "koennen wir verschieben?")
        == gem.INTENT_TERMIN_VERSCHIEBEN
    )


def test_intent_keyword_overlap_verschiebung_wins():
    """Wenn sowohl Storno- als auch Verschiebungs-Keywords matchen,
    gewinnt Verschiebung — gleiche Heuristik wie im Brevo-Prompt-Komm."""
    assert (
        gem._detect_intent_keywords(
            "absagen", "muss absagen, koennen wir verschieben?"
        ) == gem.INTENT_TERMIN_VERSCHIEBEN
    )


def test_intent_keyword_no_match():
    assert gem._detect_intent_keywords("Hallo", "Preis-Anfrage") is None


# =====================================================================
# 2. PUSH-HELPERS rendern korrekt
# =====================================================================

@pytest.mark.asyncio
async def test_push_new_anfrage_renders_outlook_link(push_capture):
    tenant = _make_tenant()
    ok = await mail_pipeline.push_tenant_new_anfrage_notification(
        tenant, sender_email="x@y.de", sender_name="Max",
        subject="Anfrage", body_preview="Test-Anliegen",
        web_link="https://outlook.office.com/abc",
        anfrage_url="https://hallo.gewerbeagent.de/anfrage/abc",
    )
    assert ok is True
    text = push_capture[0]["text"]
    assert "Neue Kundenanfrage" in text
    assert "Max" in text
    assert "Im Outlook oeffnen" in text
    assert "https://outlook.office.com/abc" in text
    assert "Formular-Link" in text


@pytest.mark.asyncio
async def test_push_followup_uses_assigned_employee(push_capture):
    tenant = _make_tenant()
    emp_id = uuid.uuid4()
    conv = _make_conv(assigned_employee_id=emp_id)
    await mail_pipeline.push_tenant_followup_mail(
        tenant, sender_email="x@y.de", sender_name="Max",
        subject="Re: Termin", body_preview="passt nicht", conv=conv,
    )
    assert push_capture[0]["employee_id"] == emp_id


@pytest.mark.asyncio
async def test_push_intent_event_with_detail(push_capture):
    tenant = _make_tenant()
    await mail_pipeline.push_tenant_intent_event(
        tenant, sender_email="x@y.de", sender_name="Max",
        subject="Storno", body_preview="muss absagen",
        label="Storno verarbeitet", detail="1 Termin storniert",
    )
    text = push_capture[0]["text"]
    assert "Storno verarbeitet" in text
    assert "1 Termin storniert" in text


@pytest.mark.asyncio
async def test_push_bounce_notification_alarm_format(push_capture):
    tenant = _make_tenant()
    conv = _make_conv(
        kunde_email="falsche@gmx.de",
        last_subject="Re: Anfrage Kuechenmontage",
    )
    await mail_pipeline.push_tenant_bounce_notification(
        tenant, conv=conv,
        bounce_sender="mailer-daemon@gmx.de",
        bounce_reason="User unknown (550 5.1.1)",
    )
    text = push_capture[0]["text"]
    assert "fehlgeschlagen" in text
    assert "falsche@gmx.de" in text
    assert "User unknown" in text


# =====================================================================
# 3. STORNO/VERSCHIEBUNG/RECHNUNG-Handler direkt aufgerufen
# =====================================================================

@pytest.mark.asyncio
async def test_storno_handler_cancels_events_and_sends_confirmation(
    monkeypatch, push_capture, mail_send_capture, persistence_capture,
    kalender_capture,
):
    """Storno-Intent: find_events liefert 2 Termine → beide werden
    gecancelt + Bestaetigungs-Mail + Push + state STORNIERT."""
    kalender_capture["kalender"] = _FakeKalender(
        find_events_result={
            "erfolg": True, "anzahl": 2,
            "termine": [
                {"event_id": "evt-1"},
                {"event_id": "evt-2"},
            ],
        },
    )
    tenant = _make_tenant()
    result = await microsoft_inbox._handle_storno_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="msg-1",
        sender_email="kunde@example.de", sender_name="Anna",
        subject="Termin absagen", body_preview="muss leider absagen",
        existing_conv=None, employee_id=None,
        ms_conversation_id="ms-conv-99",
        classification="RELEVANT_KUNDE", confidence="high",
        reason="storno", categories=[],
    )
    assert result["success"] is True
    assert result["intent"] == "termin_stornieren"
    assert result["cancelled_count"] == 2
    # 2 cancel-calls
    cancel_calls = [
        c for c in kalender_capture["kalender"].calls
        if c[0] == "cancel_appointment"
    ]
    assert len(cancel_calls) == 2
    # Mail wurde gesendet
    assert len(mail_send_capture) == 1
    sent = mail_send_capture[0]
    assert sent["to_email"] == "kunde@example.de"
    assert "storniert" in sent["body_html"]
    # state wurde auf STORNIERT gesetzt
    assert len(persistence_capture["set_conversation_state"]) == 1
    assert (
        persistence_capture["set_conversation_state"][0]["state"]
        == "storniert"
    )
    # Push raus
    assert len(push_capture) == 1
    assert "Storno verarbeitet" in push_capture[0]["text"]


@pytest.mark.asyncio
async def test_storno_handler_zero_cancelled_still_sends_rueckfrage(
    monkeypatch, push_capture, mail_send_capture, persistence_capture,
    kalender_capture,
):
    """find_events liefert nichts → trotzdem hoefliche Rueckfrage-Mail
    + state STORNIERT (Kunde wollte stornieren, also Vorgang zu)."""
    # default kalender liefert anzahl=0
    tenant = _make_tenant()
    result = await microsoft_inbox._handle_storno_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="msg-1",
        sender_email="kunde@example.de", sender_name="Anna",
        subject="Storno", body_preview="absagen",
        existing_conv=None, employee_id=None,
        ms_conversation_id=None,
        classification="RELEVANT_KUNDE", confidence="high",
        reason="storno", categories=[],
    )
    assert result["cancelled_count"] == 0
    assert len(mail_send_capture) == 1
    sent = mail_send_capture[0]
    # zero-cancelled-Pfad: hoefliche Rueckfrage
    assert "keinen bestehenden Termin" in sent["body_html"]
    # Push enthaelt Hinweis
    assert "kein passender Termin gefunden" in push_capture[0]["text"]


@pytest.mark.asyncio
async def test_verschiebung_handler_finds_event_sends_rueckfrage(
    monkeypatch, push_capture, mail_send_capture, persistence_capture,
    kalender_capture,
):
    """Verschiebungs-Intent: find_events liefert Termin → Rueckfrage-Mail
    + Push, KEIN Cancel."""
    kalender_capture["kalender"] = _FakeKalender(
        find_events_result={
            "erfolg": True, "anzahl": 1,
            "termine": [{
                "event_id": "evt-1", "datum": "23.05.2026", "uhrzeit": "14:00",
            }],
        },
    )
    tenant = _make_tenant()
    result = await microsoft_inbox._handle_verschiebung_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="msg-1",
        sender_email="kunde@example.de", sender_name="Maria",
        subject="Verschiebung", body_preview="koennen wir verschieben",
        existing_conv=None, employee_id=None,
        ms_conversation_id=None,
        classification="RELEVANT_KUNDE", confidence="high",
        reason="verschiebung", categories=[],
    )
    assert result["success"] is True
    assert result["intent"] == "termin_verschieben"
    assert result["found_count"] == 1
    # KEIN cancel
    cancel_calls = [
        c for c in kalender_capture["kalender"].calls
        if c[0] == "cancel_appointment"
    ]
    assert len(cancel_calls) == 0
    # Mail mit Datum
    assert "23.05.2026" in mail_send_capture[0]["body_html"]
    # State bleibt unveraendert (kein set_state-call)
    assert len(persistence_capture["set_conversation_state"]) == 0


@pytest.mark.asyncio
async def test_rechnung_handler_pushes_no_auto_reply(
    monkeypatch, push_capture, mail_send_capture, persistence_capture,
):
    """Rechnungsanfrage: KEINE Auto-Antwort, nur Telegram-Push.
    Outlook-Kategorie wird gesetzt (impliziert ueber das Aufrufer-
    Verhalten _mark_and_categorize_message)."""
    tenant = _make_tenant()
    # _mark_and_categorize_message ruft set_message_categories + mark_as_read
    monkeypatch.setattr(
        microsoft_inbox, "set_message_categories", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        microsoft_inbox, "mark_as_read", AsyncMock(return_value=True),
    )
    result = await microsoft_inbox._handle_rechnungsanfrage_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="msg-r",
        sender_email="kunde@example.de", sender_name="Anna",
        subject="Frage zur Rechnung 2024-001",
        body_preview="ich habe eine Frage zur Rechnung",
        existing_conv=None, employee_id=None,
        ms_conversation_id=None,
        classification="RELEVANT_KUNDE", confidence="high",
        reason="rechnung", categories=[],
    )
    assert result["intent"] == "rechnungsanfrage"
    assert len(mail_send_capture) == 0  # KEINE Auto-Antwort
    assert len(push_capture) == 1
    assert "Rechnungsanfrage" in push_capture[0]["text"]
    assert "manuell pruefen" in push_capture[0]["text"]


# =====================================================================
# 4. classify_mail_subject Intent + Keyword-Backup
# =====================================================================

@pytest.mark.asyncio
async def test_classify_intent_keyword_overrides_gemini(monkeypatch):
    """Wenn Gemini neu_anfrage sagt, Body aber 'muss leider absagen'
    enthaelt, override auf termin_stornieren."""
    fake_response = (
        '{"classification": "RELEVANT_KUNDE", "intent": "neu_anfrage", '
        '"confidence": "high", "reason": "Test"}'
    )
    monkeypatch.setattr(
        gem, "call_gemini", AsyncMock(return_value=fake_response),
    )
    result = await gem.classify_mail_subject(
        subject="Termin", sender="x@y.de",
        body_preview="muss leider absagen, kann doch nicht",
    )
    assert result["intent"] == gem.INTENT_TERMIN_STORNIEREN


@pytest.mark.asyncio
async def test_classify_intent_default_when_missing_from_gemini(monkeypatch):
    """Alte Gemini-Antwort ohne intent-Feld → default per classification."""
    fake_response = (
        '{"classification": "RELEVANT_KUNDE", "confidence": "high", '
        '"reason": "Test"}'
    )
    monkeypatch.setattr(
        gem, "call_gemini", AsyncMock(return_value=fake_response),
    )
    result = await gem.classify_mail_subject(
        subject="Preise?", sender="x@y.de", body_preview="Was kostet?",
    )
    # body hat keine keywords → default fuer RELEVANT_KUNDE = neu_anfrage
    assert result["intent"] == gem.INTENT_NEU_ANFRAGE


@pytest.mark.asyncio
async def test_classify_failure_pathway_still_yields_keyword_intent(monkeypatch):
    """Gemini crasht → keyword-backup detected immer noch storno-intent."""
    async def boom(*a, **kw):
        raise RuntimeError("Gemini ist tot")
    monkeypatch.setattr(gem, "call_gemini", boom)
    result = await gem.classify_mail_subject(
        subject="absagen", sender="x@y.de",
        body_preview="muss leider absagen",
    )
    assert result["classification"] == "UNSICHER"
    assert result["intent"] == gem.INTENT_TERMIN_STORNIEREN


@pytest.mark.asyncio
async def test_classify_unsicher_gets_sonstiges_intent(monkeypatch):
    """NICHT_RELEVANT/UNSICHER/PRIVAT → default-intent=sonstiges,
    keyword-Override greift nur fuer RELEVANT_KUNDE."""
    fake_response = (
        '{"classification": "NICHT_RELEVANT", "confidence": "high", '
        '"reason": "Werbung"}'
    )
    monkeypatch.setattr(
        gem, "call_gemini", AsyncMock(return_value=fake_response),
    )
    result = await gem.classify_mail_subject(
        subject="Newsletter", sender="news@x.de",
        body_preview="absagen wollten Sie nie unseren Newsletter",
    )
    # Trotz keyword "absagen" im body: nur RELEVANT_KUNDE wird overridden
    assert result["classification"] == "NICHT_RELEVANT"
    assert result["intent"] == gem.INTENT_SONSTIGES


# =====================================================================
# 5. generate_anfrage_reply Regression-Tests (kein "Daniel" hardcode)
# =====================================================================

@pytest.mark.asyncio
async def test_generate_reply_requires_owner_first_name():
    """tenant_owner_first_name ist Pflicht-Parameter — kein Default mehr."""
    with pytest.raises(TypeError):
        await gem.generate_anfrage_reply(
            subject="x", sender_name="y", sender_email="x@y.de",
            body="b", form_url="u",
            tenant_company="Dietz",
            # tenant_owner_first_name fehlt absichtlich
        )


@pytest.mark.asyncio
async def test_generate_reply_none_owner_uses_team_signer(monkeypatch):
    """Bei tenant_owner_first_name=None signiert Q mit Team-String,
    NICHT mit 'Daniel'."""
    captured: dict = {}

    async def fake_call_gemini(*, prompt, **kw):
        captured["prompt"] = prompt
        return "Hallo Sven, danke fuer deine Anfrage. Gruss, Ihr Team"

    monkeypatch.setattr(gem, "call_gemini", fake_call_gemini)
    result = await gem.generate_anfrage_reply(
        subject="Anfrage", sender_name="Sven", sender_email="x@y.de",
        body="Hi", form_url="u",
        tenant_company="Tischlerei Dietz",
        tenant_owner_first_name=None,
    )
    assert "Daniel" not in captured["prompt"]
    assert "Ihr Team von Tischlerei Dietz" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_reply_with_name_uses_personal_signer(monkeypatch):
    captured: dict = {}

    async def fake_call_gemini(*, prompt, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(gem, "call_gemini", fake_call_gemini)
    await gem.generate_anfrage_reply(
        subject="x", sender_name="y", sender_email="x@y.de",
        body="b", form_url="u",
        tenant_company="Dietz",
        tenant_owner_first_name="Daniel",
    )
    assert "Daniel (via Q)" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_reply_no_sprach_lock_in_prompt(monkeypatch):
    """REPLY_PROMPT sagt nicht mehr 'Schreib auf Deutsch'."""
    captured: dict = {}

    async def fake_call_gemini(*, prompt, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(gem, "call_gemini", fake_call_gemini)
    await gem.generate_anfrage_reply(
        subject="x", sender_name="y", sender_email="x@y.de",
        body="b", form_url="u",
        tenant_company="Dietz", tenant_owner_first_name="X",
    )
    assert "Schreib auf Deutsch" not in captured["prompt"]
    assert "in derselben Sprache" in captured["prompt"]


# =====================================================================
# 6. Regression: keine Brevo-Imports im neuen Code
# =====================================================================

def test_no_brevo_imports_in_mail_pipeline():
    """mail_pipeline.py darf nicht von core.integrations.brevo oder
    plugins.mail_intake importieren (sonst Coupling-Kreis)."""
    import inspect
    src = inspect.getsource(mail_pipeline)
    assert "core.integrations.brevo" not in src
    assert "plugins.mail_intake" not in src
    assert "BrevoMailer" not in src


def test_no_brevo_imports_in_microsoft_inbox():
    """microsoft_inbox.py darf weder von brevo noch von mail_intake importieren."""
    import inspect
    src = inspect.getsource(microsoft_inbox)
    assert "core.integrations.brevo" not in src
    assert "plugins.mail_intake" not in src


# =====================================================================
# 7. Conversation-Lookup-Logik (mit echtem mail_pipeline, ohne DB-Patch)
# =====================================================================
# Diese Tests laufen NUR wenn AsyncSessionLocal gepatcht ist — sonst
# brauchen sie eine echte Postgres. Wir mocken AsyncSessionLocal:


@pytest.fixture
def db_session_mock(monkeypatch):
    """Sehr leichter Mock fuer AsyncSessionLocal — gibt eine Session
    zurueck, die Selects + Commits/Refreshes/Expunges abfaengt."""
    from contextlib import asynccontextmanager

    state: dict = {
        "added": [],
        "select_results": [],  # Tests koennen das vorher fuellen
    }

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        async def execute(self, stmt):
            # einfach: liefere den naechsten queued result
            value = state["select_results"].pop(0) if state["select_results"] else None
            return _Result(value)

        def add(self, obj):
            state["added"].append(obj)

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

        def expunge(self, obj):
            pass

    @asynccontextmanager
    async def fake_session_ctx():
        yield _FakeSession()

    monkeypatch.setattr(mail_pipeline, "AsyncSessionLocal", fake_session_ctx)
    return state


@pytest.mark.asyncio
async def test_find_open_conversation_excludes_closed(db_session_mock):
    """CLOSED-Konversationen werden nicht zurueckgegeben — find_open_conv
    wird mit None geprueft + queries kommen alle ohne Treffer zurueck."""
    db_session_mock["select_results"] = [None, None, None]
    result = await mail_pipeline.find_open_conversation(
        tenant_id=uuid.uuid4(), sender_email="x@y.de",
        microsoft_conversation_id="ms-1", in_reply_to="<r>",
    )
    assert result is None


@pytest.mark.asyncio
async def test_find_open_conversation_prefers_ms_conv_id(db_session_mock):
    """Wenn ms_conv_id-Lookup hit: wird das zurueckgegeben, andere
    Lookups (in_reply_to / sender_email) laufen nicht."""
    expected = _make_conv()
    db_session_mock["select_results"] = [expected]
    result = await mail_pipeline.find_open_conversation(
        tenant_id=uuid.uuid4(), sender_email="x@y.de",
        microsoft_conversation_id="ms-1", in_reply_to="<r>",
    )
    assert result is expected


# =====================================================================
# 8. Idempotenz / Buchungs-Bestaetigung
# =====================================================================

def test_create_conversation_signature_supports_voice_booking_params():
    """create_conversation muss gcal_event_id + termin_datum + state
    als Parameter akzeptieren (E.3 Threading)."""
    import inspect
    sig = inspect.signature(mail_pipeline.create_conversation)
    assert "gcal_event_id" in sig.parameters
    assert "termin_datum" in sig.parameters
    assert "state" in sig.parameters


def test_send_buche_confirmation_signature_has_employee_id():
    """send_buche_confirmation muss employee_id akzeptieren damit
    der MA-Account die Mail verschickt (Spec E.1)."""
    import inspect
    sig = inspect.signature(mail_pipeline.send_buche_confirmation)
    assert "employee_id" in sig.parameters
