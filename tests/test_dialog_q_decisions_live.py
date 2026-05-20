"""Live-Q-Decision-Tests: gegen echtes Gemini, kein Mock.

Dieser Test deckt die Klasse Fehler ab, die alle anderen Tests
verpassen: Q waehlt die falsche next_action obwohl die Pipeline-
Logik drumherum stimmt. Bisheriger Lauf zeigte z.B. bei "Ich habe
das Formular ausgefuellt … Montag passt mir" hat Q SEND_FORMULAR
gewaehlt (sollte PROPOSE_SLOTS sein).

Test schickt 6 Beispiel-Mails durch handle_kunde_mail_dialog gegen
das echte Gemini-Modell und verifiziert nur die next_action — der
genaue Antwort-Text ist freigestellt.

LANGSAM: jeder Test-Case = 1 Gemini-Call (~2-4s). Daher
`pytest.mark.slow` — bei normaler `pytest`-Suite uebersprungen, nur
mit `pytest -m slow` (oder explizit `pytest tests/test_dialog_q_decisions_live.py`)
ausgefuehrt.

Wenn der Test ohne Gemini-Credentials laeuft (z.B. CI ohne
GOOGLE_GENAI_USE_VERTEXAI), wird er geskippt.
"""
from __future__ import annotations

import os
import pytest

from core.ai.gemini import handle_kunde_mail_dialog


# Skip Marker — wir brauchen Vertex-Credentials (das Modul setzt
# GOOGLE_GENAI_USE_VERTEXAI selbst, deshalb nur die Cred-Pfade pruefen)
_HAS_VERTEX = bool(
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")
)
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _HAS_VERTEX,
        reason="braucht GOOGLE_APPLICATION_CREDENTIALS fuer Gemini-Vertex",
    ),
]


# Faktoren-Kontext fuer alle Cases (Tenant-Plumbing)
_BASE = dict(
    tenant_company="Schreinerei Test GbR",
    tenant_owner_first_name="Anna",
    tenant_branche="Tischler",
    wissensbasis=(
        "- [Oeffnungszeiten] Mo-Fr 7-17 Uhr, Sa 9-13 Uhr\n"
        "- [Lieferung] im Umkreis 50km um Trier"
    ),
)


# Stellvertretender "Formular wurde ausgefuellt"-Status, fuer alle
# Tests die Termin-Aktionen erwarten. Ohne diesen Block schickt Q nach
# der neuen Workflow-Regel das Formular zuerst.
_FORM_SUBMITTED = {
    "status": "submitted",
    "sent_at": None,
    "submitted_at": None,
    "antworten": {"produkt": "Treppe", "anliegen": "Beratung"},
    "anliegen": "Beratung Treppe",
}


# =====================================================================
# Decision-Cases
# =====================================================================

@pytest.mark.asyncio
async def test_q_picks_book_direct_for_concrete_date_and_time():
    """22.05.26 um 14 Uhr -> BOOK_DIRECT mit direct_datum + direct_uhrzeit.
    Voraussetzung: Anfrage-Formular bereits ausgefuellt."""
    res = await handle_kunde_mail_dialog(
        subject="Termin",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message="Der 22.05.26 um 14 Uhr wuerde mir passen.",
        anfrage_status=_FORM_SUBMITTED,
        **_BASE,
    )
    assert res["next_action"] == "BOOK_DIRECT", (
        f"Erwartet BOOK_DIRECT, bekam {res['next_action']} "
        f"(reason={res.get('reason')!r}, reply={res['reply_text'][:80]!r})"
    )
    assert res.get("direct_datum"), "direct_datum fehlt"
    assert res.get("direct_uhrzeit"), "direct_uhrzeit fehlt"
    assert "22.05.2026" in res["direct_datum"] or "22.5.2026" in res["direct_datum"]


@pytest.mark.asyncio
async def test_q_picks_propose_slots_for_day_only():
    """'Montag passt mir' (nur Tag, keine Uhrzeit) -> PROPOSE_SLOTS.
    Voraussetzung: Anfrage-Formular bereits ausgefuellt."""
    res = await handle_kunde_mail_dialog(
        subject="Termin Beratung",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message=(
            "Koennte ich naechste Woche einen Termin haben fuer "
            "eine Beratung? Mir passt Montag ganz gut."
        ),
        anfrage_status=_FORM_SUBMITTED,
        **_BASE,
    )
    assert res["next_action"] == "PROPOSE_SLOTS", (
        f"Erwartet PROPOSE_SLOTS, bekam {res['next_action']} "
        f"(reason={res.get('reason')!r}, reply={res['reply_text'][:80]!r})"
    )


@pytest.mark.asyncio
async def test_q_does_not_resend_form_if_already_submitted():
    """Wenn anfrage_status=submitted und Kunde nach Termin fragt:
    NIE SEND_FORMULAR. Sollte PROPOSE_SLOTS (Montag) sein."""
    res = await handle_kunde_mail_dialog(
        subject="Termin Beratung",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message=(
            "Ich habe das Formular jetzt ausgefuellt — koennte ich "
            "naechste Woche einen Termin haben fuer eine Beratung? "
            "Mir passt Montag ganz gut."
        ),
        anfrage_status={
            "status": "submitted",
            "sent_at": None,
            "submitted_at": None,
            "antworten": {
                "produkt": "Treppe",
                "anliegen": "Beratung Treppe im Rohbau",
            },
            "anliegen": "Beratung Treppe im Rohbau",
        },
        **_BASE,
    )
    assert res["next_action"] != "SEND_FORMULAR", (
        f"SEND_FORMULAR obwohl Formular schon eingegangen — "
        f"reason={res.get('reason')!r}"
    )
    # Optimal: PROPOSE_SLOTS (Montag als Anker) oder BOOK_DIRECT
    assert res["next_action"] in ("PROPOSE_SLOTS", "BOOK_DIRECT"), (
        f"Erwartet PROPOSE_SLOTS/BOOK_DIRECT, bekam {res['next_action']}"
    )


