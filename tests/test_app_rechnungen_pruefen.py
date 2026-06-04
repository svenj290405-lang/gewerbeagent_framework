"""Tests fuer den Rechnungs-Bezahlstatus-Abgleich der PWA
(core/api/app_screens.py: POST /rechnungen/pruefen).

Reine Unit-Tests mit Fakes — keine DB/Lexware/Netz. Patcht is_feature_enabled
und check_pending_invoices_for_tenant.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


def _req(tid=None):
    req = SimpleNamespace()

    async def _json():
        return {}

    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tid or uuid.uuid4()),
        app_employee=SimpleNamespace(id=uuid.uuid4()),
    )
    return req


@pytest.mark.asyncio
async def test_pruefen_feature_aus_403(monkeypatch):
    async def feat(tid, k):
        return False
    monkeypatch.setattr("core.features.check.is_feature_enabled", feat)
    res = await app_screens.api_rechnungen_pruefen(_req(), _e=None, _c=None)
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_pruefen_happy_path_mappt_summary(monkeypatch):
    async def feat(tid, k):
        return True
    monkeypatch.setattr("core.features.check.is_feature_enabled", feat)

    async def check(tid):
        return {"checked": 4, "paid": 2, "errors": 0, "no_change": 2}
    monkeypatch.setattr(
        "core.integrations.rechnung_payment_monitor.check_pending_invoices_for_tenant",
        check,
    )
    res = await app_screens.api_rechnungen_pruefen(_req(), _e=None, _c=None)
    assert res.status_code == 200
    b = json.loads(res.body)
    assert b["ok"] is True
    assert b["geprueft"] == 4
    assert b["bezahlt"] == 2
    assert b["unveraendert"] == 2
    assert b["fehler"] == 0
