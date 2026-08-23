"""Tests fuer den Datenexport (GET /app/api/einstellungen/export).

Die Website verspricht in den haeufigen Fragen: "Du bekommst alle deine
Daten als Export — Anfragen, Briefings, Belege, alles." Bis zum Audit am
2026-08-23 gab es dafuer nichts. Diese Tests halten die zwei Dinge fest,
die dabei wirklich zaehlen: es kommt etwas Brauchbares heraus, und es
sind ausschliesslich die Daten des eigenen Betriebs.
"""
from __future__ import annotations

import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from core.api import app_screens


class _FakeResult:
    def __init__(self, zeilen):
        self._zeilen = zeilen

    def all(self):
        return self._zeilen


class _FakeSession:
    """Gibt fuer jede Tabelle dieselben zwei Zeilen zurueck und merkt sich,
    ob wirklich nach tenant_id gefiltert wurde."""

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.abfragen = 0
        self.ungefiltert = []

    async def execute(self, stmt):
        self.abfragen += 1
        text = str(stmt)
        if "tenant_id" not in text:
            self.ungefiltert.append(text[:80])
        return _FakeResult([("wert-a", "wert-b")])


def _request(tenant_id):
    tenant = SimpleNamespace(slug="testbetrieb", company_name="Test GmbH")
    return SimpleNamespace(state=SimpleNamespace(app_tenant=tenant))


def _patch(monkeypatch, session, tenant_id):
    import contextlib

    @contextlib.asynccontextmanager
    async def _session():
        yield session
    monkeypatch.setattr(app_screens, "get_session", _session)
    monkeypatch.setattr(app_screens, "current_tenant_id", lambda _r: tenant_id)


@pytest.mark.asyncio
async def test_export_liefert_ein_zip_mit_csvs(monkeypatch):
    tid = uuid.uuid4()
    session = _FakeSession(tid)
    _patch(monkeypatch, session, tid)

    resp = await app_screens.api_datenexport(_request(tid))

    assert resp.media_type == "application/zip"
    assert "attachment" in resp.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.body)) as z:
        namen = z.namelist()
        assert "LIESMICH.txt" in namen
        csvs = [n for n in namen if n.endswith(".csv")]
        assert len(csvs) > 5, "Es sollten mehrere Tabellen enthalten sein"
        beispiel = z.read(csvs[0]).decode("utf-8")
        assert ";" in beispiel, "CSV mit Semikolon als Trennzeichen"


@pytest.mark.asyncio
async def test_export_fragt_immer_mit_tenant_filter(monkeypatch):
    """Der eigentliche Punkt: kein fremder Betrieb im eigenen Export."""
    tid = uuid.uuid4()
    session = _FakeSession(tid)
    _patch(monkeypatch, session, tid)

    await app_screens.api_datenexport(_request(tid))

    assert session.abfragen > 5
    assert not session.ungefiltert, (
        f"Abfragen ohne tenant_id-Filter: {session.ungefiltert}"
    )


@pytest.mark.asyncio
async def test_zugangsdaten_bleiben_draussen(monkeypatch):
    """Token, Sitzungen und Konfiguration gehoeren in keinen Export."""
    tid = uuid.uuid4()
    session = _FakeSession(tid)
    _patch(monkeypatch, session, tid)

    resp = await app_screens.api_datenexport(_request(tid))

    with zipfile.ZipFile(io.BytesIO(resp.body)) as z:
        namen = set(z.namelist())
    for tabu in ("oauth_tokens", "app_sessions", "tool_configs",
                 "app_login_tokens"):
        assert f"{tabu}.csv" not in namen, f"{tabu} darf nicht exportiert werden"
