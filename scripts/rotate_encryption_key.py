#!/usr/bin/env python3
"""Encryption-Key-Rotation (Phase B11).

Tauscht den ENCRYPTION_KEY in einer kontrollierten Downtime aus:
  1. Container stoppen (Framework, NICHT Postgres)
  2. Skript ausfuehren mit --old-key=<aktueller> --new-key=<neuer>
     → liest alle Ciphertexts mit OLD, schreibt mit NEW zurueck
  3. .env: ENCRYPTION_KEY=<neuer Wert>
  4. Container starten

Was verschluesselt ist:
  - oauth_tokens._refresh_token_encrypted
  - oauth_tokens._access_token_encrypted
  - tool_configs.config['encrypted_api_key']  (Lexware-Keys)
  - tool_configs.config['encrypted_*']        (defensiv: jede 'encrypted_*'-Key)
  - tool_configs.config['bot_token']          (Telegram-Bot pro Betrieb —
    verschluesselt gespeichert, folgt aber NICHT dem 'encrypted_'-Schema;
    muss explizit mit-rotiert werden, sonst Daten-Verlust)

Dry-Run zuerst (Default), dann mit --execute fuer den echten Rotation.

WICHTIG: Backup der DB direkt vor der Rotation machen — falls die
Verschluesselung schief geht, ist der Backup das Sicherheitsnetz.

Verwendung:
    # Backup zuerst!
    ./scripts/backup_db.sh

    # Dry-run zum Pruefen
    python -m scripts.rotate_encryption_key \\
        --old-key="$(grep ENCRYPTION_KEY .env | cut -d= -f2-)" \\
        --new-key="$(openssl rand -base64 48)"

    # Echt rotieren
    python -m scripts.rotate_encryption_key \\
        --old-key="..." --new-key="..." --execute
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import logging
import sys
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update

from core.database import AsyncSessionLocal
from core.models import OAuthToken, ToolConfig

logger = logging.getLogger("rotate_encryption_key")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _fernet_for(key: str) -> Fernet:
    """Erzeugt Fernet-Instance fuer einen gegebenen Plain-Text-Key.

    Identisch zur Logik in core/security/encryption.py:_get_fernet —
    bewusst kopiert damit das Skript komplett standalone ist.
    """
    key_bytes = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _recrypt(old: Fernet, new: Fernet, ciphertext: str | None) -> str | None:
    """Liest Ciphertext mit OLD, gibt Ciphertext mit NEW zurueck.

    None / leerer String → unchanged. InvalidToken propagiert nach oben
    damit der Caller entscheiden kann (zaehlen / abbrechen).
    """
    if not ciphertext:
        return ciphertext
    plain = old.decrypt(ciphertext.encode()).decode()
    return new.encrypt(plain.encode()).decode()


async def _rotate_oauth_tokens(
    old: Fernet, new: Fernet, *, execute: bool,
) -> dict:
    """Liest alle OAuthToken-Zeilen, recrypted refresh + access."""
    stats = {"total": 0, "refresh_recrypted": 0, "access_recrypted": 0, "errors": 0}
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(OAuthToken))).scalars().all()
        stats["total"] = len(rows)

        for row in rows:
            # Properties triggern decrypt — wir wollen rohe Cipher-Bytes,
            # also auf die _-prefixed Spalten direkt zugreifen.
            try:
                old_refresh_cipher = row._refresh_token_encrypted  # noqa: SLF001
                if old_refresh_cipher:
                    new_refresh_cipher = _recrypt(old, new, old_refresh_cipher)
                    if execute:
                        row._refresh_token_encrypted = new_refresh_cipher  # noqa: SLF001
                    stats["refresh_recrypted"] += 1
            except InvalidToken:
                logger.error(
                    f"oauth_token {row.id}: refresh InvalidToken — "
                    f"OLD-Key passt nicht. Skip."
                )
                stats["errors"] += 1

            try:
                old_access_cipher = row._access_token_encrypted  # noqa: SLF001
                if old_access_cipher:
                    new_access_cipher = _recrypt(old, new, old_access_cipher)
                    if execute:
                        row._access_token_encrypted = new_access_cipher  # noqa: SLF001
                    stats["access_recrypted"] += 1
            except InvalidToken:
                logger.error(
                    f"oauth_token {row.id}: access InvalidToken — Skip."
                )
                stats["errors"] += 1

        if execute:
            await s.commit()
    return stats


# Felder, die verschluesselt gespeichert werden, aber NICHT dem
# 'encrypted_'-Namensschema folgen (historisch gewachsen). Diese muessen
# bei der Rotation mit-recrypted werden, sonst sind sie nach dem
# Key-Wechsel dauerhaft unentschluesselbar (Daten-Verlust).
_PLAINTEXT_NAMED_ENCRYPTED_KEYS = {"bot_token"}


async def _rotate_tool_configs(
    old: Fernet, new: Fernet, *, execute: bool,
) -> dict:
    """Sucht in ToolConfig.config alle 'encrypted_*'-Keys + recryptet.

    Conventions:
      - cfg['encrypted_api_key']  (Lexware)
      - andere encrypted_*-Felder werden defensiv mitgenommen
      - cfg['bot_token'] (Telegram-Bot pro Betrieb) wird explizit
        mitgenommen, obwohl es nicht dem 'encrypted_'-Schema folgt.
        Es kann historisch im Klartext vorliegen (try_decrypt-Fallback);
        in dem Fall bleibt es unveraendert (kein harter Fehler).
    """
    stats = {"total": 0, "configs_touched": 0, "fields_recrypted": 0, "errors": 0}
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(ToolConfig))).scalars().all()
        stats["total"] = len(rows)

        for row in rows:
            cfg = dict(row.config or {})
            touched = False
            for key, val in list(cfg.items()):
                is_named_encrypted = key in _PLAINTEXT_NAMED_ENCRYPTED_KEYS
                if not key.startswith("encrypted_") and not is_named_encrypted:
                    continue
                if not isinstance(val, str) or not val:
                    continue
                try:
                    cfg[key] = _recrypt(old, new, val)
                    stats["fields_recrypted"] += 1
                    touched = True
                except InvalidToken:
                    if is_named_encrypted:
                        # bot_token kann Klartext-Altbestand sein (vor der
                        # Verschluesselung-at-rest gespeichert). Klartext
                        # bleibt via try_decrypt lesbar → unveraendert
                        # lassen, kein harter Fehler.
                        logger.warning(
                            f"tool_config {row.id} field={key}: kein gueltiges "
                            f"Ciphertext (vermutlich Klartext-Altbestand) — "
                            f"unveraendert gelassen."
                        )
                    else:
                        logger.error(
                            f"tool_config {row.id} field={key} InvalidToken — Skip."
                        )
                        stats["errors"] += 1
            if touched:
                stats["configs_touched"] += 1
                if execute:
                    await s.execute(
                        update(ToolConfig)
                        .where(ToolConfig.id == row.id)
                        .values(config=cfg)
                    )

        if execute:
            await s.commit()
    return stats


def _sanity_check_keys(old_key: str, new_key: str) -> None:
    if old_key == new_key:
        sys.exit("FEHLER: --old-key und --new-key sind identisch — Abbruch.")
    if len(new_key) < 32:
        sys.exit("FEHLER: --new-key ist kuerzer als 32 Zeichen — unsicher.")
    if len(new_key) < 64:
        logger.warning(
            "WARN: --new-key ist nur %d Zeichen lang. Empfohlen sind 64+ "
            "(`openssl rand -base64 48`).", len(new_key),
        )


async def _main(old_key: str, new_key: str, execute: bool) -> int:
    _sanity_check_keys(old_key, new_key)

    old = _fernet_for(old_key)
    new = _fernet_for(new_key)

    mode = "EXECUTE" if execute else "DRY-RUN"
    logger.info(f"=== Encryption-Key-Rotation [{mode}] ===")

    oauth_stats = await _rotate_oauth_tokens(old, new, execute=execute)
    logger.info(
        f"OAuthTokens: total={oauth_stats['total']} "
        f"refresh_recrypted={oauth_stats['refresh_recrypted']} "
        f"access_recrypted={oauth_stats['access_recrypted']} "
        f"errors={oauth_stats['errors']}"
    )

    tc_stats = await _rotate_tool_configs(old, new, execute=execute)
    logger.info(
        f"ToolConfigs: total={tc_stats['total']} "
        f"configs_touched={tc_stats['configs_touched']} "
        f"fields_recrypted={tc_stats['fields_recrypted']} "
        f"errors={tc_stats['errors']}"
    )

    total_errors = oauth_stats["errors"] + tc_stats["errors"]
    if total_errors > 0:
        logger.error(
            f"!!! {total_errors} Felder konnten nicht recrypted werden. "
            "Vermutlich passt der --old-key nicht."
        )
        return 1

    if execute:
        logger.info(
            "=== ROTATION FERTIG. JETZT TUN: ===\n"
            "  1. ENCRYPTION_KEY in .env auf den neuen Wert setzen\n"
            "  2. docker compose -p prod restart framework\n"
            "  3. /status im Telegram pruefen: alle Tokens funktionieren\n"
            "Bei Problemen: DB-Restore aus dem Pre-Rotation-Backup."
        )
    else:
        logger.info(
            "=== DRY-RUN OK — nichts geschrieben. Mit --execute echt rotieren. ==="
        )
    return 0


def cli() -> None:
    p = argparse.ArgumentParser(description="Rotate ENCRYPTION_KEY.")
    p.add_argument("--old-key", required=True, help="Bisheriger ENCRYPTION_KEY")
    p.add_argument("--new-key", required=True, help="Neuer ENCRYPTION_KEY")
    p.add_argument(
        "--execute", action="store_true",
        help="Echt schreiben. Ohne dieses Flag: Dry-Run.",
    )
    args = p.parse_args()
    rc = asyncio.run(_main(args.old_key, args.new_key, args.execute))
    sys.exit(rc)


if __name__ == "__main__":
    cli()
