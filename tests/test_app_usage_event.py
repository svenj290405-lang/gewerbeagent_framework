"""Tests fuer das App-Nutzungs-Tracking (core/models/app_usage_event.py)
+ die Dashboard-Verdrahtung.

DB wird gemockt (Konvention der uebrigen App-Tests).
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.models.app_usage_event as aue


# --------------------------------------------------------------------------
# record_app_usage — Failsafe-Guards (kein DB-Zugriff bei ungueltiger Eingabe)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_app_usage_ignores_missing_tenant(monkeypatch):
    called = {"db": False}
    import core.database as db
    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: (_ for _ in ()).throw(AssertionError("DB nicht erwartet")))
    # tenant_id=None -> sofortiger Return, KEIN DB-Zugriff
    await aue.record_app_usage(None, uuid.uuid4(), aue.USAGE_LOGIN)
    assert called["db"] is False


@pytest.mark.asyncio
async def test_record_app_usage_ignores_unknown_kind(monkeypatch):
    import core.database as db
    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: (_ for _ in ()).throw(AssertionError("DB nicht erwartet")))
    await aue.record_app_usage(uuid.uuid4(), uuid.uuid4(), "voellig_unbekannt")


@pytest.mark.asyncio
async def test_record_app_usage_swallows_db_errors(monkeypatch):
    """Tracking darf den Request nie brechen — DB-Fehler werden geschluckt."""
    import core.database as db

    @asynccontextmanager
    async def _boom():
        raise RuntimeError("db weg")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: _boom())
    # darf NICHT raisen
    await aue.record_app_usage(uuid.uuid4(), uuid.uuid4(), aue.USAGE_LOGIN)


# --------------------------------------------------------------------------
# usage_counts_by_employee — Aggregations-Form
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_counts_shaping(monkeypatch):
    e1, e2 = uuid.uuid4(), uuid.uuid4()
    rows = [
        (e1, aue.USAGE_LOGIN, 3),
        (e1, aue.USAGE_DIKTAT, 2),
        (e2, aue.USAGE_ASSISTENT_BEFEHL, 5),
    ]

    class _S:
        async def execute(self, stmt):
            return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def _gs():
        yield _S()

    import core.database as db
    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: _gs())

    out = await aue.usage_counts_by_employee(
        uuid.uuid4(), since=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert out[str(e1)][aue.USAGE_LOGIN] == 3
    assert out[str(e1)][aue.USAGE_DIKTAT] == 2
    assert out[str(e2)][aue.USAGE_ASSISTENT_BEFEHL] == 5


# --------------------------------------------------------------------------
# Dashboard-Verdrahtung + Routen
# --------------------------------------------------------------------------

def test_metric_columns_include_app_usage():
    from core.admin.routes import _METRIC_COLUMNS, _METRIC_GROUPS
    keys = {k for k, _h, _f in _METRIC_COLUMNS}
    assert "app_logins" in keys
    assert "assistent_befehle" in keys
    group_titles = {t for t, _ in _METRIC_GROUPS}
    assert "App & Assistent" in group_titles


def test_activation_routes_registered():
    from core.api.app import app
    paths = {r.path for r in app.routes}
    assert "/app/activate" in paths
    assert "/app/activate/info" in paths
    assert "/app/activate/setzen" in paths
