"""Gespraechs-Abschluss — „fertig, beim Kunden einpflegen".

Am Ende eines Kundengespraechs steht genau eine Bewegung: alles, was vor
Ort zusammengekommen ist, wandert dorthin, wo der Betrieb es spaeter
wiederfindet — zum Kunden.

    1. Kunde sicherstellen  Gibt es den Kunden im Kundenstamm noch nicht,
                            wird er hier angelegt (der Handwerker tippt
                            vor Ort einen Namen, nicht einen Datensatz).
    2. Drive-Kundenordner   Protokoll als Google Doc, dazu die im Q-Chat
                            gerenderten Visualisierungen. Fotos liegen
                            schon dort — die wurden beim Hochladen direkt
                            in den Kundenordner gelegt.
    3. Status               ``abgeschlossen`` + Zeitstempel. Das Gespraech
                            bleibt sichtbar, faellt aber aus den offenen
                            Beratungs-Leads raus.

**Der Kunde wird IMMER angelegt, Drive ist best-effort.** Ohne
Drive-Verbindung (oder bei einem API-Fehler) gilt das Gespraech trotzdem
als eingepflegt; der Aufrufer bekommt einen ``hinweis`` fuer die Oberflaeche
statt einer Fehlermeldung. Ein Gespraech, das wegen eines Drive-Timeouts
offen bleibt, waere fuer den Handwerker schlimmer als ein fehlendes PDF.

**Was NICHT ins Drive geht: das Roh-Transkript.** Zusammenfassung, Notizen,
To-dos und die interne Handnotiz sind der bewusst festgehaltene Ertrag des
Gespraechs — die woertliche Rede des Kunden ist Arbeitsmaterial und faellt
im Betrieb unter die Aufbewahrungsfrist (``dsgvo_cleanup_cron``). Im Drive
wuerde sie diese Frist ueberleben, deshalb bleibt sie in der DB.

Nicht zu verwechseln mit der Vertraulichkeitsgrenze in
``core/services/kundengespraech.py``: die regelt, was der KUNDE per Mail
sieht. Der Drive-Kundenordner gehoert dem Betrieb — interne Notizen duerfen
hier stehen und sind als solche gekennzeichnet.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

from sqlalchemy import select

from core.database.connection import get_session
from core.models.kundengespraech import (
    KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN,
    Kundengespraech,
)

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


def _fmt(zeit: dt.datetime | None) -> str:
    return zeit.strftime("%d.%m.%Y um %H:%M Uhr") if zeit else ""


def protokoll_dateiname(g: Kundengespraech) -> str:
    """'Gespraech 2026-07-31 — Mueller, Hans'. Drive erlaubt alles ausser '/'."""
    tag = (g.gespraech_datum or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    name = (g.kunde_name or "Unbekannt").replace("/", "-").strip()
    return f"Gespraech {tag} — {name}"[:180]


def protokoll_html(daten: dict) -> str:
    """Das Protokoll als HTML — Drive macht daraus beim Upload ein Google
    Doc (lesbar, druckbar, durchsuchbar, ohne PDF-Bibliothek).

    Aufbau folgt dem Gespraech: erst der Kunde, dann was besprochen wurde,
    dann was zu tun ist, ganz unten das Interne — klar abgesetzt, damit
    niemand die Handnotiz versehentlich weiterreicht.
    """
    from html import escape

    def absatz(titel: str, text: str) -> str:
        if not (text or "").strip():
            return ""
        return (f"<h2>{escape(titel)}</h2>"
                f"<p>{escape(text).replace(chr(10), '<br>')}</p>")

    def z(label: str, wert: str) -> str:
        return (f"<tr><td><b>{escape(label)}</b></td>"
                f"<td>{escape(wert)}</td></tr>") if wert else ""

    todos = "".join(f"<li>{escape(str(t))}</li>"
                    for t in daten.get("todos") or [])
    bilder = "".join(f"<li>{escape(str(b))}</li>"
                     for b in daten.get("bilder") or [])

    intern = daten.get("handnotiz") or ""
    intern_block = (
        "<hr><h2>Intern — nicht an den Kunden</h2>"
        f"<p>{escape(intern).replace(chr(10), '<br>')}</p>"
    ) if intern.strip() else ""

    return f"""<html><body>
