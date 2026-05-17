"""Tests fuer den Formular-Eingang (Status-Tracking + Heartbeat).

Deckt:
- short_id-Generierung
- _should_trigger (Heartbeat-Zeit-Logik) — pure Funktion, kein DB-Zugriff
- _build_ping_text (Plural-Forms)

Die DB-touching Funktionen (set_status, list_recent_for_tenant,
find_tenants_with_overdue) brauchen eine echte DB und sind in den
Integrations-Tests gesondert abgedeckt — hier bewusst nicht gemockt,
weil ein gemockter SQLAlchemy-Query keine Aussage darueber macht ob
die JOIN-Logik richtig ist.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from core.integrations.formular_eingang import short_id
from core.integrations.formular_heartbeat_cron import (
    _build_ping_text,
    _should_trigger,
    PING_HOUR_LOCAL,
    PING_MINUTE_LOCAL,
    LOCAL_TZ,
)


# =====================================================================
# short_id
# =====================================================================

def test_short_id_is_8_hex_chars():
    sid = short_id(uuid.uuid4())
    assert len(sid) == 8
    assert all(c in "0123456789abcdef" for c in sid)


def test_short_id_stable_for_same_uuid():
    u = uuid.uuid4()
    assert short_id(u) == short_id(u)


def test_short_id_differs_for_different_uuids():
    # Mit 16^8 Praefixen ist eine Kollision bei zwei zufaelligen UUIDs
    # praktisch ausgeschlossen — verifiziert defensiv die Annahme
    a, b = uuid.uuid4(), uuid.uuid4()
    assert short_id(a) != short_id(b)


# =====================================================================
# _build_ping_text
# =====================================================================

def test_ping_text_singular():
    text = _build_ping_text(1)
    assert "1 offenes Formular" in text
    assert "Formulare" not in text
    assert "/formulare_offen" in text


def test_ping_text_plural():
    text = _build_ping_text(7)
    assert "7 offene Formulare" in text
    assert "/formulare_offen" in text


# =====================================================================
# _should_trigger
# =====================================================================

def _local(year, month, day, hour, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)


def test_should_trigger_when_after_ping_time_and_not_run_today():
    now = _local(2026, 5, 17, PING_HOUR_LOCAL, PING_MINUTE_LOCAL)
    assert _should_trigger(now, last_run_date=None) is True


def test_should_trigger_one_minute_after_threshold():
    now = _local(2026, 5, 17, PING_HOUR_LOCAL, PING_MINUTE_LOCAL + 1)
    assert _should_trigger(now, last_run_date=None) is True


def test_should_not_trigger_before_ping_time():
    now = _local(2026, 5, 17, PING_HOUR_LOCAL - 1, 59)
    assert _should_trigger(now, last_run_date=None) is False


def test_should_not_trigger_if_already_ran_today():
    now = _local(2026, 5, 17, PING_HOUR_LOCAL + 2, 0)
    today = dt.date(2026, 5, 17)
    assert _should_trigger(now, last_run_date=today) is False


def test_should_trigger_next_day_after_yesterday_run():
    now = _local(2026, 5, 18, PING_HOUR_LOCAL, PING_MINUTE_LOCAL)
    yesterday = dt.date(2026, 5, 17)
    assert _should_trigger(now, last_run_date=yesterday) is True


# =====================================================================
# Status-Validation
# =====================================================================

@pytest.mark.asyncio
async def test_set_status_rejects_invalid(monkeypatch):
    """Ein Tippfehler im Status-Code soll nichts veraendern."""
    from core.integrations import formular_eingang as fe
    result = await fe.set_status(uuid.uuid4(), status="bogus")
    assert result is False


def test_status_label_covers_all_states():
    """Wenn jemand spaeter einen neuen Status hinzufuegt, soll dieser
    Test rot werden bis ein Label existiert — Telegram-UI haette sonst
    ein leeres Button-Text-Feld."""
    from core.models import FORMULAR_STATUS_VALID, FORMULAR_STATUS_LABEL
    for s in FORMULAR_STATUS_VALID:
        assert s in FORMULAR_STATUS_LABEL, f"Label fehlt fuer {s}"
        assert FORMULAR_STATUS_LABEL[s]


def test_open_set_is_subset_of_valid():
    """OFFEN muss eine Teilmenge von VALID sein — sonst koennten
    find_tenants_with_overdue auf Status-Werte filtern die die
    DB-Constraint gar nicht zulaesst."""
    from core.models import FORMULAR_STATUS_OFFEN, FORMULAR_STATUS_VALID
    assert FORMULAR_STATUS_OFFEN.issubset(FORMULAR_STATUS_VALID)
