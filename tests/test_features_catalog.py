"""Konsistenz-Tests fuer den Feature-Catalog.

Diese Tests laufen rein in-process (kein DB-Zugriff). Sie pruefen dass:
- FEATURES[key].key == key
- jedes Feature.requires nur existierende Features referenziert
- jeder Telegram-Befehl exklusiv zu einem Feature gehoert

Es gibt keine Pakete/Tiers mehr — jeder Tenant wird per Feature einzeln
konfiguriert (Admin-UI bzw. Default-Set in scripts/onboard.py).
"""
from __future__ import annotations

from core.features.catalog import (
    FEATURES,
    COMMAND_TO_FEATURE,
)


# =====================================================================
# Catalog-Integritaet
# =====================================================================


def test_feature_keys_match_dict_keys() -> None:
    """FEATURES[key].key == key (sonst verwirrend)."""
    for key, feature in FEATURES.items():
        assert feature.key == key, (
            f"FEATURES['{key}'].key == '{feature.key}' — muss matchen"
        )


def test_feature_requires_are_known() -> None:
    """Jedes Feature.requires referenziert nur existierende Features."""
    for f_key, feature in FEATURES.items():
        for req in feature.requires:
            assert req in FEATURES, (
                f"Feature '{f_key}' braucht unbekanntes '{req}'"
            )


# =====================================================================
# Telegram-Command-Mapping
# =====================================================================


def test_command_to_feature_no_duplicates() -> None:
    """Jeder Telegram-Befehl gehoert zu genau einem Feature."""
    seen: dict[str, str] = {}
    for f_key, feature in FEATURES.items():
        for cmd in feature.telegram_commands:
            if cmd in seen:
                raise AssertionError(
                    f"Telegram-Befehl '{cmd}' ist zu Feature "
                    f"'{seen[cmd]}' UND '{f_key}' zugeordnet"
                )
            seen[cmd] = f_key


def test_command_to_feature_lookup_works() -> None:
    """COMMAND_TO_FEATURE ist konsistent mit FEATURES."""
    assert COMMAND_TO_FEATURE.get("/help") == "telegram_bot"
    assert COMMAND_TO_FEATURE.get("/archiv") == "drive_archiv"
    assert COMMAND_TO_FEATURE.get("/visualisierung") == "visualisierung"
    assert COMMAND_TO_FEATURE.get("/material") == "material"
    assert COMMAND_TO_FEATURE.get("/aufnahme") == "voice_init"
