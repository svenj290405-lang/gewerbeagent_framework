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
# Bild-Intent-Routing: Bild + Freitext → Q entscheidet, was zu tun ist
# =====================================================================
#
# Der Nutzer haengt ein Bild im Q-Chat an und tippt (optional) dazu, was
# damit passieren soll. Gemini entscheidet per Function-Calling zwischen
# den freigeschalteten Aktionen — visualisieren, im Kundenarchiv ablegen,
# als Beleg erfassen — oder beantwortet eine Frage zum Bild als Text.
#
# Ist KEIN klarer Auftrag dabei, fragt Q in einem Satz nach (statt das Bild
# einfach nur zu beschreiben). Die eigentliche Aktion fuehrt das Frontend
# danach mit den vorhandenen Endpunkten aus (es haelt die Bytes ohnehin) —
# diese Funktion liefert nur Intent + extrahierte Argumente.

# action -> (feature-flag, Tool-Beschreibung, Argument-Schema-Bauer)
_BILD_AKTIONEN = {
    "visualisieren": (
        "visualisierung",
        "Erstellt aus dem Foto eine fotorealistische Visualisierung mit der "
        "gewuenschten Aenderung (z.B. Waende streichen, anderer Bodenbelag, "
        "Moebel umstellen). Nur waehlen, wenn der Nutzer eine VERAENDERUNG am "
        "Bild sehen will.",
        {"beschreibung": "Was am Bild geaendert/visualisiert werden soll, "
                         "moeglichst konkret (z.B. 'Waende in warmem Grau, "
                         "Eichenparkett verlegen')."},
    ),
    "archiv": (
        "drive_archiv",
        "Legt das Foto/Dokument im Google-Drive-Ordner eines Kunden ab "
        "(Kunden-Archiv). Waehlen, wenn der Nutzer das Bild bei einem Kunden "
        "speichern/ablegen/archivieren will.",
        {"kunde_name": "Name des Kunden, zu dem das Bild abgelegt wird."},
    ),
    "beleg": (
        "lexware",
        "Erfasst das Foto als Beleg/Rechnung in der Buchhaltung. Waehlen, wenn "
        "es sich um eine Quittung, einen Kassenbon oder eine Rechnung handelt.",
        {},
    ),
}


async def route_image_intent(
    image_bytes: bytes,
    mime_type: str,
    text: str,
    *,
    features: set[str] | None = None,
    company_name: str = "dem Betrieb",
    employee_name: str = "dir",
    history: list | None = None,
) -> dict:
    """Bild + (optionaler) Freitext → Intent. Gemini 2.5 Flash, Function-Calling.

    Rueckgabe (genau einer der Typen):
      * {"type": "message", "text": str}
            Q hat geantwortet oder nachgefragt — keine Aktion noetig.
      * {"type": "action", "action": "visualisieren", "beschreibung": str}
      * {"type": "action", "action": "archiv", "kunde_name": str}
      * {"type": "action", "action": "beleg"}

    Die Aktion fuehrt das Frontend mit den vorhandenen Endpunkten aus.
    Verarbeitung in europe-west3 (Frankfurt) — DSGVO-konform.
    """
    import datetime as _dt
    from google.genai import types

    feats = features or set()
    heute = _dt.date.today()
    wochentag = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                 "Freitag", "Samstag", "Sonntag"][heute.weekday()]

    # Nur freigeschaltete Aktionen anbieten.
    decls = []
    aktiv: list[str] = []
    for action, (feature, beschreibung, schema) in _BILD_AKTIONEN.items():
        if feature not in feats:
            continue
        aktiv.append(action)
        params = {
            "type": "OBJECT",
            "properties": {k: {"type": "STRING", "description": v}
                           for k, v in schema.items()},
            "required": list(schema.keys()),
        }
        decls.append(types.FunctionDeclaration(
            name=action, description=beschreibung, parameters=params))

    aktions_text = {
        "visualisieren": "eine Visualisierung erstellen (Aenderung am Foto)",
        "archiv": "im Kundenarchiv (Google Drive) ablegen",
        "beleg": "als Beleg in der Buchhaltung erfassen",
    }
    optionen = "; ".join(aktions_text[a] for a in aktiv) or "keine"

    system_text = (
        f"Du bist Q, der Assistent von {employee_name} bei {company_name}. "
        f"Heute ist {wochentag}, der {heute.strftime('%d.%m.%Y')}. "
        "Der Nutzer hat ein Bild angehaengt. Entscheide, was er damit moechte.\n"
        f"Moegliche Aktionen mit dem Bild: {optionen}.\n"
        "Regeln:\n"
        "- Verlangt der Text klar eine dieser Aktionen, rufe die passende "
        "Funktion mit den extrahierten Argumenten auf.\n"
        "- Fehlt eine Pflichtangabe (z.B. der Kundenname zum Ablegen), dann "
        "FRAGE kurz nach, statt zu raten — rufe noch keine Funktion auf.\n"
        "- Stellt der Nutzer eine FRAGE zum Bild ('was ist das?', 'was "
        "stimmt hier nicht?'), beantworte sie kurz als Text, ohne Funktion.\n"
        "- Hat der Nutzer NICHTS oder nur Unklares geschrieben, frage in EINEM "
        "kurzen Satz nach, was mit dem Bild passieren soll, und nenne die "
        "moeglichen Aktionen. Beschreibe das Bild dann nicht von dir aus.\n"
        "- Antworte immer kurz, auf Deutsch, in der Du-Form, wie ein Kollege."
    )

    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=1024,
        system_instruction=system_text,
        tools=[types.Tool(function_declarations=decls)] if decls else None,
    )

    # Verlauf voranstellen (Text-Turns); das Bild haengt am letzten User-Turn.
    contents: list = []
    started = False
    for turn in (history or []):
        role = "model" if (turn or {}).get("role") == "model" else "user"
        t = ((turn or {}).get("text") or "").strip()
        if not t or (not started and role != "user"):
            continue
        started = True
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=t)]))

    frage = text.strip() or "(Ich habe ein Bild angehaengt.)"
    contents.append(types.Content(role="user", parts=[
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        types.Part.from_text(text=frage),
    ]))

    client = _get_genai_client(location=GENAI_TEXT_LOCATION)

    def _sync_call():
        return client.models.generate_content(
            model="gemini-2.5-flash", contents=contents, config=config)

    try:
        resp = await asyncio.to_thread(_sync_call)
    except Exception:
        logger.exception("route_image_intent: Gemini-Call fehlgeschlagen")
        return {"type": "message",
                "text": "Konnte das Bild gerade nicht verarbeiten — bitte gleich nochmal."}

    if not resp.candidates or not resp.candidates[0].content:
        return {"type": "message", "text": "Keine Antwort erhalten — bitte erneut."}

    parts = resp.candidates[0].content.parts or []
    fc = next((p.function_call for p in parts if getattr(p, "function_call", None)), None)
    say = "".join(p.text for p in parts if getattr(p, "text", None)).strip()

    if fc is not None and fc.name in aktiv:
        args = {k: v for k, v in dict(fc.args or {}).items()}
        if fc.name == "visualisieren":
            beschreibung = (args.get("beschreibung") or text).strip()
            if len(beschreibung) < 5:
                return {"type": "message",
                        "text": "Beschreib mir kurz, was ich am Foto aendern soll."}
            return {"type": "action", "action": "visualisieren",
                    "beschreibung": beschreibung[:500]}
        if fc.name == "archiv":
            kunde = (args.get("kunde_name") or "").strip()
            if len(kunde) < 2:
                return {"type": "message",
                        "text": "Bei welchem Kunden soll ich das Bild ablegen?"}
            return {"type": "action", "action": "archiv", "kunde_name": kunde[:200]}
        if fc.name == "beleg":
            return {"type": "action", "action": "beleg"}

    return {"type": "message",
            "text": say or "Was soll ich mit dem Bild machen?"}


# =====================================================================
# Rechnung-Extraktion (Text + Audio) via google-genai (europe-west3)
# =====================================================================

