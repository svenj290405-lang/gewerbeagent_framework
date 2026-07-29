"""Tests fuer den Anfrage-Formular-Editor der PWA
(core/api/app_screens.py: GET/POST /formulare/{typ}, POST .../reset und der
Normalisierer _normalize_formular_fields).

Reine Unit-Tests mit Fakes — keine echte DB, kein Netz (Muster wie
test_app_verbindungen.py). Die wiederverwendeten anfrage_forms-Funktionen
werden gepatcht; _normalize_formular_fields nutzt die ECHTEN Konstanten
(ALLOWED_FIELD_TYPES / RESERVED_FIELD_NAMES).

Deckt:
- Normalisierung: name aus Label generiert, eindeutig, reservierte/leere
  Namen ersetzt, Optionen aus String/Liste, Typ-Whitelist, Label-Pflicht
- GET: Feature aus -> 403, unbekannter Typ -> 400, Happy-Path liefert
  Schema + Metadaten (field_types/anfrage_typen)
- POST: Feature aus -> 403, fehlende Felder -> 400, Happy-Path ruft
  upsert mit normalisierten Feldern; upsert-Fehler -> 400
- reset: ruft delete + liefert Default-Schema
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.api import app_screens


def _req(body=None, tenant_id=None):
    req = SimpleNamespace()

    async def _json():
        return body or {}

    req.json = _json
    req.state = SimpleNamespace(
        app_tenant=SimpleNamespace(id=tenant_id or uuid.uuid4(), slug="pilot"),
        app_employee=SimpleNamespace(id=uuid.uuid4(), slug="inhaber"),
        app_is_inhaber=True,
    )
    return req


def _body(res):
    return json.loads(res.body)


def _feature(monkeypatch, on: bool):
    async def fake(tid, key):
        return on
    monkeypatch.setattr("core.features.check.is_feature_enabled", fake)


# --------------------------------------------------------------------------
# Normalisierer (echte Konstanten)
# --------------------------------------------------------------------------

def test_normalize_generiert_namen_aus_label():
    raw = [
        {"label": "Wie heißt du?", "type": "text"},
        {"label": "Telefon", "type": "tel", "required": True},
    ]
    fields, err = app_screens._normalize_formular_fields(raw)
    assert err == ""
    assert all(f["name"] and f["name"][0].isalpha() for f in fields)
    # name darf nur [a-z0-9_]
    import re
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", f["name"]) for f in fields)
    assert fields[1]["required"] is True


def test_normalize_dedupliziert_und_ersetzt_reservierte():
    raw = [
        {"label": "Name", "type": "text"},   # 'name' ist reserviert -> ersetzt
        {"label": "Name", "type": "text"},   # gleiches Label -> eindeutiger Name
    ]
    fields, err = app_screens._normalize_formular_fields(raw)
    assert err == ""
    names = [f["name"] for f in fields]
    assert "name" not in names
    assert len(set(names)) == 2  # eindeutig


def test_normalize_optionen_aus_string_und_liste():
    raw = [
        {"label": "Material", "type": "select", "options": "Holz, Metall , Glas"},
        {"label": "Farbe", "type": "radio", "options": ["Weiß", "Schwarz"]},
    ]
    fields, err = app_screens._normalize_formular_fields(raw)
    assert err == ""
    assert fields[0]["options"] == ["Holz", "Metall", "Glas"]
    assert fields[1]["options"] == ["Weiß", "Schwarz"]


def test_normalize_unbekannter_typ_und_fehlendes_label():
    f1, e1 = app_screens._normalize_formular_fields([{"label": "X", "type": "zauberei"}])
    assert f1 is None and "Feldtyp" in e1
    f2, e2 = app_screens._normalize_formular_fields([{"label": "  ", "type": "text"}])
    assert f2 is None and "Bezeichnung" in e2


def test_normalize_behaelt_gueltigen_namen():
    raw = [{"name": "wunschtermin", "label": "Wunschtermin", "type": "date"}]
    fields, err = app_screens._normalize_formular_fields(raw)
    assert err == "" and fields[0]["name"] == "wunschtermin"


# --------------------------------------------------------------------------
# GET /formulare/{typ}
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_feature_aus_403(monkeypatch):
    _feature(monkeypatch, False)
    res = await app_screens.api_formular_get("allgemein", _req(), _e=None)
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_get_unbekannter_typ_400(monkeypatch):
    _feature(monkeypatch, True)
    res = await app_screens.api_formular_get("zauberei", _req(), _e=None)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_get_happy_path_liefert_schema_und_meta(monkeypatch):
    _feature(monkeypatch, True)

    async def fake_schema(tid, typ):
        return {"title": "Deine Anfrage", "subtitle": "Sub", "fields": [{"name": "x", "label": "X", "type": "text"}]}

    monkeypatch.setattr("core.integrations.anfrage_forms.get_schema_for_tenant", fake_schema)
    res = await app_screens.api_formular_get("allgemein", _req(), _e=None)
    assert res.status_code == 200
    b = _body(res)
    assert b["ok"] is True
    assert b["title"] == "Deine Anfrage"
    assert b["fields"][0]["name"] == "x"
    assert any(t["value"] == "select" for t in b["field_types"])
    assert {t["value"] for t in b["anfrage_typen"]} == {"allgemein", "tischler"}
    assert "checkbox_multi" in b["option_types"]


# --------------------------------------------------------------------------
# POST /formulare/{typ}
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_feature_aus_403(monkeypatch):
    _feature(monkeypatch, False)
    res = await app_screens.api_formular_save(
        "allgemein", _req({"fields": []}), _e=None, _c=None
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_save_fehlende_felder_400(monkeypatch):
    _feature(monkeypatch, True)
    res = await app_screens.api_formular_save(
        "allgemein", _req({"title": "x"}), _e=None, _c=None
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_save_happy_path_ruft_upsert_mit_normalisierten_feldern(monkeypatch):
    _feature(monkeypatch, True)
    seen = {}

    async def fake_upsert(tenant_id, anfrage_typ, fields, title=None, subtitle=None):
        seen["fields"] = fields
        seen["title"] = title
        seen["typ"] = anfrage_typ
        return True, ""

    monkeypatch.setattr("core.integrations.anfrage_forms.upsert_tenant_schema", fake_upsert)
    body = {"title": "Anfrage", "subtitle": "", "fields": [
        {"label": "Beschreibung", "type": "textarea", "required": True},
    ]}
    res = await app_screens.api_formular_save("tischler", _req(body), _e=None, _c=None)
    assert res.status_code == 200
    assert seen["typ"] == "tischler"
    assert seen["title"] == "Anfrage"
    # Name wurde aus dem Label generiert (kein roher Name im Body)
    assert seen["fields"][0]["name"]
    assert seen["fields"][0]["type"] == "textarea"
    assert seen["fields"][0]["required"] is True


@pytest.mark.asyncio
async def test_save_upsert_fehler_400(monkeypatch):
    _feature(monkeypatch, True)

    async def fake_upsert(tenant_id, anfrage_typ, fields, title=None, subtitle=None):
        return False, "Feld 'farbe': mindestens 2 Optionen noetig."

    monkeypatch.setattr("core.integrations.anfrage_forms.upsert_tenant_schema", fake_upsert)
    body = {"fields": [{"label": "Farbe", "type": "select", "options": ["nur eins"]}]}
    res = await app_screens.api_formular_save("allgemein", _req(body), _e=None, _c=None)
    assert res.status_code == 400
    assert "Optionen" in _body(res)["error"]


# --------------------------------------------------------------------------
# POST /formulare/{typ}/reset
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_loescht_und_liefert_default(monkeypatch):
    _feature(monkeypatch, True)
    called = {}

    async def fake_delete(tid, typ):
        called["typ"] = typ
        return True

    def fake_default(typ):
        return {"title": "Standard", "subtitle": "", "fields": [{"name": "anliegen", "label": "Anliegen", "type": "textarea"}]}

    monkeypatch.setattr("core.integrations.anfrage_forms.delete_tenant_schema", fake_delete)
    monkeypatch.setattr("core.integrations.anfrage_forms.get_default_schema", fake_default)
    res = await app_screens.api_formular_reset("allgemein", _req(), _e=None, _c=None)
    assert res.status_code == 200
    b = _body(res)
    assert called["typ"] == "allgemein"
    assert b["title"] == "Standard"
    assert b["fields"][0]["name"] == "anliegen"


# --------------------------------------------------------------------------
# GET /formulare/{typ} — preview_url im Response
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_liefert_preview_url(monkeypatch):
    _feature(monkeypatch, True)

    async def fake_schema(tid, typ):
        return {"title": "T", "subtitle": "", "fields": []}

    monkeypatch.setattr("core.integrations.anfrage_forms.get_schema_for_tenant", fake_schema)

    res = await app_screens.api_formular_get("allgemein", _req(), _e=None)
    assert res.status_code == 200
    b = _body(res)
    assert "preview_url" in b
    # Tenant-Slug "pilot" + Typ "allgemein" muessen im Pfad vorkommen
    assert "/anfrage/preview/pilot/allgemein" in b["preview_url"]


# --------------------------------------------------------------------------
# POST /formulare/{typ}/link
# --------------------------------------------------------------------------

import datetime as _dt


def _token_obj(name="Max Muster", email="max@example.com"):
    return SimpleNamespace(
        token="abc123",
        expires_at=_dt.datetime(2026, 7, 1, tzinfo=_dt.timezone.utc),
    )


@pytest.mark.asyncio
async def test_link_feature_aus_403(monkeypatch):
    _feature(monkeypatch, False)
    res = await app_screens.api_formular_link_generieren(
        "allgemein", _req({"kunde_name": "Max"}), _e=None, _c=None
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_link_unbekannter_typ_400(monkeypatch):
    _feature(monkeypatch, True)
    res = await app_screens.api_formular_link_generieren(
        "zauberei", _req({"kunde_name": "Max"}), _e=None, _c=None
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_link_fehlender_name_400(monkeypatch):
    _feature(monkeypatch, True)
    res = await app_screens.api_formular_link_generieren(
        "allgemein", _req({}), _e=None, _c=None
    )
    assert res.status_code == 400
    assert "Pflicht" in _body(res)["error"]


@pytest.mark.asyncio
async def test_link_happy_path_mit_email(monkeypatch):
    _feature(monkeypatch, True)
    seen = {}

    async def fake_create(tenant_id, kunde_email, kunde_name, anfrage_typ,
                          kunde_telefon=None, valid_days=7, **kw):
        seen["email"] = kunde_email
        seen["name"] = kunde_name
        seen["typ"] = anfrage_typ
        seen["days"] = valid_days
        return _token_obj()

    def fake_url(token):
        return f"https://example.com/anfrage/{token}"

    monkeypatch.setattr("core.integrations.anfrage_forms.create_anfrage_token", fake_create)
    monkeypatch.setattr("core.integrations.anfrage_forms.build_anfrage_url", fake_url)

    body = {"kunde_name": "Max Muster", "kunde_email": "max@test.de", "valid_days": 14}
    res = await app_screens.api_formular_link_generieren("allgemein", _req(body), _e=None, _c=None)
    assert res.status_code == 200
    b = _body(res)
    assert b["ok"] is True
    assert b["url"] == "https://example.com/anfrage/abc123"
    assert b["kunde_name"] == "Max Muster"
    assert b["expires_fmt"] == "01.07.2026"
    assert seen["email"] == "max@test.de"
    assert seen["days"] == 14


@pytest.mark.asyncio
async def test_link_ohne_email_nutzt_platzhalter(monkeypatch):
    _feature(monkeypatch, True)
    seen = {}

    async def fake_create(tenant_id, kunde_email, kunde_name, anfrage_typ,
                          kunde_telefon=None, valid_days=7, **kw):
        seen["email"] = kunde_email
        return _token_obj()

    monkeypatch.setattr("core.integrations.anfrage_forms.create_anfrage_token", fake_create)
    monkeypatch.setattr("core.integrations.anfrage_forms.build_anfrage_url", lambda t: f"https://x/{t}")

    res = await app_screens.api_formular_link_generieren(
        "tischler", _req({"kunde_name": "Müller"}), _e=None, _c=None
    )
    assert res.status_code == 200
    # Platzhalter-Email enthält @formular.intern
    assert "@formular.intern" in seen["email"]


@pytest.mark.asyncio
async def test_link_valid_days_wird_geclampt(monkeypatch):
    _feature(monkeypatch, True)
    seen = {}

    async def fake_create(tenant_id, kunde_email, kunde_name, anfrage_typ,
                          kunde_telefon=None, valid_days=7, **kw):
        seen["days"] = valid_days
        return _token_obj()

    monkeypatch.setattr("core.integrations.anfrage_forms.create_anfrage_token", fake_create)
    monkeypatch.setattr("core.integrations.anfrage_forms.build_anfrage_url", lambda t: "https://x")

    # 999 Tage -> wird auf 30 geclampt
    res = await app_screens.api_formular_link_generieren(
        "allgemein", _req({"kunde_name": "X", "valid_days": 999}), _e=None, _c=None
    )
    assert res.status_code == 200
    assert seen["days"] == 30
