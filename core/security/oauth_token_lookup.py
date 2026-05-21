"""Zentraler Helper fuer OAuth-Token-Lookup mit employee-Aware-Fallback.

Phase 1 Multi-OAuth (Plan: das-machen-wir-gleich-foamy-frost.md).
Wird von 4+ Stellen genutzt damit die Lookup-Hierarchie einheitlich
ist und die UNIQUE-Constraint-Strategie (M1: parallel; M2: drop alt)
nicht in jedem Caller dupliziert werden muss.

Lookup-Reihenfolge:
1. employee_id gesetzt → Token mit (employee_id, provider) — der
   Mitarbeiter-spezifische Token, falls vorhanden
2. employee_id gesetzt aber kein Match → Default-Employee-Token
   (Backward-Compat fuer Mitarbeiter die noch nicht onboarded sind)
3. employee_id None → Default-Employee-Token des Tenants
4. Letzter Fallback: Legacy-Token mit employee_id IS NULL
   (vor-Phase-1-Tokens die noch nicht migriert sind — sollte nach
   Phase-0-Backfill leer sein, ist aber das Sicherheitsnetz)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import OAuthToken

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def find_oauth_token(
    tenant_id: UUID,
    provider: str,
    employee_id: UUID | None = None,
) -> OAuthToken | None:
    """Liefert den passenden OAuthToken oder None.

    Hierarchie (siehe Modul-Docstring):
      1. (employee_id, provider) wenn employee_id != None
      2. Default-Employee + provider
      3. Legacy: tenant_id + provider + employee_id IS NULL
    """
    from core.models.employee import Employee, get_default_employee

    async with AsyncSessionLocal() as session:
        # 1) Direkter Match auf (tenant_id, employee_id)
        # tenant_id-Predicate ist sicherheitskritisch: ohne ihn koennte ein
        # (von aussen steuerbarer) employee_id eines FREMDEN Tenants dessen
        # Token zurueckliefern (Cross-Tenant-Leak). employee_id ist global
        # eindeutig, daher schraenkt der Tenant-Filter legitime Lookups nicht ein.
        if employee_id is not None:
            tok = (await session.execute(
                select(OAuthToken).where(
                    OAuthToken.tenant_id == tenant_id,
                    OAuthToken.employee_id == employee_id,
                    OAuthToken.provider == provider,
                )
            )).scalar_one_or_none()
            if tok is not None:
                return tok

        # 2) Default-Employee-Fallback
        default_emp = await get_default_employee(tenant_id)
        if default_emp is not None:
            tok = (await session.execute(
                select(OAuthToken).where(
                    OAuthToken.employee_id == default_emp.id,
                    OAuthToken.provider == provider,
                )
            )).scalar_one_or_none()
            if tok is not None:
                return tok

        # 3) Legacy-Token (employee_id IS NULL) — nur falls Backfill nicht
        # gelaufen ist (sollte nicht vorkommen)
        tok = (await session.execute(
            select(OAuthToken).where(
                OAuthToken.tenant_id == tenant_id,
                OAuthToken.provider == provider,
                OAuthToken.employee_id.is_(None),
            )
        )).scalar_one_or_none()
        return tok
