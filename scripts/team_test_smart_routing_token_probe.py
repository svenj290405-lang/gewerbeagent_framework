"""Probe: ist max_output_tokens=512 die Ursache der '```json'-Truncation?

Schickt denselben Routing-Prompt mehrfach durch das Vertex-Modell bei
512 vs 2048 Tokens und zeigt finish_reason + rohen Text. MAX_TOKENS bei
512 => Thinking frisst das Budget => Output bricht nach dem Fence ab.

Ephemer — danach loeschen. Keine DB-Mutation.
"""
from __future__ import annotations

import asyncio

from vertexai.generative_models import GenerationConfig
from core.ai.gemini import _get_model

PROMPT = (
    "Du ordnest eine eingehende Kunden-Anfrage dem fachlich am besten "
    "geeigneten Mitarbeiter eines Handwerksbetriebs zu.\n\n"
    "ANFRAGE:\nMein Durchlauferhitzer macht keinen Strom und es kommt kein "
    "warmes Wasser.\n\n"
    'VERFUEGBARE MITARBEITER:\n- slug "sven" (Sven (Inhaber)) — Skills: '
    "(keine Skills hinterlegt)\n"
    '- slug "emil" (Emil Elektrik) — Skills: elektrik\n'
    '- slug "klaus" (Klaus Klempner) — Skills: sanitaer\n\n'
    "Waehle GENAU EINEN Mitarbeiter, dessen Skills fachlich am besten zum "
    "Gewerk/Problem der Anfrage passt. Wenn KEIN Mitarbeiter fachlich klar "
    "passt, gib slug null zurueck.\n"
    "Antworte AUSSCHLIESSLICH mit gueltigem JSON (kein Markdown, keine "
    'Erklaerung), z.B.:\n{"slug": "max", "reason": "Heizung -> heizung-Skill"}'
)


def _run(max_tokens: int):
    model = _get_model()
    cfg = GenerationConfig(temperature=0.0, max_output_tokens=max_tokens)
    resp = model.generate_content(PROMPT, generation_config=cfg)
    cand = resp.candidates[0]
    fr = getattr(cand, "finish_reason", "?")
    try:
        txt = resp.text
    except Exception as e:
        txt = f"<kein .text: {e}>"
    return fr, txt


async def main():
    print("=" * 74)
    for mt in (512, 512, 512, 2048, 2048, 2048):
        fr, txt = await asyncio.to_thread(_run, mt)
        print(f"max_tokens={mt:5d} | finish_reason={fr} | text={txt!r}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
