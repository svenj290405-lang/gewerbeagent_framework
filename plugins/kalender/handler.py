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

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Booking Concurrency + Idempotency
# ----------------------------------------------------------------------
# In-process Lock-Map: pro (tenant_id, slot-start-minute) genau ein Booking
# gleichzeitig. Verhindert Doppelbuchung wenn z.B. Mail-Pipeline und
# Voice-Pipeline parallel auf den gleichen Slot zielen.
_SLOT_LOCKS: dict[tuple[UUID, datetime], asyncio.Lock] = {}


# Idempotency-Cache: bei wiederholtem _book_appointment-Aufruf mit
# gleichem Key (z.B. Mail-Message-ID) gibt es das vorherige Resultat
# zurueck statt eine zweite Buchung zu machen.
# TTL: 24h (genug fuer Container-Restart-Recovery).
# Obergrenzen der Slot-Suche. MAX_SLOTS entspricht der bisherigen
# 3+2+1-Staffel im Wunschtermin-Modus; MAX_DAYS_AHEAD deckelt den
# Tage-Modus, damit ein Aufrufer nicht ein Jahr Kalender abfragt.
MAX_SLOTS = 6
MAX_DAYS_AHEAD = 14
# Mindest-Vorlauf fuer Slots am heutigen Tag — niemandem einen Termin
# in 5 Minuten vorschlagen (und erst recht keinen in der Vergangenheit).
SLOT_VORLAUF_MINUTEN = 60

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
        # Kein zusaetzliches Guard-Lock noetig: diese Funktion ist synchron
        # und enthaelt keinen await-Punkt -> in asyncios Single-Thread-Loop
        # laeuft das get-check-set atomar, zwei Coroutinen koennen hier nicht
        # interleaven und versehentlich zwei Locks fuer denselben Slot bauen.
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
        # kalender ist ein INTERNAL-ONLY Plugin (Manifest external_webhook=
        # False): es wird ausschliesslich in-process aufgerufen (voice_init,
        # mail_pipeline.cancel_kunde_termine, anfrage_eingang) — diese Caller
        # rufen on_webhook OHNE `headers` auf (headers=None). Externe
        # HTTP-Aufrufe blockt bereits der zentrale Dispatcher (core/api/app.py)
        # mit 404. Defense-in-depth: sollte uns DOCH je ein HTTP-dispatchter
        # Aufruf erreichen (headers gesetzt), ist das ein Routing-/Konfig-
        # Fehler — fail-closed, niemals ausfuehren (sonst koennte jemand
        # Termine buchen/stornieren oder via find_events Kunden-PII abgreifen).
        if headers is not None:
            raise PermissionError("kalender-internal-only")
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
        elif endpoint == "attach_drive_url":
            return await self._attach_drive_url(payload)
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

            # Spiegel-Termin im Kalender des Inhabers: der Chef soll die
            # Betriebslage im eigenen Kalender sehen, ohne dass jemand die
            # App aufmachen muss. Nur wenn der Termin NICHT ohnehin bei ihm
            # liegt.
            #
            # Zwei Details, ohne die es schadet:
            #  - showAs/transparency "frei": sonst blockiert die
            #    Betriebsuebersicht die eigene Slot-Suche des Inhabers und
            #    er ist rechnerisch dauerhaft ausgebucht.
            #  - ga_mirror-Marker: damit find_events den Spiegel aus der
            #    Trefferliste filtert (sonst doppelte Storno-Vorschlaege)
            #    und der Storno ihn gezielt mitloeschen kann.
            await self._spiegel_termin(
                employee_id=employee_id, summary=summary,
                description=description, location=adresse,
                start=start, ende=ende, idempotency_key=idempotency_key,
            )

            # Push an den fuer den Termin zustaendigen Mitarbeiter (silent
            # fail, blockiert nie den Termin). Ohne employee_id geht der
            # Push an den ganzen Betrieb. Kunde/Anliegen bleiben bewusst
            # draussen — Push-Inhalte laufen ueber FCM/APNs, die Details
            # holt die App vom EU-Server.
            from core.integrations.notify import notify_employee
            await notify_employee(
                self.tenant_id, employee_id,
                title="Neuer Termin",
                body=(
                    f"{start.strftime('%a %d.%m., %H:%M')} Uhr — "
                    f"Details in der App."
                ),
                url="/app#termine", tag="buchung",
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

    async def _attach_drive_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Traegt nachtraeglich den Drive-Link in ein bestehendes Event ein.

        Im neuen Mail-Flow wird der Termin VOR dem Formular gebucht — der
        Drive-Ordner mit den Formular-Infos (Fotos, Masse, Wuensche) ent-
        steht erst beim Formular-Eingang. Dieser Endpoint haengt die
        Drive-Zeile dann an die Event-Beschreibung an (idempotent). Gleiches
        Format wie _book_appointment, damit kein Doppel-Eintrag entsteht.
        """
        event_id = (payload.get("event_id") or "").strip()
        drive_url = (payload.get("drive_url") or "").strip()
        if not event_id or not drive_url:
            return {
                "erfolg": False,
                "nachricht": "event_id oder drive_url fehlt",
            }
        employee_id = payload.get("employee_id")
        try:
            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=employee_id,
                fallback_calendar_id=self.config["calendar_id"],
            )
            ok = await adapter.attach_drive_link(event_id, drive_url)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"attach_drive_url fehlgeschlagen: {e}")
            return {"erfolg": False, "nachricht": str(e)}
        return {"erfolg": bool(ok)}

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
            # Zweiter Aufruf-Modus: "die naechsten N Tage" statt eines
            # konkreten Wunschtermins. So rufen die PWA
            # (/termine/freie-slots, /verbindungen/kalender/test) und der
            # Q-Assistent auf. Ohne diesen Zweig lief
            # _parse_datum_uhrzeit("", "") in einen ValueError und die App
            # zeigte IMMER eine leere Slot-Liste.
            days_ahead_raw = payload.get("days_ahead")

            if wunsch_datum:
                wunsch = self._parse_datum_uhrzeit(wunsch_datum, wunsch_uhrzeit)
                anker_zeit = wunsch.time()
            else:
                # Kein Wunschtermin: ab heute suchen (bzw. ab dem naechsten
                # Arbeitstag), ohne Uhrzeit-Anker.
                heute = datetime.now()
                if heute.weekday() in self.config["arbeitstage"]:
                    wunsch = heute
                else:
                    wunsch = datetime.combine(
                        self._naechster_werktag(heute.date()), heute.time(),
                    )
                anker_zeit = None

            # --- Kandidaten bestimmen -------------------------------
            # Ein explizit angefragter Mitarbeiter schraenkt auf genau
            # den ein. Sonst Fan-Out ueber alle, die am Zieltag arbeiten
            # (get_available_employees prueft Abwesenheit UND
            # Arbeitszeit) — bisher filterten Krankmeldungen nur den
            # Router, nicht die Slot-Suche.
            kandidaten_emps = await self._slot_kandidaten(
                employee_id, wunsch,
            )

            slots: list[dict] = []

            if kandidaten_emps:
                slots = await self._slots_ueber_mitarbeiter(
                    kandidaten_emps, wunsch, anker_zeit, dauer,
                    days_ahead_raw if not wunsch_datum else None,
                )
                # Smart-Filter braucht einen Adapter nur, wenn er auch
                # laeuft (Kundenadresse vorhanden). Sonst waere es ein
                # DB- und Token-Roundtrip fuer nichts.
                adapter = None
                if kunde_adresse:
                    adapter = await get_calendar_adapter(
                        self.tenant_id, employee_id=employee_id,
                        fallback_calendar_id=self.config["calendar_id"],
                    )
                smart_meta = {"applied": False, "reason": None, "removed": 0}
                if adapter is not None:
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

            # --- Rueckfall: kein Mitarbeiter mit eigenem Kalender ----
            # Dann wie bisher der Tenant-Default-Kalender.
            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=employee_id,
                fallback_calendar_id=self.config["calendar_id"],
            )

            if days_ahead_raw is not None and not wunsch_datum:
                # Tage-Modus: Arbeitstage durchgehen bis MAX_SLOTS voll sind.
                # Clamp, damit ein Aufrufer nicht ein Jahr Kalender abfragt —
                # der PWA-Pfad (app_screens.py) clampt selbst nicht.
                try:
                    days_ahead = int(days_ahead_raw)
                except (TypeError, ValueError):
                    days_ahead = 7
                days_ahead = max(1, min(days_ahead, MAX_DAYS_AHEAD))

                tag = wunsch.date()
                for i in range(days_ahead):
                    if len(slots) >= MAX_SLOTS:
                        break
                    if tag.weekday() in self.config["arbeitstage"]:
                        slots.extend(await self._suche_slots_am_tag(
                            adapter, tag,
                            wunsch_uhrzeit_anker=anker_zeit if i == 0 else None,
                            max_count=MAX_SLOTS - len(slots), dauer=dauer,
                        ))
                    tag = tag + timedelta(days=1)
                slots = slots[:MAX_SLOTS]
            else:
                # Wunschtermin-Modus (unveraendert): 3 Slots am Wunschtag,
                # 2 am naechsten, 1 am uebernaechsten Werktag.
                slots.extend(await self._suche_slots_am_tag(
                    adapter, wunsch.date(), wunsch_uhrzeit_anker=anker_zeit,
                    max_count=3, dauer=dauer,
                ))
                naechster_tag = self._naechster_werktag(wunsch.date())
                slots.extend(await self._suche_slots_am_tag(
                    adapter, naechster_tag, wunsch_uhrzeit_anker=None, max_count=2, dauer=dauer
                ))
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
            # "slots" MUSS mit raus: alle Aufrufer machen
            # `out.get("slots") or []` und koennen sonst "nichts frei" nicht
            # von "Aufruf kaputt" unterscheiden — genau daran hing das
            # falsche Gruen im Diagnose-Screen.
            logger.exception("find_free_slots fehlgeschlagen: %s", e)
            return {
                "erfolg": False,
                "nachricht": f"Fehler bei Slot-Suche: {str(e)}",
                "slots": [],
                "anzahl": 0,
            }

    async def _spiegel_termin(
        self, *, employee_id, summary, description, location,
        start, ende, idempotency_key,
    ) -> None:
        """Legt eine Kopie des Termins im Kalender des Inhabers ab.

        Best-effort: schlaegt es fehl, bleibt die Hauptbuchung gueltig —
        eine fehlende Betriebsuebersicht ist aergerlich, ein verlorener
        Kundentermin waere schlimm.
        """
        from core.models.employee import get_default_employee

        try:
            chef = await get_default_employee(self.tenant_id)
            if chef is None or not getattr(chef, "calendar_provider", None):
                return
            if employee_id and str(chef.id) == str(employee_id):
                return  # liegt ohnehin schon bei ihm
            if not employee_id:
                return  # ohne Mitarbeiter-Bezug landet der Termin eh beim Chef

            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=chef.id,
                fallback_calendar_id=self.config["calendar_id"],
            )
            await adapter.create_event(
                summary=f"[Team] {summary}",
                description=description,
                location=location,
                start=start, end=ende,
                timezone=self.config["zeitzone"],
                idempotency_key=idempotency_key,
                transparent=True,
                zusatz_props={"ga_mirror": "1"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Spiegel-Termin im Inhaber-Kalender fehlgeschlagen: %s", exc,
            )

    async def _spiegel_loeschen(self, employee_id, event_id, payload) -> None:
        """Entfernt den Spiegel-Termin aus dem Kalender des Inhabers.

        Gefunden wird er ueber den Kunden (Telefon/Mail/Name) im selben
        Zeitfenster; die Event-ID des Originals gilt im fremden Kalender
        nicht. Best-effort — schlaegt es fehl, ist der Haupttermin
        trotzdem storniert.
        """
        from core.models.employee import get_default_employee

        try:
            chef = await get_default_employee(self.tenant_id)
            if chef is None or not getattr(chef, "calendar_provider", None):
                return
            if not employee_id or str(chef.id) == str(employee_id):
                return

            telefon = normalize_phone(payload.get("kunde_telefon") or "") or None
            email = (payload.get("kunde_email") or "").strip().lower() or None
            name = (payload.get("kunde_name") or "").strip() or None
            if not (telefon or email or name):
                return

            adapter = await get_calendar_adapter(
                self.tenant_id, employee_id=chef.id,
                fallback_calendar_id=self.config["calendar_id"],
            )
            now = datetime.now()
            treffer = await adapter.find_events(
                time_min=now - timedelta(days=1),
                time_max=now + timedelta(days=365),
                kunde_telefon_normalized=telefon,
                kunde_email=email,
                kunde_name=name,
            )
            for ev in treffer:
                if (ev.get("summary") or "").startswith("[Team] "):
                    await adapter.delete_event(ev.get("event_id"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spiegel-Termin konnte nicht entfernt werden: %s", exc)

    # ------------------------------------------------------------------
    # FAN-OUT ueber Mitarbeiter-Kalender
    # ------------------------------------------------------------------

    async def _slot_kandidaten(self, employee_id, anker_dt):
        """Welche Mitarbeiter kommen fuer die Slot-Suche in Frage?

        Leere Liste = kein Fan-Out moeglich, der Aufrufer faellt auf den
        Tenant-Default-Kalender zurueck (Verhalten wie vor dem Fan-Out).

        Bedingungen:
          - eigener Kalender verbunden (calendar_provider gesetzt) —
            ohne den wuerde der stille Token-Fallback die Termine
            im Kalender des Inhabers landen lassen
          - arbeitet zum Zielzeitpunkt (Abwesenheit + Arbeitszeit)
        """
        from core.models.employee import Employee
        from core.models.employee_absence import get_available_employees

        if employee_id:
            # Tenant-Filter mit in die Query: eine employee_id aus einem
            # fremden Betrieb darf nie einen Kalender aufmachen.
            async with AsyncSessionLocal() as session:
                emp = (await session.execute(
                    select(Employee)
                    .where(Employee.id == employee_id)
                    .where(Employee.tenant_id == self.tenant_id)
                )).scalar_one_or_none()
                if emp is not None:
                    session.expunge(emp)
            return [emp] if emp is not None else []

        try:
            verfuegbar = await get_available_employees(self.tenant_id, anker_dt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kandidaten-Ermittlung fehlgeschlagen: %s", exc)
            return []
        return [e for e in verfuegbar if getattr(e, "calendar_provider", None)]

    async def _slots_ueber_mitarbeiter(
        self, employees, wunsch, anker_zeit, dauer, days_ahead_raw,
    ) -> list[dict]:
        """Sucht Slots parallel in mehreren Mitarbeiter-Kalendern.

        Entdoppelt nach Zeitpunkt: bei drei freien Mitarbeitern soll der
        Kunde nicht dreimal "Di 10:00" angeboten bekommen, sondern
        einmal — mit einem davon. Wer den Slot bekommt, entscheidet die
        Reihenfolge der Kandidaten (Nicht-Inhaber zuerst, der Chef
        springt nur ein).
        """
        tage = self._slot_tage(wunsch, days_ahead_raw)

        async def _fuer(emp):
            try:
                adapter = await get_calendar_adapter(
                    self.tenant_id, employee_id=emp.id,
                    fallback_calendar_id=self.config["calendar_id"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Kalender fuer %s nicht nutzbar: %s", emp.slug, exc)
                return []
            raus: list[dict] = []
            for i, tag in enumerate(tage):
                if len(raus) >= MAX_SLOTS:
                    break
                try:
                    raus.extend(await self._suche_slots_am_tag(
                        adapter, tag,
                        wunsch_uhrzeit_anker=anker_zeit if i == 0 else None,
                        max_count=MAX_SLOTS - len(raus), dauer=dauer,
                        employee=emp,
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Slot-Suche %s am %s: %s", emp.slug, tag, exc,
                    )
            return raus

        # Parallel — seriell waeren es bei zehn Mitarbeitern zehnmal
        # die FreeBusy-Latenz hintereinander.
        ergebnisse = await asyncio.gather(
            *(_fuer(e) for e in employees), return_exceptions=True,
        )

        # Inhaber ans Ende: er soll nur einspringen, wenn sonst niemand
        # kann. Die Reihenfolge entscheidet bei der Entdopplung.
        sortiert: list[dict] = []
        for emp, res in zip(employees, ergebnisse):
            if isinstance(res, Exception):
                logger.warning("Slot-Suche %s abgebrochen: %s", emp.slug, res)
                continue
            sortiert.append((getattr(emp, "is_default", False), res))
        sortiert.sort(key=lambda x: x[0])

        gesehen: set[tuple[str, str]] = set()
        zusammen: list[dict] = []
        for _, slots in sortiert:
            for slot in slots:
                key = (slot["datum"], slot["uhrzeit"])
                if key in gesehen:
                    continue
                gesehen.add(key)
                zusammen.append(slot)

        zusammen.sort(key=lambda s: (
            datetime.strptime(s["datum"], "%d.%m.%Y"), s["uhrzeit"],
        ))
        return zusammen[:MAX_SLOTS]

    def _slot_tage(self, wunsch, days_ahead_raw) -> list:
        """Welche Tage abgesucht werden — beide Aufruf-Modi."""
        if days_ahead_raw is not None:
            try:
                n = int(days_ahead_raw)
            except (TypeError, ValueError):
                n = 7
            n = max(1, min(n, MAX_DAYS_AHEAD))
            return [wunsch.date() + timedelta(days=i) for i in range(n)]
        # Wunschtermin-Modus: Wunschtag + die zwei folgenden Werktage.
        t0 = wunsch.date()
        t1 = self._naechster_werktag(t0)
        t2 = self._naechster_werktag(t1)
        return [t0, t1, t2]

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

        # Werkstatt/Smart-Routing ist momentan deaktiviert (Feature-Kill-
        # Switch in core/features/check.py). Hart aus, damit auch Tenants
        # mit Alt-Heimat-Geo keine Fahrtzeit-Filterung mehr bekommen.
        # Reversibel: diesen Block entfernen + Feature reaktivieren.
        meta["reason"] = "werkstatt-disabled"
        return slots, meta

        if not slots:  # noqa: unreachable — bewusst, bis Werkstatt zurueckkommt
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
        App-Ansichten rufen das hier auf, kriegen eine deduplizierte
        Liste passender Events ueber alle aktiven Mitarbeiter zurueck.

        Payload:
          - kunde_telefon (str, optional): wird hier normalisiert
          - kunde_email (str, optional)
          - kunde_name (str, optional): Volltext-Namenssuche
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
            name_raw = (payload.get("kunde_name") or "").strip()
            telefon_norm = normalize_phone(telefon_raw) or None
            email_norm = email_raw.lower() or None
            name_query = name_raw or None
            if not telefon_norm and not email_norm and not name_query:
                return {
                    "erfolg": False,
                    "nachricht": (
                        "kunde_telefon, kunde_email oder kunde_name erforderlich"
                    ),
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
                        kunde_name=name_query,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"find_events: emp={emp.slug} crash: {exc}")
                    continue
                for ev in found:
                    eid = ev.get("event_id") or ""
                    if not eid or eid in seen_event_ids:
                        continue
                    # Spiegel-Termine (Kopie im Inhaber-Kalender) sind
                    # keine eigenstaendigen Termine. Ohne diesen Filter
                    # bekaeme der Storno-Wizard denselben Termin zweimal
                    # zur Auswahl.
                    if (ev.get("summary") or "").startswith("[Team] "):
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
                        "kunde_name_match": ev.get("kunde_name_match", False),
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

            # Spiegel im Inhaber-Kalender mitnehmen — sonst bleibt dort
            # ein Geistertermin stehen, den niemand mehr zuordnen kann.
            await self._spiegel_loeschen(employee_id, event_id, payload)

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
        employee=None,
    ) -> list[dict]:
        """
        Sucht freie Slots an einem konkreten Tag.

        Geht in 30-Minuten-Schritten durch die Arbeitszeiten und prueft
        Belegung gegen die FreeBusy-API des jeweiligen Providers.
        """
        from datetime import datetime, time, timedelta

        # Arbeitstage/-zeiten: der Mitarbeiter schlaegt den Tenant-Default.
        # Employee.arbeitszeiten/-tage wurden bisher NUR vom Router
        # gelesen — die Slot-Suche bot dem Fruehschichtler trotzdem
        # Termine bis 17 Uhr an.
        arbeitstage = self.config["arbeitstage"]
        start_str = self.config["arbeitszeiten_start"]
        ende_str = self.config["arbeitszeiten_ende"]
        if employee is not None:
            if getattr(employee, "arbeitstage", None):
                arbeitstage = employee.arbeitstage
            zeiten = getattr(employee, "arbeitszeiten", None) or {}
            start_str = zeiten.get("start") or start_str
            ende_str = zeiten.get("end") or zeiten.get("ende") or ende_str

        # Wochentag-Filter (z.B. Mo-Fr)
        if target_date.weekday() not in arbeitstage:
            return []

        # Arbeitszeiten parsen
        h_start, m_start = self._parse_zeit(start_str)
        h_ende, m_ende = self._parse_zeit(ende_str)

        slot_start_dt = datetime.combine(
            target_date, time(hour=h_start, minute=m_start)
        )
        tag_ende_dt = datetime.combine(
            target_date, time(hour=h_ende, minute=m_ende)
        )

        # Vergangenheit ueberspringen: am heutigen Tag darf der erste Slot
        # nicht vor "jetzt + Vorlauf" liegen. Faellt erst seit dem
        # days_ahead-Modus auf — im Wunschtermin-Modus lag der Anker
        # praktisch immer in der Zukunft.
        jetzt = datetime.now()
        if target_date == jetzt.date():
            frueheste = jetzt + timedelta(minutes=SLOT_VORLAUF_MINUTEN)
            # auf das naechste 30-Minuten-Raster aufrunden
            rest = frueheste.minute % 30
            if rest or frueheste.second or frueheste.microsecond:
                frueheste += timedelta(minutes=30 - rest)
            frueheste = frueheste.replace(second=0, microsecond=0)
            if frueheste > slot_start_dt:
                slot_start_dt = frueheste

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
                    # Wessen Kalender dieser Slot ist. Ohne dieses Feld
                    # kann kein Aufrufer den Slot spaeter auf einen
                    # Kalender zurueckbilden — genau daran lief der
                    # Mail-Pfad ins Leere, der es schon abfragt.
                    "employee_id": str(employee.id) if employee is not None else None,
                    "employee_slug": getattr(employee, "slug", None) if employee is not None else None,
                    "employee_name": getattr(employee, "name", None) if employee is not None else None,
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