RECHNUNG_PROMPT = """Du bekommst entweder einen Text oder eine Sprachnachricht eines Handwerkers, der eine Rechnung erstellen will.

Extrahiere strukturierte Felder als JSON. Wenn ein Feld nicht erwaehnt wird, setze es auf null. Erfinde KEINE Werte.

KUNDE:
- kunde_name: VOLLSTAENDIGER Name des Kunden — bevorzugt Anrede + Nachname
  oder Vor- + Nachname. Beispiele: "Frau Anna Mueller", "Herr Schmidt",
  "Familie Weber", "Bauunternehmen Schmidt GmbH". Pflicht. Wenn der
  Handwerker nur einen einzelnen Nachnamen nennt: setze ihn trotzdem,
  aber liste "kunde_name" in missing_fields auf — der Bot fragt dann nach.
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
- kunde_name: Vollstaendiger Name — bevorzugt Anrede+Nachname oder
  Vor+Nachname (z.B. "Frau Anna Mueller", "Familie Schmidt",
  "Bauunternehmen Schmidt GmbH"). Pflicht: mindestens 2 Wortbestandteile.
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

    # Gemini 2.5 verbraucht intern Thinking-Tokens — bei nur 2048-4096
    # kann es passieren dass die Response leer kommt (finish_reason=
    # MAX_TOKENS), speziell wenn der Prompt um den Kalkulationsblock
    # erweitert ist. 8192 ist defensiv genug fuer komplette Angebote
    # mit 5+ Positionen + Thinking-Budget.
    config = GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=8192,
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


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transkribiert eine Sprachnachricht WORTGETREU zu reinem Text (kein
    Schema, kein Task). Fuer Archiv-Sprachnotizen. Verarbeitung in
    europe-west3 (Frankfurt). Leerer String bei leerem Audio/Fehler.

    mime_type: audio/ogg fuer Telegram-Voice-Notes (Opus codec)."""
    if not audio_bytes:
        return ""
    from google.genai.types import GenerateContentConfig, Part

    client = _get_genai_client(location=GENAI_TEXT_LOCATION)
    model = "gemini-2.5-flash"
    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    prompt = (
        "Transkribiere die folgende Sprachnachricht wortgetreu. Gib NUR den "
        "reinen Transkript-Text in der gesprochenen Sprache zurueck — ohne "
        "Einleitung, ohne Anfuehrungszeichen, ohne Zeitstempel, ohne "
        "Kommentar."
    )
    config = GenerateContentConfig(temperature=0.0, max_output_tokens=2048)

    def _sync_call():
        return client.models.generate_content(
            model=model, contents=[audio_part, prompt], config=config,
        )

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_call)
    except Exception:
        logger.exception("transcribe_audio: Gemini-Call fehlgeschlagen")
        return ""
    if not response.candidates:
        return ""
    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        return ""
    out = ""
    for p in candidate.content.parts:
        if getattr(p, "text", None):
            out += p.text
    return out.strip()



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

    tenant_id wird aktuell nicht ausgewertet — Parameter bleibt fuer
    Backward-Compat. (Vorher wurde hier ein Kalkulations-Block in den
    Prompt eingespeist; das Feature ist Stand 2026-05-17 entfernt.)

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

# ----------------------------------------------------------------------
# Intent-Konstanten — orthogonal zur Kategorie-Klassifikation.
# classification (RELEVANT_KUNDE/...) sagt WER schreibt; intent sagt
# WAS sie wollen. Verwendet vom Microsoft-Inbox-Handler in Teil D
# fuer Routing (Storno -> Kalender-Cancel, Verschiebung -> Rueckfrage,
# Rechnungsanfrage -> nur Telegram-Push, Neuanfrage -> Auto-Reply +
# Formular).
# ----------------------------------------------------------------------
INTENT_NEU_ANFRAGE = "neu_anfrage"
INTENT_TERMINBESTAETIGUNG = "terminbestaetigung"
INTENT_TERMIN_VERSCHIEBEN = "termin_verschieben"
INTENT_TERMIN_STORNIEREN = "termin_stornieren"
INTENT_RUECKFRAGE = "rueckfrage_offener_vorgang"
INTENT_RECHNUNGSANFRAGE = "rechnungsanfrage"
INTENT_SONSTIGES = "sonstiges"

ALLE_INTENTS = (
    INTENT_NEU_ANFRAGE, INTENT_TERMINBESTAETIGUNG,
    INTENT_TERMIN_VERSCHIEBEN, INTENT_TERMIN_STORNIEREN,
    INTENT_RUECKFRAGE, INTENT_RECHNUNGSANFRAGE, INTENT_SONSTIGES,
)


# Keyword-Backup fuer D.3: portiert aus plugins/mail_intake/handler.py
# (extract_termin_aus_mail-Prompt). Wenn Gemini die Intent-Klassifikation
# uneindeutig zurueckgibt (z.B. neu_anfrage trotz "muss leider absagen"
# im Body), uebersteuern wir das hier auf der sicheren Seite.
# Match-Logik: case-insensitive substring auf subject+body_preview.
_INTENT_STORNO_KEYWORDS = (
    "absagen", "stornieren", "muss leider absagen",
    "schaffe es doch nicht", "nicht mehr noetig", "nicht mehr nötig",
    "hat sich erledigt", "doch keinen termin", "abbrechen",
    "termin loeschen", "termin löschen", "krankheitsbedingt absagen",
)
_INTENT_VERSCHIEBEN_KEYWORDS = (
    "verschieben", "umbuchen", "verlegen", "passt doch nicht",
    "umlegen", "stattdessen", "anstatt", "frueher machen",
    "früher machen", "spaeter machen", "später machen", "anderer tag",
)


def _detect_intent_keywords(subject: str, body_preview: str) -> str | None:
    """Sucht starke Intent-Trigger im subject+body.

    Returns: INTENT_TERMIN_STORNIEREN / INTENT_TERMIN_VERSCHIEBEN / None.

    Storno und Verschiebung koennen ueberlappen ("ich muss absagen,
    koennen wir verschieben?"). Auch der Brevo-Prompt-Kommentar in
    handler.py:362-378 sagt: bei Verschiebungs-Wunsch IM Storno-Text
    bevorzugt VERSCHIEBUNG. Deswegen Verschiebungs-Check VOR Storno —
    wenn beides matched, gewinnt Verschiebung.
    """
    blob = f"{subject or ''}\n{body_preview or ''}".lower()
    if any(kw in blob for kw in _INTENT_VERSCHIEBEN_KEYWORDS):
        return INTENT_TERMIN_VERSCHIEBEN
    if any(kw in blob for kw in _INTENT_STORNO_KEYWORDS):
        return INTENT_TERMIN_STORNIEREN
    return None