<h1>Gespraech {escape(daten.get('kunde') or '')}</h1>
<p><i>{escape(daten.get('datum') or '')}</i></p>
<table border="0" cellpadding="4">
{z('Kunde', daten.get('kunde') or '')}
{z('Anschrift', daten.get('adresse') or '')}
{z('E-Mail', daten.get('email') or '')}
{z('Telefon', daten.get('telefon') or '')}
{z('Gefuehrt von', daten.get('mitarbeiter') or '')}
{z('Dauer', daten.get('dauer') or '')}
{z('Naechster Termin', daten.get('termin') or '')}
</table>
{absatz('Zusammenfassung', daten.get('briefing') or '')}
{absatz('Notizen', daten.get('notizen') or '')}
{f'<h2>To-dos</h2><ul>{todos}</ul>' if todos else ''}
{f'<h2>Bilder im Kundenordner</h2><ul>{bilder}</ul>' if bilder else ''}
{intern_block}
<hr>
<p><small>Automatisch erzeugt vom Gewerbeagent beim Abschluss des
Gespraechs. Das Roh-Transkript bleibt im System und faellt unter die
Aufbewahrungsfrist des Betriebs.</small></p>
</body></html>"""


async def _mitarbeiter_name(employee_id: uuid.UUID | None) -> str:
    if employee_id is None:
        return ""
    from core.models.employee import Employee
    async with get_session() as s:
        emp = await s.get(Employee, employee_id)
        return (getattr(emp, "name", "") or "") if emp else ""


async def _kunde_sicherstellen(g: Kundengespraech, tenant_id: uuid.UUID) -> dict:
    """Haengt das Gespraech an einen Kunden — legt ihn an, wenn noetig.

    Returns ``{"kunde_id", "kunde_neu", "email", "telefon", "adresse"}``.
    Eigene Session + Commit, damit der Kundenstamm auch dann steht, wenn
    Drive danach umkippt.
    """
    from core.services.kunde_identity import resolve_kunde, resolve_or_create_kunde
    from core.models import Kunde

    async with get_session() as s:
        row = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == g.id,
                Kundengespraech.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:
            return {"kunde_id": None, "kunde_neu": False,
                    "email": "", "telefon": "", "adresse": ""}

        kunde_neu = False
        if row.kunde_id is not None:
            kunde = await s.get(Kunde, row.kunde_id)
        else:
            # Vorher nachsehen, damit wir dem Handwerker ehrlich sagen
            # koennen, ob wir jemanden angelegt oder nur zugeordnet haben.
            vorher = await resolve_kunde(s, tenant_id, name=row.kunde_name)
            kunde = await resolve_or_create_kunde(s, tenant_id, row.kunde_name)
            kunde_neu = vorher is None
            row.kunde_id = kunde.id
        ergebnis = {
            "kunde_id": kunde.id if kunde else None,
            "kunde_neu": kunde_neu,
            "email": (kunde.email if kunde else "") or "",
            "telefon": (kunde.telefon if kunde else "") or "",
            "adresse": (kunde.adresse if kunde else "") or "",
        }
        await s.commit()
    return ergebnis


async def _bilder_sichern(
    tenant_id: uuid.UUID, g: Kundengespraech, employee_id: uuid.UUID | None,
) -> list[str]:
    """Schiebt die Visualisierungen in den Kundenordner und liefert die
    Namen aller Bilder, die dort jetzt liegen (fuers Protokoll)."""
    from core.models.gespraech_datei import GespraechDatei
    from core.services.kundengespraech import als_drive_anhang

    async with get_session() as s:
        dateien = list((await s.execute(
            select(GespraechDatei)
            .where(GespraechDatei.gespraech_id == g.id)
            .where(GespraechDatei.tenant_id == tenant_id)
            .order_by(GespraechDatei.created_at.asc())
        )).scalars().all())

    namen: list[str] = []
    for d in dateien:
        anhang = await als_drive_anhang(tenant_id, g.kunde_name, d, employee_id)
        if anhang and anhang.get("id"):
            namen.append(anhang.get("name") or "Bild")
    return namen


async def _protokoll_hochladen(
    tenant_id: uuid.UUID, g: Kundengespraech, employee_id: uuid.UUID | None,
    *, kunde: dict, bilder: list[str],
) -> tuple[str, str, str]:
    """Legt das Protokoll als Google Doc in den Kundenordner.

    Returns ``(folder_url, file_id, file_url)``. Wirft bei Drive-Problemen —
    der Aufrufer faengt und macht daraus einen Hinweis.
    """
    import io

    from googleapiclient.http import MediaIoBaseUpload

    from core.integrations.google_drive import (
        get_drive_service, get_or_create_kunde_folder,
    )

    # Ordner-Schluessel bewusst nur ueber den Namen — genau wie beim
    # Foto-Upload waehrend des Gespraechs. So landet das Protokoll im
    # selben Ordner wie die Fotos, ohne Umschluesselung.
    folder_id, folder_url = await get_or_create_kunde_folder(
        tenant_id, g.kunde_name, employee_id,
    )
    service = await get_drive_service(tenant_id, employee_id)

    dauer = ""
    if g.audio_dauer_sekunden:
        m, sek = divmod(int(g.audio_dauer_sekunden), 60)
        dauer = f"{m}:{sek:02d} min Diktat"
    termin = _fmt(g.termin_datum)
    if termin and g.termin_ort:
        termin += f" ({g.termin_ort})"

    html = protokoll_html({
        "kunde": g.kunde_name,
        "datum": _fmt(g.gespraech_datum),
        "adresse": kunde.get("adresse") or "",
        "email": kunde.get("email") or "",
        "telefon": kunde.get("telefon") or "",
        "mitarbeiter": await _mitarbeiter_name(employee_id),
        "dauer": dauer,
        "termin": termin,
        "briefing": g.briefing_kurz or "",
        "notizen": g.notizen_lang or "",
        "todos": list(g.todos or []),
        "handnotiz": g.handnotiz or "",
        "bilder": bilder,
    }).encode("utf-8")

    def _sync_upload():
        media = MediaIoBaseUpload(
            io.BytesIO(html), mimetype="text/html", resumable=False)
        return service.files().create(
            body={"name": protokoll_dateiname(g), "parents": [folder_id],
                  "mimeType": GOOGLE_DOC_MIME},
            media_body=media, fields="id, webViewLink",
        ).execute()

    hochgeladen = await asyncio.to_thread(_sync_upload)
    return (folder_url, hochgeladen.get("id") or "",
            hochgeladen.get("webViewLink") or "")


async def schliesse_gespraech_ab(
    tenant_id: uuid.UUID,
    gespraech_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Pflegt ein Gespraech beim Kunden ein. Wirft nie.

    Returns::

        {"ok": bool, "error": str|None, "bereits": bool,
         "kunde_name": str, "kunde_id": str, "kunde_neu": bool,
         "folder_url": str, "protokoll_url": str, "bilder": int,
         "hinweis": str|None}

    ``bereits=True`` heisst: war schon abgeschlossen — idempotent, es
    passiert nichts Zweites (kein doppeltes Protokoll im Ordner).
    """
    async with get_session() as s:
        g = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gespraech_id,
                Kundengespraech.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if g is None:
            return {"ok": False, "error": "Gespräch nicht gefunden.",
                    "bereits": False}
        if g.status == KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN:
            return {
                "ok": True, "error": None, "bereits": True,
                "kunde_name": g.kunde_name,
                "kunde_id": str(g.kunde_id) if g.kunde_id else "",
                "kunde_neu": False,
                "folder_url": "", "protokoll_url": g.protokoll_drive_url or "",
                "bilder": 0,
                "hinweis": "Dieses Gespräch war schon eingepflegt.",
            }
        s.expunge(g)

    kunde = await _kunde_sicherstellen(g, tenant_id)

    folder_url = protokoll_url = protokoll_id = ""
    bilder: list[str] = []
    hinweis = None
    try:
        bilder = await _bilder_sichern(tenant_id, g, employee_id)
        folder_url, protokoll_id, protokoll_url = await _protokoll_hochladen(
            tenant_id, g, employee_id, kunde=kunde, bilder=bilder)
    except ValueError:
        # google_drive wirft ValueError, wenn kein Token/Tenant da ist.
        hinweis = ("Google Drive ist nicht verbunden — das Gespräch ist "
                   "eingepflegt, im Kundenordner liegt aber noch nichts.")
        logger.info("Gespräch %s ohne Drive abgeschlossen (nicht verbunden).",
                    gespraech_id)
    except Exception as exc:  # noqa: BLE001
        hinweis = ("Das Gespräch ist eingepflegt, das Protokoll konnte "
                   "aber nicht in den Kundenordner geschrieben werden.")
        logger.exception("Gespräch-Abschluss: Drive-Teil gescheitert: %s", exc)

    async with get_session() as s:
        row = (await s.execute(
            select(Kundengespraech).where(
                Kundengespraech.id == gespraech_id,
                Kundengespraech.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is not None:
            row.status = KUNDENGESPRAECH_STATUS_ABGESCHLOSSEN
            row.abgeschlossen_am = dt.datetime.now(dt.timezone.utc)
            if protokoll_id:
                row.protokoll_drive_file_id = protokoll_id
                row.protokoll_drive_url = protokoll_url
            await s.commit()

    logger.info(
        "Gespräch eingepflegt: id=%s tenant=%s kunde_neu=%s bilder=%d drive=%s",
        gespraech_id, tenant_id, kunde["kunde_neu"], len(bilder),
        bool(protokoll_id),
    )
    return {
        "ok": True, "error": None, "bereits": False,
        "kunde_name": g.kunde_name,
        "kunde_id": str(kunde["kunde_id"]) if kunde["kunde_id"] else "",
        "kunde_neu": bool(kunde["kunde_neu"]),
        "folder_url": folder_url, "protokoll_url": protokoll_url,
        "bilder": len(bilder), "hinweis": hinweis,
    }


__all__ = [
    "protokoll_dateiname",
    "protokoll_html",
    "schliesse_gespraech_ab",
]
