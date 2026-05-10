"""FastAPI-Routes fuer das oeffentliche Anfrage-Formular.

GET  /anfrage/{token}        -> rendert HTML-Formular
POST /anfrage/{token}/submit -> speichert Antworten + Telegram-Push

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


@router.get("/anfrage/{token}", response_class=HTMLResponse)
async def render_anfrage_form(token: str):
    """Rendert das Anfrage-Formular fuer den gegebenen Token."""
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


@router.post("/anfrage/{token}/submit")
async def submit_anfrage_form(token: str, request: Request):
    """Verarbeitet das abgesendete Formular.

    Unterstuetzt jetzt File-Uploads als multipart/form-data:
    - Bilder (jpeg/png/webp/heic) und PDFs
    - max 5MB pro Datei, max 3 Dateien pro Anfrage
    - Files werden base64-encoded in antworten[<field>] = [{filename,
      content_type, size, base64}, ...] gespeichert
    """
    from starlette.datastructures import UploadFile as _UploadFile
    from core.integrations.anfrage_forms import (
        ANFRAGE_FILE_MAX_BYTES,
        ANFRAGE_FILE_MAX_COUNT,
        ANFRAGE_FILE_ALLOWED_MIME,
    )
    import base64 as _b64

    form_data = await request.form()
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

    # Telegram-Push (nicht blockierend)
    try:
        from core.integrations.anfrage_telegram import notify_tenant_anfrage_submitted
        await notify_tenant_anfrage_submitted(token_str=token, antworten=antworten)
    except Exception as e:
        logger.warning(f"Telegram-Push fehler (non-fatal): {e}")

    return HTMLResponse(content=render_success_page(), status_code=200)
