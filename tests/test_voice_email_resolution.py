"""Tests fuer kunde_email-Resolution im Voice-Flow.

Deckt die Source-of-Truth-Architektur:
- AnfrageToken speichert kunde_email + kunde_telefon
- _handle_buche_termin holt die Mail per phone-Lookup (Token > Payload > none)
- Integration: Mail landet als extendedProperty am Kalender-Event

Keine echte DB / HTTP — Lookups und Adapter werden gemockt.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.voice_init import handler as voice_handler


# =====================================================================
# Test-Doubles
# =====================================================================

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _session_factory(results):
    """Shared-Queue Session-Factory (analog test_voice_routing.py)."""
    shared = list(results)

    class _S:
        async def execute(self, _stmt):
            return _FakeResult(shared.pop(0) if shared else None)

    @asynccontextmanager
    async def cm():
        yield _S()

    return cm


def _make_tenant(slug="demo"):
    return SimpleNamespace(id=uuid.uuid4(), slug=slug)


class _FakeKalender:
    """Erfasst die book_appointment-Payloads — so koennen wir pruefen
    welcher kunde_email-Wert reingegangen ist."""

    def __init__(self):
        self.received_payloads: list[tuple[str, dict]] = []

    async def on_webhook(self, endpoint, payload):
        self.received_payloads.append((endpoint, dict(payload)))
        if endpoint == "book_appointment":
            return {"erfolg": True, "event_id": "evt-fake"}
        return {"erfolg": False, "nachricht": f"unbekannt: {endpoint}"}


def _make_plugin():
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return voice_handler.Plugin(context)


def _make_token(
    *, tenant_id, kunde_email="kunde@example.de",
    kunde_telefon="49301234567",
    created_at=None,
):
    """Stub fuer AnfrageToken (SimpleNamespace reicht — kein DB-Mapping noetig)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kunde_email=kunde_email,
        kunde_telefon=kunde_telefon,
        created_at=created_at or dt.datetime.now(dt.timezone.utc),
    )


def _patch_buche_termin_deps(
    monkeypatch, *, tenant, kalender, lookup_result, choose_result=None,
):
    """Wire alle Abhaengigkeiten von _handle_buche_termin auf Stubs.

    - AsyncSessionLocal liefert Tenant + (falls choose_result=None auch
      Default-Employee fuer Ensure-Routing)
    - get_plugin_for_tenant -> kalender
    - lookup_recent_anfrage_by_phone -> lookup_result
    - choose_employee + _ensure_calendar_capable_routing -> No-Op-Routing
    """
    # Tenant via Session-Factory + ggf. Default-Employee
    session_results = [tenant]
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _session_factory(session_results),
    )
    import core.plugin_system as ps
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))
    # Lookup wird in _handle_buche_termin lokal importiert — Patch ueber
    # core.integrations.anfrage_forms.
    import core.integrations.anfrage_forms as af
    monkeypatch.setattr(
        af, "lookup_recent_anfrage_by_phone",
        AsyncMock(return_value=lookup_result),
    )
    # Routing umgehen: choose_employee + ensure_calendar_capable_routing
    # liefern None, sodass employee_id im book_payload nicht gesetzt wird
    # (das stoert die Email-Resolution-Tests nicht).
    monkeypatch.setattr(
        voice_handler, "choose_employee",
        AsyncMock(return_value=choose_result),
    )
    monkeypatch.setattr(
        voice_handler, "_ensure_calendar_capable_routing",
        AsyncMock(return_value=choose_result),
    )


# =====================================================================
# Test 1: speichere_kontakt -> AnfrageToken erzeugt mit email + telefon
# =====================================================================

@pytest.mark.asyncio
async def test_create_anfrage_token_stores_email_and_normalized_phone(monkeypatch):
    """create_anfrage_token speichert kunde_email lowercase und
    kunde_telefon normalisiert (Ziffern-only ohne 490-Prefix-0)."""
    from core.integrations import anfrage_forms as af

    inserted = {}

    class _StubSession:
        def add(self, obj):
            inserted["obj"] = obj

        async def commit(self):
            pass

        async def refresh(self, obj):
            # SQLAlchemy-Defaults (token, id) greifen nur bei echtem
            # flush — im Stub manuell setzen damit der logger.info
            # nicht crasht.
            obj.id = uuid.uuid4()
            if obj.token is None:
                obj.token = "stub-token-1234567890"

    @asynccontextmanager
    async def cm():
        yield _StubSession()

    monkeypatch.setattr(af, "AsyncSessionLocal", cm)
    tenant_id = uuid.uuid4()
    token = await af.create_anfrage_token(
        tenant_id=tenant_id,
        kunde_email="MAX.Mueller@Example.DE",
        kunde_name="Max Mueller",
        kunde_telefon="+49 (0) 30 1234 567",
    )
    assert token.kunde_email == "max.mueller@example.de"
    # normalize_phone("+49 (0) 30 1234 567") -> "49301234567"
    assert token.kunde_telefon == "49301234567"


