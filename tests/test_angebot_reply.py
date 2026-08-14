"""Tests fuer core/services/angebot_reply.py — Angebots-Antwort-Erkennung.

DB gemockt (kein echtes Postgres, Konvention wie test_document_flow). Getestet
werden die Entscheidungslogik: Zuordnung, Modus-Gate, Klassifikations-Gate und
die Status-Wirkung je Automatisierungsgrad.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.services.angebot_reply as ar


def _make_get_session(angebot):
    """get_session-Ersatz. Liefert bei jedem execute() das gegebene Angebot
    (oder None) zurueck — reicht fuer Lookup + Re-Load im automatisch-Pfad."""
    class _S:
        async def execute(self, stmt):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    first=lambda: angebot,
                ),
                scalar_one_or_none=lambda: angebot,
            )
        async def commit(self): pass

    @asynccontextmanager
    async def _gs():
        yield _S()
    return _gs


def _angebot():
    return SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Familie Weber",
        status="mail_sent", accepted_at=None, rejected_at=None,
    )


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    # Push nie echt senden — nur zaehlen.
    calls = {"push": 0}
    async def _fake_push(tenant_id, **kw):
        calls["push"] += 1
        return 1
    import core.integrations.push_notifier as pn
    monkeypatch.setattr(pn, "send_push_to_tenant", _fake_push)
    return calls


def _patch_classify(monkeypatch, classification, confidence="high"):
    async def _fake(*, mail_subject, mail_body):
        return {"classification": classification, "confidence": confidence, "reason": "x"}
    import core.ai.gemini as g
    monkeypatch.setattr(g, "classify_angebot_response", _fake)


def _patch_mode(monkeypatch, mode):
    async def _fake(tenant_id, key):
        return mode
    monkeypatch.setattr(ar, "mode_for_automation", _fake)


@pytest.mark.asyncio
async def test_ohne_conversation_id_keine_aktion(monkeypatch):
    _patch_mode(monkeypatch, "automatisch")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id=None,
        mail_subject="Re: Angebot", mail_body="ja",
    )
    assert res is None


@pytest.mark.asyncio
async def test_modus_manuell_tut_nichts(monkeypatch):
    _patch_mode(monkeypatch, "manuell")
    monkeypatch.setattr(ar, "get_session", _make_get_session(_angebot()))
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="ja, machen wir",
    )
    assert res is None


@pytest.mark.asyncio
async def test_kein_passendes_angebot(monkeypatch):
    _patch_mode(monkeypatch, "automatisch")
    monkeypatch.setattr(ar, "get_session", _make_get_session(None))
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="ja",
    )
    assert res is None


@pytest.mark.asyncio
async def test_rueckfrage_loest_nichts_aus(monkeypatch):
    _patch_mode(monkeypatch, "automatisch")
    monkeypatch.setattr(ar, "get_session", _make_get_session(_angebot()))
    _patch_classify(monkeypatch, "RUECKFRAGE")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="Kann man den Termin verschieben?",
    )
    assert res is None


@pytest.mark.asyncio
async def test_low_confidence_loest_nichts_aus(monkeypatch):
    _patch_mode(monkeypatch, "automatisch")
    monkeypatch.setattr(ar, "get_session", _make_get_session(_angebot()))
    _patch_classify(monkeypatch, "ANNAHME", confidence="low")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="ok?",
    )
    assert res is None


@pytest.mark.asyncio
async def test_assistiert_setzt_status_NICHT_aber_meldet(monkeypatch, _patch_common):
    _patch_mode(monkeypatch, "assistiert")
    ang = _angebot()
    monkeypatch.setattr(ar, "get_session", _make_get_session(ang))
    _patch_classify(monkeypatch, "ANNAHME", confidence="high")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="Ja, bitte machen Sie.",
    )
    assert res is not None
    assert res["status_gesetzt"] is False
    assert ang.status == "mail_sent"      # unveraendert
    assert _patch_common["push"] == 1     # aber gemeldet


@pytest.mark.asyncio
async def test_automatisch_setzt_accepted(monkeypatch, _patch_common):
    _patch_mode(monkeypatch, "automatisch")
    ang = _angebot()
    monkeypatch.setattr(ar, "get_session", _make_get_session(ang))
    _patch_classify(monkeypatch, "ANNAHME", confidence="high")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="Ja, passt.",
    )
    assert res["status_gesetzt"] is True
    assert ang.status == "accepted"
    assert ang.accepted_at is not None
    assert _patch_common["push"] == 1


@pytest.mark.asyncio
async def test_automatisch_setzt_rejected(monkeypatch, _patch_common):
    _patch_mode(monkeypatch, "automatisch")
    ang = _angebot()
    monkeypatch.setattr(ar, "get_session", _make_get_session(ang))
    _patch_classify(monkeypatch, "ABLEHNUNG", confidence="high")
    res = await ar.detect_and_handle_angebot_reply(
        tenant_id=uuid.uuid4(), conversation_id="c1",
        mail_subject="Re: Angebot", mail_body="Danke, zu teuer.",
    )
    assert res["status_gesetzt"] is True
    assert ang.status == "rejected"
    assert ang.rejected_at is not None
