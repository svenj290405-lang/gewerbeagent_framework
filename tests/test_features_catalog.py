"""Konsistenz-Tests fuer den Feature-Catalog.

Diese Tests laufen rein in-process (kein DB-Zugriff). Sie pruefen dass:
- jedes Feature in einem Paket auch im Catalog existiert
- jedes Paket monoton waechst (Basis ⊂ Pro ⊂ Enterprise)
- jeder Telegram-Befehl exklusiv zu einem Feature gehoert
- detect_package_from_features die 3 Pakete sauber zurueckgibt
"""
from __future__ import annotations

from core.features.catalog import (
    FEATURES,
    PACKAGES,
    PACKAGE_BASIS,
    PACKAGE_PRO,
    PACKAGE_ENTERPRISE,
    PACKAGE_CUSTOM,
    COMMAND_TO_FEATURE,
    features_in_package,
)
from core.features.check import detect_package_from_features


# =====================================================================
# Catalog-Integritaet
# =====================================================================


def test_every_package_feature_is_in_catalog() -> None:
    """Jedes Feature in einem Paket muss auch in FEATURES existieren."""
    for pkg_name, features in PACKAGES.items():
        for f_key in features:
            assert f_key in FEATURES, (
                f"Paket '{pkg_name}' enthaelt unbekanntes Feature '{f_key}'"
            )


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
# Paket-Hierarchie
# =====================================================================


def test_packages_are_monotonic() -> None:
    """Basis ⊂ Pro ⊂ Enterprise. Wer mehr zahlt, kriegt mindestens
    alles vom kleineren Paket."""
    basis = PACKAGES[PACKAGE_BASIS]
    pro = PACKAGES[PACKAGE_PRO]
    enterprise = PACKAGES[PACKAGE_ENTERPRISE]

    assert basis.issubset(pro), (
        f"Basis hat Features die Pro nicht hat: {basis - pro}"
    )
    assert pro.issubset(enterprise), (
        f"Pro hat Features die Enterprise nicht hat: {pro - enterprise}"
    )


def test_package_custom_is_not_in_packages() -> None:
    """PACKAGE_CUSTOM hat keine feste Feature-Liste."""
    assert PACKAGE_CUSTOM not in PACKAGES


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


# =====================================================================
# detect_package_from_features
# =====================================================================


def test_detect_basis() -> None:
    enabled = features_in_package(PACKAGE_BASIS)
    assert detect_package_from_features(enabled) == PACKAGE_BASIS


def test_detect_pro() -> None:
    enabled = features_in_package(PACKAGE_PRO)
    assert detect_package_from_features(enabled) == PACKAGE_PRO


def test_detect_enterprise() -> None:
    enabled = features_in_package(PACKAGE_ENTERPRISE)
    assert detect_package_from_features(enabled) == PACKAGE_ENTERPRISE


def test_detect_custom_when_not_matching() -> None:
    """Pro + drive_archiv (ohne den Rest von Enterprise) -> custom."""
    enabled = PACKAGES[PACKAGE_PRO] | frozenset({"drive_archiv"})
    assert detect_package_from_features(enabled) == PACKAGE_CUSTOM


def test_detect_custom_when_subset() -> None:
    """Pro minus 'lexware' ist kein vorgesehener Tier -> custom."""
    enabled = PACKAGES[PACKAGE_PRO] - frozenset({"lexware"})
    assert detect_package_from_features(enabled) == PACKAGE_CUSTOM
