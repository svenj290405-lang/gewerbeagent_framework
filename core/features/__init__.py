"""Feature-Catalog + per-Tenant-Toggle-Helpers.

Single-Source-of-Truth fuer alle Features die das System anbieten kann
plus die 3 vordefinierten Pakete (Basis/Pro/Enterprise). Pro Tenant wird
in `tool_configs` (tool_name == feature.key) gespeichert ob das Feature
aktiv ist.
"""
from core.features.catalog import (
    Feature,
    FEATURES,
    PACKAGES,
    PACKAGE_BASIS,
    PACKAGE_PRO,
    PACKAGE_ENTERPRISE,
    PACKAGE_CUSTOM,
    ALL_PACKAGES,
)
from core.features.check import (
    is_feature_enabled,
    enabled_features_for_tenant,
    apply_package,
    detect_package_from_features,
    invalidate_feature_cache,
)

__all__ = [
    "Feature",
    "FEATURES",
    "PACKAGES",
    "PACKAGE_BASIS",
    "PACKAGE_PRO",
    "PACKAGE_ENTERPRISE",
    "PACKAGE_CUSTOM",
    "ALL_PACKAGES",
    "is_feature_enabled",
    "enabled_features_for_tenant",
    "apply_package",
    "detect_package_from_features",
    "invalidate_feature_cache",
]