@pytest.mark.asyncio
async def test_q_picks_cancel_for_storno_intent():
    """'Ich muss meinen Termin absagen' -> CANCEL_TERMIN."""
    res = await handle_kunde_mail_dialog(
        subject="Re: Terminbestaetigung",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message="Hallo, ich muss meinen Termin doch leider absagen.",
        **_BASE,
    )
    assert res["next_action"] == "CANCEL_TERMIN", (
        f"Erwartet CANCEL_TERMIN, bekam {res['next_action']}"
    )


@pytest.mark.asyncio
async def test_q_picks_book_slot_when_confirming_listed_slot():
    """Mit previous_proposed_slots und 'der erste passt' -> BOOK_SLOT idx=0.
    Voraussetzung: Anfrage-Formular bereits ausgefuellt."""
    slots = [
        {"datum": "25.05.2026", "uhrzeit": "14:00", "wochentag": "Mo"},
        {"datum": "26.05.2026", "uhrzeit": "10:00", "wochentag": "Di"},
    ]
    res = await handle_kunde_mail_dialog(
        subject="Re: Termin",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message="Der erste Termin passt mir, bitte buchen.",
        previous_proposed_slots=slots,
        anfrage_status=_FORM_SUBMITTED,
        **_BASE,
    )
    assert res["next_action"] == "BOOK_SLOT", (
        f"Erwartet BOOK_SLOT, bekam {res['next_action']}"
    )
    assert res.get("chosen_slot_index") == 0


@pytest.mark.asyncio
async def test_q_picks_ask_more_for_pure_knowledge_question():
    """Reine Wissensfrage ohne Auftrag/Termin -> ASK_MORE."""
    res = await handle_kunde_mail_dialog(
        subject="Frage",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message=(
            "Hallo, kurze Frage: wann habt ihr Mo-Fr geoeffnet? "
            "Aktuell nichts Konkretes geplant, wollte nur Bescheid."
        ),
        **_BASE,
    )
    assert res["next_action"] == "ASK_MORE", (
        f"Erwartet ASK_MORE, bekam {res['next_action']} "
        f"(reply={res['reply_text'][:120]!r})"
    )


@pytest.mark.asyncio
async def test_q_sends_formular_first_when_termin_wish_without_form():
    """Vor-Gate: Termin-Wunsch (PROPOSE_SLOTS-Signal) OHNE Formular
    eingegangen -> Q soll SEND_FORMULAR waehlen, NICHT direkt einen
    Slot vorschlagen. Sonst kommt der Handwerker blind zum Termin."""
    res = await handle_kunde_mail_dialog(
        subject="Termin Beratung",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message=(
            "Hallo, ich braeuchte naechste Woche einen Beratungs-"
            "termin. Montag passt mir gut."
        ),
        anfrage_status=None,  # noch nie ein Token raus
        **_BASE,
    )
    assert res["next_action"] == "SEND_FORMULAR", (
        f"Erwartet SEND_FORMULAR (Gate vor Termin), bekam "
        f"{res['next_action']} (reason={res.get('reason')!r})"
    )


@pytest.mark.asyncio
async def test_q_reminds_instead_of_resending_when_form_open():
    """Formular OFFEN (Token raus, nicht ausgefuellt) + Termin-Wunsch ->
    Q soll NICHT nochmal das Formular schicken (SEND_FORMULAR), sondern
    ans offene erinnern -> ASK_MORE. Genau der Doppel-Formular-Bug."""
    res = await handle_kunde_mail_dialog(
        subject="Termin",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message="22.05.26 um 14 Uhr wuerde mir passen.",
        anfrage_status={
            "status": "open", "sent_at": None, "submitted_at": None,
            "antworten": None, "anliegen": None,
        },
        **_BASE,
    )
    assert res["next_action"] == "ASK_MORE", (
        f"Erwartet ASK_MORE (Erinnerung statt Doppel-Formular), "
        f"bekam {res['next_action']} (reason={res.get('reason')!r})"
    )


@pytest.mark.asyncio
async def test_q_picks_send_formular_for_offer_without_date():
    """Konkrete Auftrags-/Angebots-Anfrage OHNE Termin-Signal -> SEND_FORMULAR."""
    res = await handle_kunde_mail_dialog(
        subject="Anfrage neue Werkbank",
        sender_name="Sven",
        sender_email="kunde@x.de",
        latest_message=(
            "Hallo, ich brauche fuer meine Werkstatt eine massive "
            "Werkbank, ca. 2,40 m breit, Ahorn oder Buche. Koennt "
            "ihr mir ein Angebot machen?"
        ),
        **_BASE,
    )
    assert res["next_action"] == "SEND_FORMULAR", (
        f"Erwartet SEND_FORMULAR, bekam {res['next_action']}"
    )
