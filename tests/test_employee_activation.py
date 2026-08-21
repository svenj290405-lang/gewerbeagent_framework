"""Tests fuer Teil C der Multi-Mitarbeiter-Erweiterung:
Mitarbeiter-Aktivierungs-Token.

Deckt:
- _generate_token: laenge + URL-safe
- EmployeeActivationToken.is_valid: used_at + expires_at-Logik
- create_activation_token: erzeugt Token mit korrektem Lifecycle,
  ruft session.add + commit
- consume_activation_token: valid → used_at gesetzt + Row zurueck,
  expired/used/unknown → None
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.models import employee_activation_token as eat


# =====================================================================
# Fake Session mit add/commit/refresh/expunge fuer Lifecycle-Tests
# =====================================================================


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    def __init__(self, *, execute_returns=None):
        self.execute_returns = execute_returns
        self.added = []
        self.commit_count = 0
        self.refreshed = []
        self.expunged = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)

    def expunge(self, obj):
        self.expunged.append(obj)

    async def execute(self, stmt):
        return _FakeResult(self.execute_returns)


def _make_session_factory(*, execute_returns=None):
    """Liefert AsyncSessionLocal-Ersatz. Optional ein vorgebackenes
    execute().scalar_one_or_none()-Result."""
    holder = {}

    @asynccontextmanager
    async def cm():
        session = _FakeSession(execute_returns=execute_returns)
        holder["session"] = session
        yield session

    def factory():
        return cm()

    factory._holder = holder  # damit Tests die Session inspizieren koennen
    return factory


# =====================================================================
# Pure helpers
# =====================================================================


def test_generate_token_url_safe_and_long_enough():
    t = eat._generate_token()
    assert isinstance(t, str)
    # token_urlsafe(48) liefert ca. 64 Zeichen (4/3 base64-Expansion).
    assert 60 <= len(t) <= 70
    # URL-safe Charset: a-z A-Z 0-9 - _
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]+", t)


def test_is_valid_fresh_token():
    tok = eat.EmployeeActivationToken(
        tenant_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        token="abc",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
        used_at=None,
    )
    assert tok.is_valid() is True


def test_is_valid_expired():
    tok = eat.EmployeeActivationToken(
        tenant_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        token="abc",
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1),
        used_at=None,
    )
    assert tok.is_valid() is False


def test_is_valid_used():
    tok = eat.EmployeeActivationToken(
        tenant_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        token="abc",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
        used_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
    )
    assert tok.is_valid() is False


# =====================================================================
# create_activation_token
# =====================================================================


@pytest.mark.asyncio
async def test_create_activation_token_inserts_with_correct_fields(monkeypatch):
    factory = _make_session_factory()
    # Lazy-Import-Pattern in den Helpern: AsyncSessionLocal wird im
    # Funktions-Body von core.database importiert, daher Source-Patch.
    monkeypatch.setattr("core.database.AsyncSessionLocal", factory)

    tenant_id = uuid.uuid4()
    emp_id = uuid.uuid4()
    obj = await eat.create_activation_token(tenant_id, emp_id, ttl_days=7)

    assert obj.tenant_id == tenant_id
    assert obj.employee_id == emp_id
    assert isinstance(obj.token, str) and len(obj.token) > 30
    # expires_at ~7 Tage in der Zukunft
    delta = obj.expires_at - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(days=6, hours=23) < delta < dt.timedelta(days=7, hours=1)
    assert obj.used_at is None

    session = factory._holder["session"]
    assert len(session.added) == 1 and session.added[0] is obj
    assert session.commit_count == 1


# =====================================================================
# consume_activation_token
# =====================================================================


def _make_token_row(*, used_at=None, expires_in=dt.timedelta(days=3)):
    return eat.EmployeeActivationToken(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        token="sample-token-xyz",
        expires_at=dt.datetime.now(dt.timezone.utc) + expires_in,
        used_at=used_at,
    )


@pytest.mark.asyncio
async def test_consume_valid_token_marks_used(monkeypatch):
    row = _make_token_row()
    factory = _make_session_factory(execute_returns=row)
    # Lazy-Import-Pattern in den Helpern: AsyncSessionLocal wird im
    # Funktions-Body von core.database importiert, daher Source-Patch.
    monkeypatch.setattr("core.database.AsyncSessionLocal", factory)

    result = await eat.consume_activation_token("sample-token-xyz")

    assert result is row
    assert result.used_at is not None
    session = factory._holder["session"]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_consume_unknown_token_returns_none(monkeypatch):
    factory = _make_session_factory(execute_returns=None)
    # Lazy-Import-Pattern in den Helpern: AsyncSessionLocal wird im
    # Funktions-Body von core.database importiert, daher Source-Patch.
    monkeypatch.setattr("core.database.AsyncSessionLocal", factory)

    result = await eat.consume_activation_token("does-not-exist")
    assert result is None
    session = factory._holder["session"]
    assert session.commit_count == 0  # nichts geschrieben


@pytest.mark.asyncio
async def test_consume_expired_token_returns_none(monkeypatch):
    row = _make_token_row(expires_in=dt.timedelta(seconds=-10))
    factory = _make_session_factory(execute_returns=row)
    # Lazy-Import-Pattern in den Helpern: AsyncSessionLocal wird im
    # Funktions-Body von core.database importiert, daher Source-Patch.
    monkeypatch.setattr("core.database.AsyncSessionLocal", factory)

    result = await eat.consume_activation_token("abc")
    assert result is None
    session = factory._holder["session"]
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_consume_already_used_token_returns_none(monkeypatch):
    row = _make_token_row(
        used_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2),
    )
    factory = _make_session_factory(execute_returns=row)
    # Lazy-Import-Pattern in den Helpern: AsyncSessionLocal wird im
    # Funktions-Body von core.database importiert, daher Source-Patch.
    monkeypatch.setattr("core.database.AsyncSessionLocal", factory)

    result = await eat.consume_activation_token("abc")
    assert result is None
    session = factory._holder["session"]
    assert session.commit_count == 0


# =====================================================================
# _handle_activate_token_start
