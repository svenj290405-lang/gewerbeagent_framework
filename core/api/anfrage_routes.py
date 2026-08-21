"""FastAPI-Routes fuer das oeffentliche Anfrage-Formular.

GET  /anfrage/{token}        -> rendert HTML-Formular
POST /anfrage/{token}/submit -> speichert Antworten + Push an den Betrieb

Wird von core/api/app.py via app.include_router() eingebunden.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core.integrations.anfrage_forms import (
    get_schema_for_tenant,
    get_token_with_tenant,
    submit_anfrage,
)
from core.integrations.anfrage_form_template import (
    render_already_submitted_page,
    render_anfrage_form_html,
    render_invalid_token_page,
    render_submit_error_page,
    render_success_page,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ----------------------------------------------------------------------
# Brute-Force-Schutz fuer Anfrage-Endpoints
# ----------------------------------------------------------------------
# In-Memory Rate-Limit pro IP. Reicht fuer Single-Container-Setup.
# Window 1h, ein Counter pro (ip, kind).
import datetime as _dt
from threading import Lock as _Lock
_ANFRAGE_HITS: dict[tuple[str, str], list[_dt.datetime]] = {}
_ANFRAGE_HITS_GUARD = _Lock()


# Obergrenzen fuer den oeffentlichen Submit. 3 Dateien x 5 MB plus Text und
# MIME-Overhead passen bequem in 18 MB; alles darueber ist kein Kunde mehr.
_SUBMIT_MAX_BODY_BYTES = 18 * 1024 * 1024
_SUBMIT_MAX_FORM_FIELDS = 80
# Zweite Bremse neben dem IP-Limit: selbst wenn die Anfragen aus vielen
# Netzen kommen (Botnetz, geleakter Link), soll ein einzelner Betrieb nicht
# mit Anfragen und Push-Meldungen zugeschuettet werden.
_SUBMIT_MAX_PRO_BETRIEB_H = 30


def _check_tenant_submit_limit(tenant_id, max_per_hour: int) -> bool:
    """Wie _check_anfrage_rate_limit, aber pro Betrieb statt pro IP."""
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=1)
    key = (f"tenant:{tenant_id}", "submit")
    with _ANFRAGE_HITS_GUARD:
        hits = [h for h in _ANFRAGE_HITS.get(key, []) if h >= cutoff]
        if len(hits) >= max_per_hour:
            _ANFRAGE_HITS[key] = hits
            logger.warning(
                "anfrage: Betriebs-Limit erreicht tenant=%s (%d/h)",
                tenant_id, max_per_hour)
            return False
        hits.append(now)
        _ANFRAGE_HITS[key] = hits
    return True


def _client_ip_anfrage(request: Request) -> str:
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.split(",")[0].strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _check_anfrage_rate_limit(
    request: Request, *, kind: str, max_per_hour: int,
) -> bool:
    """True wenn weiter erlaubt, False wenn Limit ueberschritten."""
    ip = _client_ip_anfrage(request)
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=1)
    key = (ip, kind)
    with _ANFRAGE_HITS_GUARD:
        hits = _ANFRAGE_HITS.get(key, [])
        # Alte Hits aussortieren
        hits = [h for h in hits if h >= cutoff]
        if len(hits) >= max_per_hour:
            _ANFRAGE_HITS[key] = hits
            logger.info(
                f"anfrage rate-limit hit kind={kind} ip={ip} "
                f"({len(hits)}/{max_per_hour}/h)"
            )
            return False
        hits.append(now)
        _ANFRAGE_HITS[key] = hits
        # Garbage Collection bei zu vielen Keys
        if len(_ANFRAGE_HITS) > 5000:
            _ANFRAGE_HITS.clear()
    return True


@router.get("/anfrage/{token}", response_class=HTMLResponse)
async def render_anfrage_form(token: str, request: Request):
    """Rendert das Anfrage-Formular fuer den gegebenen Token.

    Brute-Force-Schutz: max 60 GETs pro IP pro Stunde. Token-Bruteforce
    waere sonst unbemerkt moeglich, weil GET ohne Auth erfolgt.
    """
    if not _check_anfrage_rate_limit(request, kind="get", max_per_hour=60):
        return HTMLResponse(
            content="<h1>Zu viele Versuche</h1>"
                    "<p>Bitte einen Moment warten und dann erneut versuchen.</p>",
            status_code=429,
        )

    token_obj, tenant = await get_token_with_tenant(token)

    if token_obj is None:
        # Ungueltig oder abgelaufen
        return HTMLResponse(content=render_invalid_token_page(), status_code=404)

    if tenant is None:
        # Token gefunden, aber schon abgesendet
        return HTMLResponse(content=render_already_submitted_page(), status_code=200)

    schema = await get_schema_for_tenant(token_obj.tenant_id, token_obj.anfrage_typ)
    body = render_anfrage_form_html(
        schema=schema,
        token=token,
        company_name=tenant.company_name or "Dein Handwerker",
        branche=getattr(tenant, "branche", "") or "",
    )
    return HTMLResponse(content=body, status_code=200)


@router.get(
    "/anfrage/preview/{tenant_slug}/{anfrage_typ}",
    response_class=HTMLResponse,
)
async def render_anfrage_preview(
    tenant_slug: str, anfrage_typ: str, request: Request,
):
    """Vorschau des aktuellen Schemas — kein Token noetig.

    Aufgerufen aus der App (Formular-Werkstatt) damit der
    Handwerker sieht wie sein Web-Formular fuer Kunden aussieht.
    Submit ist im Preview-Modus deaktiviert.

    Rate-Limit identisch zum normalen Anfrage-GET (60/h pro IP).
    Tenant-Lookup via slug — wir leaken keine Token, nur die
    oeffentliche Schema-Struktur die der Tenant ohnehin via QR-Code
    teilt.
    """
    if not _check_anfrage_rate_limit(request, kind="preview", max_per_hour=60):
        return HTMLResponse(
            content="<h1>Zu viele Versuche</h1>"
                    "<p>Bitte einen Moment warten.</p>",
            status_code=429,
        )

    from sqlalchemy import select as _select
    from core.database import AsyncSessionLocal
    from core.models import Tenant

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            _select(Tenant).where(Tenant.slug == tenant_slug.lower())
        )).scalar_one_or_none()
    if tenant is None:
        return HTMLResponse(
            content=render_invalid_token_page(), status_code=404,
        )

    # Nur erlaubte anfrage-typen — sonst koennten User beliebige Strings
    # mitgeben und wir machen unnoetige Schema-Lookups.
    from core.models.anfrage import (
        ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN,
    )
    allowed = {ANFRAGE_TYP_TISCHLER, ANFRAGE_TYP_ALLGEMEIN}
    if anfrage_typ not in allowed:
        return HTMLResponse(
            content=render_invalid_token_page(), status_code=404,
        )

    schema = await get_schema_for_tenant(tenant.id, anfrage_typ)
    body = render_anfrage_form_html(
        schema=schema,
        token="preview",  # Placeholder — wird im Preview-Modus nicht
                         # benutzt (form action = javascript:void(0))
        company_name=tenant.company_name or "Dein Handwerker",
        branche=getattr(tenant, "branche", "") or "",
        preview_mode=True,
    )
    return HTMLResponse(content=body, status_code=200)


@router.post("/anfrage/{token}/submit")
async def submit_anfrage_form(token: str, request: Request):
    """Verarbeitet das abgesendete Formular.

    Unterstuetzt jetzt File-Uploads als multipart/form-data:
    - Bilder (jpeg/png/webp/heic) und PDFs
    - max 5MB pro Datei, max 3 Dateien pro Anfrage
    - Files werden base64-encoded in antworten[<field>] = [{filename,
      content_type, size, base64}, ...] gespeichert

    Brute-Force-Schutz: max 10 Submits pro IP pro Stunde.
    """
    if not _check_anfrage_rate_limit(request, kind="submit", max_per_hour=10):
        return HTMLResponse(
            content="<h1>Zu viele Anfragen</h1>"
                    "<p>Bitte einen Moment warten.</p>",
            status_code=429,
        )
    from starlette.datastructures import UploadFile as _UploadFile
    from core.integrations.anfrage_forms import (
        ANFRAGE_FILE_MAX_BYTES,
        ANFRAGE_FILE_MAX_COUNT,
        ANFRAGE_FILE_ALLOWED_MIME,
        HONEYPOT_FIELD,
        filter_antworten_gegen_schema,
        verify_magic_bytes,
        zeitstempel_plausibel,
    )
    import base64 as _b64

    # Groesse begrenzen, BEVOR irgendwas geparst wird. Vorher las Starlette
    # erst den kompletten Body (und lagerte ihn auf Platte aus), und erst
    # danach griffen die Per-Datei-Limits — ein einzelner POST durfte also
    # beliebig gross sein.
    content_length = request.headers.get("content-length")
    if content_length is None:
        # Browser senden bei Formularen immer Content-Length. Fehlt sie
        # (chunked), koennten wir die Groesse vorher nicht kennen.
        return HTMLResponse(
            content=render_submit_error_page("Ungültige Anfrage."),
            status_code=411,
        )
    try:
        if int(content_length) > _SUBMIT_MAX_BODY_BYTES:
            logger.info(
                "submit_anfrage: Body zu gross (%s bytes) ip=%s",
                content_length, _client_ip_anfrage(request),
            )
            return HTMLResponse(
                content=render_submit_error_page(
                    "Die Anfrage ist zu groß. Bitte weniger oder kleinere "
                    "Dateien anhängen (max. 5 MB pro Datei)."),
                status_code=413,
            )
    except ValueError:
        return HTMLResponse(
            content=render_submit_error_page("Ungültige Anfrage."),
            status_code=400)

    # Auch der Parser bekommt harte Grenzen (Feldzahl, Dateien, Teilgroesse).
    try:
        form_data = await request.form(
            max_files=ANFRAGE_FILE_MAX_COUNT + 2,
            max_fields=_SUBMIT_MAX_FORM_FIELDS,
            max_part_size=ANFRAGE_FILE_MAX_BYTES,
        )
    except Exception as e:  # noqa: BLE001 — Starlette wirft bei Limit-Bruch
        logger.info("submit_anfrage: Formular abgewiesen (%s) ip=%s",
                    type(e).__name__, _client_ip_anfrage(request))
        return HTMLResponse(
            content=render_submit_error_page(
                "Die Anfrage konnte nicht verarbeitet werden. Bitte mit "
                "weniger Angaben erneut versuchen."),
            status_code=413,
        )
    antworten: dict = {}
    file_count_total = 0

    for key, value in form_data.multi_items():
        # Ist das eine hochgeladene Datei?
        if isinstance(value, _UploadFile):
            if file_count_total >= ANFRAGE_FILE_MAX_COUNT:
                logger.info(
                    f"submit_anfrage: max {ANFRAGE_FILE_MAX_COUNT} Files "
                    f"erreicht, weitere ignoriert"
                )
                continue
            ct = (value.content_type or "").lower()
            if ct not in ANFRAGE_FILE_ALLOWED_MIME:
                logger.info(
                    f"submit_anfrage: skip File mit content_type={ct!r}"
                )
                continue
            raw = await value.read()
            if len(raw) > ANFRAGE_FILE_MAX_BYTES:
                logger.info(
                    f"submit_anfrage: skip File {value.filename!r} - "
                    f"{len(raw)} bytes > {ANFRAGE_FILE_MAX_BYTES}"
                )
                continue
            # Phase B8: Magic-Bytes-Check — Angreifer der content-type-
            # Header faelscht (z.B. ".exe" mit content_type="image/jpeg")
            # wird hier geblockt.
            if not verify_magic_bytes(raw, claimed_content_type=ct):
                logger.warning(
                    f"submit_anfrage: magic-bytes mismatch fuer "
                    f"{value.filename!r} (claimed={ct}) — verworfen"
                )
                continue
            file_obj = {
                "filename": (value.filename or "datei")[:200],
                "content_type": ct,
                "size": len(raw),
                "base64": _b64.b64encode(raw).decode("ascii"),
            }
            base = key[:-2] if key.endswith("[]") else key
            antworten.setdefault(base, []).append(file_obj)
            file_count_total += 1
            continue

        # Text-Eintraege wie bisher
        if key.endswith("[]"):
            base = key[:-2]
            antworten.setdefault(base, []).append(value)
        else:
            if key in antworten:
                if isinstance(antworten[key], list):
                    antworten[key].append(value)
                else:
                    antworten[key] = [antworten[key], value]
            else:
                antworten[key] = value

    # ---- Bot-Bremse ---------------------------------------------------
    # Honeypot: ein unsichtbares Feld, das nur ein Skript ausfuellt. Wir
    # antworten mit der normalen Erfolgsseite — ein Bot soll nicht lernen,
    # woran er gescheitert ist. Gespeichert wird nichts.
    if (antworten.get(HONEYPOT_FIELD) or "").strip():
        logger.warning(
            "submit_anfrage: Honeypot ausgefuellt (Bot) ip=%s token=%s…",
            _client_ip_anfrage(request), token[:10])
        return HTMLResponse(content=render_success_page(), status_code=200)

    # Zeitfalle: unter 3 Sekunden fuellt kein Mensch ein mehrstufiges
    # Formular aus. Fehlt der Zeitstempel (Formular von vor dieser
    # Aenderung), gilt er als in Ordnung.
    if not zeitstempel_plausibel(antworten.get("_ts") or ""):
        logger.warning(
            "submit_anfrage: Zeitstempel unplausibel ip=%s token=%s…",
            _client_ip_anfrage(request), token[:10])
        return HTMLResponse(content=render_success_page(), status_code=200)

    # ---- Nur Felder, die es im Schema wirklich gibt ---------------------
    # Vorher landete JEDER mitgeschickte Schluessel in der DB und (unge-
    # kuerzt) im Prompt der automatischen Mail-Antwort.
    token_obj, _tenant = await get_token_with_tenant(token)
    if token_obj is None:
        return HTMLResponse(
            content=render_submit_error_page("Der Link ist nicht mehr gültig."),
            status_code=400)

    if not _check_tenant_submit_limit(
            token_obj.tenant_id, _SUBMIT_MAX_PRO_BETRIEB_H):
        return HTMLResponse(
            content="<h1>Zu viele Anfragen</h1>"
                    "<p>Bitte später erneut versuchen.</p>",
            status_code=429,
        )

    schema = await get_schema_for_tenant(
        token_obj.tenant_id, token_obj.anfrage_typ)
    antworten, verworfen = filter_antworten_gegen_schema(schema, antworten)
    if verworfen:
        logger.warning(
            "submit_anfrage: %d unbekannte/zu grosse Felder verworfen "
            "(token=%s… ip=%s): %s",
            len(verworfen), token[:10], _client_ip_anfrage(request),
            verworfen[:10])

    # DSGVO: Einwilligung ist Pflicht (Art. 6/7). Der Browser sendet
    # `_consent` nur, wenn die Checkbox angehakt ist — fehlt sie, brechen
    # wir serverseitig ab (clientseitige Pruefung allein ist umgehbar).
    if not antworten.get("_consent"):
        return HTMLResponse(
            content=render_submit_error_page(
                "Bitte bestätige die Datenschutz-Einwilligung, um die "
                "Anfrage abzusenden."
            ),
            status_code=400,
        )

    submitted_ip = request.client.host if request.client else None

    success, message = await submit_anfrage(
        token_str=token,
        antworten=antworten,
        submitted_ip=submitted_ip,
    )

    if not success:
        return HTMLResponse(
            content=render_submit_error_page(message), status_code=400
        )

    # Push an den Betrieb (nicht blockierend)
    try:
        from core.integrations.anfrage_eingang import notify_tenant_anfrage_submitted
        await notify_tenant_anfrage_submitted(token_str=token, antworten=antworten)
    except Exception as e:
        logger.warning(f"Anfrage-Push fehler (non-fatal): {e}")

    return HTMLResponse(content=render_success_page(), status_code=200)
