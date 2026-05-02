"""
Lexware-Office-API-Adapter.

API-Doku: https://developers.lexware.io/docs/

Wir nutzen primaer:
  - GET  /v1/profile           - Health-Check
  - POST /v1/files             - Beleg-Datei hochladen (erstellt auto Voucher)
  - GET  /v1/vouchers/{id}     - Voucher-Status abfragen
  - DELETE /v1/vouchers/{id}   - Voucher loeschen (Cleanup/Undo)

Rate-Limit: 2 req/sec. Wir halten uns dran via Trivial-Sleep
(reicht fuer unseren Use-Case, kein paralleler Massenupload).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from uuid import UUID

import httpx

from core.integrations.accounting_base import (
    AccountingError,
    AccountingProvider,
    ContactMatch,
    InvoiceDraft,
    InvoiceLineItem,
    UploadResult,
    VoucherInfo,
)

logger = logging.getLogger(__name__)

LEXWARE_API_BASE = "https://api.lexware.io"
LEXWARE_APP_BASE = "https://app.lexware.de"
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_DELAY = 0.6  # Sekunden zwischen Calls (max 2/s laut Doku)

def dt_now_iso() -> str:
    """ISO-Datum fuer Lexware (UTC offset, +02:00 in DE-Zeitzone)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")



