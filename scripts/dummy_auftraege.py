"""Legt Beispiel-Auftraege zum Ausprobieren an — und raeumt sie wieder weg.

Damit laesst sich der Auftragsflow (Liste, Detailansicht, Fortschritts-Regler,
Prozess-Editor) mit realistisch aussehenden Daten anschauen, ohne echte
Auftraege anfassen zu muessen.

Aufruf im Container:
  PYTHONPATH=/app python scripts/dummy_auftraege.py <tenant_slug>
  PYTHONPATH=/app python scripts/dummy_auftraege.py <tenant_slug> --loeschen

Die Auftraege tragen ``quelle="dummy"`` — daran und an nichts anderem
erkennt sie das Aufraeumen wieder. Echte Auftraege (quelle "voice", "mail",
"manuell" ...) fasst das Skript nie an.
"""
import asyncio
import datetime as dt
import sys
import uuid

from sqlalchemy import select

from core.plugin_system import discover_plugins
from core.database.connection import get_session
from core.services.document_flow import create_auftrag_manuell

QUELLE = "dummy"

# Jeder Eintrag: Kunde, Adresse, Positionen, Startschritt, Alter in Tagen und
# (nur bei "arbeit_laeuft") der Stand des Fortschritts-Reglers.
BEISPIELE = [
    dict(
        kunde="Familie Weber", email="weber@example.de",
        strasse="Lindenweg 14", plz="54497", ort="Horath",
        status="rechnung_erstellt", tage=21,
        positionen=[
            dict(name="Einbauschrank Schlafzimmer", beschreibung="Eiche massiv, 3,20 m",
                 menge=1, einheit="Stueck", preis_brutto_eur=3850),
            dict(name="Montage vor Ort", menge=8, einheit="Stunde", preis_brutto_eur=68),
        ],
    ),
    dict(
        kunde="Baeckerei Klein", email="info@baeckerei-klein.example",
        strasse="Marktstr. 2", plz="54486", ort="Mülheim",
        status="accepted", tage=14,
        positionen=[
            dict(name="Ladentheke", beschreibung="Nussbaum furniert, mit Vitrine",
                 menge=1, einheit="Stueck", preis_brutto_eur=6400),
            dict(name="Wandregal hinter der Theke", menge=3, einheit="Meter",
                 preis_brutto_eur=210),
        ],
    ),
    dict(
        kunde="Familie Hoffmann", email="hoffmann@example.de",
        strasse="Am Hang 7", plz="54497", ort="Horath",
        status="arbeit_laeuft", tage=10, fortschritt=35,
        positionen=[
            dict(name="Parkett verlegen Wohnzimmer", beschreibung="Eiche Landhausdiele, 42 m²",
                 menge=42, einheit="qm", preis_brutto_eur=89),
            dict(name="Sockelleisten", menge=28, einheit="Meter", preis_brutto_eur=12),
        ],
    ),
    dict(
        kunde="Praxis Dr. Reuter", email="praxis@reuter.example",
        strasse="Bahnhofstr. 31", plz="54470", ort="Bernkastel-Kues",
        status="arbeit_laeuft", tage=6, fortschritt=80,
        positionen=[
            dict(name="Empfangstresen", beschreibung="Weiss lackiert, mit Kabelkanal",
                 menge=1, einheit="Stueck", preis_brutto_eur=4200),
        ],
    ),
    dict(
        kunde="Herr Neumann", email="t.neumann@example.de",
        strasse="Moselstr. 5", plz="54492", ort="Zeltingen",
        status="arbeit_fertig", tage=3,
        positionen=[
            dict(name="Terrassendielen Laerche", menge=24, einheit="qm",
                 preis_brutto_eur=124),
            dict(name="Unterkonstruktion", menge=1, einheit="Pauschal",
                 preis_brutto_eur=680),
        ],
    ),
    # Abgebrochen ist KEIN erlaubter Startschritt (die Neuanlage kennt nur
    # den Lifecycle) — dieser hier wird angelegt und danach abgebrochen,
    # damit die Liste auch eine graue Karte zeigt.
    dict(
        kunde="Frau Sander", email="sander@example.de",
        strasse="Kirchgasse 9", plz="54497", ort="Horath",
        status="accepted", tage=2, abbrechen=True,
        positionen=[
            dict(name="Kuechenarbeitsplatte", beschreibung="Buche, 2,60 m",
                 menge=1, einheit="Stueck", preis_brutto_eur=940),
        ],
    ),
]