CLASSIFY_PROMPT = """Du klassifizierst eingehende Mails fuer einen Handwerker.

Mail-Daten:
- Betreff: {subject}
- Absender: {sender}
- Body-Auszug: {body_preview}
- Tenant: {tenant_company}, Branche: {tenant_branche}

Antworte als JSON mit:
- classification: EINER von: RELEVANT_KUNDE, RELEVANT_GESCHAEFT, NICHT_RELEVANT, PRIVAT, UNSICHER
- intent: EINER von: neu_anfrage, terminbestaetigung, termin_verschieben, termin_stornieren, rueckfrage_offener_vorgang, rechnungsanfrage, sonstiges
- confidence: low / medium / high
- reason: 1 Satz Begruendung

Kategorien:
- RELEVANT_KUNDE: Kunden-Anfrage (Termin, Anfrage, Angebot, Reklamation)
- RELEVANT_GESCHAEFT: Geschaefts-Mail (Lieferant, Material-Bestellung, Rechnung von Dienstleister)
- NICHT_RELEVANT: Newsletter, Spam, Werbung, Auto-Notifications
- PRIVAT: Privat-Mail (Familie, Steuerberater, Bank, Versicherung)
- UNSICHER: nicht eindeutig zuzuordnen

Intent-Bedeutung:
- neu_anfrage: neue Kundenanfrage ohne Bezug zu bestehendem Termin
- terminbestaetigung: Kunde bestaetigt einen vereinbarten Termin ("passt!")
- termin_verschieben: Kunde will einen bestehenden Termin verschieben
- termin_stornieren: Kunde will einen bestehenden Termin komplett absagen
- rueckfrage_offener_vorgang: Frage zu laufender Sache (Status, Detail)
- rechnungsanfrage: Frage zu Rechnung/Mahnung/Bezahlung
- sonstiges: alles andere

Bei classification=NICHT_RELEVANT, PRIVAT oder UNSICHER ist intent=sonstiges
ueblich (das Intent-Feld interessiert dann nicht). Bei classification=
RELEVANT_KUNDE waehle das passendste Intent.

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
        "intent": {
            "type": "string",
            "enum": list(ALLE_INTENTS),
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
    body_preview: str = "",
) -> dict:
    """Klassifiziert eine Mail anhand Subject + Sender + Body-Auszug.

    Sehr schnell, sehr billig (subject + 200 Zeichen body).

    body_preview ist optional fuer Rueckwaerts-Kompatibilitaet — wenn
    leer, klassifiziert Gemini ohne Body-Kontext und das Keyword-Backup
    fuer Intent kann nur am Subject matchen.

    Returns: {classification, intent, confidence, reason}
    Bei Fehler: classification=UNSICHER, intent=sonstiges, confidence=low
    """
    import json as _json
    import re as _re

    prompt = CLASSIFY_PROMPT.format(
        subject=(subject or "(kein Betreff)")[:200],
        sender=(sender or "unbekannt")[:200],
        body_preview=(body_preview or "(kein Auszug)")[:300],
        tenant_company=tenant_company[:100],
        tenant_branche=tenant_branche[:50],
    )
    prompt += "\n\nAntworte AUSSCHLIESSLICH mit gueltigem JSON (kein Markdown, keine Erklaerung), z.B.:\n"
    prompt += (
        '{"classification": "RELEVANT_KUNDE", "intent": "neu_anfrage", '
        '"confidence": "high", "reason": "Klare Anfrage"}'
    )

    # Default-Intent passend zur Kategorie — fuer den Fall dass Gemini
    # das Feld nicht zurueckliefert oder die response gar nicht parsen
    # konnten. RELEVANT_KUNDE bekommt neu_anfrage (loest existierenden
    # Auto-Reply-Pfad aus, Verhalten vor Teil D), sonst sonstiges.
    def _default_intent_for(cls_value: str) -> str:
        return INTENT_NEU_ANFRAGE if cls_value == "RELEVANT_KUNDE" else INTENT_SONSTIGES

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

        # Intent extrahieren + validieren mit Default-Fallback fuer alte
        # Caller-Mocks oder Gemini-Antworten ohne intent-Feld.
        intent = result.get("intent") or _default_intent_for(cls)
        if intent not in ALLE_INTENTS:
            intent = _default_intent_for(cls)

        # D.3 Keyword-Backup: starke Storno-/Verschiebungs-Trigger
        # uebersteuern Gemini wenn die Kategorie als RELEVANT_KUNDE
        # erkannt wurde aber das Intent nicht Termin-aenderung sagt.
        # Schuetzt vor dem Audit-Bug "Mail 'Termin absagen' geht durch
        # als neu_anfrage und kriegt Formular-Link".
        if cls == "RELEVANT_KUNDE":
            kw_intent = _detect_intent_keywords(subject or "", body_preview or "")
            if kw_intent and intent not in (
                INTENT_TERMIN_STORNIEREN, INTENT_TERMIN_VERSCHIEBEN,
            ):
                logger.info(
                    f"classify_mail_subject: keyword-override "
                    f"gemini_intent={intent} -> keyword_intent={kw_intent} "
                    f"(subject={subject[:60]!r})"
                )
                intent = kw_intent

        logger.info(
            "classify_mail_subject: subject=%r sender=%r -> %s/%s (%s) %s",
            subject[:60] if subject else "",
            sender[:40] if sender else "",
            cls, intent, conf, reason[:80],
        )
        return {
            "classification": cls, "intent": intent,
            "confidence": conf, "reason": reason,
        }

    except Exception as e:
        logger.warning(f"classify_mail_subject fehler: {e}")
        # Auch im Fehler-Pfad das Keyword-Backup fuer Intent versuchen —
        # wenn der Subject sagt "absagen", soll das Routing das wissen
        # selbst wenn Gemini gerade tot ist.
        kw_intent = _detect_intent_keywords(subject or "", body_preview or "")
        return {
            "classification": "UNSICHER",
            "intent": kw_intent or INTENT_SONSTIGES,
            "confidence": "low",
            "reason": f"Klassifikation fehlgeschlagen: {e}",
        }


async def rank_employee_for_request(
    anliegen_text: str,
    candidates: list[dict],
    *,
    tenant_id: str | None = None,
) -> str | None:
    """Waehlt per Gemini den fachlich passendsten Mitarbeiter fuer eine
    Anfrage — anhand der Skills (+ optional Job-Titel) der VERFUEGBAREN
    Kandidaten.

    candidates: Liste von dicts mit Keys "slug", "name", "skills"
    (list[str]) und optional "job_title". Es werden nur bereits
    verfuegbare Kandidaten uebergeben — der Abwesenheits-/Arbeitszeit-
    Filter laeuft vorher im employee_router.

    Returns: slug des gewaehlten Kandidaten, oder None wenn Gemini keinen
    klaren fachlichen Treffer sieht, die Antwort unbrauchbar ist oder der
    Aufruf scheitert. None heisst fuer den Caller: auf die deterministische
    Stichwort-Logik zurueckfallen — diese Funktion wirft NIE.
    """
    import json as _json
    import re as _re

    if not anliegen_text or not anliegen_text.strip():
        return None
    valid_slugs = {c.get("slug") for c in candidates if c.get("slug")}
    if len(valid_slugs) < 2:
        return None

    zeilen = []
    for c in candidates:
        if not c.get("slug"):
            continue
        skills = ", ".join(c.get("skills") or []) or "(keine Skills hinterlegt)"
        jt = c.get("job_title")
        jt_part = f", Rolle: {jt}" if jt else ""
        name = c.get("name") or c.get("slug")
        zeilen.append(f'- slug "{c["slug"]}" ({name}{jt_part}) — Skills: {skills}')
    kandidaten_block = "\n".join(zeilen)

    prompt = (
        "Du ordnest eine eingehende Kunden-Anfrage dem fachlich am besten "
        "geeigneten Mitarbeiter eines Handwerksbetriebs zu.\n\n"
        f"ANFRAGE:\n{anliegen_text.strip()[:1500]}\n\n"
        f"VERFUEGBARE MITARBEITER:\n{kandidaten_block}\n\n"
        "Waehle GENAU EINEN Mitarbeiter, dessen Skills fachlich am besten "
        "zum Gewerk/Problem der Anfrage passen. Wenn KEIN Mitarbeiter "
        "fachlich klar passt, gib slug null zurueck.\n"
        "Antworte AUSSCHLIESSLICH mit gueltigem JSON (kein Markdown, keine "
        'Erklaerung), z.B.:\n{"slug": "max", "reason": "Heizung -> heizung-Skill"}'
    )

    try:
        text = await call_gemini(
            prompt=prompt,
            # 2048 statt 512: Gemini 2.5 Flash verbraucht Tokens fuers
            # interne "Thinking" VOR dem Output (siehe call_gemini-Doc).
            # Bei 512 brach die Antwort gelegentlich nach dem Markdown-
            # Fence ab ('```json' ohne JSON) -> stiller Stichwort-Fallback.
            # 2048 wie classify_mail_subject/transcribe_audio. Cap, kein
            # Zwang -> keine Latenz-Erhoehung, nur Truncation weg.
            temperature=0.0,
            max_output_tokens=2048,
            tenant_id=tenant_id,
            operation_kind="employee_routing",
        )
        text_stripped = text.strip()
        if text_stripped.startswith("```"):
            text_stripped = _re.sub(r"^```(?:json)?\s*", "", text_stripped)
            text_stripped = _re.sub(r"\s*```$", "", text_stripped)
        try:
            result = _json.loads(text_stripped)
        except _json.JSONDecodeError:
            match = _re.search(r"\{[^{}]*\}", text_stripped, _re.DOTALL)
            if not match:
                logger.warning(
                    f"rank_employee: kein JSON in Antwort: {text[:200]!r}"
                )
                return None
            result = _json.loads(match.group(0))
        slug = result.get("slug")
        if not slug or slug not in valid_slugs:
            logger.info(
                f"rank_employee: kein gueltiger slug ({slug!r}) -> Fallback"
            )
            return None
        logger.info(
            "rank_employee: gewaehlt slug=%s reason=%s",
            slug, (result.get("reason") or "")[:120],
        )
        return slug
    except Exception as e:  # noqa: BLE001
        logger.warning(f"rank_employee_for_request fehler: {e} -> Fallback")
        return None


# =====================================================================
# Mail-Reply-Generator fuer RELEVANT_KUNDE Mails
# Generiert persoenliche Antwort auf Kunden-Anfrage mit Wissensbasis-
# Kontext und Verweis auf Anfrage-Formular.
# =====================================================================

REPLY_PROMPT = """Du bist Q, ein freundlicher Assistent fuer den Handwerker {tenant_company} (Branche: {tenant_branche}).

Du beantwortest eingehende Kunden-Mails im Namen von {im_namen_von}.

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
3. Bitte am Ende mit EINEM kurzen Satz darum, dass der Kunde das
   Anfrage-Formular kurz ausfuellt. WICHTIG: schreibe KEINEN Link,
   KEINE URL und KEINEN "https://..."-String in den Text. Der Button
   zum Formular wird automatisch unter deinem Text eingefuegt — der
   Text darf den Button erwaehnen ("ueber den Button unten" o.ae.),
   aber niemals eine URL enthalten.
4. Antworte in derselben Sprache wie die eingehende Mail. Wenn unklar: Deutsch. Du-Form falls Kunde Du verwendet, sonst Sie. Innerhalb der Antwort MUSST du Du oder Sie konsequent durchziehen — nicht mischen.
5. Unterzeichne mit "{signer}"
6. KEIN Marketing-Geschwafel, KEINE Floskeln wie "Vielen Dank fuer ihre Anfrage"
7. Sei direkt und ehrlich
8. ECHTE UMLAUTE: Schreibe mit echten Umlauten (ä, ö, ü, Ä, Ö, Ü) und ß — NIEMALS Umschreibungen wie ae/oe/ue/ss.

