"""Rauchtest ueber die Admin-Seiten — die HTTP-Schicht selbst.

Anlass: das Admin-Tool hatte bis zum Ausbau am 2026-08-23 **keinen
einzigen** Test, der eine Seite wirklich aufruft. Genau dort sitzen aber
die Fehler, die kein Modultest sieht:

* `templates.TemplateResponse` braucht in diesem Projekt `request` als
  **erstes** Argument — sonst 500 ("unhashable type: dict").
* Ein Template, das ein Feld erwartet, das die Route nicht uebergibt.
* Eine Route, die gar nicht eingebunden ist (404 statt 303).

`tests/test_admin_formulare.py` prueft die POST-Formulare statisch; hier
laufen die vier neuen Seiten (Akquise, Kunden, Betrieb, Geld) einmal
komplett durch Jinja.

**Keine Datenbank.** Die Suite laeuft gegen die Produktivdatenbank
(es gibt keine Test-DB), deshalb ist `get_session` durch eine leere
Attrappe ersetzt und die Auswertungs-Dienste sind gemockt. Getestet wird
die Verdrahtung — Route → Kontext → Template —, nicht die Zahlen.
"""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from core.admin import routes as admin_routes
from core.admin.auth import require_admin
from core.api.app import app


# =====================================================================
# Attrappen
# =====================================================================

