"""Test fuer den chat_id-Coercion-Guard in get_employee_by_telegram_chat.

telegram_chat_id ist eine bigint-Spalte. Eine String-Chat-ID wuerde sonst
"bigint = varchar" werfen und die Tenant-Aufloesung still auf _global
zuruckfallen lassen. Nicht-numerische Eingaben muessen sauber None
liefern (ohne DB-Zugriff).
"""
from __future__ import annotations

import pytest

from core.models.employee import get_employee_by_telegram_chat


@pytest.mark.asyncio
async def test_nonnumeric_chat_id_returns_none_without_db():
    # int("abc") -> ValueError -> None, noch vor jedem DB-Zugriff
    assert await get_employee_by_telegram_chat("abc") is None


@pytest.mark.asyncio
async def test_none_chat_id_returns_none():
    assert await get_employee_by_telegram_chat(None) is None
