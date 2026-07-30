"""Freie E-Mails aus dem Assistenten heraus verfassen und verschicken.

Der Handwerker sagt Q "schreib der Frau Meier, dass wir Donnerstag doch
erst um 10 kommen"; Q formuliert Empfaenger, Betreff und Text (optional
mit Anhang), die App zeigt den Entwurf zum Redigieren an, und erst nach
ausdruecklicher Freigabe geht die Mail raus. Dieses Modul haelt alles,
was dafuer serverseitig noetig ist: Adress-Aufloesung ueber den
Kundenstamm, Anhang-Aufloesung (Drive-Archiv oder direkt hochgeladene
Datei), Body-Rendering und den Versand.

Verschickt wird ueber genau denselben Graph-Pfad (``send_tracked_mail``),
den auch die Anfrage-Antwort nutzt — die Mail landet also im Postfach des
Betriebs unter "Gesendet" und ein Reply des Kunden kommt ueber das
normale Inbox-Polling zurueck. Ein eigener Versandweg waere eine zweite
Wahrheit; das wollen wir nicht.
"""
from __future__ import annotations

import base64
import binascii
import html as _html
import logging
import re
import uuid

logger = logging.getLogger(__name__)

# Empfaenger-Adresse: bewusst pragmatisch (kein RFC-5322-Parser), reicht
# um Tippfehler und leere Felder abzufangen. Der Mailserver ist die
# eigentliche Instanz, die eine Adresse akzeptiert oder ablehnt.
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")

# Drive-Datei-IDs (gleiche Pruefung wie im Archiv-Proxy in app_screens).
DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{10,200}$")

MAX_BETREFF = 200
MAX_TEXT = 8000
MAX_ANHAENGE = 5
# Microsoft Graph nimmt Anhaenge bis 3 MB direkt am Draft entgegen;
# darueber braucht es eine Upload-Session, die wir hier nicht fahren.
MAX_ANHANG_BYTES = 3 * 1024 * 1024
MAX_ANHAENGE_GESAMT_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Empfaenger
# ---------------------------------------------------------------------------

async def lookup_kunde_email(tid: uuid.UUID, kunde_name: str) -> str | None:
    """Sucht die Mail-Adresse eines Kunden im Kundenstamm.

    Nur bei genau einem Treffer mit hinterlegter Adresse — bei mehreren
    passenden Kunden wird nichts geraten, dann muss der Nutzer die
    Adresse im Entwurf selbst eintragen.
    """
    name = (kunde_name or "").strip()
    if len(name) < 2:
        return None
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.kunde import Kunde

    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(Kunde)
                .where(Kunde.tenant_id == tid)
                .where(Kunde.name.ilike(f"%{name}%"))
                .where(Kunde.merged_into_id.is_(None))
                .where(Kunde.email.is_not(None))
                .limit(2)
            )).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("lookup_kunde_email fehlgeschlagen (tenant=%s): %s", tid, exc)
        return None
    if len(rows) != 1:
        return None
    return (rows[0].email or "").strip() or None


# ---------------------------------------------------------------------------
# Anhaenge
# ---------------------------------------------------------------------------

def normalize_anhaenge(raw) -> list[dict]:
    """Bringt die Anhang-Angaben aus Gemini-Args oder App-Payload auf eine
    Form: ``{"quelle": "drive"|"upload", "id"|"b64", "name", "mime"}``.

    Toleriert die schmale Gemini-Variante (nur Drive-Datei-IDs als
    Strings) genauso wie das reiche Format, das die App beim Senden
    zurueckschickt. Unbrauchbare Eintraege fallen still raus — ein
    kaputter Anhang darf nie den ganzen Versand kippen.
    """
    if not raw:
        return []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for item in raw[:MAX_ANHAENGE]:
        if isinstance(item, str):
            fid = item.strip()
            if DRIVE_FILE_ID_RE.match(fid):
                out.append({"quelle": "drive", "id": fid, "name": ""})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:200]
        b64 = item.get("b64")
        if b64:
            out.append({
                "quelle": "upload",
                "name": name or "anhang",
                "mime": str(item.get("mime") or "application/octet-stream")[:100],
                "b64": str(b64),
            })
            continue
        fid = str(item.get("id") or item.get("drive_file_id") or "").strip()
        if DRIVE_FILE_ID_RE.match(fid):
            out.append({"quelle": "drive", "id": fid, "name": name})
    return out


