"""Drive-Archiv fuer abgeschlossene Auftraege.

Sobald die Rechnung raus ist (Status ``rechnung_gesendet``), wandert der
Auftrag in einen eigenen Ordner im Google Drive des Tenants:

    Gewerbeagent — <Betrieb>/
      Abgeschlossene Auftraege/
        2026-07-30 Mueller, Hans — Bad/
          Auftragsuebersicht        (Google Doc)
          Angebot.pdf               (aus Lexware)
          Rechnung.pdf              (aus Lexware)
          <Fotos + Aufnahmen aus dem Kundenordner>

**Best-effort, niemals blockierend.** Die Archivierung haengt am Geld-Pfad
(``finalize_and_send_invoice``), darf ihn aber unter keinen Umstaenden
kippen: jeder Teilschritt ist einzeln gekapselt, Fehler werden geloggt und
schlucken sich. Ein Auftrag ohne Archiv-Ordner ist trotzdem abgeschlossen.

Die Fotos werden per ``files.copy`` uebernommen statt Download+Upload —
Quelle und Ziel liegen im selben Drive, das spart die Bytes komplett.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import uuid

from sqlalchemy import select

from core.database.connection import get_session
from core.models.angebot import Angebot

logger = logging.getLogger(__name__)

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# Name des Sammel-Ordners unter dem Tenant-Root.
ARCHIV_ORDNER_NAME = "Abgeschlossene Auftraege"

# Wie viele Dateien maximal aus dem Kundenordner mitkopiert werden.
# Deckelt die Laufzeit, falls ein Kunde ein 500-Foto-Archiv hat.
MAX_KOPIERTE_DATEIEN = 40

# Dateien, die NICHT mitkopiert werden: Unterordner und die Dokumente,
# die wir ohnehin frisch aus Lexware ziehen.
_SKIP_NAMEN = re.compile(r"^(angebot|rechnung)\.pdf$", re.IGNORECASE)


def _sicherer_ordnername(datum: dt.datetime | None, kunde: str) -> str:
    """'2026-07-30 Mueller, Hans'. Drive erlaubt fast alles ausser '/'."""
    tag = (datum or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    name = (kunde or "Unbekannt").replace("/", "-").strip()
    return f"{tag} {name}"[:180]


async def _finde_oder_erstelle_unterordner(
    service, parent_id: str, name: str,
) -> str:
    """Sucht einen Unterordner per Name unter ``parent_id`` oder legt ihn an."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")

    def _sync() -> str:
        q = (
            f"name='{escaped}' and mimeType='{DRIVE_FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed=false"
        )
        res = service.files().list(
            q=q, spaces="drive", fields="files(id)", pageSize=5,
        ).execute()
        vorhanden = res.get("files", [])
        if vorhanden:
            return vorhanden[0]["id"]
        created = service.files().create(
            body={"name": name, "mimeType": DRIVE_FOLDER_MIME,
                  "parents": [parent_id]},
            fields="id",
        ).execute()
        return created["id"]

    return await asyncio.to_thread(_sync)


def _uebersicht_html(daten: dict) -> str:
    """Die Auftragsuebersicht als HTML — Drive wandelt das beim Upload in
    ein Google Doc um (druck-/PDF-fertig, ohne PDF-Bibliothek im Projekt)."""
    from html import escape

    zeilen = "".join(
        f"<tr><td>{escape(p['name'])}</td><td align=\"right\">{escape(p['menge'])}</td>"
        f"<td align=\"right\">{escape(p['preis'])}</td></tr>"
        for p in daten.get("positionen", [])
    ) or '<tr><td colspan="3"><i>Keine Positionen erfasst</i></td></tr>'

    schritte = "".join(
        f"<li>{'✔' if s.get('zustand') == 'erledigt' else '○'} "
        f"{escape(s.get('label') or '')}"
        + (f" <i>({escape(s['erledigt_am'][:10])})</i>"
           if s.get("erledigt_am") else "")
        + "</li>"
        for s in daten.get("schritte", [])
    ) or "<li><i>Keine Schritte</i></li>"

    def z(label: str, wert: str) -> str:
        return (f"<tr><td><b>{escape(label)}</b></td>"
                f"<td>{escape(wert or '—')}</td></tr>") if wert else ""

    return f"""<html><body>
<h1>Auftrag {escape(daten.get('kunde') or '')}</h1>
<p><i>Abgeschlossen am {escape(daten.get('abgeschlossen') or '')}</i></p>
<h2>Kunde</h2>
<table border="0" cellpadding="4">
{z('Name', daten.get('kunde') or '')}
{z('Anschrift', daten.get('adresse') or '')}
{z('E-Mail', daten.get('email') or '')}
</table>
<h2>Auftrag</h2>
<table border="0" cellpadding="4">
{z('Gesamtbetrag (brutto)', daten.get('betrag') or '')}
{z('Angebotsnummer', daten.get('angebot_nr') or '')}
{z('Angebot versendet', daten.get('angebot_versendet') or '')}
{z('Auftrag angenommen', daten.get('angenommen') or '')}
</table>
<h2>Positionen</h2>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th align="left">Leistung</th><th>Menge</th><th>Preis (brutto)</th></tr>
{zeilen}
</table>
<h2>Prozess-Schritte</h2>
<ul>{schritte}</ul>
<hr>
<p><small>Automatisch erzeugt vom Gewerbeagent beim Abschluss des Auftrags.</small></p>
</body></html>"""


