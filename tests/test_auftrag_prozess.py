"""Tests fuer den konfigurierbaren Auftragsprozess
(core/services/auftrag_prozess.py).

Reine Unit-Tests mit Fakes — keine echte DB (Muster wie
test_app_auftraege.py).

Der Kern der Sache: die fuenf Lifecycle-Schritte sind unveraenderlich
(an ihnen haengen Fortschritts-Regler und Rechnungsversand), die eigenen
Schritte dazwischen sind frei. Genau diese Grenze wird hier geprueft —
inklusive der Frage, ob ein fehlerhafter/boesartiger Client die
Kern-Reihenfolge kaputtmachen kann (er kann nicht: sie wird gar nicht
erst aus dem Request gelesen).
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from core.models.angebot import AUFTRAG_LIFECYCLE
from core.services import auftrag_prozess as ap


class _FakeSession:
    """Sammelt add/delete und liefert vorgegebene Zeilen je Aufruf."""

    def __init__(self, ergebnisse):
        self.ergebnisse = list(ergebnisse)
        self.added = []
        self.deleted = []
        self.geloescht_stmts = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        if not self.ergebnisse:
            self.geloescht_stmts.append(stmt)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
            )
        wert = self.ergebnisse.pop(0)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: wert),
            scalar_one_or_none=lambda: (wert[0] if isinstance(wert, list) and wert else wert),
        )

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


def _schritt(label, anker, idx=0, sid=None):
    return SimpleNamespace(
        id=sid or uuid.uuid4(), label=label, nach_kern_status=anker,
        sort_index=idx, created_at=None,
    )


# =====================================================================
# lade_prozess — Verweben von Kern- und eigenen Schritten
# =====================================================================

@pytest.mark.asyncio
async def test_prozess_ohne_eigene_schritte_ist_der_kern(monkeypatch):
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[]]))
    schritte = await ap.lade_prozess(uuid.uuid4())
    assert [s["kern_status"] for s in schritte] == AUFTRAG_LIFECYCLE
    assert all(s["gesperrt"] for s in schritte)


@pytest.mark.asyncio
async def test_eigene_schritte_landen_hinter_ihrem_anker(monkeypatch):
    eigene = [
        _schritt("Aufmass nehmen", None, 0),
        _schritt("Material bestellen", "accepted", 1),
        _schritt("Abnahme", "arbeit_fertig", 2),
    ]
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([eigene]))
    labels = [s["label"] for s in await ap.lade_prozess(uuid.uuid4())]

    # Ohne Anker => ganz vorne; sonst direkt hinter dem Anker-Kernschritt.
    assert labels[0] == "Aufmass nehmen"
    assert labels.index("Material bestellen") == labels.index("✅ Angenommen") + 1
    assert labels.index("Abnahme") == labels.index("🏁 Fertig") + 1


@pytest.mark.asyncio
async def test_unbekannter_anker_verliert_den_schritt_nicht(monkeypatch):
    """Ein Anker, den es im Lifecycle nicht (mehr) gibt, darf den Schritt
    nicht unsichtbar machen — er rutscht nach vorne."""
    monkeypatch.setattr(ap, "get_session",
                        lambda: _FakeSession([[_schritt("Waise", "gibt_es_nicht")]]))
    schritte = await ap.lade_prozess(uuid.uuid4())
    assert schritte[0]["label"] == "Waise"
    assert len(schritte) == len(AUFTRAG_LIFECYCLE) + 1


# =====================================================================
# lade_auftrag_schritte — Erledigt-Zustaende
# =====================================================================

@pytest.mark.asyncio
async def test_kern_zustaende_folgen_dem_auftrags_status(monkeypatch):
    ang = SimpleNamespace(id=uuid.uuid4(), status="arbeit_laeuft")
    # 1. Aufruf: eigene Schritte (lade_prozess), 2. Aufruf: Haken
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[], []]))
    schritte = await ap.lade_auftrag_schritte(uuid.uuid4(), ang)
    nach_status = {s["kern_status"]: s["zustand"] for s in schritte}

    assert nach_status["rechnung_erstellt"] == ap.SCHRITT_ERLEDIGT
    assert nach_status["accepted"] == ap.SCHRITT_ERLEDIGT
    assert nach_status["arbeit_laeuft"] == ap.SCHRITT_AKTIV
    assert nach_status["arbeit_fertig"] == ap.SCHRITT_OFFEN
    assert nach_status["rechnung_gesendet"] == ap.SCHRITT_OFFEN


@pytest.mark.asyncio
async def test_abgeschlossener_auftrag_ist_komplett_abgehakt(monkeypatch):
    """Der letzte Schritt ist mit dem Erreichen auch erledigt — danach
    kommt nichts mehr, ein 'laeuft gerade' waere gelogen."""
    ang = SimpleNamespace(id=uuid.uuid4(), status="rechnung_gesendet")
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[], []]))
    schritte = await ap.lade_auftrag_schritte(uuid.uuid4(), ang)
    assert all(s["zustand"] == ap.SCHRITT_ERLEDIGT for s in schritte)


@pytest.mark.asyncio
async def test_eigener_schritt_gilt_erst_mit_haken_als_erledigt(monkeypatch):
    sid = uuid.uuid4()
    ang = SimpleNamespace(id=uuid.uuid4(), status="accepted")
    haken = SimpleNamespace(
        schritt_id=sid, erledigt_am=dt.datetime(2026, 7, 30, tzinfo=dt.timezone.utc))
    # EINE Session-Instanz fuer beide Aufrufe (Schritte, dann Haken) —
    # sonst bekaeme der zweite Aufruf wieder das erste Ergebnis.
    sess = _FakeSession([[_schritt("Material bestellen", "accepted", sid=sid)],
                         [haken]])
    monkeypatch.setattr(ap, "get_session", lambda: sess)
    schritte = await ap.lade_auftrag_schritte(uuid.uuid4(), ang)
    eigen = next(s for s in schritte if s["typ"] == "eigen")
    assert eigen["zustand"] == ap.SCHRITT_ERLEDIGT
    assert eigen["erledigt_am"].startswith("2026-07-30")


@pytest.mark.asyncio
async def test_eigener_schritt_ohne_haken_ist_offen(monkeypatch):
    ang = SimpleNamespace(id=uuid.uuid4(), status="accepted")
    sess = _FakeSession([[_schritt("Material bestellen", "accepted")], []])
    monkeypatch.setattr(ap, "get_session", lambda: sess)
    schritte = await ap.lade_auftrag_schritte(uuid.uuid4(), ang)
    eigen = next(s for s in schritte if s["typ"] == "eigen")
    assert eigen["zustand"] == ap.SCHRITT_OFFEN
    assert eigen["erledigt_am"] is None


# =====================================================================
# speichere_prozess — Anker aus der Listenposition, Kern unantastbar
# =====================================================================

def _editor_liste(*eintraege):
    """Baut die Liste, wie sie der Editor hochschickt."""
    return [
        ({"typ": "kern", "id": e, "kern_status": e} if e in AUFTRAG_LIFECYCLE
         else {"typ": "eigen", "id": None, "label": e})
        for e in eintraege
    ]


@pytest.mark.asyncio
async def test_anker_ergibt_sich_aus_der_position(monkeypatch):
    sess = _FakeSession([[], []])
    monkeypatch.setattr(ap, "get_session", lambda: sess)
    await ap.speichere_prozess(uuid.uuid4(), _editor_liste(
        "Aufmass", "rechnung_erstellt", "accepted", "Material", "arbeit_laeuft",
    ))
    angelegt = {s.label: s.nach_kern_status for s in sess.added}
    assert angelegt == {"Aufmass": None, "Material": "accepted"}
    assert sess.committed is True


@pytest.mark.asyncio
async def test_client_kann_kern_reihenfolge_nicht_veraendern(monkeypatch):
    """Selbst wenn der Client die Kern-Schritte durcheinanderwirbelt oder
    weglaesst: gespeichert werden ausschliesslich eigene Schritte, die
    Kern-Kette kommt beim Lesen wieder aus dem Code."""
    sess = _FakeSession([[], []])
    monkeypatch.setattr(ap, "get_session", lambda: sess)
    await ap.speichere_prozess(uuid.uuid4(), _editor_liste(
        "rechnung_gesendet", "Zwischenschritt", "rechnung_erstellt",
    ))
    assert [s.label for s in sess.added] == ["Zwischenschritt"]
    assert sess.added[0].nach_kern_status == "rechnung_gesendet"


@pytest.mark.asyncio
async def test_entfernte_schritte_werden_geloescht(monkeypatch):
    bleibt = _schritt("Bleibt", None)
    faellt_weg = _schritt("Weg", None)
    sess = _FakeSession([[bleibt, faellt_weg]])
    monkeypatch.setattr(ap, "get_session", lambda: sess)
    await ap.speichere_prozess(uuid.uuid4(), [
        {"typ": "eigen", "id": str(bleibt.id), "label": "Bleibt"},
    ])
    # Kein Neuanlegen, aber ein DELETE fuer den entfallenen Schritt.
    assert sess.added == []
    assert sess.geloescht_stmts, "erwartet ein DELETE fuer den entfernten Schritt"


@pytest.mark.asyncio
async def test_leerer_name_wird_abgelehnt(monkeypatch):
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[]]))
    with pytest.raises(ap.ProzessFehler):
        await ap.speichere_prozess(uuid.uuid4(), [{"typ": "eigen", "label": "   "}])


@pytest.mark.asyncio
async def test_zu_langer_name_wird_abgelehnt(monkeypatch):
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[]]))
    with pytest.raises(ap.ProzessFehler):
        await ap.speichere_prozess(
            uuid.uuid4(), [{"typ": "eigen", "label": "x" * 200}])


@pytest.mark.asyncio
async def test_zu_viele_schritte_werden_abgelehnt(monkeypatch):
    monkeypatch.setattr(ap, "get_session", lambda: _FakeSession([[]]))
    from core.models.auftrag_prozess import MAX_EIGENE_SCHRITTE
    zu_viele = [{"typ": "eigen", "label": f"S{i}"}
                for i in range(MAX_EIGENE_SCHRITTE + 1)]
    with pytest.raises(ap.ProzessFehler):
        await ap.speichere_prozess(uuid.uuid4(), zu_viele)