@pytest.mark.asyncio
async def test_create_anfrage_token_without_phone_stays_null(monkeypatch):
    """Mail-Pipeline ruft ohne Telefon — Feld bleibt None, kein Crash."""
    from core.integrations import anfrage_forms as af

    inserted = {}

    class _StubSession:
        def add(self, obj): inserted["obj"] = obj
        async def commit(self): pass
        async def refresh(self, obj):
            obj.id = uuid.uuid4()
            if obj.token is None:
                obj.token = "stub-token-1234567890"

    @asynccontextmanager
    async def cm():
        yield _StubSession()

    monkeypatch.setattr(af, "AsyncSessionLocal", cm)
    token = await af.create_anfrage_token(
        tenant_id=uuid.uuid4(),
        kunde_email="x@y.de",
    )
    assert token.kunde_telefon is None


# =====================================================================
# Test 2: buche_termin findet Token + uebernimmt Mail
# =====================================================================

@pytest.mark.asyncio
async def test_buche_termin_uses_token_email_when_phone_matches(monkeypatch):
    tenant = _make_tenant("demo")
    kalender = _FakeKalender()
    token = _make_token(
        tenant_id=tenant.id, kunde_email="kunde@example.de",
        kunde_telefon="49301234567",
    )
    _patch_buche_termin_deps(
        monkeypatch, tenant=tenant, kalender=kalender, lookup_result=token,
    )

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "22.05.2026|14:00|60",
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 30 1234 567",
        "anliegen": "Beratung",
    })
    assert result["erfolg"] is True
    _, book_payload = kalender.received_payloads[0]
    assert book_payload["kunde_email"] == "kunde@example.de"


# =====================================================================
# Test 3: kein Token-Treffer + kein Payload -> keine kunde_email
# =====================================================================

@pytest.mark.asyncio
async def test_buche_termin_no_token_no_payload_no_email(monkeypatch):
    tenant = _make_tenant("demo")
    kalender = _FakeKalender()
    _patch_buche_termin_deps(
        monkeypatch, tenant=tenant, kalender=kalender, lookup_result=None,
    )

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "22.05.2026|14:00|60",
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 30 1234 567",
        "anliegen": "Beratung",
    })
    assert result["erfolg"] is True
    _, book_payload = kalender.received_payloads[0]
    assert "kunde_email" not in book_payload


# =====================================================================
# Test 4: kein Token + Payload-Mail -> Payload wird genutzt
# =====================================================================

@pytest.mark.asyncio
async def test_buche_termin_payload_email_fallback(monkeypatch):
    tenant = _make_tenant("demo")
    kalender = _FakeKalender()
    _patch_buche_termin_deps(
        monkeypatch, tenant=tenant, kalender=kalender, lookup_result=None,
    )

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "22.05.2026|14:00|60",
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 30 1234 567",
        "kunde_email": "from-payload@example.de",
        "anliegen": "Beratung",
    })
    assert result["erfolg"] is True
    _, book_payload = kalender.received_payloads[0]
    assert book_payload["kunde_email"] == "from-payload@example.de"


# =====================================================================
# Test 5: Token + Payload BEIDE -> Token gewinnt (single source of truth)
# =====================================================================

@pytest.mark.asyncio
async def test_buche_termin_token_wins_over_payload(monkeypatch):
    tenant = _make_tenant("demo")
    kalender = _FakeKalender()
    token = _make_token(
        tenant_id=tenant.id,
        kunde_email="from-token@example.de",
        kunde_telefon="49301234567",
    )
    _patch_buche_termin_deps(
        monkeypatch, tenant=tenant, kalender=kalender, lookup_result=token,
    )

    plugin = _make_plugin()
    result = await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "22.05.2026|14:00|60",
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 30 1234 567",
        "kunde_email": "from-payload@example.de",
        "anliegen": "Beratung",
    })
    assert result["erfolg"] is True
    _, book_payload = kalender.received_payloads[0]
    # Single Source of Truth: Token-Mail gewinnt
    assert book_payload["kunde_email"] == "from-token@example.de"


