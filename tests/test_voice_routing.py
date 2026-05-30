"""Tests fuer voice_init Skill-Routing (Teil A der Multi-Mitarbeiter-
Voice-Anbindung).

Deckt:
- _parse_wunschzeit_for_routing: ISO + deutsches Format + garbage
- _routing_to_response: reason-Formatierung, passthrough, None
- _ensure_calendar_capable_routing: kalenderloser Employee → Default-Fallback
- _handle_checke_kalender: routing-Block + employee_id im kalender-Payload
- _handle_buche_termin: employee_id aus Payload, Re-Route bei Fehlen,
  idempotency_key enthaelt employee_id
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.routing.employee_router import RoutingDecision
from plugins.voice_init import handler as voice_handler


# =====================================================================
# Test-Doubles
# =====================================================================


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _make_session_factory(results):
    """Liefert AsyncSessionLocal-Ersatz mit gemeinsamer FIFO-Queue.

    Mehrere `async with AsyncSessionLocal()`-Bloecke teilen sich denselben
    Queue-State: jeder `execute().scalar_one_or_none()` pop't das naechste
    Element, egal in welcher Session. So koennen wir Test-Setups als
    flache Liste der erwarteten Query-Resultate schreiben.
    """
    shared = list(results)

    class _SharedSession:
        async def execute(self, stmt):
            obj = shared.pop(0) if shared else None
            return _FakeResult(obj)

    @asynccontextmanager
    async def cm():
        yield _SharedSession()

    def factory():
        return cm()

    return factory


def _make_employee(
    *, slug="emp", name=None, is_default=False,
    calendar_provider="google", skills=None, tenant_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        slug=slug,
        name=name or slug.capitalize(),
        is_default=is_default,
        is_active=True,
        calendar_provider=calendar_provider,
        calendar_id=None,
        skills=skills or [],
    )


def _make_plugin():
    """Plugin-Instanz fuer Tests. context wird in den getesteten Methoden
    nicht gelesen, daher reicht ein Stub mit den Duck-Type-Attributen."""
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return voice_handler.Plugin(context)


# =====================================================================
# _parse_wunschzeit_for_routing
# =====================================================================


def test_parse_wunschzeit_iso_format():
    dt_obj = voice_handler._parse_wunschzeit_for_routing("2026-05-20", "14:00")
    assert isinstance(dt_obj, dt.datetime)
    assert dt_obj.year == 2026 and dt_obj.month == 5 and dt_obj.day == 20
    assert dt_obj.hour == 14 and dt_obj.minute == 0


def test_parse_wunschzeit_german_format():
    dt_obj = voice_handler._parse_wunschzeit_for_routing("20.05.2026", "14:30")
    assert isinstance(dt_obj, dt.datetime)
    assert dt_obj.day == 20 and dt_obj.hour == 14 and dt_obj.minute == 30


def test_parse_wunschzeit_garbage_returns_none():
    assert voice_handler._parse_wunschzeit_for_routing("nicht-ein-datum", "14:00") is None
    assert voice_handler._parse_wunschzeit_for_routing("", "") is None


# =====================================================================
# _routing_to_response
# =====================================================================


def test_routing_response_none():
    assert voice_handler._routing_to_response(None) is None


def test_routing_response_skill_match_appends_skills():
    routing = RoutingDecision(
        employee_id=uuid.uuid4(),
        employee_name="Max Mustermann",
        employee_slug="max",
        reason="skill-match",
        score=2.0,
        debug={"needed_skills": ["tischler", "holz"]},
    )
    out = voice_handler._routing_to_response(routing)
    assert out["employee_slug"] == "max"
    assert out["employee_name"] == "Max Mustermann"
    assert out["reason"] == "skill-match: tischler, holz"
    assert out["score"] == 2.0
    assert isinstance(out["employee_id"], str)  # UUID muss serialisiert sein


def test_routing_response_other_reasons_passthrough():
    for reason in ("only-active", "distance", "fallback-default", "no-coverage", "no-calendar"):
        routing = RoutingDecision(
            employee_id=uuid.uuid4(), employee_name="X", employee_slug="x",
            reason=reason, score=1.0, debug={},
        )
        out = voice_handler._routing_to_response(routing)
        assert out["reason"] == reason


def test_routing_response_skill_match_without_debug_falls_back():
    """Falls debug kein 'needed_skills' enthaelt: nur 'skill-match' ohne Annotation."""
    routing = RoutingDecision(
        employee_id=uuid.uuid4(), employee_name="X", employee_slug="x",
        reason="skill-match", score=1.0, debug={},
    )
    out = voice_handler._routing_to_response(routing)
    assert out["reason"] == "skill-match"


# =====================================================================
# _ensure_calendar_capable_routing (A.3 Edge-Case)
# =====================================================================


@pytest.mark.asyncio
async def test_ensure_calendar_capable_passes_through_when_provider_set(monkeypatch):
    """Employee mit calendar_provider → Routing bleibt unveraendert."""
    tenant_id = uuid.uuid4()
    routed_emp = _make_employee(slug="max", calendar_provider="microsoft")
    routing = RoutingDecision(
        employee_id=routed_emp.id, employee_name="Max", employee_slug="max",
        reason="skill-match", score=2.0, debug={"needed_skills": ["heizung"]},
    )
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([routed_emp]),
    )
    out = await voice_handler._ensure_calendar_capable_routing(tenant_id, routing)
    assert out is routing  # gleiche Instanz


@pytest.mark.asyncio
async def test_ensure_calendar_capable_falls_back_to_default_with_no_calendar_reason(
    monkeypatch,
):
    """Employee ohne calendar_provider → Default-Employee, reason='no-calendar'."""
    tenant_id = uuid.uuid4()
    routed_emp = _make_employee(slug="max", calendar_provider=None)
    default_emp = _make_employee(
        slug="inhaber", is_default=True, calendar_provider="google",
    )
    routing = RoutingDecision(
        employee_id=routed_emp.id, employee_name="Max", employee_slug="max",
        reason="skill-match", score=1.0, debug={"needed_skills": ["heizung"]},
    )
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([routed_emp, default_emp]),
    )
    out = await voice_handler._ensure_calendar_capable_routing(tenant_id, routing)
    assert out is not None
    assert out.employee_id == default_emp.id
    assert out.reason == "no-calendar"
    assert out.debug["original_routing"]["employee_slug"] == "max"
    assert out.debug["original_routing"]["reason"] == "skill-match"


@pytest.mark.asyncio
async def test_ensure_calendar_capable_keeps_original_when_default_also_uncalendared(
    monkeypatch,
):
    """Wenn auch der Default keinen Kalender hat: Original-Routing bleibt
    stehen (find_free_slots wird sauber failen, das ist bessere Diagnose
    als ein verwirrender no-calendar-Fallback ohne Wirkung)."""
    tenant_id = uuid.uuid4()
    routed_emp = _make_employee(slug="max", calendar_provider=None)
    default_emp = _make_employee(
        slug="inhaber", is_default=True, calendar_provider=None,
    )
    routing = RoutingDecision(
        employee_id=routed_emp.id, employee_name="Max", employee_slug="max",
        reason="skill-match", score=1.0, debug={"needed_skills": ["heizung"]},
    )
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([routed_emp, default_emp]),
    )
    out = await voice_handler._ensure_calendar_capable_routing(tenant_id, routing)
    assert out is routing


@pytest.mark.asyncio
async def test_ensure_calendar_capable_none_input():
    out = await voice_handler._ensure_calendar_capable_routing(uuid.uuid4(), None)
    assert out is None


# =====================================================================
# _handle_checke_kalender — End-to-End (gemockt)
# =====================================================================


class _FakeKalender:
    """Ersetzt das kalender-Plugin in get_plugin_for_tenant."""

    def __init__(self, find_slots_result, config=None):
        self.find_slots_result = find_slots_result
        self.config = config or {"termin_dauer_minuten": 90}
        self.received_payloads = []

    async def on_webhook(self, endpoint, payload):
        self.received_payloads.append((endpoint, dict(payload)))
        if endpoint == "find_free_slots":
            return self.find_slots_result
        if endpoint == "book_appointment":
            return {"erfolg": True, "event_id": "evt-1"}
        return {"erfolg": False, "nachricht": f"unbekannt: {endpoint}"}


def _make_tenant(slug="demo"):
    return SimpleNamespace(id=uuid.uuid4(), slug=slug)


@pytest.mark.asyncio
async def test_checke_kalender_passes_employee_id_and_returns_routing(monkeypatch):
    tenant = _make_tenant("demo")
    routed_emp = _make_employee(
        slug="daniel", name="Daniel Mueller", calendar_provider="microsoft",
        tenant_id=tenant.id, skills=["tischler"],
    )
    routing = RoutingDecision(
        employee_id=routed_emp.id, employee_name="Daniel Mueller",
        employee_slug="daniel", reason="skill-match", score=1.0,
        debug={"needed_skills": ["tischler"]},
    )
    kalender = _FakeKalender(
        find_slots_result={
            "erfolg": True,
            "slots": [{"datum": "20.05.2026", "uhrzeit": "14:00"}],
            "smart_routing": None,
        },
    )

    # 1) Tenant-Lookup → tenant; 2) ensure_calendar_capable → emp (hat provider)
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([tenant, routed_emp]),
    )
    monkeypatch.setattr(voice_handler, "choose_employee", AsyncMock(return_value=routing))

    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    result = await plugin._handle_checke_kalender({
        "tenant_slug": "demo",
        "wunschzeit": "2026-05-20T14:00",
        "anliegen": "Kuechenmontage Tischler",
        "kunde_adresse": "Musterstr 1",
    })

    assert result["erfolg"] is True
    assert len(result["slots"]) == 1
    assert "slot_id" in result["slots"][0]
    # Wichtigster Assert: employee_id wurde ans kalender-Plugin durchgereicht.
    _, k_payload = kalender.received_payloads[0]
    assert k_payload["employee_id"] == routed_emp.id
    # routing-Block in Response
    assert result["routing"]["employee_slug"] == "daniel"
    assert result["routing"]["reason"] == "skill-match: tischler"


@pytest.mark.asyncio
async def test_checke_kalender_tenant_not_found(monkeypatch):
    """Unbekannter tenant_slug → erfolg=False, keine weiteren Calls."""
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([None]),  # Tenant nicht gefunden
    )
    choose_mock = AsyncMock()
    monkeypatch.setattr(voice_handler, "choose_employee", choose_mock)

    plugin = _make_plugin()
    result = await plugin._handle_checke_kalender({
        "tenant_slug": "ghost",
        "wunschzeit": "2026-05-20T14:00",
    })
    assert result["erfolg"] is False
    assert "ghost" in result["nachricht"]
    choose_mock.assert_not_called()


# =====================================================================
# _handle_buche_termin — employee_id-Pfad
# =====================================================================


@pytest.mark.asyncio
async def test_buche_termin_uses_payload_employee_id(monkeypatch):
    """Voice-Agent reicht employee_id zurueck → wird unveraendert genutzt,
    KEIN erneuter choose_employee-Call, idempotency_key enthaelt employee_id."""
    tenant = _make_tenant("demo")
    emp_id = uuid.uuid4()
    kalender = _FakeKalender(find_slots_result=None)

    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal", _make_session_factory([tenant]),
    )
    choose_mock = AsyncMock()
    monkeypatch.setattr(voice_handler, "choose_employee", choose_mock)
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "20.05.2026|14:00|90",
        "employee_id": str(emp_id),
        "kunde_name": "Frau Mueller",
        "anliegen": "Kuechenmontage",
    })

    assert result["erfolg"] is True
    choose_mock.assert_not_called()  # employee_id war im Payload, kein Re-Route
    _, book_payload = kalender.received_payloads[0]
    assert book_payload["employee_id"] == emp_id
    assert str(emp_id) in book_payload["idempotency_key"]
    assert "20.05.2026|14:00|90" in book_payload["idempotency_key"]


@pytest.mark.asyncio
async def test_buche_termin_reroutes_when_employee_id_missing(monkeypatch):
    """Fehlende employee_id → choose_employee + Calendar-Check, dann gebucht."""
    tenant = _make_tenant("demo")
    fallback_emp = _make_employee(
        slug="inhaber", is_default=True, calendar_provider="google",
        tenant_id=tenant.id,
    )
    routing = RoutingDecision(
        employee_id=fallback_emp.id, employee_name="Inhaber",
        employee_slug="inhaber", reason="only-active", score=1.0, debug={},
    )
    kalender = _FakeKalender(find_slots_result=None)

    # Session-Sequence:
    #   1) Tenant-Lookup           → tenant
    #   2) ensure-calendar 1. Query → fallback_emp (hat provider, kein Fallback noetig)
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([tenant, fallback_emp]),
    )
    monkeypatch.setattr(voice_handler, "choose_employee", AsyncMock(return_value=routing))
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "20.05.2026|14:00|90",
        # employee_id fehlt absichtlich
        "kunde_name": "Frau Mueller",
        "anliegen": "Wasserhahn tropft",
    })

    assert result["erfolg"] is True
    _, book_payload = kalender.received_payloads[0]
    assert book_payload["employee_id"] == fallback_emp.id
    assert str(fallback_emp.id) in book_payload["idempotency_key"]
    # Routing landet auch im Response (weil hier rekonstruiert).
    assert result["routing"]["employee_slug"] == "inhaber"


@pytest.mark.asyncio
async def test_buche_termin_invalid_employee_id_falls_back_to_rerouting(monkeypatch):
    """Ungueltige UUID im Payload → reroute statt crash."""
    tenant = _make_tenant("demo")
    fallback_emp = _make_employee(
        slug="inhaber", is_default=True, calendar_provider="google",
        tenant_id=tenant.id,
    )
    routing = RoutingDecision(
        employee_id=fallback_emp.id, employee_name="Inhaber",
        employee_slug="inhaber", reason="only-active", score=1.0, debug={},
    )
    kalender = _FakeKalender(find_slots_result=None)

    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([tenant, fallback_emp]),
    )
    choose_mock = AsyncMock(return_value=routing)
    monkeypatch.setattr(voice_handler, "choose_employee", choose_mock)
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "20.05.2026|14:00|90",
        "employee_id": "not-a-uuid",  # ungueltig
        "kunde_name": "Frau Mueller",
        "anliegen": "Heizung defekt",
    })

    assert result["erfolg"] is True
    choose_mock.assert_called_once()


@pytest.mark.asyncio
async def test_buche_termin_no_coverage_does_not_book(monkeypatch):
    """reason='no-coverage' (kein Mitarbeiter zur Zeit verfuegbar) → NICHT
    blind den Default-Employee buchen, sondern erfolg=False zurueckgeben."""
    tenant = _make_tenant("demo")
    fallback_emp = _make_employee(
        slug="inhaber", is_default=True, calendar_provider="google",
        tenant_id=tenant.id,
    )
    # choose_employee liefert bei no-coverage zwar den Default-Employee als
    # Signal-Traeger, aber eine Buchung waere auf einen abwesenden Mitarbeiter.
    routing = RoutingDecision(
        employee_id=fallback_emp.id, employee_name="Inhaber",
        employee_slug="inhaber", reason="no-coverage", score=0.0, debug={},
    )
    kalender = _FakeKalender(find_slots_result=None)

    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([tenant, fallback_emp]),
    )
    monkeypatch.setattr(
        voice_handler, "choose_employee", AsyncMock(return_value=routing),
    )
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "20.05.2026|14:00|90",
        # employee_id fehlt → reroute → no-coverage
        "kunde_name": "Frau Mueller",
        "anliegen": "Notfall",
    })

    assert result["erfolg"] is False
    assert "kein mitarbeiter" in result["nachricht"].lower()
    # KEINE Buchung an den Kalender weitergereicht.
    assert kalender.received_payloads == []


@pytest.mark.asyncio
async def test_buche_termin_invalid_slot_id():
    """Kaputte slot_id → frueher Fail mit aussagekraeftiger Nachricht."""
    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "kaputt",
        "kunde_name": "X",
    })
    assert result["erfolg"] is False
    assert "Slot" in result["nachricht"]


# =====================================================================
# call_ended — Billing-Dedup
# =====================================================================


@pytest.mark.asyncio
async def test_call_ended_dedups_by_conversation_id(monkeypatch):
    """Doppeltes call_ended (Webhook-Retry) fuer dieselbe conversation_id
    darf die Anruf-Dauer nicht zweimal billen."""
    import core.billing as billing
    voice_handler._PROCESSED_CALLS.clear()

    tenant = _make_tenant("demo")
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        # je Aufruf ein frischer Tenant-Lookup-Treffer
        lambda: _make_session_factory([tenant, tenant])(),
    )
    calls = {"deepgram": 0, "sipgate": 0}
    monkeypatch.setattr(
        billing, "track_deepgram_seconds",
        AsyncMock(side_effect=lambda *a, **k: calls.__setitem__("deepgram", calls["deepgram"] + 1)),
    )
    monkeypatch.setattr(billing, "track_elevenlabs_chars", AsyncMock())
    monkeypatch.setattr(
        billing, "track_api_usage",
        AsyncMock(side_effect=lambda *a, **k: calls.__setitem__("sipgate", calls["sipgate"] + 1)),
    )

    plugin = _make_plugin()
    payload = {
        "tenant_slug": "demo",
        "duration_seconds": 142,
        "conversation_id": "conv-abc-123",
    }
    first = await plugin._handle_call_ended(payload)
    second = await plugin._handle_call_ended(dict(payload))

    assert first["tracked"] is True
    assert second.get("deduped") is True and second["tracked"] is False
    # Tracking genau einmal — nicht doppelt gebillt.
    assert calls["deepgram"] == 1
    assert calls["sipgate"] == 1


# =====================================================================
# Async-Terminsuche — starte_terminsuche + hole_terminvorschlaege
# =====================================================================


@pytest.mark.asyncio
async def test_starte_terminsuche_returns_jobid_immediately_and_hole_delivers(
    monkeypatch,
):
    """starte_terminsuche gibt sofort job_id+laeuft zurueck; nach Abschluss
    des Hintergrund-Tasks liefert hole_terminvorschlaege die Slots+Routing."""
    voice_handler._TERMINSUCHE_JOBS.clear()
    tenant = _make_tenant("demo")
    routed_emp = _make_employee(
        slug="daniel", name="Daniel Mueller", calendar_provider="microsoft",
        tenant_id=tenant.id, skills=["tischler"],
    )
    routing = RoutingDecision(
        employee_id=routed_emp.id, employee_name="Daniel Mueller",
        employee_slug="daniel", reason="skill-match", score=1.0,
        debug={"needed_skills": ["tischler"]},
    )
    kalender = _FakeKalender(find_slots_result={
        "erfolg": True,
        "slots": [{"datum": "20.05.2026", "uhrzeit": "14:00"}],
        "smart_routing": None,
    })
    # Session-Queue: 1) Tenant (starte), 2) emp (ensure_calendar im Worker)
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _make_session_factory([tenant, routed_emp]),
    )
    monkeypatch.setattr(voice_handler, "choose_employee", AsyncMock(return_value=routing))
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    start = await plugin._handle_starte_terminsuche({
        "tenant_slug": "demo",
        "wunschzeit": "2026-05-20T14:00",
        "anliegen": "Kuechenmontage Tischler",
    })
    assert start["erfolg"] is True
    assert start["status"] == "laeuft"
    job_id = start["job_id"]
    assert job_id

    # Hintergrund-Task zu Ende laufen lassen (im echten Betrieb laeuft er
    # weiter waehrend der Agent redet).
    await voice_handler._TERMINSUCHE_JOBS[job_id]["task"]

    done = await plugin._handle_hole_terminvorschlaege({"job_id": job_id})
    assert done["erfolg"] is True
    assert done["status"] == "fertig"
    assert len(done["slots"]) == 1
    assert "slot_id" in done["slots"][0]
    assert done["routing"]["employee_slug"] == "daniel"
    # employee_id wurde im Worker ans kalender-Plugin durchgereicht
    _, k_payload = kalender.received_payloads[0]
    assert k_payload["employee_id"] == routed_emp.id


@pytest.mark.asyncio
async def test_starte_terminsuche_tenant_not_found(monkeypatch):
    """Unbekannter Tenant → erfolg=False, kein Job, kein choose_employee."""
    voice_handler._TERMINSUCHE_JOBS.clear()
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal", _make_session_factory([None]),
    )
    choose_mock = AsyncMock()
    monkeypatch.setattr(voice_handler, "choose_employee", choose_mock)

    plugin = _make_plugin()
    result = await plugin._handle_starte_terminsuche({
        "tenant_slug": "ghost", "wunschzeit": "2026-05-20T14:00",
    })
    assert result["erfolg"] is False
    assert "ghost" in result["nachricht"]
    choose_mock.assert_not_called()
    assert len(voice_handler._TERMINSUCHE_JOBS) == 0


@pytest.mark.asyncio
async def test_hole_terminvorschlaege_unknown_job():
    """Unbekannte/abgelaufene job_id → erfolg=False, status=unbekannt."""
    voice_handler._TERMINSUCHE_JOBS.clear()
    plugin = _make_plugin()
    result = await plugin._handle_hole_terminvorschlaege({"job_id": "gibtsnicht"})
    assert result["erfolg"] is False
    assert result["status"] == "unbekannt"


@pytest.mark.asyncio
async def test_hole_terminvorschlaege_still_running():
    """Job existiert, aber noch nicht fertig → status=laeuft (Agent wartet)."""
    voice_handler._TERMINSUCHE_JOBS.clear()
    voice_handler._TERMINSUCHE_JOBS["job-x"] = {
        "created_at": dt.datetime.now(dt.timezone.utc),
        "status": "laeuft",
        "result": None,
    }
    plugin = _make_plugin()
    result = await plugin._handle_hole_terminvorschlaege({"job_id": "job-x"})
    assert result["erfolg"] is True
    assert result["status"] == "laeuft"
    assert "slots" not in result


@pytest.mark.asyncio
async def test_starte_terminsuche_worker_failure_is_reported(monkeypatch):
    """Crasht die Suche im Hintergrund, meldet hole_terminvorschlaege das
    sauber (erfolg=False) statt den Job ewig auf 'laeuft' zu lassen."""
    voice_handler._TERMINSUCHE_JOBS.clear()
    tenant = _make_tenant("demo")
    kalender = _FakeKalender(find_slots_result=None)
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal", _make_session_factory([tenant]),
    )
    monkeypatch.setattr(
        voice_handler, "choose_employee",
        AsyncMock(side_effect=RuntimeError("Gemini down")),
    )
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_plugin()
    start = await plugin._handle_starte_terminsuche({
        "tenant_slug": "demo", "wunschzeit": "2026-05-20T14:00",
    })
    job_id = start["job_id"]
    await voice_handler._TERMINSUCHE_JOBS[job_id]["task"]

    done = await plugin._handle_hole_terminvorschlaege({"job_id": job_id})
    assert done["status"] == "fertig"
    assert done["erfolg"] is False
    assert "fehlgeschlagen" in done["nachricht"]
