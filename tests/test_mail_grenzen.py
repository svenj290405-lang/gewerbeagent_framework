"""Zwei Wege, auf denen Fremde die Mail-Automatik steuern konnten.

Beide folgen demselben Muster: etwas, das ein Beliebiger von aussen setzen
kann (die From-Adresse, ein Feld in Geminis Antwort), loeste eine Aktion
mit echten Folgen aus, ohne dass ein Mensch dazwischen war.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.ai.command_center as cc


# =====================================================================
# Storno per Mail: erst der Absender, dann der Kalender
# =====================================================================

def _msg(auth_results: str | None):
    kopf = []
    if auth_results is not None:
        kopf.append({"name": "Authentication-Results", "value": auth_results})
    return {"internetMessageHeaders": kopf}


@pytest.mark.parametrize("zeile,erwartet", [
    ("spf=pass (sender ip) smtp.mailfrom=kunde.de; dkim=pass; dmarc=pass", True),
    ("spf=fail; dkim=fail; dmarc=fail action=none", False),
    ("spf=fail smtp.mailfrom=kunde.de; dkim=fail header.d=kunde.de", False),
    ("spf=none; dkim=none; dmarc=none", None),
    ("dkim=pass header.d=gmx.de", True),
])
def test_absender_urteil(zeile, erwartet):
    from core.integrations.microsoft_inbox import absender_authentisch

    echt, _grund = absender_authentisch(_msg(zeile))
    assert echt is erwartet


def test_ohne_header_kein_urteil():
    """Fehlende Information darf nichts blockieren — sonst haette ein
    Graph-Ausfall jede echte Absage verschluckt."""
    from core.integrations.microsoft_inbox import absender_authentisch

    echt, grund = absender_authentisch(_msg(None))
    assert echt is None
    assert "keine" in grund.lower()


@pytest.mark.asyncio
async def test_gefaelschter_absender_storniert_nichts(monkeypatch):
    """Der Kern des Fundes.

    `cancel_kunde_termine` sucht die Termine ueber die From-Adresse — wer
    sie faelscht, loescht also die Termine des echten Kunden. Ein
    Keyword-Backup setzt den Storno-Intent sogar unabhaengig von Geminis
    Urteil, sobald "stornieren" im Text steht.
    """
    import core.integrations.microsoft_inbox as inbox

    cancel = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "core.integrations.mail_pipeline.cancel_kunde_termine", cancel)
    monkeypatch.setattr(
        "core.integrations.mail_pipeline.push_tenant_new_anfrage_notification",
        AsyncMock(return_value=None))
    monkeypatch.setattr(
        inbox, "fetch_full_message",
        AsyncMock(return_value=_msg("spf=fail; dkim=fail; dmarc=fail")))

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot", company_name="Jantos")
    res = await inbox._handle_storno_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="AAMk123",
        sender_email="kunde@example.de", sender_name="Kunde",
        subject="Termin stornieren", body_preview="bitte absagen",
        existing_conv=None, employee_id=None, ms_conversation_id=None,
        classification="RELEVANT_KUNDE", confidence="high", reason="—",
        categories=[],
    )
    assert cancel.await_count == 0, "Bei nicht bestaetigtem Absender darf nichts storniert werden."
    assert res["action"] == "storno_abgelehnt_absender_ungeprueft"


@pytest.mark.asyncio
async def test_bestaetigter_absender_darf_stornieren(monkeypatch):
    """Der Normalfall bleibt unberuehrt."""
    import core.integrations.microsoft_inbox as inbox

    cancel = AsyncMock(return_value=["ev1"])
    monkeypatch.setattr(
        "core.integrations.mail_pipeline.cancel_kunde_termine", cancel)
    for name in ("create_conversation", "record_inbound",
                 "record_outbound_q_reply", "set_conversation_state",
                 "push_tenant_intent_event"):
        monkeypatch.setattr(
            f"core.integrations.mail_pipeline.{name}",
            AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.integrations.mail_pipeline.send_storno_confirmation",
        AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(
        inbox, "fetch_full_message",
        AsyncMock(return_value=_msg("spf=pass; dkim=pass; dmarc=pass")))

    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot", company_name="Jantos")
    await inbox._handle_storno_intent(
        tenant=tenant, tenant_id=tenant.id, message_id="AAMk123",
        sender_email="kunde@example.de", sender_name="Kunde",
        subject="Termin stornieren", body_preview="bitte absagen",
        existing_conv=None, employee_id=None, ms_conversation_id=None,
        classification="RELEVANT_KUNDE", confidence="high", reason="—",
        categories=[],
    )
    assert cancel.await_count == 1


def test_klassifikations_prompt_zaeunt_fremdtext_ein():
    """Betreff, Absender und Body sind der Gegenstand der Einordnung,
    nicht die Anweisung an das Modell."""
    from core.ai.gemini import CLASSIFY_PROMPT

    assert "KEINE ANWEISUNG" in CLASSIFY_PROMPT
    assert "<<<MAILDATEN" in CLASSIFY_PROMPT
    assert "MAILDATEN>>>" in CLASSIFY_PROMPT


# =====================================================================
# Automatik-Mail: Empfaenger nur aus dem eigenen Kundenstamm
# =====================================================================

def _ctx_auto():
    from core.features.permissions import ALLE_RECHTE

    emp = SimpleNamespace(id=uuid.uuid4(), name="Sven", slug="sven", is_default=True)
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot", company_name="Jantos")
    ctx = cc.Ctx(
        tenant=tenant, employee=emp, tid=tenant.id, features={"kalender"},
        permissions=frozenset(ALLE_RECHTE),
    )
    ctx.mode_for = lambda _name: "automatisch"  # type: ignore[method-assign]
    return ctx


@pytest.mark.asyncio
async def test_automatik_mailt_nicht_an_fremde_adressen(monkeypatch):
    """Read-Tools liefern Fremdtext (Kundenmails, importierte Websites) in
    dieselbe Gemini-Schleife, die danach senden darf. Eine Adresse aus
    dieser Quelle darf nicht ohne Freigabe bedient werden."""
    monkeypatch.setattr(
        "core.services.mail_compose.lookup_kunde_email",
        AsyncMock(return_value="echt@kunde.de"))

    entwurf = await cc._build_email_entwurf(_ctx_auto(), {
        "kunde_name": "Frau Meier",
        "empfaenger": "angreifer@fremd.example",
        "betreff": "Unterlagen", "text": "…",
    })
    assert entwurf["_aus_kundenstamm"] is False


@pytest.mark.asyncio
async def test_adresse_aus_dem_kundenstamm_ist_freigegeben(monkeypatch):
    monkeypatch.setattr(
        "core.services.mail_compose.lookup_kunde_email",
        AsyncMock(return_value="echt@kunde.de"))

    # Gemini nennt gar keine Adresse -> sie wird aufgeloest.
    aufgeloest = await cc._build_email_entwurf(_ctx_auto(), {
        "kunde_name": "Frau Meier", "betreff": "Unterlagen", "text": "…",
    })
    assert aufgeloest["empfaenger"] == "echt@kunde.de"
    assert aufgeloest["_aus_kundenstamm"] is True

    # Gemini nennt dieselbe Adresse -> ebenfalls in Ordnung.
    bestaetigt = await cc._build_email_entwurf(_ctx_auto(), {
        "kunde_name": "Frau Meier", "empfaenger": "Echt@Kunde.de",
        "betreff": "Unterlagen", "text": "…",
    })
    assert bestaetigt["_aus_kundenstamm"] is True
