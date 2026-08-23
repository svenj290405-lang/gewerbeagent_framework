"""Tests fuer Mitarbeiter-Abwesenheit (Krank/Urlaub), Verfuegbarkeit,
Skill-Routing und die Termin-Umverteilung bei Krankmeldung.

Ergaenzt tests/test_notification_routing.py (das _notify_move abdeckt).

Deckt:
- extract_skills_from_text: Substring-Skill-Erkennung (Sanitaer/Heizung/…)
- EmployeeAbsence.covers: Datums-Abdeckung (geschlossen + open-ended)
- create_absence: Eingabe-Validierung (Typ, end < start) — vor jedem DB-Call
- is_employee_working_at: aktiv + Absence + Arbeitstag + Arbeitszeit
- _run_cron_for_today: KERNREGEL — nur 'krank' wird umverteilt, 'urlaub' nicht

Alle DB-Zugriffe sind gemockt (kein echtes Postgres noetig) — gleiche
Test-Double-Konvention wie die uebrigen Tests.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.models.employee_absence as ea
from core.models.employee_absence import (
    ABSENCE_KRANK,
    ABSENCE_URLAUB,
    EmployeeAbsence,
)
from core.integrations import absence_redistribution as ar
from core.routing import employee_router as er
from core.routing.employee_router import choose_employee, extract_skills_from_text


# =====================================================================
# Test-Doubles: gemockte AsyncSessionLocal
# =====================================================================


class _FakeResult:
    """Unterstuetzt scalar_one_or_none(), scalars().all() und all()."""

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
    """AsyncSessionLocal-Ersatz: FIFO-Queue ueber alle `async with`-Bloecke;
    jeder execute() pop't das naechste Ergebnis."""
    queue = list(results)

    class _Session:
        async def execute(self, stmt):
            return _FakeResult(queue.pop(0) if queue else None)

        def add(self, obj):  # no-op
            pass

        def expunge(self, obj):
            pass

        def expunge_all(self):
            pass

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

    @asynccontextmanager
    async def cm():
        yield _Session()

    def factory():
        return cm()

    return factory


def _emp(**kw):
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        slug="emp",
        name="Emp",
        is_active=True,
        is_default=False,
        arbeitstage=None,
        arbeitszeiten=None,
        skills=None,
        calendar_provider=None,
        calendar_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clear_redistribution_state():
    """Die In-Memory-Anti-Doppellauf-Sets sind Modul-global — vor jedem
    Test leeren, damit Tests sich nicht gegenseitig beeinflussen."""
    ar._processed_events.clear()
    ar._inflight.clear()
    yield
    ar._processed_events.clear()
    ar._inflight.clear()


@pytest.fixture(autouse=True)
def _disable_smart_routing(monkeypatch):
    """Diese Tests pruefen die deterministische Stichwort-Logik. Das neue
    Gemini-Smart-Routing hier abschalten, sonst wuerde choose_employee
    einen echten Gemini-Call versuchen (Latenz/Flakiness). Der Gemini-Pfad
    hat eigene Tests in test_employee_router_gemini.py."""
    monkeypatch.setattr(er.settings, "smart_routing_enabled", False)


# =====================================================================
# extract_skills_from_text (Skill-Erkennung — pure)
# =====================================================================


def test_skill_single_match():
    assert extract_skills_from_text("Der Wasserhahn tropft") == ["sanitaer"]


def test_skill_heizung():
    assert extract_skills_from_text("Heizung wird nicht warm") == ["heizung"]


def test_skill_multiple_distinct():
    skills = extract_skills_from_text("Heizung defekt und eine Steckdose kaputt")
    assert set(skills) == {"heizung", "elektrik"}


def test_skill_dedup_same_skill_twice():
    # 'steckdose' und 'lampe' mappen beide auf 'elektrik' -> nur einmal
    assert extract_skills_from_text("Steckdose und Lampe defekt") == ["elektrik"]


def test_skill_case_insensitive_and_umlaut():
    assert extract_skills_from_text("KÜCHE neu bauen") == ["tischler"]
    assert extract_skills_from_text("Die Spülung läuft") == ["sanitaer"]


def test_skill_no_match_and_empty():
    assert extract_skills_from_text("Allgemeine Anfrage ohne Gewerk") == []
    assert extract_skills_from_text("") == []
    assert extract_skills_from_text(None) == []


