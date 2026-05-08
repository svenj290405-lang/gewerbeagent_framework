"""FastAPI-Routes fuer das oeffentliche Anfrage-Formular.

GET  /anfrage/{token}        -> rendert HTML-Formular
POST /anfrage/{token}/submit -> speichert Antworten + Telegram-Push

Wird von core/api/app.py via app.include_router() eingebunden.
"""
from __future__ import annotations

import html as _html
import json
import logging
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.integrations.anfrage_forms import (
    get_schema,
    get_schema_for_tenant,
    get_token_with_tenant,
    submit_anfrage,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# =====================================================================
# HTML-Templates (inline, kein Jinja2 noetig)
# =====================================================================

HTML_HEADER = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  .field-error {{ color: #dc2626; font-size: 0.875rem; margin-top: 0.25rem; }}
</style>
</head>
<body class="bg-slate-50 min-h-screen">
<div class="max-w-2xl mx-auto px-4 py-8 md:py-12">
"""

HTML_FOOTER = """
<footer class="mt-12 text-center text-xs text-slate-400">
  Powered by Gewerbeagent &middot; Deine Daten werden DSGVO-konform verarbeitet.
</footer>
</div>
</body>
</html>
"""


def _render_field(field: dict) -> str:
    """Rendert ein einzelnes Formular-Feld als HTML."""
    name = _html.escape(field["name"])
    label = _html.escape(field["label"])
    required_attr = "required" if field.get("required") else ""
    required_mark = ' <span class="text-red-500">*</span>' if field.get("required") else ""
    placeholder = _html.escape(field.get("placeholder", ""))
    ftype = field.get("type", "text")

    label_html = f'<label class="block font-medium text-slate-700 mb-2">{label}{required_mark}</label>'

    if ftype == "text":
        body = (
            f'<input type="text" name="{name}" placeholder="{placeholder}" {required_attr} '
            f'class="w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 focus:outline-none">'
        )
    elif ftype == "tel":
        body = (
            f'<input type="tel" name="{name}" placeholder="{placeholder}" {required_attr} '
            f'class="w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 focus:outline-none">'
        )
    elif ftype == "date":
        body = (
            f'<input type="date" name="{name}" {required_attr} '
            f'class="w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 focus:outline-none">'
        )
    elif ftype == "textarea":
        body = (
            f'<textarea name="{name}" rows="3" placeholder="{placeholder}" {required_attr} '
            f'class="w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 focus:outline-none"></textarea>'
        )
    elif ftype == "radio":
        opts = []
        for opt in field.get("options", []):
            opt_e = _html.escape(opt)
            opts.append(
                f'<label class="flex items-center gap-2 py-1 cursor-pointer">'
                f'<input type="radio" name="{name}" value="{opt_e}" {required_attr} '
                f'class="text-blue-600 focus:ring-blue-500"> '
                f'<span>{opt_e}</span></label>'
            )
        body = '<div class="space-y-1">' + "".join(opts) + "</div>"
    elif ftype == "checkbox_multi":
        opts = []
        for opt in field.get("options", []):
            opt_e = _html.escape(opt)
            opts.append(
                f'<label class="flex items-center gap-2 py-1 cursor-pointer">'
                f'<input type="checkbox" name="{name}[]" value="{opt_e}" '
                f'class="text-blue-600 focus:ring-blue-500"> '
                f'<span>{opt_e}</span></label>'
            )
        body = '<div class="space-y-1">' + "".join(opts) + "</div>"
    elif ftype == "select":
        opts_html = '<option value="">Bitte waehlen</option>'
        for opt in field.get("options", []):
            opt_e = _html.escape(opt)
            opts_html += f'<option value="{opt_e}">{opt_e}</option>'
        body = (
            f'<select name="{name}" {required_attr} '
            f'class="w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-blue-500 focus:ring-2 focus:ring-blue-200 focus:outline-none">'
            f"{opts_html}</select>"
        )
    elif ftype == "masse":
        # Spezial-Feld: 3 Zahlen-Inputs nebeneinander
        body = (
            '<div class="grid grid-cols-3 gap-2">'
            f'<input type="number" name="{name}_hoehe" placeholder="Hoehe" '
            'class="rounded-lg border border-slate-300 px-3 py-2">'
            f'<input type="number" name="{name}_breite" placeholder="Breite" '
            'class="rounded-lg border border-slate-300 px-3 py-2">'
            f'<input type="number" name="{name}_tiefe" placeholder="Tiefe" '
            'class="rounded-lg border border-slate-300 px-3 py-2">'
            '</div>'
        )
    else:
        body = f'<!-- unbekannter Feldtyp: {ftype} -->'

    return f'<div class="mb-5">{label_html}{body}</div>'


@router.get("/anfrage/{token}", response_class=HTMLResponse)
async def render_anfrage_form(token: str):
    """Rendert das Anfrage-Formular fuer den gegebenen Token."""
    token_obj, tenant = await get_token_with_tenant(token)

    if token_obj is None:
        # Ungueltig oder abgelaufen
        return HTMLResponse(
            content=HTML_HEADER.format(title="Link ungueltig") + """
            <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-8 text-center">
              <h1 class="text-2xl font-semibold text-slate-800 mb-3">Link ungueltig oder abgelaufen</h1>
              <p class="text-slate-600">Bitte wende dich an den Absender, dann bekommst du einen neuen Link.</p>
            </div>
            """ + HTML_FOOTER,
            status_code=404,
        )

    if tenant is None:
        # Token gefunden, aber schon abgesendet
        return HTMLResponse(
            content=HTML_HEADER.format(title="Schon abgesendet") + """
            <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-8 text-center">
              <h1 class="text-2xl font-semibold text-slate-800 mb-3">Anfrage schon abgesendet</h1>
              <p class="text-slate-600">Du hast diese Anfrage bereits ausgefuellt. Wir melden uns in Kuerze!</p>
            </div>
            """ + HTML_FOOTER,
            status_code=200,
        )

    schema = await get_schema_for_tenant(token_obj.tenant_id, token_obj.anfrage_typ)
    fields_html = "".join(_render_field(f) for f in schema["fields"])

    company = _html.escape(tenant.company_name or "Dein Handwerker")
    title = _html.escape(schema["title"])
    subtitle = _html.escape(schema["subtitle"])
    kunde_name_display = _html.escape(token_obj.kunde_name) if token_obj.kunde_name else ""
    greeting = (
        f"Hallo {kunde_name_display}," if kunde_name_display else "Hallo,"
    )

    body = HTML_HEADER.format(title=f"{title} - {company}") + f"""
    <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
      <div class="bg-gradient-to-r from-blue-600 to-indigo-600 px-6 py-5 text-white">
        <div class="text-sm opacity-90">{company}</div>
        <h1 class="text-2xl font-semibold mt-1">{title}</h1>
        <p class="opacity-90 mt-1">{subtitle}</p>
      </div>
      <form method="POST" action="/anfrage/{_html.escape(token)}/submit" class="p-6 md:p-8">
        <p class="text-slate-700 mb-6">{greeting} bitte fuelle die Felder aus, dann melden wir uns mit einem konkreten Angebot.</p>
        {fields_html}
        <button type="submit"
                class="w-full bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white font-medium py-3 rounded-lg transition shadow-sm">
          Anfrage absenden
        </button>
        <p class="text-xs text-slate-400 mt-3 text-center">
          Mit dem Absenden stimmst du der Verarbeitung deiner Daten zu.
        </p>
      </form>
    </div>
    """ + HTML_FOOTER

    return HTMLResponse(content=body, status_code=200)


@router.post("/anfrage/{token}/submit")
async def submit_anfrage_form(token: str, request: Request):
    """Verarbeitet das abgesendete Formular."""
    form_data = await request.form()
    # multi-select kommt mit []-Suffix; sammeln zu Listen
    antworten: dict = {}
    for key, value in form_data.multi_items():
        if key.endswith("[]"):
            base = key[:-2]
            antworten.setdefault(base, []).append(value)
        else:
            # Wenn schon vorhanden -> Liste
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
            content=HTML_HEADER.format(title="Fehler") + f"""
            <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-8 text-center">
              <h1 class="text-2xl font-semibold text-red-600 mb-3">Hmm, das hat nicht geklappt</h1>
              <p class="text-slate-600">{_html.escape(message)}</p>
            </div>
            """ + HTML_FOOTER,
            status_code=400,
        )

    # Telegram-Push (nicht blockierend)
    try:
        from core.integrations.anfrage_telegram import notify_tenant_anfrage_submitted
        await notify_tenant_anfrage_submitted(token_str=token, antworten=antworten)
    except Exception as e:
        logger.warning(f"Telegram-Push fehler (non-fatal): {e}")

    return HTMLResponse(
        content=HTML_HEADER.format(title="Vielen Dank!") + """
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 p-8 text-center">
          <div class="text-6xl mb-4">✅</div>
          <h1 class="text-2xl font-semibold text-slate-800 mb-3">Vielen Dank!</h1>
          <p class="text-slate-600 mb-2">Deine Anfrage wurde erfolgreich abgesendet.</p>
          <p class="text-slate-600">Wir melden uns in Kuerze mit einem konkreten Angebot.</p>
        </div>
        """ + HTML_FOOTER,
        status_code=200,
    )