async def _sammle_uebersichtsdaten(tenant_id: uuid.UUID, angebot: Angebot) -> dict:
    """Baut die Daten fuer die Auftragsuebersicht zusammen."""
    from core.models.angebot_position import AngebotPosition
    from core.services.auftrag_prozess import lade_auftrag_schritte

    def eur(wert) -> str:
        return f"{float(wert):.2f} €".replace(".", ",") if wert is not None else ""

    async with get_session() as s:
        positionen = (await s.execute(
            select(AngebotPosition)
            .where(AngebotPosition.angebot_id == angebot.id)
            .order_by(AngebotPosition.position_nr)
        )).scalars().all()

    try:
        schritte = await lade_auftrag_schritte(tenant_id, angebot)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Archiv: Schritte nicht ladbar (egal): %s", exc)
        schritte = []

    adresse = " ".join(x for x in [
        angebot.kunde_strasse,
        " ".join(y for y in [angebot.kunde_plz, angebot.kunde_ort] if y),
    ] if x).strip()

    return {
        "kunde": angebot.kunde_name,
        "adresse": adresse,
        "email": angebot.kunde_email or "",
        "betrag": eur(angebot.gesamtbetrag_brutto_eur),
        "angebot_nr": angebot.lexware_voucher_number or "",
        "angebot_versendet": (
            angebot.mail_sent_at.strftime("%d.%m.%Y") if angebot.mail_sent_at else ""),
        "angenommen": (
            angebot.accepted_at.strftime("%d.%m.%Y") if angebot.accepted_at else ""),
        "abgeschlossen": dt.datetime.now(dt.timezone.utc).strftime("%d.%m.%Y"),
        "positionen": [{
            "name": p.name or "",
            "menge": f"{float(p.menge):g} {p.einheit or ''}".strip(),
            "preis": eur(p.preis_brutto_eur),
        } for p in positionen],
        "schritte": schritte,
    }


