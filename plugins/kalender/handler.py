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

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.openrouteservice import (
    GeoPoint,
    geocode_address as ors_geocode_address,
    is_configured as ors_is_configured,
    travel_time_minutes as ors_travel_time_minutes,
)
from core.models import Tenant
from core.plugin_system import BasePlugin
from plugins.kalender.google_auth import get_calendar_service
from plugins.kalender.manifest import MANIFEST
from plugins.telegram_notify.handler import TelegramNotifier

logger = logging.getLogger(__name__)


class Plugin(BasePlugin):
    manifest = MANIFEST

    # ---- Dispatch ----

    async def on_webhook(
        self, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if endpoint == "check_availability":
            return await self._check_availability(payload)
        elif endpoint == "book_appointment":
            return await self._book_appointment(payload)
        elif endpoint == "find_free_slots":
            return await self._find_free_slots(payload)
        elif endpoint == "cancel_appointment":
            return await self._cancel_appointment(payload)
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

            # Google Calendar abfragen
            service = await get_calendar_service(self.tenant_id)
            tz_offset = "+02:00"  # TODO: dynamisch aus zeitzone
            events_result = service.events().list(
                calendarId=self.config["calendar_id"],
                timeMin=start.isoformat() + tz_offset,
                timeMax=ende.isoformat() + tz_offset,
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = events_result.get("items", [])

            if not events:
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
        """Traegt einen Termin in Google Calendar ein."""
        try:
            name = payload.get("name", "")
            anliegen = payload.get("anliegen", "")
            adresse = payload.get("adresse") or "Adresse nicht angegeben"
            telefon = payload.get("telefon")
            datum = payload.get("datum", "")
            uhrzeit = payload.get("uhrzeit", "")
            dauer = payload.get(
                "dauer_minuten", self.config["termin_dauer_minuten"]
            )

            start = self._parse_datum_uhrzeit(datum, uhrzeit)
            ende = start + timedelta(minutes=dauer)

            service = await get_calendar_service(self.tenant_id)

            betrieb_name = self.config["betrieb_name"]
            telefon_text = f"\nTelefon: {telefon}" if telefon else ""

            event = {
                "summary": f"[{betrieb_name}] {anliegen} - {name}",
                "description": (
                    f"Betrieb: {betrieb_name}\n"
                    f"Kunde: {name}\n"
                    f"Anliegen: {anliegen}\n"
                    f"Adresse: {adresse}"
                    f"{telefon_text}\n\n"
                    f"Eingetragen via KI-Agent Q (Gewerbeagent Framework)"
                ),
                "location": adresse,
                "start": {
                    "dateTime": start.isoformat(),
                    "timeZone": self.config["zeitzone"],
                },
                "end": {
                    "dateTime": ende.isoformat(),
                    "timeZone": self.config["zeitzone"],
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 60},
                        {"method": "popup", "minutes": 1440},
                    ],
                },
            }

            result = service.events().insert(
                calendarId=self.config["calendar_id"],
                body=event,
            ).execute()

            # Telegram-Push (silent fail, blockiert nie den Termin)
            telefon_line = f"\n<b>Telefon:</b> {telefon}" if telefon else ""
            adresse_line = f"\n<b>Adresse:</b> {adresse}" if adresse and adresse != "Adresse nicht angegeben" else ""
            await TelegramNotifier.send_for_tenant(
                self.tenant_id,
                (
                    "📅 <b>Neuer Termin</b>\n"
                    f"<b>Kunde:</b> {name}\n"
                    f"<b>Anliegen:</b> {anliegen}\n"
                    f"<b>Wann:</b> {start.strftime('%a %d.%m.%Y, %H:%M')} Uhr"
                    f"{adresse_line}"
                    f"{telefon_line}"
                ),
            )

            return {
                "erfolg": True,
                "nachricht": (
                    f"Termin erfolgreich eingetragen! "
                    f"{name}, {anliegen}, am {start.strftime('%d.%m.%Y')} "
                    f"um {start.strftime('%H:%M')} Uhr."
                ),
                "event_id": result.get("id"),
                "link": result.get("htmlLink"),
            }

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
            service = await get_calendar_service(self.tenant_id)

            slots: list[dict] = []

            # Tag 0: selber Tag
            slots.extend(self._suche_slots_am_tag(
                service, wunsch.date(), wunsch_uhrzeit_anker=wunsch.time(), max_count=3, dauer=dauer
            ))
            # Tag 1: naechster Werktag
            naechster_tag = self._naechster_werktag(wunsch.date())
            slots.extend(self._suche_slots_am_tag(
                service, naechster_tag, wunsch_uhrzeit_anker=None, max_count=2, dauer=dauer
            ))
            # Tag 2: uebernaechster Werktag
            uebernaechster_tag = self._naechster_werktag(naechster_tag)
            slots.extend(self._suche_slots_am_tag(
                service, uebernaechster_tag, wunsch_uhrzeit_anker=None, max_count=1, dauer=dauer
            ))

            # Smart-Filter (best-effort, schluckt eigene Fehler)
            smart_meta = {"applied": False, "reason": None, "removed": 0}
            try:
                slots, smart_meta = await self._smart_filter_slots(
                    slots, kunde_adresse, dauer, service,
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
        service,
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

        # Cache fuer Tagespläne (1 GCal-Call pro Tag, nicht pro Slot)
        day_events_cache: dict[str, list[dict]] = {}

        def _events_for_day(target_date) -> list[dict]:
            key = target_date.isoformat()
            if key in day_events_cache:
                return day_events_cache[key]
            try:
                from datetime import datetime as _dt, time as _time
                day_start = _dt.combine(target_date, _time(0, 0)).isoformat() + "+02:00"
                day_end = _dt.combine(target_date, _time(23, 59)).isoformat() + "+02:00"
                resp = service.events().list(
                    calendarId=self.config["calendar_id"],
                    timeMin=day_start,
                    timeMax=day_end,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                items = resp.get("items", [])
                events = []
                from dateutil import parser as _p  # type: ignore
                for ev in items:
                    s = ev.get("start", {})
                    e = ev.get("end", {})
                    s_iso = s.get("dateTime") or s.get("date")
                    e_iso = e.get("dateTime") or e.get("date")
                    if not s_iso or not e_iso:
                        continue
                    events.append({
                        "start_dt": _p.isoparse(s_iso).replace(tzinfo=None),
                        "end_dt": _p.isoparse(e_iso).replace(tzinfo=None),
                        "location": (ev.get("location") or "").strip(),
                    })
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
            day_events = _events_for_day(slot_dt.date())

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

    async def _cancel_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Loescht einen Termin per Google-Calendar event_id."""
        try:
            event_id = payload.get("event_id")
            if not event_id:
                return {"erfolg": False, "nachricht": "event_id fehlt"}

            service = await get_calendar_service(self.tenant_id)
            service.events().delete(
                calendarId=self.config["calendar_id"],
                eventId=event_id,
            ).execute()

            return {
                "erfolg": True,
                "nachricht": "Termin geloescht.",
                "event_id": event_id,
            }

        except Exception as e:
            return {"erfolg": False, "nachricht": f"Fehler beim Loeschen: {str(e)}"}

    def _suche_slots_am_tag(
        self,
        service,
        target_date,
        wunsch_uhrzeit_anker,
        max_count: int,
        dauer: int,
    ) -> list[dict]:
        """
        Sucht freie Slots an einem konkreten Tag.

        Geht in 30-Minuten-Schritten durch die Arbeitszeiten und prueft
        Belegung gegen FreeBusy-API.
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

        # FreeBusy-Range fuer den Tag holen (1 API-Call statt n)
        zeitzone = self.config["zeitzone"]
        fb_query = {
            "timeMin": slot_start_dt.isoformat() + "+02:00",  # vereinfacht; TZ-mathematisch korrekt waere besser
            "timeMax": tag_ende_dt.isoformat() + "+02:00",
            "timeZone": zeitzone,
            "items": [{"id": self.config["calendar_id"]}],
        }
        try:
            fb_result = service.freebusy().query(body=fb_query).execute()
            busy = fb_result["calendars"][self.config["calendar_id"]]["busy"]
        except Exception:
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