Antworte NUR mit dem Mail-Text (keine Begruessung wie "Hier die Antwort:", kein Markdown)."""


async def generate_anfrage_reply(
    subject: str,
    sender_name: str,
    sender_email: str,
    body: str,
    form_url: str,
    tenant_company: str,
    tenant_owner_first_name: str | None,
    tenant_branche: str = "Handwerk",
    wissensbasis: str = "(keine spezifischen Infos hinterlegt)",
) -> str:
    """Generiert eine persoenliche Antwort auf eine Kunden-Mail.

    tenant_company + tenant_owner_first_name sind Pflicht-Parameter (kein
    Default) damit kein versehentlicher Halluzinations-Name wie "Daniel"
    durchrutscht wenn der Caller den Tenant nicht sauber aufloest.

    tenant_owner_first_name: Vorname des Inhabers (z.B. aus extract_first_name
    auf tenant.contact_name). None oder leer -> Q unterschreibt mit
    "Ihr Team von {tenant_company} (via Q)" statt mit Personen-Name.

    Returns: Plain-Text Antwort (kein HTML), bereit fuer send_mail_as_user.
    """
    owner_first = (tenant_owner_first_name or "").strip()
    company = (tenant_company or "").strip() or "dem Betrieb"
    if owner_first:
        signer = f"{owner_first[:50]} (via Q)"
        im_namen_von = owner_first[:50]
    else:
        signer = f"Ihr Team von {company[:80]} (via Q)"
        im_namen_von = f"Ihr Team von {company[:80]}"

    prompt = REPLY_PROMPT.format(
        tenant_company=company[:100],
        tenant_branche=tenant_branche[:50],
        im_namen_von=im_namen_von,
        signer=signer,
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
        # Fallback-Antwort — keine URL im Text, der Button kommt aus dem Template
        return (
            f"Hallo {sender_name or 'zusammen'},\n\n"
            f"danke für deine Nachricht. Damit ich dir gut weiterhelfen kann, "
            f"füll bitte kurz unser Anfrage-Formular über den Button unten aus. "
            f"Danach melde ich mich schnell mit einem konkreten Angebot.\n\n"
            f"Viele Grüße\n"
            f"{tenant_owner_first_name} (via Q)"
        )


# =====================================================================
# Dialog-Antwort (Phase 1 Mail-Pipeline): Multi-Turn-Konversation mit
# strukturiertem Entscheidungs-Output. Q entscheidet pro Turn ob er
# noch eine Rueckfrage stellt (ASK_MORE) oder das Anfrage-Formular
# rausschickt (SEND_FORMULAR). Wissensbasis kommt als Kontext.
# =====================================================================

DIALOG_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {
            "type": "string",
            "description": (
                "Q's Antwort an den Kunden, Plain-Text mit \\n als "
                "Zeilenumbruch. KEINE URLs, KEIN Markdown, kein "
                "Schlussgruss (Signatur kommt vom Template). Bei "
                "PROPOSE_SLOTS, BOOK_SLOT, CANCEL_TERMIN NICHT die "
                "Slots/Daten im reply_text wiederholen — die rendert "
                "das Template automatisch unter deinem Text."
            ),
        },
        "next_action": {
            "type": "string",
            "enum": [
                "ASK_MORE",
                "SEND_FORMULAR",
                "PROPOSE_SLOTS",
                "BOOK_SLOT",
                "BOOK_DIRECT",
                "CANCEL_TERMIN",
            ],
            "description": (
                "ASK_MORE = Q antwortet rein inhaltlich (Wissensfrage). "
                "SEND_FORMULAR = Anfrage-Formular wird mitgeschickt. "
                "PROPOSE_SLOTS = Kunde nennt nur einen Zeitraum/Tag — "
                "wunsch_datum/wunsch_uhrzeit als Anker mitliefern, die "
                "Pipeline ruft den Kalender und rendert die Vorschlaege. "
                "BOOK_SLOT = Kunde bestaetigt einen vorher angebotenen "
                "Slot aus der letzten Vorschlags-Runde — "
                "chosen_slot_index 0-basiert mitliefern. BOOK_DIRECT = "
                "Kunde nennt einen konkreten Termin direkt (z.B. "
                "'22.05.26 um 14 Uhr', 'Donnerstag 10:30') — direct_datum "
                "+ direct_uhrzeit setzen, Pipeline prueft Verfuegbarkeit "
                "und bucht direkt wenn frei, sonst werden Alternativen "
                "vorgeschlagen. CANCEL_TERMIN = Kunde moechte einen "
                "bestehenden Termin absagen — Pipeline loescht alle "
                "Termine zu dieser Mail-Adresse."
            ),
        },
        "anrede_form": {
            "type": "string",
            "enum": ["DU", "SIE"],
            "description": (
                "Welche Anrede wurde gewaehlt. Bleibt fuer die "
                "naechsten Turns sticky (siehe history)."
            ),
        },
        "wunsch_datum": {
            "type": "string",
            "description": (
                "Nur bei PROPOSE_SLOTS: Wunsch-Datum als TT.MM.JJJJ "
                "wenn der Kunde eines genannt hat (z.B. 'naechsten "
                "Donnerstag' -> konkretes Datum auflesen). Leer wenn "
                "der Kunde gar kein Datum genannt hat — Pipeline "
                "nimmt dann den naechsten Werktag."
            ),
        },
        "wunsch_uhrzeit": {
            "type": "string",
            "description": (
                "Nur bei PROPOSE_SLOTS: Wunsch-Uhrzeit als HH:MM "
                "wenn der Kunde eine genannt hat ('vormittags' -> "
                "09:00, 'nachmittags' -> 14:00, 'frueh' -> 08:00). "
                "Leer wenn keine Praeferenz."
            ),
        },
        "chosen_slot_index": {
            "type": "integer",
            "description": (
                "Nur bei BOOK_SLOT: Index (0-basiert) des Slots aus "
                "der letzten Vorschlags-Runde, den der Kunde bestaetigt "
                "hat. Pipeline loest darueber den konkreten Termin auf."
            ),
        },
        "direct_datum": {
            "type": "string",
            "description": (
                "Nur bei BOOK_DIRECT: konkretes Datum aus dem Kunden-"
                "wunsch als TT.MM.JJJJ. Beispiel: 'naechsten Mittwoch' "
                "-> in das tatsaechliche Datum umrechnen relativ zum "
                "heutigen Datum."
            ),
        },
        "direct_uhrzeit": {
            "type": "string",
            "description": (
                "Nur bei BOOK_DIRECT: konkrete Uhrzeit aus dem Kunden-"
                "wunsch als HH:MM. Beispiel: '14 Uhr' -> '14:00'."
            ),
        },
        "kunde_voller_name": {
            "type": "string",
            "description": (
                "Der VOLLE Name des Kunden (Vor- + Nachname), so wie er "
                "in der Mail, der Signatur oder im Verlauf steht. "
                "PFLICHT-Voraussetzung fuer jede Termin-Aktion "
                "(PROPOSE_SLOTS/BOOK_SLOT/BOOK_DIRECT). Der Absender-"
                "Anzeigename zaehlt nur, wenn er aus Vor- UND Nachname "
                "besteht. Leer lassen, wenn kein voller Name bekannt ist."
            ),
        },
        "kunde_telefon": {
            "type": "string",
            "description": (
                "Die Telefonnummer des Kunden, so wie sie in der Mail, "
                "der Signatur oder im Verlauf steht (Roh-Format genuegt, "
                "die Pipeline normalisiert). PFLICHT-Voraussetzung fuer "
                "jede Termin-Aktion. Leer lassen, wenn keine Nummer "
                "bekannt ist."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Ein-Satz-Begruendung warum next_action so gewaehlt "
                "wurde (fuer Logging)."
            ),
        },
    },
    "required": ["reply_text", "next_action", "anrede_form"],
}


DIALOG_PROMPT = """Du bist Q, ein freundlicher AI-Assistent fuer den Handwerker {tenant_company} (Branche: {tenant_branche}).
Du fuehrst eine Mail-Konversation im Namen von {im_namen_von} mit einem (potenziellen) Kunden.

Dein Ziel: Dem Kunden konkret helfen — Auskunfts-Fragen aus der Wissensbasis beantworten, Termine selbststaendig vorschlagen/buchen/stornieren, und nur wenn wirklich keine Termin-Aktion passt das Anfrage-Formular schicken. Heutiges Datum: {today_date}.

Kontext-Wissen ueber den Betrieb:
{wissensbasis}

{anfrage_status_block}{termin_block}{slots_block}{history_block}Neue Nachricht des Kunden:
- Betreff: {subject}
- Von: {sender_name} ({sender_email})
- Inhalt:
---
{latest_message}
---

HARTES VOR-GATE — WICHTIG, PRUEFE ZUERST:

(I) TERMIN-BUCHUNG BRAUCHT VOLLEN NAMEN + TELEFONNUMMER. Bevor du eine Termin-Aktion (PROPOSE_SLOTS / BOOK_SLOT / BOOK_DIRECT) waehlst, MUSST du zwei Dinge vom Kunden haben:
  - seinen VOLLEN NAMEN (Vor- UND Nachname) — trag ihn in kunde_voller_name ein. Der Absender-Anzeigename zaehlt nur, wenn er aus Vor- und Nachname besteht.
  - eine TELEFONNUMMER — trag sie in kunde_telefon ein (Roh-Format genuegt).
  Beides steht oft in der Signatur oder im Mail-Text — lies es dort heraus. Fehlt eines von beiden und der Kunde will einen Termin: waehle **ASK_MORE** und frag im reply_text freundlich nach dem vollen Namen UND der Telefonnummer (kurz erklaeren: brauchst du fuer die Terminbestaetigung und Rueckfragen). Buche NICHTS, solange nicht beides vorliegt.

