"""Krank-E2E mit ECHTEN Termindaten — prueft den Datenerhalt beim Umziehen.

Der alte Test (team_test_setup.py) legte Termine mit einer 20-Zeichen-
Beschreibung und ohne Kunden-Metadaten an — genau deshalb blieb der Fehler
unentdeckt, dass _move_event die Kopie aus der GEKUERZTEN Tagesliste baute
(Google body_preview [:300]) und extendedProperties gar nicht mitnahm.

Dieser Test legt deshalb Termine an, wie Q sie wirklich schreibt: lange
Beschreibung mit Drive-Link am ENDE plus kunde_email/kunde_telefon in den
extendedProperties. Nach der Umverteilung wird geprueft, ob beim Ersatz
alles ankommt und ob die Storno-Suche den Termin dort noch findet.

Isoliert: zwei NEUE sekundaere Kalender im Google-Konto des Inhabers,
zwei Test-Mitarbeiter. Der Primaer-Kalender wird nie angefasst.
Aufraeumen: scripts/team_test_teardown.py (deckt max/anna/tom ab).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee
from core.models.employee import get_default_employee
from core.models.employee_absence import create_absence
from core.integrations.google_calendar import (
    GOOGLE_CAL_BASE, _ensure_fresh_access_token, list_events_for_day,
    get_event_details,
)
from core.integrations.absence_redistribution import redistribute_for_employee
from core.security.oauth_token_lookup import find_oauth_token
from core.plugin_system.registry import discover_plugins

TENANT_SLUG = "pilot"
CAL_MAX = "🧪 Test Max (Krank-Datentest)"
CAL_ANNA = "🧪 Test Anna (Krank-Datentest)"

DRIVE_LINK = (
    "https://drive.google.com/drive/folders/"
    "1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789-TESTORDNER"
)


def lange_beschreibung(nr: int, kunde: str, mail: str, tel: str) -> str:
    """~450 Zeichen — der Drive-Link steht bewusst GANZ am Ende."""
    return (
        f"Auftrag {nr} — {kunde}\n"
        f"Kontakt: {mail} / {tel}\n"
        "Aufgenommen am Telefon durch Q. Kunde meldet: Heizung macht seit "
        "Freitag laute Geraeusche, Vorlauftemperatur schwankt, im Bad wird "
        "der Handtuchheizkoerper gar nicht mehr warm. Anlage ist Baujahr "
        "2011, letzte Wartung liegt nach Kundenangabe ueber zwei Jahre "
        "zurueck. Zugang ueber den Hof, Klingel unten rechts, Hund im "
        "Haus. Kunde ist ab 9 Uhr vor Ort und bittet um kurzen Anruf "
        "vorher.\n"
        f"Unterlagen: {DRIVE_LINK}"
    )


async def _neuer_kalender(access: str, summary: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{GOOGLE_CAL_BASE}/calendars",
            headers={"Authorization": f"Bearer {access}"},
            json={"summary": summary, "timeZone": "Europe/Berlin"},
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Kalender-Anlage {r.status_code}: {r.text[:200]}")
        return r.json()["id"]


async def _neuer_mitarbeiter(tenant_id, *, slug, name, skills, cal_id):
    async with AsyncSessionLocal() as s:
        vorhanden = (await s.execute(select(Employee).where(
            Employee.tenant_id == tenant_id, Employee.slug == slug,
        ))).scalar_one_or_none()
        if vorhanden is not None:
            raise RuntimeError(
                f"'{slug}' existiert schon — erst team_test_teardown.py."
            )
        emp = Employee(
            tenant_id=tenant_id, slug=slug, name=name,
            is_default=False, is_active=True, skills=skills,
            calendar_provider="google", calendar_id=cal_id,
            # Test laeuft evtl. am Wochenende: sonst waere niemand
            # verfuegbar und ALLES ginge in no-coverage.
            arbeitstage=[0, 1, 2, 3, 4, 5, 6],
            arbeitszeiten={"start": "08:00", "end": "18:00"},
        )
        s.add(emp)
        await s.commit()
        await s.refresh(emp)
        s.expunge(emp)
        return emp


async def main():
    discover_plugins()
    heute = dt.date.today()
    print("=" * 74)
    print(f"Krank-E2E mit echten Termindaten — '{TENANT_SLUG}', {heute}")
    print("=" * 74)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print("FEHLER: kein Tenant 'pilot'.")
            return 1
        s.expunge(tenant)

    inhaber = await get_default_employee(tenant.id)
    token = await find_oauth_token(tenant.id, "google", inhaber.id)
    if token is None:
        print("FEHLER: kein Google-Token.")
        return 1
    access = await _ensure_fresh_access_token(token)

    print("\n[1] Test-Kalender + Test-Mitarbeiter anlegen …")
    cal_max = await _neuer_kalender(access, CAL_MAX)
    cal_anna = await _neuer_kalender(access, CAL_ANNA)
    max_e = await _neuer_mitarbeiter(
        tenant.id, slug="max", name="Max Test", skills=["heizung"],
        cal_id=cal_max,
    )
    anna_e = await _neuer_mitarbeiter(
        tenant.id, slug="anna", name="Anna Test",
        skills=["heizung", "sanitaer"], cal_id=cal_anna,
    )
    print(f"    max  {max_e.id}  cal={cal_max[:30]}…")
    print(f"    anna {anna_e.id}  cal={cal_anna[:30]}…")

    print("\n[2] Drei Termine heute in Max' Kalender (ueber den Adapter, "
          "also MIT extendedProperties) …")
    from plugins.kalender.adapters import GoogleCalendarAdapter
    adapter_max = GoogleCalendarAdapter(
        tenant.id, calendar_id=cal_max, employee_id=max_e.id,
    )
    spec = [
        (dt.time(10, 0), "Heizung Müller – Kessel tropft", "Hauptstr. 5",
         "Familie Müller", "mueller.test@example.com", "+4915112345678",
         "ga-test-10"),
        (dt.time(14, 0), "Wasserhahn tropft – Bad Schmidt", "Lindenweg 12",
         "Frau Schmidt", "schmidt.test@example.com", "+4915187654321",
         "ga-test-14"),
        (dt.time(19, 0), "Abend-Notdienst (nach Feierabend)", "Ringstr. 3",
         "Herr Abend", "abend.test@example.com", "+4915199999999",
         "ga-test-19"),
    ]
    original: dict[str, dict] = {}
    for i, (t, betreff, ort, kunde, mail, tel, ref) in enumerate(spec, 1):
        start = dt.datetime.combine(heute, t)
        ende = start + dt.timedelta(hours=1)
        beschr = lange_beschreibung(i, kunde, mail, tel)
        res = await adapter_max.create_event(
            summary=betreff, description=beschr, location=ort,
            start=start, end=ende, timezone="Europe/Berlin",
            kunde_telefon_normalized=tel, kunde_email=mail,
            idempotency_key=ref,
        )
        eid = res.get("event_id") or res.get("id")
        original[betreff] = {
            "beschreibung": beschr, "mail": mail, "tel": tel, "ref": ref,
            "start": start, "event_id": eid,
        }
        print(f"    {t:%H:%M} {betreff[:38]:38} desc={len(beschr)} Zeichen")

    print("\n[3] Beweis, dass die Tagesliste kuerzt (das war die Fehlerquelle):")
    liste = await list_events_for_day(
        tenant.id, heute, employee_id=max_e.id, calendar_id=cal_max,
    )
    for ev in liste:
        voll = original.get(ev.get("subject"), {}).get("beschreibung", "")
        vorschau = ev.get("body_preview") or ""
        print(f"    {ev['start_dt']:%H:%M} body_preview={len(vorschau):3} von "
              f"{len(voll):3} Zeichen  Drive-Link in Vorschau: "
              f"{'ja' if DRIVE_LINK in vorschau else 'NEIN'}")

    print("\n[4] Max krankmelden + Umverteilung fuer heute starten …")
    await create_absence(
        max_e.id, heute, heute, "krank", notes="E2E-Datentest",
        created_by_employee_id=inhaber.id,
    )
    report = await redistribute_for_employee(tenant.id, max_e.id, (heute, heute))
    print(f"    verschoben:  {[(r.event_subject[:28], r.new_emp_slug) for r in report.reassigned]}")
    print(f"    no-coverage: {[r.event_subject[:28] for r in report.no_coverage]}")
    print(f"    Fehler:      {[(r.event_subject[:28], r.error) for r in report.errors]}")

    print("\n[5] Kontrolle beim Ersatz (Volltext + Metadaten nachladen) …")
    fehler = []
    anna_liste = await list_events_for_day(
        tenant.id, heute, employee_id=anna_e.id, calendar_id=cal_anna,
    )
    print(f"    Annas Kalender: {len(anna_liste)} Termin(e)")
    for ev in anna_liste:
        betreff = ev.get("subject") or ""
        orig = original.get(betreff)
        det = await get_event_details(
            tenant.id, ev["event_id"], employee_id=anna_e.id,
            calendar_id=cal_anna,
        )
        beschr = det.get("description") or ""
        props = det.get("props") or {}
        print(f"\n    ▸ {ev['start_dt']:%H:%M} {betreff}")
        print(f"      Beschreibung: {len(beschr)} Zeichen")
        if orig is None:
            fehler.append(f"{betreff}: kein Original bekannt")
            continue
        if orig["beschreibung"] not in beschr:
            fehler.append(f"{betreff}: Beschreibung nicht vollstaendig uebernommen")
            print("      ❌ Original-Beschreibung fehlt/ist gekuerzt")
        else:
            print("      ✅ Original-Beschreibung vollstaendig enthalten")
        if DRIVE_LINK in beschr:
            print("      ✅ Drive-Link unversehrt")
        else:
            fehler.append(f"{betreff}: Drive-Link abgeschnitten")
            print("      ❌ Drive-Link abgeschnitten")
        for key, erwartet in (("kunde_email", orig["mail"]),
                              ("kunde_telefon", orig["tel"]),
                              ("ga_ref", orig["ref"])):
            ist = props.get(key)
            ok = (ist == erwartet)
            print(f"      {'✅' if ok else '❌'} {key}: {ist!r}")
            if not ok:
                fehler.append(f"{betreff}: {key} {ist!r} != {erwartet!r}")
        vermerk = props.get("ga_umverteilt_von")
        print(f"      {'✅' if vermerk == 'max' else '❌'} ga_umverteilt_von: {vermerk!r}")
        if "umverteilt" not in beschr.lower():
            fehler.append(f"{betreff}: Uebernahme-Vermerk fehlt")

        # Der eigentliche Zweck der Metadaten: Storno-Suche.
        from plugins.kalender.adapters import GoogleCalendarAdapter
        adapter_anna = GoogleCalendarAdapter(
            tenant.id, calendar_id=cal_anna, employee_id=anna_e.id,
        )
        treffer = await adapter_anna.find_events(
            time_min=dt.datetime.combine(heute, dt.time(0, 0)),
            time_max=dt.datetime.combine(heute, dt.time(23, 59)),
            kunde_email=orig["mail"],
        )
        via = [t.get("match_source") for t in treffer]
        ok = any(t["event_id"] == ev["event_id"] for t in treffer)
        print(f"      {'✅' if ok else '❌'} Storno-Suche per Mail findet den "
              f"Termin ({len(treffer)} Treffer, via {via})")
        if not ok:
            fehler.append(f"{betreff}: per Kunden-Mail nicht mehr auffindbar")

    rest = await list_events_for_day(
        tenant.id, heute, employee_id=max_e.id, calendar_id=cal_max,
    )
    print(f"\n    Max' Kalender danach: {len(rest)} Termin(e) "
          f"{[e['subject'][:28] for e in rest]}")

    print("\n" + "=" * 74)
    if fehler:
        print("ERGEBNIS: ❌ FEHLER")
        for f in fehler:
            print(f"  - {f}")
    else:
        print("ERGEBNIS: ✅ alle Daten sind beim Ersatz vollstaendig angekommen")
    print("Aufraeumen: scripts/team_test_teardown.py")
    print("=" * 74)
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
