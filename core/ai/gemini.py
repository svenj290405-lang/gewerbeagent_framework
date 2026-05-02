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
    max_output_tokens: int = 4096,
) -> str:
    """
    Synchrones Vertex-SDK in Threadpool, damit es in Async-Code passt.
    Default-Temperatur niedrig fuer strukturierte Extraction.

    HINWEIS: Gemini 2.5 Flash macht "Thinking" vor Output. Bei zu niedrigem
    max_output_tokens werden alle Tokens fuers Thinking verbraucht und die
    eigentliche Antwort bleibt leer. Daher Default 4096.
    """
    def _run() -> str:
        model = _get_model()
        config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        response = model.generate_content(prompt, generation_config=config)
        # response.text wirft wenn Content leer (z.B. MAX_TOKENS bei Thinking).
        try:
            return response.text or ""
        except Exception as e:
            logger.warning(f"Gemini-Antwort hat keinen Text: {e}")
            # Fallback: erste Candidate-Parts manuell joinen
            try:
                parts = response.candidates[0].content.parts
                return "".join(p.text for p in parts if hasattr(p, "text"))
            except Exception:
                return ""

    return await asyncio.to_thread(_run)


# =====================================================================
# Image Generation / Editing via google.genai (Gemini 2.5 Flash Image)
# =====================================================================

@lru_cache(maxsize=4)
def _get_genai_client(location: str = "europe-west4"):
    """
    Lazy init google-genai client gebunden an eine Location.

    DSGVO-Hinweis: location bestimmt wo die Daten verarbeitet werden.
    - 'europe-west3' (Frankfurt) - fuer Text/Audio (gemini-2.5-flash)
    - 'europe-west4' (Niederlande) - fuer Image (gemini-2.5-flash-image)
    - 'global' - fragwuerdig DSGVO, Daten koennten USA landen
    """
    import os as _os
    _os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    _os.environ["GOOGLE_CLOUD_LOCATION"] = location
    project = _get_project_id()
    _os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    from google import genai as _genai
    logger.info(f"google-genai Client initialisiert: location={location} project={project}")
    return _genai.Client(vertexai=True, project=project, location=location)


# Default-Locations
GENAI_TEXT_LOCATION = "europe-west3"
GENAI_IMAGE_LOCATION = "europe-west4"


async def generate_image_from_image(
    image_bytes: bytes,
    prompt: str,
    mime_type: str = "image/jpeg",
    model: str = "gemini-2.5-flash-image",
) -> bytes | None:
    """
    Bearbeitet ein Eingabe-Bild gemaess Prompt mit Gemini 2.5 Flash Image.

    Args:
        image_bytes: Original-Bild als Bytes (z.B. von Telegram-Download)
        prompt: Was im Bild geaendert/eingefuegt werden soll
        mime_type: image/jpeg oder image/png
        model: Modell-Name (Default: gemini-2.5-flash-image)

    Returns:
        Generated image as PNG-Bytes, oder None bei Fehler.
    """
    try:
        from google.genai.types import GenerateContentConfig, Modality, Part
        from PIL import Image
        from io import BytesIO

        client = _get_genai_client(location=GENAI_IMAGE_LOCATION)

        image_part = Part.from_bytes(data=image_bytes, mime_type=mime_type)

        # Async-Wrapper um den synchronen genai-Call
        loop = asyncio.get_event_loop()

        def _sync_call():
            return client.models.generate_content(
                model=model,
                contents=[image_part, prompt],
                config=GenerateContentConfig(
                    response_modalities=[Modality.TEXT, Modality.IMAGE],
                ),
            )

        response = await loop.run_in_executor(None, _sync_call)

        # Bild aus Response extrahieren
        if not response.candidates:
            logger.warning("generate_image_from_image: Keine Candidates in Response")
            return None

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            logger.warning(
                f"generate_image_from_image: Empty content/parts. "
                f"finish_reason={getattr(candidate, 'finish_reason', '?')}"
            )
            return None

        for part in candidate.content.parts:
            if getattr(part, "inline_data", None):
                # PIL um nach PNG zu konvertieren (Telegram bevorzugt PNG)
                img = Image.open(BytesIO(part.inline_data.data))
                buf = BytesIO()
                img.save(buf, format="PNG")
                logger.info(
                    f"generate_image_from_image: OK - Output {img.size}, "
                    f"{len(buf.getvalue())} bytes"
                )
                return buf.getvalue()

        logger.warning("generate_image_from_image: Kein Bild in Parts gefunden")
        return None

    except Exception as e:
        logger.exception(f"generate_image_from_image: {e}")
        return None



# =====================================================================
# Rechnung-Extraktion (Text + Audio) via google-genai (europe-west3)
# =====================================================================

