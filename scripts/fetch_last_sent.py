"""Holt die zuletzt gesendete Mail aus dem Sent-Ordner via Graph API.

Nutzt das gleiche OAuth-Token wie die Pipeline. Default-Filter:
to=svenj05@gmx.de Subject startet mit 'Re: [Pipeline-Test]'.
Druckt Subject + Plain-Body + die ersten Zeilen vom HTML-Body.
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx
from sqlalchemy import select

from core.database import AsyncSessionLocal
from core.integrations.microsoft import GRAPH_API_BASE, get_microsoft_token
from core.models import OAuthToken, Tenant


async def main() -> int:
    to_filter = sys.argv[1] if len(sys.argv) > 1 else "svenj05@gmx.de"
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(Tenant, OAuthToken)
            .join(OAuthToken, OAuthToken.tenant_id == Tenant.id)
            .where(OAuthToken.provider == "microsoft")
            .order_by(OAuthToken.employee_id.is_(None).desc())
        )).first()
    if not row:
        print("Kein Microsoft-Token")
        return 2
    tenant, token = row
    access = await get_microsoft_token(tenant.id, employee_id=token.employee_id)
    headers = {"Authorization": f"Bearer {access}"}

    url = (
        f"{GRAPH_API_BASE}/me/mailFolders/SentItems/messages"
        f"?$top=5&$orderby=sentDateTime desc"
        f"&$select=id,subject,toRecipients,sentDateTime,bodyPreview,body"
    )
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url, headers=headers)
        r.raise_for_status()
        msgs = r.json().get("value", [])

    target = None
    for m in msgs:
        tos = [t["emailAddress"]["address"].lower()
               for t in m.get("toRecipients", [])]
        if to_filter.lower() in tos and m.get("subject", "").startswith("Re: [Pipeline-Test]"):
            target = m
            break
    if not target:
        print(f"Keine passende Sent-Mail an {to_filter} mit 'Re: [Pipeline-Test]' gefunden.")
        print("Letzte 5 Sent-Subjects:")
        for m in msgs:
            print(f"  - {m.get('sentDateTime')} {m.get('subject')!r}")
        return 1

    print(f"Subject: {target['subject']}")
    print(f"Sent:    {target['sentDateTime']}")
    print(f"To:      {[t['emailAddress']['address'] for t in target['toRecipients']]}")
    print()
    body = target.get("body") or {}
    ctype = body.get("contentType")
    content = body.get("content") or ""
    print(f"--- contentType: {ctype} ---")
    if ctype == "html":
        text = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        print("Plain-Extract:")
        print(text)
        print()
        print("--- HTML (raw, gekuerzt) ---")
        print(content[:3000])
    else:
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
