"""Smoke-Tests fuer Phase-B-Module.

Importe + pure-Python-Logik. Keine DB / HTTP-Mocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_logging_context_filters():
    """TenantContextFilter haengt context an LogRecord."""
    import logging
    from core.logging_context import (
        TenantContextFilter, set_log_employee, set_log_tenant,
    )

    set_log_tenant("406013f4-2854-4099-b3b1-41131a81fef2")
    set_log_employee("abc12345-aaaa-bbbb-cccc-dddddddddddd")

    record = logging.LogRecord(
        "test", logging.INFO, "x", 1, "hello", None, None,
    )
    TenantContextFilter().filter(record)
    assert record.tenant == "406013f4"
    assert record.employee == "abc12345"

    # Reset
    set_log_tenant(None)
    set_log_employee(None)
    record = logging.LogRecord("t", logging.INFO, "x", 1, "x", None, None)
    TenantContextFilter().filter(record)
    assert record.tenant == "—"
    assert record.employee == "—"


def test_log_tenant_context_manager():
    """log_tenant setzt + restored bei nested calls."""
    from core.logging_context import log_tenant, get_log_context

    assert get_log_context()["tenant"] is None
    with log_tenant("aaa-bbb-ccc"):
        assert get_log_context()["tenant"] == "aaabbbcc"  # 8 chars after strip
        with log_tenant("xxx-yyy"):
            assert get_log_context()["tenant"] == "xxxyyy"
        # restored
        assert get_log_context()["tenant"] == "aaabbbcc"
    # restored outer
    assert get_log_context()["tenant"] is None


def test_verify_magic_bytes_accepts_real_jpegs():
    from core.integrations.anfrage_forms import verify_magic_bytes
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
    assert verify_magic_bytes(jpeg, claimed_content_type="image/jpeg") is True
    # Aber NICHT als PNG
    assert verify_magic_bytes(jpeg, claimed_content_type="image/png") is False


def test_verify_magic_bytes_rejects_exe_disguised_as_jpeg():
    from core.integrations.anfrage_forms import verify_magic_bytes
    exe = b"MZ\x90\x00\x03\x00\x00\x00"
    assert verify_magic_bytes(exe, claimed_content_type="image/jpeg") is False


def test_verify_magic_bytes_rejects_unknown_mime():
    from core.integrations.anfrage_forms import verify_magic_bytes
    pdf = b"%PDF-1.4\n%abc"
    assert verify_magic_bytes(pdf, claimed_content_type="application/pdf") is True
    # text/html ist nicht erlaubt
    assert verify_magic_bytes(pdf, claimed_content_type="text/html") is False


def test_verify_magic_bytes_webp_full_header():
    from core.integrations.anfrage_forms import verify_magic_bytes
    # 12-byte WebP-Header: RIFF + size + WEBP
    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 50
    assert verify_magic_bytes(webp, claimed_content_type="image/webp") is True
    # Aber nur "RIFF" am Anfang reicht NICHT
    fake = b"RIFF\x00\x00\x00\x00AVI " + b"x" * 50
    assert verify_magic_bytes(fake, claimed_content_type="image/webp") is False


def test_branche_templates_are_well_formed():
    """Alle scripts/templates/branche_*.json muessen gueltig + komplett sein."""
    from core.models import ALLE_KATEGORIEN
    templates_dir = Path(__file__).parent.parent / "scripts" / "templates"
    files = list(templates_dir.glob("branche_*.json"))
    assert len(files) >= 3, f"Erwartet mindestens 3 Branchen-Templates, gefunden: {len(files)}"
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "key" in data and "label" in data
        assert isinstance(data.get("knowledge"), list)
        for entry in data["knowledge"]:
            assert "kategorie" in entry and "text" in entry
            assert entry["kategorie"] in ALLE_KATEGORIEN, (
                f"{f.name}: unbekannte Kategorie {entry['kategorie']}"
            )
            assert len(entry["text"]) <= 2000


def test_sentry_init_returns_false_without_dsn(monkeypatch):
    """init_sentry ist no-op wenn SENTRY_DSN leer."""
    from core.integrations.error_tracking import init_sentry
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False


def test_status_routes_registered():
    """B6 Status-Page-Routes sind im Router."""
    from core.api.status_routes import router
    paths = [r.path for r in router.routes]
    assert "/api/status" in paths
    assert "/status" in paths


def test_db_maintenance_constants():
    """B3 Retention-Konstanten sind plausibel."""
    from core.integrations import db_maintenance_cron as dbm
    assert dbm.AUDIT_LOG_RETENTION_DAYS == 180
    assert dbm.OAUTH_STATE_RETENTION_DAYS == 7
    assert dbm.VISUALISIERUNG_BLOB_RETENTION_DAYS == 90
    assert dbm.MAINTENANCE_HOUR_LOCAL == 2  # NICHT 03:00 (DSGVO-Konflikt)


def test_ors_quota_alert_helper_callable():
    """B9 _maybe_alert_quota_exhausted darf importiert + aufgerufen werden."""
    import asyncio
    from core.integrations.openrouteservice import _maybe_alert_quota_exhausted
    # Failsafe-Pfad: kein Token gesetzt → silent skip, kein Crash
    asyncio.run(_maybe_alert_quota_exhausted())


def test_rotate_encryption_key_module_imports():
    """B11 Rotation-Skript muss importierbar sein."""
    from scripts import rotate_encryption_key
    assert hasattr(rotate_encryption_key, "cli")
    assert hasattr(rotate_encryption_key, "_recrypt")
    assert hasattr(rotate_encryption_key, "_fernet_for")
