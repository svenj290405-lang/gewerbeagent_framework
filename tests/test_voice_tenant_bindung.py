"""Der Betrieb eines Anrufs kommt aus der Anruf-Bindung, nicht aus dem Payload.

Vor diesen Tests las jeder der neun Voice-Handler `payload["tenant_slug"]`
und glaubte ihn. Der Wert reist als *dynamic variable* durch ein
Sprachmodell, das ein Anrufer im Gespraech umstimmen kann, und Slugs sind
kurz und ratbar. Damit liessen sich fremde Termine lesen und stornieren,
in fremde Kalender buchen und fremde Wissensbasen abfragen.

Diese Datei haelt die neue Grenze fest — und dass keine der neun Stellen
sie wieder verliert.
"""
from __future__ import annotations

import inspect
import re
import uuid
from types import SimpleNamespace

import pytest

from plugins.voice_init import handler as voice_handler


def test_initiation_gibt_eine_anruf_bindung_aus():
    """Ohne Token in den dynamic_variables kann kein Tool mehr arbeiten."""
    quelle = inspect.getsource(voice_handler.Plugin._handle_initiation)
    assert '"tenant_token": _create_call_tenant_token(tenant.slug)' in quelle


def test_kein_handler_liest_den_slug_noch_direkt():
    """Soll-Bestand: nur der Aufloeser fasst das Payload-Feld an.

    Eine neue Stelle mit `payload.get("tenant_slug")` waere ein zehnter
    Weg an der Bindung vorbei — genau so ist der Fund entstanden.
    """
    quelle = inspect.getsource(voice_handler)
    treffer = re.findall(r'payload\.get\("tenant_slug"\)', quelle)
    assert len(treffer) == 1, (
        f"{len(treffer)} Stellen lesen tenant_slug direkt aus dem Payload. "
        "Erlaubt ist nur _tenant_slug_fuer_aufruf(); alles andere umgeht "
        "die Anruf-Bindung."
    )


def test_token_bestimmt_den_betrieb():
    slug, fehler = voice_handler._tenant_slug_fuer_aufruf(
        {"tenant_token": voice_handler._create_call_tenant_token("pilot")}
    )
    assert fehler is None
    assert slug == "pilot"


def test_blanker_slug_ohne_token_wird_abgelehnt():
    slug, fehler = voice_handler._tenant_slug_fuer_aufruf({"tenant_slug": "pilot"})
    assert slug == ""
    assert fehler


def test_widersprechender_slug_wird_abgelehnt():
    """Der Kern des Fundes: Token sagt A, Payload behauptet B."""
    token = voice_handler._create_call_tenant_token("pilot")
    slug, fehler = voice_handler._tenant_slug_fuer_aufruf(
        {"tenant_token": token, "tenant_slug": "demotour"}
    )
    assert slug == ""
    assert fehler
    # Kein stiller Fix auf den richtigen Wert — der Aufruf faellt aus.


def test_unbekanntes_token_faellt_nicht_auf_den_slug_zurueck():
    slug, fehler = voice_handler._tenant_slug_fuer_aufruf(
        {"tenant_token": "gibtsnicht", "tenant_slug": "pilot"}
    )
    assert slug == ""
    assert fehler


@pytest.mark.asyncio
async def test_terminsuche_ergebnis_bleibt_beim_eigenen_anruf():
    """Slots + Mitarbeitername gehoeren dem Betrieb, der gesucht hat."""
    import datetime as dt

    voice_handler._TERMINSUCHE_JOBS.clear()
    voice_handler._TERMINSUCHE_JOBS["job-fremd"] = {
        "created_at": dt.datetime.now(dt.timezone.utc),
        "status": "fertig",
        "result": {"erfolg": True, "slots": [{"slot_id": "geheim"}]},
        "tenant_slug": "pilot",
    }
    plugin = voice_handler.Plugin(
        SimpleNamespace(tenant_id=uuid.uuid4(), config={}))
    result = await plugin._handle_hole_terminvorschlaege({
        "job_id": "job-fremd",
        "tenant_token": voice_handler._create_call_tenant_token("demotour"),
    })
    assert result["erfolg"] is False
    assert result["status"] == "unbekannt"
    assert "slots" not in result
