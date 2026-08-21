"""Tests fuer das token-basierte Inhaber-Onboarding (S13).

Deckt:
- Kurzer Aktivierungs-Code: Erzeugung, Normalisierung, Formatierung
  sowie die Vor-Validierung in consume_activation_code.
- core.onboarding.create_tenant_record: Slug-Validierung (vor DB-Zugriff).

Die frueheren Tests der `/start`-Bindung im Telegram-Bot sind mit dem
Bot entfallen (2026-08-21); der Aktivierungs-Link fuehrt jetzt nach
`/app/activate?token=…`.
"""
from __future__ import annotations

import pytest


# =====================================================================
# Kurzer Aktivierungs-Code
# =====================================================================

def test_short_code_helpers():
    from core.models.employee_activation_token import (
        _generate_short_code, _SHORT_CODE_ALPHABET, _SHORT_CODE_LEN,
        normalize_short_code, format_short_code,
    )
    code = _generate_short_code()
    assert len(code) == _SHORT_CODE_LEN
    assert all(c in _SHORT_CODE_ALPHABET for c in code)
    # normalize: Grossschrift, Bindestrich/Leerzeichen weg
    assert normalize_short_code(" k7p4-9x2m ") == "K7P49X2M"
    # format: XXXX-XXXX
    assert format_short_code("K7P49X2M") == "K7P4-9X2M"


@pytest.mark.asyncio
async def test_consume_activation_code_bad_format_returns_none():
    """Falsche Laenge -> None, ohne DB-Zugriff (Vor-Validierung)."""
    from core.models import consume_activation_code
    assert await consume_activation_code("abc") is None
    assert await consume_activation_code("") is None


# =====================================================================
# Tenant-Anlage
# =====================================================================

@pytest.mark.asyncio
async def test_create_tenant_record_rejects_bad_slug():
    """Slug-Validierung greift VOR jedem DB-Zugriff."""
    from core.onboarding import OnboardingError, create_tenant_record
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="x", name="A", email="a@b.de", contact="C",
        )  # zu kurz
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="hat leerzeichen", name="A", email="a@b.de", contact="C",
        )  # ungueltiges Zeichen
    with pytest.raises(OnboardingError):
        await create_tenant_record(
            slug="_global", name="A", email="a@b.de", contact="C",
        )  # reserviert