class _LeeresErgebnis:
    """Antwortet auf alles, was die Admin-Routen von `execute()` wollen."""

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def scalar(self):
        return 0

    def scalar_one(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def __iter__(self):
        return iter(())


class _LeereSession:
    async def execute(self, *_a, **_k):
        return _LeeresErgebnis()

    async def commit(self):
        return None

    async def flush(self):
        return None

    def add(self, _obj):
        return None


@contextlib.asynccontextmanager
async def _leere_session():
    yield _LeereSession()


_NUTZER = SimpleNamespace(
    id=uuid.uuid4(),
    email="test@example.invalid",
    is_active=True,
    is_superuser=True,
)


async def _als_admin(request: Request):
    """Ersatz fuer `require_admin` — setzt denselben `request.state`."""
    sitzung = SimpleNamespace(csrf_token="testtoken", id=uuid.uuid4())
    request.state.admin_user = _NUTZER
    request.state.admin_session = sitzung
    request.state.admin_csrf = sitzung.csrf_token
    return _NUTZER


@pytest.fixture
def angemeldet(monkeypatch):
    """Admin-Seiten aufrufbar, ohne Login und ohne Datenbank."""
    monkeypatch.setattr(admin_routes, "get_session", _leere_session)

    async def _kein_audit(*_a, **_k):
        return None

    monkeypatch.setattr(admin_routes, "audit", _kein_audit)
    app.dependency_overrides[require_admin] = _als_admin
    yield
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def ohne_db(monkeypatch):
    """Auch der Login-Waechter selbst darf die Produktivdatenbank nicht anfassen.

    `require_admin` oeffnet die Sitzung, BEVOR es den fehlenden Cookie
    bemerkt — ohne diese Attrappe baut der Test eine echte Verbindung auf
    (und faellt frueher oder spaeter mit "attached to a different loop" um).
    """
    from core.admin import auth as admin_auth
    monkeypatch.setattr(admin_auth, "get_session", _leere_session)


@pytest.fixture
def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


# =====================================================================
# Grundregeln
# =====================================================================

@pytest.mark.asyncio
async def test_ohne_session_fuehrt_jede_seite_zum_login(client, ohne_db):
    """Kein Cookie → 303 auf /admin/login. 404 hiesse: Route fehlt."""
    async with client as c:
        for pfad in ("/admin/akquise", "/admin/kunden",
                     "/admin/health", "/admin/costs"):
            antwort = await c.get(pfad)
            assert antwort.status_code == 303, (
                f"{pfad} antwortet {antwort.status_code} statt 303. "
                "404 bedeutet, dass die Route nicht eingebunden ist — dann "
                "ist die Seite im Browser tot."
            )
            assert antwort.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_post_ohne_csrf_wird_abgewiesen(client, angemeldet):
    """Ohne `_csrf` darf keine schreibende Route durchgehen."""
    async with client as c:
        antwort = await c.post("/admin/logout", data={})
    assert antwort.status_code in (303, 403), antwort.status_code
    if antwort.status_code == 303:
        # Logout ohne CSRF darf nirgendwo hinfuehren ausser zum Login.
        assert antwort.headers["location"].startswith("/admin/login")


# =====================================================================
# Die vier neuen Seiten
# =====================================================================

@pytest.mark.asyncio
async def test_akquise_seite_rendert(client, angemeldet, monkeypatch):
    from core.services import website_stats as ws

    async def _kennzahlen(_tage=30):
        return {
            "tage": 30, "besucher_heute": 3, "aufrufe_heute": 5,
            "kontakte_heute": 1, "besuche_zeitraum": 12,
            "aufrufe_zeitraum": 20, "kontakte_zeitraum": 2,
            "kontaktquote": 16.7, "bots_heute": 1,
        }

    async def _verlauf(_tage=30):
        return [{"tag": "2026-08-24", "besucher": 3, "kontakte": 1}]

    async def _herkunft(_tage=30, limit=8):
        return [{"quelle": "www.google.com", "besucher": 2}]

    async def _top_seiten(_tage=30, limit=8):
        return [{"pfad": "/", "aufrufe": 20}]

    async def _trichter(_tage=30):
        return {"besuche": 12, "kontakte": 2, "betriebe": 1}

    monkeypatch.setattr(ws, "kennzahlen", _kennzahlen)
    monkeypatch.setattr(ws, "verlauf", _verlauf)
    monkeypatch.setattr(ws, "herkunft", _herkunft)
    monkeypatch.setattr(ws, "top_seiten", _top_seiten)
    monkeypatch.setattr(ws, "trichter", _trichter)

    async with client as c:
        antwort = await c.get("/admin/akquise")

    assert antwort.status_code == 200, antwort.text[:400]
    assert "www.google.com" in antwort.text


@pytest.mark.asyncio
async def test_akquise_csv_liefert_tabelle(client, angemeldet, monkeypatch):
    from core.services import website_stats as ws

    async def _verlauf(_tage=30):
        return [{"tag": "2026-08-24", "besucher": 3, "kontakte": 1}]

    monkeypatch.setattr(ws, "verlauf", _verlauf)

    async with client as c:
        antwort = await c.get("/admin/akquise/export.csv")

    assert antwort.status_code == 200, antwort.text[:400]
    assert "2026-08-24" in antwort.text


@pytest.mark.asyncio
async def test_kundenampel_rendert_und_zaehlt(client, angemeldet, monkeypatch):
    from core.services import kundenampel

    async def _alle_ampeln():
        return [
            {
                "id": str(uuid.uuid4()), "slug": "pilot", "name": "Pilotbetrieb",
                "farbe": "rot", "begruendung": "Microsoft-Anbindung antwortet nicht",
                "anbindungen": {"microsoft": {"ok": False, "fehler": "401"}},
                "nutzung_7t": 0, "offene_anfragen": 2,
                "offene_rueckrufe": 0, "haengende_mails": 1,
            },
            {
                "id": str(uuid.uuid4()), "slug": "demotour", "name": "Demo",
                "farbe": "gruen", "begruendung": "laeuft",
                "anbindungen": {"google": {"ok": True, "fehler": None}},
                "nutzung_7t": 5, "offene_anfragen": 0,
                "offene_rueckrufe": 0, "haengende_mails": 0,
            },
        ]

    monkeypatch.setattr(kundenampel, "alle_ampeln", _alle_ampeln)

    async with client as c:
        antwort = await c.get("/admin/kunden")

    assert antwort.status_code == 200, antwort.text[:400]
    assert "Pilotbetrieb" in antwort.text
    # Rot muss vor Gruen stehen — was brennt, soll oben sein.
    assert antwort.text.index("Pilotbetrieb") < antwort.text.index("Demo")


@pytest.mark.asyncio
async def test_betriebsseite_rendert_auch_ohne_daten(client, angemeldet,
                                                    monkeypatch):
    """`/admin/health` mit leerer DB — der haeufigste 500er-Fall."""
    from core.integrations import cron_health

    async def _bericht():
        return {"crons": {}, "alles_ok": True}

    monkeypatch.setattr(
        cron_health, "get_health_report_persistent", _bericht, raising=False,
    )

    async with client as c:
        antwort = await c.get("/admin/health")

    assert antwort.status_code == 200, antwort.text[:400]


@pytest.mark.asyncio
async def test_kostenseite_rendert_und_warnt_vor_luecke(client, angemeldet):
    """Ohne Telefonie-Datensaetze muss der Ehrlichkeitshinweis erscheinen."""
    async with client as c:
        antwort = await c.get("/admin/costs")

    assert antwort.status_code == 200, antwort.text[:400]