(II) BESTEHT BEREITS EIN TERMIN (siehe "Bestehender Termin"-Block oben), schlage von dir aus KEINEN neuen Termin vor und buche keinen zweiten. Verweise im reply_text freundlich auf den bestehenden Termin und beantworte etwaige Fragen (ASK_MORE). Nur wenn der Kunde absagen/verschieben will -> CANCEL_TERMIN.

(III) FORMULAR: Das Anfrage-Formular wird AUTOMATISCH nach einer erfolgreichen Buchung mitgeschickt — du musst es bei einer Buchung NICHT selbst anfordern. SEND_FORMULAR waehlst du nur, wenn der Kunde ein Angebot/einen Auftrag OHNE Terminbezug will und noch kein Formular offen/eingegangen ist. Hat der Kunde schon eines offen oder ausgefuellt: kein zweites schicken (-> ASK_MORE).

ENTSCHEIDUNGS-REIHENFOLGE fuer next_action (PRUEFE IN DIESER REIHENFOLGE und nimm das erste was zutrifft):

(1) Will der Kunde einen bestehenden Termin absagen/verschieben? ("absagen", "stornieren", "doch nicht koennen", "verschieben") -> **CANCEL_TERMIN**. Geht IMMER.

(2) Besteht bereits ein Termin (Block oben) UND der Kunde will nicht absagen? -> KEINE neue Termin-Aktion. Auf den bestehenden Termin verweisen + Fragen beantworten -> **ASK_MORE**.

(3) Bestaetigt der Kunde einen der oben aufgelisteten "Zuletzt vorgeschlagenen Termin-Slots"? ("der erste passt", "ja Donnerstag 14 Uhr aus deiner Liste") -> **BOOK_SLOT** mit chosen_slot_index = 0-basierter Index. NUR wenn voller Name + Telefon vorliegen (sonst ASK_MORE und nach den fehlenden Angaben fragen).

(4) Nennt der Kunde einen KONKRETEN Termin mit Datum UND Uhrzeit? Beispiele: "22.05.26 um 14 Uhr", "Donnerstag 10:30" -> **BOOK_DIRECT** mit direct_datum + direct_uhrzeit. NUR wenn voller Name + Telefon vorliegen (sonst ASK_MORE und nachfragen).

(5) Nennt der Kunde nur einen Tag/Zeitraum (ohne konkrete Uhrzeit)? Beispiele: "Montag passt mir", "diese Woche", "vormittags" -> **PROPOSE_SLOTS** mit wunsch_datum/wunsch_uhrzeit. NUR wenn voller Name + Telefon vorliegen (sonst ASK_MORE und nachfragen).

(6) Will der Kunde ein Angebot / einen Auftrag OHNE Terminbezug? Beispiele: "koennt ihr ein Angebot machen?", "ich brauche eine Kueche" — und es ist noch kein Formular offen/eingegangen -> **SEND_FORMULAR**.

(7) Hat der Kunde rein eine Wissensfrage gestellt (Oeffnungszeiten, Lieferzeiten) ohne Auftrag/Termin? -> **ASK_MORE** — direkt aus der Wissensbasis antworten.

WICHTIGE LEITPLANKEN:

- ECHTE UMLAUTE: Schreibe den reply_text in korrektem Deutsch mit echten Umlauten (ä, ö, ü, Ä, Ö, Ü) und ß — NIEMALS Umschreibungen wie ae/oe/ue/ss. Also "für", "möchte", "Grüße", nicht "fuer", "moechte", "Gruesse".
- IMMER ZUERST DIE FRAGE BEANTWORTEN: Wenn der Kunde eine inhaltliche Frage stellt (Preise, Material, Ablauf, Lieferzeit, Oeffnungszeiten — alles was in der Wissensbasis steht), beantworte sie im reply_text konkret, BEVOR du auf Termin oder Formular verweist. Die Frage des Kunden darf nie unbeantwortet bleiben.
- TERMINBUCHUNG OHNE VOLLEN NAMEN + TELEFONNUMMER IST VERBOTEN. Fehlt eines: ASK_MORE und freundlich danach fragen, statt zu buchen.
- BESTEHT SCHON EIN TERMIN: nie von dir aus einen neuen vorschlagen — auf den bestehenden verweisen.
- Anrede: konsistent Du oder Sie ueber die GESAMTE Antwort. Wenn ein vorheriger Turn eine Anrede gesetzt hat (siehe history), nimm DIESELBE wieder. Sonst: Du wenn der Kunde Du verwendet, sonst Sie.
- KEIN Marketing-Geschwafel, KEINE Floskeln wie "vielen Dank fuer Ihre Anfrage", KEIN "ich freue mich".
- Im reply_text NIEMALS eine URL/Link/"https://"-String einbauen. Bei SEND_FORMULAR darfst du den Button erwaehnen ("ueber den Button unten"), bei PROPOSE_SLOTS die Slot-Liste ("unten findest du Vorschlaege"), sonst keinen Verweis.
- Bei einer Buchung (BOOK_SLOT/BOOK_DIRECT) rendert das Template automatisch eine "Termin bestaetigt"-Box UND den Formular-Button unter deinem Text — wiederhole Datum/Uhrzeit also nicht und fordere das Formular nicht selbst im Text an. Ein kurzer Satz wie "ich habe den Termin fuer dich eingetragen" genuegt.
- KEINE eigene Begruessungszeile ("Hallo X,") und KEINE eigene Signatur — beides macht das Template.
- Schreibe NUR den eigentlichen Mail-Text. Kurz, ehrlich, direkt.

