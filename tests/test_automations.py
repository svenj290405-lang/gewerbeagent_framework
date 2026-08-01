"""Tests fuer den Automatisierungsgrad (manuell | assistiert | automatisch).

Reine Unit-Tests: Registry-Konsistenz plus das Verhalten der
Kommando-Zentrale bei den drei Stufen. Gemini wird wie in
test_app_assistent.py gefaket, die Tool-``run``-Funktionen werden gepatcht
— keine DB, kein Netz.

Deckt:
- Registry: Keys/Modi konsistent, kein Tool doppelt zugeordnet, Defaults
  entsprechen dem Verhalten VOR dem Feature
- manuell:    Tool wird Gemini nicht angeboten, execute_confirmed weigert sich
- assistiert: confirm-Vorschlag, Tool laeuft NICHT
- automatisch: Tool laeuft sofort, Antwort ist type=done
- mode_for_tool: unbekanntes Tool faellt auf 'assistiert' zurueck
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.ai import command_center as cc
from core.features import automations as au
from core.features.automation_check import mode_for_tool


# --------------------------------------------------------------------------
# Fakes (Muster aus test_app_assistent.py)
# --------------------------------------------------------------------------

def _part_fc(name, args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None)


def _resp(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


class _FakeModels:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        resp = self.scripted[self.calls]
        self.calls += 1
        return resp


def _patch_gemini(monkeypatch, scripted):
    import core.ai.gemini as gem
    models = _FakeModels(scripted)
    monkeypatch.setattr(
        gem, "_get_genai_client",
        lambda location="x": SimpleNamespace(models=models),
    )
    return models


def _ctx(modes=None, features=("kalender",), is_inhaber=True):
    emp = SimpleNamespace(id=uuid.uuid4(), name="Sven Jantos", slug="sven",
                          is_default=is_inhaber)
    tenant = SimpleNamespace(id=uuid.uuid4(), slug="pilot",
                             company_name="Jantos GmbH")
    return cc.Ctx(tenant=tenant, employee=emp, tid=tenant.id,
                  features=set(features), automation_modes=dict(modes or {}))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_registry_keys_stimmen_mit_dict_keys():
    for key, auto in au.AUTOMATIONS.items():
        assert auto.key == key


def test_allowed_modes_und_default_sind_gueltig():
    for auto in au.AUTOMATIONS.values():
        assert auto.allowed_modes, f"{auto.key} hat keine erlaubte Stufe"
        for m in auto.allowed_modes:
            assert m in au.ALL_MODES, f"{auto.key}: unbekannte Stufe {m}"
        assert auto.default_mode in auto.allowed_modes, (
            f"{auto.key}: Default {auto.default_mode} ist nicht erlaubt"
        )


def test_kein_tool_gehoert_zu_zwei_automatisierungen():
    gesehen: dict[str, str] = {}
    for auto in au.AUTOMATIONS.values():
        for tool in auto.tools:
            assert tool not in gesehen, (
                f"Tool {tool} ist {gesehen[tool]} UND {auto.key} zugeordnet — "
                "dann ist nicht entscheidbar, welche Stufe gilt"
            )
            gesehen[tool] = auto.key


def test_zugeordnete_tools_existieren_und_sind_write():
    """Ein Tippfehler in Automation.tools waere sonst still wirkungslos."""
    for auto in au.AUTOMATIONS.values():
        for tool in auto.tools:
            spec = cc._spec_by_name(tool)
            assert spec is not None, f"{auto.key}: Tool {tool} gibt es nicht"
            assert spec.kind == "write", (
                f"{auto.key}: {tool} ist ein Read-Tool — 'manuell' wuerde "
                "dem Betrieb das Nachschauen verbieten"
            )


def test_chat_defaults_sind_assistiert():
    """Rollout darf nichts aendern: Chat-Aktionen fragten schon immer nach."""
    for auto in au.AUTOMATIONS.values():
        if auto.tools:
            assert auto.default_mode == au.MODE_ASSISTIERT, auto.key


def test_hintergrund_defaults_sind_automatisch():
    """Telefon und Mail liefen bisher ohne Rueckfrage — das bleibt so."""
    for key in ("telefon_buchung", "mail_auto_antwort"):
        assert au.AUTOMATIONS[key].default_mode == au.MODE_AUTOMATISCH


def test_hintergrund_kann_kein_assistiert():
    for key in ("telefon_buchung", "mail_auto_antwort"):
        auto = au.AUTOMATIONS[key]
        assert au.MODE_ASSISTIERT not in auto.allowed_modes
        assert auto.unsupported_hint, f"{key}: Hinweis fehlt, warum"


def test_is_valid_mode_faellt_geschlossen_aus():
    assert au.is_valid_mode("termin_buchen", au.MODE_AUTOMATISCH)
    assert not au.is_valid_mode("termin_buchen", "voellig_frei")
    assert not au.is_valid_mode("gibt_es_nicht", au.MODE_MANUELL)
    # Die Stufe existiert, aber nicht fuer diese Automatisierung:
    assert not au.is_valid_mode("telefon_buchung", au.MODE_ASSISTIERT)


def test_mode_for_tool_defaults_auf_assistiert():
    # Nicht registriertes Write-Tool darf nie versehentlich automatisch laufen.
    assert mode_for_tool({}, "voellig_neues_tool") == au.MODE_ASSISTIERT
    # Registriertes Tool ohne gespeicherte Stufe -> Default der Automatisierung.
    assert mode_for_tool({}, "termin_anlegen") == au.MODE_ASSISTIERT
    assert mode_for_tool(
        {"termin_buchen": au.MODE_AUTOMATISCH}, "termin_anlegen",
    ) == au.MODE_AUTOMATISCH


# --------------------------------------------------------------------------
# Verhalten im Chat
# --------------------------------------------------------------------------

def test_manuell_entfernt_write_tool_aus_dem_angebot():
    ctx_an = _ctx({"termin_buchen": au.MODE_ASSISTIERT})
    ctx_aus = _ctx({"termin_buchen": au.MODE_MANUELL})

    namen_an = {s.name for s in cc._available_tools(ctx_an)}
    namen_aus = {s.name for s in cc._available_tools(ctx_aus)}

    assert "termin_anlegen" in namen_an
    assert "termin_anlegen" not in namen_aus
    assert "termin_verschieben" not in namen_aus
    # Read-Tools bleiben: "manuell" heisst nicht handeln, nicht blind sein.
    assert "freie_termine_finden" in namen_aus


def test_manuell_taucht_im_system_prompt_auf():
    ctx = _ctx({"termin_buchen": au.MODE_MANUELL})
    prompt = cc._system_instruction(ctx)
    assert au.AUTOMATIONS["termin_buchen"].manuell_hint in prompt


def test_manuell_ohne_treffer_erzeugt_keinen_block():
    prompt = cc._system_instruction(_ctx())
    assert "erledigt der Betrieb bewusst selbst" not in prompt


@pytest.mark.asyncio
async def test_assistiert_fragt_nach_und_fuehrt_nicht_aus(monkeypatch):
    lief = []

    async def fake_run(ctx, args):
        lief.append(args)
        return {"ok": True}

    monkeypatch.setattr(cc._spec_by_name("termin_anlegen"), "run", fake_run)
    _patch_gemini(monkeypatch, [_resp([_part_fc("termin_anlegen", {
        "kunde_name": "Meier", "datum": "05.08.2026", "uhrzeit": "14:00",
    })])])

    out = await cc.run_command("Termin für Meier", _ctx(
        {"termin_buchen": au.MODE_ASSISTIERT}))

    assert out["type"] == "confirm"
    assert out["tool"] == "termin_anlegen"
    assert lief == [], "assistiert darf das Tool NICHT ausgefuehrt haben"


@pytest.mark.asyncio
async def test_automatisch_fuehrt_sofort_aus(monkeypatch):
    lief = []

    async def fake_run(ctx, args):
        lief.append(args)
        return {"ok": True, "event_id": "abc"}

    monkeypatch.setattr(cc._spec_by_name("termin_anlegen"), "run", fake_run)
    _patch_gemini(monkeypatch, [_resp([_part_fc("termin_anlegen", {
        "kunde_name": "Meier", "datum": "05.08.2026", "uhrzeit": "14:00",
    })])])

    out = await cc.run_command("Termin für Meier", _ctx(
        {"termin_buchen": au.MODE_AUTOMATISCH}))

    assert out["type"] == "done"
    assert out["tool"] == "termin_anlegen"
    assert out["result"]["ok"] is True
    assert out.get("summary"), "Vollzugsmeldung fehlt"
    assert len(lief) == 1


@pytest.mark.asyncio
async def test_automatisch_meldet_fehler_statt_done(monkeypatch):
    async def kaputt(ctx, args):
        raise RuntimeError("Kalender weg")

    monkeypatch.setattr(cc._spec_by_name("termin_anlegen"), "run", kaputt)
    _patch_gemini(monkeypatch, [_resp([_part_fc("termin_anlegen", {
        "kunde_name": "Meier", "datum": "05.08.2026", "uhrzeit": "14:00",
    })])])

    out = await cc.run_command("Termin für Meier", _ctx(
        {"termin_buchen": au.MODE_AUTOMATISCH}))

    assert out["type"] == "error"


@pytest.mark.asyncio
async def test_execute_confirmed_weigert_sich_bei_manuell(monkeypatch):
    """Der Client koennte einen alten confirm-Dialog nachtraeglich abschicken,
    nachdem der Inhaber auf 'manuell' gestellt hat."""
    lief = []

    async def fake_run(ctx, args):
        lief.append(args)
        return {"ok": True}

    monkeypatch.setattr(cc._spec_by_name("termin_anlegen"), "run", fake_run)

    out = await cc.execute_confirmed(
        "termin_anlegen", {"kunde_name": "Meier"},
        _ctx({"termin_buchen": au.MODE_MANUELL}),
    )

    assert out["type"] == "error"
    assert lief == []


@pytest.mark.asyncio
async def test_execute_confirmed_laeuft_bei_assistiert(monkeypatch):
    async def fake_run(ctx, args):
        return {"ok": True}

    monkeypatch.setattr(cc._spec_by_name("termin_anlegen"), "run", fake_run)

    out = await cc.execute_confirmed(
        "termin_anlegen", {"kunde_name": "Meier"},
        _ctx({"termin_buchen": au.MODE_ASSISTIERT}),
    )

    assert out["type"] == "done"


# --------------------------------------------------------------------------
# Robustheit des Lesepfads
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_fehler_liefert_defaults_statt_zu_crashen(monkeypatch):
    """Der Lesepfad haengt am Telefon-Webhook — ein DB-Fehler (z.B. Code
    deployed, Migration noch nicht) darf den laufenden Anruf nicht killen."""
    import core.features.automation_check as ac

    ac.invalidate_automation_cache()

    def kaputt(*a, **kw):
        raise RuntimeError("relation \"automation_settings\" does not exist")

    monkeypatch.setattr(ac, "AsyncSessionLocal", kaputt)

    modes = await ac.automation_modes_for_tenant(uuid.uuid4())

    assert modes == au.default_modes()
    ac.invalidate_automation_cache()


@pytest.mark.asyncio
async def test_db_fehler_behaelt_zuletzt_bekannten_stand(monkeypatch):
    """Transienter Fehler: der zuletzt gelesene Stand ist naeher an der
    Wahrheit als der Default — sonst wuerde ein auf 'manuell' gestellter
    Betrieb bei einem DB-Hickser wieder vollautomatisch handeln."""
    import time

    import core.features.automation_check as ac

    tid = uuid.uuid4()
    ac.invalidate_automation_cache()
    # Abgelaufenen Cache-Eintrag setzen, als haetten wir vorhin gelesen.
    ac._cache[tid] = ac._CacheEntry(
        modes={**au.default_modes(), "telefon_buchung": au.MODE_MANUELL},
        expires_at=time.monotonic() - 1,
    )

    def kaputt(*a, **kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(ac, "AsyncSessionLocal", kaputt)

    modes = await ac.automation_modes_for_tenant(tid)

    assert modes["telefon_buchung"] == au.MODE_MANUELL
    ac.invalidate_automation_cache()
