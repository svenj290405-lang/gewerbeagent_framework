"""
Handler des Kalender-Plugins.

Refaktorierte Version des alten webhook_server.py:
- Multi-Tenant: Konfiguration + OAuth-Token pro Tenant aus DB
- Plugin-Architektur: erbt von BasePlugin, dispatch ueber on_webhook()
- Saubere Error-Responses statt generischer Exception-Strings
- Smart-Slot-Filter: wenn Tenant Werkstatt-Adresse + Kunden-Adresse +
  OPENROUTESERVICE_API_KEY verfuegbar ist, wird die Fahrtzeit zwischen
  Vor-Termin/Nach-Termin und neuem Kunden eingerechnet — Slots die
  nicht passen fallen raus, der Rest wird nach Gesamt-Fahrtzeit sortiert.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.geo import (
    GeoPoint,
    geocode_address as ors_geocode_address,
    is_configured as ors_is_configured,
    travel_time_minutes as ors_travel_time_minutes,
)
from core.models import Tenant
from core.plugin_system import BasePlugin
from core.utils.phone import normalize_phone
from plugins.kalender.adapters import get_calendar_adapter
from plugins.kalender.manifest import MANIFEST
from plugins.telegram_notify.handler import TelegramNotifier

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Booking Concurrency + Idempotency
# ----------------------------------------------------------------------
# In-process Lock-Map: pro (tenant_id, slot-start-minute) genau ein Booking
# gleichzeitig. Verhindert Doppelbuchung wenn z.B. Mail-Pipeline und
# Voice-Pipeline parallel auf den gleichen Slot zielen.
_SLOT_LOCKS: dict[tuple[UUID, datetime], asyncio.Lock] = {}
_SLOT_LOCKS_GUARD = asyncio.Lock()


# Idempotency-Cache: bei wiederholtem _book_appointment-Aufruf mit
# gleichem Key (z.B. Mail-Message-ID) gibt es das vorherige Resultat
# zurueck statt eine zweite Buchung zu machen.
# TTL: 24h (genug fuer Container-Restart-Recovery).
_BOOKING_IDEMPOTENCY: dict[tuple[UUID, str], tuple[datetime, dict]] = {}
_IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_IDEMPOTENCY_MAX_ENTRIES = 5000  # safety cap


def _get_slot_lock(tenant_id: UUID, slot_start: datetime) -> asyncio.Lock:
    """Liefert (oder erstellt) den Lock fuer einen konkreten Slot.

    Wir runden auf Minuten-Granularitaet damit Slot-Variationen wie
    14:00 und 14:00:30 den gleichen Lock bekommen.
    """
    minute_key = slot_start.replace(second=0, microsecond=0)
    key = (tenant_id, minute_key)
    lock = _SLOT_LOCKS.get(key)
    if lock is None:
        # Garde gegen Race beim Lock-Erstellen selbst (sehr klein, aber sauber)
        lock = asyncio.Lock()
        _SLOT_LOCKS[key] = lock
    return lock


def _cache_booking_idempotency(
    tenant_id: UUID, key: str, response: dict,
) -> None:
    """Speichert das Booking-Resultat fuer 24h damit Wiederholungs-Aufrufe
    mit dem gleichen Key kein zweites Event anlegen."""
    try:
        # Garbage Collect uralte Eintraege wenn Cap erreicht
        if len(_BOOKING_IDEMPOTENCY) > _IDEMPOTENCY_MAX_ENTRIES:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=_IDEMPOTENCY_TTL_SECONDS)
            stale = [k for k, (ts, _) in _BOOKING_IDEMPOTENCY.items() if ts < cutoff]
            for k in stale:
                _BOOKING_IDEMPOTENCY.pop(k, None)
        _BOOKING_IDEMPOTENCY[(tenant_id, key)] = (
            datetime.now(timezone.utc), response,
        )
    except Exception as e:
        logger.debug(f"idempotency cache failed (egal): {e}")


def _check_booking_idempotency(tenant_id: UUID, key: str) -> dict | None:
    """Liefert das gecachte Resultat fuer (tenant, key) wenn vorhanden +
    nicht aelter als 24h. Sonst None."""
    entry = _BOOKING_IDEMPOTENCY.get((tenant_id, key))
    if entry is None:
        return None
    ts, response = entry
    if (datetime.now(timezone.utc) - ts).total_seconds() > _IDEMPOTENCY_TTL_SECONDS:
        _BOOKING_IDEMPOTENCY.pop((tenant_id, key), None)
        return None
    return response


class Plugin(BasePlugin):
    manifest = MANIFEST

    # ---- Dispatch ----

    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # Kalender wird nur intern aufgerufen (voice_init-Handler +
        # microsoft_inbox via mail_pipeline.cancel_kunde_termine), nicht
        # von externen Webhooks.
        # Header-Verifikation entfaellt; Param fuer BasePlugin-Konformitaet.
        _ = headers  # noqa: F841
        if endpoint == "check_availability":
            return await self._check_availability(payload)
        elif endpoint == "book_appointment":
            return await self._book_appointment(payload)
        elif endpoint == "find_free_slots":
            return await self._find_free_slots(payload)
        elif endpoint == "cancel_appointment":
            return await self._cancel_appointment(payload)
        elif endpoint == "find_events":
            return await self._find_events(payload)
        return {"error": f"Unbekannter Endpunkt: {endpoint}"}

    # ---- Endpoints ----

    async def _check_availability(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Prueft ob ein Termin frei ist."""
        try:
            datum = payload.get("datum", "")
            uhrzeit = payload.get("uhrzeit", "")
            dauer = payload.get(
                "dauer_minuten", self.config["termin_dauer_minuten"]
            )

            start = self._parse_datum_uhrzeit(datum, uhrzeit)
            ende = start + timedelta(minutes=dauer)

            # Arbeitstage pruefen
            if start.weekday() not in self.config["arbeitstage"]:
                return {
                    "verfuegbar": False,
                    "nachricht": (
                        f"An diesem Tag hat {self.config['betrieb_name']} geschlossen. "
                        "Bitte einen Arbeitstag waehlen."
                    ),
                }

            # Arbeitszeiten pruefen
            start_h, start_m = self._parse_zeit(self.config["arbeitszeiten_start"])
            ende_h, ende_m = self._parse_zeit(self.config["arbeitszeiten_ende"])

            if (
                start.hour < start_h
                or (start.hour == start_h and start.minute < start_m)
                or ende.hour > ende_h
                or (ende.hour == ende_h and ende.minute > ende_m)
            ):
                return {
                    "verfuegbar": False,
                    "nachricht": (
                        f"Termine nur zwischen {self.config['arbeitszeiten_start']} "
                        f"und {self.config['arbeitszeiten_ende']} moeglich."
                    ),
                }

            # Provider-agnostisch: Adapter holen (Google oder Outlook)
            employee_id = payload.get("employee_id")
            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=employee_id,
                fallback_calendar_id=self.config["calendar_id"],
            )
            busy = await adapter.is_slot_busy(start, ende)

            if not busy:
                return {
                    "verfuegbar": True,
                    "nachricht": (
                        f"Der Termin am {start.strftime('%d.%m.%Y')} um "
                        f"{start.strftime('%H:%M')} Uhr ist frei."
                    ),
                }
            else:
                return {
                    "verfuegbar": False,
                    "nachricht": (
                        f"Der Termin am {start.strftime('%d.%m.%Y')} um "
                        f"{start.strftime('%H:%M')} Uhr ist leider schon belegt."
                    ),
                }

        except Exception as e:
            return {
                "verfuegbar": False,
                "nachricht": f"Fehler bei der Pruefung: {str(e)}",
            }

    async def _book_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Traegt einen Termin in Google Calendar ein.

        TOCTOU-Schutz: in-process Lock pro (tenant_id, slot_start_minute)
        serialisiert parallele Buchungs-Anfragen auf den gleichen Slot.
        Verhindert Doppelbuchung wenn z.B. Mail- und Voice-Pipeline
        gleichzeitig den selben Termin buchen wollen.

        Idempotency: bei wiederholtem Aufruf mit gleichem idempotency_key
        wird das vorherige Resultat zurueckgegeben (Container-Restart-
        Schutz - kommt vom Caller via payload['idempotency_key']).
        """
        try:
            name = payload.get("name", "")
            anliegen = payload.get("anliegen", "")
            adresse = payload.get("adresse") or "Adresse nicht angegeben"
            telefon = payload.get("telefon")
            # Optionaler Link zum Kunden-Drive-Ordner (Anfrage-Formular-
            # Daten + Fotos). Mail-Pipeline reicht das durch sobald der
            # Kunde das Formular ausgefuellt hat; landet in der Event-
            # Beschreibung damit der Handwerker direkt zu den Unterlagen
            # springen kann.
            drive_url = (payload.get("drive_url") or "").strip()
            # kunde_email kommt aus Mail-Pipeline immer, aus Voice optional
            # (Phase 1: nur wenn Q die Mail aktiv erfragt hat). Lowercase-
            # normalisiert damit Storno-Suche per Mail spaeter exakt matched.
            kunde_email_raw = payload.get("kunde_email") or ""
            kunde_email = kunde_email_raw.strip().lower() or None
            # Telefon-Normalisierung: Voice gibt "+49 30 1234" rein,
            # Storno-Suche kommt vielleicht als "030 1234" — beide muessen
            # auf den selben Key mappen damit die Suche findet.
            kunde_telefon_normalized = normalize_phone(telefon) or None
            datum = payload.get("datum", "")
            uhrzeit = payload.get("uhrzeit", "")
            dauer = payload.get(
                "dauer_minuten", self.config["termin_dauer_minuten"]
            )
            idempotency_key = payload.get("idempotency_key")

            start = self._parse_datum_uhrzeit(datum, uhrzeit)
            ende = start + timedelta(minutes=dauer)

            employee_id = payload.get("employee_id")

            # Idempotency-Check: gibt es bereits ein Booking-Result fuer
            # diesen Key? (z.B. Mail-Message-ID — verhindert Doppel-Buchung
            # bei Mail-Re-Polling nach Container-Crash.)
            if idempotency_key:
                cached = _check_booking_idempotency(
                    self.tenant_id, idempotency_key,
                )
                if cached is not None:
                    logger.info(
                        f"book_appointment idempotency-hit: "
                        f"key={idempotency_key} -> reuse cached result"
                    )
                    return cached

            # TOCTOU-Lock: pro Tenant + Slot-Start nur ein Booking
            # gleichzeitig
            lock = _get_slot_lock(self.tenant_id, start)
            async with lock:
                # Re-Check innerhalb des Locks: vielleicht hat ein anderer
                # Request den Slot gerade gebucht
                adapter = await get_calendar_adapter(
                    self.tenant_id, employee_id=employee_id,
                    fallback_calendar_id=self.config["calendar_id"],
                )
                if await adapter.is_slot_busy(start, ende):
                    return {
                        "erfolg": False,
                        "nachricht": (
                            f"Slot {start.strftime('%d.%m.%Y %H:%M')} ist "
                            f"belegt. Anderer Termin wurde gerade gebucht."
                        ),
                        "konflikt": True,
                    }

                betrieb_name = self.config["betrieb_name"]
                telefon_text = f"\nTelefon: {telefon}" if telefon else ""
                # Kunden-Mail sichtbar in die Beschreibung (nicht nur als
                # extendedProperty fuer die Storno-Suche) — der Handwerker
                # sieht so direkt, mit welcher Adresse korrespondiert wird.
                email_text = f"\nE-Mail: {kunde_email}" if kunde_email else ""

                summary = f"[{betrieb_name}] {anliegen} - {name}"
                drive_line = (
                    f"\nUnterlagen (Drive): {drive_url}" if drive_url else ""
                )
                description = (
                    f"Betrieb: {betrieb_name}\n"
                    f"Kunde: {name}\n"
                    f"Anliegen: {anliegen}\n"
                    f"Adresse: {adresse}"
                    f"{telefon_text}"
                    f"{email_text}"
                    f"{drive_line}\n\n"
                    f"Eingetragen via KI-Agent Q (Gewerbeagent Framework)"
                )
                if idempotency_key:
                    description += f"\nGA-Ref: {idempotency_key}"
                result = await adapter.create_event(
                    summary=summary,
                    description=description,
                    location=adresse,
                    start=start,
                    end=ende,
                    timezone=self.config["zeitzone"],
                    kunde_telefon_normalized=kunde_telefon_normalized,
                    kunde_email=kunde_email,
                    idempotency_key=idempotency_key,
                )

            # Telegram-Push an den fuer den Termin zustaendigen Mitarbeiter
            # (silent fail, blockiert nie den Termin). Wenn employee_id im
            # Payload fehlt (Legacy-Caller), faellt send_for_employee mit
            # employee_id=None automatisch auf den Default-Employee zurueck.
            telefon_line = f"\n<b>Telefon:</b> {telefon}" if telefon else ""
            adresse_line = f"\n<b>Adresse:</b> {adresse}" if adresse and adresse != "Adresse nicht angegeben" else ""
            push_text = (
                "📅 <b>Neuer Termin</b>\n"
                f"<b>Kunde:</b> {name}\n"
                f"<b>Anliegen:</b> {anliegen}\n"
                f"<b>Wann:</b> {start.strftime('%a %d.%m.%Y, %H:%M')} Uhr"
                f"{adresse_line}"
                f"{telefon_line}"
            )
            await TelegramNotifier.send_for_employee(
                self.tenant_id, push_text, employee_id=employee_id,
            )

            booking_response = {
                "erfolg": True,
                "nachricht": (
                    f"Termin erfolgreich eingetragen! "
                    f"{name}, {anliegen}, am {start.strftime('%d.%m.%Y')} "
                    f"um {start.strftime('%H:%M')} Uhr."
                ),
                "event_id": result.get("id"),
                "link": result.get("html_link") or result.get("htmlLink"),
            }

            if idempotency_key:
                _cache_booking_idempotency(
                    self.tenant_id, idempotency_key, booking_response,
                )
            return booking_response

        except Exception as e:
            return {
                "erfolg": False,
                "nachricht": f"Fehler beim Eintragen: {str(e)}",
            }

    async def _find_free_slots(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Sucht freie Slots im Kalender. Nimmt einen Wunschtermin als Anker
        und gibt Alternativen zurueck:
        - bis zu 3 Slots am selben Tag (vor und nach Wunsch-Uhrzeit)
        - bis zu 2 Slots am naechsten Werktag
        - bis zu 1 Slot am uebernaechsten Werktag

        Maximale Antwortliste: 6 Slots.

        Smart-Filter (optional, wenn payload['kunde_adresse'] + Werkstatt
        + ORS-API-Key verfuegbar): nach FreeBusy-Filter werden Slots
        gegen Travel-Time-Constraints geprueft. Slots wo Anfahrt vom
        Vor-Termin oder Weiterfahrt zum Nach-Termin nicht passen, fallen
        raus. Sortierung nach kuerzester Gesamt-Fahrtzeit.
        """
        try:
            wunsch_datum = payload.get("datum", "")
            wunsch_uhrzeit = payload.get("uhrzeit", "")
            dauer = payload.get("dauer_minuten", self.config["termin_dauer_minuten"])
            kunde_adresse = (payload.get("kunde_adresse") or "").strip()
            # Phase-3: optional welcher Mitarbeiter — entscheidet welche
            # Heimat-Adresse fuer Routing-Origin verwendet wird.
            employee_id = payload.get("employee_id")

            wunsch = self._parse_datum_uhrzeit(wunsch_datum, wunsch_uhrzeit)
            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=employee_id,
                fallback_calendar_id=self.config["calendar_id"],
            )

            slots: list[dict] = []

            # Tag 0: selber Tag
            slots.extend(await self._suche_slots_am_tag(
                adapter, wunsch.date(), wunsch_uhrzeit_anker=wunsch.time(), max_count=3, dauer=dauer
            ))
            # Tag 1: naechster Werktag
            naechster_tag = self._naechster_werktag(wunsch.date())
            slots.extend(await self._suche_slots_am_tag(
                adapter, naechster_tag, wunsch_uhrzeit_anker=None, max_count=2, dauer=dauer
            ))
            # Tag 2: uebernaechster Werktag
            uebernaechster_tag = self._naechster_werktag(naechster_tag)
            slots.extend(await self._suche_slots_am_tag(
                adapter, uebernaechster_tag, wunsch_uhrzeit_anker=None, max_count=1, dauer=dauer
            ))

            # Smart-Filter (best-effort, schluckt eigene Fehler)
            smart_meta = {"applied": False, "reason": None, "removed": 0}
            try:
                slots, smart_meta = await self._smart_filter_slots(
                    slots, kunde_adresse, dauer, adapter,
                    employee_id=employee_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Smart-Filter crashed, using raw slots: {exc}")
                smart_meta = {"applied": False, "reason": "filter-error", "removed": 0}

            return {
                "erfolg": True,
                "slots": slots,
                "anzahl": len(slots),
                "smart_routing": smart_meta,
            }

        except Exception as e:
            return {"erfolg": False, "nachricht": f"Fehler bei Slot-Suche: {str(e)}"}

    # ------------------------------------------------------------------
    # SMART-SLOT-FILTER (Travel-Time aware)
    # ------------------------------------------------------------------

    async def _resolve_routing_origin(
        self, tenant, employee_id,
    ) -> tuple[float | None, float | None, int, str]:
        """Ermittelt Werkstatt-Origin (lat, lon, puffer, source) fuer Routing.

        Hierarchie:
        1. employee_id gesetzt + Mitarbeiter mit eigener Heimat → diese
           ('employee')
        2. Default-Employee mit eigener Heimat → diese ('default-employee')
        3. tenant.heimat_* (Mirror / Legacy) → diese ('tenant')
        4. nichts gesetzt → (None, None, 15, 'none')
        """
        from core.models.employee import Employee, get_default_employee

        async def _from_employee(emp_id):
            async with AsyncSessionLocal() as s:
                return (await s.execute(
                    select(Employee).where(Employee.id == emp_id)
                )).scalar_one_or_none()

        if employee_id is not None:
            emp = await _from_employee(employee_id)
            if emp and emp.heimat_lat is not None and emp.heimat_lon is not None:
                return (
                    float(emp.heimat_lat), float(emp.heimat_lon),
                    int(emp.fahrtzeit_puffer_min or 15), "employee",
                )

        # Default-Employee als Fallback (auch wenn employee_id None war
        # oder Mitarbeiter selbst keine Heimat hat)
        default_emp = await get_default_employee(tenant.id)
        if (default_emp and default_emp.heimat_lat is not None
                and default_emp.heimat_lon is not None):
            return (
                float(default_emp.heimat_lat), float(default_emp.heimat_lon),
                int(default_emp.fahrtzeit_puffer_min or 15),
                "default-employee",
            )

        # Letzter Fallback: Tenant-Mirror (Backward-Compat fuer Tenants
        # die /werkstatt vor Phase-3 durchlaufen sind)
        if tenant.heimat_lat is not None and tenant.heimat_lon is not None:
            return (
                float(tenant.heimat_lat), float(tenant.heimat_lon),
                int(tenant.fahrtzeit_puffer_min or 15), "tenant",
            )

        return (None, None, 15, "none")

    async def _smart_filter_slots(
        self,
        slots: list[dict],
        kunde_adresse: str,
        dauer_min: int,
        adapter,
        employee_id=None,
    ) -> tuple[list[dict], dict]:
        """Filtert Slots gegen Travel-Time-Constraints.

        Returns: (gefilterte Liste, Meta-Dict mit Diagnose-Infos).
        Bei jedem Skip-Grund (kein Key, kein Tenant-Geo, keine Adresse)
        liefern wir die Original-Liste unveraendert zurueck.

        Phase-3-Multi-Mitarbeiter (`das-machen-wir-gleich-foamy-frost.md`):
        Werkstatt-Geo wird employee-aware aufgeloest. Bei employee_id
        gesetzt + Mitarbeiter mit eigener Heimat: dessen lat/lon.
        Sonst: Default-Employee-Heimat oder Tenant-Mirror als Fallback.
        Damit kann jeder Mitarbeiter morgens von seiner Heimat-Adresse
        losfahren statt von der Werkstatt.
        """
        meta = {"applied": False, "reason": None, "removed": 0}

        if not slots:
            meta["reason"] = "no-slots"
            return slots, meta
        if not kunde_adresse:
            meta["reason"] = "no-customer-address"
            return slots, meta
        if not ors_is_configured():
            meta["reason"] = "ors-not-configured"
            return slots, meta

        # Routing-Origin (Werkstatt) ermitteln: Employee > Default-Employee
        # > Tenant-Mirror. Fallback-Hierarchie damit Phase-3-Migration
        # graceful funktioniert auch fuer Tenants die /werkstatt vor der
        # Migration durchlaufen sind.
        from core.models.employee import Employee, get_default_employee
        async with AsyncSessionLocal() as session:
            tenant = (await session.execute(
                select(Tenant).where(Tenant.id == self.tenant_id)
            )).scalar_one_or_none()
        if tenant is None:
            meta["reason"] = "tenant-not-found"
            return slots, meta

        origin_lat, origin_lon, puffer, origin_source = await self._resolve_routing_origin(
            tenant, employee_id,
        )
        if origin_lat is None or origin_lon is None:
            meta["reason"] = "no-werkstatt-geo"
            return slots, meta
        meta["origin_source"] = origin_source

        werkstatt = GeoPoint(float(origin_lat), float(origin_lon))

        # Kunden-Adresse geocoden (cache-first)
        kunde_geo = await ors_geocode_address(kunde_adresse)
        if kunde_geo is None:
            meta["reason"] = "customer-not-geocodable"
            return slots, meta

        # Cache fuer Tagespläne (1 API-Call pro Tag, nicht pro Slot).
        # Provider-agnostisch ueber den Adapter — Google ODER Outlook.
        day_events_cache: dict[str, list[dict]] = {}

        async def _events_for_day(target_date) -> list[dict]:
            key = target_date.isoformat()
            if key in day_events_cache:
                return day_events_cache[key]
            try:
                events = await adapter.list_events_for_day(target_date)
                day_events_cache[key] = events
                return events
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Smart-Filter: events.list({target_date}) crashed: {exc}"
                )
                day_events_cache[key] = []
                return []

        async def _location_geo(loc_text: str) -> GeoPoint:
            """Adresse aus Calendar-Event geocoden, oder Werkstatt-Fallback."""
            if not loc_text:
                return werkstatt
            geo = await ors_geocode_address(loc_text)
            return geo or werkstatt

        # Pro Slot: Travel-Time-Check + Sortier-Score
        enriched: list[dict] = []
        for slot in slots:
            try:
                slot_dt = datetime.strptime(
                    f"{slot['datum']} {slot['uhrzeit']}", "%d.%m.%Y %H:%M",
                )
            except Exception:
                # Slot-Parsing failed — best-effort durchlassen
                enriched.append(slot)
                continue
            slot_end = slot_dt + timedelta(minutes=dauer_min)
            day_events = await _events_for_day(slot_dt.date())

            # Vor-Termin (letzter, der vor slot_dt endet)
            vor = max(
                (e for e in day_events if e["end_dt"] <= slot_dt),
                key=lambda e: e["end_dt"],
                default=None,
            )
            # Nach-Termin (erster, der nach slot_end startet)
            nach = min(
                (e for e in day_events if e["start_dt"] >= slot_end),
                key=lambda e: e["start_dt"],
                default=None,
            )

            vor_geo = await _location_geo(vor["location"]) if vor else werkstatt
            nach_geo = await _location_geo(nach["location"]) if nach else werkstatt

            t_in = await ors_travel_time_minutes(vor_geo, kunde_geo)
            t_out = await ors_travel_time_minutes(kunde_geo, nach_geo)

            if t_in is None or t_out is None:
                # ORS-Fail — Slot durchlassen ohne Bewertung
                slot["_total_travel"] = 9999
                enriched.append(slot)
                continue

            # Constraint: passt Slot zwischen Vor und Nach?
            if vor is not None:
                avail = (slot_dt - vor["end_dt"]).total_seconds() / 60.0
                if avail < (t_in + puffer):
                    meta["removed"] += 1
                    continue
            if nach is not None:
                avail = (nach["start_dt"] - slot_end).total_seconds() / 60.0
                if avail < (t_out + puffer):
                    meta["removed"] += 1
                    continue

            slot["_total_travel"] = t_in + t_out
            slot["fahrtzeit_min"] = t_in + t_out
            slot["fahrtzeit_info"] = (
                f"Anfahrt {t_in} Min" + (
                    f", Weiterfahrt {t_out} Min" if nach is not None
                    else f", danach Heimfahrt ca. {t_out} Min"
                )
            )
            enriched.append(slot)

        # Sortieren: kuerzeste Gesamt-Fahrtzeit oben, dann Datum/Uhrzeit
        enriched.sort(
            key=lambda s: (
                s.get("_total_travel", 9999), s["datum"], s["uhrzeit"],
            ),
        )
        for s in enriched:
            s.pop("_total_travel", None)

        meta["applied"] = True
        meta["puffer_min"] = puffer
        meta["kunde_lat"] = kunde_geo.lat
        meta["kunde_lon"] = kunde_geo.lon
        return enriched, meta

    async def _find_events(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sucht Termine nach Telefon ODER Email ueber ALLE Mitarbeiter-Kalender.

        Storno-Pipeline-Eintrittspunkt: Voice-Anrufer / Mail-Storno /
        Telegram-Wizard rufen das hier auf, kriegen eine deduplizierte
        Liste passender Events ueber alle aktiven Mitarbeiter zurueck.

        Payload:
          - kunde_telefon (str, optional): wird hier normalisiert
          - kunde_email (str, optional)
          - time_min (ISO-String, optional, Default: jetzt)
          - time_max (ISO-String, optional, Default: jetzt + 30 Tage)

        Response:
          {"erfolg": True, "anzahl": N, "termine": [...]}
          Pro Termin: event_id, employee_id (welcher Kalender),
          start_dt/end_dt (ISO), summary, description, location,
          kunde_telefon_match, kunde_email_match, match_source.
        """
        try:
            telefon_raw = (payload.get("kunde_telefon") or "").strip()
            email_raw = (payload.get("kunde_email") or "").strip()
            telefon_norm = normalize_phone(telefon_raw) or None
            email_norm = email_raw.lower() or None
            if not telefon_norm and not email_norm:
                return {
                    "erfolg": False,
                    "nachricht": "kunde_telefon oder kunde_email erforderlich",
                }

            # Zeitraum default: jetzt → +30 Tage
            from dateutil import parser as _p  # type: ignore
            now = datetime.now()
            time_min_raw = payload.get("time_min")
            time_max_raw = payload.get("time_max")
            time_min = _p.isoparse(time_min_raw).replace(tzinfo=None) if time_min_raw else now
            time_max = _p.isoparse(time_max_raw).replace(tzinfo=None) if time_max_raw else now + timedelta(days=30)

            # Alle aktiven Mitarbeiter durchgehen — jeder hat eigenen
            # Kalender. Default-Employee zuerst (Reihenfolge aus
            # get_employees_for_tenant ist is_default DESC, slug ASC).
            from core.models.employee import get_employees_for_tenant
            employees = await get_employees_for_tenant(self.tenant_id, active_only=True)
            if not employees:
                return {"erfolg": True, "anzahl": 0, "termine": []}

            termine: list[dict[str, Any]] = []
            seen_event_ids: set[str] = set()  # gegen Cross-Kalender-Dupes (selten)
            for emp in employees:
                try:
                    adapter = await get_calendar_adapter(
                        self.tenant_id, employee_id=emp.id,
                        fallback_calendar_id=self.config["calendar_id"],
                    )
                    found = await adapter.find_events(
                        time_min=time_min, time_max=time_max,
                        kunde_telefon_normalized=telefon_norm,
                        kunde_email=email_norm,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"find_events: emp={emp.slug} crash: {exc}")
                    continue
                for ev in found:
                    eid = ev.get("event_id") or ""
                    if not eid or eid in seen_event_ids:
                        continue
                    seen_event_ids.add(eid)
                    termine.append({
                        "event_id": eid,
                        "employee_id": str(emp.id),
                        "employee_slug": emp.slug,
                        "start_dt": ev["start_dt"].isoformat(),
                        "end_dt": ev["end_dt"].isoformat(),
                        "summary": ev.get("summary", ""),
                        "description": ev.get("description", ""),
                        "location": ev.get("location", ""),
                        "kunde_telefon_match": ev.get("kunde_telefon_match", False),
                        "kunde_email_match": ev.get("kunde_email_match", False),
                        "match_source": ev.get("match_source", ""),
                    })

            # Chronologisch sortieren — naechster Termin oben
            termine.sort(key=lambda t: t["start_dt"])
            return {"erfolg": True, "anzahl": len(termine), "termine": termine}

        except Exception as e:
            logger.exception(f"find_events crashed: {e}")
            return {"erfolg": False, "nachricht": f"Fehler bei Termin-Suche: {str(e)}"}

    async def _cancel_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Loescht einen Termin (provider-agnostisch via Adapter)."""
        try:
            event_id = payload.get("event_id")
            if not event_id:
                return {"erfolg": False, "nachricht": "event_id fehlt"}

            employee_id = payload.get("employee_id")
            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=employee_id,
                fallback_calendar_id=self.config["calendar_id"],
            )
            ok = await adapter.delete_event(event_id)
            if not ok:
                return {"erfolg": False, "nachricht": "Loeschen fehlgeschlagen"}

            return {
                "erfolg": True,
                "nachricht": "Termin geloescht.",
                "event_id": event_id,
            }

        except Exception as e:
            return {"erfolg": False, "nachricht": f"Fehler beim Loeschen: {str(e)}"}

    async def _suche_slots_am_tag(
        self,
        adapter,
        target_date,
        wunsch_uhrzeit_anker,
        max_count: int,
        dauer: int,
    ) -> list[dict]:
        """
        Sucht freie Slots an einem konkreten Tag.

        Geht in 30-Minuten-Schritten durch die Arbeitszeiten und prueft
        Belegung gegen die FreeBusy-API des jeweiligen Providers.
        """
        from datetime import datetime, time, timedelta

        # Wochentag-Filter (z.B. Mo-Fr)
        if target_date.weekday() not in self.config["arbeitstage"]:
            return []

        # Arbeitszeiten parsen
        h_start, m_start = self._parse_zeit(self.config["arbeitszeiten_start"])
        h_ende, m_ende = self._parse_zeit(self.config["arbeitszeiten_ende"])

        slot_start_dt = datetime.combine(
            target_date, time(hour=h_start, minute=m_start)
        )
        tag_ende_dt = datetime.combine(
            target_date, time(hour=h_ende, minute=m_ende)
        )

        # Liste aller potentiellen Slot-Starts (30-Min-Raster)
        kandidaten: list[datetime] = []
        cur = slot_start_dt
        while cur + timedelta(minutes=dauer) <= tag_ende_dt:
            kandidaten.append(cur)
            cur += timedelta(minutes=30)

        # Sortierung: bei Anker -> wunsch_uhrzeit zuerst (closest), sonst chronologisch
        if wunsch_uhrzeit_anker is not None:
            anker_dt = datetime.combine(target_date, wunsch_uhrzeit_anker)
            kandidaten.sort(key=lambda c: abs((c - anker_dt).total_seconds()))

        # FreeBusy-Range fuer den Tag holen (1 API-Call statt n).
        # Provider-agnostisch via Adapter: Google nutzt freebusy().query(),
        # Microsoft nutzt /me/calendar/getSchedule.
        try:
            busy = await adapter.get_busy_periods(slot_start_dt, tag_ende_dt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"FreeBusy-Query crashed ({adapter.provider_name}): {exc}")
            busy = []

        # Helper: ist das Intervall [start, start+dauer] frei?
        def ist_frei(start_dt: datetime) -> bool:
            ende_dt = start_dt + timedelta(minutes=dauer)
            for b in busy:
                from dateutil import parser  # type: ignore
                b_start = parser.isoparse(b["start"]).replace(tzinfo=None)
                b_ende = parser.isoparse(b["end"]).replace(tzinfo=None)
                # Ueberlappung: NICHT (slot_ende <= b_start ODER slot_start >= b_ende)
                if not (ende_dt <= b_start or start_dt >= b_ende):
                    return False
            return True

        # Filter: nur freie Slots, max max_count
        freie: list[dict] = []
        for kandidat in kandidaten:
            if len(freie) >= max_count:
                break
            if ist_frei(kandidat):
                freie.append({
                    "datum": kandidat.strftime("%d.%m.%Y"),
                    "uhrzeit": kandidat.strftime("%H:%M"),
                    "wochentag": [
                        "Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"
                    ][kandidat.weekday()],
                })

        # Sortiere zur Ausgabe wieder chronologisch
        freie.sort(key=lambda s: (s["datum"], s["uhrzeit"]))
        return freie

    def _naechster_werktag(self, d):
        """Gibt das naechste Datum zurueck das in arbeitstage liegt."""
        from datetime import timedelta
        cand = d + timedelta(days=1)
        # Maximal 14 Tage in die Zukunft suchen, sonst Notfall-Abbruch
        for _ in range(14):
            if cand.weekday() in self.config["arbeitstage"]:
                return cand
            cand += timedelta(days=1)
        return cand

    # ---- Hilfsfunktionen ----

    @staticmethod
    def _parse_datum_uhrzeit(datum: str, uhrzeit: str) -> datetime:
        """Parst verschiedene Datums- und Zeitformate."""
        if "." in datum:
            dt = datetime.strptime(datum, "%d.%m.%Y")
        elif "-" in datum:
            dt = datetime.strptime(datum, "%Y-%m-%d")
        else:
            raise ValueError(f"Unbekanntes Datumsformat: {datum}")

        h, m = Plugin._parse_zeit(uhrzeit)
        return dt.replace(hour=h, minute=m, second=0)

    @staticmethod
    def _parse_zeit(zeit: str) -> tuple[int, int]:
        """Parst "HH:MM" oder "H Uhr" zu (hour, minute)."""
        zeit = zeit.replace(" Uhr", "").strip()
        if ":" in zeit:
            h, m = map(int, zeit.split(":"))
        else:
            h = int(zeit)
            m = 0
        return h, m
