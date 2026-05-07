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

KUNDE:
- kunde_name: Name des Kunden (z.B. "Frau Mueller", "Bauunternehmen Schmidt"). Pflicht.
- kunde_ort: Stadt/Ort falls genannt
- kunde_strasse: Strasse + Hausnummer falls genannt
- kunde_plz: Postleitzahl falls genannt
- kunde_email: E-Mail falls genannt

POSITIONEN (Liste - mindestens 1 Eintrag):
- positionen: Liste von Objekten. Jedes Objekt:
  * name: Kurzbezeichnung (z.B. "Moebelmontage", "Anfahrt", "Material"). Pflicht.
  * beschreibung: Optional, laengere Detailbeschreibung
  * menge: Anzahl als Zahl (default 1.0). "3 Stunden" -> 3.0, "5 Liter" -> 5.0
  * einheit: Default "Stueck". Andere: "Stunde", "Meter", "Liter", "kg", "qm", "Tag"
  * preis_brutto_eur: Einzelpreis brutto pro Einheit. "Netto"? Brutto errechnen (Netto * 1.19).
  * mwst_prozent: Default 19. Photovoltaik 0, Buecher 7.

WICHTIG zur Positionen-Erkennung:
- "Moebelmontage 350 Euro" -> 1 Position (name=Moebelmontage, menge=1, preis=350)
- "Moebelmontage 250 plus 50 Anfahrt plus 70 Material" -> 3 Positionen mit Einzelpreisen
- "3 Stunden Arbeit a 80 Euro" -> 1 Position (menge=3, einheit=Stunde, preis=80)
- "Tueren einbauen 150 pro Stueck, 4 Stueck" -> 1 Position (menge=4, preis=150)
- Wenn nur 1 Gesamtbetrag ohne Aufschluesselung -> 1 Position mit menge=1

GESAMT:
- gesamtbetrag_brutto_eur: Summe aller Positionen. Bei 1 Position = Position-Preis. Bei mehreren = Summe der menge*preis. Pflicht.

META:
- transcript: Bei Audio das wortgetreue Transkript. Bei Text der Original-Text.
- extraction_confidence: "high" wenn alles klar, "medium" bei Unsicherheiten, "low" wenn vieles unklar.
- missing_fields: Liste der fehlenden Pflichtfelder als Strings.

Pflichtfelder: kunde_name, positionen (mit mindestens 1 Eintrag), gesamtbetrag_brutto_eur.
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
        "positionen": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "beschreibung": {"type": "STRING", "nullable": True},
                    "menge": {"type": "NUMBER"},
                    "einheit": {"type": "STRING"},
                    "preis_brutto_eur": {"type": "NUMBER"},
                    "mwst_prozent": {"type": "INTEGER"},
                },
                "required": ["name", "menge", "einheit", "preis_brutto_eur", "mwst_prozent"],
            },
        },
        "gesamtbetrag_brutto_eur": {"type": "NUMBER", "nullable": True},
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
        "positionen",
        "gesamtbetrag_brutto_eur",
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
        "positionen": [],
        "gesamtbetrag_brutto_eur": None,
        "transcript": None,
        "extraction_confidence": "low",
        "missing_fields": [],
    }
    out = {**defaults, **(data or {})}

    # Positionen normalisieren
    positionen = out.get("positionen") or []
    if not isinstance(positionen, list):
        positionen = []
    cleaned = []
    for p in positionen:
        if not isinstance(p, dict):
            continue
        try:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            menge = float(p.get("menge") or 1)
            einheit = p.get("einheit") or "Stueck"
            preis = p.get("preis_brutto_eur")
            if isinstance(preis, str):
                preis = float(preis.replace(",", ".").strip())
            else:
                preis = float(preis or 0)
            mwst = int(p.get("mwst_prozent") or 19)
            cleaned.append({
                "name": name,
                "beschreibung": p.get("beschreibung"),
                "menge": menge,
                "einheit": einheit,
                "preis_brutto_eur": round(preis, 2),
                "mwst_prozent": mwst,
            })
        except Exception:
            continue
    out["positionen"] = cleaned

    # Gesamtbetrag: explizit oder errechnen
    gb = out.get("gesamtbetrag_brutto_eur")
    if isinstance(gb, str):
        try:
            gb = float(gb.replace(",", ".").strip())
        except Exception:
            gb = None
    if gb is None and cleaned:
        gb = sum(p["menge"] * p["preis_brutto_eur"] for p in cleaned)
    out["gesamtbetrag_brutto_eur"] = round(gb, 2) if gb is not None else None

    if not isinstance(out["missing_fields"], list):
        out["missing_fields"] = []
    return out


