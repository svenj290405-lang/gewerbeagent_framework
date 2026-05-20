"""Guard: 'werkstatt' (Smart-Routing) ist deaktiviert.

Verhindert versehentliches Reaktivieren. Das Feature bleibt im Katalog
definiert (dormant, reversibel) — per Kill-Switch global aus. Die
Geschaeftsadresse wird weiterhin im Onboarding fuers Impressum erfasst
(nicht hier getestet — siehe Onboarding-Flow).
"""
from __future__ import annotations

from core.features.catalog import FEATURES
from core.features.check import GLOBALLY_DISABLED_FEATURES


def test_werkstatt_global_kill_switch():
    assert "werkstatt" in GLOBALLY_DISABLED_FEATURES


def test_werkstatt_feature_still_defined_dormant():
    # Definition bleibt erhalten, damit Reaktivieren = Kill-Switch leeren.
    assert "werkstatt" in FEATURES
