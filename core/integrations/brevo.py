"""
Brevo (Sendinblue) Mail-Adapter.

Generischer Mail-Versand via Brevo API. Unterstuetzt Anhaenge (z.B.
Rechnungs-PDFs).

API-Doku: https://developers.brevo.com/reference/sendtransacemail
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BREVO_API_BASE = "https://api.brevo.com/v3"
DEFAULT_TIMEOUT = 30.0


class BrevoError(Exception):
    """Brevo-API-Fehler."""

    def __init__(self, message: str, status_code: int | None = None,
                 raw_response: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.raw_response = raw_response

    def __str__(self) -> str:
        if self.status_code:
            return f"{self.message} (HTTP {self.status_code})"
        return self.message


@dataclass
class MailAttachment:
    """Ein Datei-Anhang fuer Mailversand."""
    filename: str
    content_bytes: bytes
    content_type: str = "application/pdf"


@dataclass
class MailRecipient:
    """Ein Empfaenger-Eintrag."""
    email: str
    name: str | None = None


class BrevoMailer:
    """Wrapper um Brevo /smtp/email Endpoint mit Attachment-Support."""

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError("Brevo-API-Key fehlt")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json",
        }

    async def send(
        self,
        sender_email: str,
        sender_name: str,
        to: MailRecipient,
        subject: str,
        html_body: str,
        text_body: str | None = None,
        reply_to_email: str | None = None,
        reply_to_name: str | None = None,
        attachments: list[MailAttachment] | None = None,
        custom_headers: dict | None = None,
    ) -> dict:
        """
        Verschickt eine Mail. Wirft BrevoError bei Fehler (im Gegensatz zur
        send_reply_via_brevo-Funktion in mail_intake, die silent failt).

        Returns: Brevo-Response-JSON (enthaelt messageId).
        """
        if not to.email:
            raise ValueError("Empfaenger-E-Mail fehlt")

        payload = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": to.email, "name": to.name or to.email}],
            "subject": subject,
            "htmlContent": html_body,
        }
        if text_body:
            payload["textContent"] = text_body

        if reply_to_email:
            payload["replyTo"] = {
                "email": reply_to_email,
                "name": reply_to_name or sender_name,
            }

        if attachments:
            payload["attachment"] = []
            for a in attachments:
                payload["attachment"].append({
                    "name": a.filename,
                    "content": base64.b64encode(a.content_bytes).decode("ascii"),
                })

        if custom_headers:
            payload["headers"] = custom_headers

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{BREVO_API_BASE}/smtp/email",
                    headers=self._headers,
                    json=payload,
                )
        except httpx.RequestError as e:
            raise BrevoError(f"Verbindung zu Brevo fehlgeschlagen: {e}") from e

        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            logger.info(
                "Brevo send OK: to=%s subject=%r message_id=%s attachments=%d",
                to.email, subject, data.get("messageId"),
                len(attachments) if attachments else 0,
            )
            return data

        try:
            err_body = resp.json()
        except Exception:
            err_body = {"raw_text": resp.text[:300]}
        logger.error(
            "Brevo send fehlgeschlagen: HTTP %s body=%s to=%s subject=%r",
            resp.status_code, err_body, to.email, subject,
        )
        raise BrevoError(
            f"Brevo-Fehler beim Mail-Versand",
            status_code=resp.status_code,
            raw_response=err_body,
        )