RECHNUNG_PROMPT = """Du bekommst entweder einen Text oder eine Sprachnachricht eines Handwerkers, der eine Rechnung erstellen will.

Extrahiere strukturierte Felder als JSON. Wenn ein Feld nicht erwaehnt wird, setze es auf null. Erfinde KEINE Werte.

Felder:
- kunde_name: Name des Kunden (z.B. "Frau Mueller", "Bauunternehmen Schmidt"). Pflicht. Falls unklar -> null + missing_fields-Eintrag.
- kunde_ort: Stadt/Ort des Kunden falls genannt
- kunde_strasse: Strasse mit Hausnummer falls genannt
- kunde_plz: Postleitzahl falls genannt
- kunde_email: E-Mail falls genannt
- leistung_titel: Kurze Bezeichnung (z.B. "Moebelmontage", "Kuechenmontage"). Pflicht.
- leistung_beschreibung: Detaillierte Beschreibung falls vorhanden
- betrag_brutto_eur: Betrag in Euro als Zahl (float). WICHTIG: "350 Euro" -> 350.00 als Zahl. "Netto" gesagt? Trotzdem Brutto errechnen (Netto * 1.19 fuer 19% MwSt). Pflicht.
- transcript: Bei Audio das wortgetreue Transkript. Bei Text der Original-Text.
- extraction_confidence: "high" wenn alle Pflichtfelder klar, "medium" bei Unsicherheiten, "low" wenn vieles unklar.
- missing_fields: Liste der fehlenden Pflichtfelder als Strings.

Pflichtfelder: kunde_name, leistung_titel, betrag_brutto_eur.
Antworte AUSSCHLIESSLICH mit dem JSON, kein Markdown, keine Erlaeuterung.
"""

RECHNUNG_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kunde_name": {"type": "STRING", "nullable": True},
        "kunde_ort": {"type": "STRING", "nullable": True},
        "kunde_strasse": {"type": "STRING", "nullable": True},
        "kunde_plz": {"type": "STRING", "nullable": True},
        "kunde_email": {"type": "STRING", "nullable": True},
        "leistung_titel": {"type": "STRING", "nullable": True},
        "leistung_beschreibung": {"type": "STRING", "nullable": True},
        "betrag_brutto_eur": {"type": "NUMBER", "nullable": True},
        "transcript": {"type": "STRING", "nullable": True},
        "extraction_confidence": {
            "type": "STRING",
            "enum": ["high", "medium", "low"],
        },
        "missing_fields": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
    },
    "required": [
        "kunde_name",
        "leistung_titel",
        "betrag_brutto_eur",
        "extraction_confidence",
        "missing_fields",
    ],
}


def _normalize_rechnung_extraction(data: dict) -> dict:
    """Defensive: stellt sicher dass alle Felder existieren + Beträge sauber sind."""
    defaults = {
        "kunde_name": None,
        "kunde_ort": None,
        "kunde_strasse": None,
        "kunde_plz": None,
        "kunde_email": None,
        "leistung_titel": None,
        "leistung_beschreibung": None,
        "betrag_brutto_eur": None,
        "transcript": None,
        "extraction_confidence": "low",
        "missing_fields": [],
    }
    out = {**defaults, **(data or {})}
    b = out.get("betrag_brutto_eur")
    if isinstance(b, str):
        try:
            out["betrag_brutto_eur"] = float(b.replace(",", ".").strip())
        except Exception:
            out["betrag_brutto_eur"] = None
    if not isinstance(out["missing_fields"], list):
        out["missing_fields"] = []
    return out


async def _gemini_extract_rechnung(parts: list, mode: str) -> dict:
    """Interner Helper: Gemini-Call mit response_schema, location europe-west3."""
    import json as _json
    from google.genai.types import GenerateContentConfig

    client = _get_genai_client(location=GENAI_TEXT_LOCATION)
    model = "gemini-2.5-flash"

    config = GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=RECHNUNG_RESPONSE_SCHEMA,
    )

    def _sync_call():
        return client.models.generate_content(
            model=model,
            contents=parts,
            config=config,
        )

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, _sync_call)

    if not response.candidates:
        logger.warning(f"extract_rechnung ({mode}): Keine Candidates")
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        logger.warning(
            f"extract_rechnung ({mode}): Empty parts. "
            f"finish_reason={getattr(candidate, 'finish_reason', '?')}"
        )
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    raw_text = ""
    for p in candidate.content.parts:
        if getattr(p, "text", None):
            raw_text += p.text

    if not raw_text:
        logger.warning(f"extract_rechnung ({mode}): Kein Text in Response")
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    try:
        data = _json.loads(raw_text)
    except _json.JSONDecodeError as e:
        logger.warning(f"extract_rechnung ({mode}): JSON parse fail: {e} | raw={raw_text[:300]!r}")
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    normalized = _normalize_rechnung_extraction(data)
    logger.info(
        "extract_rechnung OK (%s): kunde=%r leistung=%r betrag=%s conf=%s missing=%s",
        mode,
        normalized.get("kunde_name"),
        normalized.get("leistung_titel"),
        normalized.get("betrag_brutto_eur"),
        normalized.get("extraction_confidence"),
        normalized.get("missing_fields"),
    )
    return normalized


async def extract_rechnung_from_text(text: str) -> dict:
    """
    Extrahiert Rechnungs-Felder aus Text-Eingabe.
    Verarbeitung in europe-west3 (Frankfurt) - DSGVO-konform.
    """
    if not text or not text.strip():
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    full_prompt = (
        RECHNUNG_PROMPT
        + "\n\n--- Text-Eingabe des Handwerkers: ---\n"
        + text.strip()
    )
    return await _gemini_extract_rechnung([full_prompt], mode="text")


async def extract_rechnung_from_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> dict:
    """
    Extrahiert Rechnungs-Felder aus Sprachnachricht.
    Gemini transkribiert + extrahiert in einem Call.
    Verarbeitung in europe-west3 (Frankfurt) - DSGVO-konform.

    mime_type: audio/ogg fuer Telegram-Voice-Notes (Opus codec).
               audio/mpeg, audio/wav, audio/flac auch unterstuetzt.
    """
    if not audio_bytes:
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    from google.genai.types import Part

    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_extract_rechnung(
        [audio_part, RECHNUNG_PROMPT],
        mode=f"audio/{mime_type}",
    )
