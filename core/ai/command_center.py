"""Gemini-Kommando-Zentrale für die PWA.

Der Handwerker tippt oder spricht einen Befehl in natürlicher Sprache
("trag Frau Meier morgen 14 Uhr für eine Heizungswartung ein", "bestell
20 Meter Kupferrohr", "Tobias ist die ganze Woche krank"). Gemini
entscheidet per **Function-Calling**, welches Tool auszuführen ist, und
ruft es mit strukturierten Argumenten auf.

Architektur — bewusst sicher:
  * **Read-Tools** (freie Slots suchen, Kunde suchen, Material auflisten,
    offene Rückrufe) laufen SOFORT in der Gemini-Schleife. Ihr Ergebnis
    fließt an Gemini zurück, das daraus den nächsten Schritt ableitet
    (erst Slot suchen → dann Termin buchen).
  * **Write-Tools** (Termin anlegen/stornieren, Rückruf anlegen, Material
    bestellen, Abwesenheit melden) werden NICHT automatisch ausgeführt.
    Gemini schlägt sie vor; ``run_command`` gibt einen ``confirm``-Vorschlag
    zurück. Erst nach ausdrücklicher Bestätigung des Nutzers führt
    ``execute_confirmed`` die Aktion aus (fail-closed).

Jedes Tool ist tenant-gescoped (alle DB-Zugriffe über ``ctx.tid``),
feature-gegated (z.B. Kalender-Tools nur bei aktivem ``kalender``-Feature)
und optional inhaber-gegated (Abwesenheit melden). So sieht Gemini nur die
Tools, die dieser Mitarbeiter in diesem Betrieb wirklich nutzen darf.

Die Aktionen selbst sind dünne Wrapper um genau dieselben Primitive, die
auch die manuellen App-Routen und der Telegram-Bot nutzen
(``kalender.on_webhook(...)``, ``Rueckruf``-Insert, ``create_absence`` …) —
keine doppelte Geschäftslogik.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Wie viele Gemini-Runden maximal (Read-Tool → Ergebnis → nächste Runde),
# bevor wir abbrechen. Verhindert Endlosschleifen bei kaputten Prompts.
MAX_STEPS = 6


@dataclass
class Ctx:
    """Ausführungskontext eines Befehls — hart tenant-isoliert."""
    tenant: Any                 # Tenant-Objekt (.id, .slug, .company_name)
    employee: Any               # Employee-Objekt (.id, .name, .slug, .is_default)
    tid: uuid.UUID              # current_tenant_id(request)
    features: set[str] = field(default_factory=set)

    @property
    def is_inhaber(self) -> bool:
        return bool(getattr(self.employee, "is_default", False))


# ---------------------------------------------------------------------------
# Tool-Spezifikation
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    kind: str                                   # "read" | "write"
    description: str
    parameters: dict                            # JSON-Schema (Vertex-Style, OBJECT/STRING…)
    run: Callable[[Ctx, dict], Awaitable[dict]]  # führt die Aktion aus
    feature: str | None = None                  # benötigtes Feature-Flag (None = immer)
    requires_inhaber: bool = False
    # Baut für Write-Tools die menschenlesbare Bestätigungs-Zeile.
    summarize: Callable[[Ctx, dict], str] | None = None


def _available_tools(ctx: Ctx) -> list[ToolSpec]:
    """Filtert die Registry auf das, was dieser Mitarbeiter nutzen darf."""
    out: list[ToolSpec] = []
    for spec in _REGISTRY:
        if spec.feature and spec.feature not in ctx.features:
            continue
        if spec.requires_inhaber and not ctx.is_inhaber:
            continue
        out.append(spec)
    return out


def _spec_by_name(name: str) -> ToolSpec | None:
    for spec in _REGISTRY:
        if spec.name == name:
            return spec
    return None


# ---------------------------------------------------------------------------
# Gemini-Schleife
# ---------------------------------------------------------------------------

def _system_instruction(ctx: Ctx) -> str:
    heute = dt.date.today()
    wochentag = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                 "Freitag", "Samstag", "Sonntag"][heute.weekday()]
    name = (getattr(ctx.employee, "name", "") or "").split(" ")[0] or "der Nutzer"
    betrieb = getattr(ctx.tenant, "company_name", "") or "dem Betrieb"
    return (
        "Du bist der Assistent in der App eines Handwerksbetriebs. "
        f"Du hilfst {name} von {betrieb}, Aufgaben per Sprach- oder "
        "Tippbefehl zu erledigen, indem du die bereitgestellten Tools "
        "aufrufst.\n\n"
        f"Heute ist {wochentag}, der {heute.strftime('%d.%m.%Y')}. Rechne "
        "relative Angaben wie 'morgen', 'übermorgen', 'nächsten Montag' "
        "in ein konkretes Datum im Format TT.MM.JJJJ um. Uhrzeiten im "
        "Format HH:MM.\n\n"
        "Regeln:\n"
        "- Nutze immer ein Tool, wenn der Befehl eine Aktion verlangt. "
        "Erfinde niemals Ergebnisse.\n"
        "- Fehlt eine Pflichtangabe (z.B. Name, Datum oder Uhrzeit für "
        "einen Termin), dann FRAGE kurz nach, statt zu raten.\n"
        "- Brauchst du für eine Buchung einen freien Slot, suche ihn erst "
        "mit dem passenden Such-Tool.\n"
        "- Willst du Material bestellen, hole dir zuerst die Material-Liste, "
        "um die richtige ID zu finden.\n"
        "- Antworte kurz und auf Deutsch, in der Du-Form, wie ein Kollege."
    )


def _to_plain(value: Any) -> Any:
    """genai-Args (Map/RepeatedComposite) → reine Python-Strukturen."""
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _build_genai_tool(specs: list[ToolSpec]):
    from google.genai import types
    decls = [
        types.FunctionDeclaration(
            name=s.name,
            description=s.description,
            parameters=s.parameters,
        )
        for s in specs
    ]
    return types.Tool(function_declarations=decls)


async def run_command(text: str, ctx: Ctx) -> dict:
    """Führt einen Befehl aus.

    Rückgabe (genau einer der Typen):
      * {"type": "message", "text": str}
            Gemini hat geantwortet/nachgefragt, keine Aktion nötig.
      * {"type": "confirm", "tool": str, "args": dict, "summary": str,
         "frage": str|None}
            Eine schreibende Aktion ist vorbereitet und wartet auf
            Bestätigung. ``summary`` ist die Klartext-Zeile für die UI.
      * {"type": "error", "text": str}
    """
    text = (text or "").strip()
    if not text:
        return {"type": "error", "text": "Leerer Befehl."}

    specs = _available_tools(ctx)
    if not specs:
        return {"type": "message",
                "text": "Für dich sind aktuell keine Assistent-Aktionen freigeschaltet."}

    from google.genai import types
    from core.ai.gemini import _get_genai_client, GENAI_TEXT_LOCATION

    tool = _build_genai_tool(specs)
    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=2048,
        system_instruction=_system_instruction(ctx),
        tools=[tool],
    )
    contents: list = [
        types.Content(role="user", parts=[types.Part.from_text(text=text)])
    ]

    def _sync_call(_contents):
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_contents,
            config=config,
        )

    for _step in range(MAX_STEPS):
        try:
            resp = await asyncio.to_thread(_sync_call, contents)
        except Exception as exc:  # noqa: BLE001
            logger.exception("command_center Gemini-Call fehlgeschlagen: %s", exc)
            return {"type": "error",
                    "text": "Der Assistent ist gerade nicht erreichbar. Bitte gleich nochmal."}

        if not resp.candidates or not resp.candidates[0].content:
            return {"type": "error", "text": "Keine Antwort vom Assistenten."}

        cand_content = resp.candidates[0].content
        parts = cand_content.parts or []
        fc = next((p.function_call for p in parts
                   if getattr(p, "function_call", None)), None)
        say = "".join(p.text for p in parts if getattr(p, "text", None)).strip()

        if fc is None:
            # Kein Tool-Call → Gemini hat geantwortet oder nachgefragt.
            return {"type": "message",
                    "text": say or "Ich habe dich nicht ganz verstanden — kannst du es anders sagen?"}

        spec = _spec_by_name(fc.name)
        args = _to_plain(dict(fc.args or {}))
        if spec is None or spec not in specs:
            # Unbekanntes/ungegatetes Tool — defensiv abbrechen.
            logger.warning("command_center: Gemini rief unzulässiges Tool %r auf", fc.name)
            return {"type": "message",
                    "text": "Das kann ich hier nicht. Frag mich z.B. nach Terminen, Rückrufen oder Material."}

        if spec.kind == "write":
            # NICHT ausführen — Bestätigung einholen.
            summary = spec.summarize(ctx, args) if spec.summarize else f"{spec.name} ausführen"
            return {"type": "confirm", "tool": spec.name, "args": args,
                    "summary": summary, "frage": say or None}

        # Read-Tool: ausführen und Ergebnis an Gemini zurückgeben.
        try:
            result = await spec.run(ctx, args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("command_center read-tool %s crash: %s", spec.name, exc)
            result = {"error": "Tool-Aufruf fehlgeschlagen."}

        contents.append(cand_content)
        contents.append(types.Content(
            role="user",
            parts=[types.Part.from_function_response(
                name=spec.name, response={"result": _to_plain(result)})],
        ))

    return {"type": "message",
            "text": "Das war mir zu komplex — bitte den Befehl in kleinere Schritte teilen."}


async def execute_confirmed(tool_name: str, args: dict, ctx: Ctx) -> dict:
    """Führt eine zuvor vorgeschlagene **Write**-Aktion nach Bestätigung aus.

    Re-validiert Tool-Name, Feature- und Inhaber-Gating (der Client darf
    nichts erzwingen, was Gemini nicht auch durfte).
    """
    spec = _spec_by_name(tool_name)
    if spec is None or spec.kind != "write":
        return {"type": "error", "text": "Unbekannte Aktion."}
    if spec not in _available_tools(ctx):
        return {"type": "error", "text": "Diese Aktion ist für dich nicht freigeschaltet."}
    try:
        result = await spec.run(ctx, _to_plain(args or {}))
    except Exception as exc:  # noqa: BLE001
        logger.exception("command_center write-tool %s crash: %s", tool_name, exc)
        return {"type": "error", "text": "Aktion fehlgeschlagen. Bitte erneut versuchen."}
    return {"type": "done", "tool": tool_name, "result": result}


# ===========================================================================
# Tool-Implementierungen
# ===========================================================================
#
# Jede ``run``-Funktion bekommt (ctx, args) und gibt ein jsonable dict zurück.
# Read-Tools liefern Daten für Gemini; Write-Tools liefern das Ergebnis der
# Aktion für die UI (immer mit "ok": bool).

async def _get_kalender(ctx: Ctx):
    from core.plugin_system import get_plugin_for_tenant
    return await get_plugin_for_tenant(ctx.tenant.slug, "kalender")


# ---- READ -----------------------------------------------------------------

async def _run_freie_slots(ctx: Ctx, args: dict) -> dict:
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"error": "Kalender ist nicht eingerichtet."}
    try:
        days = int(args.get("tage") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 30))
    out = await kalender.on_webhook("find_free_slots", {"days_ahead": days})
    slots = (out or {}).get("slots") or []
    return {"anzahl": len(slots), "slots": slots[:12]}


async def _run_kunde_suchen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from core.models.angebot import Angebot
    from core.models.rechnung import Rechnung
    from sqlalchemy import select

    name = (args.get("name") or "").strip()
    if len(name) < 2:
        return {"error": "Bitte mindestens 2 Zeichen für die Suche."}
    like = f"%{name}%"
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.kunde_name.ilike(like))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(10)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == ctx.tid)
            .where(Angebot.kunde_name.ilike(like))
            .order_by(Angebot.created_at.desc()).limit(10)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung).where(Rechnung.tenant_id == ctx.tid)
            .where(Rechnung.kunde_name.ilike(like))
            .order_by(Rechnung.created_at.desc()).limit(10)
        )).scalars().all()
    return {
        "gespraeche": [{"kunde": x.kunde_name,
                        "briefing": (x.briefing_kurz or "")[:160],
                        "termin": x.termin_datum.isoformat() if x.termin_datum else None}
                       for x in g],
        "angebote_anzahl": len(a),
        "rechnungen_anzahl": len(r),
    }


async def _run_material_liste(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_material import TenantMaterial
    from sqlalchemy import select

    suche = (args.get("suche") or "").strip()
    async with get_session() as s:
        q = (select(TenantMaterial)
             .where(TenantMaterial.tenant_id == ctx.tid)
             .where(TenantMaterial.aktiv.is_(True)))
        if suche:
            q = q.where(TenantMaterial.name.ilike(f"%{suche}%"))
        rows = (await s.execute(q.order_by(TenantMaterial.name).limit(25))).scalars().all()
    return {"material": [{"id": str(m.id), "name": m.name,
                          "einheit": m.einheit,
                          "standard_menge": m.standard_menge} for m in rows]}


async def _run_offene_rueckrufe(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import Rueckruf, RUECKRUF_STATUS_OFFEN
    from sqlalchemy import select

    async with get_session() as s:
        rows = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.tenant_id == ctx.tid)
            .where(Rueckruf.status == RUECKRUF_STATUS_OFFEN)
            .order_by(Rueckruf.created_at.desc()).limit(15)
        )).scalars().all()
    return {"anzahl": len(rows),
            "rueckrufe": [{"kunde": r.kunde_name, "telefon": r.kunde_telefon,
                           "anliegen": (r.anliegen or "")[:120]} for r in rows]}


# ---- WRITE ----------------------------------------------------------------

async def _run_termin_anlegen(ctx: Ctx, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    datum = (args.get("datum") or "").strip()
    uhrzeit = (args.get("uhrzeit") or "").strip()
    if not name or not datum or not uhrzeit:
        return {"ok": False, "error": "Name, Datum und Uhrzeit sind Pflicht."}
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"ok": False, "error": "Kalender nicht eingerichtet."}
    try:
        dauer = int(args.get("dauer_minuten") or 60)
    except (TypeError, ValueError):
        dauer = 60
    payload = {
        "name": name, "datum": datum, "uhrzeit": uhrzeit,
        "dauer_minuten": dauer,
        "anliegen": (args.get("anliegen") or "").strip() or None,
        "adresse": (args.get("adresse") or "").strip() or None,
        "telefon": (args.get("telefon") or "").strip() or None,
        "kunde_email": (args.get("kunde_email") or "").strip() or None,
    }
    res = await kalender.on_webhook("book_appointment", payload)
    if (res or {}).get("error"):
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, "datum": datum, "uhrzeit": uhrzeit,
            "kunde": name, "event_id": (res or {}).get("event_id")}


def _summary_termin(ctx: Ctx, args: dict) -> str:
    teile = [f"Termin für {(args.get('name') or '—').strip()}",
             f"am {(args.get('datum') or '?').strip()}",
             f"um {(args.get('uhrzeit') or '?').strip()} Uhr"]
    if args.get("anliegen"):
        teile.append(f"({str(args['anliegen']).strip()})")
    return " ".join(teile) + " anlegen?"


async def _run_termin_stornieren(ctx: Ctx, args: dict) -> dict:
    """Storniert sicher: findet genau EINEN passenden Termin per Kundenname,
    sonst bricht ab (mirror von app_screens.api_termin_storno)."""
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from sqlalchemy import select

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    heute = dt.date.today()
    async with get_session() as s:
        treffer = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.kunde_name.ilike(f"%{name}%"))
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= heute)
            .order_by(Kundengespraech.termin_datum.asc())
        )).scalars().all()
    if len(treffer) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger anstehender Termin für '{name}' gefunden "
            f"({len(treffer)} Treffer). Bitte im Kalender direkt stornieren.")}

    k = treffer[0]
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"ok": False, "error": "Kalender nicht eingerichtet."}
    tmin = (k.termin_datum - dt.timedelta(days=1)).isoformat()
    tmax = (k.termin_datum + dt.timedelta(days=1)).isoformat()
    found = await kalender.on_webhook("find_events", {
        "kunde_name": k.kunde_name, "time_min": tmin, "time_max": tmax})
    termine = (found or {}).get("termine") or []
    if len(termine) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger Kalender-Termin gefunden ({len(termine)} Treffer). "
            "Bitte im Kalender direkt stornieren.")}
    match = termine[0]
    event_id = match.get("event_id")
    cancel_payload: dict = {"event_id": event_id}
    emp_uuid = None
    if match.get("employee_id"):
        try:
            emp_uuid = uuid.UUID(match["employee_id"])
            cancel_payload["employee_id"] = emp_uuid
        except (ValueError, TypeError):
            pass
    res = await kalender.on_webhook("cancel_appointment", cancel_payload)
    if not (res or {}).get("erfolg"):
        return {"ok": False, "error": (res or {}).get("nachricht") or "Storno fehlgeschlagen."}
    mail_sent = False
    try:
        from core.integrations.mail_pipeline import send_storno_confirmation_for_event
        mail_sent = await send_storno_confirmation_for_event(
            tenant_id=ctx.tenant.id,
            company_name=ctx.tenant.company_name or "",
            event_id=event_id, employee_id=emp_uuid, cancelled_count=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_center storno mail crash: %s", exc)
    return {"ok": True, "kunde": k.kunde_name,
            "datum": k.termin_datum.isoformat(), "mail_sent": mail_sent}


def _summary_storno(ctx: Ctx, args: dict) -> str:
    return f"Anstehenden Termin von {(args.get('kunde_name') or '—').strip()} stornieren?"


async def _run_rueckruf_anlegen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import Rueckruf, RUECKRUF_STATUS_OFFEN

    kunde_name = (args.get("kunde_name") or "").strip()
    kunde_telefon = (args.get("kunde_telefon") or "").strip()
    if not kunde_name or not kunde_telefon:
        return {"ok": False, "error": "Name und Telefon sind Pflicht."}
    anliegen = (args.get("anliegen") or "").strip() or \
        f"Manuell angelegt von {getattr(ctx.employee, 'name', None) or 'Mitarbeiter'}"
    async with get_session() as s:
        r = Rueckruf(
            tenant_id=ctx.tid, kunde_name=kunde_name, kunde_telefon=kunde_telefon,
            kunde_email=(args.get("kunde_email") or "").strip() or None,
            anliegen=anliegen, status=RUECKRUF_STATUS_OFFEN,
            assigned_employee_id=getattr(ctx.employee, "id", None))
        s.add(r)
        await s.commit()
        await s.refresh(r)
    return {"ok": True, "id": str(r.id), "kunde": kunde_name}


def _summary_rueckruf(ctx: Ctx, args: dict) -> str:
    return (f"Rückruf für {(args.get('kunde_name') or '—').strip()} "
            f"({(args.get('kunde_telefon') or '?').strip()}) anlegen?")


async def _run_material_bestellen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_material import (
        TenantMaterial, MaterialBestellung, BESTELL_ART_LINK)
    from sqlalchemy import select

    mid_raw = (args.get("material_id") or "").strip()
    name = (args.get("name") or "").strip()
    try:
        menge = int(args.get("menge") or 0)
    except (TypeError, ValueError):
        menge = 0
    async with get_session() as s:
        m = None
        if mid_raw:
            try:
                m = (await s.execute(
                    select(TenantMaterial)
                    .where(TenantMaterial.id == uuid.UUID(mid_raw))
                    .where(TenantMaterial.tenant_id == ctx.tid))).scalar_one_or_none()
            except (ValueError, TypeError):
                m = None
        if m is None and name:
            rows = (await s.execute(
                select(TenantMaterial)
                .where(TenantMaterial.tenant_id == ctx.tid)
                .where(TenantMaterial.aktiv.is_(True))
                .where(TenantMaterial.name.ilike(f"%{name}%")).limit(2)
            )).scalars().all()
            if len(rows) == 1:
                m = rows[0]
            elif len(rows) > 1:
                return {"ok": False, "error": f"'{name}' ist nicht eindeutig — bitte genauer."}
        if m is None:
            return {"ok": False, "error": "Material nicht gefunden."}
        if not m.aktiv:
            return {"ok": False, "error": "Material ist deaktiviert."}
        if menge < 1:
            menge = m.standard_menge or 1
        s.add(MaterialBestellung(
            tenant_id=ctx.tid, material_id=m.id,
            employee_id=getattr(ctx.employee, "id", None),
            material_name=m.name, bestell_link=m.bestell_link,
            menge=menge, einheit=m.einheit, bestell_art=BESTELL_ART_LINK))
        await s.commit()
        return {"ok": True, "material": m.name, "menge": menge,
                "einheit": m.einheit, "bestell_link": m.bestell_link}


def _summary_material(ctx: Ctx, args: dict) -> str:
    bez = (args.get("name") or args.get("material_id") or "Material").strip()
    menge = args.get("menge")
    return (f"{menge}× " if menge else "") + f"{bez} bestellen?"


async def _run_abwesenheit(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.employee import Employee
    from core.models.employee_absence import create_absence
    from sqlalchemy import select, or_

    typ = (args.get("typ") or "krank").strip()
    if typ not in ("krank", "urlaub", "sonstiges"):
        return {"ok": False, "error": "Typ muss krank, urlaub oder sonstiges sein."}
    mitarbeiter = (args.get("mitarbeiter") or "").strip()
    if not mitarbeiter:
        return {"ok": False, "error": "Bitte den Mitarbeiter nennen."}
    try:
        start = dt.date.fromisoformat((args.get("start") or "").strip()) \
            if args.get("start") else dt.date.today()
    except ValueError:
        return {"ok": False, "error": "Start-Datum ungültig (YYYY-MM-DD)."}
    ende = None
    if (args.get("ende") or "").strip():
        try:
            ende = dt.date.fromisoformat(args["ende"].strip())
        except ValueError:
            return {"ok": False, "error": "End-Datum ungültig (YYYY-MM-DD)."}
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == ctx.tid)
            .where(or_(Employee.slug == mitarbeiter,
                       Employee.name.ilike(f"%{mitarbeiter}%")))
            .limit(2))).scalars().all()
    if not emp:
        return {"ok": False, "error": f"Mitarbeiter '{mitarbeiter}' nicht gefunden."}
    if len(emp) > 1:
        return {"ok": False, "error": f"'{mitarbeiter}' ist nicht eindeutig — bitte Vor- und Nachname."}
    try:
        ab = await create_absence(
            employee_id=emp[0].id, start_date=start, end_date=ende,
            absence_type=typ, notes=(args.get("notes") or "").strip() or None,
            created_by_employee_id=getattr(ctx.employee, "id", None))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "mitarbeiter": emp[0].name, "typ": typ,
            "start": start.isoformat(), "ende": ende.isoformat() if ende else None,
            "absence_id": str(ab.id)}


def _summary_abwesenheit(ctx: Ctx, args: dict) -> str:
    typ = (args.get("typ") or "krank").strip()
    label = {"krank": "krankmelden", "urlaub": "in Urlaub setzen",
             "sonstiges": "abwesend melden"}.get(typ, "abwesend melden")
    mit = (args.get("mitarbeiter") or "—").strip()
    zeit = ""
    if args.get("start"):
        zeit = f" ab {args['start']}"
        if args.get("ende"):
            zeit = f" von {args['start']} bis {args['ende']}"
    return f"{mit}{zeit} {label}?"


# ---- READ (Erweiterung) ---------------------------------------------------

async def _run_anstehende_termine(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from sqlalchemy import select

    try:
        tage = int(args.get("tage") or 14)
    except (TypeError, ValueError):
        tage = 14
    tage = max(1, min(tage, 60))
    heute = dt.date.today()
    bis = heute + dt.timedelta(days=tage)
    async with get_session() as s:
        rows = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= heute)
            .where(Kundengespraech.termin_datum < bis)
            .order_by(Kundengespraech.termin_datum.asc()).limit(30)
        )).scalars().all()
    return {"anzahl": len(rows), "termine": [
        {"kunde": r.kunde_name,
         "termin": r.termin_datum.isoformat() if r.termin_datum else None,
         "info": (r.briefing_kurz or "")[:120]} for r in rows]}


async def _run_team_status(ctx: Ctx, args: dict) -> dict:
    from core.models.employee import get_employees_for_tenant
    from core.models.employee_absence import get_active_absences, get_upcoming_absences

    heute = dt.date.today()
    employees = await get_employees_for_tenant(ctx.tid, active_only=False)
    active = await get_active_absences(ctx.tid, heute)
    upcoming = await get_upcoming_absences(ctx.tid, days_ahead=7)
    absent_today = {emp.id: ab for emp, ab in active}
    upc: dict = {}
    for emp, ab in upcoming:
        upc.setdefault(emp.id, []).append(ab)
    out = []
    for e in employees:
        ab = absent_today.get(e.id)
        out.append({
            "name": e.name,
            "inhaber": bool(getattr(e, "is_default", False)),
            "aktiv": bool(getattr(e, "is_active", True)),
            "abwesend_heute": (ab.absence_type if ab else None),
            "kommende_abwesenheit": [a.start_date.isoformat() for a in upc.get(e.id, [])][:1],
        })
    return {"team": out}


async def _run_offene_anfragen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.email_conversation import EmailConversation, STATE_CLOSED
    from sqlalchemy import select

    async with get_session() as s:
        rows = (await s.execute(
            select(EmailConversation)
            .where(EmailConversation.tenant_id == ctx.tid)
            .where(EmailConversation.state != STATE_CLOSED)
            .order_by(EmailConversation.updated_at.desc()).limit(12)
        )).scalars().all()
    return {"anzahl": len(rows), "anfragen": [
        {"kunde": r.kunde_name or "—", "betreff": (r.last_subject or "")[:120]} for r in rows]}


async def _run_wissen_suchen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_knowledge import TenantKnowledge, KATEGORIE_LABELS
    from sqlalchemy import select

    frage = (args.get("frage") or "").strip()

    async def _fetch(filtered: bool):
        async with get_session() as s:
            q = select(TenantKnowledge).where(TenantKnowledge.tenant_id == ctx.tid)
            if filtered and len(frage) >= 2:
                q = q.where(TenantKnowledge.text.ilike(f"%{frage}%"))
            return (await s.execute(
                q.order_by(TenantKnowledge.kategorie).limit(30))).scalars().all()

    rows = await _fetch(filtered=True)
    if frage and not rows:  # Filter ohne Treffer → alles liefern (Tabelle ist klein)
        rows = await _fetch(filtered=False)
    return {"eintraege": [
        {"kategorie": KATEGORIE_LABELS.get(r.kategorie, r.kategorie), "text": r.text}
        for r in rows]}


# ---- WRITE (Erweiterung) --------------------------------------------------

async def _run_wissen_merken(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_knowledge import TenantKnowledge, KATEGORIE_LABELS

    kategorie = (args.get("kategorie") or "").strip()
    text = (args.get("text") or "").strip()
    if kategorie not in KATEGORIE_LABELS:
        kategorie = "faq"
    if not (3 <= len(text) <= 2000):
        return {"ok": False, "error": "Text muss 3–2000 Zeichen haben."}
    async with get_session() as s:
        s.add(TenantKnowledge(tenant_id=ctx.tid, kategorie=kategorie, text=text))
        await s.commit()
    return {"ok": True, "kategorie": KATEGORIE_LABELS.get(kategorie, kategorie), "text": text}


def _summary_wissen(ctx: Ctx, args: dict) -> str:
    t = (args.get("text") or "").strip()
    return f"Zur Wissensdatenbank merken: „{t[:90]}{'…' if len(t) > 90 else ''}\"?"


async def _run_rueckruf_erledigt(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import (
        Rueckruf, RUECKRUF_STATUS_OFFEN, RUECKRUF_STATUS_ERLEDIGT)
    from sqlalchemy import select

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    async with get_session() as s:
        rows = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.tenant_id == ctx.tid)
            .where(Rueckruf.status == RUECKRUF_STATUS_OFFEN)
            .where(Rueckruf.kunde_name.ilike(f"%{name}%")).limit(2)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein offener Rückruf für '{name}'."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere offene Rückrufe für '{name}' — bitte genauer."}
        r = rows[0]
        r.status = RUECKRUF_STATUS_ERLEDIGT
        r.erledigt_at = dt.datetime.now(dt.timezone.utc)
        r.erledigt_by_employee_id = getattr(ctx.employee, "id", None)
        await s.commit()
        kunde = r.kunde_name
    return {"ok": True, "kunde": kunde}


def _summary_rueckruf_erledigt(ctx: Ctx, args: dict) -> str:
    return f"Rückruf von {(args.get('kunde_name') or '—').strip()} als erledigt abhaken?"


async def _run_mitarbeiter_zurueck(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.employee import Employee
    from core.models.employee_absence import close_absence
    from sqlalchemy import select, or_

    mitarbeiter = (args.get("mitarbeiter") or "").strip()
    if not mitarbeiter:
        return {"ok": False, "error": "Bitte den Mitarbeiter nennen."}
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == ctx.tid)
            .where(or_(Employee.slug == mitarbeiter,
                       Employee.name.ilike(f"%{mitarbeiter}%"))).limit(2)
        )).scalars().all()
    if not emp:
        return {"ok": False, "error": f"Mitarbeiter '{mitarbeiter}' nicht gefunden."}
    if len(emp) > 1:
        return {"ok": False, "error": f"'{mitarbeiter}' ist nicht eindeutig — bitte Vor- und Nachname."}
    closed = await close_absence(emp[0].id, dt.date.today())
    return {"ok": True, "mitarbeiter": emp[0].name, "war_abwesend": closed is not None}


def _summary_mitarbeiter_zurueck(ctx: Ctx, args: dict) -> str:
    return f"{(args.get('mitarbeiter') or '—').strip()} als wieder verfügbar melden?"


async def _run_auftrag_status(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.angebot import (
        Angebot, AUFTRAG_LIFECYCLE, AUFTRAG_LIFECYCLE_LABELS,
        ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_WORK_IN_PROGRESS,
        ANGEBOT_STATUS_WORK_DONE, ANGEBOT_STATUS_ABGEBROCHEN)
    from sqlalchemy import select

    settable = {ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_WORK_IN_PROGRESS,
                ANGEBOT_STATUS_WORK_DONE, ANGEBOT_STATUS_ABGEBROCHEN}
    name = (args.get("kunde_name") or "").strip()
    status = (args.get("status") or "").strip()
    if status not in settable:
        return {"ok": False, "error": (
            "Status muss accepted, arbeit_laeuft, arbeit_fertig oder "
            "abgebrochen sein. (Rechnung-raus läuft separat.)")}
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    relevante = set(AUFTRAG_LIFECYCLE) | {ANGEBOT_STATUS_ABGEBROCHEN}
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == ctx.tid)
            .where(Angebot.kunde_name.ilike(f"%{name}%"))
            .where(Angebot.status.in_(relevante)).limit(2)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein laufender Auftrag für '{name}' gefunden."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere Aufträge für '{name}' — bitte genauer."}
        a = rows[0]
        a.status = status
        if status == ANGEBOT_STATUS_ACCEPTED and not a.accepted_at:
            a.accepted_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        kunde = a.kunde_name
    return {"ok": True, "kunde": kunde, "status": status,
            "status_label": AUFTRAG_LIFECYCLE_LABELS.get(status, status)}


def _summary_auftrag_status(ctx: Ctx, args: dict) -> str:
    label = {"accepted": "angenommen", "arbeit_laeuft": "Arbeit läuft",
             "arbeit_fertig": "fertig", "abgebrochen": "abgebrochen"}.get(
        (args.get("status") or "").strip(), args.get("status") or "?")
    return f"Auftrag von {(args.get('kunde_name') or '—').strip()} auf „{label}\" setzen?"


async def _run_material_anlegen(ctx: Ctx, args: dict) -> dict:
    import re
    from core.database.connection import get_session
    from core.models.tenant_material import TenantMaterial
    from sqlalchemy import select

    name = (args.get("name") or "").strip()
    link = (args.get("bestell_link") or "").strip()
    if not name or not link:
        return {"ok": False, "error": "Name und Bestell-Link sind Pflicht."}
    try:
        std = int(args.get("standard_menge") or 1)
    except (TypeError, ValueError):
        std = 1
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "material"
    async with get_session() as s:
        slug = base
        i = 2
        while (await s.execute(
            select(TenantMaterial.id)
            .where(TenantMaterial.tenant_id == ctx.tid)
            .where(TenantMaterial.slug == slug))).scalar_one_or_none() is not None:
            slug = f"{base}-{i}"
            i += 1
            if i > 30:
                return {"ok": False, "error": "Konnte keinen eindeutigen Slug bilden."}
        m = TenantMaterial(
            tenant_id=ctx.tid, slug=slug, name=name, bestell_link=link,
            lieferant_name=(args.get("lieferant") or "").strip() or None,
            einheit=(args.get("einheit") or "Stück").strip() or "Stück",
            standard_menge=max(1, std),
            notes=(args.get("notes") or "").strip() or None, aktiv=True)
        s.add(m)
        await s.commit()
    return {"ok": True, "name": name, "slug": slug}


def _summary_material_anlegen(ctx: Ctx, args: dict) -> str:
    return f"Material „{(args.get('name') or '—').strip()}\" im Katalog anlegen?"


# ---- READ (Telegram-Paritaet) ---------------------------------------------

async def _run_archiv_suchen(ctx: Ctx, args: dict) -> dict:
    """Findet die Drive-Archiv-Ordner eines Kunden (spiegelt /archiv ohne
    Upload-Wizard: reine Suche + Link)."""
    from core.integrations.google_drive import list_tenant_kunde_drives

    name = (args.get("kunde_name") or "").strip()
    folders = await list_tenant_kunde_drives(ctx.tid, limit=500)
    if name:
        folders = [f for f in folders if name.lower() in (f.kunde_name or "").lower()]
    return {"anzahl": len(folders), "ordner": [
        {"kunde": f.kunde_name, "link": f.drive_folder_url,
         "dateien": f.upload_count,
         "letzter_upload": f.last_upload_at.isoformat() if f.last_upload_at else None}
        for f in folders[:15]]}


async def _run_rechnungen_pruefen(ctx: Ctx, args: dict) -> dict:
    """Synchronisiert den Bezahl-Status offener Rechnungen mit Lexware
    (spiegelt /rechnung_pruefen). Kein Versand, nur Abgleich + Markierung."""
    from core.integrations.rechnung_payment_monitor import (
        check_pending_invoices_for_tenant)

    summary = await check_pending_invoices_for_tenant(ctx.tid)
    return {"geprueft": summary.get("checked", 0),
            "neu_als_bezahlt_markiert": summary.get("paid", 0),
            "unveraendert": summary.get("no_change", 0),
            "fehler": summary.get("errors", 0)}


async def _run_formulare_status(ctx: Ctx, args: dict) -> dict:
    """Status der Kunden-Anfrage-Formulare der letzten 30 Tage
    (spiegelt /formulare-Überschrift: offen/ausgefüllt/abgelaufen)."""
    from core.integrations.anfrage_status import count_status_for_tenant

    counts = await count_status_for_tenant(ctx.tid)
    return {"letzte_30_tage": counts}


# ---- WRITE (Telegram-Paritaet) --------------------------------------------

async def _run_wissen_loeschen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_knowledge import TenantKnowledge
    from sqlalchemy import select

    such = (args.get("suchtext") or "").strip()
    if len(such) < 3:
        return {"ok": False, "error": "Bitte einen Suchtext (min. 3 Zeichen) nennen."}
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == ctx.tid)
            .where(TenantKnowledge.text.ilike(f"%{such}%")).limit(3)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein Wissens-Eintrag mit '{such}' gefunden."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere Einträge passen auf '{such}' — bitte genauer."}
        entry = rows[0]
        geloescht = entry.text
        await s.delete(entry)
        await s.commit()
    return {"ok": True, "geloescht": geloescht[:140]}


def _summary_wissen_loeschen(ctx: Ctx, args: dict) -> str:
    return f"Wissens-Eintrag mit „{(args.get('suchtext') or '—').strip()}\" löschen?"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_S = "STRING"
_I = "INTEGER"

_REGISTRY: list[ToolSpec] = [
    # ---- READ ----
    ToolSpec(
        name="freie_termine_finden", kind="read", feature="kalender",
        description="Sucht freie Termin-Slots in den nächsten Tagen im Kalender. "
                    "Vor einer Buchung aufrufen, um einen passenden Slot zu finden.",
        parameters={"type": "OBJECT", "properties": {
            "tage": {"type": _I, "description": "Wie viele Tage vorausschauen (Standard 7, max 30)."}}},
        run=_run_freie_slots),
    ToolSpec(
        name="kunde_suchen", kind="read",
        description="Sucht einen Kunden anhand des Namens und liefert seine "
                    "letzten Gespräche/Termine sowie die Anzahl Angebote/Rechnungen.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Kundenname oder Teil davon."}},
            "required": ["name"]},
        run=_run_kunde_suchen),
    ToolSpec(
        name="material_liste", kind="read",
        description="Listet das hinterlegte Material des Betriebs (mit IDs). "
                    "Vor einer Bestellung aufrufen, um die richtige material_id zu finden.",
        parameters={"type": "OBJECT", "properties": {
            "suche": {"type": _S, "description": "Optionaler Namensfilter."}}},
        run=_run_material_liste),
    ToolSpec(
        name="offene_rueckrufe", kind="read",
        description="Zeigt die aktuell offenen Rückrufe.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_offene_rueckrufe),

    # ---- WRITE ----
    ToolSpec(
        name="termin_anlegen", kind="write", feature="kalender",
        description="Legt einen Termin im Kalender an. Nur mit konkretem Datum "
                    "(TT.MM.JJJJ) und Uhrzeit (HH:MM) aufrufen.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Name des Kunden."},
            "datum": {"type": _S, "description": "Datum TT.MM.JJJJ."},
            "uhrzeit": {"type": _S, "description": "Uhrzeit HH:MM."},
            "dauer_minuten": {"type": _I, "description": "Dauer in Minuten (Standard 60)."},
            "anliegen": {"type": _S, "description": "Worum geht es (z.B. Heizungswartung)."},
            "adresse": {"type": _S}, "telefon": {"type": _S},
            "kunde_email": {"type": _S}},
            "required": ["name", "datum", "uhrzeit"]},
        run=_run_termin_anlegen, summarize=_summary_termin),
    ToolSpec(
        name="termin_stornieren", kind="write", feature="kalender",
        description="Storniert den anstehenden Termin eines Kunden (nur wenn "
                    "eindeutig genau ein Termin gefunden wird).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."}},
            "required": ["kunde_name"]},
        run=_run_termin_stornieren, summarize=_summary_storno),
    ToolSpec(
        name="rueckruf_anlegen", kind="write",
        description="Legt einen Rückruf an, damit der Kunde zurückgerufen wird.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S}, "kunde_telefon": {"type": _S},
            "anliegen": {"type": _S, "description": "Worum geht es."},
            "kunde_email": {"type": _S}},
            "required": ["kunde_name", "kunde_telefon"]},
        run=_run_rueckruf_anlegen, summarize=_summary_rueckruf),
    ToolSpec(
        name="material_bestellen", kind="write",
        description="Bestellt Material aus dem Katalog (per material_id aus "
                    "material_liste, oder eindeutigem Namen).",
        parameters={"type": "OBJECT", "properties": {
            "material_id": {"type": _S, "description": "ID aus material_liste."},
            "name": {"type": _S, "description": "Alternativ: eindeutiger Material-Name."},
            "menge": {"type": _I, "description": "Bestellmenge (Standard = Standardmenge)."}}},
        run=_run_material_bestellen, summarize=_summary_material),
    ToolSpec(
        name="abwesenheit_melden", kind="write", requires_inhaber=True,
        description="Meldet einen Mitarbeiter krank, in Urlaub oder sonst "
                    "abwesend. Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "mitarbeiter": {"type": _S, "description": "Name oder Kürzel des Mitarbeiters."},
            "typ": {"type": _S, "description": "krank | urlaub | sonstiges.",
                    "enum": ["krank", "urlaub", "sonstiges"]},
            "start": {"type": _S, "description": "Start YYYY-MM-DD (Standard heute)."},
            "ende": {"type": _S, "description": "Ende YYYY-MM-DD (leer = offen)."},
            "notes": {"type": _S}},
            "required": ["mitarbeiter", "typ"]},
        run=_run_abwesenheit, summarize=_summary_abwesenheit),

    # ---- READ (Erweiterung) ----
    ToolSpec(
        name="anstehende_termine", kind="read",
        description="Zeigt die anstehenden Termine der nächsten Tage "
                    "(aus den erfassten Kundengesprächen).",
        parameters={"type": "OBJECT", "properties": {
            "tage": {"type": _I, "description": "Vorausschau in Tagen (Standard 14, max 60)."}}},
        run=_run_anstehende_termine),
    ToolSpec(
        name="team_status", kind="read",
        description="Zeigt das Team: wer heute abwesend (krank/Urlaub) ist und "
                    "welche Abwesenheiten anstehen.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_team_status),
    ToolSpec(
        name="offene_anfragen", kind="read", feature="mail_intake",
        description="Zeigt die offenen Kundenanfragen (E-Mail-Eingang), die noch "
                    "nicht abgeschlossen sind.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_offene_anfragen),
    ToolSpec(
        name="wissen_suchen", kind="read",
        description="Durchsucht die Wissensdatenbank des Betriebs (Preise, "
                    "Leistungen, Anfahrt, Öffnungszeiten, Besonderheiten …) und "
                    "liefert passende Einträge, um eine Frage zu beantworten.",
        parameters={"type": "OBJECT", "properties": {
            "frage": {"type": _S, "description": "Suchbegriff/Stichwort (optional — leer = alles)."}}},
        run=_run_wissen_suchen),

    # ---- WRITE (Erweiterung) ----
    ToolSpec(
        name="wissen_merken", kind="write",
        description="Speichert eine Information dauerhaft in der "
                    "Wissensdatenbank (z.B. Preis, Regel, Besonderheit).",
        parameters={"type": "OBJECT", "properties": {
            "text": {"type": _S, "description": "Der zu merkende Text."},
            "kategorie": {"type": _S,
                          "description": "leistungen | materialien | preise | anfahrt | "
                                         "oeffnungszeiten | notfall | besonderheiten | faq.",
                          "enum": ["leistungen", "materialien", "preise", "anfahrt",
                                   "oeffnungszeiten", "notfall", "besonderheiten", "faq"]}},
            "required": ["text"]},
        run=_run_wissen_merken, summarize=_summary_wissen),
    ToolSpec(
        name="rueckruf_erledigt", kind="write",
        description="Hakt den offenen Rückruf eines Kunden als erledigt ab "
                    "(nur bei genau einem eindeutigen offenen Rückruf).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."}},
            "required": ["kunde_name"]},
        run=_run_rueckruf_erledigt, summarize=_summary_rueckruf_erledigt),
    ToolSpec(
        name="mitarbeiter_zurueck", kind="write", requires_inhaber=True,
        description="Meldet einen Mitarbeiter wieder verfügbar (beendet seine "
                    "laufende Abwesenheit). Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "mitarbeiter": {"type": _S, "description": "Name oder Kürzel des Mitarbeiters."}},
            "required": ["mitarbeiter"]},
        run=_run_mitarbeiter_zurueck, summarize=_summary_mitarbeiter_zurueck),
    ToolSpec(
        name="auftrag_status", kind="write", requires_inhaber=True, feature="lexware",
        description="Setzt den Status eines laufenden Auftrags (per Kundenname). "
                    "Mögliche Stufen: accepted (angenommen), arbeit_laeuft, "
                    "arbeit_fertig, abgebrochen. Der Rechnungsversand läuft separat.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "status": {"type": _S, "description": "Neuer Status.",
                       "enum": ["accepted", "arbeit_laeuft", "arbeit_fertig", "abgebrochen"]}},
            "required": ["kunde_name", "status"]},
        run=_run_auftrag_status, summarize=_summary_auftrag_status),
    ToolSpec(
        name="material_anlegen", kind="write", requires_inhaber=True,
        description="Legt einen neuen Material-Eintrag im Bestell-Katalog an "
                    "(braucht Name und Bestell-Link). Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Material-Name."},
            "bestell_link": {"type": _S, "description": "URL zum Bestellen."},
            "lieferant": {"type": _S}, "einheit": {"type": _S, "description": "z.B. Stück, Meter, kg."},
            "standard_menge": {"type": _I}, "notes": {"type": _S}},
            "required": ["name", "bestell_link"]},
        run=_run_material_anlegen, summarize=_summary_material_anlegen),

    # ---- READ (Telegram-Paritaet) ----
    ToolSpec(
        name="archiv_suchen", kind="read", feature="drive_archiv",
        description="Findet den Drive-Archiv-Ordner eines Kunden (mit Link und "
                    "Anzahl Dateien). Ohne Namen: zuletzt genutzte Ordner.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Kundenname (optional)."}}},
        run=_run_archiv_suchen),
    ToolSpec(
        name="rechnungen_pruefen", kind="read", feature="lexware",
        description="Gleicht den Bezahl-Status offener Rechnungen mit Lexware ab "
                    "und markiert bezahlte. Verschickt nichts.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_rechnungen_pruefen),
    ToolSpec(
        name="formulare_status", kind="read", feature="anfrage_formular",
        description="Zeigt den Status der Kunden-Anfrage-Formulare der letzten "
                    "30 Tage (offen / ausgefüllt / abgelaufen).",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_formulare_status),

    # ---- WRITE (Telegram-Paritaet) ----
    ToolSpec(
        name="wissen_loeschen", kind="write",
        description="Löscht einen Eintrag aus der Wissensdatenbank (per "
                    "Suchtext, nur bei eindeutigem Treffer).",
        parameters={"type": "OBJECT", "properties": {
            "suchtext": {"type": _S, "description": "Teil des zu löschenden Eintrags."}},
            "required": ["suchtext"]},
        run=_run_wissen_loeschen, summarize=_summary_wissen_loeschen),
]
