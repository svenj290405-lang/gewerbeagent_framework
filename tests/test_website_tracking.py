"""Tests fuer die Besucherzaehlung der Website.

Zwei Dinge muessen stimmen, alles andere ist Beiwerk:

1. Der Zaehl-Endpunkt gibt **immer** 204 zurueck und wirft nie — eine
   Zaehlung darf keine Seite kaputtmachen.
2. Die Besucherkennung ist **innerhalb eines Tages stabil** (sonst zaehlt
   derselbe Mensch mehrfach) und **ueber Tage hinweg nicht** (sonst waere
   es doch eine Verfolgung, und genau das versprechen wir nicht zu tun).
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.api import track_routes as tr
from core.models import website_visit as wv


# =====================================================================
# Hilfen
# =====================================================================

def _request(nutzlast, *, user_agent="Mozilla/5.0 (Windows NT 10.0)",
             ip="203.0.113.7"):
    roh = nutzlast if isinstance(nutzlast, bytes) else json.dumps(
        nutzlast).encode()

    class _Headers(dict):
        def get(self, k, default=None):
            return super().get(k.lower(), default)

    kopf = _Headers()
    kopf["user-agent"] = user_agent
    kopf["x-real-ip"] = ip

    req = SimpleNamespace(headers=kopf, client=SimpleNamespace(host=ip))

    async def _body():
        return roh
    req.body = _body
    return req


@pytest.fixture(autouse=True)
def _sauber(monkeypatch):
    """Kein DB-Zugriff, keine Zaehler aus vorherigen Tests."""
    tr._TREFFER.clear()
    tr._TAGESZAEHLER.clear()
    geschrieben = []

    async def _record(**kwargs):
        geschrieben.append(kwargs)

    async def _salt(tag):
        return f"salt-{tag.isoformat()}"

    monkeypatch.setattr(tr, "record_website_visit", _record)
    monkeypatch.setattr(tr, "hole_salt", _salt)
    return geschrieben


# =====================================================================
# Der Endpunkt antwortet immer freundlich
# =====================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("nutzlast", [
    {"p": "/", "k": "aufruf"},
    {"p": "/", "k": "unbekannte-art"},
    {"p": "ohne-schraegstrich", "k": "aufruf"},
    b"kein json",
    b"",
    {"p": "/", "k": "aufruf", "r": "kaputt://"},
])
async def test_antwortet_immer_204(nutzlast):
    resp = await tr.zaehle_ereignis(_request(nutzlast))
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_zu_grosser_body_wird_verworfen(_sauber):
    resp = await tr.zaehle_ereignis(_request(b"x" * (tr.MAX_BODY_BYTES + 1)))
    assert resp.status_code == 204
    assert _sauber == []


@pytest.mark.asyncio
async def test_gueltiges_ereignis_wird_geschrieben(_sauber):
    await tr.zaehle_ereignis(_request(
        {"p": "/preise?utm=abc#dort", "k": "aufruf",
         "r": "https://www.google.com/search?q=geheim"}))

    assert len(_sauber) == 1
    eintrag = _sauber[0]
    assert eintrag["art"] == "aufruf"
    assert eintrag["pfad"] == "/preise", "Query und Anker gehoeren nicht ins Log"
    assert eintrag["ref_host"] == "www.google.com", "nur der Host, nie die Suchanfrage"
    assert eintrag["bot"] is False


@pytest.mark.asyncio
async def test_eigener_verweis_zaehlt_nicht_als_herkunft(_sauber):
    await tr.zaehle_ereignis(_request(
        {"p": "/impressum", "k": "aufruf",
         "r": "https://www.gewerbeagent.de/"}))
    assert _sauber[0]["ref_host"] is None


@pytest.mark.asyncio
async def test_bots_werden_markiert_nicht_verworfen(_sauber):
    await tr.zaehle_ereignis(_request(
        {"p": "/", "k": "aufruf"}, user_agent="Mozilla/5.0 (compatible; GPTBot/1.0)"))
    assert len(_sauber) == 1, "Automaten werden gezaehlt, aber gekennzeichnet"
    assert _sauber[0]["bot"] is True


@pytest.mark.asyncio
async def test_ohne_browserkennung_keine_zaehlung(_sauber):
    await tr.zaehle_ereignis(_request({"p": "/", "k": "aufruf"}, user_agent=""))
    assert _sauber == []


@pytest.mark.asyncio
async def test_flut_von_einer_leitung_wird_gebremst(_sauber):
    for _ in range(tr.MAX_PRO_IP_H + 20):
        await tr.zaehle_ereignis(_request({"p": "/", "k": "aufruf"}))
    assert len(_sauber) <= tr.MAX_PRO_IP_H


# =====================================================================
# Der eigentliche Datenschutz-Nachweis
# =====================================================================

def test_gleiche_person_gleicher_tag_ist_ein_besucher():
    salt = "salt-des-tages"
    a = wv.besucher_hash(salt, "203.0.113.7", "Mozilla/5.0")
    b = wv.besucher_hash(salt, "203.0.113.7", "Mozilla/5.0")
    assert a == b, (
        "Ohne stabile Tageskennung zaehlt derselbe Mensch bei jedem Klick neu"
    )


def test_neuer_tag_neue_kennung():
    """Der Salt wechselt taeglich — damit endet jede Wiedererkennung."""
    gestern = wv.besucher_hash("salt-montag", "203.0.113.7", "Mozilla/5.0")
    heute = wv.besucher_hash("salt-dienstag", "203.0.113.7", "Mozilla/5.0")
    assert gestern != heute


def test_andere_person_andere_kennung():
    salt = "salt-des-tages"
    a = wv.besucher_hash(salt, "203.0.113.7", "Mozilla/5.0")
    b = wv.besucher_hash(salt, "198.51.100.4", "Mozilla/5.0")
    assert a != b


def test_kennung_enthaelt_die_ip_nicht():
    hash_ = wv.besucher_hash("salt", "203.0.113.7", "Mozilla/5.0")
    assert "203.0.113" not in hash_
    assert len(hash_) == 32


def test_port_wird_abgeschnitten():
    """Caddy schickte lange IP:Port — damit war jedes IP-Limit wirkungslos."""
    assert tr._ohne_port("203.0.113.7:52344") == "203.0.113.7"
    assert tr._ohne_port("203.0.113.7") == "203.0.113.7"
    assert tr._ohne_port("[2001:db8::1]:443") == "2001:db8::1"
    assert tr._ohne_port("2001:db8::1") == "2001:db8::1"


@pytest.mark.asyncio
async def test_zaehlung_stoert_nie_wenn_die_datenbank_klemmt(monkeypatch, _sauber):
    async def _kracht(**kwargs):
        raise RuntimeError("DB weg")
    monkeypatch.setattr(tr, "record_website_visit", _kracht)

    resp = await tr.zaehle_ereignis(_request({"p": "/", "k": "aufruf"}))
    assert resp.status_code == 204