# =====================================================================
# EmployeeAbsence.covers (Datums-Abdeckung — pure)
# =====================================================================


def _absence(start, end, typ=ABSENCE_KRANK):
    return EmployeeAbsence(
        start_date=start, end_date=end, absence_type=typ,
        tenant_id=uuid.uuid4(), employee_id=uuid.uuid4(),
    )


def test_covers_closed_range_boundaries():
    a = _absence(dt.date(2026, 5, 10), dt.date(2026, 5, 14))
    assert a.covers(dt.date(2026, 5, 9)) is False    # davor
    assert a.covers(dt.date(2026, 5, 10)) is True    # start
    assert a.covers(dt.date(2026, 5, 12)) is True    # mitte
    assert a.covers(dt.date(2026, 5, 14)) is True    # end (inklusiv)
    assert a.covers(dt.date(2026, 5, 15)) is False   # danach


def test_covers_open_ended():
    a = _absence(dt.date(2026, 5, 10), None)  # "krank, weiss noch nicht"
    assert a.covers(dt.date(2026, 5, 9)) is False
    assert a.covers(dt.date(2026, 5, 10)) is True
    assert a.covers(dt.date(2030, 1, 1)) is True  # offen -> immer ab start


# =====================================================================
# create_absence — Validierung greift VOR jedem DB-Zugriff
# =====================================================================


@pytest.mark.asyncio
async def test_create_absence_invalid_type_raises():
    with pytest.raises(ValueError):
        await ea.create_absence(
            employee_id=uuid.uuid4(),
            start_date=dt.date(2026, 5, 10),
            end_date=dt.date(2026, 5, 12),
            absence_type="quatsch",
        )


@pytest.mark.asyncio
async def test_create_absence_end_before_start_raises():
    with pytest.raises(ValueError):
        await ea.create_absence(
            employee_id=uuid.uuid4(),
            start_date=dt.date(2026, 5, 12),
            end_date=dt.date(2026, 5, 10),
            absence_type=ABSENCE_KRANK,
        )


# =====================================================================
# is_employee_working_at (aktiv + Absence + Arbeitstag + Arbeitszeit)
# =====================================================================


@pytest.mark.asyncio
async def test_working_inactive_employee_false(monkeypatch):
    emp = _emp(is_active=False)
    monkeypatch.setattr("core.database.AsyncSessionLocal", _session_factory([emp]))
    target = dt.datetime(2026, 5, 20, 10, 0)
    assert await ea.is_employee_working_at(emp.id, target) is False


@pytest.mark.asyncio
async def test_working_absent_false(monkeypatch):
    emp = _emp(is_active=True)
    monkeypatch.setattr("core.database.AsyncSessionLocal", _session_factory([emp]))
    monkeypatch.setattr(ea, "is_employee_absent_on", AsyncMock(return_value=True))
    target = dt.datetime(2026, 5, 20, 10, 0)
    assert await ea.is_employee_working_at(emp.id, target) is False


@pytest.mark.asyncio
async def test_working_wrong_weekday_false(monkeypatch):
    target = dt.datetime(2026, 5, 20, 10, 0)
    # Arbeitstage explizit OHNE den Wochentag von target
    emp = _emp(is_active=True, arbeitstage=[(target.weekday() + 1) % 7])
    monkeypatch.setattr("core.database.AsyncSessionLocal", _session_factory([emp]))
    monkeypatch.setattr(ea, "is_employee_absent_on", AsyncMock(return_value=False))
    assert await ea.is_employee_working_at(emp.id, target) is False


@pytest.mark.asyncio
async def test_working_outside_hours_false(monkeypatch):
    target = dt.datetime(2026, 5, 20, 19, 30)  # nach Feierabend
    emp = _emp(
        is_active=True, arbeitstage=[target.weekday()],
        arbeitszeiten={"start": "08:00", "end": "17:00"},
    )
    monkeypatch.setattr("core.database.AsyncSessionLocal", _session_factory([emp]))
    monkeypatch.setattr(ea, "is_employee_absent_on", AsyncMock(return_value=False))
    assert await ea.is_employee_working_at(emp.id, target) is False


