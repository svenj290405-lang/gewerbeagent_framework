"""Tests fuer das Storno-System.

Deckt:
- Phone-Normalisierung + Suffix-Match-Key
- Fulltext-Verifier (phone + email)
- Stornier-Token-Cache Lifecycle (create, consume, expired, doppelt, wrong-tenant)
- kalender._find_events Multi-Mitarbeiter-Aggregation + Dedup
- voice _handle_finde_termine / _handle_storniere_termin Roundtrip
- mail _resolve_and_cancel_storno_events (find first, fallback conv)
- Rueckwaerts-Kompat: Legacy-Event ohne Metadaten findbar via Fulltext

Keine echte DB/HTTP — Adapter und Plugin-Lookup werden gemockt.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.utils.phone import normalize_phone, phone_match_key
from plugins.kalender.event_match import (
    verify_fulltext_email_match,
    verify_fulltext_phone_match,
)


# =====================================================================
# Phone-Normalisierung
# =====================================================================

def test_phone_normalize_plus_country():
    assert normalize_phone("+49 30 1234") == "49301234"


def test_phone_normalize_double_zero_country():
    assert normalize_phone("0049 30 1234 56") == "4930123456"


def test_phone_normalize_strips_paren_zero():
    assert normalize_phone("+49 (0) 30 1234") == "49301234"


def test_phone_normalize_empty_inputs():
    assert normalize_phone(None) == ""
    assert normalize_phone("") == ""
    assert normalize_phone("---/...") == ""


def test_phone_match_key_long_returns_last_eight():
    assert phone_match_key("4930123456789") == "23456789"


def test_phone_match_key_short_passthrough():
    assert phone_match_key("123") == "123"


# =====================================================================
# Fulltext-Verifier
# =====================================================================

def test_verify_phone_match_via_telefon_line():
    desc = (
        "Betrieb: Tischlerei X\n"
        "Kunde: Mueller\n"
        "Telefon: +49 30 1234 56\n"
        "Adresse: Hauptstr 1"
    )
    assert verify_fulltext_phone_match("4930123456", desc) is True


def test_verify_phone_match_via_suffix_fallback():
    """Kein 'Telefon:'-Zeile aber Nummer steht irgendwo in description -> Suffix-Match.

    Suffix-Fallback ist absichtlich generoes (matched die letzten 8
    Ziffern als Substring im normalisierten description-Text). Damit
    matcht "+49 30 1234 5678" auch gegen "030 1234 5678" im Freitext.
    """
    desc = "Kunde rief unter 030 1234 5678 an, will Beratung."
    # Suffix-Match: last8 von needle "493012345678" = "12345678"
    # full digits in desc = "03012345678" — enthaelt "12345678" → True
    assert verify_fulltext_phone_match("493012345678", desc) is True


def test_verify_phone_no_suffix_match_when_only_partial():
    """Wenn Suffix-Match-Key (8 Ziffern) gar nicht in description vorkommt -> False."""
    desc = "Adresse Hauptstr 5, 10115 Berlin"
    # last8 = "99999999" → nirgends in description
    assert verify_fulltext_phone_match("4930999999999", desc) is False


def test_verify_phone_no_match_returns_false():
    desc = "Kunde: Mueller\nTelefon: 0211 9999"
    assert verify_fulltext_phone_match("4930123456", desc) is False


def test_verify_email_match_substring():
    desc = "Kunde: a@b.de hat angefragt"
    assert verify_fulltext_email_match("a@b.de", desc) is True


def test_verify_email_no_match():
    assert verify_fulltext_email_match("x@y.de", "kein mail hier") is False


# =====================================================================
# Stornier-Token-Cache
# =====================================================================

@pytest.fixture(autouse=True)
def _reset_storno_tokens():
    """Stellt sicher dass der globale Token-Cache pro Test leer ist."""
    from plugins.voice_init import handler as voice_handler
    voice_handler._STORNIER_TOKENS.clear()
    yield
    voice_handler._STORNIER_TOKENS.clear()


def test_storno_token_create_and_consume():
    from plugins.voice_init import handler as voice_handler
    tenant_id = uuid.uuid4()
    tok = voice_handler._create_stornier_token(tenant_id, "evt-1", "emp-a")
    assert isinstance(tok, str) and len(tok) > 10
    entry = voice_handler._consume_stornier_token(tok, tenant_id)
    assert entry is not None
    assert entry["event_id"] == "evt-1"
    assert entry["employee_id"] == "emp-a"


def test_storno_token_double_consume_fails():
    from plugins.voice_init import handler as voice_handler
    tenant_id = uuid.uuid4()
    tok = voice_handler._create_stornier_token(tenant_id, "evt-1", None)
    assert voice_handler._consume_stornier_token(tok, tenant_id) is not None
    assert voice_handler._consume_stornier_token(tok, tenant_id) is None


def test_storno_token_wrong_tenant_fails():
    from plugins.voice_init import handler as voice_handler
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    tok = voice_handler._create_stornier_token(tenant_a, "evt-1", None)
    assert voice_handler._consume_stornier_token(tok, tenant_b) is None
    # Original-Tenant kann immer noch (Token war NICHT als used markiert
    # wenn tenant-mismatch — siehe _consume_stornier_token).
    assert voice_handler._consume_stornier_token(tok, tenant_a) is not None


def test_storno_token_unknown_fails():
    from plugins.voice_init import handler as voice_handler
    assert voice_handler._consume_stornier_token("garbage", uuid.uuid4()) is None


def test_storno_token_expired_fails():
    from plugins.voice_init import handler as voice_handler
    tenant_id = uuid.uuid4()
    tok = voice_handler._create_stornier_token(tenant_id, "evt-x", None)
    # Manuell altern: created_at >TTL in die Vergangenheit setzen
    voice_handler._STORNIER_TOKENS[tok]["created_at"] = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(seconds=voice_handler.STORNIER_TOKEN_TTL_SECONDS + 60)
    )
    assert voice_handler._consume_stornier_token(tok, tenant_id) is None


# =====================================================================
# Test-Doubles fuer Plugin-Tests
# =====================================================================

class _FakeAdapter:
    """Erfuellt das CalendarAdapter-Interface fuer find_events-Tests.
    Konstruktor nimmt die Liste der Treffer die find_events liefern soll."""

    def __init__(self, find_result):
        self._find_result = find_result
        self.received_find_args = None

    async def find_events(self, *, time_min, time_max,
                          kunde_telefon_normalized=None, kunde_email=None):
        self.received_find_args = {
            "time_min": time_min, "time_max": time_max,
            "kunde_telefon_normalized": kunde_telefon_normalized,
            "kunde_email": kunde_email,
        }
        return list(self._find_result)


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _session_factory(results):
    shared = list(results)

    class _S:
        async def execute(self, _stmt):
            return _FakeResult(shared.pop(0) if shared else None)

    @asynccontextmanager
    async def cm():
        yield _S()

    return cm


def _make_employee(slug="emp", is_default=False):
    return SimpleNamespace(
        id=uuid.uuid4(), slug=slug, is_default=is_default,
        is_active=True,
    )


def _make_kalender_plugin():
    from plugins.kalender import handler as kalender_handler
    context = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        config={
            "calendar_id": "primary",
            "betrieb_name": "Test-Betrieb",
            "termin_dauer_minuten": 60,
            "arbeitszeiten_start": "08:00",
            "arbeitszeiten_ende": "17:00",
            "arbeitstage": [0, 1, 2, 3, 4],
            "zeitzone": "Europe/Berlin",
        },
    )
    return kalender_handler.Plugin(context)


# =====================================================================
# kalender._find_events — Plugin-Endpoint
# =====================================================================

@pytest.mark.asyncio
async def test_find_events_requires_phone_or_email():
    plugin = _make_kalender_plugin()
    res = await plugin._find_events({})
    assert res["erfolg"] is False
    assert "erforderlich" in res["nachricht"].lower()


@pytest.mark.asyncio
async def test_find_events_empty_when_no_employees(monkeypatch):
    from core.models import employee as emp_module
    from plugins.kalender import handler as kalender_handler
    monkeypatch.setattr(
        emp_module, "get_employees_for_tenant",
        AsyncMock(return_value=[]),
    )
    # auch im handler-Modul re-bound (handler importiert lokal innerhalb
    # der Funktion, also reicht Patch auf core.models.employee)
    plugin = _make_kalender_plugin()
    res = await plugin._find_events({"kunde_email": "x@y.de"})
    assert res == {"erfolg": True, "anzahl": 0, "termine": []}


@pytest.mark.asyncio
async def test_find_events_dedups_across_employees(monkeypatch):
    """Wenn zwei Mitarbeiter-Adapter den gleichen event_id liefern (selten,
    aber moeglich bei geteilten Kalendern), darf der nur 1x erscheinen."""
    from plugins.kalender import handler as kalender_handler
    from core.models import employee as emp_module

    emp1 = _make_employee("daniel", is_default=True)
    emp2 = _make_employee("max")
    monkeypatch.setattr(
        emp_module, "get_employees_for_tenant",
        AsyncMock(return_value=[emp1, emp2]),
    )

    base_ev = {
        "event_id": "evt-shared", "summary": "Beratung",
        "description": "...", "location": "Musterstr",
        "kunde_telefon_match": True, "kunde_email_match": False,
        "match_source": "metadata",
        "start_dt": dt.datetime(2026, 6, 1, 10, 0),
        "end_dt": dt.datetime(2026, 6, 1, 11, 0),
    }
    adapter1 = _FakeAdapter([base_ev])
    adapter2 = _FakeAdapter([base_ev])
    factory_calls = []

    async def fake_get_adapter(tenant_id, employee_id=None, fallback_calendar_id=None):
        factory_calls.append(employee_id)
        return adapter1 if employee_id == emp1.id else adapter2

    monkeypatch.setattr(kalender_handler, "get_calendar_adapter", fake_get_adapter)
    plugin = _make_kalender_plugin()

    res = await plugin._find_events({"kunde_telefon": "+49 30 1234"})
    assert res["erfolg"] is True
    assert res["anzahl"] == 1
    assert res["termine"][0]["event_id"] == "evt-shared"
    # Beide Adapter wurden bemueht
    assert emp1.id in factory_calls and emp2.id in factory_calls


@pytest.mark.asyncio
async def test_find_events_sorts_chronologically(monkeypatch):
    from plugins.kalender import handler as kalender_handler
    from core.models import employee as emp_module

    emp = _make_employee("daniel", is_default=True)
    monkeypatch.setattr(
        emp_module, "get_employees_for_tenant",
        AsyncMock(return_value=[emp]),
    )

    later = {
        "event_id": "later",
        "start_dt": dt.datetime(2026, 7, 5, 9, 0),
        "end_dt": dt.datetime(2026, 7, 5, 10, 0),
        "summary": "spaeter", "description": "", "location": "",
        "kunde_telefon_match": False, "kunde_email_match": True,
        "match_source": "metadata",
    }
    earlier = {
        "event_id": "earlier",
        "start_dt": dt.datetime(2026, 6, 1, 9, 0),
        "end_dt": dt.datetime(2026, 6, 1, 10, 0),
        "summary": "frueher", "description": "", "location": "",
        "kunde_telefon_match": False, "kunde_email_match": True,
        "match_source": "metadata",
    }
    adapter = _FakeAdapter([later, earlier])

    async def fake_get_adapter(*a, **kw):
        return adapter
    monkeypatch.setattr(kalender_handler, "get_calendar_adapter", fake_get_adapter)

    plugin = _make_kalender_plugin()
    res = await plugin._find_events({"kunde_email": "x@y.de"})
    assert res["anzahl"] == 2
    assert res["termine"][0]["event_id"] == "earlier"
    assert res["termine"][1]["event_id"] == "later"


@pytest.mark.asyncio
async def test_find_events_passes_normalized_phone_to_adapter(monkeypatch):
    """Telefon roh in Payload, normalized an Adapter."""
    from plugins.kalender import handler as kalender_handler
    from core.models import employee as emp_module

    emp = _make_employee("daniel", is_default=True)
    monkeypatch.setattr(
        emp_module, "get_employees_for_tenant",
        AsyncMock(return_value=[emp]),
    )
    adapter = _FakeAdapter([])

    async def fake_get_adapter(*a, **kw):
        return adapter
    monkeypatch.setattr(kalender_handler, "get_calendar_adapter", fake_get_adapter)

    plugin = _make_kalender_plugin()
    await plugin._find_events({"kunde_telefon": "+49 (0) 30 1234"})
    assert adapter.received_find_args["kunde_telefon_normalized"] == "49301234"
    assert adapter.received_find_args["kunde_email"] is None


# =====================================================================
# mail _resolve_and_cancel_storno_events
# =====================================================================

class _FakeKalenderPlugin:
    """Mock fuer get_plugin_for_tenant("kalender") in mail-Tests."""
    def __init__(self, find_result, cancel_ok=True):
        self.find_result = find_result
        self.cancel_ok = cancel_ok
        self.cancelled_ids = []
        self.find_calls = 0

    async def on_webhook(self, endpoint, payload):
        if endpoint == "find_events":
            self.find_calls += 1
            return self.find_result
        if endpoint == "cancel_appointment":
            self.cancelled_ids.append(payload.get("event_id"))
            return {"erfolg": self.cancel_ok}
        return {}


def _make_mail_plugin():
    from plugins.mail_intake import handler as mail_handler
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return mail_handler.Plugin(context)


@pytest.mark.asyncio
async def test_mail_storno_uses_find_events_first(monkeypatch):
    """find_events liefert Treffer -> die werden geloescht, conv-fallback bleibt unangetastet."""
    import core.plugin_system as ps
    kalender = _FakeKalenderPlugin(find_result={
        "erfolg": True, "anzahl": 2,
        "termine": [
            {"event_id": "evt-1"},
            {"event_id": "evt-2"},
        ],
    })
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_mail_plugin()
    tenant = SimpleNamespace(slug="demo")
    ids = await plugin._resolve_and_cancel_storno_events(
        tenant, "kunde@x.de", conv_event_id="evt-conv-fallback",
    )
    # conv-fallback geht NUR rein wenn er noch nicht via find_events erfasst war.
    # evt-conv-fallback war nicht in find_result -> wird zusaetzlich geloescht.
    assert ids == ["evt-1", "evt-2", "evt-conv-fallback"]
    assert kalender.cancelled_ids == ["evt-1", "evt-2", "evt-conv-fallback"]


@pytest.mark.asyncio
async def test_mail_storno_falls_back_to_conv_when_find_empty(monkeypatch):
    """find_events leer -> conv.gcal_event_id wird genutzt."""
    import core.plugin_system as ps
    kalender = _FakeKalenderPlugin(find_result={
        "erfolg": True, "anzahl": 0, "termine": [],
    })
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_mail_plugin()
    tenant = SimpleNamespace(slug="demo")
    ids = await plugin._resolve_and_cancel_storno_events(
        tenant, "kunde@x.de", conv_event_id="evt-legacy",
    )
    assert ids == ["evt-legacy"]
    assert kalender.cancelled_ids == ["evt-legacy"]


@pytest.mark.asyncio
async def test_mail_storno_no_events_at_all(monkeypatch):
    """Kein find-Treffer und kein conv -> leere Liste, kein crash."""
    import core.plugin_system as ps
    kalender = _FakeKalenderPlugin(find_result={
        "erfolg": True, "anzahl": 0, "termine": [],
    })
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_mail_plugin()
    tenant = SimpleNamespace(slug="demo")
    ids = await plugin._resolve_and_cancel_storno_events(
        tenant, "kunde@x.de", conv_event_id=None,
    )
    assert ids == []
    assert kalender.cancelled_ids == []


@pytest.mark.asyncio
async def test_mail_storno_skips_duplicate_conv_id(monkeypatch):
    """conv.gcal_event_id == ein find_events-Treffer -> nicht doppelt loeschen."""
    import core.plugin_system as ps
    kalender = _FakeKalenderPlugin(find_result={
        "erfolg": True, "anzahl": 1,
        "termine": [{"event_id": "evt-1"}],
    })
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_mail_plugin()
    tenant = SimpleNamespace(slug="demo")
    ids = await plugin._resolve_and_cancel_storno_events(
        tenant, "kunde@x.de", conv_event_id="evt-1",
    )
    assert ids == ["evt-1"]
    assert kalender.cancelled_ids == ["evt-1"]  # nur einmal!


# =====================================================================
# voice _handle_finde_termine / _handle_storniere_termin
# =====================================================================

class _FakeVoiceKalender:
    """Stub-Kalender fuer voice-Storno-Tests."""
    def __init__(self, find_result=None, cancel_ok=True):
        self.find_result = find_result or {"erfolg": True, "anzahl": 0, "termine": []}
        self.cancel_ok = cancel_ok
        self.cancelled = []

    async def on_webhook(self, endpoint, payload):
        if endpoint == "find_events":
            return self.find_result
        if endpoint == "cancel_appointment":
            self.cancelled.append(payload)
            return {"erfolg": self.cancel_ok, "event_id": payload.get("event_id")}
        return {}


def _make_voice_plugin():
    from plugins.voice_init import handler as voice_handler
    context = SimpleNamespace(tenant_id=uuid.uuid4(), config={})
    return voice_handler.Plugin(context)


@pytest.mark.asyncio
async def test_voice_finde_termine_creates_tokens(monkeypatch):
    """finde_termine: pro Event ein Stornier-Token + voice-freundliche Felder."""
    from plugins.voice_init import handler as voice_handler
    import core.plugin_system as ps

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    kalender = _FakeVoiceKalender(find_result={
        "erfolg": True, "anzahl": 1,
        "termine": [{
            "event_id": "evt-abc",
            "employee_id": "emp-1",
            "employee_slug": "daniel",
            "start_dt": dt.datetime(2026, 5, 22, 14, 0).isoformat(),
            "end_dt": dt.datetime(2026, 5, 22, 15, 0).isoformat(),
            "summary": "Kuechenberatung",
            "description": "...",
            "location": "Musterstr 1",
            "kunde_telefon_match": True,
            "kunde_email_match": False,
            "match_source": "metadata",
        }],
    })
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal", _session_factory([tenant]),
    )
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_voice_plugin()
    res = await plugin._handle_finde_termine({
        "tenant_slug": "demo",
        "kunde_telefon": "+49 30 1234",
    })
    assert res["erfolg"] is True
    assert res["anzahl"] == 1
    t = res["termine"][0]
    assert t["datum"] == "22.05.2026"
    assert t["wochentag"] == "Fr"
    assert t["uhrzeit"] == "14:00"
    assert t["anliegen"] == "Kuechenberatung"
    assert t["ort"] == "Musterstr 1"
    # Token muss erzeugt sein und auf das Event zeigen
    tok = t["stornier_token"]
    assert tok in voice_handler._STORNIER_TOKENS
    assert voice_handler._STORNIER_TOKENS[tok]["event_id"] == "evt-abc"


@pytest.mark.asyncio
async def test_voice_finde_termine_requires_one_query(monkeypatch):
    plugin = _make_voice_plugin()
    res = await plugin._handle_finde_termine({"tenant_slug": "demo"})
    assert res["erfolg"] is False


@pytest.mark.asyncio
async def test_voice_storniere_termin_with_valid_token(monkeypatch):
    """Roundtrip: finde_termine -> storniere_termin loescht den richtigen Event."""
    from plugins.voice_init import handler as voice_handler
    import core.plugin_system as ps

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    kalender = _FakeVoiceKalender(find_result={
        "erfolg": True, "anzahl": 1,
        "termine": [{
            "event_id": "evt-xyz",
            "employee_id": str(uuid.uuid4()),
            "employee_slug": "daniel",
            "start_dt": dt.datetime(2026, 5, 22, 14, 0).isoformat(),
            "end_dt": dt.datetime(2026, 5, 22, 15, 0).isoformat(),
            "summary": "X", "description": "", "location": "",
            "kunde_telefon_match": True, "kunde_email_match": False,
            "match_source": "metadata",
        }],
    })
    # Eine Session reicht NICHT — finde_termine UND storniere_termin
    # rufen jeweils select(Tenant). Wir geben Tenant zweimal raus.
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal",
        _session_factory([tenant, tenant]),
    )
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))
    # Telegram-Push silent-fail tolerieren
    from plugins.telegram_notify import handler as tn
    monkeypatch.setattr(tn.TelegramNotifier, "send_for_employee", AsyncMock())

    plugin = _make_voice_plugin()
    finde_res = await plugin._handle_finde_termine({
        "tenant_slug": "demo", "kunde_telefon": "+49 30 1234",
    })
    tok = finde_res["termine"][0]["stornier_token"]

    cancel_res = await plugin._handle_storniere_termin({
        "tenant_slug": "demo", "stornier_token": tok,
    })
    assert cancel_res["erfolg"] is True
    assert kalender.cancelled[0]["event_id"] == "evt-xyz"
    # Token darf nicht zweimal funktionieren
    cancel_res2 = await plugin._handle_storniere_termin({
        "tenant_slug": "demo", "stornier_token": tok,
    })
    assert cancel_res2["erfolg"] is False


@pytest.mark.asyncio
async def test_voice_storniere_termin_unknown_token(monkeypatch):
    """Unbekannter/ungueltiger Token -> generic error, kein cancel-call."""
    from plugins.voice_init import handler as voice_handler
    import core.plugin_system as ps

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    kalender = _FakeVoiceKalender()
    monkeypatch.setattr(
        voice_handler, "AsyncSessionLocal", _session_factory([tenant]),
    )
    monkeypatch.setattr(ps, "get_plugin_for_tenant", AsyncMock(return_value=kalender))

    plugin = _make_voice_plugin()
    res = await plugin._handle_storniere_termin({
        "tenant_slug": "demo", "stornier_token": "definitiv-nicht-existent",
    })
    assert res["erfolg"] is False
    assert kalender.cancelled == []


# =====================================================================
# Rueckwaertskompat: Legacy-Event ohne Metadaten kommt via Fulltext-Verifier
# =====================================================================

def test_legacy_event_findable_via_fulltext_verifier():
    """Bestands-Event-description (vor Metadaten-Phase) -> Verifier matched."""
    legacy_desc = (
        "Betrieb: Tischlerei X\n"
        "Kunde: Frau Mueller\n"
        "Anliegen: Kuechenmontage\n"
        "Adresse: Musterstr 1\n"
        "Telefon: +49 30 9876 54\n\n"
        "Eingetragen via KI-Agent Q (Gewerbeagent Framework)"
    )
    # User stoniert mit Telefon "030 9876 54" -> normalize -> "030987654",
    # passt nicht auf die Telefon-Zeile "+49 30 9876 54" -> "4930987654".
    # Suffix-Match: last8 von "030987654" = "30987654" → in "4930987654"
    # vorhanden. Daher TRUE.
    assert verify_fulltext_phone_match("030987654", legacy_desc) is True
    # Mail-Verifier: keine Mail in description -> False
    assert verify_fulltext_email_match("kunde@x.de", legacy_desc) is False
