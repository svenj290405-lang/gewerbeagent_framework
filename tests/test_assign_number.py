"""Tests fuer scripts/assign_number.py — E.164-Normalisierung (Beta-1 B1-1).

Pure Python — keine DB-Tests (die brauchen Postgres).
"""
from __future__ import annotations

import pytest

from scripts.assign_number import normalize_to_e164


@pytest.mark.parametrize("raw, expected", [
    ("+4965021234", "+4965021234"),
    ("065021234", "+4965021234"),
    ("0049 6502 1234", "+4965021234"),
    ("+49 (650) 2-1234", "+4965021234"),
    ("00 49 6502 1234", "+4965021234"),
    ("  +491721234567  ", "+491721234567"),
    ("030/12345678", "+493012345678"),
    ("+4915123456789", "+4915123456789"),
])
def test_normalize_positive_cases(raw, expected):
    assert normalize_to_e164(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "123",          # zu kurz
    "abc",          # keine Ziffer
    "0987",         # zu kurz fuer DE
    "+",            # nur +
])
def test_normalize_negative_cases(raw):
    with pytest.raises(ValueError):
        normalize_to_e164(raw)


def test_normalize_strips_brackets_and_spaces():
    assert normalize_to_e164("+49 (172) 1234-567") == "+491721234567"