# ================================================================
# Kundengespraech-Analyse - lange Audio-Aufnahmen vom Tenant
# ================================================================
# Workflow: Tenant nimmt Gespraech beim Kunden auf, schickt Audio
# an Telegram-Bot. Gemini analysiert Audio und gibt strukturierte
# Daten zurueck: Briefing, Positionen, Termin, Todos.
# Verarbeitung in europe-west3 (Frankfurt) - DSGVO-konform.

GESPRAECH_PROMPT = """Du analysierst eine Audio-Aufnahme eines Kundengespraechs zwischen einem Handwerker und einem Kunden.

Dein Ziel: Strukturierte Daten extrahieren als JSON, damit der Handwerker
1. ein Angebot erstellen kann
2. sich vor seinem Termin nochmal briefen kann

EXTRAHIERE FOLGENDE FELDER:

KUNDE:
- kunde_name: Vollstaendiger Name (z.B. "Frau Mueller", "Familie Schmidt"). Pflicht.
- kunde_ort: Ort falls genannt
- kunde_strasse: Strasse + Hausnummer falls genannt
- kunde_plz: PLZ falls genannt
- kunde_telefon: Telefonnummer falls genannt
- kunde_email: E-Mail falls genannt

POSITIONEN (Liste, kann leer sein wenn unklar):
- positionen: Liste von Leistungen die besprochen wurden:
  * name: Kurzbezeichnung (z.B. "Moebelmontage", "Schrankbau Massiv", "Anfahrt")
  * beschreibung: Detail-Beschreibung was zu tun ist (1-2 Saetze)
  * menge: Anzahl als Zahl (default 1.0). "3 Stunden" -> 3.0, "5 Liter" -> 5.0
  * einheit: Default "Stueck". Andere: "Stunde", "Meter", "Liter", "kg", "qm", "Tag", "lfm", "Pauschal"
  * preis_brutto_eur: Brutto-Einzelpreis falls Tenant einen genannt hat. Wenn nicht genannt: null.
  * mwst_prozent: Default 19. Photovoltaik 0, Buecher 7.

WICHTIG: Erfinde KEINE Preise. Wenn der Tenant keinen Preis nannte, setze preis_brutto_eur=null.
Erfinde KEINE Mengen. Wenn unklar, setze menge=1 und nimm einheit="Pauschal".

TERMIN:
- termin_datum: Falls ein konkretes Datum/Zeit vereinbart wurde (ISO-Format YYYY-MM-DD HH:MM). Sonst null.
- termin_ort: Falls Termin-Adresse genannt wurde und sie von Kunden-Adresse abweicht. Sonst null.

BRIEFING UND NOTIZEN (das wichtigste fuer den Handwerker):
- briefing_kurz: 3-5 Saetze die der Handwerker vor dem Termin lesen will. Konkret und kurz.
  Beispiel: "Frau Mueller, Wohnung im 2. Stock OHNE Aufzug. Soll Massivholz-Schrank im Schlafzimmer montieren. Schwerpunkt: Schiebetueren-Mechanik klemmt. Termin Mittwoch 9 Uhr."
- notizen_lang: Vollstaendige Notizen mit allen Details aus dem Gespraech.
  Inkl. Kundenwuensche, Sorgen, Zeitplan, Material-Anforderungen, Besonderheiten.
- todos: Liste von TODO-Punkten die der Handwerker erledigen muss BEVOR oder BEIM Termin.
  Beispiel: ["Schiebetueren-Mechanik bestellen", "Wasserwaage und Akkuschrauber mitnehmen", "Kollegen mitbringen wegen 2. Stock"]

META:
- transcript: Wortgetreues Transkript der gesamten Aufnahme.
- extraction_confidence: "high" wenn vieles klar war, "medium" bei Unsicherheiten, "low" wenn das Audio undeutlich war.
- missing_fields: Liste fehlender Pflichtfelder als Strings.

Pflicht: kunde_name. Alles andere ist optional.

Antworte AUSSCHLIESSLICH mit dem JSON, kein Markdown, keine Erlaeuterung."""


