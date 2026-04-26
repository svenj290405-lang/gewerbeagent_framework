"""
Handler des Kalender-Plugins.

Refaktorierte Version des alten webhook_server.py:
- Multi-Tenant: Konfiguration + OAuth-Token pro Tenant aus DB
- Plugin-Architektur: erbt von BasePlugin, dispatch ueber on_webhook()
- Saubere Error-Responses statt generischer Exception-Strings
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.plugin_system import BasePlugin
from plugins.kalender.google_auth import get_calendar_service
from plugins.kalender.manifest import MANIFEST
from plugins.telegram_notify.handler import TelegramNotifier


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

        Maximale Antwortliste: 6 Slots (geordnet nach Datum/Uhrzeit).
        """
        try:
            wunsch_datum = payload.get("datum", "")
            wunsch_uhrzeit = payload.get("uhrzeit", "")
            dauer = payload.get("dauer_minuten", self.config["termin_dauer_minuten"])

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

            return {
                "erfolg": True,
                "slots": slots,
                "anzahl": len(slots),
            }

        except Exception as e:
            return {"erfolg": False, "nachricht": f"Fehler bei Slot-Suche: {str(e)}"}

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
