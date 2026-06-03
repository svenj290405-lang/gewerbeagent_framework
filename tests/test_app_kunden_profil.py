"""Tests fuer das Kunden-Profil der PWA
(core/api/app_screens.py: GET /kunden/profil).

Reine Unit-Tests mit Fakes — keine echte DB.

Deckt:
- Name zu kurz -> 400
- Happy-Path: Aggregation (Gespraeche/Angebote/Rechnungen), E-Mail aus
  Angebot, Drive-Block; Status-Labels gemappt
- Kein Drive-Ordner -> drive None
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


class _FakeProfSession:
    """execute() liefert nacheinander die Werte aus seq. Listen werden ueber
    scalars().all() abgefragt (Gespraeche/Angebote/Rechnungen), das
    Drive-Objekt ueber scalar_one_or_none()."""
    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        val = self.seq[self.i]
        self.i += 1
        return SimpleNamespace(
            scalars=lambda v=val: SimpleNamespace(all=lambda: v if isinstance(v, list) else []),
            scalar_one_or_none=lambda v=val: (None if isinstance(v, list) else v),
        )


def _req(tenant_id=None):
    return SimpleNamespace(
        state=SimpleNamespace(app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4()))
    )


def _json(resp):
    return json.loads(bytes(resp.body))


@pytest.mark.asyncio
async def test_profil_short_name_returns_400(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session", lambda: _FakeProfSession([]))
    resp = await app_screens.api_kunde_profil(request=_req(), name="a", _e=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_profil_happy_path_aggregates(monkeypatch):
    gespraeche = [SimpleNamespace(id=uuid.uuid4(), briefing_kurz="Bad sanieren", gespraech_datum=None)]
    angebote = [SimpleNamespace(gesamtbetrag_brutto_eur=1000, status="accepted",
                                created_at=None, kunde_email="kunde@example.de")]
    rechnungen = [SimpleNamespace(betrag_brutto_eur=500, lexware_voucher_number="RE-1",
                                  status="bezahlt", created_at=None)]
    drive = SimpleNamespace(drive_folder_url="https://drive.example/x",
                            upload_count=3, last_upload_at=None)
    monkeypatch.setattr(app_screens, "get_session",
                        lambda: _FakeProfSession([gespraeche, angebote, rechnungen, drive]))

    resp = await app_screens.api_kunde_profil(request=_req(), name="Mueller", _e=None)
    assert resp.status_code == 200
    j = _json(resp)
    assert j["ok"] is True
    assert j["name"] == "Mueller"
    assert j["email"] == "kunde@example.de"
    assert len(j["gespraeche"]) == 1 and j["gespraeche"][0]["briefing"] == "Bad sanieren"
    assert j["angebote"][0]["status"]      # gemapptes Label, nicht leer
    assert j["drive"]["url"] == "https://drive.example/x"
    assert j["drive"]["anzahl"] == 3


@pytest.mark.asyncio
async def test_profil_without_drive(monkeypatch):
    monkeypatch.setattr(app_screens, "get_session",
                        lambda: _FakeProfSession([[], [], [], None]))
    resp = await app_screens.api_kunde_profil(request=_req(), name="Unbekannt", _e=None)
    assert resp.status_code == 200
    j = _json(resp)
    assert j["drive"] is None
    assert j["email"] == ""
    assert j["gespraeche"] == []
