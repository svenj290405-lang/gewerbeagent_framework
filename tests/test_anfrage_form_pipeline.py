"""Tests fuer den Anfrage-Form-Pipeline-Flow.

Voice/Mail erstellt einen AnfrageToken -> Kunde fuellt Formular aus
-> submit_anfrage validiert + speichert.

Deckt die Edges des Submit-Pfads:
- happy path: gueltiger Token -> success
- doppeltes Submit: zweiter Aufruf -> "Schon abgesendet"
- abgelaufener Token: -> "Token abgelaufen"
- unbekannter Token: -> "Token unbekannt"

Sowie den Validierungs-Pfad (get_token_with_tenant) der vom Web-View
beim Rendern des Formulars benutzt wird:
- gueltiger Token + Tenant gefunden -> (token, tenant)
- abgelaufen -> (None, None)
- schon submitted -> (token, None) [signalisiert: nicht mehr renderbar]
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.integrations import anfrage_forms as af


# =====================================================================
# Test-Doubles
# =====================================================================

class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _session_factory(results, *, capture=None):
    """Liefert pro execute() das naechste Result aus der Queue.

    capture (optional dict): wird mit "added" + "committed" befuellt
    zum Verifizieren was im Session-Lifecycle passiert ist.
    """
    queue = list(results)
    captured = capture if capture is not None else {}
    captured.setdefault("added", [])
    captured["commits"] = 0

    class _S:
        async def execute(self, _stmt):
            return _FakeResult(queue.pop(0) if queue else None)

        def add(self, obj):
            captured["added"].append(obj)

        async def commit(self):
            captured["commits"] += 1

        async def refresh(self, obj):
            pass

    @asynccontextmanager
    async def cm():
        yield _S()

    return cm, captured


def _make_token(
    *,
    token_str: str = "tok-abc-1234567890",
    submitted: bool = False,
    expires_in_seconds: int = 3 * 86400,
    tenant_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        token=token_str,
        tenant_id=tenant_id or uuid.uuid4(),
        kunde_email="kunde@example.de",
        kunde_name="Max Kunde",
        kunde_telefon=None,
        submitted_at=(
            dt.datetime.now(dt.timezone.utc) if submitted else None
        ),
        expires_at=(
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=expires_in_seconds)
        ),
    )


# =====================================================================
# submit_anfrage
# =====================================================================

@pytest.mark.asyncio
async def test_submit_anfrage_happy_path(monkeypatch):
    """Gueltiger Token + Antworten -> AnfrageResponse erzeugt,
    submitted_at gesetzt, Return (True, 'OK')."""
    tok = _make_token()
    factory, capture = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)

    ok, msg = await af.submit_anfrage(
        token_str=tok.token,
        antworten={"produkt": "Schrank", "beschreibung": "modern"},
        submitted_ip="1.2.3.4",
    )
    assert ok is True
    assert msg == "OK"
    assert len(capture["added"]) == 1
    response = capture["added"][0]
    assert response.antworten == {"produkt": "Schrank", "beschreibung": "modern"}
    assert response.submitted_ip == "1.2.3.4"
    assert capture["commits"] == 1
    # Token wurde als submitted markiert
    assert tok.submitted_at is not None


@pytest.mark.asyncio
async def test_submit_anfrage_unknown_token(monkeypatch):
    factory, capture = _session_factory([None])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    ok, msg = await af.submit_anfrage("does-not-exist", {})
    assert ok is False
    assert msg == "Token unbekannt"
    assert capture["added"] == []


@pytest.mark.asyncio
async def test_submit_anfrage_already_submitted(monkeypatch):
    """Zweiter Submit-Aufruf auf gleichen Token -> 'Schon abgesendet'."""
    tok = _make_token(submitted=True)
    factory, capture = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    ok, msg = await af.submit_anfrage(tok.token, {"produkt": "x"})
    assert ok is False
    assert msg == "Schon abgesendet"
    assert capture["added"] == []


@pytest.mark.asyncio
async def test_submit_anfrage_expired_token(monkeypatch):
    """Token mit expires_at in der Vergangenheit -> 'Token abgelaufen'."""
    tok = _make_token(expires_in_seconds=-3600)
    factory, capture = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    ok, msg = await af.submit_anfrage(tok.token, {"produkt": "x"})
    assert ok is False
    assert msg == "Token abgelaufen"
    assert capture["added"] == []


@pytest.mark.asyncio
async def test_submit_anfrage_truncates_long_ip(monkeypatch):
    """submitted_ip wird auf 50 Zeichen geclamped (DB-Column-Limit-Schutz)."""
    tok = _make_token()
    factory, capture = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    long_ip = "a" * 200
    ok, _ = await af.submit_anfrage(tok.token, {"x": "y"}, submitted_ip=long_ip)
    assert ok is True
    assert len(capture["added"][0].submitted_ip) == 50


# =====================================================================
# get_token_with_tenant (Web-View-Pfad: Render-Zeit)
# =====================================================================

@pytest.mark.asyncio
async def test_get_token_with_tenant_valid(monkeypatch):
    """Gueltiger Token + Tenant existiert -> (token, tenant)."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    tok = _make_token(tenant_id=tenant.id)
    # Erste execute() -> Token, zweite -> Tenant
    factory, _ = _session_factory([tok, tenant])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)

    got_tok, got_tenant = await af.get_token_with_tenant(tok.token)
    assert got_tok is tok
    assert got_tenant is tenant


@pytest.mark.asyncio
async def test_get_token_with_tenant_unknown(monkeypatch):
    """Token unbekannt -> (None, None)."""
    factory, _ = _session_factory([None])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    got_tok, got_tenant = await af.get_token_with_tenant("nope")
    assert got_tok is None
    assert got_tenant is None


@pytest.mark.asyncio
async def test_get_token_with_tenant_expired(monkeypatch):
    """Abgelaufener Token -> (None, None) — Web-View zeigt 410."""
    tok = _make_token(expires_in_seconds=-1)
    factory, _ = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    got_tok, got_tenant = await af.get_token_with_tenant(tok.token)
    assert got_tok is None
    assert got_tenant is None


@pytest.mark.asyncio
async def test_get_token_with_tenant_submitted_returns_token_no_tenant(monkeypatch):
    """Schon abgesendet -> (token, None) — Web-View kann 'Schon ausgefuellt'
    anzeigen statt 404."""
    tok = _make_token(submitted=True)
    factory, _ = _session_factory([tok])
    monkeypatch.setattr(af, "AsyncSessionLocal", factory)
    got_tok, got_tenant = await af.get_token_with_tenant(tok.token)
    assert got_tok is tok
    assert got_tenant is None


# =====================================================================
# build_anfrage_url
# =====================================================================

def test_build_anfrage_url_uses_public_url():
    url = af.build_anfrage_url("tok-xyz")
    assert url.endswith("/anfrage/tok-xyz")
    assert "//" in url  # http(s)://...
