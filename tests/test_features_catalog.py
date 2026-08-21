"""Konsistenz-Tests fuer den Feature-Catalog.

Diese Tests laufen rein in-process (kein DB-Zugriff). Sie pruefen dass:
- FEATURES[key].key == key
- jedes Feature.requires nur existierende Features referenziert

Es gibt keine Pakete/Tiers mehr — jeder Tenant wird per Feature einzeln
konfiguriert (Admin-UI bzw. Default-Set in scripts/onboard.py).
"""
from __future__ import annotations

from core.features.catalog import FEATURES


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
