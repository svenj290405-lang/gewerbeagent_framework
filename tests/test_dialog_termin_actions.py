"""Tests fuer den Phase-2 Termin-Action-Switch in process_relevant_kunde_mail.

Deckt die drei neuen next_action-Pfade ab:
  - PROPOSE_SLOTS: kalender.find_free_slots -> proposed_slots persistieren
    + Slot-Box im Body
  - BOOK_SLOT: previous_proposed_slots[idx] -> kalender.book_appointment
    -> state=BOOKED + Bestaetigungs-Box
  - CANCEL_TERMIN: cancel_kunde_termine -> state=STORNIERT + Storno-Box

Externe Calls (Microsoft Graph fetch_full_message/send_tracked_mail/
mark_as_read/move_to_gewerbeagent, kalender-Plugin, DB-Helper) sind
gemockt. Tests verifizieren Verzweigungslogik + Persistenz + Body-
Inhalt, nicht die unterliegenden Services.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.ai.gemini as gem
from core.integrations import mail_pipeline
from core.integrations import microsoft_inbox


# =====================================================================
# Shared helpers — Test-Doubles
# =====================================================================

def _make_tenant(slug="demo"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        company_name="Tischlerei Dietz",
        branche="Tischler",
        contact_name="Daniel Dietz",
        contact_email="info@dietz.de",
        contact_phone="+49 211 12345",
    )


def _make_conv(*, tenant_id, state, kunde_email="sven@example.de",
               proposed_slots=None, last_user_message=None,
               last_q_reply=None, drive_folder_url=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kunde_email=kunde_email,
        kunde_name="Sven Kunde",
        state=state,
        last_subject="Re: Termin",
        last_message_id="<q-1@dietz.de>",
        gcal_event_id=None,
        assigned_employee_id=None,
        classification_reason=None,
        last_user_message=last_user_message,
        last_q_reply=last_q_reply,
        proposed_slots=proposed_slots,
        drive_folder_url=drive_folder_url,
    )


class _FakeKalender:
    def __init__(self, *, find_free_slots_result=None,
                 book_result=None, find_events_result=None,
                 cancel_result=None):
        self.calls: list[tuple[str, dict]] = []
        self.find_free_slots_result = find_free_slots_result or {
            "erfolg": True, "slots": [], "anzahl": 0,
        }
        self.book_result = book_result or {
            "erfolg": True, "event_id": "ev-1", "nachricht": "ok",
        }
        self.find_events_result = find_events_result or {
            "erfolg": True, "termine": [],
        }
        self.cancel_result = cancel_result or {"erfolg": True}

    async def on_webhook(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        if endpoint == "find_free_slots":
            return self.find_free_slots_result
        if endpoint == "book_appointment":
            return self.book_result
        if endpoint == "find_events":
            return self.find_events_result
        if endpoint == "cancel_appointment":
            return self.cancel_result
        return {}


# =====================================================================
# pytest-fixtures: Komplette I/O-Isolation fuer
# process_relevant_kunde_mail
# =====================================================================

@pytest.fixture
def captured_state():
    """Sammelt alle Persistenz- und Send-Calls fuer Assertions."""
    return {
        "create_conversation": [],
        "set_conversation_state": [],
        "set_proposed_slots": [],
        "record_inbound": [],
        "record_outbound_q_reply": [],
        "send_tracked_mail": [],
        "telegram_pushes": [],
    }


@pytest.fixture
def pipeline_mocks(monkeypatch, captured_state):
    """Patcht alle externen Calls von process_relevant_kunde_mail.
    Yieldet ein dict mit dem aktiven FakeKalender + Tenant +
    captured_state — Tests koennen das vor dem Call manipulieren.
    """
    tenant = _make_tenant()
    kalender = _FakeKalender()
    holder = {
        "tenant": tenant,
        "kalender": kalender,
        "fetch_full_response": {
            "subject": "Anfrage Termin",
            "from": {
                "emailAddress": {
                    "address": "sven@example.de",
                    "name": "Sven Kunde",
                },
            },
            "body": {"content": "Brauche einen Termin", "contentType": "text"},
            "bodyPreview": "Brauche einen Termin",
            "internetMessageId": "<inbound-1@example.de>",
            "hasAttachments": False,
            "webLink": "https://outlook.office.com/x",
        },
        "existing_conv": None,
        "captured": captured_state,
    }

    # 1. Microsoft Graph
    async def fake_fetch(tenant_id, message_id, **kw):
        return holder["fetch_full_response"]
    monkeypatch.setattr(microsoft_inbox, "fetch_full_message", fake_fetch)

    async def fake_send_tracked(*, tenant_id, to_email, subject, body_html,
                                cc=None, attachments=None, employee_id=None):
        captured_state["send_tracked_mail"].append({
            "to_email": to_email, "subject": subject,
            "body_html": body_html,
        })
        return {
            "success": True,
            "internet_message_id": f"<sent-{len(captured_state['send_tracked_mail'])}@dietz.de>",
            "conversation_id": f"ms-conv-{len(captured_state['send_tracked_mail'])}",
            "error": None,
        }
    import core.integrations.microsoft as ms_mod
    monkeypatch.setattr(ms_mod, "send_tracked_mail", fake_send_tracked)

    monkeypatch.setattr(
        microsoft_inbox, "mark_as_read", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        microsoft_inbox, "move_to_gewerbeagent", AsyncMock(return_value=True),
    )

    # 2. Tenant aus DB — der echte Code macht ein AsyncSessionLocal()-
    # Block; wir kapern den ganzen Block indem wir AsyncSessionLocal
    # durch einen Async-Ctx ersetzen, der einen Session-Mock mit
    # passender execute()-Antwort liefert.
    class _FakeResult:
        def __init__(self, val):
            self._val = val

        def scalar_one_or_none(self):
            return self._val

        def scalars(self):
            class _S:
                def __init__(self, v):
                    self._v = v

                def all(self):
                    return self._v
            return _S([])

    class _FakeSession:
        async def execute(self, stmt):
            # Tenant-Lookup ist der erste Query; alle weiteren liefern leere
            # Listen (TenantKnowledge etc).
            if not hasattr(self, "_first"):
                self._first = True
                return _FakeResult(tenant)
            return _FakeResult(None)

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

        def expunge(self, obj):
            pass

        def add(self, obj):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def fake_sessionlocal():
        return _FakeSession()

    import core.database as db_mod
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake_sessionlocal)
    # Plus die direkten Imports der Helfer in mail_pipeline + inbox.
    monkeypatch.setattr(mail_pipeline, "AsyncSessionLocal", fake_sessionlocal)
    monkeypatch.setattr(microsoft_inbox, "AsyncSessionLocal", fake_sessionlocal)

    # 3. kalender-Plugin
    async def fake_get_plugin(tenant_slug, plugin_name):
        if plugin_name == "kalender" and tenant_slug == tenant.slug:
            return kalender
        return None
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", fake_get_plugin)

    # 3a. Anfrage-Status: default "submitted" damit Termin-Aktionen
    # nicht vom Vor-Gate blockiert werden. Tests die "Formular noch
    # nicht ausgefuellt" testen wollen, ueberschreiben das im Holder.
    holder["anfrage_status"] = {
        "status": "submitted",
        "sent_at": None,
        "submitted_at": None,
        "antworten": {"anliegen": "Mock-Anliegen"},
        "anliegen": "Mock-Anliegen",
    }

    async def fake_anfrage_status(tenant_id, kunde_email, max_age_days=30):
        return holder.get("anfrage_status")

    import core.integrations.anfrage_forms as af
    monkeypatch.setattr(
        af, "get_latest_anfrage_status_for_email", fake_anfrage_status,
    )

    # Token-Erstellung mocken (sonst crasht FakeSession.refresh)
    async def fake_create_token(*, tenant_id, kunde_email, kunde_name,
                                anfrage_typ, original_subject,
                                original_message_id, valid_days=14):
        return SimpleNamespace(
            id=uuid.uuid4(), token="mock-token-12345",
            tenant_id=tenant_id, kunde_email=kunde_email,
        )
    monkeypatch.setattr(af, "create_anfrage_token", fake_create_token)
    # Auch im Pipeline-Modul-Namespace ueberschreiben (Import via from..import)
    monkeypatch.setattr(
        microsoft_inbox, "create_anfrage_token", fake_create_token,
        raising=False,
    )

    # 4. mail_pipeline-Helper (Persistenz)
    async def fake_create_conv(tenant_id, sender_email, sender_name,
                               subject, **kw):
        conv = _make_conv(tenant_id=tenant_id, state=kw.get("state", "dialog"),
                          kunde_email=sender_email)
        # gcal_event_id & termin_datum aus kwargs uebernehmen damit Tests
        # die Buchungs-Persistenz pruefen koennen
        if "gcal_event_id" in kw:
            conv.gcal_event_id = kw["gcal_event_id"]
        if "termin_datum" in kw:
            conv.termin_datum = kw["termin_datum"]
        captured_state["create_conversation"].append({
            "tenant_id": tenant_id, "sender_email": sender_email,
            "sender_name": sender_name, "subject": subject, **kw,
        })
        return conv
    monkeypatch.setattr(mail_pipeline, "create_conversation", fake_create_conv)
    monkeypatch.setattr(
        microsoft_inbox.__name__ and microsoft_inbox or mail_pipeline,
        "create_conversation", fake_create_conv, raising=False,
    )

    async def fake_set_state(conv_id, state):
        captured_state["set_conversation_state"].append(
            {"conv_id": conv_id, "state": state}
        )
    monkeypatch.setattr(mail_pipeline, "set_conversation_state", fake_set_state)

    async def fake_set_slots(conv_id, slots, *, state=None):
        captured_state["set_proposed_slots"].append(
            {"conv_id": conv_id, "slots": slots, "state": state}
        )
    monkeypatch.setattr(mail_pipeline, "set_proposed_slots", fake_set_slots)

    async def fake_record_inbound(conv_id, **kw):
        captured_state["record_inbound"].append({"conv_id": conv_id, **kw})
    monkeypatch.setattr(mail_pipeline, "record_inbound", fake_record_inbound)

    async def fake_record_outbound(conv_id, **kw):
        captured_state["record_outbound_q_reply"].append(
            {"conv_id": conv_id, **kw}
        )
    monkeypatch.setattr(
        mail_pipeline, "record_outbound_q_reply", fake_record_outbound,
    )

    async def fake_cancel_termine(tenant_arg, sender_email, existing_conv):
        # Wird im CANCEL_TERMIN-Pfad gerufen. Default: 1 stornierter Termin.
        captured_state.setdefault("cancel_kunde_termine", []).append({
            "kunde_email": sender_email,
        })
        return holder.get("cancelled_event_ids", ["ev-cancelled-1"])
    monkeypatch.setattr(mail_pipeline, "cancel_kunde_termine", fake_cancel_termine)

    async def fake_push_intent(*, tenant, sender_email, sender_name,
                               subject, body_preview, label, detail,
                               employee_id=None):
        captured_state["telegram_pushes"].append({
            "label": label, "detail": detail, "sender_email": sender_email,
        })
    monkeypatch.setattr(
        mail_pipeline, "push_tenant_intent_event", fake_push_intent,
    )

    return holder


@pytest.fixture
def dialog_capture(monkeypatch):
    """Erlaubt einem Test die Dialog-Antwort von Q vorzugeben."""
    state = {"response": None, "calls": []}

    async def fake_dialog(**kwargs):
        state["calls"].append(kwargs)
        resp = state["response"]
        if isinstance(resp, dict):
            # Neuer Flow: Q extrahiert vollen Namen + Telefonnummer aus der
            # Mail. Default setzen, damit der Kontakt-Pflicht-Gate die
            # Termin-Pfade nicht blockt. Tests die das Gate pruefen, setzen
            # kunde_voller_name/kunde_telefon explizit (auch "" moeglich).
            resp = dict(resp)
            resp.setdefault("kunde_voller_name", "Sven Kunde")
            resp.setdefault("kunde_telefon", "0151 23456789")
        return resp

    monkeypatch.setattr(gem, "handle_kunde_mail_dialog", fake_dialog)
    return state


# =====================================================================
# PROPOSE_SLOTS
# =====================================================================

@pytest.mark.asyncio
async def test_propose_slots_calls_kalender_and_persists_slots(
    pipeline_mocks, dialog_capture,
):
    """Q waehlt PROPOSE_SLOTS -> Pipeline ruft find_free_slots, persistiert
    Slots, rendert Slot-Box im Body, state=PROPOSING_SLOTS."""
    pipeline_mocks["kalender"].find_free_slots_result = {
        "erfolg": True,
        "slots": [
            {"datum": "22.05.2026", "uhrzeit": "14:00",
             "dauer_minuten": 90, "employee_id": None},
            {"datum": "23.05.2026", "uhrzeit": "09:00",
             "dauer_minuten": 90, "employee_id": None},
        ],
    }
    dialog_capture["response"] = {
        "reply_text": "hier sind zwei Vorschlaege.",
        "next_action": "PROPOSE_SLOTS",
        "anrede_form": "DU",
        "wunsch_datum": "22.05.2026",
        "wunsch_uhrzeit": "14:00",
        "chosen_slot_index": None,
        "reason": "Kunde fragt nach Terminen",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-1",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["sent"] is True
    assert res["next_action"] == "PROPOSE_SLOTS"
    # find_free_slots wurde mit dem Wunsch-Anker gerufen
    fs_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "find_free_slots"
    )
    assert fs_call[1]["datum"] == "22.05.2026"
    assert fs_call[1]["uhrzeit"] == "14:00"
    # set_proposed_slots wurde mit den Slots aufgerufen
    sp = pipeline_mocks["captured"]["set_proposed_slots"]
    assert len(sp) == 1
    assert len(sp[0]["slots"]) == 2
    # Conv-Anlage mit state=proposing_slots
    cc = pipeline_mocks["captured"]["create_conversation"]
    assert cc[0]["state"] == "proposing_slots"
    # Body enthaelt die Slot-Box
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Mögliche Termine" in body
    assert "22.05.2026" in body
    # Push-Politik: Slot-Vorschlaege pingen NICHT mehr (nur Buchung/Storno)
    pushes = pipeline_mocks["captured"]["telegram_pushes"]
    assert not any("Slots vorgeschlagen" in p["label"] for p in pushes)


@pytest.mark.asyncio
async def test_propose_slots_with_empty_calendar_degrades_to_ask_more(
    pipeline_mocks, dialog_capture,
):
    """Wenn kalender 0 Slots liefert -> Pipeline degradiert auf
    ASK_MORE statt eine leere Vorschlagsbox zu schicken."""
    pipeline_mocks["kalender"].find_free_slots_result = {
        "erfolg": True, "slots": [],
    }
    dialog_capture["response"] = {
        "reply_text": "hier sind Vorschlaege.",
        "next_action": "PROPOSE_SLOTS",
        "anrede_form": "DU",
        "wunsch_datum": "22.05.2026",
        "wunsch_uhrzeit": "14:00",
        "chosen_slot_index": None,
        "reason": "kunde fragt",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-1",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "ASK_MORE"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Mögliche Termine" not in body
    # state = dialog (NICHT proposing_slots)
    cc = pipeline_mocks["captured"]["create_conversation"]
    assert cc[0]["state"] == "dialog"


# =====================================================================
# BOOK_SLOT
# =====================================================================

@pytest.mark.asyncio
async def test_book_slot_uses_previous_proposed_slots(
    pipeline_mocks, dialog_capture,
):
    """Q waehlt BOOK_SLOT mit chosen_slot_index=1 -> Pipeline holt den
    2. Slot aus existing_conv.proposed_slots und ruft book_appointment."""
    tenant_id = pipeline_mocks["tenant"].id
    existing = _make_conv(
        tenant_id=tenant_id,
        state="proposing_slots",
        proposed_slots=[
            {"datum": "22.05.2026", "uhrzeit": "14:00",
             "dauer_minuten": 90, "employee_id": None, "wochentag": "Do"},
            {"datum": "23.05.2026", "uhrzeit": "09:00",
             "dauer_minuten": 90, "employee_id": None, "wochentag": "Fr"},
        ],
    )
    pipeline_mocks["kalender"].book_result = {
        "erfolg": True, "event_id": "ev-booked-1",
    }
    dialog_capture["response"] = {
        "reply_text": "perfekt, ist gebucht.",
        "next_action": "BOOK_SLOT",
        "anrede_form": "DU",
        "wunsch_datum": None,
        "wunsch_uhrzeit": None,
        "chosen_slot_index": 1,
        "reason": "kunde bestaetigt slot 2",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-2",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "folge",
        },
        existing_conv=existing,
    )

    assert res["next_action"] == "BOOK_SLOT"
    # book_appointment wurde mit Slot 1 gerufen
    book_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "book_appointment"
    )
    assert book_call[1]["datum"] == "23.05.2026"
    assert book_call[1]["uhrzeit"] == "09:00"
    # state forwards -> BOOKED
    set_calls = pipeline_mocks["captured"]["set_conversation_state"]
    assert any(c["state"] == "booked" for c in set_calls)
    # Slots geloescht
    sp = pipeline_mocks["captured"]["set_proposed_slots"]
    assert sp and sp[-1]["slots"] == []
    # Body enthaelt Termin-Bestaetigung
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Termin bestätigt" in body
    assert "23.05.2026" in body
    # Push-Politik: die Buchung meldet der Kalender-Handler ("Neuer
    # Termin"), NICHT der Mail-Dialog -> hier kein Intent-Push.
    pushes = pipeline_mocks["captured"]["telegram_pushes"]
    assert not any("gebucht" in p["label"] for p in pushes)


@pytest.mark.asyncio
async def test_book_slot_passes_drive_url_to_calendar(
    pipeline_mocks, dialog_capture,
):
    """Wenn die Konversation eine drive_folder_url hat (Formular wurde
    ausgefuellt + ins Drive archiviert), traegt die Pipeline den Link
    NACH der Buchung klickbar ins Event ein (attach_drive_url) — nicht
    mehr ueber book_payload (sonst in Outlook nur nackter Text)."""
    tenant_id = pipeline_mocks["tenant"].id
    existing = _make_conv(
        tenant_id=tenant_id,
        state="proposing_slots",
        proposed_slots=[
            {"datum": "22.05.2026", "uhrzeit": "14:00",
             "dauer_minuten": 90, "employee_id": None, "wochentag": "Do"},
        ],
        drive_folder_url="https://drive.google.com/drive/folders/abc123",
    )
    pipeline_mocks["kalender"].book_result = {
        "erfolg": True, "event_id": "ev-drive-1",
    }
    dialog_capture["response"] = {
        "reply_text": "gebucht.",
        "next_action": "BOOK_SLOT",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": 0,
        "direct_datum": None, "direct_uhrzeit": None,
        "reason": "kunde bestaetigt",
    }

    await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-drive",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "folge",
        },
        existing_conv=existing,
    )

    # Drive-Link wird NACH der Buchung via attach_drive_url eingetragen
    attach_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "attach_drive_url"
    )
    assert attach_call[1].get("drive_url") == (
        "https://drive.google.com/drive/folders/abc123"
    )
    assert attach_call[1].get("event_id") == "ev-drive-1"
    # ... und NICHT mehr ins book_payload gepackt
    book_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "book_appointment"
    )
    assert "drive_url" not in book_call[1]


@pytest.mark.asyncio
async def test_book_slot_with_invalid_index_falls_back_to_propose(
    pipeline_mocks, dialog_capture,
):
    """Wenn Q BOOK_SLOT mit out-of-range index liefert, ueberschreibt
    handle_kunde_mail_dialog das schon zu PROPOSE_SLOTS. Hier testen
    wir die zweite Verteidigungslinie: ein index der bei der
    Pipeline-Ankunft trotzdem nicht aufloest -> degradiert auf
    PROPOSE_SLOTS-Antwort (Rueckfrage statt Buchung)."""
    tenant_id = pipeline_mocks["tenant"].id
    existing = _make_conv(
        tenant_id=tenant_id,
        state="proposing_slots",
        proposed_slots=[
            {"datum": "22.05.2026", "uhrzeit": "14:00"},
        ],
    )
    dialog_capture["response"] = {
        "reply_text": "ok ich buche.",
        "next_action": "BOOK_SLOT",
        "anrede_form": "DU",
        "wunsch_datum": None,
        "wunsch_uhrzeit": None,
        "chosen_slot_index": 5,  # out of range
        "reason": "test",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-3",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "folge",
        },
        existing_conv=existing,
    )

    assert res["next_action"] == "PROPOSE_SLOTS"
    # book_appointment wurde NICHT gerufen
    assert not any(
        c[0] == "book_appointment"
        for c in pipeline_mocks["kalender"].calls
    )
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Termin bestätigt" not in body


@pytest.mark.asyncio
async def test_book_slot_conflict_offers_alternatives(
    pipeline_mocks, dialog_capture,
):
    """Wenn book_appointment Konflikt liefert (Slot inzwischen weg) ->
    Pipeline schlaegt Alternativen rund um den Wunsch vor statt ratloser
    Rueckfrage."""
    tenant_id = pipeline_mocks["tenant"].id
    existing = _make_conv(
        tenant_id=tenant_id,
        state="proposing_slots",
        proposed_slots=[
            {"datum": "22.05.2026", "uhrzeit": "14:00",
             "dauer_minuten": 90, "employee_id": None},
        ],
    )
    pipeline_mocks["kalender"].book_result = {
        "erfolg": False, "konflikt": True,
        "nachricht": "Slot ist belegt",
    }
    pipeline_mocks["kalender"].find_free_slots_result = {
        "erfolg": True,
        "slots": [
            {"datum": "22.05.2026", "uhrzeit": "15:30",
             "dauer_minuten": 90, "employee_id": None},
            {"datum": "23.05.2026", "uhrzeit": "09:00",
             "dauer_minuten": 90, "employee_id": None},
        ],
    }
    dialog_capture["response"] = {
        "reply_text": "buche jetzt.",
        "next_action": "BOOK_SLOT",
        "anrede_form": "DU",
        "wunsch_datum": None,
        "wunsch_uhrzeit": None,
        "chosen_slot_index": 0,
        "reason": "kunde bestaetigt",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-4",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "folge",
        },
        existing_conv=existing,
    )

    assert res["next_action"] == "PROPOSE_SLOTS"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Termin bestätigt" not in body
    assert "Mögliche Termine" in body
    assert "15:30" in body
    # NICHT auf BOOKED gewechselt
    set_calls = pipeline_mocks["captured"]["set_conversation_state"]
    assert not any(c["state"] == "booked" for c in set_calls)


# =====================================================================
# BOOK_DIRECT — Kunde nennt konkretes Datum + Uhrzeit
# =====================================================================

@pytest.mark.asyncio
async def test_book_direct_books_named_termin_if_free(
    pipeline_mocks, dialog_capture,
):
    """Kunde nennt konkreten Termin -> Pipeline bucht direkt, ohne
    Alternativen-Umweg."""
    tenant_id = pipeline_mocks["tenant"].id
    pipeline_mocks["kalender"].book_result = {
        "erfolg": True, "event_id": "ev-direct-1",
    }
    dialog_capture["response"] = {
        "reply_text": "alles klar, traget ich ein.",
        "next_action": "BOOK_DIRECT",
        "anrede_form": "DU",
        "wunsch_datum": None,
        "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": "22.05.2026",
        "direct_uhrzeit": "14:00",
        "reason": "kunde nennt termin",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-direct-1",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "BOOK_DIRECT"
    # Kalender hat genau den gewuenschten Termin gebucht
    book_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "book_appointment"
    )
    assert book_call[1]["datum"] == "22.05.2026"
    assert book_call[1]["uhrzeit"] == "14:00"
    # find_free_slots wurde NICHT vorher gerufen — direkt gebucht
    assert not any(
        c[0] == "find_free_slots"
        for c in pipeline_mocks["kalender"].calls
    )
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Termin bestätigt" in body
    assert "22.05.2026" in body and "14:00" in body
    cc = pipeline_mocks["captured"]["create_conversation"]
    assert cc[0]["state"] == "booked"


@pytest.mark.asyncio
async def test_book_direct_conflict_falls_back_to_alternatives(
    pipeline_mocks, dialog_capture,
):
    """Wenn der gewuenschte Termin belegt ist -> Pipeline schlaegt
    Alternativen rund um den Wunsch vor (statt ASK_MORE)."""
    tenant_id = pipeline_mocks["tenant"].id
    pipeline_mocks["kalender"].book_result = {
        "erfolg": False, "konflikt": True,
        "nachricht": "Slot ist belegt",
    }
    pipeline_mocks["kalender"].find_free_slots_result = {
        "erfolg": True,
        "slots": [
            {"datum": "22.05.2026", "uhrzeit": "15:30",
             "dauer_minuten": 90, "employee_id": None},
            {"datum": "23.05.2026", "uhrzeit": "09:00",
             "dauer_minuten": 90, "employee_id": None},
        ],
    }
    dialog_capture["response"] = {
        "reply_text": "moment ich pruefe das.",
        "next_action": "BOOK_DIRECT",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": "22.05.2026",
        "direct_uhrzeit": "14:00",
        "reason": "termin von kunde",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-direct-2",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "PROPOSE_SLOTS"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Mögliche Termine" in body
    assert "15:30" in body
    assert "Termin bestätigt" not in body


# =====================================================================
# CANCEL_TERMIN
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_termin_calls_cancel_helper_and_renders_storno_box(
    pipeline_mocks, dialog_capture,
):
    """Q waehlt CANCEL_TERMIN -> Pipeline ruft cancel_kunde_termine,
    rendert Storno-Box, state=STORNIERT, Push mit cnt."""
    tenant_id = pipeline_mocks["tenant"].id
    pipeline_mocks["cancelled_event_ids"] = ["ev-x", "ev-y"]
    dialog_capture["response"] = {
        "reply_text": "ist erledigt, dein Termin ist storniert.",
        "next_action": "CANCEL_TERMIN",
        "anrede_form": "DU",
        "wunsch_datum": None,
        "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "reason": "kunde sagt ab",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-5",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "storno",
        },
    )

    assert res["next_action"] == "CANCEL_TERMIN"
    # cancel-helper wurde gerufen
    cc = pipeline_mocks["captured"].get("cancel_kunde_termine", [])
    assert len(cc) == 1
    # Storno-Box im Body
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "2 Termine storniert" in body
    # state=storniert
    cc2 = pipeline_mocks["captured"]["create_conversation"]
    assert cc2[0]["state"] == "storniert"
    # Push
    pushes = pipeline_mocks["captured"]["telegram_pushes"]
    assert any("Termin storniert" in p["label"] for p in pushes)


# =====================================================================
# VOR-GATE: Kontaktdaten-Pflicht + Termin-Status (neuer Flow:
# erst Termin (braucht Name+Telefon), dann Formular)
# =====================================================================

@pytest.mark.asyncio
async def test_termin_action_proceeds_with_open_form_when_contact_present(
    pipeline_mocks, dialog_capture,
):
    """Neuer Flow: ein offenes (nicht ausgefuelltes) Formular blockiert
    Termin-Aktionen NICHT mehr. Liegen Name + Telefon vor, schlaegt Q
    Termine vor — das Formular folgt erst nach der Buchung."""
    pipeline_mocks["anfrage_status"] = {
        "status": "open",
        "sent_at": None,
        "submitted_at": None,
        "antworten": None,
        "anliegen": None,
    }
    pipeline_mocks["kalender"].find_free_slots_result = {
        "erfolg": True,
        "slots": [
            {"datum": "25.05.2026", "uhrzeit": "10:00",
             "dauer_minuten": 90, "employee_id": None},
        ],
    }
    dialog_capture["response"] = {
        "reply_text": "Hier kommen ein paar Vorschlaege.",
        "next_action": "PROPOSE_SLOTS",
        "anrede_form": "DU",
        "wunsch_datum": "25.05.2026",
        "wunsch_uhrzeit": "10:00",
        "chosen_slot_index": None,
        "direct_datum": None,
        "direct_uhrzeit": None,
        "reason": "Kunde fragt nach Termin",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-1",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "PROPOSE_SLOTS"
    assert any(
        c[0] == "find_free_slots"
        for c in pipeline_mocks["kalender"].calls
    )
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Mögliche Termine" in body
    # Kein (zweiter) Formular-Button in der Vorschlags-Mail
    assert "Formular ausf" not in body


@pytest.mark.asyncio
async def test_termin_action_blocked_without_phone(
    pipeline_mocks, dialog_capture,
):
    """Pflicht-Gate: Termin-Buchung ohne Telefonnummer wird abgefangen ->
    ASK_MORE (Q fragt nach), KEIN Kalender-Call."""
    dialog_capture["response"] = {
        "reply_text": "ich buche fuer dich.",
        "next_action": "BOOK_DIRECT",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": "22.05.2026",
        "direct_uhrzeit": "14:00",
        "kunde_voller_name": "Max Mustermann",
        "kunde_telefon": "",  # keine Nummer -> Gate blockt
        "reason": "termin direkt",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-nophone",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "ASK_MORE"
    assert not any(
        c[0] == "book_appointment"
        for c in pipeline_mocks["kalender"].calls
    )
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Telefonnummer" in body


@pytest.mark.asyncio
async def test_termin_action_blocked_when_termin_exists(
    pipeline_mocks, dialog_capture,
):
    """Besteht fuer die Konversation schon ein Termin (state=booked),
    schlaegt Q von sich aus keinen neuen vor -> ASK_MORE, KEIN
    Kalender-Call."""
    tenant_id = pipeline_mocks["tenant"].id
    existing = _make_conv(tenant_id=tenant_id, state="booked")
    existing.gcal_event_id = "ev-existing-1"
    existing.termin_datum = dt.date(2026, 5, 22)
    dialog_capture["response"] = {
        "reply_text": "ich schlage neue Termine vor.",
        "next_action": "PROPOSE_SLOTS",
        "anrede_form": "DU",
        "wunsch_datum": "28.05.2026", "wunsch_uhrzeit": "10:00",
        "chosen_slot_index": None,
        "direct_datum": None, "direct_uhrzeit": None,
        "reason": "kunde fragt",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=tenant_id,
        message_id="msg-gate-exists",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "folge",
        },
        existing_conv=existing,
    )

    assert res["next_action"] == "ASK_MORE"
    assert not any(
        c[0] in ("find_free_slots", "book_appointment")
        for c in pipeline_mocks["kalender"].calls
    )
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "bereits einen Termin" in body


@pytest.mark.asyncio
async def test_send_formular_with_open_form_does_not_resend(
    pipeline_mocks, dialog_capture,
):
    """Q waehlt SEND_FORMULAR aber Token ist schon offen -> ASK_MORE
    (kein Doppel-Formular). Genau der Bug aus dem Live-Test."""
    pipeline_mocks["anfrage_status"] = {
        "status": "open", "sent_at": None, "submitted_at": None,
        "antworten": None, "anliegen": None,
    }
    dialog_capture["response"] = {
        "reply_text": "Fuell bitte das Formular aus.",
        "next_action": "SEND_FORMULAR",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": None, "direct_uhrzeit": None,
        "reason": "neue anfrage",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-1b",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "ASK_MORE"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Formular ausf" not in body


@pytest.mark.asyncio
async def test_send_formular_when_already_submitted_degrades(
    pipeline_mocks, dialog_capture,
):
    """SEND_FORMULAR obwohl Formular bereits SUBMITTED -> ASK_MORE
    (kein zweites Formular nach Eingang)."""
    pipeline_mocks["anfrage_status"] = {
        "status": "submitted", "sent_at": None, "submitted_at": None,
        "antworten": {"anliegen": "Treppe"}, "anliegen": "Treppe",
    }
    dialog_capture["response"] = {
        "reply_text": "Fuell bitte das Formular aus.",
        "next_action": "SEND_FORMULAR",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": None, "direct_uhrzeit": None,
        "reason": "fehlentscheidung",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-1c",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "ASK_MORE"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    assert "Formular ausf" not in body


@pytest.mark.asyncio
async def test_book_direct_without_form_books_then_sends_form(
    pipeline_mocks, dialog_capture,
):
    """Neuer Flow: noch kein Formular + Kontaktdaten vorhanden -> Q bucht
    direkt UND schickt das Anfrage-Formular gleich in derselben Mail mit
    (Termin-Bestaetigung + Formular-Button). Telefon + Mail wandern an
    den Kalender."""
    pipeline_mocks["anfrage_status"] = None  # gar kein Token
    pipeline_mocks["kalender"].book_result = {
        "erfolg": True, "event_id": "ev-direct-noform",
    }
    dialog_capture["response"] = {
        "reply_text": "ich trage den Termin ein.",
        "next_action": "BOOK_DIRECT",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": "22.05.2026",
        "direct_uhrzeit": "14:00",
        "reason": "termin direkt",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-2",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "neu",
        },
    )

    assert res["next_action"] == "BOOK_DIRECT"
    book_call = next(
        c for c in pipeline_mocks["kalender"].calls
        if c[0] == "book_appointment"
    )
    # Telefon + Kunden-Mail wurden an den Kalender durchgereicht
    assert book_call[1].get("telefon") == "0151 23456789"
    assert book_call[1].get("kunde_email") == "sven@example.de"
    body = pipeline_mocks["captured"]["send_tracked_mail"][0]["body_html"]
    # Termin-Bestaetigung UND Formular-Button (Formular folgt dem Termin)
    assert "Termin bestätigt" in body
    assert "Formular ausf" in body


@pytest.mark.asyncio
async def test_cancel_termin_works_even_without_form(
    pipeline_mocks, dialog_capture,
):
    """Storno darf IMMER durch, unabhaengig vom Formular-Status — der
    Kunde will ja nichts neues anstossen, sondern etwas Bestehendes
    loeschen."""
    pipeline_mocks["anfrage_status"] = None
    pipeline_mocks["cancelled_event_ids"] = ["ev-cancel"]
    dialog_capture["response"] = {
        "reply_text": "alles klar, ich storniere.",
        "next_action": "CANCEL_TERMIN",
        "anrede_form": "DU",
        "wunsch_datum": None, "wunsch_uhrzeit": None,
        "chosen_slot_index": None,
        "direct_datum": None, "direct_uhrzeit": None,
        "reason": "kunde sagt ab",
    }

    res = await microsoft_inbox.process_relevant_kunde_mail(
        tenant_id=pipeline_mocks["tenant"].id,
        message_id="msg-gate-3",
        classification_result={
            "classification": "RELEVANT_KUNDE", "confidence": "high",
            "reason": "storno",
        },
    )

    assert res["next_action"] == "CANCEL_TERMIN"


# =====================================================================
# Dialog-Schema: Validation
# =====================================================================

def test_dialog_schema_contains_all_termin_actions():
    enum = gem.DIALOG_RESPONSE_SCHEMA["properties"]["next_action"]["enum"]
    for action in ("PROPOSE_SLOTS", "BOOK_SLOT", "CANCEL_TERMIN"):
        assert action in enum


def test_dialog_schema_has_slot_fields():
    props = gem.DIALOG_RESPONSE_SCHEMA["properties"]
    assert "wunsch_datum" in props
    assert "wunsch_uhrzeit" in props
    assert "chosen_slot_index" in props