Antwort als JSON gemaess Schema (kein Markdown drumherum)."""


async def handle_kunde_mail_dialog(
    subject: str,
    sender_name: str,
    sender_email: str,
    latest_message: str,
    *,
    history_turns: list[dict] | None = None,
    tenant_company: str,
    tenant_owner_first_name: str | None,
    tenant_branche: str = "Handwerk",
    wissensbasis: str = "(keine spezifischen Infos hinterlegt)",
    previous_anrede_form: str | None = None,
    previous_proposed_slots: list[dict] | None = None,
    anfrage_status: dict | None = None,
    existing_termin: dict | None = None,
) -> dict:
    """Multi-Turn-Dialog-Schritt fuer die Mail-Pipeline.

    history_turns: [{"role": "kunde" | "q", "text": "..."}] in
    chronologischer Reihenfolge. Letzter Eintrag ist nicht die aktuelle
    Mail (die kommt separat in latest_message), sondern alles davor.

    previous_anrede_form: "DU" oder "SIE" wenn aus einem vorherigen Turn
    bekannt — damit Q nicht in der Mitte einer Konversation wechselt.

    previous_proposed_slots: Liste der Slots, die Q in der letzten
    Runde angeboten hat (aus EmailConversation.proposed_slots). Format:
    [{"datum": "22.05.2026", "uhrzeit": "14:00", ...}, ...]. Wird im
    Prompt aufgelistet, damit Q bei einer Bestaetigung "ja, der erste
    passt" den richtigen chosen_slot_index waehlen kann. Ohne diesen
    Block wuerde Q in einer Folge-Mail nicht wissen welche Slots
    aktuell zur Auswahl stehen.

    Returns: dict mit Schluesseln reply_text, next_action,
    anrede_form, reason, plus optional wunsch_datum/wunsch_uhrzeit
    (bei PROPOSE_SLOTS) bzw. chosen_slot_index (bei BOOK_SLOT). Bei
    Fehler: SEND_FORMULAR-Fallback (loggt warning).
    """
    import datetime as _dt
    import json as _json
    from google.genai.types import GenerateContentConfig

    owner_first = (tenant_owner_first_name or "").strip()
    company = (tenant_company or "").strip() or "dem Betrieb"
    im_namen_von = owner_first[:50] if owner_first else f"Ihr Team von {company[:80]}"

    # History-Block bauen (oder leer lassen)
    history_lines: list[str] = []
    for turn in (history_turns or []):
        role = (turn.get("role") or "").lower()
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        speaker = "Q" if role in ("q", "model", "assistant") else "Kunde"
        # je Turn auf 800 Zeichen begrenzen damit der Prompt nicht explodiert
        history_lines.append(f"[{speaker}]: {text[:800]}")
    if previous_anrede_form in ("DU", "SIE"):
        history_lines.append(f"(Anrede in dieser Konversation: {previous_anrede_form})")
    history_block = ""
    if history_lines:
        history_block = "Bisheriger Mail-Verlauf:\n" + "\n".join(history_lines) + "\n\n"

    # Anfrage-Formular-Status-Block: wenn der Kunde das Formular schon
    # ausgefuellt hat (oder eines offen ist), bekommt Q den Kontext.
    # Verhindert dass Q noch ein Formular schickt nachdem der Kunde
    # bereits eines ausgefuellt hat.
    anfrage_status_block = ""
    if anfrage_status:
        status = anfrage_status.get("status")
        if status == "submitted":
            lines = ["Anfrage-Formular-Status:"]
            submitted_at = anfrage_status.get("submitted_at")
            if submitted_at:
                try:
                    lines.append(
                        f"  Der Kunde hat das Anfrage-Formular am "
                        f"{submitted_at.strftime('%d.%m.%Y um %H:%M')} "
                        f"ausgefuellt eingereicht."
                    )
                except Exception:
                    lines.append("  Das Formular wurde bereits ausgefuellt.")
            antw = anfrage_status.get("antworten") or {}
            if antw:
                lines.append("  Daten aus dem Formular:")
                for k, v in list(antw.items())[:8]:
                    if v in (None, "", [], {}):
                        continue
                    v_str = str(v)[:200].replace("\n", " ")
                    lines.append(f"    - {k}: {v_str}")
            lines.append(
                "  -> WICHTIG: Schicke das Formular NICHT nochmal. "
                "Wenn der Kunde nach Termin fragt, mach einen Vorschlag "
                "oder buche direkt (BOOK_DIRECT/PROPOSE_SLOTS)."
            )
            anfrage_status_block = "\n".join(lines) + "\n\n"
        elif status == "open":
            sent_at = anfrage_status.get("sent_at")
            sent_str = ""
            try:
                if sent_at:
                    sent_str = f" am {sent_at.strftime('%d.%m.%Y')}"
            except Exception:
                pass
            anfrage_status_block = (
                f"Anfrage-Formular-Status:\n"
                f"  Ein Anfrage-Formular wurde dem Kunden{sent_str} "
                f"per Mail geschickt, ist aber noch NICHT ausgefuellt. "
                f"Du musst es nicht nochmal schicken — wenn relevant "
                f"darfst du den Kunden einmal hoeflich daran erinnern, "
                f"sonst geh inhaltlich auf seine Mail ein.\n\n"
            )

    # Bestehender-Termin-Block: ist fuer diese Konversation schon ein
    # Termin gebucht, darf Q von sich aus keinen neuen vorschlagen —
    # nur auf Kundenwunsch absagen/verschieben (CANCEL_TERMIN).
    termin_block = ""
    if existing_termin:
        et_datum = (existing_termin.get("datum") or "").strip()
        et_uhrzeit = (existing_termin.get("uhrzeit") or "").strip()
        wann_parts = [p for p in (
            et_datum, (f"um {et_uhrzeit} Uhr" if et_uhrzeit else "")
        ) if p]
        wann = " ".join(wann_parts).strip()
        termin_block = (
            "Bestehender Termin:\n"
            "  Fuer diesen Kunden ist bereits ein Termin gebucht"
            + (f" ({wann})" if wann else "")
            + ".\n"
            "  -> Schlage von dir aus KEINEN neuen Termin vor und buche "
            "keinen zweiten. Verweise hoeflich auf den bestehenden Termin. "
            "Nur wenn der Kunde absagen/verschieben will: CANCEL_TERMIN.\n\n"
        )

    # Slot-Block: nur wenn Q in der letzten Runde Slots vorgeschlagen
    # hat. Damit Q bei einer Bestaetigung "ja Donnerstag um 14 Uhr"
    # den richtigen Index in chosen_slot_index packen kann.
    slots_block = ""
    if previous_proposed_slots:
        slot_lines = ["Zuletzt vorgeschlagene Termin-Slots:"]
        for idx, sl in enumerate(previous_proposed_slots[:6]):
            datum = (sl.get("datum") or "").strip()
            uhrzeit = (sl.get("uhrzeit") or "").strip()
            wochentag = (sl.get("wochentag") or "").strip()
            label_parts = []
            if wochentag:
                label_parts.append(wochentag)
            if datum:
                label_parts.append(datum)
            if uhrzeit:
                label_parts.append(f"{uhrzeit} Uhr")
            label = " ".join(label_parts) or f"Slot {idx}"
            slot_lines.append(f"  [{idx}] {label}")
        slot_lines.append(
            "(Bei BOOK_SLOT chosen_slot_index = die Zahl in eckigen Klammern.)"
        )
        slots_block = "\n".join(slot_lines) + "\n\n"

    signer = f"{owner_first} (via Q)" if owner_first else f"Ihr Team von {company[:80]} (via Q)"

    today_str = _dt.date.today().strftime("%A, %d.%m.%Y")

    prompt = DIALOG_PROMPT.format(
        tenant_company=company[:100],
        tenant_branche=tenant_branche[:50],
        im_namen_von=im_namen_von[:80],
        signer=signer[:80],
        today_date=today_str,
        wissensbasis=(wissensbasis or "(keine Infos)")[:3000],
        anfrage_status_block=anfrage_status_block,
        termin_block=termin_block,
        slots_block=slots_block,
        history_block=history_block,
        subject=(subject or "(kein Betreff)")[:200],
        sender_name=(sender_name or "Kunde")[:100],
        sender_email=(sender_email or "")[:100],
        latest_message=(latest_message or "(kein Inhalt)")[:4000],
    )

    config = GenerateContentConfig(
        temperature=0.3,
        max_output_tokens=4096,
        response_mime_type="application/json",
        response_schema=DIALOG_RESPONSE_SCHEMA,
    )

    def _sync_call():
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config,
        )

    def _fallback(reason: str) -> dict:
        return {
            "reply_text": (
                "danke für deine Nachricht. Damit wir gut weiterhelfen "
                "können, füll bitte kurz unser Anfrage-Formular über "
                "den Button unten aus. Wir melden uns danach schnell."
            ),
            "next_action": "SEND_FORMULAR",
            "anrede_form": previous_anrede_form or "DU",
            "wunsch_datum": None,
            "wunsch_uhrzeit": None,
            "chosen_slot_index": None,
            "direct_datum": None,
            "direct_uhrzeit": None,
            "kunde_voller_name": None,
            "kunde_telefon": None,
            "reason": f"fallback: {reason}",
        }

    try:
        response = await asyncio.to_thread(_sync_call)

        if not response.candidates:
            logger.warning("handle_kunde_mail_dialog: Keine Candidates")
            return _fallback("no_candidates")

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            logger.warning(
                "handle_kunde_mail_dialog: Empty parts. "
                f"finish_reason={getattr(candidate, 'finish_reason', '?')}"
            )
            return _fallback("empty_parts")

        raw_text = "".join(p.text for p in candidate.content.parts if getattr(p, "text", None))
        if not raw_text:
            logger.warning("handle_kunde_mail_dialog: Kein Text in Response")
            return _fallback("empty_text")

        try:
            data = _json.loads(raw_text)
        except _json.JSONDecodeError as e:
            logger.warning(f"handle_kunde_mail_dialog JSON parse fail: {e} | raw={raw_text[:300]!r}")
            return _fallback("json_parse")

        # Validate
        reply_text = (data.get("reply_text") or "").strip()
        next_action = data.get("next_action") or "SEND_FORMULAR"
        if next_action not in (
            "ASK_MORE",
            "SEND_FORMULAR",
            "PROPOSE_SLOTS",
            "BOOK_SLOT",
            "BOOK_DIRECT",
            "CANCEL_TERMIN",
        ):
            next_action = "SEND_FORMULAR"
        anrede_form = data.get("anrede_form") or previous_anrede_form or "DU"
        if anrede_form not in ("DU", "SIE"):
            anrede_form = "DU"

        # BOOK_SLOT braucht einen gueltigen Index — sonst auf
        # PROPOSE_SLOTS zurueckfallen, damit die Pipeline neu vorschlaegt
        # statt willkuerlich zu buchen.
        chosen_slot_index: int | None = None
        if next_action == "BOOK_SLOT":
            raw_idx = data.get("chosen_slot_index")
            try:
                chosen_slot_index = int(raw_idx) if raw_idx is not None else None
            except (TypeError, ValueError):
                chosen_slot_index = None
            slot_count = len(previous_proposed_slots or [])
            if (
                chosen_slot_index is None
                or chosen_slot_index < 0
                or chosen_slot_index >= slot_count
            ):
                logger.warning(
                    f"handle_kunde_mail_dialog: BOOK_SLOT mit ungueltigem "
                    f"index={chosen_slot_index!r} (slots={slot_count}) -> "
                    f"fallback PROPOSE_SLOTS"
                )
                next_action = "PROPOSE_SLOTS"
                chosen_slot_index = None

        # PROPOSE_SLOTS: Wunsch-Datum/-Uhrzeit normalisieren — leer ist OK,
        # die Pipeline waehlt dann den naechsten Werktag.
        wunsch_datum = (data.get("wunsch_datum") or "").strip()
        wunsch_uhrzeit = (data.get("wunsch_uhrzeit") or "").strip()

        # Kontaktdaten fuer die Buchung (Pflicht-Gate in der Pipeline):
        # voller Name + Telefonnummer. Q liest sie aus Mail/Signatur.
        kunde_voller_name = (data.get("kunde_voller_name") or "").strip()
        kunde_telefon = (data.get("kunde_telefon") or "").strip()

        # BOOK_DIRECT braucht direct_datum + direct_uhrzeit. Wenn eines
        # fehlt, faellt der Pfad auf PROPOSE_SLOTS zurueck (mit dem
        # vorhandenen Feld als Anker).
        direct_datum = (data.get("direct_datum") or "").strip()
        direct_uhrzeit = (data.get("direct_uhrzeit") or "").strip()
        if next_action == "BOOK_DIRECT":
            if not direct_datum or not direct_uhrzeit:
                logger.warning(
                    f"handle_kunde_mail_dialog: BOOK_DIRECT ohne "
                    f"direct_datum/uhrzeit ({direct_datum!r}/{direct_uhrzeit!r}) "
                    f"-> fallback PROPOSE_SLOTS"
                )
                next_action = "PROPOSE_SLOTS"
                # Wenn nur eines fehlt, das vorhandene als Anker
                # uebernehmen
                if direct_datum and not wunsch_datum:
                    wunsch_datum = direct_datum
                if direct_uhrzeit and not wunsch_uhrzeit:
                    wunsch_uhrzeit = direct_uhrzeit
                direct_datum = ""
                direct_uhrzeit = ""

        if not reply_text:
            logger.warning("handle_kunde_mail_dialog: reply_text leer, fallback")
            return _fallback("empty_reply_text")

        logger.info(
            f"handle_kunde_mail_dialog: subject={subject[:60]!r} sender={sender_email!r} "
            f"-> next_action={next_action} anrede={anrede_form} reply_len={len(reply_text)}"
            + (f" idx={chosen_slot_index}" if chosen_slot_index is not None else "")
            + (f" datum={wunsch_datum}" if wunsch_datum else "")
            + (f" uhrzeit={wunsch_uhrzeit}" if wunsch_uhrzeit else "")
        )

        return {
            "reply_text": reply_text,
            "next_action": next_action,
            "anrede_form": anrede_form,
            "wunsch_datum": wunsch_datum or None,
            "wunsch_uhrzeit": wunsch_uhrzeit or None,
            "chosen_slot_index": chosen_slot_index,
            "direct_datum": direct_datum or None,
            "direct_uhrzeit": direct_uhrzeit or None,
            "kunde_voller_name": kunde_voller_name or None,
            "kunde_telefon": kunde_telefon or None,
            "reason": (data.get("reason") or "")[:200],
        }
    except Exception as e:
        logger.exception(f"handle_kunde_mail_dialog fehler: {e}")
        return _fallback(f"exception:{type(e).__name__}")


# =====================================================================
# Angebot-Workflow: Extraktion + Update via Sprache + Annahme-Erkennung
# Wiederverwendet RECHNUNG_RESPONSE_SCHEMA (Angebot hat strukturell die
# gleichen Felder: kunde_*, positionen[], gesamtbetrag).
# =====================================================================


ANGEBOT_PROMPT = """Du bekommst entweder einen Text oder eine Sprachnachricht eines Handwerkers, der ein Angebot fuer einen Kunden erstellen will.

