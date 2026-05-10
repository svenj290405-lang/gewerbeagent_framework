"""Anfrage-Formular-Logik: Token erstellen, validieren, abschicken.

Workflow:
1. create_anfrage_token() -> bei RELEVANT_KUNDE Mail aufrufen
2. URL bauen + an Kunde mailen
3. Kunde fuellt Formular aus (Web)
4. submit_anfrage() -> speichert AnfrageResponse + Telegram-Push
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from config.settings import settings
from core.database import AsyncSessionLocal
from core.models import (
    ANFRAGE_TYP_ALLGEMEIN,
    ANFRAGE_TYP_TISCHLER,
    AnfrageResponse,
    AnfrageToken,
    Tenant,
)

logger = logging.getLogger(__name__)


# Default-Schemas pro Anfrage-Typ.
# Spaeter koennen Tenants eigene Schemas pflegen ueber tenant_knowledge oder eigene Tabelle.
ANFRAGE_SCHEMAS = {
    ANFRAGE_TYP_TISCHLER: {
        "title": "Anfrage-Formular",
        "subtitle": "Damit wir dir das beste Angebot machen koennen",
        "fields": [
            {
                "name": "produkt",
                "label": "Was moechtest du anfertigen lassen?",
                "type": "radio",
                "required": True,
                "options": ["Schrank", "Tisch", "Regal", "Bett", "Etwas anderes"],
            },
            {
                "name": "beschreibung",
                "label": "Beschreib es kurz (Stil, besondere Wuensche)",
                "type": "textarea",
                "required": False,
                "placeholder": "z.B. moderner Schrank mit Schiebetueren",
            },
            {
                "name": "masse",
                "label": "Maße ungefaehr (in cm)",
                "type": "masse",
                "required": False,
            },
            {
                "name": "material",
                "label": "Material",
                "type": "checkbox_multi",
                "required": False,
                "options": ["Eiche", "Buche", "Nussbaum", "Lackiert weiss", "Lackiert farbig", "Egal / Beratung"],
            },
            {
                "name": "aufstellort",
                "label": "Wo soll es aufgestellt werden?",
                "type": "text",
                "required": False,
                "placeholder": "z.B. Wohnzimmer / Schlafzimmer",
            },
            {
                "name": "termin",
                "label": "Wann brauchst du es fertig?",
                "type": "date",
                "required": False,
            },
            {
                "name": "budget",
                "label": "Budget-Vorstellung",
                "type": "select",
                "required": False,
                "options": ["Bis 500 EUR", "500-1500 EUR", "1500-3000 EUR", "3000-5000 EUR", "Mehr / offen"],
            },
            {
                "name": "telefon",
                "label": "Deine Telefonnummer (optional, fuer Rueckrufe)",
                "type": "tel",
                "required": False,
            },
            {
                "name": "anmerkungen",
                "label": "Weitere Wuensche oder Fragen",
                "type": "textarea",
                "required": False,
            },
        ],
    },
    ANFRAGE_TYP_ALLGEMEIN: {
        "title": "Anfrage-Formular",
        "subtitle": "Damit wir dir besser helfen koennen",
        "fields": [
            {
                "name": "anliegen",
                "label": "Worum geht es?",
                "type": "textarea",
                "required": True,
                "placeholder": "Beschreib dein Anliegen kurz",
            },
            {
                "name": "termin",
                "label": "Wunsch-Termin (falls relevant)",
                "type": "date",
                "required": False,
            },
            {
                "name": "telefon",
                "label": "Telefonnummer (optional)",
                "type": "tel",
                "required": False,
            },
            {
                "name": "anmerkungen",
                "label": "Anmerkungen",
                "type": "textarea",
                "required": False,
            },
        ],
    },
}


def get_default_schema(anfrage_typ: str) -> dict:
    """Liefert das HARDCODED Default-Schema. Fallback wenn kein Tenant-Schema in DB."""
    return ANFRAGE_SCHEMAS.get(anfrage_typ) or ANFRAGE_SCHEMAS[ANFRAGE_TYP_ALLGEMEIN]


async def get_schema_for_tenant(
    tenant_id: "UUID | None",
    anfrage_typ: str,
) -> dict:
    """Liefert das Formular-Schema fuer einen Tenant + Anfrage-Typ.

    Reihenfolge:
    1. Wenn tenant_id gegeben: DB-Lookup auf tenant_anfrage_schemas
       (active = True UND tenant_id + anfrage_typ matchen)
    2. Sonst: hardcoded Default-Schema aus ANFRAGE_SCHEMAS

    Returns: {title, subtitle, fields}
    """
    if tenant_id is not None:
        try:
            from core.models import TenantAnfrageSchema
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TenantAnfrageSchema).where(
                        TenantAnfrageSchema.tenant_id == tenant_id,
                        TenantAnfrageSchema.anfrage_typ == anfrage_typ,
                        TenantAnfrageSchema.is_active.is_(True),
                    )
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    default = get_default_schema(anfrage_typ)
                    logger.info(
                        f"get_schema_for_tenant: DB-Schema gefunden fuer "
                        f"tenant={tenant_id} typ={anfrage_typ}"
                    )
                    return {
                        "title": row.title or default.get("title", "Anfrage-Formular"),
                        "subtitle": row.subtitle or default.get("subtitle", ""),
                        "fields": row.fields or default.get("fields", []),
                    }
        except Exception as e:
            logger.warning(f"DB-Schema-Lookup fehler (fallback auf Default): {e}")

    # Fallback
    return get_default_schema(anfrage_typ)


# Backwards-compat: alte sync-Funktion bleibt fuer Default-Aufrufe
def get_schema(anfrage_typ: str) -> dict:
    """Liefert das HARDCODED Default-Schema (sync, ohne DB).
    Fuer neue Calls bitte get_schema_for_tenant() nutzen.
    """
    return get_default_schema(anfrage_typ)


# =====================================================================
# Schema-Schreibweg fuer Tenant-Editor (Telegram-Wizard)
# =====================================================================

# Welche Field-Types das Form-Template kennt (vgl. anfrage_form_template.render_field)
ALLOWED_FIELD_TYPES = {
    "text", "tel", "date", "textarea",
    "radio", "checkbox_multi", "select", "masse",
}

# Reserviert weil im Mail-Pipeline / Submit-Logik anders behandelt
RESERVED_FIELD_NAMES = {"name", "email", "token"}


def validate_schema_fields(fields: list[dict]) -> tuple[bool, str]:
    """Strukturpruefung. Returns (ok, error_msg). Fuer Telegram-Wizard + DB-Schreibweg."""
    if not isinstance(fields, list) or not fields:
        return False, "Mindestens 1 Feld noetig."
    seen = set()
    for f in fields:
        if not isinstance(f, dict):
            return False, "Feld-Eintrag ist kein Dict."
        n = (f.get("name") or "").strip()
        t = (f.get("type") or "").strip()
        lab = (f.get("label") or "").strip()
        if not n or not lab or not t:
            return False, "Jedes Feld braucht name, label, type."
        if n in seen:
            return False, f"Feldname '{n}' kommt doppelt vor."
        seen.add(n)
        if n in RESERVED_FIELD_NAMES:
            return False, f"Feldname '{n}' ist reserviert."
        if t not in ALLOWED_FIELD_TYPES:
            return False, f"Unbekannter Field-Type '{t}'."
        if t in {"radio", "checkbox_multi", "select"}:
            opts = f.get("options") or []
            if not isinstance(opts, list) or len(opts) < 2:
                return False, f"Feld '{n}': mindestens 2 Optionen noetig."
    return True, ""


async def upsert_tenant_schema(
    tenant_id: UUID,
    anfrage_typ: str,
    fields: list[dict],
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
) -> tuple[bool, str]:
    """Speichert oder aktualisiert das Tenant-Schema.

    UNIQUE(tenant_id, anfrage_typ) wird vom DB-Index erzwungen.
    Returns (ok, message).
    """
    ok, err = validate_schema_fields(fields)
    if not ok:
        return False, err

    from core.models import TenantAnfrageSchema

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TenantAnfrageSchema).where(
                TenantAnfrageSchema.tenant_id == tenant_id,
                TenantAnfrageSchema.anfrage_typ == anfrage_typ,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = TenantAnfrageSchema(
                tenant_id=tenant_id,
                anfrage_typ=anfrage_typ,
                title=title,
                subtitle=subtitle,
                fields=fields,
                is_active=True,
            )
            session.add(row)
        else:
            row.fields = fields
            if title is not None:
                row.title = title
            if subtitle is not None:
                row.subtitle = subtitle
            row.is_active = True
        await session.commit()

    logger.info(
        f"upsert_tenant_schema: tenant={tenant_id} typ={anfrage_typ} "
        f"fields={len(fields)}"
    )
    return True, "ok"


async def delete_tenant_schema(tenant_id: UUID, anfrage_typ: str) -> bool:
    """Loescht das Tenant-Schema (fuer /formular_zuruecksetzen).

    get_schema_for_tenant() faellt danach automatisch auf Defaults zurueck.
    Returns True wenn ein Eintrag entfernt wurde.
    """
    from core.models import TenantAnfrageSchema

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TenantAnfrageSchema).where(
                TenantAnfrageSchema.tenant_id == tenant_id,
                TenantAnfrageSchema.anfrage_typ == anfrage_typ,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()

    logger.info(
        f"delete_tenant_schema: tenant={tenant_id} typ={anfrage_typ} "
        f"-> Default wird wieder genutzt"
    )
    return True


async def create_anfrage_token(
    tenant_id: UUID,
    kunde_email: str,
    kunde_name: Optional[str] = None,
    anfrage_typ: str = ANFRAGE_TYP_ALLGEMEIN,
    original_subject: Optional[str] = None,
    original_message_id: Optional[str] = None,
    valid_days: int = 3,
) -> AnfrageToken:
    """Erstellt einen neuen Anfrage-Token fuer einen Kunden.

    Returns: AnfrageToken-Instance (mit token-String und URL).

    Hardening (Tier-1): Default-Lebensdauer von 7 auf 3 Tage reduziert.
    Wenn der Token-Link in einer Spam-Mail oder einem geleakten Mail-
    Postfach landet, ist das Zeitfenster fuer Termin-Spoofing kuerzer.
    Caller (Mail-Antwort-Generator) kann ueberschreiben wenn ein
    laengerer Zeitraum bewusst gewollt ist.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(days=valid_days)

    async with AsyncSessionLocal() as session:
        token_obj = AnfrageToken(
            tenant_id=tenant_id,
            kunde_email=kunde_email.lower(),
            kunde_name=kunde_name,
            anfrage_typ=anfrage_typ,
            original_subject=original_subject,
            original_message_id=original_message_id,
            expires_at=expires_at,
        )
        session.add(token_obj)
        await session.commit()
        await session.refresh(token_obj)

    logger.info(
        f"AnfrageToken erstellt: tenant_id={tenant_id} kunde={kunde_email} "
        f"typ={anfrage_typ} token={token_obj.token[:10]}..."
    )
    return token_obj


