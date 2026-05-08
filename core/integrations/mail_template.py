"""HTML-Mail-Template fuer professionelle Kunden-Antworten.

Nutzt Tabellen-Layout (E-Mail-Standard, kompatibel mit Outlook + Gmail
+ Apple Mail) statt flexbox/grid. Inline-CSS weil die meisten Mail-
Clients <style>-Tags ignorieren oder strippen.

Workflow:
- build_kunde_reply_html(kontext) -> str (komplettes HTML)
- extract_first_name(name_or_email) -> str (z.B. "Sven Jantos" -> "Sven")
"""
from __future__ import annotations

import html as _html
import re as _re
from typing import Optional


def extract_first_name(name_or_email: str) -> str:
    """Extrahiert den Vornamen aus '<Vorname Nachname>' oder fallback auf Mail-Lokalteil.

    Beispiele:
    - "Sven Jantos" -> "Sven"
    - "Dr. Max Müller" -> "Max"
    - "maria.mueller@example.com" -> "Maria"
    - "info@firma.de" -> "" (leer)
    """
    if not name_or_email:
        return ""

    name = name_or_email.strip()

    # Falls noch '<email@x>' im Namen drin: rauschmeissen
    name = _re.sub(r"\s*<[^>]+>", "", name).strip()

    # Wenn ohne @ und mit Leerzeichen -> Anrede-Name
    if "@" not in name and " " in name:
        parts = [p for p in name.split() if not p.endswith(".")]
        if parts:
            return parts[0]

    # Sonst Mail-Lokalteil als Fallback
    if "@" in name:
        local = name.split("@")[0]
        # Generische Adressen wie info@, mail@, hello@ -> leer zurueckgeben
        if local.lower() in {"info", "kontakt", "mail", "hello", "noreply", "no-reply", "service"}:
            return ""
        # "vorname.nachname" oder "vorname_nachname"
        for sep in (".", "_", "-"):
            if sep in local:
                first = local.split(sep)[0]
                return first.capitalize()
        return local.capitalize()

    # Einzelner Name ohne Leerzeichen
    return name.split()[0] if name else ""