async def _lade_lexware_pdfs(tenant_id: uuid.UUID, angebot: Angebot) -> list[tuple[str, bytes]]:
    """Holt Angebots- und Rechnungs-PDF aus Lexware. Failsafe: was nicht
    geht, fehlt einfach."""
    from core.services.document_flow import _lexware_provider

    out: list[tuple[str, bytes]] = []
    try:
        provider = await _lexware_provider(tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Archiv: Lexware-Provider nicht verfuegbar: %s", exc)
        return out
    if provider is None:
        return out

    if angebot.lexware_quotation_id:
        try:
            out.append(("Angebot.pdf",
                        await provider.download_quotation_pdf(angebot.lexware_quotation_id)))
        except Exception as exc:  # noqa: BLE001
            logger.info("Archiv: Angebots-PDF nicht ladbar: %s", exc)
    if angebot.lexware_invoice_id:
        try:
            out.append(("Rechnung.pdf",
                        await provider.download_invoice_pdf(angebot.lexware_invoice_id)))
        except Exception as exc:  # noqa: BLE001
            logger.info("Archiv: Rechnungs-PDF nicht ladbar: %s", exc)
    return out


async def _kopiere_kundendateien(
    service, tenant_id: uuid.UUID, kunde_name: str, ziel_id: str,
) -> int:
    """Kopiert Fotos/Aufnahmen aus dem Kundenordner ins Auftrags-Archiv."""
    from core.integrations.google_drive import list_files_in_kunde_folder

    try:
        dateien = await list_files_in_kunde_folder(
            tenant_id, kunde_name, page_size=MAX_KOPIERTE_DATEIEN)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Archiv: Kundenordner nicht lesbar (egal): %s", exc)
        return 0

    kopiert = 0
    for f in dateien[:MAX_KOPIERTE_DATEIEN]:
        fid, name = f.get("id"), (f.get("name") or "Datei")
        if not fid or f.get("mime_type") == DRIVE_FOLDER_MIME:
            continue
        if _SKIP_NAMEN.match(name):
            continue

        def _sync_copy(file_id=fid, dateiname=name):
            return service.files().copy(
                fileId=file_id,
                body={"name": dateiname, "parents": [ziel_id]},
                fields="id",
            ).execute()

        try:
            await asyncio.to_thread(_sync_copy)
            kopiert += 1
        except Exception as exc:  # noqa: BLE001
            logger.info("Archiv: Datei '%s' nicht kopierbar: %s", name, exc)
    return kopiert


async def archiviere_auftrag(
    tenant_id: uuid.UUID,
    angebot_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Legt den Drive-Archivordner fuer einen abgeschlossenen Auftrag an.

    Idempotent: ist ``archiv_drive_folder_id`` schon gesetzt, passiert
    nichts. Returns ``{"ok": bool, "folder_url": str|None, "error": str|None}``
    — wirft nie.
    """
    try:
        from core.integrations.google_drive import (
            _ensure_root_folder, get_drive_service,
        )
        from core.models.tenant import Tenant

        async with get_session() as s:
            angebot = (await s.execute(
                select(Angebot)
                .where(Angebot.id == angebot_id, Angebot.tenant_id == tenant_id)
            )).scalar_one_or_none()
            if angebot is None:
                return {"ok": False, "folder_url": None, "error": "Auftrag nicht gefunden."}
            if angebot.archiv_drive_folder_id:
                return {"ok": True, "folder_url": angebot.archiv_drive_folder_url,
                        "error": None}
            tenant = (await s.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )).scalar_one_or_none()
            s.expunge(angebot)
            if tenant is not None:
                s.expunge(tenant)
        if tenant is None:
            return {"ok": False, "folder_url": None, "error": "Betrieb nicht gefunden."}

        service = await get_drive_service(tenant_id, employee_id)
        root_id = await _ensure_root_folder(service, tenant)
        archiv_id = await _finde_oder_erstelle_unterordner(
            service, root_id, ARCHIV_ORDNER_NAME)
        auftrag_ordner = _sicherer_ordnername(
            angebot.abgeschlossen_am, angebot.kunde_name)
        ordner_id = await _finde_oder_erstelle_unterordner(
            service, archiv_id, auftrag_ordner)

        def _sync_link(fid=ordner_id) -> str:
            meta = service.files().get(fileId=fid, fields="webViewLink").execute()
            return meta.get("webViewLink") or ""

        try:
            ordner_url = await asyncio.to_thread(_sync_link)
        except Exception:  # noqa: BLE001
            ordner_url = f"https://drive.google.com/drive/folders/{ordner_id}"

        # Ordner + Link SOFORT persistieren. Alles Weitere ist Beiwerk —
        # bricht der Upload ab, hat der Handwerker trotzdem seinen Ordner.
        async with get_session() as s:
            a = (await s.execute(
                select(Angebot).where(Angebot.id == angebot_id)
            )).scalar_one_or_none()
            if a is not None:
                a.archiv_drive_folder_id = ordner_id
                a.archiv_drive_folder_url = ordner_url
                await s.commit()

        await _befuelle_ordner(service, tenant_id, angebot, ordner_id)
        return {"ok": True, "folder_url": ordner_url, "error": None}

    except Exception as exc:  # noqa: BLE001
        logger.exception("Auftrags-Archivierung gescheitert: %s", exc)
        return {"ok": False, "folder_url": None, "error": str(exc)[:200]}


async def _befuelle_ordner(
    service, tenant_id: uuid.UUID, angebot: Angebot, ordner_id: str,
) -> None:
    """Uebersicht + Lexware-PDFs + Kundendateien in den Archivordner legen."""
    import io

    from googleapiclient.http import MediaIoBaseUpload

    def _sync_upload(daten: bytes, name: str, mime: str, ziel_mime: str | None):
        media = MediaIoBaseUpload(io.BytesIO(daten), mimetype=mime, resumable=False)
        body = {"name": name[:200], "parents": [ordner_id]}
        if ziel_mime:
            body["mimeType"] = ziel_mime
        return service.files().create(body=body, media_body=media, fields="id").execute()

    try:
        daten = await _sammle_uebersichtsdaten(tenant_id, angebot)
        html = _uebersicht_html(daten).encode("utf-8")
        await asyncio.to_thread(
            _sync_upload, html, "Auftragsuebersicht", "text/html", GOOGLE_DOC_MIME)
    except Exception as exc:  # noqa: BLE001
        logger.info("Archiv: Uebersicht nicht erstellbar: %s", exc)

    for name, pdf in await _lade_lexware_pdfs(tenant_id, angebot):
        try:
            await asyncio.to_thread(_sync_upload, pdf, name, "application/pdf", None)
        except Exception as exc:  # noqa: BLE001
            logger.info("Archiv: '%s' nicht hochladbar: %s", name, exc)

    await _kopiere_kundendateien(service, tenant_id, angebot.kunde_name, ordner_id)


def archiviere_im_hintergrund(tenant_id: uuid.UUID, angebot_id: uuid.UUID) -> None:
    """Feuert die Archivierung als Hintergrund-Task ab.

    Aufrufer ist der Rechnungs-Versand: der Handwerker soll nicht auf ein
    Dutzend Drive-Requests warten, und ein Drive-Problem darf die
    Erfolgsmeldung des Versands nicht in eine Fehlermeldung drehen.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("Archiv: kein laufender Event-Loop — uebersprungen.")
        return
    task = loop.create_task(archiviere_auftrag(tenant_id, angebot_id))
    # Referenz halten, damit der Task nicht wegge-gc-t wird.
    _HINTERGRUND_TASKS.add(task)
    task.add_done_callback(_HINTERGRUND_TASKS.discard)


_HINTERGRUND_TASKS: set = set()


__all__ = [
    "ARCHIV_ORDNER_NAME",
    "archiviere_auftrag",
    "archiviere_im_hintergrund",
]
