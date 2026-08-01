"""Read/Write-Pruefung der Automatisierungs-Stufen gegen die echte DB.

Setzt die Stufen fuer einen Test-Tenant, prueft die Wirkung im
command_center-Gating und raeumt hinterher wieder auf.

    docker exec -w /app gewerbeagent_framework uv run python -m scripts.verify_automations
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from sqlalchemy import delete, select

from core.database import AsyncSessionLocal
from core.models import Tenant
from core.plugin_system.registry import discover_plugins

SLUG = os.environ.get("TENANT_SLUG", "pilot")


async def main() -> int:
    discover_plugins()
    import core.ai.command_center as cc
    from core.features.automation_check import (
        automation_modes_for_tenant,
        invalidate_automation_cache,
        mode_for_automation,
        set_automation_mode,
    )
    from core.features.automations import AUTOMATIONS
    from core.features.check import enabled_features_for_tenant
    from core.models import AutomationSetting

    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(
            select(Tenant).where(Tenant.slug == SLUG)
        )).scalar_one_or_none()
        if tenant is None:
            print(f"FEHLER: Tenant {SLUG} fehlt")
            return 1
        emp = None
        from core.models import Employee
        emp = (await s.execute(
            select(Employee).where(Employee.tenant_id == tenant.id)
            .order_by(Employee.created_at)
        )).scalars().first()

    feats = set(await enabled_features_for_tenant(tenant.id))
    print(f"Tenant {SLUG}: {len(feats)} Features aktiv")

    def ctx_for(modes):
        return cc.Ctx(tenant=tenant, employee=emp, tid=tenant.id,
                      features=feats, automation_modes=modes)

    # --- 1) Ausgangslage: alles auf Default ---
    modes = await automation_modes_for_tenant(tenant.id)
    print("1) Defaults gelesen:", json.dumps(modes, ensure_ascii=False))
    assert modes["termin_buchen"] == "assistiert", modes["termin_buchen"]
    assert modes["telefon_buchung"] == "automatisch", modes["telefon_buchung"]

    tools_default = {t.name for t in cc._available_tools(ctx_for(modes))}
    print("   termin_anlegen im Werkzeugkasten:", "termin_anlegen" in tools_default)
    assert "termin_anlegen" in tools_default

    # --- 2) manuell: Tool verschwindet + Hinweis fuer den Prompt ---
    ok = await set_automation_mode(tenant.id, "termin_buchen", "manuell")
    print("2) set termin_buchen=manuell ->", ok)
    assert ok
    modes = await automation_modes_for_tenant(tenant.id)
    assert modes["termin_buchen"] == "manuell", modes
    c = ctx_for(modes)
    tools_manuell = {t.name for t in cc._available_tools(c)}
    print("   termin_anlegen weg:", "termin_anlegen" not in tools_manuell)
    print("   termin_verschieben weg:", "termin_verschieben" not in tools_manuell)
    # Read-Tools bleiben: 'manuell' heisst "Q handelt nicht", nicht
    # "Q darf nichts mehr nachschauen".
    print("   freie_termine_finden (read) bleibt:",
          "freie_termine_finden" in tools_manuell)
    assert "freie_termine_finden" in tools_manuell
    assert "termin_anlegen" not in tools_manuell
    assert "termin_verschieben" not in tools_manuell
    hinweise = cc._manuelle_hinweise(c)
    print("   Prompt-Hinweis:", hinweise)
    assert any("Termine" in h for h in hinweise)
    # Bestaetigungs-Endpunkt darf danach nicht mehr ausfuehren
    res = await cc.execute_confirmed("termin_anlegen", {"kunde": "X"}, c)
    print("   execute_confirmed geblockt:", res)
    assert res["type"] == "error"

    # --- 3) automatisch: Tool da, Modus greift ---
    ok = await set_automation_mode(tenant.id, "termin_buchen", "automatisch")
    print("3) set termin_buchen=automatisch ->", ok)
    modes = await automation_modes_for_tenant(tenant.id)
    c = ctx_for(modes)
    print("   mode_for(termin_anlegen):", c.mode_for("termin_anlegen"))
    assert c.mode_for("termin_anlegen") == "automatisch"
    assert "termin_anlegen" in {t.name for t in cc._available_tools(c)}

    # --- 4) Ungueltige Stufe wird abgewiesen ---
    bad = await set_automation_mode(tenant.id, "telefon_buchung", "assistiert")
    print("4) telefon_buchung=assistiert (soll False) ->", bad)
    assert bad is False
    bad2 = await set_automation_mode(tenant.id, "gibtsnicht", "manuell")
    print("   unbekannter Key (soll False) ->", bad2)
    assert bad2 is False

    # --- 5) Hintergrund-Schalter ---
    await set_automation_mode(tenant.id, "mail_auto_antwort", "manuell")
    m = await mode_for_automation(tenant.id, "mail_auto_antwort")
    print("5) mail_auto_antwort:", m)
    assert m == "manuell"

    # --- Aufraeumen: alle Testzeilen wieder weg (= zurueck auf Default) ---
    async with AsyncSessionLocal() as s:
        await s.execute(delete(AutomationSetting).where(
            AutomationSetting.tenant_id == tenant.id))
        await s.commit()
    invalidate_automation_cache(tenant.id)
    rest = await automation_modes_for_tenant(tenant.id)
    print("aufgeraeumt, wieder Default:", rest["termin_buchen"], rest["mail_auto_antwort"])
    assert rest["termin_buchen"] == "assistiert"
    assert rest["mail_auto_antwort"] == "automatisch"

    print(f"\nALLE PRUEFUNGEN BESTANDEN ({len(AUTOMATIONS)} Automatisierungen registriert)")
    return 0


sys.exit(asyncio.run(main()))