async def _tenant_id(slug: str):
    from core.models.tenant import Tenant
    async with get_session() as s:
        t = (await s.execute(
            select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        return t.id if t else None


async def anlegen(tid) -> None:
    from core.models.angebot import Angebot, ANGEBOT_STATUS_ABGEBROCHEN

    jetzt = dt.datetime.now(dt.timezone.utc)
    for b in BEISPIELE:
        res = await create_auftrag_manuell(
            tid, kunde_name=b["kunde"], positionen=b["positionen"],
            status=b["status"], kunde_email=b.get("email"),
            kunde_strasse=b.get("strasse"), kunde_plz=b.get("plz"),
            kunde_ort=b.get("ort"), quelle=QUELLE,
        )
        if not res.get("ok"):
            print(f"  ! {b['kunde']}: {res.get('error')}")
            continue
        # Alter, Regler-Stand und Abbruch nachziehen — das sind Zustaende,
        # die im echten Leben erst NACH der Anlage entstehen.
        async with get_session() as s:
            a = (await s.execute(select(Angebot).where(
                Angebot.id == uuid.UUID(res["id"])))).scalar_one()
            a.created_at = jetzt - dt.timedelta(days=b.get("tage", 0))
            if b.get("fortschritt"):
                a.arbeit_fortschritt = b["fortschritt"]
            if b.get("abbrechen"):
                a.status = ANGEBOT_STATUS_ABGEBROCHEN
            status = a.status
            await s.commit()
        zusatz = f", Fortschritt {b['fortschritt']} %" if b.get("fortschritt") else ""
        print(f"  + {b['kunde']}: {res['gesamt_brutto_eur']:.2f} EUR, "
              f"{status}{zusatz}")


async def loeschen(tid) -> None:
    from core.models.angebot import Angebot
    from core.models.kunde import Kunde
    from core.models.rechnung import Rechnung

    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == tid,
                                  Angebot.quelle == QUELLE))).scalars().all()
        namen = {a.kunde_name for a in rows}
        for a in rows:
            print(f"  - {a.kunde_name} ({a.status})")
            await s.delete(a)          # Positionen haengen per CASCADE dran
        await s.commit()

    # Die Kunden aus dem Kundenstamm nur dann mitnehmen, wenn wirklich
    # nichts mehr an ihnen haengt — an einem Namen kann auch echte
    # Historie kleben.
    async with get_session() as s:
        for name in sorted(namen):
            k = (await s.execute(
                select(Kunde).where(Kunde.tenant_id == tid,
                                    Kunde.name == name))).scalar_one_or_none()
            if k is None:
                continue
            noch_da = (await s.execute(
                select(Angebot.id).where(Angebot.kunde_id == k.id).limit(1)
            )).scalar_one_or_none() or (await s.execute(
                select(Rechnung.id).where(Rechnung.kunde_id == k.id).limit(1)
            )).scalar_one_or_none()
            if noch_da:
                print(f"  ~ Kunde {name!r} bleibt (haengt noch an echten Belegen)")
                continue
            await s.delete(k)
            print(f"  - Kunde {name!r}")
        await s.commit()


async def main() -> None:
    discover_plugins()
    slug = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    weg = "--loeschen" in sys.argv

    tid = await _tenant_id(slug)
    if tid is None:
        print(f"Kein Tenant mit slug={slug!r}")
        return
    if weg:
        print(f"Raeume Dummy-Auftraege von Tenant {slug!r} weg:")
        await loeschen(tid)
    else:
        print(f"Lege Dummy-Auftraege fuer Tenant {slug!r} an:")
        await anlegen(tid)
    print("Fertig.")


asyncio.run(main())
