"""Tests fuer den Drive-Root-Folder-Cache (Fix 2026-05-17).

Bugkontext: Vorher hat `_ensure_root_folder` bei jedem Upload ueber
`name='Gewerbeagent — <company>'` gesucht und einen neuen Ordner
angelegt wenn nicht gefunden. Bei Tenant-Umbenennung oder Naming-
Konvention-Wechsel im Code entstand jedes Mal ein neuer, leerer
Root-Ordner — alte Dateien blieben verwaist.

Jetzt: drei-stufige Strategie
1. DB-Cache (`tenants.drive_root_folder_id`) — falls vorhanden + valid
2. Suche-by-Name mit allen historischen Naming-Varianten
3. Erstellen + cachen

Test deckt alle drei Pfade + den Validate-Drop ab.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.integrations.google_drive import _ensure_root_folder


# =====================================================================
# Test-Doubles
# =====================================================================

class _FakeDriveService:
    """Simuliert googleapiclient.discovery.build('drive') — nur die
    Sub-Calls die _ensure_root_folder braucht."""

    def __init__(
        self,
        *,
        get_returns: dict | None = None,
        list_results_by_query: list[list[dict]] | None = None,
        create_returns: dict | None = None,
    ):
        self.get_returns = get_returns
        self.list_results_by_query = list(list_results_by_query or [])
        self.create_returns = create_returns or {"id": "newly-created"}
        self.calls: dict[str, int] = {
            "get": 0, "list": 0, "create": 0,
        }

    def files(self):
        outer = self

        class _Files:
            def get(self, *, fileId, fields):
                outer.calls["get"] += 1
                exec_obj = MagicMock()
                if outer.get_returns is None:
                    exec_obj.execute.side_effect = Exception("404 not found")
                else:
                    exec_obj.execute.return_value = outer.get_returns
                return exec_obj

            def list(self, *, q, spaces, fields, pageSize):
                outer.calls["list"] += 1
                exec_obj = MagicMock()
                if outer.list_results_by_query:
                    exec_obj.execute.return_value = {
                        "files": outer.list_results_by_query.pop(0),
                    }
                else:
                    exec_obj.execute.return_value = {"files": []}
                return exec_obj

            def create(self, *, body, fields):
                outer.calls["create"] += 1
                exec_obj = MagicMock()
                exec_obj.execute.return_value = outer.create_returns
                return exec_obj

        return _Files()


def _make_tenant(
    *,
    company_name="Schreinerei Test GbR",
    slug="demo",
    cached_id: str | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        company_name=company_name,
        drive_root_folder_id=cached_id,
    )


def _patch_db_no_write(monkeypatch, tenant_obj):
    """Patcht AsyncSessionLocal so dass der DB-Write am Ende von
    _ensure_root_folder nicht crasht. Wir speichern keinen echten
    Tenant-Update — tests interessiert nur die Drive-Folder-ID-Logik."""
    import core.integrations.google_drive as gd

    class _S:
        async def execute(self, _stmt):
            class _R:
                def scalar_one_or_none(self):
                    return tenant_obj
            return _R()

        async def commit(self):
            pass

    @asynccontextmanager
    async def cm():
        yield _S()

    monkeypatch.setattr(gd, "AsyncSessionLocal", cm)


# =====================================================================
# Cache-Hit-Pfad
# =====================================================================

@pytest.mark.asyncio
async def test_cached_id_is_returned_when_valid(monkeypatch):
    """Cache vorhanden + Folder existiert -> direkter Return, kein list, kein create."""
    tenant = _make_tenant(cached_id="folder-cached-123")
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        get_returns={"id": "folder-cached-123", "trashed": False},
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "folder-cached-123"
    assert service.calls["get"] == 1
    assert service.calls["list"] == 0
    assert service.calls["create"] == 0


@pytest.mark.asyncio
async def test_cached_id_dropped_when_folder_trashed(monkeypatch):
    """Cache vorhanden aber Folder ist trashed -> fallback auf Suche+Create."""
    tenant = _make_tenant(cached_id="folder-trashed")
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        get_returns={"id": "folder-trashed", "trashed": True},
        list_results_by_query=[[]],  # Suche findet nichts
        create_returns={"id": "freshly-created"},
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "freshly-created"
    assert service.calls["create"] == 1


@pytest.mark.asyncio
async def test_cached_id_dropped_when_get_404(monkeypatch):
    """Cache vorhanden aber files.get crasht (404) -> fallback."""
    tenant = _make_tenant(cached_id="folder-deleted")
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        get_returns=None,  # → side_effect = Exception
        list_results_by_query=[[]],
        create_returns={"id": "freshly-created"},
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "freshly-created"


# =====================================================================
# Suche-by-Name-Pfad (kein Cache)
# =====================================================================

@pytest.mark.asyncio
async def test_finds_existing_canonical_name(monkeypatch):
    """Kein Cache, Em-Dash-Name existiert in Drive -> uebernimmt + kein create."""
    tenant = _make_tenant(cached_id=None)
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        list_results_by_query=[
            [{"id": "existing-em-dash", "name": "Gewerbeagent — Schreinerei Test GbR"}],
        ],
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "existing-em-dash"
    assert service.calls["create"] == 0
    # Nur die erste Naming-Variante musste durchsucht werden
    assert service.calls["list"] == 1


@pytest.mark.asyncio
async def test_finds_legacy_underscore_name(monkeypatch):
    """Em-Dash nicht da, aber Unterstrich-Variante (alter Code) existiert."""
    tenant = _make_tenant(cached_id=None)
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        list_results_by_query=[
            [],  # Em-Dash: nicht da
            [{"id": "legacy-underscore", "name": "Gewerbeagent_ Schreinerei Test GbR"}],
        ],
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "legacy-underscore"
    assert service.calls["create"] == 0


@pytest.mark.asyncio
async def test_finds_legacy_ascii_minus_name(monkeypatch):
    """Em-Dash + Unterstrich nicht da, ASCII-Minus existiert."""
    tenant = _make_tenant(cached_id=None)
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        list_results_by_query=[
            [],
            [],
            [{"id": "legacy-minus", "name": "Gewerbeagent - Schreinerei Test GbR"}],
        ],
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "legacy-minus"


# =====================================================================
# Create-Pfad
# =====================================================================

@pytest.mark.asyncio
async def test_creates_new_when_no_match(monkeypatch):
    """Kein Cache + keine Naming-Variante existiert -> neuen Ordner anlegen."""
    tenant = _make_tenant(cached_id=None)
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        list_results_by_query=[[], [], []],
        create_returns={"id": "brand-new"},
    )
    folder_id = await _ensure_root_folder(service, tenant)
    assert folder_id == "brand-new"
    assert service.calls["create"] == 1
    # Alle drei Naming-Varianten wurden gesucht bevor erstellt wurde
    assert service.calls["list"] == 3


@pytest.mark.asyncio
async def test_db_cache_is_written_after_find(monkeypatch):
    """Nach Suche/Create: drive_root_folder_id wird in DB geschrieben."""
    tenant = _make_tenant(cached_id=None)
    _patch_db_no_write(monkeypatch, tenant)
    service = _FakeDriveService(
        list_results_by_query=[
            [{"id": "existing-folder"}],
        ],
    )
    folder_id = await _ensure_root_folder(service, tenant)
    # _patch_db_no_write reicht das tenant-Objekt durch — d.h. die
    # Cache-Schreib-Logik hat es bereits aktualisiert
    assert folder_id == "existing-folder"
    assert tenant.drive_root_folder_id == "existing-folder"
