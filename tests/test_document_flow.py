"""Tests fuer den Beleg-Fluss-Service (core/services/document_flow.py).

Konvention wie die uebrigen Tests: DB wird gemockt (kein echtes Postgres).
Getestet werden die reine Positions-Mappinglogik, die Eingabe-Validierung
(die VOR jedem DB-/Lexware-Call greift) und die 'nicht gefunden'-Pfade der
Lookups/Sender — also genau die Stellen, an denen Fehler sitzen.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import core.services.document_flow as df


# --------------------------------------------------------------------------
# Fake-Session
# --------------------------------------------------------------------------

def _make_get_session(results):
    """Ersetzt core.database.connection.get_session. Jeder execute() pop't
    das naechste Ergebnis (scalar_one_or_none + scalars().all())."""
    queue = list(results)

    class _S:
        async def execute(self, stmt):
            val = queue.pop(0) if queue else None
            return SimpleNamespace(
                scalar_one_or_none=lambda: val,
                scalars=lambda: SimpleNamespace(
                    all=lambda: (val if isinstance(val, list)
                                 else ([] if val is None else [val]))),
            )

        def add(self, obj): pass
        async def flush(self): pass
        async def commit(self): pass
        async def refresh(self, obj): pass
        async def delete(self, obj): pass

    @asynccontextmanager
    async def _gs():
        yield _S()

    return _gs


def _patch_session(monkeypatch, results):
    import core.database.connection as conn
    monkeypatch.setattr(conn, "get_session", _make_get_session(results))


TID = uuid.uuid4()


# --------------------------------------------------------------------------
# _positionen_to_line_items (pure)
# --------------------------------------------------------------------------

def test_positionen_mapping_valid():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "Parkett", "menge": 20, "einheit": "qm", "preis_brutto_eur": 50, "mwst_prozent": 19},
        {"name": "Anfahrt", "menge": 1, "einheit": "Pauschale", "preis_brutto_eur": 30},
    ])
    assert err is None
    assert len(items) == 2
    assert float(gesamt) == 20 * 50 + 30
    assert items[0].tax_rate_percent == 19
    assert items[1].tax_rate_percent == 19  # default


def test_positionen_mapping_invalid_number():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "X", "menge": "viel", "einheit": "qm", "preis_brutto_eur": 50},
    ])
    assert err is not None
    assert "Position 1" in err


def test_positionen_mapping_skips_empty_name():
    items, gesamt, err = df._positionen_to_line_items([
        {"name": "", "menge": 1, "einheit": "x", "preis_brutto_eur": 5},
        {"name": "Echt", "menge": 2, "einheit": "x", "preis_brutto_eur": 10},
    ])
    assert err is None
    assert len(items) == 1
    assert items[0].name == "Echt"


# --------------------------------------------------------------------------
# Validierung (vor DB)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_angebot_requires_kunde():
    res = await df.create_angebot(TID, kunde_name="  ", positionen=[{"name": "X", "menge": 1, "einheit": "x", "preis_brutto_eur": 5}])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_angebot_requires_positionen():
    res = await df.create_angebot(TID, kunde_name="Meier", positionen=[])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_auftrag_manuell_requires_kunde():
    res = await df.create_auftrag_manuell(
        TID, kunde_name=" ", positionen=[{"name": "X", "preis_brutto_eur": 5}])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_auftrag_manuell_requires_positionen():
    res = await df.create_auftrag_manuell(TID, kunde_name="Meier", positionen=[])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_auftrag_manuell_lehnt_rechnung_gesendet_ab():
    """Der Abschluss-Schritt loest Rechnungsversand + Archiv aus (Geld-Pfad).
    Eine Neuanlage darf da nicht hineinspringen."""
    res = await df.create_auftrag_manuell(
        TID, kunde_name="Meier", status="rechnung_gesendet",
        positionen=[{"name": "X", "preis_brutto_eur": 5}])
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_auftrag_manuell_lehnt_erfundenen_status_ab():
    res = await df.create_auftrag_manuell(
        TID, kunde_name="Meier", status="voellig_erfunden",
        positionen=[{"name": "X", "preis_brutto_eur": 5}])
    assert res["ok"] is False


class _CaptureSession:
    """Fake-Session, die die angelegten Objekte festhaelt."""

    def __init__(self):
        self.objekte = []

    def add(self, obj):
        self.objekte.append(obj)

    async def flush(self):
        # Spiegelt die DB: die id steht erst nach dem Flush fest.
        for o in self.objekte:
            if getattr(o, "id", None) is None:
                o.id = uuid.uuid4()

    async def commit(self):
        pass

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


def _patch_capture(monkeypatch):
    sess = _CaptureSession()
    import core.database.connection as conn
    import core.services.kunde_identity as ki

    @asynccontextmanager
    async def _gs():
        yield sess

    monkeypatch.setattr(conn, "get_session", _gs)

    async def _resolve(*a, **kw):
        return None
    monkeypatch.setattr(ki, "resolve_kunde_id_safe", _resolve)
    return sess


@pytest.mark.asyncio
async def test_auftrag_manuell_default_ist_angenommen(monkeypatch):
    sess = _patch_capture(monkeypatch)
    res = await df.create_auftrag_manuell(
        TID, kunde_name="Meier",
        positionen=[{"name": "Bad fliesen", "menge": 2, "preis_brutto_eur": 250}])
    assert res["ok"] is True
    assert res["status"] == "accepted"
    assert res["gesamt_brutto_eur"] == 500.0
    ang = sess.objekte[0]
    assert ang.status == "accepted"
    assert ang.quelle == "manuell"
    assert ang.gesamtbetrag_brutto_eur == 500
    # ab "angenommen" gehoert die Zusage zur Geschichte des Auftrags
    assert ang.accepted_at is not None
    pos = sess.objekte[1]
    assert pos.name == "Bad fliesen" and pos.position_nr == 1


@pytest.mark.asyncio
async def test_auftrag_manuell_vor_annahme_ohne_accepted_at(monkeypatch):
    sess = _patch_capture(monkeypatch)
    res = await df.create_auftrag_manuell(
        TID, kunde_name="Meier", status="rechnung_erstellt",
        positionen=[{"name": "X", "preis_brutto_eur": 5}])
    assert res["ok"] is True
    assert sess.objekte[0].accepted_at is None


@pytest.mark.asyncio
async def test_auftrag_manuell_kappt_zu_lange_felder(monkeypatch):
    """Zu lange Eingaben werden gekappt statt der DB hingeworfen (waere ein 500)."""
    sess = _patch_capture(monkeypatch)
    res = await df.create_auftrag_manuell(
        TID, kunde_name="M" * 400, kunde_ort="O" * 400,
        positionen=[{"name": "P" * 600, "einheit": "E" * 80, "preis_brutto_eur": 5}])
    assert res["ok"] is True
    ang, pos = sess.objekte[0], sess.objekte[1]
    assert len(ang.kunde_name) == 300
    assert len(ang.kunde_ort) == 200
    assert len(pos.name) == 500
    assert len(pos.einheit) == 50


@pytest.mark.asyncio
async def test_create_rechnung_requires_kunde():
    res = await df.create_rechnung(TID, kunde_name="")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_create_rechnung_requires_mode():
    res = await df.create_rechnung(TID, kunde_name="Meier")
    assert res["ok"] is False
    assert "Pauschal" in res["error"] or "Positionen" in res["error"]


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_angebot_none(monkeypatch):
    _patch_session(monkeypatch, [[]])
    assert await df.find_angebot_for_send(TID, "Meier") is None


@pytest.mark.asyncio
async def test_find_angebot_unique(monkeypatch):
    ang = SimpleNamespace(id=uuid.uuid4(), kunde_name="Meier")
    _patch_session(monkeypatch, [[ang]])
    res = await df.find_angebot_for_send(TID, "Meier")
    assert res is ang


@pytest.mark.asyncio
async def test_find_angebot_ambiguous(monkeypatch):
    a = SimpleNamespace(id=uuid.uuid4())
    b = SimpleNamespace(id=uuid.uuid4())
    _patch_session(monkeypatch, [[a, b]])
    assert await df.find_angebot_for_send(TID, "Meier") == "AMBIG"


@pytest.mark.asyncio
async def test_find_auftrag_for_invoice_none(monkeypatch):
    _patch_session(monkeypatch, [[]])
    assert await df.find_auftrag_for_invoice(TID, "Meier") is None


# --------------------------------------------------------------------------
# Sender 'nicht gefunden'
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_angebot_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])  # scalar_one_or_none -> None
    res = await df.send_angebot(TID, angebot_id=uuid.uuid4())
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]


@pytest.mark.asyncio
async def test_send_anfrage_reply_empty_text():
    res = await df.send_anfrage_reply(TID, conv_id=uuid.uuid4(), reply_text="   ")
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_send_anfrage_reply_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])
    res = await df.send_anfrage_reply(TID, conv_id=uuid.uuid4(), reply_text="Hallo")
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]


@pytest.mark.asyncio
async def test_finalize_invoice_not_found(monkeypatch):
    _patch_session(monkeypatch, [None])
    res = await df.finalize_and_send_invoice(TID, angebot_id=uuid.uuid4())
    assert res["ok"] is False
    assert "nicht gefunden" in res["error"]


# --------------------------------------------------------------------------
# Rechnungs-Spiegel: der Auftrags-Weg schreibt jetzt eine Zeile in `rechnungen`
#
# Vorher lebte eine abgerechnete Rechnung ausschliesslich am Angebot. Offene
# Posten, Ueberfaelligkeit, Bezahl-Monitor, Tagesbericht und Zahlungs-
# erinnerung lesen aber alle die Tabelle `rechnungen` — verschickte
# Rechnungen waren fuer sie unsichtbar.
# --------------------------------------------------------------------------

def _position(menge, preis, name="Arbeit"):
    return SimpleNamespace(
        name=name, menge=menge, preis_brutto_eur=preis, einheit="Stueck",
        beschreibung=None, mwst_prozent=19, position_nr=1)


def _invoice(inv_id=None, nummer="RE-2026-007"):
    return SimpleNamespace(
        invoice_id=inv_id or uuid.uuid4(), voucher_number=nummer,
        deeplink_view="https://app.lexware.de/x")


@pytest.mark.asyncio
async def test_spiegel_legt_rechnung_an_und_verknuepft_auftrag(monkeypatch):
    ang = SimpleNamespace(id=uuid.uuid4(), rechnung_id=None)
    sess = _CaptureSession()
    # 1. execute -> vorhandene Rechnung suchen (keine), 2. execute -> Angebot
    sess_results = [None, ang]

    class _S:
        def __init__(self):
            self.objekte = []

        async def execute(self, stmt):
            val = sess_results.pop(0) if sess_results else None
            return SimpleNamespace(scalar_one_or_none=lambda: val)

        def add(self, obj):
            self.objekte.append(obj)

        async def flush(self):
            # Postgres vergibt die id per server_default beim INSERT;
            # SQLAlchemy holt sie mit RETURNING zurueck. Das bildet der
            # Fake nach, sonst haette die neue Zeile nie eine id.
            for o in self.objekte:
                if getattr(o, "id", None) is None:
                    o.id = uuid.uuid4()

        async def commit(self): pass

    s = _S()
    import core.database.connection as conn

    @asynccontextmanager
    async def _gs():
        yield s
    monkeypatch.setattr(conn, "get_session", _gs)

    inv = _invoice()
    rid = await df._spiegle_rechnung(
        TID, angebot_id=ang.id, invoice=inv,
        kunde_name="Meier", kunde_email="meier@example.de",
        kunde_strasse="Weg 1", kunde_plz="45127", kunde_ort="Essen",
        positions=[_position(2, 250), _position(1, 100)])

    assert rid is not None
    r = s.objekte[0]
    assert r.tenant_id == TID
    assert r.kunde_name == "Meier" and r.kunde_email == "meier@example.de"
    assert float(r.betrag_brutto_eur) == 600.0        # 2x250 + 1x100
    assert r.lexware_invoice_id == inv.invoice_id
    assert r.lexware_voucher_number == "RE-2026-007"
    # der Auftrag zeigt jetzt auf die Rechnung (Feld gab es, war nie gesetzt)
    assert ang.rechnung_id == r.id


@pytest.mark.asyncio
async def test_spiegel_ist_idempotent(monkeypatch):
    """Zweiter Lauf zur selben Lexware-Rechnung darf keine zweite Zeile
    anlegen — sonst stuenden Betraege doppelt in den offenen Posten."""
    inv = _invoice()
    vorhanden = SimpleNamespace(
        id=uuid.uuid4(), lexware_invoice_id=inv.invoice_id,
        kunde_name=None, kunde_email=None, kunde_strasse=None,
        kunde_plz=None, kunde_ort=None, betrag_brutto_eur=None,
        lexware_voucher_number=None, leistung_titel=None, raw_input_text=None)
    ang = SimpleNamespace(id=uuid.uuid4(), rechnung_id=None)
    queue = [vorhanden, ang]

    class _S:
        def __init__(self):
            self.objekte = []

        async def execute(self, stmt):
            val = queue.pop(0) if queue else None
            return SimpleNamespace(scalar_one_or_none=lambda: val)

        def add(self, obj):
            self.objekte.append(obj)

        async def flush(self):
            # Postgres vergibt die id per server_default beim INSERT;
            # SQLAlchemy holt sie mit RETURNING zurueck. Das bildet der
            # Fake nach, sonst haette die neue Zeile nie eine id.
            for o in self.objekte:
                if getattr(o, "id", None) is None:
                    o.id = uuid.uuid4()

        async def commit(self): pass

    s = _S()
    import core.database.connection as conn

    @asynccontextmanager
    async def _gs():
        yield s
    monkeypatch.setattr(conn, "get_session", _gs)

    rid = await df._spiegle_rechnung(
        TID, angebot_id=ang.id, invoice=inv, kunde_name="Meier",
        kunde_email=None, kunde_strasse=None, kunde_plz=None, kunde_ort=None,
        positions=[_position(1, 50)])

    assert rid == vorhanden.id
    assert s.objekte == []                       # nichts Neues angelegt
    assert float(vorhanden.betrag_brutto_eur) == 50.0   # aktualisiert


@pytest.mark.asyncio
async def test_spiegel_kippt_den_versand_nicht(monkeypatch):
    """Die Rechnung liegt zu diesem Zeitpunkt schon finalisiert in Lexware —
    ein DB-Problem darf den Mailversand nicht verhindern."""
    import core.database.connection as conn

    @asynccontextmanager
    async def _gs():
        raise RuntimeError("DB weg")
        yield
    monkeypatch.setattr(conn, "get_session", _gs)

    rid = await df._spiegle_rechnung(
        TID, angebot_id=uuid.uuid4(), invoice=_invoice(), kunde_name="X",
        kunde_email=None, kunde_strasse=None, kunde_plz=None, kunde_ort=None,
        positions=[_position(1, 10)])
    assert rid is None


@pytest.mark.asyncio
async def test_versendet_markieren_setzt_mail_sent(monkeypatch):
    """Erst `mail_sent` bringt die Rechnung in die Bezahl-Ueberwachung."""
    r = SimpleNamespace(id=uuid.uuid4(), status="drafted",
                        mail_sent_at=None, mail_sent_to=None)
    _patch_session(monkeypatch, [r])
    await df._rechnung_als_versendet_markieren(r.id, "meier@example.de")
    assert r.status == "mail_sent"
    assert r.mail_sent_at is not None
    assert r.mail_sent_to == "meier@example.de"


@pytest.mark.asyncio
async def test_versendet_markieren_ohne_id_ist_harmlos(monkeypatch):
    await df._rechnung_als_versendet_markieren(None, "x@example.de")


# --------------------------------------------------------------------------
# Angebot beim Senden finalisieren (statt Gang ins Lexware-Web)
# --------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, voucher_status="draft"):
        self.voucher_status = voucher_status
        self.geloescht = []
        self.angelegt = []

    async def get_quotation(self, qid):
        return {"voucherStatus": self.voucher_status}

    async def create_quotation_draft(self, **kw):
        self.angelegt.append(kw)
        return SimpleNamespace(quotation_id=uuid.uuid4(),
                               voucher_number="AN-2026-004",
                               deeplink_view="https://app.lexware.de/q")

    async def delete_voucher(self, vid):
        self.geloescht.append(vid)
        return True


def _angebot(qid=None):
    return SimpleNamespace(
        id=uuid.uuid4(), lexware_quotation_id=qid or uuid.uuid4(),
        lexware_voucher_number=None, kunde_name="Meier",
        kunde_strasse="Weg 1", kunde_plz="45127", kunde_ort="Essen",
        introduction_text="Guten Tag", remark_text="Bis bald")


@pytest.mark.asyncio
async def test_angebot_wird_beim_senden_ausgestellt(monkeypatch):
    ang = _angebot()
    alter_entwurf = ang.lexware_quotation_id
    prov = _FakeProvider(voucher_status="draft")

    async def _prov(tid):
        return prov
    monkeypatch.setattr(df, "_lexware_provider", _prov)
    _patch_session(monkeypatch, [ang, [_position(3, 100)], ang])

    res = await df._angebot_finalisieren(TID, angebot_id=ang.id)

    assert res["ok"] is True and res["unveraendert"] is False
    # neu angelegt, und zwar direkt ausgestellt
    assert prov.angelegt and prov.angelegt[0]["finalize"] is True
    assert prov.angelegt[0]["introduction"] == "Guten Tag"
    # der Entwurf bleibt nicht als Leiche liegen
    assert prov.geloescht == [alter_entwurf]
    # das Angebot zeigt jetzt auf das ausgestellte Dokument
    assert ang.lexware_quotation_id != alter_entwurf
    assert ang.lexware_voucher_number == "AN-2026-004"


@pytest.mark.asyncio
async def test_bereits_ausgestelltes_angebot_bleibt_unangetastet(monkeypatch):
    ang = _angebot()
    prov = _FakeProvider(voucher_status="open")

    async def _prov(tid):
        return prov
    monkeypatch.setattr(df, "_lexware_provider", _prov)
    _patch_session(monkeypatch, [ang, [_position(1, 10)]])

    res = await df._angebot_finalisieren(TID, angebot_id=ang.id)
    assert res == {"ok": True, "unveraendert": True}
    assert prov.angelegt == [] and prov.geloescht == []


@pytest.mark.asyncio
async def test_angebot_ohne_lexware_entwurf_laeuft_weiter(monkeypatch):
    ang = _angebot(qid=None)
    ang.lexware_quotation_id = None
    prov = _FakeProvider()

    async def _prov(tid):
        return prov
    monkeypatch.setattr(df, "_lexware_provider", _prov)
    _patch_session(monkeypatch, [ang])

    res = await df._angebot_finalisieren(TID, angebot_id=ang.id)
    assert res["ok"] is True and res["unveraendert"] is True


# --------------------------------------------------------------------------
# Formular-Rechnung versenden — der Weg war strukturell tot (TypeError bei
# jedem Klick, Nutzer wurde nach Lexware geschickt).
# --------------------------------------------------------------------------

class _RechnungProvider:
    def __init__(self, pdf=b"%PDF-1.4 test"):
        self.pdf = pdf
        self.angelegt = []
        self.geloescht = []

    async def create_invoice_draft(self, **kw):
        self.angelegt.append(kw)
        return SimpleNamespace(invoice_id=uuid.uuid4(),
                               voucher_number="RE-2026-011",
                               deeplink_view="https://app.lexware.de/r")

    async def delete_voucher(self, vid):
        self.geloescht.append(vid)
        return True

    async def download_invoice_pdf(self, iid):
        return self.pdf


def _rechnung(mit_positionen=True, entwurf=None):
    return SimpleNamespace(
        id=uuid.uuid4(), kunde_name="Meier", kunde_email="meier@example.de",
        kunde_strasse="Weg 1", kunde_plz="45127", kunde_ort="Essen",
        betrag_brutto_eur=595, leistung_titel="Bad sanieren",
        leistung_beschreibung=None, lexware_invoice_id=entwurf,
        lexware_voucher_number=None, bezahlt_am=None, status="drafted",
        mail_sent_at=None, mail_sent_to=None)


def _patch_rechnungsversand(monkeypatch, r, positions, mail_ok=True):
    """Session-Queue: Rechnung, Positionen, Tenant, dann die Update-Zugriffe."""
    tenant = SimpleNamespace(id=TID, company_name="Schreiberei Jantos",
                             contact_name="Sven", contact_email="s@example.de",
                             contact_phone="0201")
    _patch_session(monkeypatch, [r, positions, tenant, r, r])

    prov = _RechnungProvider()

    async def _prov(tid):
        return prov
    monkeypatch.setattr(df, "_lexware_provider", _prov)

    gesendet = {}

    async def _send(**kw):
        gesendet.update(kw)
        return {"success": mail_ok, "error": None if mail_ok else "Graph down"}
    import core.integrations.microsoft as ms
    monkeypatch.setattr(ms, "send_tracked_mail", _send)

    eingereiht = {}

    async def _enqueue(**kw):
        eingereiht.update(kw)
    import core.integrations.mail_retry_cron as mrc
    monkeypatch.setattr(mrc, "enqueue_failed_mail", _enqueue)

    return prov, gesendet, eingereiht


@pytest.mark.asyncio
async def test_formular_rechnung_geht_jetzt_raus(monkeypatch):
    r = _rechnung(entwurf=uuid.uuid4())
    alter_entwurf = r.lexware_invoice_id
    pos = [_position(1, 595, name="Bad sanieren")]
    prov, gesendet, _ = _patch_rechnungsversand(monkeypatch, r, pos)

    res = await df.finalize_and_send_rechnung(TID, rechnung_id=r.id)

    assert res["ok"] is True
    assert res["nummer"] == "RE-2026-011"
    # in Lexware ausgestellt, nicht als Entwurf
    assert prov.angelegt[0]["finalize"] is True
    # der alte Entwurf bleibt nicht liegen
    assert prov.geloescht == [alter_entwurf]
    # Mail mit PDF ging an den Kunden
    assert gesendet["to_email"] == "meier@example.de"
    assert gesendet["attachments"][0]["bytes"].startswith(b"%PDF")
    # und die Rechnung ist jetzt in der Bezahl-Ueberwachung
    assert r.status == "mail_sent" and r.mail_sent_at is not None


@pytest.mark.asyncio
async def test_gescheiterte_rechnungsmail_geht_in_die_warteschlange(monkeypatch):
    """Die Rechnung ist zu diesem Zeitpunkt steuerlich gezogen — der Versand
    darf nicht einfach verloren gehen."""
    r = _rechnung()
    pos = [_position(1, 595)]
    _, _, eingereiht = _patch_rechnungsversand(monkeypatch, r, pos, mail_ok=False)

    res = await df.finalize_and_send_rechnung(TID, rechnung_id=r.id)

    assert res["ok"] is False
    assert res["lexware_ausgestellt"] is True and res["queued"] is True
    assert eingereiht["recipient_email"] == "meier@example.de"
    assert eingereiht["rechnung_id"] == r.id
    # Status bleibt auf drafted — nichts wird faelschlich als versendet gefuehrt
    assert r.status == "drafted"


@pytest.mark.asyncio
async def test_rechnung_ohne_positionen_nutzt_sammelposition(monkeypatch):
    """Altbestand ohne gespeicherte Positionen muss trotzdem rausgehen."""
    r = _rechnung()
    prov, _, _ = _patch_rechnungsversand(monkeypatch, r, [])

    res = await df.finalize_and_send_rechnung(TID, rechnung_id=r.id)

    assert res["ok"] is True
    items = prov.angelegt[0]["line_items"]
    assert len(items) == 1
    assert items[0].name == "Bad sanieren"
    assert items[0].unit_price_gross == 595.0


@pytest.mark.asyncio
async def test_bezahlte_rechnung_wird_nicht_nochmal_verschickt(monkeypatch):
    import datetime as _dt
    r = _rechnung()
    r.bezahlt_am = _dt.datetime.now(_dt.timezone.utc)
    _patch_rechnungsversand(monkeypatch, r, [_position(1, 100)])

    res = await df.finalize_and_send_rechnung(TID, rechnung_id=r.id)
    assert res["ok"] is False and "bezahlt" in res["error"].lower()


@pytest.mark.asyncio
async def test_rechnung_ohne_empfaenger_bricht_ab(monkeypatch):
    r = _rechnung()
    r.kunde_email = None
    _patch_rechnungsversand(monkeypatch, r, [_position(1, 100)])

    res = await df.finalize_and_send_rechnung(TID, rechnung_id=r.id)
    assert res["ok"] is False and "Empfaenger" in res["error"]
