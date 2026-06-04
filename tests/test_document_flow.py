"""Tests fuer den Beleg-Fluss-Service (core/services/document_flow.py).

Konvention wie die uebrigen Tests: DB wird gemockt (kein echtes Postgres).
Getestet werden die reine Positions-Mappinglogik, die Eingabe-Validierung
(die VOR jedem DB-/Lexware-Call greift) und die 'nicht gefunden'-Pfade der
Lookups/Sender — also genau die Stellen, an denen Fehler sitzen.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.services.document_flow as df


# --------------------------------------------------------------------------
# Fake-Session
# --------------------------------------------------------------------------

def _make_get_session(results):
    """Ersetzt core.database.connection.get_session. Jeder execute() pop't
    das naechste Ergebnis (scalar_one_or_none + scalars().all())."""
    queue = list(results)

    class _S:
        async def execute(self, stmt):
            val = queue.pop(0) if queue else None
            return SimpleNamespace(
                scalar_one_or_none=lambda: val,
                scalars=lambda: SimpleNamespace(
                    all=lambda: (val if isinstance(val, list)
                                 else ([] if val is None else [val]))),
            )

        def add(self, obj): pass
        async def flush(self): pass
        async def commit(self): pass
        async def refresh(self, obj): pass
        async def delete(self, obj): pass

    @asynccontextmanager
    async def _gs():
        yield _S()

    return _gs


def _patch_session(monkeypatch, results):
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", _make_get_session(results))


TID = uuid.uuid4()


# --------------------------------------------------------------------------
# _positionen_to_line_items (pure)
# --------------------------------------------------------------------------

def test_positionen_mapping_valid():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "Parkett", "menge": 20, "einheit": "qm", "preis_brutto_eur": 50, "mwst_prozent": 19},
        {"name": "Anfahrt", "menge": 1, "einheit": "Pauschale", "preis_brutto_eur": 30},
    ])
    assert err is None
    assert len(items) == 2
    assert float(gesamt) == 20 * 50 + 30
    assert items[0].tax_rate_percent == 19
    assert items[1].tax_rate_percent == 19  # default


def test_positionen_mapping_invalid_number():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "X", "menge": "viel", "einheit": "qm", "preis_brutto_eur": 50},
    ])
    assert err is not None
    assert "Position 1" in err


def test_positionen_mapping_skips_empty_name():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "", "menge": 1, "einheit": "x", "preis_brutto_eur": 5},
        {"name": "Echt", "menge": 2, "einheit": "x", "preis_brutto_eur": 10},
    ])
    assert err is None
    assert len(items) == 1
    assert items[0].name == "Echt"


# --------------------------------------------------------------------------
# Validierung (vor DB)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_angebot_requires_kunde():
    res = await df.create_angebot(TID, kunde_name="  ", positionen=[{"name": "X", "menge": 1, "einheit": "x", "preis_brutto_eur": 5}])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_angebot_requires_positionen():
    res = await df.create_angebot(TID, kunde_name="Meier", positionen=[])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_rechnung_requires_kunde():
    res = await df.create_rechnung(TID, kunde_name="")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_rechnung_requires_mode():
    res = await df.create_rechnung(TID, kunde_name="Meier")
    assert res["ok"] is False
    assert "Pauschal" in res["error"] or "Positionen" in res["error"]


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_angebot_none(monkeypatch):
    _patch_session(monkeypatch, [[]])
    assert await df.find_angebot_for_send(TID, "Meier") is None


@pytest.mark.asyncio
async def test_find_angebot_unique(monkeypatch):
    ang = SimpleNamespace(id=uuid.uuid4(), kunde_name="Meier")
    _patch_session(monkeypatch, [[ang]])
    res = await df.find_angebot_for_send(TID, "Meier")
    assert res is ang


@pytest.mark.asyncio
async def test_find_angebot_ambiguous(monkeypatch):
    a = SimpleNamespace(id=uuid.uuid4())
    b = SimpleNamespace(id=uuid.uuid4())
    _patch_session(monkeypatch, [[a, b]])
    assert await df.find_angebot_for_send(TID, "Meier") == "AMBIG"


@pytest.mark.asyncio
async def test_find_auftrag_for_invoice_none(monkeypatch):
    _patch_session(monkeypatch, [[]])
    assert await df.find_auftrag_for_invoice(TID, "Meier") is None


# --------------------------------------------------------------------------
# Sender 'nicht gefunden'
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_angebot_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])  # scalar_one_or_none -> None
    res = await df.send_angebot(TID, angebot_id=uuid.uuid4())
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]


@pytest.mark.asyncio
async def test_send_anfrage_reply_empty_text():
    res = await df.send_anfrage_reply(TID, conv_id=uuid.uuid4(), reply_text="   ")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_send_anfrage_reply_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])
    res = await df.send_anfrage_reply(TID, conv_id=uuid.uuid4(), reply_text="Hallo")
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]


@pytest.mark.asyncio
async def test_finalize_invoice_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])
    res = await df.finalize_and_send_invoice(TID, angebot_id=uuid.uuid4())
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]
