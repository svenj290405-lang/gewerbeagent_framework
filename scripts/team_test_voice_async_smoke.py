"""Live-Smoke-Test der asynchronen Voice-Terminsuche.

Faehrt den ECHTEN deployten Handler-Code in-process: starte_terminsuche ->
echter Hintergrund-Task (echtes Gemini-Skill-Routing + echte Kalender-/
Outlook-Slot-Suche fuer Tenant pilot) -> hole_terminvorschlaege. Misst die
Latenz. READ-ONLY: bucht/storniert nichts.

Umgeht bewusst die HTTP/Webhook-Auth-Schicht (on_webhook), damit der Test
auch laeuft, wenn ELEVENLABS_WEBHOOK_SECRET nicht gesetzt ist (dann ist der
HTTP-Webhook fail-closed -> 401). Getestet wird die Feature-Logik:
non-blocking Start + Hintergrund-Arbeit + Ergebnis-Abholung.

Lauf im Container:  uv run python -m scripts.team_test_voice_async_smoke
"""
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

TENANT = "pilot"
WUNSCHZEIT = "2026-05-26T10:00"   # Mo naechste Woche
ANLIEGEN = "Meine Heizung wird nicht richtig warm, bitte einen Termin."


async def main():
    from core.plugin_system.registry import discover_plugins
    discover_plugins()
    from plugins.voice_init import handler as vh

    plugin = vh.Plugin(SimpleNamespace(tenant_id=uuid.uuid4(), config={}))
    payload = {"tenant_slug": TENANT, "wunschzeit": WUNSCHZEIT, "anliegen": ANLIEGEN}

    print("=" * 74)
    print("SMOKE: asynchrone Voice-Terminsuche (in-process, echtes Gemini+Kalender)")
    print("=" * 74)

    t0 = time.perf_counter()
    start = await plugin._handle_starte_terminsuche(payload)
    ms_start = (time.perf_counter() - t0) * 1000
    print(f"\n[starte_terminsuche]  {ms_start:6.0f} ms  -> {start}")
    job_id = start.get("job_id")
    if not job_id:
        print("\nKEIN job_id -> Abbruch.")
        return

    immediate = await plugin._handle_hole_terminvorschlaege({"job_id": job_id})
    print(f"[sofort-poll]                  status={immediate.get('status')!r}  "
          f"(erwartet 'laeuft' = Start blockiert nicht)")

    # Echten Hintergrund-Task abwarten (das ist die Latenz, die der Anrufer
    # NICHT mehr als Stille erlebt).
    t1 = time.perf_counter()
    await vh._TERMINSUCHE_JOBS[job_id]["task"]
    ms_bg = (time.perf_counter() - t1) * 1000

    done = await plugin._handle_hole_terminvorschlaege({"job_id": job_id})
    print(f"\n[Hintergrund-Arbeit fertig]  {ms_bg:6.0f} ms")
    print(f"   erfolg={done.get('erfolg')} status={done.get('status')} "
          f"anzahl={done.get('anzahl')}")
    for s in (done.get("slots") or [])[:3]:
        print(f"     - {s.get('datum')} {s.get('uhrzeit')} slot_id={s.get('slot_id')}")
    if done.get("routing"):
        print(f"   routing={done['routing']}")
    if not done.get("erfolg"):
        print(f"   nachricht={done.get('nachricht')}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
