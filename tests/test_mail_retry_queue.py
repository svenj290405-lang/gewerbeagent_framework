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
