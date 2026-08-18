"""Lesen und Schreiben der Mitarbeiter-Rechte.

Duenne Schicht ueber ``employee_permissions``, gebaut wie
``core/features/automation_check.py``: derselbe 60s-In-Process-Cache,
dieselbe Invalidierung nach dem Schreiben, derselbe defensive Lesepfad.
Nur der Cache-Key ist ein anderer — hier ``employee_id`` statt
``tenant_id``, weil Rechte pro Person gelten.

Effektive Rechte = ``ROLLEN_PRESETS[employee.role]`` ± Overrides.
Der Inhaber (``is_default``) bekommt immer alles; das ist ein
Kurzschluss VOR jedem DB-Zugriff, damit sich niemand aus seinem eigenen
Betrieb aussperren kann.

Cache-Hinweis: Die App laeuft als EIN uvicorn-Prozess ohne ``--workers``
(siehe core/security/app_auth.py), darum ist die Invalidierung
vollstaendig wirksam. Bei einem spaeteren Multi-Worker-Setup waere das
ein echter Bug — dann braucht es eine gemeinsame Invalidierung.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.features.permissions import (
    ALLE_RECHTE,
    RECHTE,
    ROLLE_DEFAULT,
    ist_delegierbar,
    ist_gueltige_rolle,
    rechte_fuer_rolle,
)

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 60


@dataclass
class _CacheEntry:
    rechte: frozenset[str]
    expires_at: float


_cache: dict[uuid.UUID, _CacheEntry] = {}


def invalidate_permission_cache(employee_id: uuid.UUID | None = None) -> None:
    """Leert den Cache (nach dem Speichern von Rolle oder Override).

    None -> kompletter Cache (z.B. im Test).
    """
    if employee_id is None:
        _cache.clear()
    else:
        _cache.pop(employee_id, None)


# =====================================================================
# Lesen
# =====================================================================

async def rechte_fuer_employee(employee) -> frozenset[str]:
    """Effektive Rechte eines Mitarbeiters.

    Erwartet ein Employee-Objekt (nicht nur die ID), weil ``is_default``
    und ``role`` daran haengen und der Aufrufer sie ohnehin schon
    geladen hat — die App-Session liefert den Employee mit.
    """
    # Inhaber darf immer alles. Vor dem Cache und vor der DB, damit ein
    # Datenbankproblem den Betriebsinhaber nie aussperrt.
    if getattr(employee, "is_default", False):
        return ALLE_RECHTE

    employee_id = employee.id
    entry = _cache.get(employee_id)
    now = time.monotonic()
    if entry is not None and entry.expires_at > now:
        return entry.rechte

    basis = set(rechte_fuer_rolle(getattr(employee, "role", None)))

    from core.models.employee_permission import EmployeePermission

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    EmployeePermission.permission_key,
                    EmployeePermission.allowed,
                ).where(EmployeePermission.employee_id == employee_id)
            )).all()
    except Exception as exc:  # noqa: BLE001
        # Zwei Faelle, beide harmlos:
        #   - Tabelle fehlt noch (Code deployed, Migration noch nicht) —
        #     dann hat niemand etwas abweichend gesetzt und das Preset ist
        #     exakt richtig.
        #   - Transienter Fehler — dann ist der letzte bekannte Stand
        #     naeher an der Wahrheit als das nackte Preset.
        logger.warning(
            "employee_permissions nicht lesbar (employee=%s): %s — nutze %s",
            employee_id, exc,
            "letzten bekannten Stand" if entry else "Rollen-Vorlage",
        )
        return entry.rechte if entry else frozenset(basis)

    for key, allowed in rows:
        # Verwaister Key aus einer alten Version: ignorieren statt crashen.
        if key not in RECHTE:
            continue
        # Ein nicht delegierbares Recht kann per Override nie dazukommen.
        # Doppelt gesichert: der Schreibpfad lehnt es schon ab, aber eine
        # von Hand gesetzte Zeile darf hier nicht durchrutschen.
        if allowed and not ist_delegierbar(key):
            logger.warning(
                "employee_permissions: %r ist nicht delegierbar, "
                "Override fuer employee=%s wird ignoriert", key, employee_id,
            )
            continue
        if allowed:
            basis.add(key)
        else:
            basis.discard(key)

    rechte = frozenset(basis)
    _cache[employee_id] = _CacheEntry(
        rechte=rechte, expires_at=now + _CACHE_TTL_SECONDS
    )
    return rechte


async def hat_recht(employee, key: str) -> bool:
    """Einzelnes Recht pruefen.

    Unbekannter Key -> False (fail-closed): lieber blockiert ein
    Tippfehler im Code eine Funktion, als dass er sie oeffnet.
    """
    if key not in RECHTE:
        logger.warning("hat_recht: unbekannter Key %r", key)
        return False
    return key in await rechte_fuer_employee(employee)


# =====================================================================
# Schreiben
# =====================================================================

async def set_rolle(employee_id: uuid.UUID, rolle: str) -> bool:
    """Rolle setzen. False bei ungueltiger Rolle oder unbekanntem Mitarbeiter."""
    if not ist_gueltige_rolle(rolle):
        logger.warning("set_rolle: ungueltige Rolle %r", rolle)
        return False

    from core.models.employee import Employee

    async with AsyncSessionLocal() as session:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            return False
        emp.role = rolle
        await session.commit()
    invalidate_permission_cache(employee_id)
    return True


async def set_override(
    employee_id: uuid.UUID,
    tenant_id: uuid.UUID,
    key: str,
    allowed: bool | None,
) -> bool:
    """Einzelnes Recht abweichend setzen.

    ``allowed=None`` loescht den Override -> die Rollen-Vorlage gilt wieder.
    False, wenn der Key unbekannt oder nicht delegierbar ist.
    """
    if key not in RECHTE:
        logger.warning("set_override: unbekannter Key %r", key)
        return False
    # Rechteausweitung verhindern: "Rechte vergeben" und "Einstellungen"
    # duerfen nie an einen Nicht-Inhaber wandern, sonst schaltet der sich
    # anschliessend selbst alles frei.
    if allowed and not ist_delegierbar(key):
        logger.warning("set_override: %r ist nicht delegierbar", key)
        return False

    from core.models.employee_permission import EmployeePermission

    async with AsyncSessionLocal() as session:
        if allowed is None:
            await session.execute(
                delete(EmployeePermission)
                .where(EmployeePermission.employee_id == employee_id)
                .where(EmployeePermission.permission_key == key)
            )
        else:
            row = (await session.execute(
                select(EmployeePermission)
                .where(EmployeePermission.employee_id == employee_id)
                .where(EmployeePermission.permission_key == key)
            )).scalar_one_or_none()
            if row is None:
                session.add(EmployeePermission(
                    tenant_id=tenant_id, employee_id=employee_id,
                    permission_key=key, allowed=allowed,
                ))
            else:
                row.allowed = allowed
        await session.commit()
    invalidate_permission_cache(employee_id)
    return True


async def overrides_fuer_employee(
    employee_id: uuid.UUID,
) -> dict[str, bool]:
    """Nur die gesetzten Abweichungen — fuer die Rechte-Oberflaeche,
    damit sie "kommt aus der Rolle" von "hast du einzeln gesetzt"
    unterscheiden kann."""
    from core.models.employee_permission import EmployeePermission

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    EmployeePermission.permission_key,
                    EmployeePermission.allowed,
                ).where(EmployeePermission.employee_id == employee_id)
            )).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("overrides nicht lesbar (employee=%s): %s", employee_id, exc)
        return {}
    return {k: v for k, v in rows if k in RECHTE}


def rolle_oder_default(employee) -> str:
    """Rolle eines Employee-Objekts, mit Rueckfall auf die restriktivste."""
    if getattr(employee, "is_default", False):
        from core.features.permissions import ROLLE_INHABER
        return ROLLE_INHABER
    rolle = getattr(employee, "role", None)
    return rolle if ist_gueltige_rolle(rolle or "") else ROLLE_DEFAULT
