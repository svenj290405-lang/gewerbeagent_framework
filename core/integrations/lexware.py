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
    QuotationDraft,
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
    # Beleglisten + Stammdaten des Kontos
    #
    # Bis 2026-08 hat die App Lexware nur beschrieben und einzelne Belege
    # per ID zurueckgelesen. Damit war alles, was wir ueber offene Betraege
    # sagten, aus unserer eigenen DB geschaetzt. Diese Leser holen die
    # Wahrheit dort, wo sie steht.
    # ------------------------------------------------------------------

    async def get_voucherlist(
        self,
        voucher_type: str,
        voucher_status: str,
        *,
        page: int = 0,
        size: int = 50,
    ) -> dict:
        """GET /v1/voucherlist — Belege eines Typs in einem Status.

        ``voucher_type`` darf kommasepariert sein (``"invoice,creditnote"``),
        ``voucher_status`` NICHT — Lexware antwortet darauf mit 400. Wer
        mehrere Status braucht, fragt mehrfach (und zahlt das Rate-Limit).

        Rueckgabe ist die rohe Seite: ``{"content": [...], "totalElements": n}``.
        Eintraege fuehren u.a. id, voucherType, voucherStatus, voucherNumber,
        voucherDate, contactName, totalAmount, currency, archived — je nach
        Typ zusaetzlich dueDate und openAmount.
        """
        await self._rate_limit()
        params = {
            "voucherType": voucher_type,
            "voucherStatus": voucher_status,
            "page": page,
            "size": size,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/voucherlist",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(r, "get_voucherlist")
            return r.json()

    async def get_payment_conditions(self) -> list[dict]:
        """GET /v1/payment-conditions — Zahlungsbedingungen des Betriebs.

        Eintraege: id, organizationDefault, paymentTermLabelTemplate,
        paymentTermDuration (Tage), optional paymentDiscountConditions
        (Skonto: discountPercentage + discountRange in Tagen).
        """
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/payment-conditions",
                headers=self._headers,
            )
            self._raise_for_status(r, "get_payment_conditions")
            data = r.json()
        return data if isinstance(data, list) else []

    async def get_default_payment_term(self) -> dict | None:
        """Die Standard-Zahlungsbedingung des Betriebs (oder None).

        Genau das steht auf jeder Rechnung, die ohne eigene Bedingung
        angelegt wird — also das echte Zahlungsziel des Betriebs.
        """
        for cond in await self.get_payment_conditions():
            if cond.get("organizationDefault"):
                return cond
        return None

    async def get_posting_categories(self) -> list[dict]:
        """GET /v1/posting-categories — Buchungskategorien des Kontos.

        Eintraege: id, name, type ("income"|"outgo"), contactRequired,
        splitAllowed, groupName. Der pilot-Betrieb hat davon 169 fuer
        Ausgaben — genau die Liste, aus der ein Beleg vorkontiert wird.
        """
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/posting-categories",
                headers=self._headers,
            )
            self._raise_for_status(r, "get_posting_categories")
            data = r.json()
        return data if isinstance(data, list) else []

    async def update_voucher(self, voucher_id: UUID, changes: dict) -> dict:
        """PUT /v1/vouchers/{id} — einen Beleg ergaenzen.

        Lexware verlangt beim Update das VOLLSTAENDIGE Objekt inklusive
        ``version`` (optimistisches Sperren). Darum wird der Beleg erst
        gelesen, dann werden die Aenderungen daraufgelegt und das Ganze
        zurueckgeschickt. Wer nur ein Feld schickt, verliert den Rest.
        """
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/vouchers/{voucher_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "update_voucher(read)")
            aktuell = r.json()

        payload = {**aktuell, **changes}
        payload["version"] = aktuell.get("version", 0)

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.put(
                f"{LEXWARE_API_BASE}/v1/vouchers/{voucher_id}",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
            self._raise_for_status(r, "update_voucher")
            logger.info("Lexware update_voucher OK: %s", voucher_id)
            return r.json() if r.content else {}

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
            "Lexware search_contacts: %d Zeichen Suchbegriff -> %d Treffer",
            len(name or ""), len(results),
        )
        return results

    async def list_contacts_page(
        self,
        page: int = 0,
        size: int = 100,
        customer_only: bool = True,
    ) -> tuple[list[dict], bool]:
        """GET /v1/contacts?page=...&size=... — eine Seite roher
        Kontakt-Objekte fuer den Onboarding-Import (Kundendatenbank
        Phase 8, scripts/import_lexware_contacts.py).

        Liefert (eintraege, letzte_seite). Bewusst rohe Dicts statt
        ContactMatch: der Import braucht mehr Felder (volle
        Billing-Adresse, Telefonnummern) als die Such-UI.
        """
        await self._rate_limit()
        params: dict = {"page": page, "size": size}
        if customer_only:
            params["customer"] = "true"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/contacts",
                headers=self._headers,
                params=params,
            )
            self._raise_for_status(r, "list_contacts_page")
            data = r.json()
        entries = data.get("content") or []
        last = bool(data.get("last", True))
        return entries, last


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
        # Name und Mail des Kunden bleiben draussen; die Lexware-ID
        # reicht, um den Vorgang nachzuvollziehen.
        logger.info("Lexware create_contact OK: id=%s", cid)

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


    async def _complete_contact(
        self,
        contact: ContactMatch,
        phone: str | None,
        email: str | None,
    ) -> ContactMatch:
        """Ergaenzt fehlende Mail/Telefon am bestehenden Kontakt.
        Failsafe: Update-Fehler werden geloggt, der Kontakt kommt
        trotzdem zurueck."""
        updated = False
        if email and not contact.email:
            try:
                await self.update_contact_email(contact.contact_id, email)
                updated = True
                logger.info(f"upsert: Mail ergaenzt fuer {contact.contact_id}")
            except Exception as e:
                logger.warning(f"upsert: Mail-Update fehlgeschlagen: {e}")
        if phone:
            try:
                await self.update_contact_phone(contact.contact_id, phone)
                if updated:
                    logger.info(f"upsert: Phone+Mail ergaenzt fuer {contact.contact_id}")
                else:
                    logger.info(f"upsert: Phone ergaenzt fuer {contact.contact_id}")
                updated = True
            except Exception as e:
                logger.warning(f"upsert: Phone-Update fehlgeschlagen: {e}")
        if updated:
            contact = await self.get_contact(contact.contact_id) or contact
        return contact

    async def upsert_customer_contact(
        self,
        name: str,
        phone: str | None = None,
        email: str | None = None,
        anliegen: str | None = None,
        is_company: bool = False,
        *,
        tenant_id=None,
        kunde_id=None,
        street: str | None = None,
        zip_code: str | None = None,
        city: str | None = None,
    ) -> tuple[ContactMatch, bool]:
        """
        Loest einen Lexware-Kontakt auf oder legt einen neuen an.
        Bei vorhandenem Kontakt: ergaenzt Phone/Mail falls fehlend.

        Mit tenant_id + kunde_id (Kundendatenbank Phase 4) laeuft die
        Aufloesung ueber kunde_external_ref: eine gepinnte Kontakt-ID
        gewinnt immer — kein namensbasiertes Raten mehr. Existiert noch
        kein Ref, wird einmalig namensbasiert gematcht bzw. angelegt und
        das Ergebnis zurueckgepinnt; ab dann ist der Kontakt fixiert.
        Ist der gepinnte Kontakt in Lexware geloescht worden, wird neu
        aufgeloest und der Ref repariert.

        Ohne kunde_id (Alt-Verhalten): Name-Match, bei mehreren
        gleichnamigen Treffern der erste + Warnung; ein mitgegebenes
        `city` bevorzugt den Treffer mit gleichem Ort.

        Returns: (ContactMatch, created_new) - True wenn neu angelegt.
        """
        if not name or len(name.strip()) < 2:
            raise ValueError("Kontakt-Name fehlt")

        # 0) Gepinnter Kontakt aus kunde_external_ref gewinnt immer.
        stale_contact_id = None
        if tenant_id and kunde_id:
            pinned = await lookup_lexware_contact_ref(tenant_id, kunde_id)
            if pinned:
                full = await self.get_contact(pinned)
                if full is not None:
                    return await self._complete_contact(full, phone, email), False
                logger.warning(
                    "upsert: gepinnter Lexware-Kontakt %s nicht ladbar "
                    "(in Lexware geloescht?) — loese neu auf", pinned,
                )
                stale_contact_id = pinned

        # 1) Existierende suchen
        try:
            existing = await self.search_contacts(name.strip(), customer_only=True)
        except Exception as e:
            logger.warning(f"upsert_customer_contact search fehlgeschlagen: {e}")
            existing = []

        # 2) Genauer Name-Match. Ist ein Ort bekannt, zaehlt NUR ein
        # ortsgleicher Treffer (zwei "Thomas Mueller" in verschiedenen
        # Staedten sind verschiedene Kontakte — dann lieber neu anlegen).
        exact = [c for c in existing
                 if c.name.strip().lower() == name.strip().lower()]
        # Kontakte, die schon einem ANDEREN Kunden gepinnt sind, duerfen
        # nicht gematcht werden — sonst kaeme die Vermischung durch die
        # Hintertuer zurueck (Rechnung an fremden Kontakt).
        if exact and tenant_id and kunde_id:
            frei = []
            for c in exact:
                owner = await lookup_lexware_ref_owner(tenant_id, c.contact_id)
                if owner is not None and owner != kunde_id:
                    logger.info(
                        "upsert: Kontakt %s (%r) gehoert schon Kunde %s — "
                        "uebersprungen", c.contact_id, c.name, owner,
                    )
                    continue
                frei.append(c)
            exact = frei
        match = None
        if exact:
            if city:
                match = next(
                    (c for c in exact
                     if c.city and c.city.strip().lower() == city.strip().lower()),
                    None,
                )
            else:
                match = exact[0]
            if match is not None and len(existing) > 1:
                logger.warning(
                    "upsert_customer_contact: %d Treffer fuer %r - nehme Match %s",
                    len(existing), name, match.contact_id,
                )

        if match:
            # Voller Datensatz holen (fuer Phone/Mail-Check)
            full = await self.get_contact(match.contact_id)
            if full is None:
                logger.warning(f"upsert: Kontakt {match.contact_id} nicht ladbar")
                return match, False
            full = await self._complete_contact(full, phone, email)
            if tenant_id and kunde_id:
                await persist_lexware_contact_ref(
                    tenant_id, kunde_id, full.contact_id,
                    replace_contact_id=stale_contact_id)
            return full, False

        # 3) Neu anlegen
        new_contact = await self.create_customer_contact(
            name=name,
            email=email,
            phone=phone,
            street=street,
            zip_code=zip_code,
            city=city,
            is_company=is_company,
        )
        if tenant_id and kunde_id:
            await persist_lexware_contact_ref(
                tenant_id, kunde_id, new_contact.contact_id,
                replace_contact_id=stale_contact_id)
        return new_contact, True


    async def update_contact_phone(
        self,
        contact_id: UUID,
        phone: str,
    ) -> bool:
        """
        PUT /v1/contacts/{id} - aktualisiert Telefonnummer.
        Wie update_contact_email mit Optimistic Locking.
        """
        if not phone or len(phone.strip()) < 4:
            raise ValueError("Ungueltige Telefonnummer")

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/contacts/{contact_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "update_contact_phone/get")
            current = r.json()

        phones = current.get("phoneNumbers") or {}
        business = phones.get("business") or []

        if len(business) > 1:
            logger.warning(
                "update_contact_phone: %s hat schon %d Business-Phones - skip",
                contact_id, len(business),
            )
            return False

        if business and business[0].strip() == phone.strip():
            return True

        phones["business"] = [phone.strip()]
        current["phoneNumbers"] = phones

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.put(
                f"{LEXWARE_API_BASE}/v1/contacts/{contact_id}",
                headers={**self._headers, "Content-Type": "application/json"},
                json=current,
            )
            self._raise_for_status(r, "update_contact_phone/put")

        logger.info("update_contact_phone OK: %s -> %r", contact_id, phone)
        return True

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
        finalize: bool = False,
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

        # finalize=true: Rechnung wird direkt als "open" angelegt statt als
        # Draft — nur dann ist PDF-Download verfuegbar, was wir fuer die
        # Rechnungs-Mail an den Kunden brauchen (Lifecycle-Schritt "Fertig"
        # im /auftraege-Flow).
        url = f"{LEXWARE_API_BASE}/v1/invoices"
        if finalize:
            url += "?finalize=true"

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                url,
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(r, "create_invoice_draft")
            data = r.json()

        invoice_id = UUID(data["id"])
        logger.info(
            "Lexware create_invoice_draft OK: id=%s items=%d tax=%s finalize=%s",
            invoice_id, len(line_items), tax_type, finalize,
        )
        return InvoiceDraft(
            invoice_id=invoice_id,
            voucher_number=None,  # bei draft noch nicht vergeben
            deeplink_view=self.invoice_deeplink_view(invoice_id),
            deeplink_edit=self.invoice_deeplink_edit(invoice_id),
            raw_response=data,
        )

    async def create_quotation_draft(
        self,
        line_items: list[InvoiceLineItem],
        contact_id: UUID | None = None,
        one_time_address: dict | None = None,
        voucher_date: str | None = None,
        expiration_date: str | None = None,
        title: str | None = None,
        introduction: str | None = None,
        remark: str | None = None,
        tax_type: str = "gross",
        finalize: bool = False,
    ) -> QuotationDraft:
        """
        POST /v1/quotations  (ohne ?finalize -> bleibt 'draft')

        Analog zu create_invoice_draft, aber fuer Angebote.
        Zusaetzliche Pflicht: expirationDate (default: voucher_date + 30 Tage).

        Entweder contact_id ODER one_time_address muss gesetzt sein.

        introduction = Anschreiben (von Gemini generiert)
        remark = Schluss-Text (von Gemini generiert)
        Pro Position kann li.description die line-Beschreibung enthalten.
        """
        if not line_items:
            raise ValueError("Mindestens eine Angebots-Position erforderlich")
        if not contact_id and not one_time_address:
            raise ValueError("Entweder contact_id oder one_time_address muss gesetzt sein")

        if not voucher_date:
            voucher_date = dt_now_iso()
        if not expiration_date:
            # Default: 30 Tage Gueltigkeit, gleiches Format wie voucher_date
            import datetime as _dt
            base = _dt.datetime.now(_dt.timezone.utc).astimezone()
            expiration_date = (base + _dt.timedelta(days=30)).isoformat(timespec="milliseconds")

        # Address-Block bauen
        if contact_id:
            address_block = {"contactId": str(contact_id)}
        else:
            address_block = dict(one_time_address)
            address_block.setdefault("countryCode", "DE")

        # LineItems in Lexware-Format wandeln (analog Invoice)
        items_payload = []
        for li in line_items:
            unit_price = {
                "currency": "EUR",
                "taxRatePercentage": li.tax_rate_percent,
            }
            if tax_type == "gross":
                unit_price["grossAmount"] = round(float(li.unit_price_gross), 2)
            else:
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
            "expirationDate": expiration_date,
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

        # ?finalize=true bewirkt dass die Quotation direkt als "open"
        # angelegt wird statt als Draft. Nur dann ist der PDF-Download
        # verfuegbar — was wir fuer die automatische Mail an den Kunden
        # brauchen. Achtung: finalisierte Angebote koennen in Lexware
        # nicht mehr inhaltlich geaendert werden.
        url = f"{LEXWARE_API_BASE}/v1/quotations"
        if finalize:
            url += "?finalize=true"

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                url,
                headers={**self._headers, "Content-Type": "application/json"},
                json=body,
            )
            self._raise_for_status(r, "create_quotation_draft")
            data = r.json()

        quotation_id = UUID(data["id"])
        logger.info(
            "Lexware create_quotation_draft OK: id=%s items=%d expires=%s "
            "tax=%s finalize=%s",
            quotation_id, len(line_items), expiration_date[:10], tax_type, finalize,
        )
        return QuotationDraft(
            quotation_id=quotation_id,
            voucher_number=None,  # bei draft noch nicht vergeben
            deeplink_view=self.quotation_deeplink_view(quotation_id),
            deeplink_edit=self.quotation_deeplink_edit(quotation_id),
            expiration_date=expiration_date,
            raw_response=data,
        )

    @staticmethod
    def quotation_deeplink_view(quotation_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/quotations/view/{quotation_id}"

    @staticmethod
    def quotation_deeplink_edit(quotation_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/quotations/edit/{quotation_id}"

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

    async def get_quotation(self, quotation_id: UUID) -> dict:
        """GET /v1/quotations/{id} - rohes JSON zurueckgeben."""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/quotations/{quotation_id}",
                headers=self._headers,
            )
            self._raise_for_status(r, "get_quotation")
            return r.json()

    async def download_quotation_pdf(self, quotation_id: UUID) -> bytes:
        """
        GET /v1/quotations/{id}/file
        Liefert PDF-Bytes des Angebots. Funktioniert nur wenn Angebot NICHT
        mehr im Draft-Status (Lexware verlangt Finalisierung).
        """
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{LEXWARE_API_BASE}/v1/quotations/{quotation_id}/file",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/pdf",
                },
            )
            if r.status_code == 409:
                raise AccountingError(
                    "Angebot ist noch im Draft-Status, PDF-Download nicht moeglich. "
                    "Bitte erst in Lexware finalisieren.",
                    status_code=409,
                    provider=self.provider_name,
                )
            self._raise_for_status(r, "download_quotation_pdf")
            return r.content

    @staticmethod
    def quotation_deeplink_view(quotation_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/quotations/view/{quotation_id}"

    @staticmethod
    def quotation_deeplink_edit(quotation_id: UUID) -> str:
        return f"{LEXWARE_APP_BASE}/permalink/quotations/edit/{quotation_id}"

    # Lexware-spezifische Helper (nicht in Basis-Klasse)
    # ------------------------------------------------------------------

    @staticmethod
    def voucher_deeplink(voucher_id: UUID) -> str:
        """Lexware-App-URL um Voucher direkt zu oeffnen."""
        return f"{LEXWARE_APP_BASE}/permalink/vouchers/view/{voucher_id}"


# ---------------------------------------------------------------------------
# kunde_external_ref: Kunde <-> Lexware-Kontakt (Kundendatenbank Phase 4)
#
# Die Verknuepfung interner Kunde -> Lexware-Kontakt-ID lebt in der
# generischen Tabelle kunde_external_ref. Beide Helper sind failsafe
# (werfen nie) — ein Ref-Problem darf keine Rechnung blockieren,
# schlimmstenfalls laeuft die Aufloesung einmal mehr ueber den Namen.
# ---------------------------------------------------------------------------

async def lookup_lexware_contact_ref(tenant_id, kunde_id) -> str | None:
    """Liefert die gepinnte Lexware-Kontakt-ID des Kunden oder None."""
    if not tenant_id or not kunde_id:
        return None
    try:
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models import KundeExternalRef, REF_SYSTEM_LEXWARE
        async with AsyncSessionLocal() as s:
            ref = (await s.execute(
                select(KundeExternalRef).where(
                    KundeExternalRef.tenant_id == tenant_id,
                    KundeExternalRef.kunde_id == kunde_id,
                    KundeExternalRef.system == REF_SYSTEM_LEXWARE,
                )
            )).scalars().first()
            return ref.external_id if ref else None
    except Exception:
        logger.exception("lookup_lexware_contact_ref fehlgeschlagen (egal)")
        return None


async def lookup_lexware_ref_owner(tenant_id, contact_id):
    """Liefert die kunde_id, die diesen Lexware-Kontakt gepinnt hat —
    oder None. Fuer den Namens-Match: ein Kontakt, der schon einem
    anderen Kunden gehoert, darf nicht nochmal gematcht werden."""
    if not tenant_id or not contact_id:
        return None
    try:
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models import KundeExternalRef, REF_SYSTEM_LEXWARE
        async with AsyncSessionLocal() as s:
            ref = (await s.execute(
                select(KundeExternalRef).where(
                    KundeExternalRef.tenant_id == tenant_id,
                    KundeExternalRef.system == REF_SYSTEM_LEXWARE,
                    KundeExternalRef.external_id == str(contact_id).lower(),
                )
            )).scalars().first()
            return ref.kunde_id if ref else None
    except Exception:
        logger.exception("lookup_lexware_ref_owner fehlgeschlagen (egal)")
        return None


async def persist_lexware_contact_ref(
    tenant_id, kunde_id, contact_id, *, replace_contact_id=None,
) -> None:
    """Pinnt die Lexware-Kontakt-ID am Kunden.

    Ein bestehender abweichender Ref wird NIE stillschweigend
    ueberschrieben — nur wenn replace_contact_id explizit den alten
    Wert benennt (Repair, nachdem der Kontakt in Lexware geloescht
    wurde). Zeigt ein anderer Kunde schon auf diesen Kontakt
    (Unique-Constraint), wird gewarnt und nichts geschrieben.
    """
    if not tenant_id or not kunde_id or not contact_id:
        return
    cid = str(contact_id).lower()
    try:
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models import KundeExternalRef, REF_SYSTEM_LEXWARE
        async with AsyncSessionLocal() as s:
            eigener = (await s.execute(
                select(KundeExternalRef).where(
                    KundeExternalRef.tenant_id == tenant_id,
                    KundeExternalRef.kunde_id == kunde_id,
                    KundeExternalRef.system == REF_SYSTEM_LEXWARE,
                ).with_for_update()
            )).scalars().first()
            if eigener is not None:
                if eigener.external_id == cid:
                    return
                if (replace_contact_id
                        and eigener.external_id == str(replace_contact_id).lower()):
                    eigener.external_id = cid
                    await s.commit()
                    logger.info(
                        "lexware-ref repariert: kunde=%s %s -> %s",
                        kunde_id, replace_contact_id, cid,
                    )
                    return
                logger.warning(
                    "lexware-ref-Konflikt: kunde=%s hat schon %s, "
                    "neuer Kontakt %s wird NICHT gepinnt",
                    kunde_id, eigener.external_id, cid,
                )
                return
            fremder = (await s.execute(
                select(KundeExternalRef).where(
                    KundeExternalRef.tenant_id == tenant_id,
                    KundeExternalRef.system == REF_SYSTEM_LEXWARE,
                    KundeExternalRef.external_id == cid,
                )
            )).scalars().first()
            if fremder is not None:
                logger.warning(
                    "lexware-ref-Konflikt: Kontakt %s gehoert schon "
                    "Kunde %s, nicht auch Kunde %s",
                    cid, fremder.kunde_id, kunde_id,
                )
                return
            s.add(KundeExternalRef(
                tenant_id=tenant_id, kunde_id=kunde_id,
                system=REF_SYSTEM_LEXWARE, external_id=cid,
            ))
            await s.commit()
            logger.info("lexware-ref gepinnt: kunde=%s -> %s", kunde_id, cid)
    except Exception:
        logger.exception("persist_lexware_contact_ref fehlgeschlagen (egal)")
