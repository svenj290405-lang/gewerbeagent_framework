"""Feature-Catalog + per-Tenant-Toggle-Helpers.

Single-Source-of-Truth fuer alle Features die das System anbieten kann.
Pro Tenant wird in `tool_configs` (tool_name == feature.key) gespeichert
ob das Feature aktiv ist — es gibt keine vordefinierten Pakete/Tiers,
jeder Tenant wird per Feature einzeln konfiguriert.
"""
from core.features.catalog import (
    Feature,
    FEATURES,
)
from core.features.check import (
    is_feature_enabled,
    enabled_features_for_tenant,
    invalidate_feature_cache,
)

__all__ = [
    "Feature",
    "FEATURES",
    "is_feature_enabled",
    "enabled_features_for_tenant",
    "invalidate_feature_cache",
]