Extrahiere strukturierte Felder als JSON. Wenn ein Feld nicht erwaehnt wird, setze es auf null. Erfinde KEINE Werte.

KUNDE:
- kunde_name: VOLLSTAENDIGER Name des Kunden — bevorzugt Anrede + Nachname
  oder Vor- + Nachname. Beispiele: "Frau Anna Mueller", "Herr Schmidt",
  "Familie Weber", "Bauunternehmen Schmidt GmbH". Pflicht. Wenn der
  Handwerker nur einen einzelnen Nachnamen nennt: setze ihn trotzdem,
  aber liste "kunde_name" in missing_fields auf — der Bot fragt dann nach.
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

    tenant_id wird aktuell nicht ausgewertet — Parameter bleibt fuer
    Backward-Compat. (Vorher wurde hier ein Kalkulations-Block in den
    Prompt eingespeist; das Feature ist Stand 2026-05-17 entfernt.)
    """
    if not text or not text.strip():
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    full_prompt = (
        ANGEBOT_PROMPT
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

    tenant_id wird aktuell nicht ausgewertet — Parameter bleibt fuer
    Backward-Compat (siehe extract_angebot_from_text).
    """
    if not audio_bytes:
        return _normalize_rechnung_extraction({"missing_fields": ["alle"]})

    from google.genai.types import Part

    audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    return await _gemini_extract_rechnung(
        [audio_part, ANGEBOT_PROMPT],
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


# =====================================================================
# Personalisiertes Anschreiben fuer ein Angebot
# =====================================================================
# Sven sagt: "schreib freundlich, erwaehne dass wir am Donnerstag Zeit
# haetten" — wir schicken die Anweisungen + Angebots-Snapshot an Gemini
# und bekommen einen kompletten Anschreibe-Text (Briefform, geht im
# Lexware-PDF in den `introduction`-Block).

ANSCHREIBEN_PROMPT_TEMPLATE = """Du bist Assistent fuer einen deutschen Handwerksbetrieb \
und schreibst das Anschreiben (Einleitungstext) zu einem Angebot.

ANGEBOTS-KONTEXT:
- Kunde: {kunde_name}
- Adresse: {kunde_adresse}
- Positionen:
{positionen_summary}
- Gesamtbetrag brutto: {gesamt:.2f} EUR

ANWEISUNGEN VOM HANDWERKER:
{instructions}

AUFGABE:
Schreibe einen kurzen, freundlichen Einleitungstext (max. 5 Saetze, max. 600 Zeichen) \
fuer das Lexware-Angebot. Der Text steht spaeter im Angebots-PDF \
direkt unter "Sehr geehrte..." und vor der Positionsliste.

REGELN:
- Beginne mit "Sehr geehrte Damen und Herren" oder mit der personalisierten \
  Anrede falls aus dem Kunden-Namen erkennbar (z.B. "Sehr geehrte Frau Mueller").
- Beziehe dich kurz auf das angefragte Gewerk (aus den Positionen).
- Setze den vom Handwerker gewuenschten Ton um (sachlich/herzlich/kurz/ausfuehrlich).
- Erwaehne explizit was der Handwerker erwaehnt haben will, falls genannt.
- KEINE Floskeln wie "es freut uns sehr". Klar und respektvoll.
- KEIN Schlusssatz wie "mit freundlichen Gruessen" — das macht Lexware.
- KEINE Markdown-Symbole, keine HTML-Tags, reines Plaintext.

Antworte AUSSCHLIESSLICH mit dem Anschreibe-Text. Keine Erlaeuterung davor oder danach.
"""


def _format_positionen_summary(positionen: list) -> str:
    """Kompakte Zeilenliste der Positionen fuer den Prompt."""
    lines = []
    for i, p in enumerate(positionen or [], 1):
        name = p.get("name") or "Position"
        menge = p.get("menge") or 1
        einheit = p.get("einheit") or "Stueck"
        preis = p.get("preis_brutto_eur") or 0
        lines.append(f"  {i}. {name} ({menge} {einheit}, {float(preis):.2f} EUR)")
    return "\n".join(lines) if lines else "  (keine Positionen)"


async def generate_angebot_anschreiben(
    extracted: dict,
    instructions: str,
    *,
    tenant_id=None,
) -> str:
    """Generiert ein personalisiertes Anschreiben fuer ein Angebot.

    extracted: das Gemini-Extraction-dict (kunde_name, positionen, gesamt…).
    instructions: was der Handwerker geschrieben/diktiert hat — Tonangabe,
                  Hinweise, was rein soll.
    Returns: Plaintext-Anschreiben (max ~600 Zeichen).
    """
    if not instructions or not instructions.strip():
        return ""

    kunde_name = extracted.get("kunde_name") or "(Kunde)"
    addr_bits = [
        extracted.get("kunde_strasse"),
        " ".join(b for b in [extracted.get("kunde_plz"),
                              extracted.get("kunde_ort")] if b).strip(),
    ]
    kunde_adresse = ", ".join(a for a in addr_bits if a) or "(keine Adresse)"
    positionen = extracted.get("positionen") or []
    gesamt = extracted.get("gesamtbetrag_brutto_eur") or sum(
        float(p.get("menge") or 1) * float(p.get("preis_brutto_eur") or 0)
        for p in positionen
    )

    prompt = ANSCHREIBEN_PROMPT_TEMPLATE.format(
        kunde_name=kunde_name,
        kunde_adresse=kunde_adresse,
        positionen_summary=_format_positionen_summary(positionen),
        gesamt=float(gesamt or 0),
        instructions=instructions.strip(),
    )
    try:
        text = await call_gemini(
            prompt,
            temperature=0.4,
            max_output_tokens=2048,
            tenant_id=tenant_id,
            operation_kind="angebot/anschreiben",
        )
    except Exception as exc:
        logger.exception(f"Anschreiben-Generation gescheitert: {exc}")
        return ""
    # Defensive: harten Cutoff bei 800 Zeichen — Lexware respektiert das
    # Feld zwar, aber zu lange Anschreiben sehen im PDF unschoen aus.
    text = (text or "").strip()
    if len(text) > 800:
        text = text[:797] + "..."
    return text


async def generate_angebot_anschreiben_from_audio(
    extracted: dict,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    *,
    tenant_id=None,
) -> str:
    """Wie generate_angebot_anschreiben, aber mit Voice-Anweisungen.

    Schickt die Audio + den Angebots-Kontext-Prompt an Gemini.
    """
    if not audio_bytes:
        return ""

    from google.genai.types import Part
    from google.genai.types import GenerateContentConfig

    # Wir bauen den Prompt OHNE die Anweisungen — die kommen aus dem Audio.
    kunde_name = extracted.get("kunde_name") or "(Kunde)"
    addr_bits = [
        extracted.get("kunde_strasse"),
        " ".join(b for b in [extracted.get("kunde_plz"),
                              extracted.get("kunde_ort")] if b).strip(),
    ]
    kunde_adresse = ", ".join(a for a in addr_bits if a) or "(keine Adresse)"
    positionen = extracted.get("positionen") or []
    gesamt = extracted.get("gesamtbetrag_brutto_eur") or sum(
        float(p.get("menge") or 1) * float(p.get("preis_brutto_eur") or 0)
        for p in positionen
    )
    prompt = ANSCHREIBEN_PROMPT_TEMPLATE.format(
        kunde_name=kunde_name,
        kunde_adresse=kunde_adresse,
        positionen_summary=_format_positionen_summary(positionen),
        gesamt=float(gesamt or 0),
        instructions="(siehe angehaengte Sprachnachricht des Handwerkers)",
    )

    try:
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        config = GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=2048,
        )
        audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        def _sync_call():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[audio_part, prompt],
                config=config,
            )
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_call)
        if not response.candidates or not response.candidates[0].content:
            return ""
        text = ""
        for p in response.candidates[0].content.parts or []:
            if getattr(p, "text", None):
                text += p.text
        text = text.strip()
        if len(text) > 800:
            text = text[:797] + "..."
        return text
    except Exception as exc:
        logger.exception(f"Anschreiben-Audio-Generation gescheitert: {exc}")
        return ""


