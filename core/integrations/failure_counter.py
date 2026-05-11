"""In-Memory Failure-Counter mit Sliding-Window.

Wird benutzt um stumme Pipeline-Failures (Drive-Upload, Mail-
Klassifikation) zu erkennen, ohne dass ein DB-Roundtrip pro Fehler
noetig ist. Ueberlebt KEINEN Container-Restart — das ist Absicht:
direkt nach Restart wollen wir keine Alert-Salve.

API:
    counter = FailureCounter("drive_upload", window_minutes=60,
                             threshold=5, cooldown_minutes=60)

    await counter.record_failure(key=str(tenant_id), reason="…")
    # gibt True zurueck wenn Schwelle + Cooldown durch sind und der
    # Caller einen Alert versenden soll.

    counter.reset(key=str(tenant_id))
    # bei erfolgreichem Call — Sliding-Window wegwerfen.

Thread-/Async-Safety: alle Operationen sind atomar via threading.Lock
(reicht weil dict-Ops in CPython sowieso GIL-geschuetzt sind, aber wir
wollen Atomar-ueber-Prune-then-Append).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import defaultdict

logger = logging.getLogger(__name__)


class FailureCounter:
    """Sliding-Window-Counter pro Schluessel.

    Ein Schluessel kann z.B. der String einer Tenant-UUID sein. Pro
    Schluessel wird eine Liste von Failure-Timestamps gepflegt.
    `record_failure` returnt True genau dann wenn:
      (a) im laufenden Fenster sind >= threshold Failures angelaufen
      (b) der letzte Alert fuer diesen Schluessel ist >= cooldown her
    """

    def __init__(
        self,
        name: str,
        *,
        window_minutes: int,
        threshold: int,
        cooldown_minutes: int,
    ) -> None:
        self.name = name
        self.window = dt.timedelta(minutes=window_minutes)
        self.threshold = threshold
        self.cooldown = dt.timedelta(minutes=cooldown_minutes)
        self._timestamps: dict[str, list[dt.datetime]] = defaultdict(list)
        self._last_alert_at: dict[str, dt.datetime] = {}
        self._last_reason: dict[str, str] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: dt.datetime) -> None:
        cutoff = now - self.window
        lst = self._timestamps[key]
        # Liste sortiert (append-only mit monoton-steigender Zeit) — vom
        # Anfang her droppen.
        i = 0
        while i < len(lst) and lst[i] < cutoff:
            i += 1
        if i > 0:
            del lst[:i]

    def record_failure(
        self, *, key: str, reason: str = "",
    ) -> tuple[bool, int]:
        """Fügt einen Failure hinzu, prüft Schwelle + Cooldown.

        Returns:
            (should_alert, current_count)
              should_alert: True wenn der Caller jetzt einen Alert
                            schicken sollte. False wenn Schwelle nicht
                            erreicht ODER Cooldown noch laeuft.
              current_count: aktuelle Failure-Anzahl im Fenster.
        """
        now = dt.datetime.now(dt.timezone.utc)
        with self._lock:
            self._prune(key, now)
            self._timestamps[key].append(now)
            self._last_reason[key] = reason[:200]
            count = len(self._timestamps[key])

            if count < self.threshold:
                return False, count

            last_alert = self._last_alert_at.get(key)
            if last_alert and (now - last_alert) < self.cooldown:
                return False, count

            # Schwelle + Cooldown ok — Caller darf Alert schicken.
            self._last_alert_at[key] = now
            return True, count

    def get_last_reason(self, key: str) -> str:
        with self._lock:
            return self._last_reason.get(key, "")

    def reset(self, *, key: str) -> None:
        """Bei Erfolg: Fenster + Cooldown fuer diesen Schluessel löschen."""
        with self._lock:
            self._timestamps.pop(key, None)
            self._last_alert_at.pop(key, None)
            self._last_reason.pop(key, None)


# Vordefinierte Counter — pro Pipeline einer.
# Drive-Upload: nach 5 Failures pro Tenant pro Stunde → Alert.
DRIVE_UPLOAD_FAILURES = FailureCounter(
    "drive_upload", window_minutes=60, threshold=5, cooldown_minutes=60,
)

# Mail-Klassifikation: nach 3 Failures pro Tenant pro 24h → Alert.
MAIL_CLASSIFY_FAILURES = FailureCounter(
    "mail_classify", window_minutes=24 * 60, threshold=3,
    cooldown_minutes=24 * 60,
)