def build_anfrage_url(token: str) -> str:
    """Baut die oeffentliche URL fuer das Formular."""
    base = settings.public_url.rstrip("/")
    return f"{base}/anfrage/{token}"


async def get_token_with_tenant(token_str: str) -> tuple[Optional[AnfrageToken], Optional[Tenant]]:
    """Laedt Token + Tenant. Returns (None, None) wenn ungueltig/abgelaufen."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AnfrageToken).where(AnfrageToken.token == token_str)
        )
        token_obj = result.scalar_one_or_none()
        if not token_obj:
            return None, None

        # Abgelaufen?
        now = datetime.now(timezone.utc)
        expires = token_obj.expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires and expires < now:
            return None, None

        # Schon abgesendet?
        if token_obj.submitted_at is not None:
            return token_obj, None  # signalisiert: schon submitted

        # Tenant laden
        t_result = await session.execute(
            select(Tenant).where(Tenant.id == token_obj.tenant_id)
        )
        tenant = t_result.scalar_one_or_none()
        return token_obj, tenant


async def submit_anfrage(
    token_str: str,
    antworten: dict,
    submitted_ip: Optional[str] = None,
) -> tuple[bool, str]:
    """Speichert die Antworten zu einem Token. Returns (success, message).

    Schutz gegen Double-Submit-Race: SELECT FOR UPDATE serialisiert
    parallele POSTs auf den gleichen Token, sodass der zweite garantiert
    `submitted_at IS NOT NULL` sieht und mit "Schon abgesendet" abbricht.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AnfrageToken)
            .where(AnfrageToken.token == token_str)
            .with_for_update()
        )
        token_obj = result.scalar_one_or_none()
        if not token_obj:
            return False, "Token unbekannt"

        if token_obj.submitted_at:
            return False, "Schon abgesendet"

        now = datetime.now(timezone.utc)
        expires = token_obj.expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires and expires < now:
            return False, "Token abgelaufen"

        response = AnfrageResponse(
            token_id=token_obj.id,
            antworten=antworten,
            submitted_ip=submitted_ip[:50] if submitted_ip else None,
        )
        session.add(response)
        token_obj.submitted_at = now
        await session.commit()

    logger.info(
        f"Anfrage abgesendet: token={token_str[:10]}... fields={list(antworten.keys())}"
    )
    return True, "OK"
