"""Tests fuer das Gemini-Smart-Routing in choose_employee.

Deckt:
- Gemini-Pick gewinnt (auch gegen den Stichwort-Treffer)
- Gemini abstain (None) -> Fallback auf Stichwort-Logik
- Gemini-Fehler/Exception -> Fallback (kein Crash)
- Smart-Routing deaktiviert -> reine Stichwort-Logik
- nur 1 Mitarbeiter -> only-active, Gemini wird gar nicht gefragt

Gemini selbst ist gemockt (kein echter Call) — schnell + deterministisch.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.routing import employee_router as er
from core.routing.employee_router import choose_employee


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        v = self._value if isinstance(self._value, list) else [self._value]
        return SimpleNamespace(all=lambda: v)

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


def _session_factory(results):
    queue = list(results)

    class _Session:
        async def execute(self, stmt):
            return _FakeResult(queue.pop(0) if queue else None)

        def add(self, obj):
            pass

        def expunge(self, obj):
            pass

        async def commit(self):
            pass

    @asynccontextmanager
    async def cm():
        yield _Session()

    def factory():
        return cm()

    return factory


def _emp(**kw):
    base = dict(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), slug="emp", name="Emp",
        is_active=True, is_default=False, arbeitstage=None, arbeitszeiten=None,
        skills=None, calendar_provider=None, calendar_id=None, job_title=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_rank(monkeypatch, *, return_value=None, side_effect=None):
    """Mockt core.ai.gemini.rank_employee_for_request — choose_employee
    importiert die Funktion erst zur Laufzeit aus dem Modul, daher reicht
    setattr auf das Modul-Attribut."""
    import core.ai.gemini as gem
    mock = AsyncMock(return_value=return_value, side_effect=side_effect)
    monkeypatch.setattr(gem, "rank_employee_for_request", mock)
    return mock


@pytest.fixture(autouse=True)
def _enable_smart_routing(monkeypatch):
    monkeypatch.setattr(er.settings, "smart_routing_enabled", True)


def _two_emps():
    return (
        _emp(slug="hans", name="Hans", skills=["heizung"]),
        _emp(slug="maler", name="Maler", skills=["maler"]),
    )


@pytest.mark.asyncio
async def test_gemini_pick_overrides_keyword(monkeypatch):
    """Gemini waehlt 'maler' obwohl der Text 'Heizung' sagt — die
    Gemini-Entscheidung gewinnt gegen den Stichwort-Treffer."""
    hans, maler = _two_emps()
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))
    _patch_rank(monkeypatch, return_value="maler")
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Die Heizung ist kaputt")
    assert dec.reason == "gemini-skill-match"
    assert dec.employee_slug == "maler"


@pytest.mark.asyncio
async def test_gemini_none_falls_back_to_keyword(monkeypatch):
    """Gemini findet keinen klaren Treffer (None) -> Stichwort-Logik."""
    hans, maler = _two_emps()
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))
    _patch_rank(monkeypatch, return_value=None)
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Die Heizung ist kaputt")
    assert dec.reason == "skill-match"
    assert dec.employee_slug == "hans"


@pytest.mark.asyncio
async def test_gemini_exception_falls_back(monkeypatch):
    """Gemini-Call wirft -> Router faengt ab und faellt auf Stichwort
    zurueck (kein Crash)."""
    hans, maler = _two_emps()
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))
    _patch_rank(monkeypatch, side_effect=RuntimeError("Gemini tot"))
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Die Heizung ist kaputt")
    assert dec.reason == "skill-match"
    assert dec.employee_slug == "hans"


@pytest.mark.asyncio
async def test_gemini_invalid_slug_falls_back(monkeypatch):
    """Gemini liefert einen slug der nicht existiert -> Fallback."""
    hans, maler = _two_emps()
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))
    _patch_rank(monkeypatch, return_value="gibtsnicht")
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Die Heizung ist kaputt")
    assert dec.reason == "skill-match"
    assert dec.employee_slug == "hans"


@pytest.mark.asyncio
async def test_smart_routing_disabled_uses_keyword(monkeypatch):
    """Flag aus -> Gemini wird nicht gefragt, reine Stichwort-Logik."""
    monkeypatch.setattr(er.settings, "smart_routing_enabled", False)
    hans, maler = _two_emps()
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))
    mock = _patch_rank(monkeypatch, return_value="maler")
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Die Heizung ist kaputt")
    assert dec.employee_slug == "hans"
    assert dec.reason == "skill-match"
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_employee_skips_gemini(monkeypatch):
    """Nur 1 aktiver Mitarbeiter -> only-active, Gemini wird nicht gefragt."""
    solo = _emp(slug="solo", name="Solo", skills=["heizung"])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[solo]]))
    mock = _patch_rank(monkeypatch, return_value="solo")
    dec = await choose_employee(uuid.uuid4(), anliegen_text="Heizung kaputt")
    assert dec.reason == "only-active"
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_anliegen_text_skips_gemini(monkeypatch):
    """Ohne Anliegen-Text gibt es nichts zu interpretieren -> kein Gemini,
    Fallback-Default."""
    a = _emp(slug="a", skills=[])
    b = _emp(slug="b", skills=[])
    default = _emp(slug="inhaber", name="Inhaber", is_default=True, skills=[])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[a, b]]))
    monkeypatch.setattr(
        "core.models.employee.get_default_employee",
        AsyncMock(return_value=default),
    )
    mock = _patch_rank(monkeypatch, return_value="a")
    dec = await choose_employee(uuid.uuid4(), anliegen_text="")
    assert dec.reason == "fallback-default"
    mock.assert_not_awaited()
