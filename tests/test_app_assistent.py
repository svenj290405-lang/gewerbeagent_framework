"""Tests fuer die Gemini-Kommando-Zentrale (core/ai/command_center.py).

Reine Unit-Tests mit einem gefaketen genai-Client — keine echte DB, kein
Netz (Muster wie test_app_material_bestellung.py / test_app_diktat.py).

Gefaket wird ausschliesslich ``_get_genai_client``; die echten genai-Typen
(Tool/FunctionDeclaration/Part/Content) werden gebaut, aber der
``generate_content``-Call liefert skriptete Antworten zurueck. Die Tool-
``run``-Funktionen werden pro Test gepatcht, damit nichts an die DB geht.

Deckt:
- Tool-Gating: Feature- und Inhaber-Filter
- Read-Tool-Schleife: Tool laeuft, Ergebnis fliesst zurueck -> message
- Write-Tool: liefert confirm-Vorschlag, fuehrt NICHT aus
- execute_confirmed: fuehrt Write-Tool aus, gegated
- Defensive: unbekanntes/ungegatetes Tool wird abgewiesen
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.ai import command_center as cc


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

def _part_fc(name, args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None)


def _part_text(text):
    return SimpleNamespace(function_call=None, text=text)


def _resp(parts):
    content = SimpleNamespace(parts=parts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class _FakeModels:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = 0
        self.seen_contents = []

    def generate_content(self, *, model, contents, config):
        self.seen_contents.append(contents)
        resp = self.scripted[self.calls]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, models):
        self.models = models


def _patch_gemini(monkeypatch, scripted):
    """Patcht _get_genai_client so, dass generate_content die skripteten
    Antworten liefert. Gibt das _FakeModels-Objekt zurueck (fuer Asserts)."""
    models = _FakeModels(scripted)
    import core.ai.gemini as gem
    monkeypatch.setattr(gem, "_get_genai_client", lambda location="x": _FakeClient(models))
    return models


def _patch_tool(monkeypatch, name, fake_run):
    """Patcht die run-Funktion eines Tools in der Registry."""
    spec = cc._spec_by_name(name)
    monkeypatch.setattr(spec, "run", fake_run)
    return spec


def _ctx(features=("kalender",), is_inhaber=True):
    emp = SimpleNamespace(id=uuid.uuid4(), name="Sven Jantos", slug="sven",
                          is_default=is_inhaber)
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot", company_name="Jantos GmbH")
    return cc.Ctx(tenant=tenant, employee=emp, tid=tenant.id, features=set(features))


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

def test_inhaber_gate_filters_abwesenheit():
    inhaber = _ctx(is_inhaber=True)
    monteur = _ctx(is_inhaber=False)
    names_inhaber = {s.name for s in cc._available_tools(inhaber)}
    names_monteur = {s.name for s in cc._available_tools(monteur)}
    assert "abwesenheit_melden" in names_inhaber
    assert "abwesenheit_melden" not in names_monteur


def test_feature_gate_filters_kalender_tools():
    ohne = _ctx(features=())
    names = {s.name for s in cc._available_tools(ohne)}
    assert "termin_anlegen" not in names
    assert "freie_termine_finden" not in names
    assert "termin_stornieren" not in names
    # Nicht-gegatete Tools bleiben verfuegbar:
    assert "rueckruf_anlegen" in names
    assert "kunde_suchen" in names


def test_feature_gate_filters_mail_and_lexware_tools():
    ohne = _ctx(features=())
    names = {s.name for s in cc._available_tools(ohne)}
    assert "offene_anfragen" not in names          # braucht mail_intake
    assert "auftrag_status" not in names            # braucht lexware (+Inhaber)
    assert "team_status" in names
    assert "wissen_suchen" in names
    assert "wissen_merken" in names
    assert "rueckruf_erledigt" in names


def test_inhaber_gate_filters_new_write_tools():
    monteur = _ctx(features=("kalender", "lexware", "mail_intake"), is_inhaber=False)
    names = {s.name for s in cc._available_tools(monteur)}
    assert "mitarbeiter_zurueck" not in names
    assert "material_anlegen" not in names
    assert "auftrag_status" not in names
    assert "rueckruf_erledigt" in names
    assert "anstehende_termine" in names


def test_registry_has_all_tools():
    assert len(cc._REGISTRY) == 18


# --------------------------------------------------------------------------
# Read-Schleife -> message
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_tool_then_message(monkeypatch):
    called = {}

    async def fake_kunde(ctx, args):
        called["args"] = args
        return {"gespraeche": [{"kunde": "Meier"}], "angebote_anzahl": 1}

    _patch_tool(monkeypatch, "kunde_suchen", fake_kunde)
    models = _patch_gemini(monkeypatch, [
        _resp([_part_fc("kunde_suchen", {"name": "Meier"})]),
        _resp([_part_text("Ich habe Meier gefunden: 1 Angebot.")]),
    ])

    res = await cc.run_command("Was läuft bei Meier?", _ctx())
    assert res["type"] == "message"
    assert "Meier" in res["text"]
    assert called["args"] == {"name": "Meier"}
    assert models.calls == 2  # Read-Ergebnis wurde zurueckgespielt


# --------------------------------------------------------------------------
# Write -> confirm (kein Auto-Execute)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_tool_returns_confirm_without_executing(monkeypatch):
    executed = {"ran": False}

    async def fake_rueckruf(ctx, args):
        executed["ran"] = True
        return {"ok": True}

    _patch_tool(monkeypatch, "rueckruf_anlegen", fake_rueckruf)
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("rueckruf_anlegen",
                        {"kunde_name": "Meier", "kunde_telefon": "0151 222"})]),
    ])

    res = await cc.run_command("Ruf Meier zurück, 0151 222", _ctx())
    assert res["type"] == "confirm"
    assert res["tool"] == "rueckruf_anlegen"
    assert res["args"]["kunde_name"] == "Meier"
    assert "Meier" in res["summary"]
    assert executed["ran"] is False  # NICHT ausgefuehrt


@pytest.mark.asyncio
async def test_execute_confirmed_runs_write(monkeypatch):
    async def fake_rueckruf(ctx, args):
        return {"ok": True, "id": "abc", "kunde": args.get("kunde_name")}

    _patch_tool(monkeypatch, "rueckruf_anlegen", fake_rueckruf)
    res = await cc.execute_confirmed(
        "rueckruf_anlegen", {"kunde_name": "Meier", "kunde_telefon": "0151"}, _ctx())
    assert res["type"] == "done"
    assert res["result"]["ok"] is True
    assert res["result"]["kunde"] == "Meier"


@pytest.mark.asyncio
async def test_execute_confirmed_rejects_gated_tool():
    # Monteur (kein Inhaber) darf abwesenheit_melden nicht ausfuehren.
    res = await cc.execute_confirmed(
        "abwesenheit_melden", {"mitarbeiter": "Tobias", "typ": "krank"},
        _ctx(is_inhaber=False))
    assert res["type"] == "error"
    assert "freigeschaltet" in res["text"].lower()


@pytest.mark.asyncio
async def test_execute_confirmed_rejects_read_tool():
    res = await cc.execute_confirmed("kunde_suchen", {"name": "Meier"}, _ctx())
    assert res["type"] == "error"


# --------------------------------------------------------------------------
# Defensive: unbekanntes Tool
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_defensively(monkeypatch):
    _patch_gemini(monkeypatch, [
        _resp([_part_fc("delete_everything", {})]),
    ])
    res = await cc.run_command("lösch alles", _ctx())
    assert res["type"] == "message"  # defensive Rueckmeldung, kein Crash


@pytest.mark.asyncio
async def test_empty_command_returns_error():
    res = await cc.run_command("   ", _ctx())
    assert res["type"] == "error"


@pytest.mark.asyncio
async def test_no_function_call_returns_message(monkeypatch):
    _patch_gemini(monkeypatch, [
        _resp([_part_text("Wie kann ich helfen?")]),
    ])
    res = await cc.run_command("Hallo", _ctx())
    assert res["type"] == "message"
    assert res["text"] == "Wie kann ich helfen?"
