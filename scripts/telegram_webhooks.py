"""
Telegram-Webhook-Verwaltung fuer Betriebe mit eigenem Bot.

Nutzung:
  uv run python -m scripts.telegram_webhooks --health
  uv run python -m scripts.telegram_webhooks --reregister
  uv run python -m scripts.telegram_webhooks --health --slug schreinerei_mueller

Hintergrund:
Jeder Betrieb mit eigenem Bot (telegram_notify.bot_token gesetzt) hat einen
eigenen Telegram-Webhook auf /webhook/<slug>/telegram_notify/incoming. Der
geteilte globale Bot (_global) wird hier bewusst ausgelassen.

- --health     : ruft getWebhookInfo fuer jeden Bot, zeigt aktuelle URL,
                 Pending-Updates und letzten Fehler. Markiert mit ⚠ wenn die
                 hinterlegte URL nicht zur erwarteten passt (z.B. nach einem
                 Domain-Wechsel) oder ein Zustellfehler vorliegt.
- --reregister : setzt setWebhook fuer alle (oder --slug) NEU. Noetig nach
                 Wechsel von public_url (Domain) oder telegram_webhook_secret
                 sowie zum Wiederbeleben toter Webhooks. Das ist die
                 O(N)-Skalierungs-Versicherung bei vielen Betrieben.

Der Token wird verschluesselt gelesen (try_decrypt mit Klartext-Fallback).
"""
from __future__ import annotations

import argparse
import asyncio

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.models import Tenant, ToolConfig
from core.security.encryption import try_decrypt
from config.settings import settings

TELEGRAM_API = "https://api.telegram.org"


async def _collect_bots(slug_filter: str | None = None) -> list[tuple[str, str]]:
    """(slug, token) aller Betriebe mit eigenem Bot-Token (ohne _global)."""
    async with AsyncSessionLocal() as s:
        q = (
            select(Tenant.slug, ToolConfig.config)
            .join(ToolConfig, ToolConfig.tenant_id == Tenant.id)
            .where(ToolConfig.tool_name == "telegram_notify")
        )
        if slug_filter:
            q = q.where(Tenant.slug == slug_filter)
        rows = (await s.execute(q)).all()
    bots: list[tuple[str, str]] = []
    for slug, cfg in rows:
        if slug == "_global":
            continue
        token = try_decrypt((cfg or {}).get("bot_token"))
        if token:
            bots.append((slug, token))
    return bots


def _expected_url(slug: str) -> str:
    base = (settings.public_url or "").rstrip("/")
    return f"{base}/webhook/{slug}/telegram_notify/incoming"


async def _health(bots: list[tuple[str, str]]) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        for slug, token in bots:
            try:
                resp = await client.get(f"{TELEGRAM_API}/bot{token}/getWebhookInfo")
                data = (resp.json() or {}).get("result", {})
            except Exception as e:
                print(f"  ✗ {slug}: getWebhookInfo-Fehler: {e}")
                continue
            url = data.get("url") or "(keiner)"
            expected = _expected_url(slug)
            flag = "✓" if url == expected else "⚠"
            print(f"  {flag} {slug}")
            print(f"      url: {url}")
            if url != expected:
                print(f"      erwartet: {expected}")
            pending = data.get("pending_update_count", 0)
            if pending:
                print(f"      pending updates: {pending}")
            last_err = data.get("last_error_message")
            if last_err:
                print(f"      letzter Fehler: {last_err} (date={data.get('last_error_date')})")


async def _reregister(bots: list[tuple[str, str]]) -> None:
    secret = (settings.telegram_webhook_secret or "").strip()
    async with httpx.AsyncClient(timeout=15.0) as client:
        for slug, token in bots:
            url = _expected_url(slug)
            payload: dict = {"url": url}
            if secret:
                payload["secret_token"] = secret
            try:
                resp = await client.post(
                    f"{TELEGRAM_API}/bot{token}/setWebhook", json=payload,
                )
                data = resp.json() if resp.content else {}
                ok = resp.status_code == 200 and data.get("ok")
                if ok:
                    print(f"  ✓ {slug} -> {url}")
                else:
                    desc = data.get("description") or f"HTTP {resp.status_code}"
                    print(f"  ✗ {slug}: {desc}")
            except Exception as e:
                print(f"  ✗ {slug}: setWebhook-Fehler: {e}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Telegram-Webhooks der Betriebs-Bots verwalten",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--health", action="store_true",
                   help="getWebhookInfo fuer alle Bots (Default)")
    g.add_argument("--reregister", action="store_true",
                   help="setWebhook fuer alle Bots NEU setzen")
    ap.add_argument("--slug", help="auf einen Betrieb beschraenken")
    args = ap.parse_args()

    bots = await _collect_bots(args.slug)
    if not bots:
        print("Keine Betriebe mit eigenem Bot-Token gefunden"
              + (f" (slug={args.slug})." if args.slug else "."))
        return
    print(f"{len(bots)} Betrieb(e) mit eigenem Bot:")

    if args.reregister:
        if not (settings.public_url or "").strip():
            print("FEHLER: settings.public_url ist leer — Webhook-URL nicht baubar.")
            return
        await _reregister(bots)
    else:
        await _health(bots)


if __name__ == "__main__":
    asyncio.run(main())
