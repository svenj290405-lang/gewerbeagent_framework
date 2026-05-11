"""Tests fuer FailureCounter (Phase A4).

Validiert Sliding-Window, Threshold, Cooldown und Reset-Logik.
Reine In-Memory-Logik, keine DB/HTTP-Mocks noetig.
"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from core.integrations.failure_counter import FailureCounter


def test_threshold_triggers_alert_exactly_once():
    """Erst beim N-ten Fehler within window soll should_alert=True kommen."""
    c = FailureCounter(
        "t", window_minutes=10, threshold=3, cooldown_minutes=5,
    )
    assert c.record_failure(key="x") == (False, 1)
    assert c.record_failure(key="x") == (False, 2)
    # Schwelle erreicht — Alert
    should, n = c.record_failure(key="x")
    assert should is True and n == 3
    # Cooldown verhindert weitere Alerts
    should, n = c.record_failure(key="x")
    assert should is False and n == 4


def test_reset_clears_window_and_cooldown():
    c = FailureCounter(
        "t", window_minutes=10, threshold=2, cooldown_minutes=5,
    )
    c.record_failure(key="x")
    c.record_failure(key="x")  # Alert
    c.reset(key="x")
    # Nach Reset zaehlt es wieder von vorne UND der Cooldown ist weg
    assert c.record_failure(key="x") == (False, 1)
    should, n = c.record_failure(key="x")
    assert should is True and n == 2


def test_keys_are_isolated():
    c = FailureCounter(
        "t", window_minutes=10, threshold=2, cooldown_minutes=5,
    )
    c.record_failure(key="a")
    # Tenant b zaehlt eigene Failures
    should, n = c.record_failure(key="b")
    assert should is False and n == 1
    # Tenant a kriegt seinen Alert
    should, n = c.record_failure(key="a")
    assert should is True and n == 2
    # Tenant b ist immer noch bei 1
    should, n = c.record_failure(key="b")
    assert should is True and n == 2


def test_sliding_window_prunes_old_entries():
    """Eintraege ausserhalb des Fensters muessen weg sein."""
    c = FailureCounter(
        "t", window_minutes=10, threshold=3, cooldown_minutes=5,
    )
    # Backdoor: alte Timestamps manuell injizieren
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    c._timestamps["x"].extend([old, old, old])  # noqa: SLF001
    # Jetzt ein frischer Failure — die 3 alten muessen weggeprunet werden
    should, n = c.record_failure(key="x")
    assert n == 1, f"erwartet 1, alte muessten weg sein, ist {n}"
    assert should is False


def test_get_last_reason_returns_most_recent():
    c = FailureCounter(
        "t", window_minutes=10, threshold=5, cooldown_minutes=5,
    )
    c.record_failure(key="x", reason="erste")
    c.record_failure(key="x", reason="zweite")
    assert c.get_last_reason(key="x") == "zweite"
    # Reset loescht auch reason
    c.reset(key="x")
    assert c.get_last_reason(key="x") == ""


def test_predefined_counters_are_correctly_configured():
    """Sanity-Check: die globalen Counter haben die Plan-Werte."""
    from core.integrations.failure_counter import (
        DRIVE_UPLOAD_FAILURES,
        MAIL_CLASSIFY_FAILURES,
    )
    assert DRIVE_UPLOAD_FAILURES.threshold == 5
    assert DRIVE_UPLOAD_FAILURES.window == dt.timedelta(hours=1)
    assert MAIL_CLASSIFY_FAILURES.threshold == 3
    assert MAIL_CLASSIFY_FAILURES.window == dt.timedelta(hours=24)