GESPRAECH_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kunde_name": {"type": "STRING", "nullable": True},
        "kunde_ort": {"type": "STRING", "nullable": True},
        "kunde_strasse": {"type": "STRING", "nullable": True},
        "kunde_plz": {"type": "STRING", "nullable": True},
        "kunde_telefon": {"type": "STRING", "nullable": True},
        "kunde_email": {"type": "STRING", "nullable": True},
        "positionen": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "beschreibung": {"type": "STRING", "nullable": True},
                    "menge": {"type": "NUMBER", "nullable": True},
                    "einheit": {"type": "STRING", "nullable": True},
                    "preis_brutto_eur": {"type": "NUMBER", "nullable": True},
                    "mwst_prozent": {"type": "INTEGER", "nullable": True},
                },
                "required": ["name"],
            },
        },
        "termin_datum": {"type": "STRING", "nullable": True},
        "termin_ort": {"type": "STRING", "nullable": True},
        "briefing_kurz": {"type": "STRING", "nullable": True},
        "notizen_lang": {"type": "STRING", "nullable": True},
        "todos": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "nullable": True,
        },
        "transcript": {"type": "STRING", "nullable": True},
        "extraction_confidence": {"type": "STRING", "nullable": True},
        "missing_fields": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "nullable": True,
        },
    },
}


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
        "extract_rechnung OK (%s): kunde=%r positionen=%d gesamt=%s conf=%s missing=%s",
        mode,
        normalized.get("kunde_name"),
        len(normalized.get("positionen") or []),
        normalized.get("gesamtbetrag_brutto_eur"),
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



def _normalize_gespraech_extraction(data: dict) -> dict:
    """Normalisiert das Gemini-Output fuer Kundengespraeche.

    Sorgt dafuer dass alle erwarteten Felder existieren (auch wenn null/leer).
    Castet Mengen + Preise zu float, Confidence-Default = 'low'.
    """
    if not isinstance(data, dict):
        data = {}

    out = {
        "kunde_name": data.get("kunde_name"),
        "kunde_ort": data.get("kunde_ort"),
        "kunde_strasse": data.get("kunde_strasse"),
        "kunde_plz": data.get("kunde_plz"),
        "kunde_telefon": data.get("kunde_telefon"),
        "kunde_email": data.get("kunde_email"),
        "positionen": [],
        "termin_datum": data.get("termin_datum"),
        "termin_ort": data.get("termin_ort"),
        "briefing_kurz": data.get("briefing_kurz"),
        "notizen_lang": data.get("notizen_lang"),
        "todos": data.get("todos") or [],
        "transcript": data.get("transcript"),
        "extraction_confidence": data.get("extraction_confidence") or "low",
        "missing_fields": data.get("missing_fields") or [],
    }

    # Positionen normalisieren
    raw_positionen = data.get("positionen") or []
    if isinstance(raw_positionen, list):
        for raw in raw_positionen:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            try:
                menge = float(raw.get("menge") or 1.0)
            except (ValueError, TypeError):
                menge = 1.0
            preis = raw.get("preis_brutto_eur")
            if preis is not None:
                try:
                    preis = float(preis)
                except (ValueError, TypeError):
                    preis = None
            out["positionen"].append({
                "name": str(raw.get("name", "")).strip(),
                "beschreibung": raw.get("beschreibung"),
                "menge": menge,
                "einheit": (raw.get("einheit") or "Stueck").strip(),
                "preis_brutto_eur": preis,
                "mwst_prozent": int(raw.get("mwst_prozent") or 19),
            })

    # Pflicht-Check
    missing = list(out.get("missing_fields") or [])
    if not out.get("kunde_name") and "kunde_name" not in missing:
        missing.append("kunde_name")
    out["missing_fields"] = missing

    return out


