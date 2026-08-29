"""Testlauf geht NIE gegen die Produktionsdatenbank.

Bis zum Audit am 2026-08-29 gab es diese Datei nicht. Damit nahmen die
Tests `DATABASE_URL` aus der `.env` — also die Prod-DB. Belegen liess sich
das an ~20 Zeilen `unexpected EOF on client connection with an open
transaction` im Postgres-Log unmittelbar nach jedem Lauf, dazu eine
Fremdschluessel-Verletzung auf `app_usage_events`.

Persistiert hat dabei nichts (die Writes scheiterten oder wurden
zurueckgerollt), aber das war Glueck: ein Test, der zufaellig eine
gueltige `tenant_id` erwischt, schreibt in echte Kundendaten. Die CI
macht es richtig und faehrt eine eigene `gewerbeagent_ci` hoch — nur der
in CLAUDE.md dokumentierte lokale Weg tat es nicht.

Diese Datei biegt die Verbindung auf eine eigene Datenbank um, BEVOR
`config.settings` zum ersten Mal gelesen wird (pytest importiert conftest
vor allem anderen), und legt sie beim ersten Lauf an.
"""
from __future__ import annotations

import os
import re
import sys

TEST_DB_NAME = "gewerbeagent_test"


def _test_url(prod_url: str) -> str:
    """Dieselbe Verbindung, andere Datenbank."""
    return re.sub(r"/[^/?]+(\?|$)", f"/{TEST_DB_NAME}\\1", prod_url, count=1)


def _sicherstellen(prod_url: str, test_url: str) -> None:
    """Legt die Test-DB an und zieht das Schema hoch — einmalig."""
    import asyncio

    import asyncpg

    async def _go() -> bool:
        # Verbindungsparameter aus der URL, aber auf die Wartungs-DB.
        admin_url = _test_url(prod_url).replace(
            f"/{TEST_DB_NAME}", "/postgres",
        ).replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(admin_url)
        try:
            da = await conn.fetchval(
                "select 1 from pg_database where datname = $1", TEST_DB_NAME,
            )
            if da:
                return False
            await conn.execute(f'create database "{TEST_DB_NAME}"')
            return True
        finally:
            await conn.close()

    neu = asyncio.run(_go())
    if not neu:
        return

    print(f"[conftest] {TEST_DB_NAME} angelegt — Schema wird gezogen …")
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_url)
    command.upgrade(cfg, "head")


_prod = os.environ.get("DATABASE_URL", "")
if not _prod:
    # .env noch nicht gelesen — settings tut das beim Import. Wir brauchen
    # den Wert aber VORHER, also hier einmal selbst nachsehen.
    from pathlib import Path

    for zeile in Path(".env").read_text().splitlines() if Path(".env").exists() else []:
        if zeile.startswith("DATABASE_URL="):
            _prod = zeile.split("=", 1)[1].strip()
            break

if _prod and TEST_DB_NAME not in _prod:
    _neu = _test_url(_prod)
    if _neu == _prod:
        print(
            "[conftest] DATABASE_URL laesst sich nicht auf eine Test-DB "
            "umbiegen — Abbruch, bevor etwas gegen Produktion laeuft.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        _sicherstellen(_prod, _neu)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[conftest] Test-DB nicht verfuegbar ({exc}). Abbruch — der "
            "Lauf wuerde sonst gegen Produktion gehen.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    os.environ["DATABASE_URL"] = _neu
