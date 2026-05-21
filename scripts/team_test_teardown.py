"""ISOLIERTER Krank-E2E-Test — Aufraeumen.

Loescht die beiden Test-Kalender (samt aller darin liegenden Events,
inkl. der zu Anna verschobenen), alle Absences der Test-Mitarbeiter und
die Test-Mitarbeiter selbst. Faesst NIE 'primary' oder den Kalender des
Inhabers an.
"""
from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.models import Tenant, Employee, EmployeeAbsence
from core.models.employee import get_default_employee
from core.integrations.google_calendar import (
    GOOGLE_CAL_BASE, _ensure_fresh_access_token,
)
from core.security.oauth_token_lookup import find_oauth_token

TENANT_SLUG = "pilot"
TEST_SLUGS = ["max", "anna", "tom"]


async def _delete_calendar(access_token: str, cal_id: str) -> int:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{GOOGLE_CAL_BASE}/calendars/{cal_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.status_code


async def main():
    print("=" * 72)
    print(f"Krank-E2E-Teardown — Tenant '{TENANT_SLUG}'")
    print("=" * 72)

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == TENANT_SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"kein Tenant '{TENANT_SLUG}'.")
            return
        s.expunge(tenant)

    inhaber = await get_default_employee(tenant.id)
    inhaber_cal = (inhaber.calendar_id or "primary")
    token = await find_oauth_token(tenant.id, "google", inhaber.id)
    access = await _ensure_fresh_access_token(token) if token else None

    async with AsyncSessionLocal() as s:
        emps = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant.id,
                Employee.slug.in_(TEST_SLUGS),
            )
        )).scalars().all()
        for e in emps:
            s.expunge(e)

    if not emps:
        print("Keine Test-Mitarbeiter gefunden — nichts zu tun.")
        return

    # 1) Test-Kalender loeschen (mit Schutz-Guards)
    print("\n[1] Test-Kalender loeschen …")
    for e in emps:
        cal = e.calendar_id
        if not cal or cal in ("primary", inhaber_cal):
            print(f"    {e.slug}: kein loeschbarer Test-Kalender (skip: {cal!r})")
            continue
        if access is None:
            print(f"    {e.slug}: kein Token — Kalender {cal} NICHT geloescht!")
            continue
        code = await _delete_calendar(access, cal)
        ok = "ok" if code in (200, 204) else f"FEHLER {code}"
        print(f"    {e.slug}: Kalender {cal[:24]}… geloescht [{ok}]")

    # 2) Absences der Test-Mitarbeiter loeschen
    emp_ids = [e.id for e in emps]
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            delete(EmployeeAbsence).where(EmployeeAbsence.employee_id.in_(emp_ids))
        )
        await s.commit()
        print(f"\n[2] Absences geloescht: {res.rowcount}")

    # 3) Test-Mitarbeiter loeschen
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            delete(Employee).where(Employee.id.in_(emp_ids))
        )
        await s.commit()
        print(f"[3] Test-Mitarbeiter geloescht: {res.rowcount} ({TEST_SLUGS})")

    print("\n" + "=" * 72)
    print("Aufgeräumt. Echter Kalender des Inhabers blieb unberührt.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
