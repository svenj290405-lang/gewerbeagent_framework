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

from config.settings import settings

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
    tenant_id: str | None = None,
    operation_kind: str | None = None,
) -> str:
    """
    Synchrones Vertex-SDK in Threadpool, damit es in Async-Code passt.
    Default-Temperatur niedrig fuer strukturierte Extraction.

    HINWEIS: Gemini 2.5 Flash macht "Thinking" vor Output. Bei zu niedrigem
    max_output_tokens werden alle Tokens fuers Thinking verbraucht und die
    eigentliche Antwort bleibt leer. Daher Default 4096.

    tenant_id und operation_kind sind optional - falls gesetzt, wird der
    Token-Verbrauch in api_usage_log gespeichert (failsafe).
    """
    def _run():
        model = _get_model()
        config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        return model.generate_content(prompt, generation_config=config)

    response = await asyncio.to_thread(_run)

    # Usage-Tracking, failsafe (loggt nur Warnungen)
    try:
        from core.billing import track_gemini_response
        await track_gemini_response(
            response,
            model=settings.gemini_model,
            tenant_id=tenant_id,
            operation_kind=operation_kind,
        )
    except Exception as e:
        logger.debug(f"Gemini-Tracking failed (egal): {e}")

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
                    # Optional: Wenn eine Kalkulationsregel auf diese
                    # Position passt, fuellt Gemini die Variable-Werte aus.
                    # Der eigentliche Preis wird dann im Handler in Python
                    # neu berechnet (Hybrid-Modus, siehe core.ai.kalkulation).
                    "kalkulation": {
                        "type": "OBJECT",
                        "nullable": True,
                        "properties": {
                            "regel_name": {"type": "STRING"},
                            "variablen": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "name": {"type": "STRING"},
                                        "wert": {"type": "NUMBER"},
                                    },
                                    "required": ["name", "wert"],
                                },
                            },
                        },
                    },
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
            # Kalkulations-Hinweis (optional): Gemini hat eine Regel
            # erkannt und die Variablenwerte mitgeliefert. Der Aufnahme-
            # Handler matcht das spaeter gegen tenant_kalkulationen
            # und rechnet den Preis deterministisch neu aus.
            kalk = p.get("kalkulation")
            kalk_clean = None
            if isinstance(kalk, dict):
                regel_name = (kalk.get("regel_name") or "").strip()
                vars_raw = kalk.get("variablen") or []
                vars_clean: dict[str, float] = {}
                if isinstance(vars_raw, list):
                    for v in vars_raw:
                        if not isinstance(v, dict):
                            continue
                        vn = (v.get("name") or "").strip()
                        try:
                            vw = float(v.get("wert"))
                        except (TypeError, ValueError):
                            continue
                        if vn:
                            vars_clean[vn] = vw
                if regel_name and vars_clean:
                    kalk_clean = {
                        "regel_name": regel_name,
                        "variablen": vars_clean,
                    }
            cleaned.append({
                "name": name,
                "beschreibung": p.get("beschreibung"),
                "menge": menge,
                "einheit": einheit,
                "preis_brutto_eur": round(preis, 2),
                "mwst_prozent": mwst,
                "kalkulation": kalk_clean,
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
  * kalkulation: Optional. Wenn auf diese Position eine der oben gelisteten
    KALKULATIONSREGELN passt (z.B. Anfahrtspauschale, Notfall-Zuschlag),
    setze hier {"regel_name": "<exakter Name der Regel>", "variablen":
    [{"name":"entfernung_km","wert":42}, ...]}. Liefere die Variablen-
    Werte gemaess der Formel-Definition - der Endpreis wird vom System
    automatisch aus der Formel berechnet, du musst preis_brutto_eur dann
    nicht selbst setzen (es wird ueberschrieben). Wenn keine Regel passt:
    weglassen / null.

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
                    # Optional: Wenn eine Kalkulationsregel des Tenants
                    # passt, fuellt Gemini Variable-Werte hier aus. Der
                    # finale Preis wird dann deterministisch in Python
                    # neu berechnet (siehe core.ai.kalkulation).
                    "kalkulation": {
                        "type": "OBJECT",
                        "nullable": True,
                        "properties": {
                            "regel_name": {"type": "STRING"},
                            "variablen": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "name": {"type": "STRING"},
                                        "wert": {"type": "NUMBER"},
                                    },
                                    "required": ["name", "wert"],
                                },
                            },
                        },
                    },
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
            # Kalkulations-Hinweis (optional, wie in _normalize_rechnung)
            kalk = raw.get("kalkulation")
            kalk_clean = None
            if isinstance(kalk, dict):
                regel_name = (kalk.get("regel_name") or "").strip()
                vars_raw = kalk.get("variablen") or []
                vars_clean: dict[str, float] = {}
                if isinstance(vars_raw, list):
                    for v in vars_raw:
                        if not isinstance(v, dict):
                            continue
                        vn = (v.get("name") or "").strip()
                        try:
                            vw = float(v.get("wert"))
                        except (TypeError, ValueError):
                            continue
                        if vn:
                            vars_clean[vn] = vw
                if regel_name and vars_clean:
                    kalk_clean = {
                        "regel_name": regel_name,
                        "variablen": vars_clean,
                    }

            out["positionen"].append({
                "name": str(raw.get("name", "")).strip(),
                "beschreibung": raw.get("beschreibung"),
                "menge": menge,
                "einheit": (raw.get("einheit") or "Stueck").strip(),
                "preis_brutto_eur": preis,
                "mwst_prozent": int(raw.get("mwst_prozent") or 19),
                "kalkulation": kalk_clean,
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
    *,
    tenant_id=None,
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

    tenant_id (optional): Wenn gesetzt, werden Kalkulationsregeln des
    Tenants in den Prompt eingespeist (Hybrid-Modus). Pro Position kann
    Gemini dann eine Regel + Variablen-Werte angeben; der finale Preis
    wird in Python neu berechnet (siehe core.ai.kalkulation).

    Returns: dict mit allen Feldern aus GESPRAECH_RESPONSE_SCHEMA.
    """
    if not audio_bytes:
        return _normalize_gespraech_extraction({"missing_fields": ["alle"]})

    from google.genai.types import Part

    kalk_block = await _build_kalkulation_block(tenant_id)
    prompt_text = (
        (kalk_block + "\n\n" if kalk_block else "")
        + GESPRAECH_PROMPT
    )
    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_analyse_gespraech(
        [audio_part, prompt_text],
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
Wenn Subject eindeutig Anfrage: RELEVANT_KUNDE

WICHTIG bei Mails in anderer Sprache (Englisch/Tuerkisch/Polnisch/...):
Klassifiziere trotzdem nach dem Anliegen — eine deutsche Anfrage bleibt
RELEVANT_KUNDE auch wenn auf Englisch geschrieben. Nutze fuer reason
einen kurzen deutschen Satz."""


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


# =====================================================================
# Mail-Reply-Generator fuer RELEVANT_KUNDE Mails
# Generiert persoenliche Antwort auf Kunden-Anfrage mit Wissensbasis-
# Kontext und Verweis auf Anfrage-Formular.
# =====================================================================

REPLY_PROMPT = """Du bist Q, ein freundlicher Assistent fuer den Handwerker {tenant_company} (Branche: {tenant_branche}).

Du beantwortest eingehende Kunden-Mails im Namen von {tenant_owner_first_name}.

Kontext-Wissen ueber den Betrieb:
{wissensbasis}

Eingehende Kunden-Mail:
- Betreff: {subject}
- Von: {sender_name} ({sender_email})

Mail-Inhalt:
---
{body}
---

Aufgabe:
Schreib eine kurze, freundliche, persoenliche Antwort. Beachte:
1. Geh konkret auf das Anliegen ein (nicht generisch)
2. Wenn die Wissensbasis Antworten enthaelt (Preise, Lieferzeiten), nutze sie
3. Bitte am Ende um Ausfuellen des Anfrage-Formulars mit dem Link {form_url}
4. Schreib auf Deutsch, hoeflich aber nicht foermlich (Du-Form falls Kunde Du verwendet, sonst Sie)
5. Unterzeichne mit "{tenant_owner_first_name} (via Q)"
6. KEIN Marketing-Geschwafel, KEINE Floskeln wie "Vielen Dank fuer ihre Anfrage"
7. Sei direkt und ehrlich

Antworte NUR mit dem Mail-Text (keine Begruessung wie "Hier die Antwort:", kein Markdown)."""


async def generate_anfrage_reply(
    subject: str,
    sender_name: str,
    sender_email: str,
    body: str,
    form_url: str,
    tenant_company: str = "Handwerksbetrieb",
    tenant_branche: str = "Handwerk",
    tenant_owner_first_name: str = "Daniel",
    wissensbasis: str = "(keine spezifischen Infos hinterlegt)",
) -> str:
    """Generiert eine persoenliche Antwort auf eine Kunden-Mail.

    Returns: Plain-Text Antwort (kein HTML), bereit fuer send_mail_as_user.
    """
    prompt = REPLY_PROMPT.format(
        tenant_company=tenant_company[:100],
        tenant_branche=tenant_branche[:50],
        tenant_owner_first_name=tenant_owner_first_name[:50],
        wissensbasis=wissensbasis[:3000],
        subject=(subject or "(kein Betreff)")[:200],
        sender_name=(sender_name or "Kunde")[:100],
        sender_email=(sender_email or "")[:100],
        body=(body or "(kein Inhalt)")[:4000],
        form_url=form_url,
    )

    try:
        text = await call_gemini(
            prompt=prompt,
            temperature=0.4,
            max_output_tokens=8192,
        )
        text = (text or "").strip()
        if not text:
            raise ValueError("Gemini hat leeren Text zurueckgegeben")

        logger.info(
            f"generate_anfrage_reply: subject={subject[:60]!r} sender={sender_email!r} "
            f"-> reply_len={len(text)}"
        )
        return text
    except Exception as e:
        logger.exception(f"generate_anfrage_reply fehler: {e}")
        # Fallback-Antwort
        return (
            f"Hallo {sender_name or 'zusammen'},\n\n"
            f"vielen Dank fuer deine Nachricht. Damit ich dir gut weiterhelfen kann, "
            f"fuell bitte kurz das folgende Formular aus:\n\n"
            f"{form_url}\n\n"
            f"Dann melde ich mich schnell mit einem konkreten Angebot.\n\n"
            f"Viele Gruesse\n"
            f"{tenant_owner_first_name} (via Q)"
        )


# =====================================================================
# Angebot-Workflow: Extraktion + Update via Sprache + Annahme-Erkennung
# Wiederverwendet RECHNUNG_RESPONSE_SCHEMA (Angebot hat strukturell die
# gleichen Felder: kunde_*, positionen[], gesamtbetrag).
# =====================================================================

async def _build_kalkulation_block(tenant_id) -> str:
    """
    Laedt aktive Kalkulationsregeln des Tenants und formatiert sie als
    Prompt-Block fuer Gemini. Wird vor ANGEBOT_PROMPT gehaengt.

    Idee (Hybrid-Modus): Gemini bekommt Name + Formel + Variablen jeder
    Regel. Die LLM entscheidet pro Position welche Regel passt und liefert
    nur die Variablen-Werte mit. Der eigentliche Preis wird anschliessend
    deterministisch in Python aus Formel + Variablen-Werten berechnet
    (siehe core.ai.kalkulation.safe_eval_formel).
    """
    # Lazy-Import - keine zyklische Abhaengigkeit beim Modul-Load
    from core.database import AsyncSessionLocal
    from core.models import TenantKalkulation, KALK_KATEGORIE_LABELS
    from sqlalchemy import select

    if not tenant_id:
        return ""

    async with AsyncSessionLocal() as s:
        regeln = (await s.execute(
            select(TenantKalkulation)
            .where(TenantKalkulation.tenant_id == tenant_id)
            .where(TenantKalkulation.aktiv.is_(True))
            .order_by(
                TenantKalkulation.kategorie,
                TenantKalkulation.sortierung,
                TenantKalkulation.created_at,
            )
        )).scalars().all()

    if not regeln:
        return ""

    # Gruppiert nach Kategorie ausgeben - macht es fuer Gemini leichter
    # zu erkennen, "fuer Anfahrt nimm Anfahrt-Regel".
    by_kat: dict[str, list[TenantKalkulation]] = {}
    for r in regeln:
        by_kat.setdefault(r.kategorie, []).append(r)

    lines = [
        "## KALKULATIONSREGELN (verbindlich anwenden)",
        "",
        "Der Handwerker hat eigene Berechnungsregeln hinterlegt. Wenn eine "
        "dieser Regeln auf eine Angebotsposition passt, fuelle das Feld "
        "`kalkulation` der Position mit dem `regel_name` und den passenden "
        "Variablen-Werten aus. Der finale Preis wird vom System "
        "deterministisch aus der Formel berechnet - du lieferst NUR die "
        "Variablen-Werte (z.B. entfernung_km, stunden, einkaufspreis), "
        "nicht den Endbetrag.",
        "",
        "Wenn keine Regel passt: lass `kalkulation` weg (null), und gib den "
        "Preis wie gewohnt direkt an.",
        "",
    ]
    for kat, rs in by_kat.items():
        label = KALK_KATEGORIE_LABELS.get(kat, kat)
        lines.append(f"### {label}")
        for r in rs:
            einheit = f" ({r.einheit})" if r.einheit else ""
            vars_txt = ", ".join(r.variablen) if r.variablen else "(keine)"
            lines.append(
                f"- **{r.name}**{einheit}: `{r.formel}`  · "
                f"Variablen: {vars_txt}"
            )
            if r.beschreibung:
                lines.append(f"  Wann: {r.beschreibung}")
        lines.append("")
    return "\n".join(lines)


ANGEBOT_PROMPT = """Du bekommst entweder einen Text oder eine Sprachnachricht eines Handwerkers, der ein Angebot fuer einen Kunden erstellen will.

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
  * preis_brutto_eur: Einzelpreis brutto pro Einheit. Bei "Netto"-Angabe Brutto errechnen (Netto * 1.19).
  * mwst_prozent: Default 19. Photovoltaik 0, Buecher 7.
  * kalkulation: Optional. Wenn ueber dieser Aufgabe ein Block "KALKULATIONS-
    REGELN" steht und eine der Regeln passt, setze hier {"regel_name":
    "<exakter Name>", "variablen": [{"name":"<varname>","wert":<zahl>}, ...]}.
    Liefere NUR die Variablen-Werte - der Preis wird vom System aus der Formel
    berechnet und ueberschreibt preis_brutto_eur. Wenn keine Regel passt: weglassen.

GESAMT:
- gesamtbetrag_brutto_eur: Summe aller (menge * preis_brutto_eur). Pflicht.

META:
- transcript: Bei Audio das wortgetreue Transkript. Bei Text der Original-Text.
- extraction_confidence: "high" wenn alles klar, "medium" bei Unsicherheiten, "low" wenn vieles unklar.
- missing_fields: Liste der fehlenden Pflichtfelder als Strings.

Pflichtfelder: kunde_name, positionen (mit mindestens 1 Eintrag), gesamtbetrag_brutto_eur.
Antworte AUSSCHLIESSLICH mit dem JSON, kein Markdown, keine Erlaeuterung.
"""


async def extract_angebot_from_text(text: str, *, tenant_id=None) -> dict:
    """Extrahiert Angebots-Felder aus Text-Eingabe (analog Rechnung).
    Schema ist identisch (RECHNUNG_RESPONSE_SCHEMA).

    tenant_id (optional): Wenn gesetzt, werden die Kalkulationsregeln des
    Tenants in den Prompt eingespeist (Hybrid-Modus). Pro Position kann
    Gemini dann eine Regel mit Variablenwerten benennen, der Preis wird
    anschliessend deterministisch in Python berechnet.
    """
    if not text or not text.strip():
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    kalk_block = await _build_kalkulation_block(tenant_id)
    full_prompt = (
        (kalk_block + "\n\n" if kalk_block else "")
        + ANGEBOT_PROMPT
        + "\n\n--- Text-Eingabe des Handwerkers: ---\n"
        + text.strip()
    )
    return await _gemini_extract_rechnung([full_prompt], mode="angebot/text")


async def extract_angebot_from_audio(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    *,
    tenant_id=None,
) -> dict:
    """Extrahiert Angebots-Felder aus Sprachnachricht.

    tenant_id (optional): siehe extract_angebot_from_text.
    """
    if not audio_bytes:
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    from google.genai.types import Part

    kalk_block = await _build_kalkulation_block(tenant_id)
    prompt_text = (
        (kalk_block + "\n\n" if kalk_block else "")
        + ANGEBOT_PROMPT
    )
    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_extract_rechnung(
        [audio_part, prompt_text],
        mode=f"angebot/audio/{mime_type}",
    )


def _build_update_prompt(current: dict) -> str:
    """Prompt der bestehende Daten zeigt + Aenderung als JSON-Patch verlangt."""
    import json as _json
    current_json = _json.dumps(current, ensure_ascii=False, indent=2, default=str)
    return f"""Du bist ein Assistent, der bestehende Angebots-Daten aktualisiert.

Hier ist der aktuelle Stand des Angebots (JSON):
{current_json}

Der Handwerker erwaehnt jetzt Aenderungen (Audio/Text folgt unten). Aufgabe:
1. Verstehe die Aenderungen (z.B. "aendere Position 2 auf 350 Euro", "fuege Anfahrt 50 Euro hinzu", "loesche die Material-Position").
2. Gib das KOMPLETTE Angebot als JSON in der gleichen Struktur zurueck, aber mit den gewuenschten Aenderungen.
3. Behalte alle Felder die nicht erwaehnt wurden bei. Erfinde keine Werte.
4. Errechne gesamtbetrag_brutto_eur neu nach den Aenderungen.

Pflichtfelder: kunde_name, positionen (mind. 1 Eintrag), gesamtbetrag_brutto_eur.
Antworte AUSSCHLIESSLICH mit dem aktualisierten JSON, kein Markdown, keine Erlaeuterung.
"""


async def update_angebot_from_text(current_extraction: dict, change_text: str) -> dict:
    """Baut den aktuellen Snapshot um Aenderungen aus Freitext um.
    current_extraction muss die Felder kunde_name, positionen, gesamtbetrag_brutto_eur haben.
    """
    if not change_text or not change_text.strip():
        return current_extraction

    prompt = _build_update_prompt(current_extraction)
    full = prompt + "\n--- Aenderungswuensche des Handwerkers: ---\n" + change_text.strip()
    return await _gemini_extract_rechnung([full], mode="angebot/update_text")


async def update_angebot_from_audio(
    current_extraction: dict,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> dict:
    """Baut den aktuellen Snapshot um Aenderungen aus Sprachnachricht um."""
    if not audio_bytes:
        return current_extraction

    from google.genai.types import Part

    prompt = _build_update_prompt(current_extraction)
    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_extract_rechnung(
        [audio_part, prompt],
        mode=f"angebot/update_audio/{mime_type}",
    )


# Antwort-Klassifikation: ist die eingehende Mail eine Annahme/Ablehnung
# oder eine Rueckfrage zum Angebot?
ANGEBOT_RESPONSE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "classification": {
            "type": "STRING",
            "enum": ["ANNAHME", "ABLEHNUNG", "RUECKFRAGE", "UNSICHER"],
        },
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "reason": {"type": "STRING"},
    },
    "required": ["classification", "confidence", "reason"],
}

ANGEBOT_RESPONSE_PROMPT = """Du klassifizierst die Antwort eines Kunden auf ein per Mail versandtes Angebot eines Handwerkers.

Moegliche Klassen:
- ANNAHME: Kunde nimmt das Angebot an, will dass beauftragt/loslegt wird ("ja, machen wir", "Auftrag erteilt", "passt so", "bitte umsetzen").
- ABLEHNUNG: Kunde lehnt ab oder hat sich anders entschieden ("zu teuer", "nicht mehr noetig", "wir nehmen einen anderen").
- RUECKFRAGE: Kunde hat Fragen oder will Aenderungen ("Koennen Sie noch X aendern?", "Was kostet Variante Y?", "Ist Termin frueher moeglich?").
- UNSICHER: Klassifikation nicht klar.

confidence: "high" wenn eindeutig, "medium" bei leichten Hinweisen, "low" bei schwachen Signalen.
reason: ein Halbsatz auf Deutsch, warum klassifiziert.

Antworte AUSSCHLIESSLICH mit dem JSON.
"""


async def classify_angebot_response(
    *, mail_subject: str, mail_body: str
) -> dict:
    """Sub-Klassifikation einer Mail-Antwort, wenn sie zu einem versandten
    Angebot gehoert. Returns: {classification, confidence, reason}.
    """
    import json as _json
    from google.genai.types import GenerateContentConfig

    prompt = (
        ANGEBOT_RESPONSE_PROMPT
        + f"\n--- Subject ---\n{mail_subject}\n--- Body ---\n{mail_body[:2500]}"
    )

    client = _get_genai_client(location=GENAI_TEXT_LOCATION)
    config = GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=512,
        response_mime_type="application/json",
        response_schema=ANGEBOT_RESPONSE_RESPONSE_SCHEMA,
    )

    def _sync_call():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            config=config,
        )

    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, _sync_call)
        if not response.candidates:
            return {"classification": "UNSICHER", "confidence": "low", "reason": "kein Candidate"}
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return {"classification": "UNSICHER", "confidence": "low", "reason": "leere Antwort"}
        raw_text = "".join(p.text for p in candidate.content.parts if getattr(p, "text", None))
        data = _json.loads(raw_text) if raw_text else {}
        cls = data.get("classification") or "UNSICHER"
        if cls not in ("ANNAHME", "ABLEHNUNG", "RUECKFRAGE", "UNSICHER"):
            cls = "UNSICHER"
        return {
            "classification": cls,
            "confidence": data.get("confidence") or "low",
            "reason": data.get("reason") or "",
        }
    except Exception as e:
        logger.warning(f"classify_angebot_response Fehler: {e}")
        return {"classification": "UNSICHER", "confidence": "low", "reason": f"Fehler: {e}"}