# =====================================================================
# Kombinierte Personalisierung: Anschreiben + Kundendaten-Korrekturen
# =====================================================================
# Wenn der Handwerker auf "Anpassen" tippt, kann er nicht nur den Ton
# des Anschreibens beeinflussen, sondern auch Korrekturen am Kunden-Block
# ansagen ("Kunde heisst Mueller mit ue, nicht Müller-Schmidt").
# Gemini extrahiert beides in einem Call und liefert sowohl
# field_updates (kunde_name/strasse/plz/ort/email) als auch ein
# fertiges Anschreiben in einem JSON.

PERSONALIZE_ANGEBOT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "field_updates": {
            "type": "OBJECT",
            "properties": {
                "kunde_name": {"type": "STRING", "nullable": True},
                "kunde_strasse": {"type": "STRING", "nullable": True},
                "kunde_plz": {"type": "STRING", "nullable": True},
                "kunde_ort": {"type": "STRING", "nullable": True},
                "kunde_email": {"type": "STRING", "nullable": True},
            },
        },
        "anschreiben": {"type": "STRING", "nullable": True},
    },
}


PERSONALIZE_PROMPT_TEMPLATE = """Du bist Assistent fuer einen deutschen \
Handwerksbetrieb. Der Handwerker hat ein Angebot vorbereitet und moechte \
es vor dem Versand noch anpassen.

AKTUELLER ANGEBOTS-STAND:
- Kunde: {kunde_name}
- Strasse: {kunde_strasse}
- PLZ + Ort: {kunde_plz} {kunde_ort}
- E-Mail: {kunde_email}
- Positionen:
{positionen_summary}
- Gesamtbetrag brutto: {gesamt:.2f} EUR

ANWEISUNGEN VOM HANDWERKER (Text oder Sprachnachricht):
{instructions}

AUFGABE:
Erkenne im Handwerker-Input ZWEI Arten von Aenderungen:

1) <b>Kundendaten-Korrekturen</b> — wenn der Handwerker Name, Strasse, \
PLZ, Ort oder E-Mail explizit oder implizit aendert:
   - "Kunde heisst Mueller, nicht Müller" → kunde_name="...Mueller..."
   - "Strasse ist Hauptstrasse 5, nicht 50" → kunde_strasse=...
   - "Ort ist Berlin" → kunde_ort=...
   - Wenn ein Feld NICHT erwaehnt wird → null lassen (NICHT alten Wert wiederholen).

2) <b>Anschreiben-Anweisungen</b> — Ton, Inhalt, Hinweise was rein soll.

OUTPUT (JSON):
- field_updates: nur Felder die geaendert werden sollen — alle anderen null.
- anschreiben: kompletter neuer Einleitungstext (max 5 Saetze, max 600 Zeichen). \
Beruecksichtigt die KORRIGIERTEN Werte falls es welche gibt.

REGELN fuer den Anschreiben-Text:
- Beginne mit "Sehr geehrte Damen und Herren" oder personalisiert (z.B. \
"Sehr geehrte Frau Mueller") aus dem (ggf. korrigierten) Namen.
- Beziehe dich kurz auf das angefragte Gewerk (aus Positionen).
- Setze den vom Handwerker gewuenschten Ton um.
- KEIN Schlusssatz wie "mit freundlichen Gruessen" (macht Lexware).
- KEINE HTML/Markdown, reines Plaintext.

Antworte AUSSCHLIESSLICH mit dem JSON, kein Erklaerungstext drumherum.
"""


def _build_personalize_prompt(extracted: dict, instructions: str) -> str:
    return PERSONALIZE_PROMPT_TEMPLATE.format(
        kunde_name=extracted.get("kunde_name") or "(nicht erkannt)",
        kunde_strasse=extracted.get("kunde_strasse") or "(nicht erkannt)",
        kunde_plz=extracted.get("kunde_plz") or "",
        kunde_ort=extracted.get("kunde_ort") or "(nicht erkannt)",
        kunde_email=extracted.get("kunde_email") or "(nicht erkannt)",
        positionen_summary=_format_positionen_summary(
            extracted.get("positionen") or []
        ),
        gesamt=float(extracted.get("gesamtbetrag_brutto_eur") or 0),
        instructions=(instructions or "(siehe angehaengte Sprachnachricht)").strip(),
    )


def _clean_field_updates(raw: dict | None) -> dict:
    """Saeubert die field_updates: leere Strings + Placeholder-Werte raus."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    bad_values = {"", "null", "none", "(nicht erkannt)", "—", "-"}
    for k in ("kunde_name", "kunde_strasse", "kunde_plz", "kunde_ort", "kunde_email"):
        v = raw.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            continue
        v = v.strip()
        if v.lower() in bad_values:
            continue
        out[k] = v
    return out


async def personalize_angebot_with_corrections(
    extracted: dict,
    instructions: str,
    *,
    tenant_id=None,
) -> tuple[dict, str]:
    """Kombinierter Call: extrahiert Kundendaten-Korrekturen + generiert
    Anschreiben aus den Handwerker-Anweisungen.

    Returns: (field_updates, anschreiben)
        field_updates: dict mit nur den geaenderten Feldern.
        anschreiben: Plaintext (max ~600 Zeichen), leer bei Fehler.
    """
    if not instructions or not instructions.strip():
        return {}, ""

    import json as _json
    from google.genai.types import GenerateContentConfig

    prompt = _build_personalize_prompt(extracted, instructions)
    try:
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        config = GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_schema=PERSONALIZE_ANGEBOT_SCHEMA,
        )
        def _sync_call():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
                config=config,
            )
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_call)
    except Exception as exc:
        logger.exception(f"personalize_angebot crashed: {exc}")
        return {}, ""

    if not response.candidates or not response.candidates[0].content:
        return {}, ""
    raw_text = ""
    for p in response.candidates[0].content.parts or []:
        if getattr(p, "text", None):
            raw_text += p.text
    try:
        data = _json.loads(raw_text)
    except Exception as exc:
        logger.warning(f"personalize_angebot JSON-parse failed: {exc}")
        return {}, ""

    field_updates = _clean_field_updates(data.get("field_updates"))
    anschreiben = (data.get("anschreiben") or "").strip()
    if len(anschreiben) > 800:
        anschreiben = anschreiben[:797] + "..."
    return field_updates, anschreiben


async def personalize_angebot_with_corrections_from_audio(
    extracted: dict,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    *,
    tenant_id=None,
) -> tuple[dict, str]:
    """Wie personalize_angebot_with_corrections, aber instructions kommen
    aus einer Sprachnachricht.
    """
    if not audio_bytes:
        return {}, ""

    import json as _json
    from google.genai.types import GenerateContentConfig, Part

    prompt = _build_personalize_prompt(
        extracted,
        instructions="(siehe angehaengte Sprachnachricht des Handwerkers)",
    )
    try:
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        config = GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_schema=PERSONALIZE_ANGEBOT_SCHEMA,
        )
        audio_part = Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        def _sync_call():
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[audio_part, prompt],
                config=config,
            )
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_call)
    except Exception as exc:
        logger.exception(f"personalize_angebot_audio crashed: {exc}")
        return {}, ""

    if not response.candidates or not response.candidates[0].content:
        return {}, ""
    raw_text = ""
    for p in response.candidates[0].content.parts or []:
        if getattr(p, "text", None):
            raw_text += p.text
    try:
        data = _json.loads(raw_text)
    except Exception as exc:
        logger.warning(f"personalize_angebot_audio JSON-parse failed: {exc}")
        return {}, ""

    field_updates = _clean_field_updates(data.get("field_updates"))
    anschreiben = (data.get("anschreiben") or "").strip()
    if len(anschreiben) > 800:
        anschreiben = anschreiben[:797] + "..."
    return field_updates, anschreiben


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

