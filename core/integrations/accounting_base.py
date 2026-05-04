"""
Abstrakte Basis-Klasse fuer Buchhaltungs-Provider.

Konkrete Implementierungen (Lexware, DATEV, sevdesk) implementieren
diese Schnittstelle. So kann der Rest des Codes Provider-agnostisch
sein und wir koennen einfach neue Provider hinzufuegen.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID


@dataclass
class UploadResult:
    """Ergebnis eines Beleg-Uploads."""
    file_id: UUID            # Provider-File-ID
    voucher_id: UUID | None  # Provider-Voucher-ID (falls automatisch erstellt)
    raw_response: dict       # Vollstaendige Provider-Antwort fuer Debugging


@dataclass
class VoucherInfo:
    """Status-Info eines Vouchers."""
    voucher_id: UUID
    status: str              # "draft" | "open" | "paid" | "voided" | "unchecked" | ...
    voucher_type: str
    raw_data: dict




@dataclass
class ContactMatch:
    """Ergebnis einer Kontakt-Suche."""
    contact_id: UUID
    name: str
    role: str  # "customer" | "vendor" | "both"
    email: str | None = None
    city: str | None = None
    raw_data: dict | None = None


@dataclass
class InvoiceLineItem:
    """Eine Position auf einer Rechnung."""
    name: str               # z.B. "Moebelmontage"
    quantity: float         # z.B. 1
    unit_name: str          # z.B. "Stueck", "Stunde"
    unit_price_gross: float # Brutto-Einzelpreis in EUR
    description: str | None = None
    tax_rate_percent: int = 19  # Standard, Photovoltaik=0, Buecher=7


@dataclass
class InvoiceDraft:
    """Ergebnis einer Rechnungs-Erstellung als Draft."""
    invoice_id: UUID
    voucher_number: str | None  # bei Draft oft None
    deeplink_view: str          # URL zum Anschauen
    deeplink_edit: str          # URL zum Bearbeiten
    raw_response: dict


@dataclass
class QuotationDraft:
    """Ergebnis einer Angebots-Erstellung als Draft.

    Analog zu InvoiceDraft, aber fuer Lexware Quotations (/v1/quotations).
    Quotation-Drafts haben zusaetzlich eine expirationDate (Gueltig-bis).
    """
    quotation_id: UUID
    voucher_number: str | None     # z.B. "AN-00042" - bei Draft oft None
    deeplink_view: str             # URL zum Anschauen in Lexware
    deeplink_edit: str             # URL zum Bearbeiten in Lexware
    expiration_date: str | None = None  # ISO-Date wann das Angebot ablaeuft
    raw_response: dict | None = None


class AccountingError(Exception):
    """Basis-Fehlerklasse fuer Buchhaltungs-Operationen."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        provider: str | None = None,
        raw_response: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.provider = provider
        self.raw_response = raw_response

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.provider:
            parts.append(f"provider={self.provider}")
        return " | ".join(parts)


class AccountingProvider(ABC):
    """Abstrakte Basis fuer alle Buchhaltungs-Provider."""

    provider_name: str = "abstract"

    @abstractmethod
    async def upload_voucher_file(
        self,
        file_bytes: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> UploadResult:
        """
        Laedt eine Beleg-Datei (Foto/PDF) zum Provider hoch.
        Manche Provider (z.B. Lexware) erstellen automatisch einen Voucher-
        Stub, andere (z.B. DATEV) brauchen einen separaten Schritt.
        """
        ...

    @abstractmethod
    async def get_voucher(self, voucher_id: UUID) -> VoucherInfo:
        """Holt Voucher-Status + Daten."""
        ...

    @abstractmethod
    async def delete_voucher(self, voucher_id: UUID) -> bool:
        """Loescht einen Voucher (z.B. fuer Test-Cleanup oder Undo)."""
        ...

    @abstractmethod
    async def health_check(self) -> dict:
        """Prueft ob API-Key gueltig ist + gibt Account-Info zurueck."""
        ...
