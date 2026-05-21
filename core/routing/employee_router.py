"""Employee-Router: waehlt automatisch den passenden Mitarbeiter
fuer eine eingehende Anfrage (Mail, Voice, Anfrage-Formular).

Phase 5 der Multi-Mitarbeiter-Erweiterung
(`das-machen-wir-gleich-foamy-frost.md`).

Score-Modell (einfach + deterministisch):
- Skill-Match aus Anliegen-Text → Substring-Hit gegen KEYWORD_TO_SKILL
  (kein Gemini in Phase 5 — Latenz/Cost; spaeter Phase 6).
- Distanz-Score (ORS) — nur wenn aktiv konfiguriert + Adresse vorhanden +
  vorgefilterte Kandidatenmenge ≤ 3 (sonst Free-Tier-Risiko).
- Verfuegbarkeit (Phase-6-Erweiterung) — heute nicht implementiert,
  Slot-Filter im Kalender-Plugin uebernimmt das ohnehin nochmal.
- Tie-Break: deterministisch nach slug ASC.

Conversation-Sticky-Routing:
Wenn `existing_conversation.assigned_employee_id` gesetzt ist,
liefern wir genau den zurueck — Folge-Mails wechseln nicht den
Bearbeiter. Skill-Router greift nur beim ersten Kontakt.

Defensive Defaults:
- Bei 0 aktiven Employees (sollte nicht vorkommen wegen Phase-0-
  Backfill) → None.
- Bei 1 aktivem Employee → der mit reason='only-active'.
- Bei kein-Match in Skills → Default-Employee mit reason='fallback-default'.

Niemals raise, immer eine RoutingDecision zurueck — Caller muss nicht
defensiv programmieren.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from config.settings import settings
from core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


# Keyword → Skill-Mapping (lowercase Substring-Match).
# Bei mehreren Hits werden alle entsprechenden Skills addiert.
# Erweiterbar ohne DB-Migration.
KEYWORD_TO_SKILL: dict[str, str] = {
    # Heizung
    "heizung": "heizung", "kessel": "heizung", "thermostat": "heizung",
    "brenner": "heizung", "warmwasser": "heizung", "fussboden": "heizung",
    "fußbodenheizung": "heizung",
    # Sanitaer
    "sanitaer": "sanitaer", "sanitär": "sanitaer",
    "wasserhahn": "sanitaer", "abfluss": "sanitaer", "tropft": "sanitaer",
    "wasser": "sanitaer", "wc": "sanitaer", "toilette": "sanitaer",
    "spuelung": "sanitaer", "spülung": "sanitaer", "rohr": "sanitaer",
    # Elektrik
    "elektrik": "elektrik", "elektro": "elektrik",
    "steckdose": "elektrik", "sicherung": "elektrik", "strom": "elektrik",
    "schalter": "elektrik", "lampe": "elektrik", "leitung": "elektrik",
    # Dach
    "dach": "dach", "daches": "dach", "ziegel": "dach", "rinne": "dach",
    "regenrinne": "dach", "dachfenster": "dach",
    # Tischler / Holz
    "tischler": "tischler", "schreiner": "tischler",
    "moebel": "tischler", "möbel": "tischler", "holz": "tischler",
    "kueche": "tischler", "küche": "tischler",
    # Maler
    "maler": "maler", "malern": "maler", "streichen": "maler",
    "tapete": "maler", "tapezieren": "maler", "fassade": "maler",
}


@dataclass
class RoutingDecision:
    """Ergebnis von choose_employee."""
    employee_id: uuid.UUID
    employee_name: str
    employee_slug: str
    reason: str  # 'sticky-conversation' | 'only-active' | 'skill-match' |
                 # 'distance' | 'fallback-default'
    score: float
    debug: dict[str, Any]


def extract_skills_from_text(text: str) -> list[str]:
    """Findet Skills im freien Text (Substring-Match, lowercase).

    Public Helper — auch von core.integrations.absence_redistribution
    benutzt um aus Kalender-Event-Subjects Skills zu extrahieren.
    """
    if not text:
        return []
    t = text.lower()
    hits: list[str] = []
    for keyword, skill in KEYWORD_TO_SKILL.items():
        if keyword in t and skill not in hits:
            hits.append(skill)
    return hits


# Backward-Compat: alter privater Name bleibt als Alias bestehen.
_extract_skills_from_text = extract_skills_from_text


async def choose_employee(
    tenant_id: uuid.UUID,
    *,
    anliegen_text: str = "",
    kunde_adresse: str | None = None,
    existing_conversation=None,
    target_datetime: dt.datetime | None = None,
    exclude_employee_ids: list[uuid.UUID] | None = None,
) -> RoutingDecision | None:
    """Waehlt den passendsten Mitarbeiter fuer eine eingehende Anfrage.

    Args:
        tenant_id: Tenant des Kunden.
        anliegen_text: freier Text der Anfrage (Mail-Body, Anfrage-Antworten,
            transkribiertes Telefonat). Wird gegen KEYWORD_TO_SKILL gematcht.
        kunde_adresse: Strasse/PLZ/Ort des Kunden — wenn vorhanden + ORS
            konfiguriert, fliesst Distanz in den Score ein.
        existing_conversation: optional EmailConversation. Wenn deren
            assigned_employee_id schon gesetzt ist, wird der Router NICHT
            neu entscheiden (Sticky-Routing).
        target_datetime: optional Termin-Zeitpunkt. Wenn gesetzt, werden
            nur Mitarbeiter beruecksichtigt die zu dieser Zeit arbeiten
            (is_employee_working_at — checkt Absence, Arbeitstag,
            Arbeitszeit). Wenn nach Filter 0 Kandidaten: Default-Employee
            als Fallback mit reason='no-coverage' (Signal an Cron/Bot
            zur Eskalation an den Inhaber).
        exclude_employee_ids: Liste der Mitarbeiter die ausgeschlossen
            werden sollen — typisch bei Umverteilung der Krank-Termine
            (der Erkrankte selbst soll nicht wieder gewaehlt werden).

    Returns:
        RoutingDecision oder None wenn der Tenant keine aktiven Employees
        hat (sollte nach Phase-0-Backfill nicht vorkommen).
    """
    from core.models.employee import Employee, get_default_employee

    # 1) Sticky: bestehende Conversation behaelt ihren Employee — aber nur
    # wenn der Employee noch aktiv ist. Sonst Re-Routing (sticky-recovered),
    # sonst landen Folge-Mails bei jemandem der nicht mehr da ist.
    if existing_conversation is not None:
        sticky_id = getattr(existing_conversation, "assigned_employee_id", None)
        if sticky_id is not None:
            async with AsyncSessionLocal() as s:
                emp = (await s.execute(
                    select(Employee).where(Employee.id == sticky_id)
                )).scalar_one_or_none()
            if emp is not None and emp.is_active:
                return RoutingDecision(
                    employee_id=emp.id,
                    employee_name=emp.name,
                    employee_slug=emp.slug,
                    reason="sticky-conversation",
                    score=1.0,
                    debug={"conversation_id": str(getattr(existing_conversation, "id", ""))},
                )
            # Employee deaktiviert oder geloescht: Sticky bricht, normales
            # Routing greift weiter unten. Wir loggen das fuer Audit.
            if emp is not None:
                logger.info(
                    f"sticky-conversation auf inaktiven Employee {emp.slug} - re-route"
                )
            else:
                logger.info(
                    f"sticky-conversation auf geloeschten Employee {sticky_id} - re-route"
                )

    # 2) Aktive Employees laden
    async with AsyncSessionLocal() as s:
        emps = (await s.execute(
            select(Employee).where(
                Employee.tenant_id == tenant_id,
                Employee.is_active.is_(True),
            ).order_by(Employee.is_default.desc(), Employee.slug.asc())
        )).scalars().all()
        # detached
        for e in emps:
            s.expunge(e)

    if not emps:
        logger.warning(f"choose_employee: tenant {tenant_id} hat keine aktiven Employees")
        return None

    # 2a) Verfuegbarkeits-Filter (Phase 6): nur Mitarbeiter die zur
    # `target_datetime` arbeiten (kein Absence, richtiger Arbeitstag,
    # innerhalb Arbeitszeit) + nicht im exclude-Set.
    excluded = set(exclude_employee_ids or [])
    candidates_after_filter = emps
    if target_datetime is not None or excluded:
        from core.models.employee_absence import is_employee_working_at
        filtered = []
        for e in emps:
            if e.id in excluded:
                continue
            if target_datetime is not None:
                if not await is_employee_working_at(e.id, target_datetime):
                    continue
            filtered.append(e)
        candidates_after_filter = filtered

    # Wenn der Verfuegbarkeits-Filter ALLES rauswirft: Eskalation
    # signalisieren via reason='no-coverage'. Wir liefern den
    # Default-Employee mit dem Signal — der Cron / Bot kann dann
    # eine "kein-Mitarbeiter-verfuegbar"-Nachricht an den Inhaber
    # schicken.
    if not candidates_after_filter:
        default_emp = await get_default_employee(tenant_id)
        if default_emp is None:
            return None
        return RoutingDecision(
            employee_id=default_emp.id,
            employee_name=default_emp.name,
            employee_slug=default_emp.slug,
            reason="no-coverage", score=0.0,
            debug={
                "target_datetime": target_datetime.isoformat() if target_datetime else None,
                "excluded_count": len(excluded),
                "all_emps": [e.slug for e in emps],
            },
        )
    # Ab hier arbeiten wir nur noch auf der gefilterten Liste.
    emps = candidates_after_filter

    # 3) Trivial-Fall: nur 1 Employee
    if len(emps) == 1:
        e = emps[0]
        return RoutingDecision(
            employee_id=e.id, employee_name=e.name, employee_slug=e.slug,
            reason="only-active", score=1.0, debug={"candidate_count": 1},
        )

    # 3a) Smart-Routing: Gemini waehlt anhand der Skills den fachlich
    #     passendsten Kandidaten. Nur wenn aktiviert, Anliegen-Text da und
    #     >1 Kandidat (sonst gibt's nichts zu entscheiden). Failsafe:
    #     bei None/Timeout/Fehler faellt es unten auf die deterministische
    #     Stichwort-Logik zurueck. Timeout schuetzt den Hot-Path (Mail/
    #     Voice/Anfrage laufen alle hier durch).
    if settings.smart_routing_enabled and (anliegen_text or "").strip():
        picked_slug = None
        try:
            from core.ai.gemini import rank_employee_for_request
            cand = [
                {
                    "slug": e.slug, "name": e.name,
                    "skills": list(e.skills or []),
                    "job_title": getattr(e, "job_title", None),
                }
                for e in emps
            ]
            picked_slug = await asyncio.wait_for(
                rank_employee_for_request(
                    anliegen_text, cand, tenant_id=str(tenant_id),
                ),
                timeout=8.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"choose_employee: Gemini-Routing fiel aus: {e}")
            picked_slug = None
        if picked_slug:
            winner = next((e for e in emps if e.slug == picked_slug), None)
            if winner is not None:
                logger.info(
                    f"choose_employee: Gemini -> {winner.slug} "
                    f"(tenant={tenant_id})"
                )
                return RoutingDecision(
                    employee_id=winner.id, employee_name=winner.name,
                    employee_slug=winner.slug, reason="gemini-skill-match",
                    score=1.0, debug={"gemini": True},
                )

    # 4) Skill-Score (deterministischer Fallback wenn Smart-Routing aus
    #    ist oder Gemini keinen klaren Treffer/Antwort lieferte)
    needed_skills = _extract_skills_from_text(anliegen_text)
    skill_scores: dict[uuid.UUID, int] = {}
    for e in emps:
        emp_skills = set((e.skills or []))
        if needed_skills:
            hits = sum(1 for sk in needed_skills if sk in emp_skills)
            skill_scores[e.id] = hits
        else:
            # Kein Anliegen-Text → alle Skill-neutral
            skill_scores[e.id] = 0

    max_skill = max(skill_scores.values())
    if max_skill > 0:
        # Vorfiltern auf Top-Skill-Matcher (max 3 fuer ORS-Quota-Schutz)
        candidates = [e for e in emps if skill_scores[e.id] == max_skill][:3]
        winner = candidates[0]
        winner_reason = "skill-match"
        debug = {
            "needed_skills": needed_skills,
            "candidate_count": len(candidates),
            "skill_winners": [c.slug for c in candidates],
        }

        # 5) Distanz-Tiebreak (nur wenn ORS + Adresse + > 1 Kandidat)
        if kunde_adresse and len(candidates) > 1:
            distance_winner = await _pick_by_distance(candidates, kunde_adresse)
            if distance_winner is not None:
                winner = distance_winner
                winner_reason = "distance"
                debug["distance_evaluated"] = True

        return RoutingDecision(
            employee_id=winner.id,
            employee_name=winner.name,
            employee_slug=winner.slug,
            reason=winner_reason, score=float(max_skill), debug=debug,
        )

    # 6) Fallback: kein Skill-Match → Default-Employee
    default_emp = await get_default_employee(tenant_id)
    if default_emp is None:
        # Sollte nicht vorkommen (Phase-0-Backfill garantiert Default).
        # Notfall: ersten aktiven nehmen.
        e = emps[0]
        return RoutingDecision(
            employee_id=e.id, employee_name=e.name, employee_slug=e.slug,
            reason="fallback-default", score=0.0,
            debug={"warning": "no-default-employee"},
        )
    return RoutingDecision(
        employee_id=default_emp.id,
        employee_name=default_emp.name,
        employee_slug=default_emp.slug,
        reason="fallback-default", score=0.0,
        debug={"needed_skills": needed_skills, "no_skill_hit": True},
    )


async def _pick_by_distance(candidates, kunde_adresse: str):
    """Sucht den Kandidaten mit kuerzester Anfahrt zum Kunden.

    Best-effort: bei Geo-Provider-Fehler oder fehlenden Geo-Daten faellt
    zurueck auf None (Caller behaelt den ersten Skill-Kandidaten).
    Nutzt geo-Wrapper: Google Maps preferred, ORS Fallback.
    """
    try:
        from core.integrations.geo import (
            geocode_address, travel_time_minutes, is_configured, GeoPoint,
        )
        if not is_configured():
            return None
        kunde_geo = await geocode_address(kunde_adresse)
        if kunde_geo is None:
            return None

        best = None
        best_minutes = None
        for c in candidates:
            if c.heimat_lat is None or c.heimat_lon is None:
                continue
            origin = GeoPoint(float(c.heimat_lat), float(c.heimat_lon))
            mins = await travel_time_minutes(origin, kunde_geo)
            if mins is None:
                continue
            if best_minutes is None or mins < best_minutes:
                best_minutes = mins
                best = c
        return best
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_pick_by_distance failed: {e}")
        return None
