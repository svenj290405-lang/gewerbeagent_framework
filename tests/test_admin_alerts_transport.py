"""Tests fuer den Alarmweg an den Betreiber (core/integrations/admin_alerts.py).

Hintergrund: nach dem Ausbau des Telegram-Bots (2026-08-21) gab
`_deliver_to_sven` hart False zurueck — es gab schlicht keinen Transport
mehr. Ein Ausfall waere nur im Container-Log gelandet, das im Ernstfall
niemand liest. Diese Tests halten fest, dass jetzt wieder etwas rausgeht
und dass die Kaskade in der gedachten Reihenfolge arbeitet.

SMTP ist gemockt — hier soll nichts wirklich verschickt werden.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from core.integrations import admin_alerts


@pytest.fixture(autouse=True)
def _kein_echtes_smtp(monkeypatch):
    """Sicherheitsnetz: der blockierende SMTP-Aufruf wird nie echt."""
    monkeypatch.setattr(
        admin_alerts, "_sende_smtp_blockierend",
        lambda *a, **k: None,
    )


def _smtp_setzen(monkeypatch, host="smtp.example.org", to="betreiber@example.org"):
    monkeypatch.setattr(admin_alerts.settings, "alert_smtp_host", host,
                        raising=False)
    monkeypatch.setattr(admin_alerts.settings, "alert_smtp_to", to,
                        raising=False)


@pytest.mark.asyncio
async def test_smtp_erfolg_zaehlt_als_zugestellt(monkeypatch):
    _smtp_setzen(monkeypatch)
    gesendet = []
    monkeypatch.setattr(
        admin_alerts, "_sende_smtp_blockierend",
        lambda betreff, text: gesendet.append((betreff, text)),
    )
    monkeypatch.setattr(
        admin_alerts, "_sende_push_an_betreiber", AsyncMock(return_value=0),
    )

    ok = await admin_alerts._deliver_to_sven("framework_down", "Alles steht")

    assert ok is True
    assert gesendet and "framework_down" in gesendet[0][0]
    assert "Alles steht" in gesendet[0][1]


@pytest.mark.asyncio
async def test_push_faengt_auf_wenn_smtp_kracht(monkeypatch):
    """Faellt der Mailweg aus, muss der Zweitweg die Zustellung retten."""
    _smtp_setzen(monkeypatch)

    def _kracht(betreff, text):
        raise OSError("Mailserver nicht erreichbar")
    monkeypatch.setattr(admin_alerts, "_sende_smtp_blockierend", _kracht)
    push = AsyncMock(return_value=1)
    monkeypatch.setattr(admin_alerts, "_sende_push_an_betreiber", push)

    ok = await admin_alerts._deliver_to_sven("db_down", "DB antwortet nicht")

    assert ok is True
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_ohne_transport_bleibt_es_ehrlich_false(monkeypatch):
    """Kein SMTP, kein Empfaenger: die Funktion darf keinen Erfolg vortaeuschen."""
    monkeypatch.setattr(admin_alerts.settings, "alert_smtp_host", "",
                        raising=False)
    monkeypatch.setattr(
        admin_alerts, "_sende_push_an_betreiber", AsyncMock(return_value=0),
    )

    ok = await admin_alerts._deliver_to_sven("cron_dead", "Cron laeuft nicht")

    assert ok is False


@pytest.mark.asyncio
async def test_push_scheitert_still_und_reisst_nichts_mit(monkeypatch):
    """Ein Alarm darf nie den Aufrufer abbrechen — auch nicht, wenn beide Wege krachen."""
    _smtp_setzen(monkeypatch, host="")

    async def _kracht(kind, message):
        raise RuntimeError("Push kaputt")
    monkeypatch.setattr(admin_alerts, "_sende_push_an_betreiber", _kracht)

    ok = await admin_alerts._deliver_to_sven("irgendwas", "Text")
    assert ok is False


@pytest.mark.asyncio
async def test_push_geht_an_den_betreiber_nicht_an_irgendwen(monkeypatch):
    """Der Zweitweg muss den Empfaenger ueber die Betreiber-Adresse suchen."""
    monkeypatch.setattr(admin_alerts.settings, "alert_smtp_to",
                        "betreiber@example.org", raising=False)
    emp_id = uuid.uuid4()
    gesucht = {}

    async def _finde(email, *, session):
        gesucht["email"] = email
        return type("E", (), {"id": emp_id})()

    import contextlib

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    monkeypatch.setattr(admin_alerts, "get_session", _session)
    import core.security.app_auth as app_auth
    monkeypatch.setattr(app_auth, "find_employee_by_email", _finde)

    from core.integrations import push_notifier
    monkeypatch.setattr(push_notifier, "push_enabled", lambda: True)
    push = AsyncMock(return_value=2)
    monkeypatch.setattr(push_notifier, "send_push_to_employee", push)

    anzahl = await admin_alerts._sende_push_an_betreiber("test", "Nachricht")

    assert anzahl == 2
    assert gesucht["email"] == "betreiber@example.org"
    assert push.await_args.args[0] == emp_id