@pytest.mark.asyncio
async def test_working_available_true(monkeypatch):
    target = dt.datetime(2026, 5, 20, 10, 0)
    emp = _emp(
        is_active=True, arbeitstage=[target.weekday()],
        arbeitszeiten={"start": "08:00", "end": "17:00"},
    )
    monkeypatch.setattr("core.database.AsyncSessionLocal", _session_factory([emp]))
    monkeypatch.setattr(ea, "is_employee_absent_on", AsyncMock(return_value=False))
    assert await ea.is_employee_working_at(emp.id, target) is True


# =====================================================================
# _run_cron_for_today — KERNREGEL: nur 'krank' wird umverteilt
# =====================================================================


@pytest.mark.asyncio
async def test_cron_redistributes_krank_not_urlaub(monkeypatch):
    """Der Tages-Cron darf NUR Krank-Termine umverteilen — Urlaub ist
    vorausgeplant und bleibt unangetastet (Inhaber-Wille)."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    emp_krank = SimpleNamespace(id=uuid.uuid4(), slug="max", name="Max")
    emp_urlaub = SimpleNamespace(id=uuid.uuid4(), slug="anna", name="Anna")
    abs_krank = SimpleNamespace(absence_type=ABSENCE_KRANK)
    abs_urlaub = SimpleNamespace(absence_type=ABSENCE_URLAUB)

    # select(Tenant).scalars().all() -> [tenant]
    monkeypatch.setattr(ar, "AsyncSessionLocal", _session_factory([[tenant]]))
    monkeypatch.setattr(
        ar, "get_active_absences",
        AsyncMock(return_value=[(emp_krank, abs_krank), (emp_urlaub, abs_urlaub)]),
    )

    calls: list[uuid.UUID] = []

    async def _fake_redistribute(tenant_id, emp_id, date_range):
        calls.append(emp_id)
        return ar.RedistributionReport(
            sick_emp_slug="x", sick_emp_name="x", date_range=date_range,
        )

    monkeypatch.setattr(ar, "redistribute_for_employee", _fake_redistribute)
    monkeypatch.setattr(ar, "_send_report_to_inhaber", AsyncMock())

    await ar._run_cron_for_today(dt.date(2026, 5, 20))

    # Nur der Kranke wurde umverteilt — der Urlauber nicht.
    assert calls == [emp_krank.id]


# =====================================================================
# Termin-Verschiebung: Event wird im Ersatz-Kalender angelegt und beim
# Kranken geloescht
# =====================================================================


def _event(event_id="e1", subject="Heizung Kunde"):
    return {
        "event_id": event_id,
        "subject": subject,
        "body_preview": "Kessel tropft",
        "location": "Hauptstr 5",
        "start_dt": dt.datetime(2026, 5, 20, 9, 0),
        "end_dt": dt.datetime(2026, 5, 20, 10, 0),
    }


@pytest.mark.asyncio
async def test_move_event_creates_in_substitute_then_deletes_from_sick(monkeypatch):
    """_move_event: ERST im neuen Kalender anlegen, DANN beim Kranken
    loeschen — kein Event-Verlust."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max", calendar_provider="google", calendar_id="primary")
    new = _emp(slug="anna", name="Anna", calendar_provider="google", calendar_id="primary")
    event = _event()

    create_mock = AsyncMock(return_value={"new_event_id": "n1", "html_link": "x"})
    delete_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(ar, "_create_event_for_employee", create_mock)
    monkeypatch.setattr(ar, "_delete_event_from_employee", delete_mock)

    res = await ar._move_event(tenant, sick, new, event)

    assert res["new_event_id"] == "n1"
    create_mock.assert_awaited_once()
    assert create_mock.await_args.args[0] is new      # angelegt beim Ersatz
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.args[0] is sick      # geloescht beim Kranken
    assert delete_mock.await_args.args[1] == "e1"


