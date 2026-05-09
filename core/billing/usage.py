"""
Usage-Tracking + Preis-Lookup.

Design:
- track_api_usage(...) ist async und failsafe: niemals einen API-Call brechen
  weil Tracking fehlgeschlagen ist. Im Fehlerfall: warnen und weiter.
- get_current_price(provider, operation, unit) liest immer den gerade
  gueltigen Preis aus api_pricing_config (valid_from <= now < valid_to).
- Convenience-Wrapper fuer die haeufigsten Provider:
    track_gemini_response(usage_metadata, model, tenant_id)
    track_elevenlabs_chars(char_count, voice, tenant_id)
    track_deepgram_seconds(seconds, model, tenant_id)
    track_mail_send(provider, tenant_id)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.connection import get_session
from core.models.admin import (
    ApiPricingConfig,
    ApiUsageLog,
    PROVIDER_DEEPGRAM,
    PROVIDER_ELEVENLABS,
    PROVIDER_GEMINI,
    UNIT_CACHED_INPUT_TOKEN,
    UNIT_CHARACTER,
    UNIT_INPUT_TOKEN,
    UNIT_MAIL_SEND,
    UNIT_OUTPUT_TOKEN,
    UNIT_SECOND,
)

logger = logging.getLogger(__name__)


# ---------- in-process Cache fuer Preise (60s TTL) ----------
_PRICE_CACHE: dict[tuple[str, str | None, str], tuple[float, ApiPricingConfig | None]] = {}
_CACHE_TTL_SECONDS = 60.0


async def get_current_price(
    provider: str,
    operation: str | None,
    unit: str,
    *,
    session: AsyncSession | None = None,
) -> ApiPricingConfig | None:
    """
    Liefert die gerade gueltige Pricing-Config-Zeile fuer (provider, operation, unit).

    Lookup-Reihenfolge:
    1) (provider, operation, unit) - exakter Match
    2) (provider, NULL, unit) - allgemeiner Provider-Preis
    """
    cache_key = (provider, operation, unit)
    cached = _PRICE_CACHE.get(cache_key)
    if cached:
        ts, cfg = cached
        if (asyncio.get_event_loop().time() - ts) < _CACHE_TTL_SECONDS:
            return cfg

    async def _fetch(s: AsyncSession) -> ApiPricingConfig | None:
        now = dt.datetime.now(dt.timezone.utc)

        # 1) exakter Match
        stmt = (
            select(ApiPricingConfig)
            .where(ApiPricingConfig.provider == provider)
            .where(ApiPricingConfig.unit == unit)
            .where(ApiPricingConfig.operation == operation)
            .where(ApiPricingConfig.valid_from <= now)
            .where(
                (ApiPricingConfig.valid_to.is_(None))
                | (ApiPricingConfig.valid_to > now)
            )
            .order_by(ApiPricingConfig.valid_from.desc())
            .limit(1)
        )
        row = (await s.execute(stmt)).scalar_one_or_none()
        if row:
            return row

        # 2) Fallback: provider+unit ohne operation (operation IS NULL)
        stmt2 = (
            select(ApiPricingConfig)
            .where(ApiPricingConfig.provider == provider)
            .where(ApiPricingConfig.unit == unit)
            .where(ApiPricingConfig.operation.is_(None))
            .where(ApiPricingConfig.valid_from <= now)
            .where(
                (ApiPricingConfig.valid_to.is_(None))
                | (ApiPricingConfig.valid_to > now)
            )
            .order_by(ApiPricingConfig.valid_from.desc())
            .limit(1)
        )
        return (await s.execute(stmt2)).scalar_one_or_none()

    if session is not None:
        cfg = await _fetch(session)
    else:
        async with get_session() as s:
            cfg = await _fetch(s)

    _PRICE_CACHE[cache_key] = (asyncio.get_event_loop().time(), cfg)
    return cfg


def _invalidate_price_cache() -> None:
    """Vom Pricing-Editor aufgerufen wenn Preise sich aendern."""
    _PRICE_CACHE.clear()


async def track_api_usage(
    *,
    tenant_id: uuid.UUID | str | None,
    provider: str,
    operation: str | None,
    units: int | float | Decimal,
    unit: str,
    request_id: str | None = None,
    metadata: dict | None = None,
) -> ApiUsageLog | None:
    """
    Schreibt eine Verbrauchszeile + berechnete Kosten in api_usage_log.

    Failsafe - schluckt Fehler und loggt sie. Niemals den eigentlichen
    API-Call ausbremsen.
    """
    try:
        units_dec = Decimal(str(units))
        if units_dec <= 0:
            return None

        # Tenant-ID normalisieren
        tid: uuid.UUID | None = None
        if tenant_id is not None:
            tid = (
                tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
            )

        async with get_session() as s:
            cfg = await get_current_price(provider, operation, unit, session=s)
            price = cfg.price_per_unit_eur if cfg else Decimal("0")
            cost = (price * units_dec).quantize(Decimal("0.00000001"))

            row = ApiUsageLog(
                tenant_id=tid,
                provider=provider,
                operation=operation,
                unit=unit,
                units_consumed=units_dec,
                price_per_unit_eur=price,
                cost_eur=cost,
                pricing_config_id=cfg.id if cfg else None,
                request_id=request_id,
                metadata_json=metadata,
            )
            s.add(row)
            await s.flush()
            return row
    except Exception as e:
        logger.warning(
            f"track_api_usage failed silently: provider={provider} "
            f"operation={operation} unit={unit} units={units} err={e}"
        )
        return None


def track_api_usage_sync(
    *,
    tenant_id: uuid.UUID | str | None,
    provider: str,
    operation: str | None,
    units: int | float | Decimal,
    unit: str,
    request_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Sync-Wrapper: schedulet Tracking als Hintergrund-Task ohne zu blockieren.

    Fuer Code-Pfade, die nicht async sind oder die wir auch in Background-
    Threads aufrufen. Faellt zurueck auf asyncio.run wenn keine Loop laeuft.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(track_api_usage(
            tenant_id=tenant_id,
            provider=provider,
            operation=operation,
            units=units,
            unit=unit,
            request_id=request_id,
            metadata=metadata,
        ))
    except RuntimeError:
        # Keine Loop - sync ausfuehren
        try:
            asyncio.run(track_api_usage(
                tenant_id=tenant_id,
                provider=provider,
                operation=operation,
                units=units,
                unit=unit,
                request_id=request_id,
                metadata=metadata,
            ))
        except Exception as e:
            logger.warning(f"track_api_usage_sync run failed: {e}")


# =====================================================================
# Convenience-Wrapper pro Provider
# =====================================================================

async def track_gemini_response(
    response: Any,
    *,
    model: str = "gemini-2.5-flash",
    tenant_id: uuid.UUID | str | None = None,
    operation_kind: str | None = None,
) -> None:
    """
    Liest usage_metadata aus einer Gemini-Response und logged Input/Output-Tokens.

    Robust gegen unterschiedliche SDK-Shapes (vertexai vs google.genai):
    - response.usage_metadata.prompt_token_count
    - response.usage_metadata.candidates_token_count
    - response.usage_metadata.cached_content_token_count (optional)
    """
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return

        # Verschiedene Property-Namen versuchen
        prompt_tokens = (
            getattr(usage, "prompt_token_count", None)
            or getattr(usage, "input_tokens", None)
            or 0
        )
        out_tokens = (
            getattr(usage, "candidates_token_count", None)
            or getattr(usage, "output_tokens", None)
            or 0
        )
        cached_tokens = (
            getattr(usage, "cached_content_token_count", None)
            or 0
        )

        op = operation_kind or model
        if prompt_tokens:
            await track_api_usage(
                tenant_id=tenant_id,
                provider=PROVIDER_GEMINI,
                operation=model,
                units=int(prompt_tokens),
                unit=UNIT_INPUT_TOKEN,
                metadata={"kind": op},
            )
        if out_tokens:
            await track_api_usage(
                tenant_id=tenant_id,
                provider=PROVIDER_GEMINI,
                operation=model,
                units=int(out_tokens),
                unit=UNIT_OUTPUT_TOKEN,
                metadata={"kind": op},
            )
        if cached_tokens:
            await track_api_usage(
                tenant_id=tenant_id,
                provider=PROVIDER_GEMINI,
                operation=model,
                units=int(cached_tokens),
                unit=UNIT_CACHED_INPUT_TOKEN,
                metadata={"kind": op},
            )
    except Exception as e:
        logger.debug(f"track_gemini_response: kein usage_metadata extractable ({e})")


async def track_elevenlabs_chars(
    char_count: int,
    *,
    voice: str = "default",
    tenant_id: uuid.UUID | str | None = None,
) -> None:
    """ElevenLabs rechnet pro generiertem Zeichen ab."""
    if char_count <= 0:
        return
    await track_api_usage(
        tenant_id=tenant_id,
        provider=PROVIDER_ELEVENLABS,
        operation="tts-default",
        units=char_count,
        unit=UNIT_CHARACTER,
        metadata={"voice": voice},
    )


async def track_deepgram_seconds(
    seconds: float,
    *,
    model: str = "nova-3-streaming",
    tenant_id: uuid.UUID | str | None = None,
) -> None:
    """Deepgram rechnet pro Audio-Sekunde ab."""
    if seconds <= 0:
        return
    await track_api_usage(
        tenant_id=tenant_id,
        provider=PROVIDER_DEEPGRAM,
        operation=model,
        units=Decimal(str(round(seconds, 3))),
        unit=UNIT_SECOND,
    )


async def track_mail_send(
    provider: str,
    *,
    tenant_id: uuid.UUID | str | None = None,
    recipient_count: int = 1,
    operation: str = "transactional-mail",
) -> None:
    """Eine Mail-Versand-Zeile pro Mail."""
    await track_api_usage(
        tenant_id=tenant_id,
        provider=provider,
        operation=operation,
        units=recipient_count,
        unit=UNIT_MAIL_SEND,
    )
