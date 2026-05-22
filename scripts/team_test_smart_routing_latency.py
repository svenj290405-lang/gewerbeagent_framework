"""Latenz-Messung Smart-Routing (echte Gemini-Calls, KEINE DB-Mutation).

Ruft rank_employee_for_request direkt mit synthetischen Kandidaten auf
(emil=elektrik, klaus=sanitaer, sven=keine Skills) — misst pro Anfrage
die reine Gemini-Routing-Latenz. Trennt Cold-Start (1. Call: vertexai.init
+ Modell-Konstruktion + erster Roundtrip) von Warm (Steady-State, das
zaehlt im Hot-Path Mail/Voice/Anfrage).

Ephemer — nach der Messung wieder loeschen. Legt nichts an, loescht nichts.
"""
from __future__ import annotations

import asyncio
import statistics
import time

from core.ai.gemini import rank_employee_for_request

CANDIDATES = [
    {"slug": "sven", "name": "Sven (Inhaber)", "skills": []},
    {"slug": "emil", "name": "Emil Elektrik", "skills": ["elektrik"]},
    {"slug": "klaus", "name": "Klaus Klempner", "skills": ["sanitaer"]},
]

ANFRAGEN = [
    "Die Steckdose in der Küche gibt keinen Strom mehr.",
    "Der Abfluss in der Dusche ist verstopft, alles steht voll Wasser.",
    "Mein Durchlauferhitzer macht keinen Strom und es kommt kein warmes Wasser.",
    "Bitte einmal die ganze Wohnung neu streichen.",  # keiner passt -> null erwartet
]


async def _timed(req: str) -> tuple[str | None, float]:
    t0 = time.perf_counter()
    slug = await rank_employee_for_request(req, CANDIDATES, tenant_id=None)
    return slug, (time.perf_counter() - t0) * 1000.0


async def main():
    print("=" * 74)
    print("Smart-Routing LATENZ — rank_employee_for_request (echte Gemini-Calls)")
    print("Modell gemini-2.5-flash @ europe-west3 | Kandidaten: sven/emil/klaus")
    print("=" * 74)

    warm_ms: list[float] = []
    for i, req in enumerate(ANFRAGEN):
        slug, ms = await _timed(req)
        kind = "COLD (inkl. vertexai.init)" if i == 0 else "warm"
        pick = slug if slug else "null → Stichwort-Fallback"
        print(f"\n[{kind:>26}] {ms:7.0f} ms")
        print(f"   Anfrage: {req}")
        print(f"   Gemini  → {pick}")
        if i > 0:
            warm_ms.append(ms)

    # Zweite Warm-Runde fuer stabilere Statistik
    for req in ANFRAGEN:
        _, ms = await _timed(req)
        warm_ms.append(ms)

    print("\n" + "-" * 74)
    print(f"Warm-Latenz ueber {len(warm_ms)} Calls (Hot-Path-relevant):")
    print(f"   min    = {min(warm_ms):7.0f} ms")
    print(f"   median = {statistics.median(warm_ms):7.0f} ms")
    print(f"   mean   = {statistics.mean(warm_ms):7.0f} ms")
    print(f"   max    = {max(warm_ms):7.0f} ms")
    print(f"\nRouter-Timeout-Schwelle (asyncio.wait_for): 8000 ms")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