@pytest.mark.asyncio
async def test_handle_one_event_moves_to_substitute(monkeypatch):
    """_handle_one_event: Skill-Router liefert Ersatz -> Event verschoben,
    reason='moved'."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max", calendar_provider="google")
    new = _emp(slug="anna", name="Anna", calendar_provider="google")
    event = _event()

    decision = SimpleNamespace(
        employee_id=new.id, employee_slug="anna", reason="skill-match",
    )
    monkeypatch.setattr(ar, "choose_employee", AsyncMock(return_value=decision))
    monkeypatch.setattr(ar, "AsyncSessionLocal", _session_factory([new]))  # lädt new_emp
    monkeypatch.setattr(ar, "_create_event_for_employee",
                        AsyncMock(return_value={"new_event_id": "n1"}))
    monkeypatch.setattr(ar, "_delete_event_from_employee", AsyncMock(return_value=True))
    monkeypatch.setattr(ar, "_notify_move", AsyncMock())

    res = await ar._handle_one_event(tenant, sick, event, dt.date(2026, 5, 20))

    assert res.reason == "moved"
    assert res.new_emp_slug == "anna"


@pytest.mark.asyncio
async def test_handle_one_event_no_coverage_does_not_move(monkeypatch):
    """Kein Kollege verfuegbar -> reason='no-coverage', KEIN Kalender-Move."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", calendar_provider="google")
    event = _event(event_id="e2")

    monkeypatch.setattr(
        ar, "choose_employee",
        AsyncMock(return_value=SimpleNamespace(
            employee_id=uuid.uuid4(), employee_slug="x", reason="no-coverage",
        )),
    )
    create_mock = AsyncMock()
    monkeypatch.setattr(ar, "_create_event_for_employee", create_mock)

    res = await ar._handle_one_event(tenant, sick, event, dt.date(2026, 5, 20))

    assert res.reason == "no-coverage"
    create_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_redistribute_for_employee_collects_moved(monkeypatch):
    """redistribute_for_employee: Tagesschleife sammelt die verschobenen
    Events in report.reassigned."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max", calendar_provider="google")
    # Lädt nacheinander tenant + sick (scalar_one_or_none x2)
    monkeypatch.setattr(ar, "AsyncSessionLocal", _session_factory([tenant, sick]))
    monkeypatch.setattr(
        ar, "_list_events_for_employee_day", AsyncMock(return_value=[_event()]),
    )
    moved = ar.EventRedistributionResult(
        event_id="e1", event_subject="Heizung Kunde",
        event_start=dt.datetime(2026, 5, 20, 9, 0),
        sick_emp_slug="max", new_emp_slug="anna", reason="moved",
    )
    monkeypatch.setattr(ar, "_handle_one_event", AsyncMock(return_value=moved))

    report = await ar.redistribute_for_employee(
        tenant.id, sick.id, (dt.date(2026, 5, 20), dt.date(2026, 5, 20)),
    )

    assert len(report.reassigned) == 1
    assert report.reassigned[0].new_emp_slug == "anna"
    assert not report.no_coverage and not report.errors


# =====================================================================
# Buchung: bei Krankheit wird KEIN Termin beim Abwesenden gebucht
# (choose_employee mit target_datetime filtert Abwesende raus)
# =====================================================================


@pytest.mark.asyncio
async def test_choose_employee_skips_absent_books_available(monkeypatch):
    """Anfrage mit Termin-Zeitpunkt: der Abwesende faellt raus, der
    Verfuegbare wird gewaehlt."""
    tenant_id = uuid.uuid4()
    absent = _emp(slug="max", name="Max", skills=["heizung"])
    available = _emp(slug="anna", name="Anna", skills=["heizung"])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[absent, available]]))

    async def _working(emp_id, target, *, dauer_minuten=None):
        # dauer_minuten kam dazu, als der Verfuegbarkeits-Check auch das
        # Termin-ENDE prueft (16:45 + 3h liegt nach Feierabend).
        return emp_id == available.id  # nur Anna arbeitet

    monkeypatch.setattr(
        "core.models.employee_absence.is_employee_working_at",
        AsyncMock(side_effect=_working),
    )
    decision = await choose_employee(
        tenant_id, anliegen_text="Heizung kaputt",
        target_datetime=dt.datetime(2026, 5, 20, 9, 0),
    )
    assert decision.employee_slug == "anna"  # der Kranke wird nicht gebucht


@pytest.mark.asyncio
async def test_choose_employee_all_absent_returns_no_coverage(monkeypatch):
    """Ist der einzige passende Mitarbeiter krank -> no-coverage (kein
    Termin gebucht, Eskalation an den Inhaber)."""
    tenant_id = uuid.uuid4()
    absent = _emp(slug="max", name="Max", skills=["heizung"], is_default=True)
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[absent]]))
    monkeypatch.setattr(
        "core.models.employee_absence.is_employee_working_at",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "core.models.employee.get_default_employee",
        AsyncMock(return_value=absent),
    )
    decision = await choose_employee(
        tenant_id, anliegen_text="Heizung kaputt",
        target_datetime=dt.datetime(2026, 5, 20, 9, 0),
    )
    assert decision.reason == "no-coverage"


# =====================================================================
# Skill-Routing: passender Mitarbeiter wird per Skill gewaehlt
# =====================================================================


@pytest.mark.asyncio
async def test_choose_employee_picks_matching_skill(monkeypatch):
    """'Heizung'-Anfrage -> der Mitarbeiter mit Skill 'heizung' gewinnt."""
    tenant_id = uuid.uuid4()
    hans = _emp(slug="hans", name="Hans", skills=["heizung"])
    maler = _emp(slug="maler", name="Maler", skills=["maler"])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[hans, maler]]))

    decision = await choose_employee(tenant_id, anliegen_text="Die Heizung ist kaputt")

    assert decision.reason == "skill-match"
    assert decision.employee_slug == "hans"
    assert decision.score == 1.0


@pytest.mark.asyncio
async def test_choose_employee_no_skill_falls_back_to_default(monkeypatch):
    """Kein Skill-Treffer im Text -> Default-Employee (fallback-default)."""
    tenant_id = uuid.uuid4()
    a = _emp(slug="a", skills=[])
    b = _emp(slug="b", skills=[])
    default = _emp(slug="inhaber", name="Inhaber", is_default=True, skills=[])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[a, b]]))
    monkeypatch.setattr(
        "core.models.employee.get_default_employee",
        AsyncMock(return_value=default),
    )
    decision = await choose_employee(
        tenant_id, anliegen_text="Eine ganz allgemeine Frage ohne Gewerk",
    )
    assert decision.reason == "fallback-default"
    assert decision.employee_slug == "inhaber"


# =====================================================================
# Datenerhalt beim Verschieben
#
# Die Tagesliste liefert die Beschreibung nur gekuerzt (Google 300,
# Microsoft ~255 Zeichen) und ohne die extendedProperties. Eine Kopie
# daraus verlor den Drive-Link mitten in der URL und die Kunden-
# Metadaten — danach fand die Storno-Suche den Termin per Mail oder
# Telefonnummer nicht mehr.
# =====================================================================

_LANGE_BESCHREIBUNG = (
    "Betrieb: Schreiberei Jantos\n"
    "Kunde: Anna Meier\n"
    "Anliegen: Einbauschrank Wohnzimmer, Aufmass\n"
    "Adresse: Musterstrasse 12, 34117 Kassel\n"
    "Telefon: +49 561 1234567\n"
    "E-Mail: anna.meier@example.de\n"
    "Zustaendig: Max Mustermann\n"
    "Unterlagen (Drive): https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz\n\n"
    "Eingetragen via KI-Agent Q (Gewerbeagent Framework)\n"
    "GA-Ref: mail-3f7a91c2-4b8e-4d1a-9c33-77aa10bb5e22"
)


@pytest.mark.asyncio
async def test_move_event_holt_volltext_statt_vorschau(monkeypatch):
    """Der verschobene Termin behaelt die GANZE Beschreibung."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max", calendar_provider="google", calendar_id="primary")
    new = _emp(slug="anna", name="Anna", calendar_provider="google", calendar_id="primary")
    event = _event()
    event["body_preview"] = _LANGE_BESCHREIBUNG[:300]   # so kommt es aus der Liste

    monkeypatch.setattr(ar, "_event_volltext", AsyncMock(return_value={
        "description": _LANGE_BESCHREIBUNG,
        "props": {
            "kunde_email": "anna.meier@example.de",
            "kunde_telefon": "+495611234567",
            "ga_ref": "mail-3f7a91c2",
        },
    }))
    create_mock = AsyncMock(return_value={"new_event_id": "n1"})
    monkeypatch.setattr(ar, "_create_event_for_employee", create_mock)
    monkeypatch.setattr(ar, "_delete_event_from_employee", AsyncMock(return_value=True))

    await ar._move_event(tenant, sick, new, event)

    kw = create_mock.await_args.kwargs
    # Der Drive-Link steht am ENDE — genau er fiel der Kuerzung zum Opfer.
    assert "1AbCdEfGhIjKlMnOpQrStUvWxYz" in kw["description"]
    assert "GA-Ref" in kw["description"]
    # Metadaten wandern mit, sonst ist der Termin nicht mehr auffindbar.
    assert kw["props"]["kunde_email"] == "anna.meier@example.de"
    assert kw["props"]["kunde_telefon"] == "+495611234567"


