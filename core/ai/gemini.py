"""
Gemini-Wrapper via Vertex AI.

Nutzung:
    from core.ai import call_gemini
    response_text = await call_gemini("Dein Prompt")

Konfig kommt aus Environment:
    GEMINI_MODEL          (z.B. gemini-2.5-flash)
    GEMINI_LOCATION       (z.B. europe-west3)
    GOOGLE_APPLICATION_CREDENTIALS  (Pfad zum Service-Account-JSON)

Project-ID wird aus dem Service-Account-JSON gelesen, falls
GEMINI_PROJECT nicht gesetzt ist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import lru_cache

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_project_id() -> str:
    """Liest project_id aus Service-Account-JSON oder ENV."""
    env_pid = os.getenv("GEMINI_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if env_pid:
        return env_pid

    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path or not os.path.exists(cred_path):
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS nicht gesetzt oder Datei fehlt"
        )

    with open(cred_path) as f:
        data = json.load(f)
    pid = data.get("project_id")
    if not pid:
        raise RuntimeError(f"project_id fehlt im JSON: {cred_path}")
    return pid


@lru_cache(maxsize=1)
def _get_model() -> GenerativeModel:
    """Initialisiert Vertex einmalig, gibt das Model zurueck."""
    project = _get_project_id()
    location = os.getenv("GEMINI_LOCATION", "europe-west3")
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    vertexai.init(project=project, location=location)
    logger.info(f"Vertex initialisiert: project={project} location={location} model={model_name}")
    return GenerativeModel(model_name)


async def call_gemini(
    prompt: str,
    *,
    temperature: float = 0.2,
    max_output_tokens: int = 1024,
) -> str:
    """
    Synchrones Vertex-SDK in Threadpool, damit es in Async-Code passt.
    Default-Temperatur niedrig fuer strukturierte Extraction.
    """
    def _run() -> str:
        model = _get_model()
        config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        response = model.generate_content(prompt, generation_config=config)
        return response.text or ""

    return await asyncio.to_thread(_run)