class LexwareProvider(AccountingProvider):
    """Lexware-Office-API-Client."""

    provider_name = "lexware"

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT):
        if not api_key:
            raise ValueError("Lexware-API-Key fehlt")
        self.api_key = api_key
        self.timeout = timeout
        self._last_call_at: float = 0.0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _rate_limit(self) -> None:
        """Blockt kurz, damit wir nicht 429 von Lexware bekommen."""
        now = time.monotonic()
        elapsed = now - self._last_call_at
        if elapsed < RATE_LIMIT_DELAY:
            await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_call_at = time.monotonic()

    def _raise_for_status(
        self, resp: httpx.Response, action: str
    ) -> None:
        if resp.is_success:
            return
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw_text": resp.text[:500]}
        msg = f"Lexware-Fehler bei '{action}'"
        logger.error(
            "%s: HTTP %s %s | body=%s",
            msg, resp.status_code, resp.reason_phrase, payload,
        )
        raise AccountingError(
            msg,
            status_code=resp.status_code,
            provider=self.provider_name,
            raw_response=payload,
        )

    # ------------------------------------------------------------------
    # AccountingProvider Interface
    # ------------------------------------------------------------------

    async def health_check(self) -> dict:
        """GET /v1/profile - prueft Auth + gibt Org-Info zurueck."""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/profile",
                headers=self._headers,
            )
            self._raise_for_status(r, "health_check")
            data = r.json()
        logger.info(
            "Lexware health_check OK: org=%s features=%s",
            data.get("organizationId"),
            data.get("businessFeatures"),
        )
        return data

    async def upload_voucher_file(
        self,
        file_bytes: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> UploadResult:
        """
        POST /v1/files
        Laedt eine Beleg-Datei hoch. Lexware erstellt automatisch einen
        Voucher-Stub (status=unchecked), den der User in der Lexware-UI
        manuell ergaenzen muss.

        Response (HTTP 202):
          { "id": "<file-uuid>", "voucherId": "<voucher-uuid>" }
        """
        if not file_bytes:
            raise ValueError("file_bytes ist leer")
        if not mime_type:
            mime_type = "application/octet-stream"

        if not filename:
            ext = mimetypes.guess_extension(mime_type) or ".bin"
            filename = f"beleg{ext}"

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            files = {
                "file": (filename, file_bytes, mime_type),
            }
            data = {"type": "voucher"}
            r = await client.post(
                f"{LEXWARE_API_BASE}/v1/files",
                headers=self._headers,
                files=files,
                data=data,
            )
            self._raise_for_status(r, "upload_voucher_file")
            payload = r.json()

        try:
            file_id = UUID(payload["id"])
            voucher_id = (
                UUID(payload["voucherId"])
                if payload.get("voucherId") else None
            )
        except (KeyError, ValueError) as e:
            raise AccountingError(
                f"Lexware-Response unerwartet: {payload}",
                provider=self.provider_name,
                raw_response=payload,
            ) from e

        logger.info(
            "Lexware upload OK: file_id=%s voucher_id=%s size=%d mime=%s",
            file_id, voucher_id, len(file_bytes), mime_type,
        )
        return UploadResult(
            file_id=file_id,
            voucher_id=voucher_id,
            raw_response=payload,
        )

    async def get_voucher(self, voucher_id: UUID) -> VoucherInfo:
        """GET /v1/vouchers/{id}"""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/vouchers/{voucher_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "get_voucher")
            data = r.json()

        return VoucherInfo(
            voucher_id=UUID(data["id"]),
            status=data.get("voucherStatus", "unknown"),
            voucher_type=data.get("type", "unknown"),
            raw_data=data,
        )

    async def delete_voucher(self, voucher_id: UUID) -> bool:
        """DELETE /v1/vouchers/{id} - Vorsicht, irreversibel."""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.delete(
                f"{LEXWARE_API_BASE}/v1/vouchers/{voucher_id}",
                headers=self._headers,
            )
            if r.status_code == 404:
                logger.warning(
                    "Lexware delete_voucher: %s schon weg (404)",
                    voucher_id,
                )
                return False
            self._raise_for_status(r, "delete_voucher")
        logger.info("Lexware delete_voucher OK: %s", voucher_id)
        return True

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    async def search_contacts(
        self,
        name: str,
        customer_only: bool = True,
        limit: int = 10,
    ) -> list[ContactMatch]:
        """
        GET /v1/contacts?name=...&customer=true
        Lexware Pattern-Match: 'Mueller' findet 'Frau Mueller', 'mueller@x.de' etc.
        Mindestens 3 Zeichen, sonst leer.
        """
        if not name or len(name.strip()) < 3:
            return []
        await self._rate_limit()
        params = {"name": name.strip()}
        if customer_only:
            params["customer"] = "true"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/contacts",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(r, "search_contacts")
            data = r.json()

        results = []
        for entry in (data.get("content") or [])[:limit]:
            try:
                cid = UUID(entry["id"])
            except Exception:
                continue
            roles = entry.get("roles") or {}
            role = "customer" if "customer" in roles else (
                "vendor" if "vendor" in roles else "unknown"
            )
            if "customer" in roles and "vendor" in roles:
                role = "both"

            company = entry.get("company") or {}
            person = entry.get("person") or {}
            display_name = (
                company.get("name")
                or " ".join(
                    p for p in [
                        person.get("salutation"),
                        person.get("firstName"),
                        person.get("lastName"),
                    ] if p
                )
                or "(unbekannt)"
            )

            email = None
            emails = entry.get("emailAddresses") or {}
            for kind in ("business", "office", "private", "other"):
                lst = emails.get(kind) or []
                if lst:
                    email = lst[0]
                    break

            city = None
            addresses = entry.get("addresses") or {}
            billing = addresses.get("billing") or []
            if billing:
                city = (billing[0] or {}).get("city")

            results.append(ContactMatch(
                contact_id=cid,
                name=display_name,
                role=role,
                email=email,
                city=city,
                raw_data=entry,
            ))
        logger.info(
            "Lexware search_contacts: name=%r -> %d Treffer",
            name, len(results),
        )
        return results


    async def create_customer_contact(
        self,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        street: str | None = None,
        zip_code: str | None = None,
        city: str | None = None,
        country_code: str = "DE",
        is_company: bool = False,
    ) -> ContactMatch:
        """
        POST /v1/contacts - legt neuen Kunden-Kontakt an.

        Wichtig: Lexware erlaubt nur 1 Eintrag pro Liste (emailAddresses.business etc.)
        beim Anlegen - sonst ValidationError.
        """
        if not name or len(name.strip()) < 2:
            raise ValueError("Kontakt-Name fehlt")

        body = {
            "version": 0,
            "roles": {"customer": {}},
        }

        # person vs company
        if is_company:
            body["company"] = {"name": name.strip()}
        else:
            # Versuch Vorname + Nachname zu trennen
            parts = name.strip().split(maxsplit=1)
            person = {}
            # Salutation aus name extrahieren falls "Frau X" oder "Herr X"
            salutation = None
            if parts and parts[0].lower() in ("frau", "herr"):
                salutation = parts[0].capitalize()
                parts = parts[1].split(maxsplit=1) if len(parts) > 1 else []
            if salutation:
                person["salutation"] = salutation
            if len(parts) == 2:
                person["firstName"] = parts[0]
                person["lastName"] = parts[1]
            elif len(parts) == 1:
                person["lastName"] = parts[0]
            else:
                person["lastName"] = name.strip()
            body["person"] = person

        # Optional: Adresse
        if street or zip_code or city:
            address = {"countryCode": country_code}
            if street:
                address["street"] = street
            if zip_code:
                address["zip"] = zip_code
            if city:
                address["city"] = city
            address["isPrimary"] = True
            body["addresses"] = {"billing": [address]}

        # Optional: Email
        if email:
            body["emailAddresses"] = {"business": [email.strip()]}

        # Optional: Telefon
        if phone:
            body["phoneNumbers"] = {"business": [phone.strip()]}

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{LEXWARE_API_BASE}/v1/contacts",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(r, "create_customer_contact")
            data = r.json()

        cid = UUID(data["id"])
        logger.info("Lexware create_contact OK: id=%s name=%r email=%r", cid, name, email)

        # Format-konsistent zu search_contacts
        roles = data.get("roles") or {}
        role = "customer" if "customer" in roles else "unknown"
        return ContactMatch(
            contact_id=cid,
            name=name,
            role=role,
            email=email,
            city=city,
            raw_data=data,
        )

    async def update_contact_email(
        self,
        contact_id: UUID,
        email: str,
    ) -> bool:
        """
        PUT /v1/contacts/{id} - aktualisiert Mail-Adresse eines Kontakts.

        Lexware-Quirks:
        - Wir muessen die aktuelle 'version' mitschicken (Optimistic Locking)
        - Wir muessen das KOMPLETTE Objekt PUT-en, nicht nur die Aenderung
        - Bei mehr als 1 Eintrag pro emailAddresses-Liste: ValidationError
        """
        if not email or "@" not in email:
            raise ValueError("Ungueltige Mail-Adresse")

        # 1) Aktuellen Kontakt holen
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/contacts/{contact_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "update_contact_email/get")
            current = r.json()

        # 2) Mail einbauen
        emails = current.get("emailAddresses") or {}
        business = emails.get("business") or []

        # Wenn schon eine business-Mail da: ueberschreiben
        # Wenn mehrere: Lexware erlaubt Update sowieso nicht - skippen
        if len(business) > 1:
            logger.warning(
                "Lexware update_contact_email: %s hat schon %d Business-Mails - skip",
                contact_id, len(business),
            )
            return False

        # Wenn schon dieselbe Mail: nichts tun
        if business and business[0].lower() == email.lower():
            logger.info("Lexware update_contact_email: %s hat schon %r", contact_id, email)
            return True

        emails["business"] = [email]
        current["emailAddresses"] = emails

        # 3) PUT mit Version
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.put(
                f"{LEXWARE_API_BASE}/v1/contacts/{contact_id}",
                headers={**self._headers, "Content-Type": "application/json"},
                json=current,
            )
            self._raise_for_status(r, "update_contact_email/put")

        logger.info("Lexware update_contact_email OK: %s -> %r", contact_id, email)
        return True

    async def get_contact(self, contact_id: UUID) -> ContactMatch | None:
        """GET /v1/contacts/{id}"""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/contacts/{contact_id}",
                headers=self._headers,
            )
            if r.status_code == 404:
                return None
            self._raise_for_status(r, "get_contact")
            data = r.json()

        roles = data.get("roles") or {}
        role = "customer" if "customer" in roles else (
            "vendor" if "vendor" in roles else "unknown"
        )
        company = data.get("company") or {}
        person = data.get("person") or {}
        display_name = (
            company.get("name")
            or " ".join(p for p in [
                person.get("salutation"),
                person.get("firstName"),
                person.get("lastName"),
            ] if p)
            or "(unbekannt)"
        )
        email = None
        emails = data.get("emailAddresses") or {}
        for kind in ("business", "office", "private", "other"):
            lst = emails.get(kind) or []
            if lst:
                email = lst[0]
                break
        city = None
        addresses = data.get("addresses") or {}
        billing = addresses.get("billing") or []
        if billing:
            city = (billing[0] or {}).get("city")
        return ContactMatch(
            contact_id=contact_id,
            name=display_name,
            role=role,
            email=email,
            city=city,
            raw_data=data,
        )

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    async def create_invoice_draft(
        self,
        line_items: list[InvoiceLineItem],
        contact_id: UUID | None = None,
        one_time_address: dict | None = None,
        voucher_date: str | None = None,
        title: str | None = None,
        introduction: str | None = None,
        remark: str | None = None,
        tax_type: str = "gross",
    ) -> InvoiceDraft:
        """
        POST /v1/invoices  (ohne ?finalize=true -> bleibt 'draft')

        Entweder contact_id ODER one_time_address muss gesetzt sein.

        one_time_address dict: {"name": "...", "city": "...", "countryCode": "DE",
                                 "street": "...", "zip": "...", "supplement": "..."}

        tax_type: "gross" (Brutto-Eingabe) | "net" | "vatfree" | ...
        """
        if not line_items:
            raise ValueError("Mindestens eine Rechnungs-Position erforderlich")
        if not contact_id and not one_time_address:
            raise ValueError("Entweder contact_id oder one_time_address muss gesetzt sein")
        if not voucher_date:
            voucher_date = dt_now_iso()

        # Address-Block bauen
        if contact_id:
            address_block = {"contactId": str(contact_id)}
        else:
            address_block = dict(one_time_address)
            address_block.setdefault("countryCode", "DE")

        # LineItems in Lexware-Format wandeln
        items_payload = []
        for li in line_items:
            unit_price = {
                "currency": "EUR",
                "taxRatePercentage": li.tax_rate_percent,
            }
            if tax_type == "gross":
                unit_price["grossAmount"] = round(float(li.unit_price_gross), 2)
            else:
                # Bei net = Eingabe ist Netto
                unit_price["netAmount"] = round(float(li.unit_price_gross), 2)

            item = {
                "type": "custom",
                "name": li.name,
                "quantity": float(li.quantity),
                "unitName": li.unit_name,
                "unitPrice": unit_price,
            }
            if li.description:
                item["description"] = li.description
            items_payload.append(item)

        body = {
            "voucherDate": voucher_date,
            "address": address_block,
            "lineItems": items_payload,
            "totalPrice": {"currency": "EUR"},
            "taxConditions": {"taxType": tax_type},
            "shippingConditions": {
                "shippingType": "service",
                "shippingDate": voucher_date,
            },
        }
        if title:
            body["title"] = title
        if introduction:
            body["introduction"] = introduction
        if remark:
            body["remark"] = remark

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{LEXWARE_API_BASE}/v1/invoices",
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(r, "create_invoice_draft")
            data = r.json()

        invoice_id = UUID(data["id"])
        logger.info(
            "Lexware create_invoice_draft OK: id=%s items=%d tax=%s",
            invoice_id, len(line_items), tax_type,
        )
        return InvoiceDraft(
            invoice_id=invoice_id,
            voucher_number=None,  # bei draft noch nicht vergeben
            deeplink_view=self.invoice_deeplink_view(invoice_id),
            deeplink_edit=self.invoice_deeplink_edit(invoice_id),
            raw_response=data,
        )

    async def get_invoice(self, invoice_id: UUID) -> dict:
        """GET /v1/invoices/{id} - rohes JSON zurueckgeben."""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/invoices/{invoice_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "get_invoice")
            return r.json()

    async def download_invoice_pdf(self, invoice_id: UUID) -> bytes:
        """
        GET /v1/invoices/{id}/file
        Liefert PDF-Bytes. Funktioniert nur wenn Rechnung NICHT mehr im Draft-Status.
        """
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/invoices/{invoice_id}/file",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/pdf",
                },
            )
            if r.status_code == 409:
                raise AccountingError(
                    "Rechnung ist noch im Draft-Status, PDF-Download nicht moeglich. Bitte erst in Lexware finalisieren.",
                    status_code=409,
                    provider=self.provider_name,
                )
            self._raise_for_status(r, "download_invoice_pdf")
            return r.content

    @staticmethod
    def invoice_deeplink_view(invoice_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/invoices/view/{invoice_id}"

    @staticmethod
    def invoice_deeplink_edit(invoice_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/invoices/edit/{invoice_id}"

    # Lexware-spezifische Helper (nicht in Basis-Klasse)
    # ------------------------------------------------------------------

    @staticmethod
    def voucher_deeplink(voucher_id: UUID) -> str:
        """Lexware-App-URL um Voucher direkt zu oeffnen."""
        return f"{LEXWARE_APP_BASE}/permalink/vouchers/view/{voucher_id}"