# =====================================================================
# Test 6: Token-Lookup respektiert max_age_seconds (Cutoff-Test)
# =====================================================================

@pytest.mark.asyncio
async def test_lookup_recent_anfrage_returns_none_for_empty_phone():
    """Edge: leere Telefonnummer -> kein DB-Hit (Short-Circuit)."""
    from core.integrations.anfrage_forms import lookup_recent_anfrage_by_phone
    result = await lookup_recent_anfrage_by_phone(uuid.uuid4(), "")
    assert result is None


@pytest.mark.asyncio
async def test_lookup_recent_anfrage_uses_cutoff(monkeypatch):
    """Cutoff wird beim Query angewendet: Wenn die Session keinen Treffer
    liefert (= simuliert: alle Tokens sind zu alt / Tabelle leer), gibt
    der Helper None zurueck. Wir verifizieren zusaetzlich dass der
    SELECT-Statement ueberhaupt ausgefuehrt wird (= Cutoff-Pfad
    aktiviert, nicht der phone='' Short-Circuit)."""
    from core.integrations import anfrage_forms as af
    executed: list = []

    class _S:
        async def execute(self, stmt):
            executed.append(stmt)
            return _FakeResult(None)  # simuliert: kein Treffer im Window

    @asynccontextmanager
    async def cm():
        yield _S()

    monkeypatch.setattr(af, "AsyncSessionLocal", cm)
    result = await af.lookup_recent_anfrage_by_phone(
        uuid.uuid4(), "49301234567", max_age_seconds=60,
    )
    assert result is None
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_buche_termin_falls_back_when_lookup_returns_none(monkeypatch):
    """Token expired/missing -> lookup returns None -> Payload-Fallback."""
    tenant = _make_tenant("demo")
    kalender = _FakeKalender()
    _patch_buche_termin_deps(
        monkeypatch, tenant=tenant, kalender=kalender, lookup_result=None,
    )

    plugin = _make_plugin()
    await plugin._handle_buche_termin({
        "tenant_slug": "demo",
        "slot_id": "22.05.2026|14:00|60",
        "kunde_name": "Frau Mueller",
        "kunde_telefon": "+49 30 1234 567",
        "kunde_email": "fallback@example.de",
        "anliegen": "Beratung",
    })
    _, book_payload = kalender.received_payloads[0]
    assert book_payload["kunde_email"] == "fallback@example.de"


# =====================================================================
# Test 7: Integration — kunde_email landet als extendedProperty
# =====================================================================

@pytest.mark.asyncio
async def test_kalender_book_appointment_passes_email_to_adapter(monkeypatch):
    """End-to-end: kalender._book_appointment ruft adapter.create_event
    mit kunde_email durch — Adapter setzt das spaeter als Google
    extendedProperty.private bzw. Microsoft singleValueExtendedProperty
    (siehe Tests in test_storno.py)."""
    from plugins.kalender import handler as kalender_handler

    received_create_event: dict = {}

    class _FakeAdapter:
        provider_name = "google"

        async def is_slot_busy(self, *a, **kw):
            return False  # Slot ist frei

        async def create_event(self, **kwargs):
            received_create_event.update(kwargs)
            return {"id": "evt-fake", "html_link": "http://x"}

    async def fake_get_adapter(*a, **kw):
        return _FakeAdapter()

    monkeypatch.setattr(kalender_handler, "get_calendar_adapter", fake_get_adapter)
    # Telegram-Push silent
    from plugins.telegram_notify import handler as tn
    monkeypatch.setattr(tn.TelegramNotifier, "send_for_employee", AsyncMock())

    context = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        config={
            "calendar_id": "primary",
            "betrieb_name": "Test",
            "termin_dauer_minuten": 60,
            "zeitzone": "Europe/Berlin",
        },
    )
    plugin = kalender_handler.Plugin(context)
    result = await plugin._book_appointment({
        "name": "Frau Mueller",
        "anliegen": "Beratung",
        "datum": "22.05.2026",
        "uhrzeit": "14:00",
        "telefon": "+49 30 1234 567",
        "kunde_email": "ICH@EXAMPLE.de",
    })
    assert result["erfolg"] is True
    # Adapter hat normalisierte Mail + normalisiertes Telefon bekommen
    assert received_create_event["kunde_email"] == "ich@example.de"
    assert received_create_event["kunde_telefon_normalized"] == "49301234567"