def _mit_endung(name: str, mime: str) -> str:
    """Ergaenzt eine fehlende Datei-Endung aus dem MIME-Typ — sonst zeigt
    das Mailprogramm des Kunden einen Anhang, den er nicht oeffnen kann."""
    import mimetypes
    if "." in name.strip(".") or not mime:
        return name
    ext = mimetypes.guess_extension(mime.split(";")[0].strip())
    return f"{name}{ext}" if ext else name


async def load_anhaenge(
    tid: uuid.UUID, anhaenge: list[dict], employee_id: uuid.UUID | None = None,
) -> tuple[list[dict], list[str]]:
    """Laedt die Anhang-Bytes fuer den Versand.

    Drive-Anhaenge werden ueber den tenant-gescopeten Drive-Service
    geholt, hochgeladene Dateien aus ihrem Base64 dekodiert. Rueckgabe:
    (Graph-Attachment-Dicts, Fehlermeldungen). Ein fehlerhafter Anhang
    bricht den Versand ab (der Nutzer hat ihn bewusst drangehaengt) —
    darum die Fehlerliste statt eines stillen Weglassens.
    """
    fertig: list[dict] = []
    fehler: list[str] = []
    gesamt = 0
    for a in anhaenge[:MAX_ANHAENGE]:
        name = a.get("name") or "anhang"
        try:
            if a.get("quelle") == "upload":
                try:
                    data = base64.b64decode(a.get("b64") or "", validate=True)
                except (binascii.Error, ValueError):
                    fehler.append(f"„{name}\" konnte nicht gelesen werden.")
                    continue
                mime = a.get("mime") or "application/octet-stream"
            else:
                from core.integrations.google_drive import get_file_bytes
                data, mime = await get_file_bytes(tid, a["id"], employee_id)
        except ValueError:
            fehler.append("Google Drive ist nicht verbunden — Anhang nicht ladbar.")
            continue
        except RuntimeError as exc:
            fehler.append(f"„{name}\": {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("Anhang laden fehlgeschlagen (tenant=%s): %s", tid, exc)
            fehler.append(f"„{name}\" konnte nicht geladen werden.")
            continue

        if not data:
            fehler.append(f"„{name}\" ist leer.")
            continue
        if len(data) > MAX_ANHANG_BYTES:
            fehler.append(f"„{name}\" ist zu gross (max 3 MB pro Anhang).")
            continue
        gesamt += len(data)
        if gesamt > MAX_ANHAENGE_GESAMT_BYTES:
            fehler.append("Die Anhaenge sind zusammen zu gross (max 8 MB).")
            break
        fertig.append({"filename": _mit_endung(name, mime), "bytes": data,
                       "content_type": mime})
    return fertig, fehler


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------

def build_mail_html(
    text: str, *, company_name: str = "", contact_name: str = "",
    contact_email: str = "", contact_phone: str = "",
) -> str:
    """Rendert den freien Text in dieselbe Apple-clean Karte, die auch
    Angebots- und Rechnungsmails nutzen.

    Der Text kommt vom Nutzer bzw. von Q und wird komplett escaped —
    Absaetze (Leerzeile) werden zu ``<p>``, einfache Umbrueche zu
    ``<br>``. Absender-Kontakt steht im Footer, damit der Empfaenger
    sieht, von wem die Mail wirklich kommt.
    """
    absaetze = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    body = "".join(
        '<p style="margin:0 0 16px;">'
        + _html.escape(p).replace("\n", "<br>")
        + "</p>"
        for p in absaetze
    ) or '<p style="margin:0 0 16px;"></p>'

    kontakt = []
    if contact_name:
        kontakt.append(_html.escape(contact_name))
    if contact_email:
        kontakt.append(
            f'<a href="mailto:{_html.escape(contact_email)}" style="color:#1d1d1f;">'
            f'{_html.escape(contact_email)}</a>'
        )
    if contact_phone:
        kontakt.append(_html.escape(contact_phone))
    kontakt_html = "<br>".join(kontakt)
    footer = (
        f'<p style="margin:24px 0 0;color:#86868b;font-size:14px;">{kontakt_html}</p>'
        if kontakt_html else ""
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;color:#1d1d1f;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;padding:32px 0;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:14px;padding:36px 40px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.04);">
<tr><td style="font-size:17px;line-height:1.5;">
{body}
{footer}
</td></tr>
</table>
<p style="font-size:12px;color:#86868b;margin:20px 0 0;text-align:center;">
{_html.escape(company_name or "")}
</p>
</td></tr>
</table>
</body></html>"""


def build_mail_text(
    text: str, *, contact_name: str = "", contact_email: str = "",
    contact_phone: str = "",
) -> str:
    """Plain-Text-Variante fuer multipart/alternative (Clients ohne HTML
    und beim Zitieren)."""
    zeilen = [(text or "").strip()]
    kontakt = [x for x in (contact_name, contact_email, contact_phone) if x]
    if kontakt:
        zeilen.append("\n".join(kontakt))
    return "\n\n".join(z for z in zeilen if z)


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------

async def send_freie_mail(
    tid: uuid.UUID, *, to_email: str, betreff: str, text: str,
    to_name: str | None = None, anhaenge: list[dict] | None = None,
    employee_id: uuid.UUID | None = None,
) -> dict:
    """Verschickt eine frei formulierte Mail an einen Empfaenger.

    Wird ausschliesslich nach Freigabe des Nutzers aufgerufen (Write-Tool
    des Assistenten). Rueckgabe wie die uebrigen Write-Tools:
    ``{"ok": bool, ...}`` — nie eine Exception nach oben.
    """
    to_email = (to_email or "").strip()
    betreff = (betreff or "").strip()[:MAX_BETREFF]
    text = (text or "").strip()[:MAX_TEXT]

    if not EMAIL_RE.match(to_email):
        return {"ok": False, "error": "Bitte eine gueltige Empfaenger-Adresse eintragen."}
    if len(betreff) < 2:
        return {"ok": False, "error": "Der Betreff fehlt."}
    if len(text) < 2:
        return {"ok": False, "error": "Der Mail-Text fehlt."}

    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.tenant import Tenant

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.id == tid))).scalar_one_or_none()
    if tenant is None:
        return {"ok": False, "error": "Betrieb nicht gefunden."}

    graph_anhaenge: list[dict] = []
    if anhaenge:
        graph_anhaenge, fehler = await load_anhaenge(tid, anhaenge, employee_id)
        if fehler:
            return {"ok": False, "error": " ".join(fehler)}

    contact_name = getattr(tenant, "contact_name", "") or ""
    contact_email = getattr(tenant, "contact_email", "") or ""
    contact_phone = getattr(tenant, "contact_phone", "") or ""
    body_html = build_mail_html(
        text, company_name=tenant.company_name or "",
        contact_name=contact_name, contact_email=contact_email,
        contact_phone=contact_phone)
    body_text = build_mail_text(
        text, contact_name=contact_name, contact_email=contact_email,
        contact_phone=contact_phone)

    from core.integrations.microsoft import send_tracked_mail

    try:
        res = await send_tracked_mail(
            tenant_id=tid, to_email=to_email, subject=betreff,
            body_html=body_html, body_text=body_text,
            attachments=graph_anhaenge or None, employee_id=employee_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_freie_mail crash (tenant=%s): %s", tid, exc)
        return {"ok": False, "error": "Mail-Versand fehlgeschlagen."}

    if not res.get("success"):
        fehler = res.get("error") or "Mail-Versand fehlgeschlagen."
        if "nicht verbunden" in fehler.lower():
            fehler = ("Microsoft/Outlook ist nicht verbunden — bitte unter "
                      "Mehr → Verbindungen einrichten.")
        return {"ok": False, "error": fehler}

    logger.info(
        "send_freie_mail OK: tenant=%s to=%s betreff=%r anhaenge=%d",
        tid, to_email, betreff[:60], len(graph_anhaenge),
    )
    return {
        "ok": True,
        "to_email": to_email,
        "empfaenger": (to_name or "").strip() or to_email,
        "betreff": betreff,
        "anhaenge": len(graph_anhaenge),
        "internet_message_id": res.get("internet_message_id"),
    }
