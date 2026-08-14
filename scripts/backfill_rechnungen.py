"""Traegt bereits abgerechnete Auftraege in die Rechnungs-Tabelle nach.

Hintergrund: der Weg „fertiger Auftrag -> Rechnung" schrieb bis 2026-08-14
ausschliesslich das Angebot fort. Die Geld-Auswertungen (offene Posten,
Ueberfaelligkeit, Bezahl-Monitor, Tagesbericht, Zahlungserinnerung) lesen
aber alle die Tabelle `rechnungen` — schon verschickte Rechnungen waren
fuer sie unsichtbar. Der Code legt die Zeile jetzt selbst an; dieses Skript
holt den Bestand nach.

Aufruf im Container (PYTHONPATH=/app, Arbeitsverzeichnis /app):
    python scripts/backfill_rechnungen.py <tenant-slug>            # Trockenlauf
    python scripts/backfill_rechnungen.py <tenant-slug> --schreiben

Idempotent: es wird nur angelegt, was noch keine Zeile mit derselben
`lexware_invoice_id` hat.
"""
import asyncio
import sys

from core.plugin_system import discover_plugins


async def main() -> None:
    discover_plugins()

    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.angebot import (
        Angebot, ANGEBOT_STATUS_RECHNUNG_GESENDET)
    from core.models.angebot_position import AngebotPosition
    from core.models.rechnung import (
        Rechnung, RECHNUNG_STATUS_MAIL_SENT)
    from core.models.tenant import Tenant

    slug = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    schreiben = "--schreiben" in sys.argv

    async with get_session() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if tenant is None:
            print(f"Tenant '{slug}' nicht gefunden.")
            return

        auftraege = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == tenant.id)
            .where(Angebot.status == ANGEBOT_STATUS_RECHNUNG_GESENDET)
            .where(Angebot.lexware_invoice_id.is_not(None))
        )).scalars().all()

    print(f"{slug}: {len(auftraege)} abgerechnete Auftraege mit Lexware-Rechnung")
    angelegt = 0

    for a in auftraege:
        async with get_session() as s:
            schon_da = (await s.execute(
                select(Rechnung)
                .where(Rechnung.tenant_id == tenant.id)
                .where(Rechnung.lexware_invoice_id == a.lexware_invoice_id)
            )).scalar_one_or_none()
            if schon_da is not None:
                print(f"  = {a.kunde_name}: hat schon eine Zeile")
                continue

            positions = (await s.execute(
                select(AngebotPosition)
                .where(AngebotPosition.angebot_id == a.id))).scalars().all()
            betrag = a.gesamtbetrag_brutto_eur or sum(
                (p.menge or 0) * (p.preis_brutto_eur or 0) for p in positions)

            print(f"  + {a.kunde_name}: {betrag} EUR "
                  f"(versendet {a.mail_sent_at or a.abgeschlossen_am})")
            if not schreiben:
                continue

            r = Rechnung(
                tenant_id=tenant.id,
                input_type="auftrag",
                # Der Versand ist nachweislich passiert (Status des Auftrags),
                # also direkt mail_sent — nur dieser Status kommt in die
                # Bezahl-Ueberwachung.
                status=RECHNUNG_STATUS_MAIL_SENT,
                kunde_name=a.kunde_name,
                kunde_email=a.kunde_email,
                kunde_strasse=a.kunde_strasse,
                kunde_plz=a.kunde_plz,
                kunde_ort=a.kunde_ort,
                betrag_brutto_eur=betrag,
                lexware_invoice_id=a.lexware_invoice_id,
                lexware_voucher_number=a.lexware_voucher_number,
                leistung_titel=f"Auftrag {a.kunde_name}",
                raw_input_text=f"nachgetragen aus Auftrag {a.id}",
                mail_sent_to=a.mail_sent_to or a.kunde_email,
                mail_sent_at=a.mail_sent_at or a.abgeschlossen_am,
                # Stellt der Monitor gleich beim ersten Lauf fest, dass eine
                # dieser Altrechnungen laengst bezahlt ist, setzt er
                # bezahlt_am auf HEUTE. Ohne diesen Marker meldete der
                # 18-Uhr-Bericht sie als „heute bezahlt" — eine
                # Erfolgsmeldung fuer Geld, das vor Monaten kam.
                paid_notification_sent=True,
            )
            s.add(r)
            await s.flush()
            a_db = (await s.execute(
                select(Angebot).where(Angebot.id == a.id))).scalar_one()
            a_db.rechnung_id = r.id
            await s.commit()
            angelegt += 1

    if schreiben:
        print(f"\n{angelegt} Rechnungszeile(n) nachgetragen. "
              f"Der Bezahl-Monitor prueft sie beim naechsten Lauf (30 Min).")
    else:
        print("\nTrockenlauf — mit --schreiben wirklich anlegen.")


if __name__ == "__main__":
    asyncio.run(main())
