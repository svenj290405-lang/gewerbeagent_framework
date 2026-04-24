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