async def _gemini_analyse_gespraech(parts: list, mode: str) -> dict:
    """Interner Helper: Gemini-Call fuer Kundengespraech-Analyse.

    Verarbeitung in europe-west3 (Frankfurt) - DSGVO-konform.
    Audio bis ~9.5h pro Request bei Vertex AI.
    """
    import json as _json
    from google.genai.types import GenerateContentConfig

    client = _get_genai_client(location=GENAI_TEXT_LOCATION)
    model = "gemini-2.5-flash"

    config = GenerateContentConfig(
        temperature=0.2,  # Etwas kreativer als Rechnung (Briefing-Texte!)
        max_output_tokens=8192,  # Langes Transkript + Briefing + Notizen
        response_mime_type="application/json",
        response_schema=GESPRAECH_RESPONSE_SCHEMA,
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
        logger.warning(f"analyse_gespraech ({mode}): Keine Candidates")
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        logger.warning(
            f"analyse_gespraech ({mode}): Empty parts. "
            f"finish_reason={getattr(candidate, 'finish_reason', '?')}"
        )
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    raw_text = ""
    for p in candidate.content.parts:
        if getattr(p, "text", None):
            raw_text += p.text

    if not raw_text:
        logger.warning(f"analyse_gespraech ({mode}): Kein Text in Response")
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    try:
        data = _json.loads(raw_text)
    except _json.JSONDecodeError as e:
        logger.warning(
            f"analyse_gespraech ({mode}): JSON parse fail: {e} | raw={raw_text[:300]!r}"
        )
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    normalized = _normalize_gespraech_extraction(data)
    logger.info(
        "analyse_gespraech OK (%s): kunde=%r positionen=%d todos=%d termin=%s conf=%s",
        mode,
        normalized.get("kunde_name"),
        len(normalized.get("positionen") or []),
        len(normalized.get("todos") or []),
        normalized.get("termin_datum"),
        normalized.get("extraction_confidence"),
    )
    return normalized


async def analyse_kundengespraech_from_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> dict:
    """Analysiert eine Audio-Aufnahme eines Kundengespraechs.

    Workflow: Tenant nimmt Gespraech via Telegram auf, schickt Audio.
    Gemini transkribiert + extrahiert in einem Call:
      - Kundendaten
      - Positionen (was zu tun ist, was es kostet)
      - Termin (falls vereinbart)
      - Briefing-Texte fuer Pre-Termin-Lesung
      - TODOs fuer Vorbereitung

    mime_type: audio/ogg fuer Telegram-Voice (Opus codec).
               audio/mpeg, audio/wav, audio/flac auch unterstuetzt.

    Returns: dict mit allen Feldern aus GESPRAECH_RESPONSE_SCHEMA.
    """
    if not audio_bytes:
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    from google.genai.types import Part

    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_analyse_gespraech(
        [audio_part, GESPRAECH_PROMPT],
        mode=f"audio/{mime_type}",
    )



# =====================================================================
# Mail-Subject-Klassifikation
# Schnelle Pre-Filterung: relevant fuer Bot oder nicht?
# =====================================================================

CLASSIFY_PROMPT = """Du klassifizierst eingehende Mails fuer einen Handwerker.

Mail-Daten:
- Betreff: {subject}
- Absender: {sender}
- Tenant: {tenant_company}, Branche: {tenant_branche}

Antworte als JSON mit:
- classification: EINER von: RELEVANT_KUNDE, RELEVANT_GESCHAEFT, NICHT_RELEVANT, PRIVAT, UNSICHER
- confidence: low / medium / high
- reason: 1 Satz Begruendung

Kategorien:
- RELEVANT_KUNDE: Kunden-Anfrage (Termin, Anfrage, Angebot, Reklamation)
- RELEVANT_GESCHAEFT: Geschaefts-Mail (Lieferant, Material-Bestellung, Rechnung von Dienstleister)
- NICHT_RELEVANT: Newsletter, Spam, Werbung, Auto-Notifications
- PRIVAT: Privat-Mail (Familie, Steuerberater, Bank, Versicherung)
- UNSICHER: nicht eindeutig zuzuordnen

Wenn Absender bekannt-privat (Banken, Versicherungen, Steuerberater): PRIVAT
Wenn typische Werbung-Subjects oder noreply-Adressen: NICHT_RELEVANT
Wenn Subject eindeutig Anfrage: RELEVANT_KUNDE"""


CLASSIFY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["RELEVANT_KUNDE", "RELEVANT_GESCHAEFT", "NICHT_RELEVANT", "PRIVAT", "UNSICHER"],
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["classification", "confidence", "reason"],
}