@pytest.mark.asyncio
async def test_move_event_vermerkt_die_uebernahme(monkeypatch):
    """Im Kalender soll stehen, warum da jemand anderes drin ist."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max Mustermann", calendar_provider="google")
    new = _emp(slug="anna", name="Anna", calendar_provider="google")
    monkeypatch.setattr(ar, "_event_volltext", AsyncMock(
        return_value={"description": "Kessel tropft", "props": {}}))
    create_mock = AsyncMock(return_value={"new_event_id": "n1"})
    monkeypatch.setattr(ar, "_create_event_for_employee", create_mock)
    monkeypatch.setattr(ar, "_delete_event_from_employee", AsyncMock(return_value=True))

    await ar._move_event(tenant, sick, new, _event())

    kw = create_mock.await_args.kwargs
    assert "Max Mustermann" in kw["description"]
    assert "umverteilt" in kw["description"].lower()
    assert kw["props"]["ga_umverteilt_von"] == "max"


@pytest.mark.asyncio
async def test_move_event_faellt_auf_vorschau_zurueck(monkeypatch):
    """Kein Volltext abrufbar -> lieber die gekuerzte Fassung als nichts."""
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="demo")
    sick = _emp(slug="max", name="Max", calendar_provider="google")
    new = _emp(slug="anna", name="Anna", calendar_provider="google")
    monkeypatch.setattr(ar, "_event_volltext", AsyncMock(
        return_value={"description": "", "props": {}}))
    create_mock = AsyncMock(return_value={"new_event_id": "n1"})
    monkeypatch.setattr(ar, "_create_event_for_employee", create_mock)
    monkeypatch.setattr(ar, "_delete_event_from_employee", AsyncMock(return_value=True))

    await ar._move_event(tenant, sick, new, _event())
    assert "Kessel tropft" in create_mock.await_args.kwargs["description"]


@pytest.mark.asyncio
async def test_routing_ohne_zeitpunkt_meidet_abwesende(monkeypatch):
    """Rueckruf und Anfrage-Formular haben keinen Termin-Zeitpunkt.

    Ohne `nur_heute_verfuegbare` lief der Abwesenheits-Filter dort gar
    nicht: der Skill-Match waehlte den Kollegen, der zwei Wochen im
    Urlaub ist, und die Rueckrufbitte blieb dort liegen.
    """
    tenant_id = uuid.uuid4()
    abwesend = _emp(slug="max", name="Max", skills=["heizung"])
    da = _emp(slug="anna", name="Anna", skills=["heizung"])
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[abwesend, da]]))

    async def _absent(emp_id, tag):
        return emp_id == abwesend.id

    monkeypatch.setattr(
        "core.models.employee_absence.is_employee_absent_on",
        AsyncMock(side_effect=_absent),
    )
    entscheidung = await choose_employee(
        tenant_id, anliegen_text="Heizung kaputt", nur_heute_verfuegbare=True,
    )
    assert entscheidung.employee_slug == "anna"


@pytest.mark.asyncio
async def test_routing_ohne_flag_bleibt_unveraendert(monkeypatch):
    """Ohne das Flag darf sich nichts aendern — sonst zoege der Filter in
    Pfade ein, die bewusst jeden Mitarbeiter zulassen sollen."""
    tenant_id = uuid.uuid4()
    a = _emp(slug="max", name="Max", skills=["heizung"], is_default=True)
    monkeypatch.setattr(er, "AsyncSessionLocal", _session_factory([[a]]))
    absent_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "core.models.employee_absence.is_employee_absent_on", absent_mock,
    )
    entscheidung = await choose_employee(tenant_id, anliegen_text="Heizung kaputt")
    assert entscheidung.employee_slug == "max"
    absent_mock.assert_not_awaited()
