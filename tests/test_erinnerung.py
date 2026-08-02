"""Tests für Zahlungserinnerung + Angebots-Nachfassen
(core/services/erinnerung.py).

Wichtig ist hier weniger der schöne Text als die Frage, wann NICHT erinnert
werden darf (bezahlt, noch gar nicht versendet) und ob der Ausfall von
Gemini den Nutzer vor einem leeren Feld stehen lässt.
"""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from core.services import erinnerung as er


def _jetzt():
    return dt.datetime.now(dt.timezone.utc)


class _Session:
    """Liefert erst die Rechnung/das Angebot, dann den Tenant."""

    def __init__(self, obj, tenant):
        self._reihe = [obj, tenant]

    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False

    async def execute(self, stmt):
        wert = self._reihe.pop(0) if self._reihe else None
        return SimpleNamespace(scalar_one_or_none=lambda: wert)


def _patch(monkeypatch, obj, tenant=None):
    import core.database.connection as conn
    monkeypatch.setattr(
        conn, "get_session",
        lambda: _Session(obj, tenant or SimpleNamespace(company_name="Testbetrieb")))


def _patch_gemini(monkeypatch, text=None, fehler=None):
    async def _fake(prompt, **kw):
        if fehler:
            raise fehler
        return text
    import core.ai.gemini as gem
    monkeypatch.setattr(gem, "call_gemini", _fake)


def _rechnung(*, bezahlt=False, versendet_vor_tagen=20, betrag="2400.00"):
    return SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Müller", kunde_email="m@example.de",
        betrag_brutto_eur=betrag, lexware_voucher_number="RE-14",
        bezahlt_am=_jetzt() if bezahlt else None,
        mail_sent_at=(_jetzt() - dt.timedelta(days=versendet_vor_tagen)
                      if versendet_vor_tagen is not None else None),
    )


def _angebot(*, tage=19):
    return SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Hausverwaltung Nord",
        kunde_email="hv@example.de", gesamtbetrag_brutto_eur="4200.00",
        created_at=_jetzt() - dt.timedelta(days=tage),
    )


# ===================== Zahlungserinnerung =====================

@pytest.mark.asyncio
async def test_erinnerung_nutzt_gemini_text(monkeypatch):
    _patch(monkeypatch, _rechnung())
    _patch_gemini(monkeypatch, "Guten Tag Herr Müller,\n\nunsere Rechnung ist noch offen. "
                               "Schauen Sie bitte kurz nach, ob sie untergegangen ist.")
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is True
    assert "Müller" in d["text"]
    assert d["empfaenger"] == "m@example.de"
    assert d["tage"] == 20
    assert "RE-14" in d["betreff"]


@pytest.mark.asyncio
async def test_bezahlte_rechnung_wird_nicht_gemahnt(monkeypatch):
    _patch(monkeypatch, _rechnung(bezahlt=True))
    _patch_gemini(monkeypatch, "egal")
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is False
    assert "bezahlt" in d["error"]


@pytest.mark.asyncio
async def test_nicht_versendete_rechnung_wird_nicht_gemahnt(monkeypatch):
    # Entwurf liegt beim Betrieb — der Kunde weiss von nichts.
    _patch(monkeypatch, _rechnung(versendet_vor_tagen=None))
    _patch_gemini(monkeypatch, "egal")
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is False
    assert "erst senden" in d["error"]


@pytest.mark.asyncio
async def test_gemini_ausfall_liefert_trotzdem_versendbaren_text(monkeypatch):
    _patch(monkeypatch, _rechnung())
    _patch_gemini(monkeypatch, fehler=RuntimeError("429 RESOURCE_EXHAUSTED"))
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is True
    assert len(d["text"]) > 60
    assert "Müller" in d["text"]
    assert "2.400,00" in d["text"]


@pytest.mark.asyncio
async def test_platzhalter_antwort_wird_verworfen(monkeypatch):
    # Gemini mit "[Kundenname]" waere schlimmer als der Fallback.
    _patch(monkeypatch, _rechnung())
    _patch_gemini(monkeypatch, "Guten Tag [Kundenname], Ihre Rechnung [Nummer] ist offen.")
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert "[" not in d["text"]


@pytest.mark.asyncio
async def test_zu_kurze_antwort_wird_verworfen(monkeypatch):
    _patch(monkeypatch, _rechnung())
    _patch_gemini(monkeypatch, "Bitte zahlen.")
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4())
    assert len(d["text"]) > 60


@pytest.mark.asyncio
async def test_unbekannter_ton_faellt_auf_freundlich(monkeypatch):
    _patch(monkeypatch, _rechnung())
    _patch_gemini(monkeypatch, "x" * 80)
    d = await er.entwurf_zahlungserinnerung(uuid.uuid4(), uuid.uuid4(), ton="brutal")
    assert d["ton"] == "freundlich"


# ===================== Nachfassen =====================

@pytest.mark.asyncio
async def test_nachfassen_entwurf(monkeypatch):
    _patch(monkeypatch, _angebot())
    _patch_gemini(monkeypatch, "Guten Tag,\n\nhaben Sie zu unserem Angebot noch Fragen? "
                               "Wir passen es gerne an Ihre Wünsche an.")
    d = await er.entwurf_nachfassen(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is True
    assert d["typ"] == "nachfass"
    assert d["tage"] == 19
    assert "4.200,00" in d["betreff"]


@pytest.mark.asyncio
async def test_nachfassen_ohne_angebot(monkeypatch):
    _patch(monkeypatch, None)
    _patch_gemini(monkeypatch, "egal")
    d = await er.entwurf_nachfassen(uuid.uuid4(), uuid.uuid4())
    assert d["ok"] is False


# ===================== Senden =====================

@pytest.mark.asyncio
async def test_senden_geht_ueber_den_vorhandenen_mailweg(monkeypatch):
    gesehen = {}

    async def _fake_send(tid, **kw):
        gesehen.update(kw)
        return {"ok": True}
    import core.services.mail_compose as mc
    monkeypatch.setattr(mc, "send_freie_mail", _fake_send)

    r = await er.senden(uuid.uuid4(), to_email="a@b.de",
                        betreff="Erinnerung", text="Bitte prüfen.")
    assert r["ok"] is True
    assert gesehen["to_email"] == "a@b.de"
    assert gesehen["betreff"] == "Erinnerung"
