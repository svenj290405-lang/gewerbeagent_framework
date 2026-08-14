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


# --------------------------------------------------------------------------
# Bezahl-Monitor: in Lexware geloeschte Rechnungen
#
# 404 heisst: den Beleg gibt es dort nicht mehr. Bisher wurde das nur in
# lexware_voucher_status vermerkt, der Status blieb 'mail_sent' — die
# Rechnung wurde also ewig weiter gepollt (entgegen dem Docstring) und
# zaehlte in den offenen Posten als Forderung mit, die es nicht gibt.
# --------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402

import core.integrations.rechnung_payment_monitor as pm  # noqa: E402


class _UpdateSession:
    """Faengt die UPDATE-Werte ab, statt sie in eine DB zu schreiben."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    async def execute(self, stmt):
        # SELECT (Liste offener Rechnungen) vs. UPDATE unterscheiden
        werte = getattr(stmt, "_values", None)
        if werte is not None:
            self.updates.append({
                (k.name if hasattr(k, "name") else str(k)): getattr(v, "value", v)
                for k, v in werte.items()
            })
            return SimpleNamespace()
        rows = self._rows
        return SimpleNamespace(all=lambda: rows)

    async def commit(self):
        pass


def _patch_monitor(monkeypatch, rows, antwort):
    sess = _UpdateSession(rows)

    @asynccontextmanager
    async def _sl():
        yield sess

    monkeypatch.setattr(pm, "AsyncSessionLocal", _sl)

    async def _prov(tid):
        return object()
    monkeypatch.setattr(pm, "_build_lexware_provider", _prov)

    async def _check(r_id, lex_id, provider):
        return antwort
    monkeypatch.setattr(pm, "_check_one_invoice", _check)
    return sess


@pytest.mark.asyncio
async def test_geloeschte_rechnung_wird_stillgelegt(monkeypatch):
    r_id, lex_id = uuid.uuid4(), uuid.uuid4()
    sess = _patch_monitor(monkeypatch, [(r_id, lex_id)], ("cancelled", False))

    summary = await pm.check_pending_invoices_for_tenant(uuid.uuid4())

    assert summary["checked"] == 1
    assert summary.get("cancelled") == 1
    assert summary["paid"] == 0
    # Status wandert weg von mail_sent -> raus aus Polling UND offenen Posten
    assert sess.updates[0]["status"] == "cancelled"
    assert sess.updates[0]["lexware_voucher_status"] == "cancelled"


@pytest.mark.asyncio
async def test_bezahlte_rechnung_wird_als_bezahlt_gebucht(monkeypatch):
    r_id, lex_id = uuid.uuid4(), uuid.uuid4()
    sess = _patch_monitor(monkeypatch, [(r_id, lex_id)], ("paid", True))

    summary = await pm.check_pending_invoices_for_tenant(uuid.uuid4())

    assert summary["paid"] == 1
    assert sess.updates[0]["status"] == "bezahlt"
    assert sess.updates[0]["bezahlt_am"] is not None


@pytest.mark.asyncio
async def test_offene_rechnung_bleibt_unveraendert(monkeypatch):
    r_id, lex_id = uuid.uuid4(), uuid.uuid4()
    sess = _patch_monitor(monkeypatch, [(r_id, lex_id)], ("open", False))

    summary = await pm.check_pending_invoices_for_tenant(uuid.uuid4())

    assert summary["no_change"] == 1
    assert "status" not in sess.updates[0]
