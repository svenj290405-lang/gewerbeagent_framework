"""Tests fuer Mail-Retry-Queue Schema + Backoff-Logik (Phase A5).

Keine DB-Calls — wir testen die reinen Konstanten + Hilfsfunktionen.
"""
from __future__ import annotations

import datetime as dt

from core.integrations.mail_retry_cron import _next_attempt_time
from core.models import (
    MAIL_TYPE_ANGEBOT,
    MAIL_TYPE_RECHNUNG,
    MAIL_TYPE_REPLY,
    MAIL_TYPE_VISUALISIERUNG,
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)


def test_backoff_is_exponential_and_bounded():
    """Backoff-Plan: 5min → 30min → 2h, dann dead."""
    assert RETRY_BACKOFF_SECONDS == [5 * 60, 30 * 60, 2 * 60 * 60]
    assert MAX_ATTEMPTS == 3


def test_next_attempt_time_returns_none_when_dead():
    """Nach MAX_ATTEMPTS muss next_attempt_time None liefern (= dead)."""
    assert _next_attempt_time(MAX_ATTEMPTS) is None
    assert _next_attempt_time(MAX_ATTEMPTS + 1) is None


def test_next_attempt_time_schedules_in_future():
    """Bei attempt_count < MAX_ATTEMPTS muss eine zukuenftige Zeit kommen."""
    before = dt.datetime.now(dt.timezone.utc)
    nxt = _next_attempt_time(0)
    assert nxt is not None
    delta = (nxt - before).total_seconds()
    # Erstes Retry-Delay: 5 min = 300s
    assert 290 < delta < 310, f"expected ~300s, got {delta}"


def test_all_mail_types_known():
    """Konstanten fuer alle Mail-Typen muessen exportiert sein."""
    assert MAIL_TYPE_RECHNUNG == "rechnung"
    assert MAIL_TYPE_VISUALISIERUNG == "visualisierung"
    assert MAIL_TYPE_ANGEBOT == "angebot"
    assert MAIL_TYPE_REPLY == "reply"


def test_enqueue_failed_mail_signature_supports_viz_and_angebot():
    """Beta-1 B1-5: enqueue muss viz_id + angebot_id + mail_backend kennen."""
    import inspect
    from core.integrations.mail_retry_cron import enqueue_failed_mail
    params = inspect.signature(enqueue_failed_mail).parameters
    assert "viz_id" in params
    assert "angebot_id" in params
    assert "mail_backend" in params
    assert params["mail_backend"].default == "brevo"


def test_mark_sent_signature_supports_viz_and_angebot():
    """Beta-1 B1-5: _mark_sent ist jetzt generisch."""
    import inspect
    from core.integrations.mail_retry_cron import _mark_sent
    params = inspect.signature(_mark_sent).parameters
    assert "viz_id" in params
    assert "angebot_id" in params


def test_dispatch_mail_hub_exists():
    """Beta-1 B1-6: _dispatch_mail routet zwischen Brevo + Microsoft Graph."""
    from core.integrations.mail_retry_cron import (
        _dispatch_mail, _send_via_microsoft_graph,
    )
    assert _dispatch_mail is not None
    assert _send_via_microsoft_graph is not None


def test_angebot_status_constants_exported():
    """Beta-1 B1-6: Angebot-Status-Konstanten."""
    from core.models import (
        ANGEBOT_STATUS_MAIL_QUEUED, ANGEBOT_STATUS_MAIL_SENT,
        ANGEBOT_STATUS_MAIL_FAILED,
    )
    assert ANGEBOT_STATUS_MAIL_QUEUED == "mail_queued"
    assert ANGEBOT_STATUS_MAIL_SENT == "mail_sent"
    assert ANGEBOT_STATUS_MAIL_FAILED == "mail_failed"


def test_viz_status_mail_queued_exported():
    """Beta-1 B1-4: VIZ_STATUS_MAIL_QUEUED muss importierbar sein."""
    from core.models import VIZ_STATUS_MAIL_QUEUED
    assert VIZ_STATUS_MAIL_QUEUED == "mail_queued"