async def classify_mail_subject(
    subject: str,
    sender: str,
    tenant_company: str = "Handwerksbetrieb",
    tenant_branche: str = "Handwerk",
) -> dict:
    """Klassifiziert eine Mail anhand Subject + Sender. Sehr schnell, sehr billig.

    Returns: {classification, confidence, reason}
    Bei Fehler: classification=UNSICHER, confidence=low
    """
    import json as _json
    import re as _re

    prompt = CLASSIFY_PROMPT.format(
        subject=(subject or "(kein Betreff)")[:200],
        sender=(sender or "unbekannt")[:200],
        tenant_company=tenant_company[:100],
        tenant_branche=tenant_branche[:50],
    )
    prompt += "\n\nAntworte AUSSCHLIESSLICH mit gueltigem JSON (kein Markdown, keine Erklaerung), z.B.:\n"
    prompt += '{"classification": "RELEVANT_KUNDE", "confidence": "high", "reason": "Klare Anfrage"}'

    try:
        text = await call_gemini(
            prompt=prompt,
            temperature=0.0,
            max_output_tokens=2048,
        )

        # JSON aus Antwort extrahieren (Gemini packt manchmal Markdown drumherum)
        text_stripped = text.strip()
        if text_stripped.startswith("```"):
            # Markdown-Codeblock entfernen
            text_stripped = _re.sub(r"^```(?:json)?\s*", "", text_stripped)
            text_stripped = _re.sub(r"\s*```$", "", text_stripped)

        # Versuch direkt zu parsen, sonst JSON-Objekt rauspicken
        try:
            result = _json.loads(text_stripped)
        except _json.JSONDecodeError:
            match = _re.search(r"\{[^{}]*\}", text_stripped, _re.DOTALL)
            if not match:
                raise ValueError(f"Kein JSON in Antwort: {text[:200]!r}")
            result = _json.loads(match.group(0))
        # Validierung
        valid_classes = {"RELEVANT_KUNDE", "RELEVANT_GESCHAEFT", "NICHT_RELEVANT", "PRIVAT", "UNSICHER"}
        cls = result.get("classification") or "UNSICHER"
        if cls not in valid_classes:
            cls = "UNSICHER"
        conf = result.get("confidence") or "low"
        if conf not in ("low", "medium", "high"):
            conf = "low"
        reason = (result.get("reason") or "")[:500]

        logger.info(
            "classify_mail_subject: subject=%r sender=%r -> %s (%s) %s",
            subject[:60] if subject else "",
            sender[:40] if sender else "",
            cls, conf, reason[:80],
        )
        return {"classification": cls, "confidence": conf, "reason": reason}

    except Exception as e:
        logger.warning(f"classify_mail_subject fehler: {e}")
        return {
            "classification": "UNSICHER",
            "confidence": "low",
            "reason": f"Klassifikation fehlgeschlagen: {e}",
        }