def build_kunde_reply_html(
    kunde_anrede_name: str,
    kunde_email: str,
    reply_text: str,
    form_url: str,
    company_name: str,
    contact_name: str,
    contact_email: str = "",
    contact_phone: str = "",
    contact_website: str = "",
) -> str:
    """Baut das komplette Mail-HTML mit Header, Body, Button, Footer.

    Args:
        kunde_anrede_name: Vorname des Kunden, z.B. "Sven"
        kunde_email: Mail des Kunden (fuer Disclaimer)
        reply_text: KI-generierter Antwort-Text (Plain-Text mit \\n)
        form_url: URL zum Anfrage-Formular
        company_name: Name des Tenants, z.B. "PURA Tischler"
        contact_name: Inhaber-Name, z.B. "Daniel Tombers"
        contact_email: optional, fuer Footer
        contact_phone: optional, fuer Footer
        contact_website: optional, fuer Footer
    """
    # Anrede generieren
    if kunde_anrede_name:
        greeting = f"Hallo {_html.escape(kunde_anrede_name)},"
    else:
        greeting = "Hallo,"

    # Reply-Text -> HTML (paragraphs aus Doppel-Newlines, br aus Einzel-Newlines)
    # Erste Zeile koennte schon eine Anrede vom Gemini sein - dann skippen
    text = (reply_text or "").strip()
    # Falls Gemini eine Anrede generiert hat (Hallo X) - rausschneiden weil wir
    # eigene haben
    text_lines = text.split("\n")
    if text_lines and _re.match(r"^(hallo|hi|sehr geehrte|guten tag)", text_lines[0].strip().lower()):
        text_lines = text_lines[1:]
        # Auch eventuell leere Zeile danach
        while text_lines and not text_lines[0].strip():
            text_lines = text_lines[1:]
    # Falls Gemini eine Signatur am Ende hat - rausschneiden weil wir eigene haben
    while text_lines and _re.match(
        r"^(viele gruesse|mit freundlichen|beste gruesse|liebe gruesse|gruesse|gruss|mfg|lg)",
        text_lines[-1].strip().lower(),
    ):
        text_lines = text_lines[:-1]
    # Letzte Zeile koennte Name "Daniel (via Q)" sein - auch raus
    while text_lines and "(via q)" in text_lines[-1].lower():
        text_lines = text_lines[:-1]
    # Trailing-Leere entfernen
    while text_lines and not text_lines[-1].strip():
        text_lines = text_lines[:-1]

    cleaned = "\n".join(text_lines)

    # Zu HTML
    paragraphs = []
    for block in cleaned.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        # In Block: <br> bei einzelnen \n
        block_html = _html.escape(block).replace("\n", "<br>")
        paragraphs.append(f'<p style="margin: 0 0 14px 0;">{block_html}</p>')
    body_html = "\n".join(paragraphs)

    # Footer-Lines
    footer_parts = [_html.escape(company_name)]
    if contact_phone:
        footer_parts.append(_html.escape(contact_phone))
    if contact_email:
        footer_parts.append(_html.escape(contact_email))
    if contact_website:
        footer_parts.append(_html.escape(contact_website))

    safe_form_url = _html.escape(form_url, quote=True)
    safe_form_url_display = _html.escape(form_url)
    safe_company = _html.escape(company_name)
    safe_contact = _html.escape(contact_name or "")

    html = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antwort von {safe_company}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1f2937;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f3f4f6; padding: 24px 12px;">
  <tr>
    <td align="center">

      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">

        <!-- Header -->
        <tr>
          <td style="background: linear-gradient(135deg, #1e40af 0%, #3730a3 100%); padding: 28px 32px;">
            <div style="font-size: 20px; color: #ffffff; font-weight: 600; letter-spacing: -0.2px;">
              {safe_company}
            </div>
            <div style="font-size: 13px; color: #c7d2fe; margin-top: 4px;">
              Persoenliche Antwort von {safe_contact}
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding: 32px;">
            <p style="margin: 0 0 18px 0; font-size: 16px; color: #111827;">
              {greeting}
            </p>

            <div style="font-size: 15px; line-height: 1.65; color: #374151;">
              {body_html}
            </div>

            <!-- CTA-Box -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 24px 0;">
              <tr>
                <td align="center" style="background-color: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 24px;">
                  <p style="margin: 0 0 16px 0; font-size: 14px; color: #4b5563;">
                    Damit ich dir ein passendes Angebot machen kann,<br>
                    fuell bitte kurz unser Anfrage-Formular aus:
                  </p>
                  <a href="{safe_form_url}"
                     style="display: inline-block; background-color: #1e40af; color: #ffffff; padding: 13px 32px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 15px;">
                    Anfrage-Formular ausfuellen &rarr;
                  </a>
                  <p style="margin: 14px 0 0 0; font-size: 12px; color: #6b7280;">
                    Direkt-Link:<br>
                    <a href="{safe_form_url}" style="color: #1e40af; word-break: break-all;">{safe_form_url_display}</a>
                  </p>
                </td>
              </tr>
            </table>

            <p style="margin: 24px 0 6px 0; font-size: 15px; color: #374151;">
              Viele Gruesse
            </p>
            <p style="margin: 0; font-size: 15px; color: #111827; font-weight: 600;">
              {safe_contact}
            </p>
            <p style="margin: 2px 0 0 0; font-size: 14px; color: #6b7280;">
              {' &middot; '.join(footer_parts)}
            </p>
          </td>
        </tr>

        <!-- Footer / Disclaimer -->
        <tr>
          <td style="background-color: #f9fafb; padding: 18px 32px; border-top: 1px solid #e5e7eb;">
            <p style="margin: 0; font-size: 12px; color: #6b7280; line-height: 1.5;">
              Diese Antwort wurde mit Hilfe von <strong>Q</strong>, dem digitalen Assistenten von
              {safe_company}, erstellt. Bei Fragen einfach auf diese Mail antworten &ndash;
              {safe_contact} liest mit.
            </p>
          </td>
        </tr>

      </table>

    </td>
  </tr>
</table>

</body>
</html>"""
    return html
