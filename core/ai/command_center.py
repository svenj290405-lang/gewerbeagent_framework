"""Gemini-Kommando-Zentrale für die PWA.

Der Handwerker tippt oder spricht einen Befehl in natürlicher Sprache
("trag Frau Meier morgen 14 Uhr für eine Heizungswartung ein", "bestell
20 Meter Kupferrohr", "Tobias ist die ganze Woche krank"). Gemini
entscheidet per **Function-Calling**, welches Tool auszuführen ist, und
ruft es mit strukturierten Argumenten auf.

Architektur — bewusst sicher:
  * **Read-Tools** (freie Slots suchen, Kunde suchen, Material auflisten,
    offene Rückrufe) laufen SOFORT in der Gemini-Schleife. Ihr Ergebnis
    fließt an Gemini zurück, das daraus den nächsten Schritt ableitet
    (erst Slot suchen → dann Termin buchen).
  * **Write-Tools** (Termin anlegen/stornieren, Rückruf anlegen, Material
    bestellen, Abwesenheit melden) richten sich nach dem
    **Automatisierungsgrad**, den der Betrieb pro Funktion einstellt
    (``core/features/automations.py``, App: Einstellungen → Automatisierung):

      - ``manuell``    — das Tool wird Gemini gar nicht erst angeboten; Q
                         sagt stattdessen, dass der Betrieb das selbst macht.
      - ``assistiert`` — Gemini schlägt vor, ``run_command`` gibt einen
                         ``confirm``-Vorschlag zurück, und erst nach
                         ausdrücklicher Bestätigung führt
                         ``execute_confirmed`` aus (fail-closed). Default.
      - ``automatisch``— ``run_command`` führt direkt aus und meldet
                         ``{"type": "done"}`` zurück.

    Ein Write-Tool ohne Registry-Eintrag verhält sich wie ``assistiert``,
    damit ein neues Tool nie versehentlich ungefragt läuft.
  * **E-Mail schreiben** ist derselbe Mechanismus mit reicherer Vorschau:
    statt einer Bestätigungszeile kommt ein vollständiger Entwurf
    (Empfänger, Betreff, Text, Anhänge) zurück, den der Nutzer in der App
    redigiert und dann über denselben Bestätigungsweg freigibt.

Jedes Tool ist tenant-gescoped (alle DB-Zugriffe über ``ctx.tid``),
feature-gegated (z.B. Kalender-Tools nur bei aktivem ``kalender``-Feature)
und optional inhaber-gegated (Abwesenheit melden). So sieht Gemini nur die
Tools, die dieser Mitarbeiter in diesem Betrieb wirklich nutzen darf.

Die Aktionen selbst sind dünne Wrapper um genau dieselben Primitive, die
auch die manuellen App-Routen nutzen
(``kalender.on_webhook(...)``, ``Rueckruf``-Insert, ``create_absence`` …) —
keine doppelte Geschäftslogik.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.features.automations import MODE_AUTOMATISCH

logger = logging.getLogger(__name__)

# Wie viele Gemini-Runden maximal (Read-Tool → Ergebnis → nächste Runde),
# bevor wir abbrechen. Verhindert Endlosschleifen bei kaputten Prompts.
MAX_STEPS = 6


@dataclass
class Ctx:
    """Ausführungskontext eines Befehls — hart tenant-isoliert."""
    tenant: Any                 # Tenant-Objekt (.id, .slug, .company_name)
    employee: Any               # Employee-Objekt (.id, .name, .slug, .is_default)
    tid: uuid.UUID              # current_tenant_id(request)
    features: set[str] = field(default_factory=set)
    # Automatisierungsgrad pro Funktion (automation_key -> Stufe). Leer
    # bedeutet "nichts geladen" → mode_for_tool fällt auf die Defaults der
    # Registry zurück, also auf das bisherige Verhalten (Bestätigung).
    automation_modes: dict[str, str] = field(default_factory=dict)
    # Effektive Rechte dieses Mitarbeiters (core/features/permissions.py).
    # Leer heißt "nichts geladen" → Tools mit Rechte-Anforderung fallen
    # raus (fail-closed), rechtefreie Tools bleiben nutzbar.
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_inhaber(self) -> bool:
        return bool(getattr(self.employee, "is_default", False))

    def mode_for(self, tool_name: str) -> str:
        """Stufe für ein Write-Tool dieses Befehls."""
        from core.features.automation_check import mode_for_tool
        return mode_for_tool(self.automation_modes, tool_name)


# ---------------------------------------------------------------------------
# Tool-Spezifikation
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    kind: str                                   # "read" | "write"
    description: str
    parameters: dict                            # JSON-Schema (Vertex-Style, OBJECT/STRING…)
    run: Callable[[Ctx, dict], Awaitable[dict]]  # führt die Aktion aus
    feature: str | None = None                  # benötigtes Feature-Flag (None = immer)
    # Benötigtes Recht aus core/features/permissions.py (None = jeder).
    # Q ist ein zweiter Weg zu denselben Daten wie die HTTP-Endpunkte,
    # deshalb TEILEN sich beide dieselben Keys. Zwei Rechtemodelle würden
    # garantiert auseinanderlaufen ("Angebot erstellen" im Chat erlaubt,
    # über den Knopf verboten).
    permission: str | None = None
    # Baut für Write-Tools die menschenlesbare Bestätigungs-Zeile.
    # Darf async sein, wenn die Zeile erst nachschlagen muss (z.B. den
    # Material-Namen zur ID) — der Aufrufer awaitet dann.
    summarize: Callable[[Ctx, dict], str | Awaitable[str]] | None = None


# Platzhalter fuer den kind-Vergleich, wenn ein Call auf kein Tool passt.
_LEER = ToolSpec(name="", kind="read", description="", parameters={},
                 run=None)  # type: ignore[arg-type]


def _available_tools(ctx: Ctx) -> list[ToolSpec]:
    """Filtert die Registry auf das, was dieser Mitarbeiter nutzen darf.

    Zusätzlich zum Feature- und Rechte-Gate fliegen Write-Tools raus,
    deren Automatisierung auf ``manuell`` steht — Gemini bekommt sie erst
    gar nicht zu sehen. Read-Tools sind nie betroffen: "manuell" heißt,
    dass Q nicht *handelt*, nicht dass Q nichts mehr nachschauen darf.

    Der Filter greift zweimal: beim Aufbau der Tool-Liste für Gemini und
    erneut in ``execute_confirmed``. Der Client kann also nichts
    erzwingen, was hier ausgefiltert wurde.
    """
    from core.features.automations import MODE_MANUELL

    out: list[ToolSpec] = []
    for spec in _REGISTRY:
        if spec.feature and spec.feature not in ctx.features:
            continue
        if spec.permission and spec.permission not in ctx.permissions:
            continue
        if spec.kind == "write" and ctx.mode_for(spec.name) == MODE_MANUELL:
            continue
        out.append(spec)
    return out


def _manuelle_hinweise(ctx: Ctx) -> list[str]:
    """Was Q auf 'manuell' gestellt bekommen hat — für den System-Prompt.

    Ohne diesen Hinweis würde Gemini das fehlende Tool nur bemerken und
    ausweichend antworten. Mit Hinweis sagt Q den Satz, den der Betrieb in
    der Registry hinterlegt hat ("Termine trägst du selbst ein …").
    """
    from core.features.automations import AUTOMATIONS, MODE_MANUELL

    hinweise: list[str] = []
    for auto in AUTOMATIONS.values():
        if not auto.tools:
            continue  # reine Hintergrund-Automatisierung, im Chat irrelevant
        if auto.feature and auto.feature not in ctx.features:
            continue
        mode = ctx.automation_modes.get(auto.key, auto.default_mode)
        if mode != MODE_MANUELL:
            continue
        hinweise.append(
            f"- {auto.label}: NICHT ausführen. Sag stattdessen sinngemäß: "
            f"„{auto.manuell_hint}“"
        )
    return hinweise


def _spec_by_name(name: str) -> ToolSpec | None:
    for spec in _REGISTRY:
        if spec.name == name:
            return spec
    return None


# ---------------------------------------------------------------------------
# Gemini-Schleife
# ---------------------------------------------------------------------------

_SCREEN_LABELS: dict[str, str] = {
    "aktuelles":        "Aktionen / Übersicht",
    "kunden":           "Kundensuche",
    "kunden_profil":    "Kundenprofil",
    "assistent":        "Q-Assistent",
    "mehr":             "Mehr",
    "rechnungen_page":  "Rechnungen",
    "angebote_page":    "Angebote",
    "buchhaltung":      "Buchhaltung (offene Posten, Rechnungen, Angebote, Belege)",
    "belege":           "Belege erfassen",
    "team":             "Team",
    "material":         "Materialien",
    "wissen":           "Wissensbasis",
    "visualisierung":   "Visualisierung",
    "aufnahmen":        "Kundengespräche (Diktat, Notizen, Fotos)",
    "rueckrufe_page":   "Rückrufe",
}


def _system_instruction(ctx: Ctx, screen_context: dict | None = None) -> str:
    heute = dt.date.today()
    wochentag = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
                 "Freitag", "Samstag", "Sonntag"][heute.weekday()]
    name = (getattr(ctx.employee, "name", "") or "").split(" ")[0] or "der Nutzer"
    betrieb = getattr(ctx.tenant, "company_name", "") or "dem Betrieb"

    # Auf 'manuell' gestellte Funktionen: Gemini sieht die Tools nicht mehr
    # (siehe _available_tools) und braucht deshalb einen Satz, mit dem Q
    # freundlich ablehnt, statt ratlos zu wirken.
    manuell = _manuelle_hinweise(ctx)
    manuell_block = (
        "\n\nDiese Aufgaben erledigt der Betrieb bewusst selbst — du hast "
        "dafür KEIN Tool und darfst sie nicht ausführen:\n"
        + "\n".join(manuell)
        if manuell else ""
    )

    context_line = ""
    if screen_context:
        screen   = screen_context.get("screen") or ""
        kunde    = screen_context.get("kunde")
        notizen  = (screen_context.get("notizen") or "").strip()
        briefing = (screen_context.get("briefing") or "").strip()
        if screen == "kunden_profil" and kunde:
            context_line = (
                f"\n\nAktueller Kontext: {name} schaut gerade auf das Profil"
                f' des Kunden "{kunde}". Nutze diesen Kunden als Standard,'
                " wenn kein anderer Kunde ausdruecklich genannt wird."
            )
            if notizen:
                context_line += (
                    f'\n\nAktuelle Gesprächsnotizen von "{kunde}" '
                    f"(die der Nutzer gerade liest):\n{notizen}"
                )
            elif briefing:
                context_line += f"\nBriefing: {briefing}"
        elif screen:
            label = _SCREEN_LABELS.get(screen, screen)
            context_line = f'\n\nAktueller Kontext: {name} befindet sich gerade im Bereich "{label}".'

    return (
        "Du bist der Assistent in der App eines Handwerksbetriebs. "
        f"Du hilfst {name} von {betrieb}, Aufgaben per Sprach- oder "
        "Tippbefehl zu erledigen, indem du die bereitgestellten Tools "
        "aufrufst.\n\n"
        f"Heute ist {wochentag}, der {heute.strftime('%d.%m.%Y')}. Rechne "
        "relative Angaben wie 'morgen', 'übermorgen', 'nächsten Montag' "
        "in ein konkretes Datum im Format TT.MM.JJJJ um. Uhrzeiten im "
        "Format HH:MM.\n\n"
        "Regeln:\n"
        "- Nutze immer ein Tool, wenn der Befehl eine Aktion verlangt. "
        "Erfinde niemals Ergebnisse.\n"
        "- Sind im Kontext unten bereits Notizen oder ein Briefing eines Kunden "
        "sichtbar, beantworte Fragen dazu (zusammenfassen, vorlesen, erklären) "
        "DIREKT als Text — kein Tool nötig.\n"
        "- Fehlt eine Pflichtangabe (z.B. Name, Datum oder Uhrzeit für "
        "einen Termin), dann FRAGE kurz nach, statt zu raten.\n"
        "- Brauchst du für eine Buchung einen freien Slot, suche ihn erst "
        "mit dem passenden Such-Tool.\n"
        "- Willst du Material bestellen, hole dir zuerst die Material-Liste, "
        "um die richtige ID zu finden.\n"
        "- Soll ein bestehender Termin VERSCHOBEN werden, nutze "
        "termin_verschieben (nicht stornieren und neu anlegen).\n"
        "- Notizen oder einen Ordner für einen Kunden im Drive-Archiv legst "
        "du mit drive_notiz_anlegen bzw. drive_ordner_anlegen an.\n"
        "- Soll eine E-Mail geschrieben werden ('schreib X eine Mail', "
        "'maile dem Kunden, dass...'), rufe SOFORT email_schreiben auf und "
        "formuliere Betreff und Text KOMPLETT aus — mit Anrede, ganzem Inhalt "
        "und Grußformel im Namen des Betriebs. Kennst du die Adresse nicht, "
        "hol sie dir vorher mit kunde_suchen. Soll eine Datei aus dem "
        "Kundenarchiv mit, hol dir erst mit archiv_dateien die datei_id.\n"
        "- Bei E-Mails NIEMALS ankündigen, zusammenfassen oder um Erlaubnis "
        "fragen ('Ich kann ihm schreiben, dass... Ist das so in Ordnung?'). "
        "Der Nutzer bekommt den fertigen Entwurf zum Lesen, Ändern und "
        "Freigeben in der App vorgelegt — DAS ist die Rückfrage. Antworte "
        "also nie mit dem Mail-Inhalt als Text, sondern rufe das Tool auf. "
        "Fehlt dir nur die Adresse, rufe es trotzdem auf und lass empfaenger "
        "leer; der Nutzer trägt sie im Entwurf nach.\n"
        "- Geht es um die Antwort auf eine bestehende offene Anfrage, nimm "
        "anfrage_beantworten (bleibt im Mail-Thread); für Angebote und "
        "Rechnungen die dafür vorgesehenen Tools — nicht email_schreiben.\n"
        "- Will der Nutzer eine Ansicht/Liste nur SEHEN ('zeig mir...', "
        "'öffne...', 'geh zu...'), rufe das Tool anzeige_oeffnen mit dem "
        "passenden Bereich auf.\n"
        "- Will der Nutzer die NOTIZEN eines Kunden sehen ('zeig mir die "
        "Notizen von...', 'Notizen Kunde X', 'was steht bei X', 'Archiv-Notizen'), "
        "rufe anzeige_oeffnen mit bereich='kunden_archiv', "
        "kunde_name=<exakter Kundenname>, kategorie='notizen' auf — "
        "NICHT kunden_profil.\n"
        "- Antworte kurz und auf Deutsch, in der Du-Form, wie ein Kollege."
        + manuell_block
        + context_line
    )


def _to_plain(value: Any) -> Any:
    """genai-Args (Map/RepeatedComposite) → reine Python-Strukturen."""
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


# Ein Mail-Auftrag im Befehl des Nutzers. Bewusst eng: entweder steht "mailen"
# als Verb da, oder ein Verb des Schreibens/Schickens trifft auf das Substantiv
# "Mail". Das blosse Wort "schreib" ("schreib das in die Wissensdatenbank")
# reicht nicht.
_MAILEN_VERB_RE = re.compile(r"\bmail(e|st|en|t)\b", re.I)
_SCHREIB_VERB_RE = re.compile(r"\b(schreib\w*|schick\w*|send\w*|verfass\w*)\b", re.I)
_MAIL_NOMEN_RE = re.compile(r"\b(mails?|e-?mails?)\b", re.I)


def _ist_mail_auftrag(text: str) -> bool:
    """Verlangt der Befehl erkennbar eine E-Mail?"""
    t = text or ""
    if _MAILEN_VERB_RE.search(t):
        return True
    return bool(_SCHREIB_VERB_RE.search(t)) and bool(_MAIL_NOMEN_RE.search(t))


# Geminis "ich hab's im Kopf fertig, darf ich?"-Formulierungen. Bewusst NUR
# Freigabe-Floskeln: eine echte Rueckfrage ("Was soll drinstehen?", "An wen?")
# ist berechtigt und darf nicht in einen erfundenen Entwurf umgebogen werden.
_FREIGABE_FRAGE_RE = re.compile(
    r"(in ordnung|soll ich|passt (das|es)|einverstanden|so ok|richtig so|"
    r"so (senden|schicken|verschicken|abschicken)|so belassen)", re.I)


def _ist_freigabe_frage(say: str) -> bool:
    """Hat Gemini den Mail-Inhalt in Prosa angekündigt und fragt nur noch,
    ob er so passt? Dann fehlt der Entwurf, nicht die Information."""
    s = say or ""
    return "?" in s and bool(_FREIGABE_FRAGE_RE.search(s))


def _build_genai_tool(specs: list[ToolSpec]):
    from google.genai import types
    decls = [
        types.FunctionDeclaration(
            name=s.name,
            description=s.description,
            parameters=s.parameters,
        )
        for s in specs
    ]
    return types.Tool(function_declarations=decls)


async def run_command(text: str, ctx: Ctx, history: list | None = None, screen_context: dict | None = None) -> dict:
    """Führt einen Befehl aus.

    ``history`` ist der bisherige Gesprächsverlauf als Liste von
    ``{"role": "user"|"model", "text": str}`` — nötig, damit Q bei
    Mehrfach-Rückfragen (z.B. „Wie heißt der Kunde?" → „Sven" → …) den
    Kontext behält und nicht erneut nach schon Gesagtem fragt.

    Rückgabe (genau einer der Typen):
      * {"type": "message", "text": str}
            Gemini hat geantwortet/nachgefragt, keine Aktion nötig.
      * {"type": "confirm", "tool": str, "args": dict, "summary": str,
         "frage": str|None}
            Eine schreibende Aktion ist vorbereitet und wartet auf
            Bestätigung. ``summary`` ist die Klartext-Zeile für die UI.
            Nur bei Automatisierungsgrad ``assistiert``.
      * {"type": "done", "tool": str, "result": dict, "summary": str,
         "frage": str|None}
            Die Aktion lief schon — Automatisierungsgrad ``automatisch``.
            ``summary`` ist dieselbe Klartext-Zeile, diesmal als
            Vollzugsmeldung für die UI.
      * {"type": "email_entwurf", "tool": "email_schreiben", "empfaenger",
         "empfaenger_name", "betreff", "text", "kunde_name", "anhaenge",
         "hinweis", "frage"}
            Ein Mail-Entwurf wartet auf Redaktion + Freigabe.
      * {"type": "error", "text": str}
    """
    text = (text or "").strip()
    if not text:
        return {"type": "error", "text": "Leerer Befehl."}

    specs = _available_tools(ctx)
    if not specs:
        return {"type": "message",
                "text": "Für dich sind aktuell keine Assistent-Aktionen freigeschaltet."}

    from google.genai import types
    from core.ai.gemini import _get_genai_client, GENAI_TEXT_LOCATION

    tool = _build_genai_tool(specs)
    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=2048,
        system_instruction=_system_instruction(ctx, screen_context=screen_context),
        tools=[tool],
    )
    contents: list = []
    # Früheren Verlauf voranstellen. Gemini verlangt, dass der erste Turn
    # eine User-Rolle hat → führende Model-Turns überspringen.
    started = False
    for turn in (history or []):
        role = "model" if (turn or {}).get("role") == "model" else "user"
        t = ((turn or {}).get("text") or "").strip()
        if not t:
            continue
        if not started and role != "user":
            continue
        started = True
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=t)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

    def _sync_call(_contents):
        client = _get_genai_client(location=GENAI_TEXT_LOCATION)
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_contents,
            config=config,
        )

    # Fuer die Mail-Nachfass-Regel unten: der Auftrag kann auch ein paar Turns
    # zurueckliegen ("Schreib ihm eine Mail" → "Worum geht's?" → "dass ...").
    mail_kontext = " ".join(
        [text] + [((t or {}).get("text") or "") for t in (history or [])[-4:]])
    nachgefasst = False  # Mail-Nachfass-Runde nur einmal (siehe unten)
    for _step in range(MAX_STEPS):
        # Bis zu 3 Versuche mit Backoff: ein 429 (RESOURCE_EXHAUSTED) ist meist
        # nur ein kurzer Burst des Vertex-Minutenkontingents (z.B. direkt nach
        # einer Bildgenerierung) und ist nach ein paar Sekunden weg.
        resp = None
        last_exc = None
        for attempt in range(3):
            try:
                resp = await asyncio.to_thread(_sync_call, contents)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if ("RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)) and attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                break
        if resp is None:
            logger.exception("command_center Gemini-Call fehlgeschlagen: %s",
                             last_exc, exc_info=last_exc)
            if "RESOURCE_EXHAUSTED" in str(last_exc) or "429" in str(last_exc):
                return {"type": "error",
                        "text": "Gerade ist viel los (Kontingent kurz erschöpft) — "
                                "probier es in einer Minute nochmal."}
            return {"type": "error",
                    "text": "Der Assistent ist gerade nicht erreichbar. Bitte gleich nochmal."}

        if not resp.candidates or not resp.candidates[0].content:
            return {"type": "error", "text": "Keine Antwort vom Assistenten."}

        cand_content = resp.candidates[0].content
        parts = cand_content.parts or []
        # Gemini darf in EINEM Zug mehrere Tools aufrufen (z.B. Termine +
        # Rückrufe für ein Tagesbriefing). Dann erwartet die API im nächsten
        # Zug GENAU so viele function_response-Parts wie es calls gab —
        # sonst 400 INVALID_ARGUMENT und der Assistent fällt komplett aus.
        fcs = [p.function_call for p in parts
               if getattr(p, "function_call", None)]
        fc = fcs[0] if fcs else None
        say = "".join(p.text for p in parts if getattr(p, "text", None)).strip()

        if fc is None:
            # Kein Tool-Call → Gemini hat geantwortet oder nachgefragt.
            # Sonderfall Mail: statt zu handeln kündigt Gemini den Inhalt
            # gern in Prosa an und fragt "Ist das so in Ordnung?". Der Nutzer
            # soll aber den fertigen Entwurf sehen — also einmal nachfassen
            # (nur einmal, sonst dreht sich die Schleife).
            if (not nachgefasst and _ist_mail_auftrag(mail_kontext)
                    and _ist_freigabe_frage(say)
                    and any(s.name == "email_schreiben" for s in specs)):
                nachgefasst = True
                logger.info("command_center: Mail-Auftrag ohne Tool-Call — fasse nach")
                contents.append(cand_content)
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Frag nicht nach und fasse nichts zusammen. Rufe jetzt "
                         "email_schreiben auf — mit Betreff und vollständig "
                         "ausformuliertem Text. Der Nutzer sieht den Entwurf und "
                         "gibt ihn selbst frei.")]))
                continue
            return {"type": "message",
                    "text": say or "Ich habe dich nicht ganz verstanden — kannst du es anders sagen?"}

        # Bei mehreren Calls entscheidet der erste Write-Call: Write-Pfade
        # kehren sofort zurück (Bestätigung/Ausführung), da wird nichts an
        # Gemini zurückgegeben — die Antwort-Parität ist dann kein Thema.
        fc = next((c for c in fcs
                   if (_spec_by_name(c.name) or _LEER).kind == "write"), fcs[0])
        spec = _spec_by_name(fc.name)
        args = _to_plain(dict(fc.args or {}))
        if spec is None or spec not in specs:
            # Unbekanntes/ungegatetes Tool — defensiv abbrechen.
            logger.warning("command_center: Gemini rief unzulässiges Tool %r auf", fc.name)
            return {"type": "message",
                    "text": "Das kann ich hier nicht. Frag mich z.B. nach Terminen, Rückrufen oder Material."}

        if spec.name == "anzeige_oeffnen":
            # Kein Datenzugriff — der App sagen, welche Ansicht sie öffnen soll.
            bereich = (args.get("bereich") or "aktuelles").strip().lower()
            kunde = (args.get("kunde_name") or "").strip() or None
            kategorie = (args.get("kategorie") or "").strip().lower() or None
            return {"type": "navigate", "bereich": bereich,
                    "kunde": kunde, "kategorie": kategorie, "text": say or None}

        if spec.name == "email_schreiben":
            # Statt der Einzeiler-Bestätigung den kompletten Entwurf an die
            # App geben — der Nutzer redigiert Empfänger/Betreff/Text und
            # gibt erst dann frei (Versand läuft über execute_confirmed).
            #
            # Auch bei 'automatisch' bauen wir den Entwurf, denn erst der
            # löst die Empfänger-Adresse aus dem Kundennamen auf und hängt
            # die Archiv-Dateien an; Gemini liefert oft nur kunde_name. Wir
            # überspringen dann nur die Freigabe-Schleife. Bleibt die
            # Adresse leer, ist Senden unmöglich — dann kommt der Entwurf
            # doch in die App, statt eine Mail ins Nichts zu schicken.
            entwurf = await _build_email_entwurf(ctx, args)
            if (ctx.mode_for(spec.name) != MODE_AUTOMATISCH
                    or not (entwurf.get("empfaenger") or "").strip()):
                entwurf["frage"] = say or None
                return entwurf
            args = {k: v for k, v in entwurf.items()
                    if k not in ("type", "tool", "hinweis")}

        if spec.kind == "write":
            summary = spec.summarize(ctx, args) if spec.summarize else f"{spec.name} ausführen"
            if inspect.isawaitable(summary):
                summary = await summary

            if ctx.mode_for(spec.name) != MODE_AUTOMATISCH:
                # 'assistiert': NICHT ausführen — Bestätigung einholen.
                return {"type": "confirm", "tool": spec.name, "args": args,
                        "summary": summary, "frage": say or None}

            # 'automatisch': direkt ausführen. Bewusst dieselbe summary wie
            # bei der Bestätigung, damit der Betrieb hinterher schwarz auf
            # weiß sieht, was Q in seinem Namen getan hat.
            try:
                result = await spec.run(ctx, args)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "command_center write-tool %s (automatisch) crash: %s",
                    spec.name, exc,
                )
                return {"type": "error",
                        "text": "Aktion fehlgeschlagen. Bitte erneut versuchen."}
            return {"type": "done", "tool": spec.name, "result": _to_plain(result),
                    "summary": summary, "frage": say or None}

        # Read-Tools: ALLE Calls dieses Zuges ausführen und je eine
        # Antwort zurückgeben (siehe Kommentar oben zur Parität).
        antwort_parts = []
        for call in fcs:
            call_spec = _spec_by_name(call.name)
            call_args = _to_plain(dict(call.args or {}))
            if call_spec is None or call_spec not in specs:
                logger.warning(
                    "command_center: Gemini rief unzulässiges Tool %r auf", call.name)
                ergebnis = {"error": "Dieses Werkzeug steht hier nicht zur Verfügung."}
            else:
                try:
                    ergebnis = await call_spec.run(ctx, call_args)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "command_center read-tool %s crash: %s", call.name, exc)
                    ergebnis = {"error": "Tool-Aufruf fehlgeschlagen."}
            antwort_parts.append(types.Part.from_function_response(
                name=call.name, response={"result": _to_plain(ergebnis)}))

        contents.append(cand_content)
        contents.append(types.Content(role="user", parts=antwort_parts))

    return {"type": "message",
            "text": "Das war mir zu komplex — bitte den Befehl in kleinere Schritte teilen."}


async def execute_confirmed(tool_name: str, args: dict, ctx: Ctx) -> dict:
    """Führt eine zuvor vorgeschlagene **Write**-Aktion nach Bestätigung aus.

    Re-validiert Tool-Name, Feature-, Inhaber- und Automatisierungs-Gating
    (der Client darf nichts erzwingen, was Gemini nicht auch durfte). Das
    Automatisierungs-Gate steckt in ``_available_tools``: auf 'manuell'
    gestellte Write-Tools sind dort nicht mehr enthalten, ein nachträglich
    abgeschalteter Bestätigungs-Dialog läuft also ins Leere statt zu feuern.
    """
    from core.features.automations import MODE_MANUELL

    spec = _spec_by_name(tool_name)
    if spec is None or spec.kind != "write":
        return {"type": "error", "text": "Unbekannte Aktion."}
    if ctx.mode_for(tool_name) == MODE_MANUELL:
        return {"type": "error",
                "text": "Diese Aktion ist auf „manuell“ gestellt — ich darf "
                        "sie nicht für dich ausführen."}
    if spec not in _available_tools(ctx):
        return {"type": "error", "text": "Diese Aktion ist für dich nicht freigeschaltet."}
    try:
        result = await spec.run(ctx, _to_plain(args or {}))
    except Exception as exc:  # noqa: BLE001
        logger.exception("command_center write-tool %s crash: %s", tool_name, exc)
        return {"type": "error", "text": "Aktion fehlgeschlagen. Bitte erneut versuchen."}
    return {"type": "done", "tool": tool_name, "result": result}


# ===========================================================================
# Tool-Implementierungen
# ===========================================================================
#
# Jede ``run``-Funktion bekommt (ctx, args) und gibt ein jsonable dict zurück.
# Read-Tools liefern Daten für Gemini; Write-Tools liefern das Ergebnis der
# Aktion für die UI (immer mit "ok": bool).

async def _get_kalender(ctx: Ctx):
    from core.plugin_system import get_plugin_for_tenant
    return await get_plugin_for_tenant(ctx.tenant.slug, "kalender")


# ---- READ -----------------------------------------------------------------

async def _run_freie_slots(ctx: Ctx, args: dict) -> dict:
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"error": "Kalender ist nicht eingerichtet."}
    try:
        days = int(args.get("tage") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 30))
    out = await kalender.on_webhook("find_free_slots", {"days_ahead": days})
    slots = (out or {}).get("slots") or []
    return {"anzahl": len(slots), "slots": slots[:12]}


async def _run_kunde_suchen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.kunde import Kunde
    from core.models.kundengespraech import Kundengespraech
    from core.models.angebot import Angebot
    from core.models.rechnung import Rechnung
    from sqlalchemy import select

    name = (args.get("name") or "").strip()
    if len(name) < 2:
        return {"error": "Bitte mindestens 2 Zeichen für die Suche."}
    like = f"%{name}%"
    async with get_session() as s:
        # Stammdaten zuerst — daraus zieht Q u.a. die Mail-Adresse, wenn
        # er eine Mail an den Kunden schreiben soll.
        k = (await s.execute(
            select(Kunde).where(Kunde.tenant_id == ctx.tid)
            .where(Kunde.name.ilike(like))
            .where(Kunde.merged_into_id.is_(None))
            .order_by(Kunde.name).limit(5)
        )).scalars().all()
        g = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.kunde_name.ilike(like))
            .order_by(Kundengespraech.gespraech_datum.desc()).limit(10)
        )).scalars().all()
        a = (await s.execute(
            select(Angebot).where(Angebot.tenant_id == ctx.tid)
            .where(Angebot.kunde_name.ilike(like))
            .order_by(Angebot.created_at.desc()).limit(10)
        )).scalars().all()
        r = (await s.execute(
            select(Rechnung).where(Rechnung.tenant_id == ctx.tid)
            .where(Rechnung.kunde_name.ilike(like))
            .order_by(Rechnung.created_at.desc()).limit(10)
        )).scalars().all()
    return {
        "kunden": [{"name": x.name, "email": x.email, "telefon": x.telefon,
                    "adresse": x.adresse} for x in k],
        "gespraeche": [{"kunde": x.kunde_name,
                        "briefing": (x.briefing_kurz or "")[:160],
                        "termin": x.termin_datum.isoformat() if x.termin_datum else None}
                       for x in g],
        "angebote_anzahl": len(a),
        "rechnungen_anzahl": len(r),
    }


async def _run_material_liste(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_material import TenantMaterial
    from sqlalchemy import select

    suche = (args.get("suche") or "").strip()
    async with get_session() as s:
        q = (select(TenantMaterial)
             .where(TenantMaterial.tenant_id == ctx.tid)
             .where(TenantMaterial.aktiv.is_(True)))
        if suche:
            q = q.where(TenantMaterial.name.ilike(f"%{suche}%"))
        rows = (await s.execute(q.order_by(TenantMaterial.name).limit(25))).scalars().all()
    return {"material": [{"id": str(m.id), "name": m.name,
                          "einheit": m.einheit,
                          "standard_menge": m.standard_menge} for m in rows]}


async def _run_material_bestellungen(ctx: Ctx, args: dict) -> dict:
    """Zuletzt ausgeloeste Material-Bestellungen (spiegelt den Verlauf im
    Material-Screen). Betriebsweit, nicht nur die eigenen — im Betrieb
    zaehlt, was bestellt IST, nicht wer geklickt hat."""
    from core.database.connection import get_session
    from core.models.tenant_material import MaterialBestellung
    from sqlalchemy import select

    try:
        anzahl = int(args.get("anzahl") or 10)
    except (TypeError, ValueError):
        anzahl = 10
    anzahl = max(1, min(anzahl, 25))
    async with get_session() as s:
        rows = (await s.execute(
            select(MaterialBestellung)
            .where(MaterialBestellung.tenant_id == ctx.tid)
            .order_by(MaterialBestellung.created_at.desc()).limit(anzahl)
        )).scalars().all()
    return {"bestellungen": [
        {"material": o.material_name, "menge": o.menge, "einheit": o.einheit,
         "zeit": o.created_at.isoformat() if o.created_at else None}
        for o in rows]}


async def _material_name(ctx: Ctx, material_id: str) -> str:
    """Name zur Material-ID — leer, wenn die ID nicht zu diesem Betrieb
    gehoert (dieselbe Tenant-Grenze wie beim Bestellen)."""
    from core.database.connection import get_session
    from core.models.tenant_material import TenantMaterial
    from sqlalchemy import select

    try:
        mid = uuid.UUID(material_id)
    except (ValueError, TypeError):
        return ""
    async with get_session() as s:
        return (await s.execute(
            select(TenantMaterial.name)
            .where(TenantMaterial.id == mid)
            .where(TenantMaterial.tenant_id == ctx.tid))).scalar_one_or_none() or ""


async def _run_offene_rueckrufe(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import Rueckruf, RUECKRUF_STATUS_OFFEN
    from sqlalchemy import select

    async with get_session() as s:
        rows = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.tenant_id == ctx.tid)
            .where(Rueckruf.status == RUECKRUF_STATUS_OFFEN)
            .order_by(Rueckruf.created_at.desc()).limit(15)
        )).scalars().all()
    return {"anzahl": len(rows),
            "rueckrufe": [{"kunde": r.kunde_name, "telefon": r.kunde_telefon,
                           "anliegen": (r.anliegen or "")[:120]} for r in rows]}


# ---- WRITE ----------------------------------------------------------------

async def _employee_aus_text(ctx: Ctx, text: str):
    """Findet einen aktiven Mitarbeiter des Betriebs per Slug oder Name.

    Gemini nennt mal "marco", mal "Marco Jantos" — beides soll treffen.
    Immer tenant-gescoped.
    """
    from core.database.connection import get_session
    from core.models.employee import Employee

    such = (text or "").strip().lower()
    if len(such) < 2:
        return None
    async with get_session() as s:
        kandidaten = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == ctx.tid)
            .where(Employee.is_active.is_(True))
        )).scalars().all()
        for e in kandidaten:
            if e.slug.lower() == such or (e.name or "").lower() == such:
                s.expunge(e)
                return e
        for e in kandidaten:
            if such in (e.name or "").lower() or such in e.slug.lower():
                s.expunge(e)
                return e
    return None


async def _run_termin_anlegen(ctx: Ctx, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    datum = (args.get("datum") or "").strip()
    uhrzeit = (args.get("uhrzeit") or "").strip()
    if not name or not datum or not uhrzeit:
        return {"ok": False, "error": "Name, Datum und Uhrzeit sind Pflicht."}
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"ok": False, "error": "Kalender nicht eingerichtet."}
    try:
        dauer = int(args.get("dauer_minuten") or 60)
    except (TypeError, ValueError):
        dauer = 60
    payload = {
        "name": name, "datum": datum, "uhrzeit": uhrzeit,
        "dauer_minuten": dauer,
        "anliegen": (args.get("anliegen") or "").strip() or None,
        "adresse": (args.get("adresse") or "").strip() or None,
        "telefon": (args.get("telefon") or "").strip() or None,
        "kunde_email": (args.get("kunde_email") or "").strip() or None,
        # Standardmaessig traegt sich ein, wer es sagt. Nennt Gemini
        # einen anderen Mitarbeiter, gewinnt der (siehe unten).
        "employee_id": ctx.employee.id,
    }
    ziel_slug = (args.get("mitarbeiter") or "").strip()
    if ziel_slug:
        ziel = await _employee_aus_text(ctx, ziel_slug)
        if ziel is not None:
            payload["employee_id"] = ziel.id
    res = await kalender.on_webhook("book_appointment", payload)
    if (res or {}).get("error"):
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, "datum": datum, "uhrzeit": uhrzeit,
            "kunde": name, "event_id": (res or {}).get("event_id"),
            # Abwesenheit / fehlender Kalender: Q soll es sagen, statt
            # kommentarlos in einen Urlaub hinein zu buchen.
            "mitarbeiter": (res or {}).get("mitarbeiter") or "",
            "hinweise": (res or {}).get("hinweise") or []}


def _summary_termin(ctx: Ctx, args: dict) -> str:
    teile = [f"Termin für {(args.get('name') or '—').strip()}",
             f"am {(args.get('datum') or '?').strip()}",
             f"um {(args.get('uhrzeit') or '?').strip()} Uhr"]
    if args.get("anliegen"):
        teile.append(f"({str(args['anliegen']).strip()})")
    return " ".join(teile) + " anlegen?"


async def _run_termin_stornieren(ctx: Ctx, args: dict) -> dict:
    """Storniert sicher: findet genau EINEN passenden Termin per Kundenname,
    sonst bricht ab (mirror von app_screens.api_termin_storno)."""
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from sqlalchemy import select

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    heute = dt.date.today()
    async with get_session() as s:
        treffer = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.kunde_name.ilike(f"%{name}%"))
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= heute)
            .order_by(Kundengespraech.termin_datum.asc())
        )).scalars().all()
    if len(treffer) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger anstehender Termin für '{name}' gefunden "
            f"({len(treffer)} Treffer). Bitte im Kalender direkt stornieren.")}

    k = treffer[0]
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"ok": False, "error": "Kalender nicht eingerichtet."}
    tmin = (k.termin_datum - dt.timedelta(days=1)).isoformat()
    tmax = (k.termin_datum + dt.timedelta(days=1)).isoformat()
    found = await kalender.on_webhook("find_events", {
        "kunde_name": k.kunde_name, "time_min": tmin, "time_max": tmax})
    termine = (found or {}).get("termine") or []
    if len(termine) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger Kalender-Termin gefunden ({len(termine)} Treffer). "
            "Bitte im Kalender direkt stornieren.")}
    match = termine[0]
    event_id = match.get("event_id")
    cancel_payload: dict = {"event_id": event_id}
    emp_uuid = None
    if match.get("employee_id"):
        try:
            emp_uuid = uuid.UUID(match["employee_id"])
            cancel_payload["employee_id"] = emp_uuid
        except (ValueError, TypeError):
            pass
    res = await kalender.on_webhook("cancel_appointment", cancel_payload)
    if not (res or {}).get("erfolg"):
        return {"ok": False, "error": (res or {}).get("nachricht") or "Storno fehlgeschlagen."}
    mail_sent = False
    try:
        from core.integrations.mail_pipeline import send_storno_confirmation_for_event
        mail_sent = await send_storno_confirmation_for_event(
            tenant_id=ctx.tenant.id,
            company_name=ctx.tenant.company_name or "",
            event_id=event_id, employee_id=emp_uuid, cancelled_count=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_center storno mail crash: %s", exc)
    return {"ok": True, "kunde": k.kunde_name,
            "datum": k.termin_datum.isoformat(), "mail_sent": mail_sent}


def _summary_storno(ctx: Ctx, args: dict) -> str:
    return f"Anstehenden Termin von {(args.get('kunde_name') or '—').strip()} stornieren?"


async def _run_rueckruf_anlegen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import Rueckruf, RUECKRUF_STATUS_OFFEN

    kunde_name = (args.get("kunde_name") or "").strip()
    kunde_telefon = (args.get("kunde_telefon") or "").strip()
    if not kunde_name or not kunde_telefon:
        return {"ok": False, "error": "Name und Telefon sind Pflicht."}
    anliegen = (args.get("anliegen") or "").strip() or \
        f"Manuell angelegt von {getattr(ctx.employee, 'name', None) or 'Mitarbeiter'}"
    async with get_session() as s:
        r = Rueckruf(
            tenant_id=ctx.tid, kunde_name=kunde_name, kunde_telefon=kunde_telefon,
            kunde_email=(args.get("kunde_email") or "").strip() or None,
            anliegen=anliegen, status=RUECKRUF_STATUS_OFFEN,
            assigned_employee_id=getattr(ctx.employee, "id", None))
        s.add(r)
        from core.services.kunde_identity import resolve_kunde_id_safe
        r.kunde_id = await resolve_kunde_id_safe(
            s, ctx.tid, kunde_name, email=r.kunde_email,
            telefon=kunde_telefon)
        await s.commit()
        await s.refresh(r)
    return {"ok": True, "id": str(r.id), "kunde": kunde_name}


def _summary_rueckruf(ctx: Ctx, args: dict) -> str:
    return (f"Rückruf für {(args.get('kunde_name') or '—').strip()} "
            f"({(args.get('kunde_telefon') or '?').strip()}) anlegen?")


async def _run_material_bestellen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_material import (
        TenantMaterial, MaterialBestellung, BESTELL_ART_LINK)
    from sqlalchemy import select

    mid_raw = (args.get("material_id") or "").strip()
    name = (args.get("name") or "").strip()
    try:
        menge = int(args.get("menge") or 0)
    except (TypeError, ValueError):
        menge = 0
    async with get_session() as s:
        m = None
        if mid_raw:
            try:
                m = (await s.execute(
                    select(TenantMaterial)
                    .where(TenantMaterial.id == uuid.UUID(mid_raw))
                    .where(TenantMaterial.tenant_id == ctx.tid))).scalar_one_or_none()
            except (ValueError, TypeError):
                m = None
        if m is None and name:
            rows = (await s.execute(
                select(TenantMaterial)
                .where(TenantMaterial.tenant_id == ctx.tid)
                .where(TenantMaterial.aktiv.is_(True))
                .where(TenantMaterial.name.ilike(f"%{name}%")).limit(2)
            )).scalars().all()
            if len(rows) == 1:
                m = rows[0]
            elif len(rows) > 1:
                return {"ok": False, "error": f"'{name}' ist nicht eindeutig — bitte genauer."}
        if m is None:
            return {"ok": False, "error": "Material nicht gefunden."}
        if not m.aktiv:
            return {"ok": False, "error": "Material ist deaktiviert."}
        if menge < 1:
            menge = m.standard_menge or 1
        s.add(MaterialBestellung(
            tenant_id=ctx.tid, material_id=m.id,
            employee_id=getattr(ctx.employee, "id", None),
            material_name=m.name, bestell_link=m.bestell_link,
            menge=menge, einheit=m.einheit, bestell_art=BESTELL_ART_LINK))
        await s.commit()
        return {"ok": True, "material": m.name, "menge": menge,
                "einheit": m.einheit, "bestell_link": m.bestell_link}


async def _summary_material(ctx: Ctx, args: dict) -> str:
    # Gemini liefert meist nur die material_id aus material_liste. Eine UUID
    # in der Bestaetigung ("4x ebf24bc3-... bestellen?") liest sich wie ein
    # Fehler, darum hier der Umweg ueber die DB auf den echten Namen.
    bez = (args.get("name") or "").strip()
    if not bez:
        bez = await _material_name(ctx, (args.get("material_id") or "").strip())
    bez = bez or "Material"
    menge = args.get("menge")
    return (f"{menge}× " if menge else "") + f"{bez} bestellen?"


async def _run_abwesenheit(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.employee import Employee
    from core.models.employee_absence import create_absence
    from sqlalchemy import select, or_

    typ = (args.get("typ") or "krank").strip()
    if typ not in ("krank", "urlaub", "sonstiges"):
        return {"ok": False, "error": "Typ muss krank, urlaub oder sonstiges sein."}
    mitarbeiter = (args.get("mitarbeiter") or "").strip()
    if not mitarbeiter:
        return {"ok": False, "error": "Bitte den Mitarbeiter nennen."}
    try:
        start = dt.date.fromisoformat((args.get("start") or "").strip()) \
            if args.get("start") else dt.date.today()
    except ValueError:
        return {"ok": False, "error": "Start-Datum ungültig (YYYY-MM-DD)."}
    ende = None
    if (args.get("ende") or "").strip():
        try:
            ende = dt.date.fromisoformat(args["ende"].strip())
        except ValueError:
            return {"ok": False, "error": "End-Datum ungültig (YYYY-MM-DD)."}
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == ctx.tid)
            .where(or_(Employee.slug == mitarbeiter,
                       Employee.name.ilike(f"%{mitarbeiter}%")))
            .limit(2))).scalars().all()
    if not emp:
        return {"ok": False, "error": f"Mitarbeiter '{mitarbeiter}' nicht gefunden."}
    if len(emp) > 1:
        return {"ok": False, "error": f"'{mitarbeiter}' ist nicht eindeutig — bitte Vor- und Nachname."}
    try:
        ab = await create_absence(
            employee_id=emp[0].id, start_date=start, end_date=ende,
            absence_type=typ, notes=(args.get("notes") or "").strip() or None,
            created_by_employee_id=getattr(ctx.employee, "id", None))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "mitarbeiter": emp[0].name, "typ": typ,
            "start": start.isoformat(), "ende": ende.isoformat() if ende else None,
            "absence_id": str(ab.id)}


def _summary_abwesenheit(ctx: Ctx, args: dict) -> str:
    typ = (args.get("typ") or "krank").strip()
    label = {"krank": "krankmelden", "urlaub": "in Urlaub setzen",
             "sonstiges": "abwesend melden"}.get(typ, "abwesend melden")
    mit = (args.get("mitarbeiter") or "—").strip()
    zeit = ""
    if args.get("start"):
        zeit = f" ab {args['start']}"
        if args.get("ende"):
            zeit = f" von {args['start']} bis {args['ende']}"
    return f"{mit}{zeit} {label}?"


# ---- READ (Erweiterung) ---------------------------------------------------

async def _run_anstehende_termine(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from sqlalchemy import select

    try:
        tage = int(args.get("tage") or 14)
    except (TypeError, ValueError):
        tage = 14
    tage = max(1, min(tage, 60))
    heute = dt.date.today()
    bis = heute + dt.timedelta(days=tage)
    async with get_session() as s:
        rows = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= heute)
            .where(Kundengespraech.termin_datum < bis)
            .order_by(Kundengespraech.termin_datum.asc()).limit(30)
        )).scalars().all()
    termine = [
        {"kunde": r.kunde_name,
         "termin": r.termin_datum.isoformat() if r.termin_datum else None,
         "info": (r.briefing_kurz or "")[:120],
         "event_id": r.kalender_event_id or ""}
        for r in rows
    ]

    # Der Kalender ist die zweite Quelle — und fuer Q die wichtigere:
    # Termine, die er selbst am Telefon oder per Mail gebucht hat, legen
    # KEIN Kundengespraech an. Ohne diesen Zweig fehlten im Tagesbriefing
    # ausgerechnet die Termine, die Q vereinbart hatte.
    try:
        from core.api.app_screens import (
            _geplante_kalendertermine, _kunde_aus_betreff,
        )
        belegt = {t["event_id"] for t in termine if t["event_id"]}
        grenze = dt.datetime.combine(bis, dt.time.min)
        for ev in await _geplante_kalendertermine(ctx.tid, ctx.employee.id, tage):
            if ev["event_id"] and ev["event_id"] in belegt:
                continue
            if ev["start"] >= grenze:
                continue
            termine.append({
                "kunde": _kunde_aus_betreff(ev["titel"]),
                "termin": ev["start"].isoformat(),
                "info": ev["ort"],
                "event_id": ev["event_id"],
            })
        termine.sort(key=lambda t: t["termin"] or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("anstehende_termine: Kalender nicht ladbar: %s", exc)

    return {"anzahl": len(termine), "termine": termine}


async def _run_team_status(ctx: Ctx, args: dict) -> dict:
    from core.models.employee import get_employees_for_tenant
    from core.models.employee_absence import get_active_absences, get_upcoming_absences

    heute = dt.date.today()
    employees = await get_employees_for_tenant(ctx.tid, active_only=False)
    active = await get_active_absences(ctx.tid, heute)
    upcoming = await get_upcoming_absences(ctx.tid, days_ahead=7)
    absent_today = {emp.id: ab for emp, ab in active}
    upc: dict = {}
    for emp, ab in upcoming:
        upc.setdefault(emp.id, []).append(ab)
    out = []
    for e in employees:
        ab = absent_today.get(e.id)
        out.append({
            "name": e.name,
            "inhaber": bool(getattr(e, "is_default", False)),
            "aktiv": bool(getattr(e, "is_active", True)),
            "abwesend_heute": (ab.absence_type if ab else None),
            "kommende_abwesenheit": [a.start_date.isoformat() for a in upc.get(e.id, [])][:1],
        })
    return {"team": out}


async def _run_offene_anfragen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.email_conversation import EmailConversation, STATE_CLOSED
    from sqlalchemy import select

    async with get_session() as s:
        rows = (await s.execute(
            select(EmailConversation)
            .where(EmailConversation.tenant_id == ctx.tid)
            .where(EmailConversation.state != STATE_CLOSED)
            .order_by(EmailConversation.updated_at.desc()).limit(12)
        )).scalars().all()
    return {"anzahl": len(rows), "anfragen": [
        {"kunde": r.kunde_name or "—", "betreff": (r.last_subject or "")[:120]} for r in rows]}


async def _run_wissen_suchen(ctx: Ctx, args: dict) -> dict:
    """Wissensdatenbank durchsuchen.

    Nutzt dasselbe Ranking wie Telefon und Mail (core/services/wissen.py)
    statt eines eigenen ``ilike`` — sonst findet Q in der App etwas, was er
    am Telefon nicht findet, und der Inhaber kann Kundenauskuenfte nicht
    nachvollziehen.

    ``nur_kunde=False``: in der App sieht der Betrieb auch seine internen
    Eintraege. Nach draussen gehen die nie (Voice/Mail filtern).
    """
    from core.models.tenant_knowledge import SICHTBARKEIT_KUNDE
    from core.services import wissen as wissen_service

    frage = (args.get("frage") or "").strip()
    alle = await wissen_service.lade(ctx.tid, nur_kunde=False)
    treffer = wissen_service.sortiere(frage, alle, limit=12) if frage else alle[:30]
    if frage and not treffer:  # nichts Passendes → lieber alles zeigen
        treffer = alle[:30]
    return {"eintraege": [{
        "kategorie": e.label,
        "text": e.text,
        "nur_intern": e.sichtbarkeit != SICHTBARKEIT_KUNDE,
    } for e in treffer]}


async def _run_wissensluecken(ctx: Ctx, args: dict) -> dict:
    """Die Kundenfragen, auf die Q keine Antwort hatte."""
    from core.services import wissen as wissen_service
    luecken = await wissen_service.offene_luecken(ctx.tid, limit=10)
    return {"anzahl": len(luecken), "luecken": luecken}


async def _run_ueberschlag(ctx: Ctx, args: dict) -> dict:
    """Richtwert nach einer hinterlegten Formel — deterministisch gerechnet."""
    from core.services.kalkulation import rechne
    werte = args.get("werte") or {}
    if not isinstance(werte, dict):
        werte = {}
    return await rechne(ctx.tid, (args.get("name") or "").strip(), werte)


# ---- WRITE (Erweiterung) --------------------------------------------------

async def _run_wissen_merken(ctx: Ctx, args: dict) -> dict:
    import datetime as _dt

    from core.database.connection import get_session
    from core.models.tenant_knowledge import (
        ALLE_SICHTBARKEITEN, KATEGORIE_LABELS, QUELLE_Q, SICHTBARKEIT_KUNDE,
        TenantKnowledge,
    )

    kategorie = (args.get("kategorie") or "").strip()
    text = (args.get("text") or "").strip()
    sichtbarkeit = (args.get("sichtbarkeit") or SICHTBARKEIT_KUNDE).strip()
    if kategorie not in KATEGORIE_LABELS:
        kategorie = "faq"
    if sichtbarkeit not in ALLE_SICHTBARKEITEN:
        sichtbarkeit = SICHTBARKEIT_KUNDE
    if not (3 <= len(text) <= 2000):
        return {"ok": False, "error": "Text muss 3–2000 Zeichen haben."}
    async with get_session() as s:
        s.add(TenantKnowledge(
            tenant_id=ctx.tid, kategorie=kategorie, text=text,
            sichtbarkeit=sichtbarkeit, quelle=QUELLE_Q,
            zuletzt_bestaetigt_am=_dt.datetime.now(_dt.timezone.utc),
        ))
        await s.commit()
    return {
        "ok": True,
        "kategorie": KATEGORIE_LABELS.get(kategorie, kategorie),
        "text": text,
        "sichtbarkeit": sichtbarkeit,
    }


def _summary_wissen(ctx: Ctx, args: dict) -> str:
    t = (args.get("text") or "").strip()
    return f"Zur Wissensdatenbank merken: „{t[:90]}{'…' if len(t) > 90 else ''}\"?"


async def _run_rueckruf_erledigt(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.rueckruf import (
        Rueckruf, RUECKRUF_STATUS_OFFEN, RUECKRUF_STATUS_ERLEDIGT)
    from sqlalchemy import select

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    async with get_session() as s:
        rows = (await s.execute(
            select(Rueckruf)
            .where(Rueckruf.tenant_id == ctx.tid)
            .where(Rueckruf.status == RUECKRUF_STATUS_OFFEN)
            .where(Rueckruf.kunde_name.ilike(f"%{name}%")).limit(2)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein offener Rückruf für '{name}'."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere offene Rückrufe für '{name}' — bitte genauer."}
        r = rows[0]
        r.status = RUECKRUF_STATUS_ERLEDIGT
        r.erledigt_at = dt.datetime.now(dt.timezone.utc)
        r.erledigt_by_employee_id = getattr(ctx.employee, "id", None)
        await s.commit()
        kunde = r.kunde_name
    return {"ok": True, "kunde": kunde}


def _summary_rueckruf_erledigt(ctx: Ctx, args: dict) -> str:
    return f"Rückruf von {(args.get('kunde_name') or '—').strip()} als erledigt abhaken?"


async def _run_mitarbeiter_zurueck(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.employee import Employee
    from core.models.employee_absence import close_absence
    from sqlalchemy import select, or_

    mitarbeiter = (args.get("mitarbeiter") or "").strip()
    if not mitarbeiter:
        return {"ok": False, "error": "Bitte den Mitarbeiter nennen."}
    async with get_session() as s:
        emp = (await s.execute(
            select(Employee)
            .where(Employee.tenant_id == ctx.tid)
            .where(or_(Employee.slug == mitarbeiter,
                       Employee.name.ilike(f"%{mitarbeiter}%"))).limit(2)
        )).scalars().all()
    if not emp:
        return {"ok": False, "error": f"Mitarbeiter '{mitarbeiter}' nicht gefunden."}
    if len(emp) > 1:
        return {"ok": False, "error": f"'{mitarbeiter}' ist nicht eindeutig — bitte Vor- und Nachname."}
    closed = await close_absence(emp[0].id, dt.date.today())
    return {"ok": True, "mitarbeiter": emp[0].name, "war_abwesend": closed is not None}


def _summary_mitarbeiter_zurueck(ctx: Ctx, args: dict) -> str:
    return f"{(args.get('mitarbeiter') or '—').strip()} als wieder verfügbar melden?"


async def _run_auftrag_status(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.angebot import (
        Angebot, AUFTRAG_LIFECYCLE, AUFTRAG_LIFECYCLE_LABELS,
        ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_WORK_IN_PROGRESS,
        ANGEBOT_STATUS_WORK_DONE, ANGEBOT_STATUS_ABGEBROCHEN,
        ANGEBOT_STATUS_RECHNUNG_GESENDET)
    from sqlalchemy import select

    settable = {ANGEBOT_STATUS_ACCEPTED, ANGEBOT_STATUS_WORK_IN_PROGRESS,
                ANGEBOT_STATUS_WORK_DONE, ANGEBOT_STATUS_ABGEBROCHEN}
    name = (args.get("kunde_name") or "").strip()
    status = (args.get("status") or "").strip()
    if status not in settable:
        return {"ok": False, "error": (
            "Status muss accepted, arbeit_laeuft, arbeit_fertig oder "
            "abgebrochen sein. (Rechnung-raus läuft separat.)")}
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    # Ohne den Abzug fand Q auch abgerechnete Auftraege und setzte sie auf
    # "fertig" zurueck — danach war der Weg zu einer zweiten Rechnungsnummer
    # offen (Audit 2026-08-24). Ein abgerechneter Auftrag ist kein laufender.
    relevante = ((set(AUFTRAG_LIFECYCLE) | {ANGEBOT_STATUS_ABGEBROCHEN})
                 - {ANGEBOT_STATUS_RECHNUNG_GESENDET})
    async with get_session() as s:
        rows = (await s.execute(
            select(Angebot)
            .where(Angebot.tenant_id == ctx.tid)
            .where(Angebot.kunde_name.ilike(f"%{name}%"))
            .where(Angebot.status.in_(relevante)).limit(2)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein laufender Auftrag für '{name}' gefunden."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere Aufträge für '{name}' — bitte genauer."}
        a = rows[0]
        a.status = status
        if status == ANGEBOT_STATUS_ACCEPTED and not a.accepted_at:
            a.accepted_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()
        kunde = a.kunde_name
    return {"ok": True, "kunde": kunde, "status": status,
            "status_label": AUFTRAG_LIFECYCLE_LABELS.get(status, status)}


def _summary_auftrag_status(ctx: Ctx, args: dict) -> str:
    label = {"accepted": "angenommen", "arbeit_laeuft": "Arbeit läuft",
             "arbeit_fertig": "fertig", "abgebrochen": "abgebrochen"}.get(
        (args.get("status") or "").strip(), args.get("status") or "?")
    return f"Auftrag von {(args.get('kunde_name') or '—').strip()} auf „{label}\" setzen?"


async def _run_material_anlegen(ctx: Ctx, args: dict) -> dict:
    import re
    from core.database.connection import get_session
    from core.models.tenant_material import TenantMaterial
    from sqlalchemy import select

    name = (args.get("name") or "").strip()[:200]
    link = (args.get("bestell_link") or "").strip()
    if not name or not link:
        return {"ok": False, "error": "Name und Bestell-Link sind Pflicht."}
    if not link.startswith(("http://", "https://")):
        return {"ok": False,
                "error": "Der Bestell-Link muss mit http:// oder https:// beginnen."}
    if len(link) > 2000:
        return {"ok": False, "error": "Der Bestell-Link ist zu lang (max. 2000 Zeichen)."}
    try:
        std = int(args.get("standard_menge") or 1)
    except (TypeError, ValueError):
        std = 1
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "material"
    async with get_session() as s:
        slug = base
        i = 2
        while (await s.execute(
            select(TenantMaterial.id)
            .where(TenantMaterial.tenant_id == ctx.tid)
            .where(TenantMaterial.slug == slug))).scalar_one_or_none() is not None:
            slug = f"{base}-{i}"
            i += 1
            if i > 30:
                return {"ok": False, "error": "Konnte keinen eindeutigen Slug bilden."}
        m = TenantMaterial(
            tenant_id=ctx.tid, slug=slug, name=name, bestell_link=link,
            lieferant_name=(args.get("lieferant") or "").strip() or None,
            einheit=(args.get("einheit") or "Stück").strip() or "Stück",
            standard_menge=max(1, std),
            notes=(args.get("notes") or "").strip() or None, aktiv=True)
        s.add(m)
        await s.commit()
    return {"ok": True, "name": name, "slug": slug}


def _summary_material_anlegen(ctx: Ctx, args: dict) -> str:
    return f"Material „{(args.get('name') or '—').strip()}\" im Katalog anlegen?"


# ---- READ -----------------------------------------------------------------

async def _run_archiv_suchen(ctx: Ctx, args: dict) -> dict:
    """Findet die Drive-Archiv-Ordner eines Kunden (spiegelt /archiv ohne
    Upload-Wizard: reine Suche + Link)."""
    from core.integrations.google_drive import list_tenant_kunde_drives

    name = (args.get("kunde_name") or "").strip()
    folders = await list_tenant_kunde_drives(ctx.tid, limit=500)
    if name:
        folders = [f for f in folders if name.lower() in (f.kunde_name or "").lower()]
    return {"anzahl": len(folders), "ordner": [
        {"kunde": f.kunde_name, "link": f.drive_folder_url,
         "dateien": f.upload_count,
         "letzter_upload": f.last_upload_at.isoformat() if f.last_upload_at else None}
        for f in folders[:15]]}


async def _run_archiv_dateien(ctx: Ctx, args: dict) -> dict:
    """Listet die Dateien im Drive-Archiv-Ordner eines Kunden — mit ID,
    damit Q eine davon als Mail-Anhang vorschlagen kann."""
    from core.integrations.google_drive import list_files_in_kunde_folder

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"error": "Bitte den Kundennamen nennen."}
    dateien = await list_files_in_kunde_folder(ctx.tid, name)
    return {"anzahl": len(dateien), "dateien": [
        {"datei_id": f["id"], "name": f["name"], "typ": f["mime_type"]}
        for f in dateien[:25]]}


async def _run_rechnungen_pruefen(ctx: Ctx, args: dict) -> dict:
    """Synchronisiert den Bezahl-Status offener Rechnungen mit Lexware
    (spiegelt /rechnung_pruefen). Kein Versand, nur Abgleich + Markierung."""
    from core.integrations.rechnung_payment_monitor import (
        check_pending_invoices_for_tenant)

    summary = await check_pending_invoices_for_tenant(ctx.tid)
    return {"geprueft": summary.get("checked", 0),
            "neu_als_bezahlt_markiert": summary.get("paid", 0),
            "unveraendert": summary.get("no_change", 0),
            "fehler": summary.get("errors", 0)}


async def _run_offene_posten(ctx: Ctx, args: dict) -> dict:
    """Wer schuldet uns noch Geld? Nutzt dieselbe Auswertung wie der
    Buchhaltungs-Bereich der App (core.services.buchhaltung), damit Q und
    Bildschirm nie unterschiedliche Zahlen nennen."""
    from core.services import buchhaltung as buch

    daten = await buch.uebersicht(ctx.tid)
    return {
        "zusammenfassung": buch.als_text(daten),
        "kennzahlen": daten["kennzahlen"],
        "zahlungsziel_tage": daten["zahlungsziel_tage"],
        "offene_posten": daten["offene_posten"][:15],
        "angebote_zum_nachfassen": daten["nachfassen"][:10],
    }


async def _run_formulare_status(ctx: Ctx, args: dict) -> dict:
    """Status der Kunden-Anfrage-Formulare der letzten 30 Tage
    (spiegelt /formulare-Überschrift: offen/ausgefüllt/abgelaufen)."""
    from core.integrations.anfrage_status import count_status_for_tenant

    counts = await count_status_for_tenant(ctx.tid)
    return {"letzte_30_tage": counts}


# ---- WRITE ----------------------------------------------------------------

async def _run_wissen_loeschen(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.tenant_knowledge import TenantKnowledge
    from sqlalchemy import select

    such = (args.get("suchtext") or "").strip()
    if len(such) < 3:
        return {"ok": False, "error": "Bitte einen Suchtext (min. 3 Zeichen) nennen."}
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == ctx.tid)
            .where(TenantKnowledge.text.ilike(f"%{such}%")).limit(3)
        )).scalars().all()
        if not rows:
            return {"ok": False, "error": f"Kein Wissens-Eintrag mit '{such}' gefunden."}
        if len(rows) > 1:
            return {"ok": False, "error": f"Mehrere Einträge passen auf '{such}' — bitte genauer."}
        entry = rows[0]
        geloescht = entry.text
        await s.delete(entry)
        await s.commit()
    return {"ok": True, "geloescht": geloescht[:140]}


def _summary_wissen_loeschen(ctx: Ctx, args: dict) -> str:
    return f"Wissens-Eintrag mit „{(args.get('suchtext') or '—').strip()}\" löschen?"


async def _run_wissensluecke_beantworten(ctx: Ctx, args: dict) -> dict:
    """Schliesst eine offene Kundenfrage: Antwort -> Wissens-Eintrag.

    Damit kann der Inhaber die Luecke im Vorbeigehen erledigen ("Q, zu
    der Vinyl-Frage: ja, verlegen wir, ab 35 Euro den Quadratmeter"),
    statt erst in die Wissens-Ansicht zu wechseln.
    """
    import datetime as _dt
    from sqlalchemy import select

    from core.database.connection import get_session
    from core.models.tenant_knowledge import (
        KATEGORIE_LABELS, QUELLE_MENSCH, SICHTBARKEIT_KUNDE, TenantKnowledge,
    )
    from core.models.wissensluecke import (
        STATUS_BEANTWORTET, STATUS_OFFEN, Wissensluecke,
    )
    from core.services import wissen as wissen_service

    such = (args.get("frage") or "").strip()
    antwort = (args.get("antwort") or "").strip()
    kategorie = (args.get("kategorie") or "faq").strip()
    if kategorie not in KATEGORIE_LABELS:
        kategorie = "faq"
    if not (3 <= len(antwort) <= 2000):
        return {"ok": False, "error": "Die Antwort muss 3–2000 Zeichen haben."}

    async with get_session() as s:
        offene = (await s.execute(
            select(Wissensluecke)
            .where(Wissensluecke.tenant_id == ctx.tid)
            .where(Wissensluecke.status == STATUS_OFFEN)
            .order_by(Wissensluecke.anzahl.desc())
            .limit(50)
        )).scalars().all()
        if not offene:
            return {"ok": False, "error": "Es sind keine offenen Fragen da."}

        if such:
            passend = [
                o for o in offene
                if wissen_service.aehnlichkeit(such, o.frage) >= 0.4
            ]
        else:
            # Ohne Suchtext nur eindeutig, wenn genau eine Frage offen ist.
            passend = offene if len(offene) == 1 else []
        if not passend:
            return {
                "ok": False,
                "error": "Ich finde die Frage nicht eindeutig. Offen sind: "
                         + "; ".join(o.frage[:80] for o in offene[:5]),
            }
        if len(passend) > 1:
            return {
                "ok": False,
                "error": "Mehrere Fragen passen: "
                         + "; ".join(o.frage[:80] for o in passend[:5]),
            }

        luecke = passend[0]
        jetzt = _dt.datetime.now(_dt.timezone.utc)
        eintrag = TenantKnowledge(
            tenant_id=ctx.tid, kategorie=kategorie, text=antwort,
            sichtbarkeit=SICHTBARKEIT_KUNDE, quelle=QUELLE_MENSCH,
            zuletzt_bestaetigt_am=jetzt,
        )
        s.add(eintrag)
        await s.flush()
        luecke.status = STATUS_BEANTWORTET
        luecke.erledigt_am = jetzt
        luecke.knowledge_id = eintrag.id
        await s.commit()
        return {"ok": True, "frage": luecke.frage, "antwort": antwort}


def _summary_wissensluecke_beantworten(ctx: Ctx, args: dict) -> str:
    f = (args.get("frage") or "die offene Frage").strip()
    return f"Die Frage „{f[:70]}\" mit deiner Antwort in die Wissensdatenbank aufnehmen?"


# ---- WRITE (Kundenzyklus / Beleg-Fluss) -----------------------------------
#
# Diese Tools rufen den geteilten Service core.services.document_flow, den
# auch die App-Routen nutzen. Senden geht an echte
# Kunden — daher (wie alle Write-Tools) erst nach Bestaetigung.

async def _run_angebot_erstellen(ctx: Ctx, args: dict) -> dict:
    from core.ai.gemini import extract_angebot_from_text
    from core.services.document_flow import create_angebot

    kunde = (args.get("kunde_name") or "").strip()
    besch = (args.get("beschreibung") or "").strip()
    if not kunde or len(besch) < 5:
        return {"ok": False, "error": "Bitte Kundenname und eine Beschreibung der Leistung nennen."}
    try:
        ex = await extract_angebot_from_text(besch, tenant_id=ctx.tid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("angebot_erstellen extract crash: %s", exc)
        return {"ok": False, "error": "Konnte die Leistung nicht in Positionen umwandeln."}
    positionen = ex.get("positionen") or []
    if not positionen:
        return {"ok": False, "error": "Keine Positionen erkannt — bitte konkreter (Leistung + Preis)."}
    return await create_angebot(
        ctx.tid, kunde_name=kunde, positionen=positionen,
        kunde_email=(args.get("kunde_email") or "").strip() or ex.get("kunde_email"),
        kunde_strasse=ex.get("kunde_strasse"), kunde_plz=ex.get("kunde_plz"),
        kunde_ort=ex.get("kunde_ort"), quelle="assistent",
        # Wer es per Q anlegt, bekommt den Auftrag zugewiesen — sonst
        # waere er "niemandem zugewiesen" und sofort unsichtbar.
        assigned_employee_id=ctx.employee.id)


def _summary_angebot_erstellen(ctx: Ctx, args: dict) -> str:
    return f"Angebot für {(args.get('kunde_name') or '—').strip()} aus deiner Beschreibung erstellen (Lexware-Entwurf)?"


async def _run_angebot_senden(ctx: Ctx, args: dict) -> dict:
    from core.services.document_flow import find_angebot_for_send, send_angebot

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    ang = await find_angebot_for_send(ctx.tid, name)
    if ang is None:
        return {"ok": False, "error": f"Kein versendbares Angebot für '{name}' gefunden."}
    if ang == "AMBIG":
        return {"ok": False, "error": f"Mehrere Angebote für '{name}' — bitte in der App senden."}
    return await send_angebot(
        ctx.tid, angebot_id=ang.id,
        to_email=(args.get("to_email") or "").strip() or None)


def _summary_angebot_senden(ctx: Ctx, args: dict) -> str:
    return f"Angebot von {(args.get('kunde_name') or '—').strip()} per Mail an den Kunden senden?"


async def _run_rechnung_erstellen(ctx: Ctx, args: dict) -> dict:
    from core.ai.gemini import extract_rechnung_from_text
    from core.services.document_flow import create_rechnung

    kunde = (args.get("kunde_name") or "").strip()
    besch = (args.get("beschreibung") or "").strip()
    if not kunde or len(besch) < 5:
        return {"ok": False, "error": "Bitte Kundenname und eine Beschreibung der Leistung nennen."}
    try:
        ex = await extract_rechnung_from_text(besch)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rechnung_erstellen extract crash: %s", exc)
        return {"ok": False, "error": "Konnte die Leistung nicht in Positionen umwandeln."}
    positionen = ex.get("positionen") or []
    if not positionen:
        return {"ok": False, "error": "Keine Positionen erkannt — bitte konkreter (Leistung + Preis)."}
    return await create_rechnung(
        ctx.tid, kunde_name=kunde, positionen=positionen,
        kunde_email=(args.get("kunde_email") or "").strip() or ex.get("kunde_email"),
        kunde_strasse=ex.get("kunde_strasse"), kunde_plz=ex.get("kunde_plz"),
        kunde_ort=ex.get("kunde_ort"), input_type="assistent")


def _summary_rechnung_erstellen(ctx: Ctx, args: dict) -> str:
    return f"Rechnung für {(args.get('kunde_name') or '—').strip()} erstellen (Lexware-Entwurf)?"


async def _run_rechnung_abrechnen(ctx: Ctx, args: dict) -> dict:
    from core.services.document_flow import (
        find_auftrag_for_invoice, finalize_and_send_invoice)

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    ang = await find_auftrag_for_invoice(ctx.tid, name)
    if ang is None:
        return {"ok": False, "error": (
            f"Kein fertiger Auftrag für '{name}' gefunden "
            "(der Auftrag muss auf 'fertig' stehen).")}
    if ang == "AMBIG":
        return {"ok": False, "error": f"Mehrere fertige Aufträge für '{name}' — bitte in der App abrechnen."}
    return await finalize_and_send_invoice(ctx.tid, angebot_id=ang.id)


def _summary_rechnung_abrechnen(ctx: Ctx, args: dict) -> str:
    return (f"Rechnung für den fertigen Auftrag von {(args.get('kunde_name') or '—').strip()} "
            "in Lexware finalisieren und per Mail an den Kunden senden?")


async def _run_anfrage_beantworten(ctx: Ctx, args: dict) -> dict:
    from core.services.document_flow import find_open_conversation, send_anfrage_reply

    name = (args.get("kunde_name") or "").strip()
    antwort = (args.get("antwort_text") or "").strip()
    if len(name) < 2 or len(antwort) < 2:
        return {"ok": False, "error": "Bitte Kunde und Antworttext nennen."}
    conv = await find_open_conversation(ctx.tid, name)
    if conv is None:
        return {"ok": False, "error": f"Keine offene Anfrage von '{name}' gefunden."}
    if conv == "AMBIG":
        return {"ok": False, "error": f"Mehrere offene Anfragen von '{name}' — bitte in der App antworten."}
    return await send_anfrage_reply(
        ctx.tid, conv_id=conv.id, reply_text=antwort,
        employee_id=getattr(ctx.employee, "id", None),
        close=bool(args.get("abschliessen")))


def _summary_anfrage_beantworten(ctx: Ctx, args: dict) -> str:
    a = (args.get("antwort_text") or "").strip()
    return (f"Antwort an {(args.get('kunde_name') or '—').strip()} senden: "
            f"„{a[:90]}{'…' if len(a) > 90 else ''}\"?")


# ---- WRITE (Freie E-Mail) -------------------------------------------------
#
# Sonderfall im Ablauf: statt der einzeiligen Bestaetigung liefert
# ``run_command`` fuer dieses Tool einen kompletten Entwurf zurueck
# (Empfaenger, Betreff, Text, Anhaenge). Die App zeigt ihn zum Redigieren
# an; abgeschickt wird er ueber denselben Bestaetigungs-Endpunkt wie jedes
# andere Write-Tool — mit den Werten, die der Nutzer am Ende freigibt.
# Fail-closed bleibt es damit: ohne Freigabe geht keine Mail raus.

async def _build_email_entwurf(ctx: Ctx, args: dict) -> dict:
    """Baut aus Geminis Vorschlag den Entwurf fuer die Entwurfs-Karte."""
    from core.services.mail_compose import (
        EMAIL_RE, MAX_BETREFF, MAX_TEXT, lookup_kunde_email, normalize_anhaenge)

    kunde = (args.get("kunde_name") or "").strip()
    empfaenger = (args.get("empfaenger") or "").strip()
    hinweis = None
    if not empfaenger and kunde:
        empfaenger = await lookup_kunde_email(ctx.tid, kunde) or ""
    if not empfaenger:
        hinweis = "Ich habe keine Adresse gefunden — bitte trag sie ein."
    elif not EMAIL_RE.match(empfaenger):
        hinweis = "Die Adresse sieht nicht vollständig aus — bitte prüfen."

    # Von Gemini vorgeschlagene Drive-Anhaenge: Namen nachschlagen, damit
    # in der Karte nicht nur eine kryptische Datei-ID steht.
    anhaenge = normalize_anhaenge(args.get("anhang_datei_ids") or args.get("anhaenge"))
    drive_ids = [a["id"] for a in anhaenge if a.get("quelle") == "drive" and not a.get("name")]
    if drive_ids and kunde:
        try:
            from core.integrations.google_drive import list_files_in_kunde_folder
            namen = {f["id"]: f["name"]
                     for f in await list_files_in_kunde_folder(ctx.tid, kunde)}
            for a in anhaenge:
                if a.get("quelle") == "drive" and not a.get("name"):
                    a["name"] = namen.get(a["id"], "Anhang")
        except Exception as exc:  # noqa: BLE001
            logger.warning("email_entwurf: Anhang-Namen nicht ladbar: %s", exc)

    return {
        "type": "email_entwurf",
        "tool": "email_schreiben",
        "empfaenger": empfaenger,
        "empfaenger_name": (args.get("empfaenger_name") or "").strip() or kunde,
        "betreff": (args.get("betreff") or "").strip()[:MAX_BETREFF],
        "text": (args.get("text") or "").strip()[:MAX_TEXT],
        "kunde_name": kunde,
        "anhaenge": [{"quelle": a.get("quelle"), "id": a.get("id"),
                      "name": a.get("name") or "Anhang"}
                     for a in anhaenge if a.get("quelle") == "drive"],
        "hinweis": hinweis,
    }


async def _run_email_senden(ctx: Ctx, args: dict) -> dict:
    from core.services.mail_compose import normalize_anhaenge, send_freie_mail

    anhaenge = normalize_anhaenge(args.get("anhaenge") or args.get("anhang_datei_ids"))
    return await send_freie_mail(
        ctx.tid,
        to_email=(args.get("empfaenger") or "").strip(),
        to_name=(args.get("empfaenger_name") or "").strip() or None,
        betreff=(args.get("betreff") or "").strip(),
        text=(args.get("text") or "").strip(),
        anhaenge=anhaenge,
        employee_id=getattr(ctx.employee, "id", None))


def _summary_email(ctx: Ctx, args: dict) -> str:
    an = (args.get("empfaenger") or args.get("empfaenger_name") or "—").strip()
    betreff = (args.get("betreff") or "").strip()
    return f"E-Mail an {an} senden: „{betreff[:70]}{'…' if len(betreff) > 70 else ''}\"?"


# ---- WRITE (Drive-Archiv) -------------------------------------------------
#
# Schreiben in Google Drive: Kunden-Ordner anlegen und Text-Notizen ablegen.
# Nutzt dieselben Primitive wie der /archiv-Upload-Flow der App
# (get_or_create_kunde_folder / upload_file_to_kunde_folder) — keine eigene
# Drive-API-Logik. Datei-Uploads laufen weiter ueber den 📎-Button (dort
# liegen die Bytes vor); per Sprach-/Textbefehl geht das Ablegen von Notizen.

async def _run_drive_ordner_anlegen(ctx: Ctx, args: dict) -> dict:
    from core.integrations.google_drive import get_or_create_kunde_folder

    name = (args.get("kunde_name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    try:
        _folder_id, folder_url = await get_or_create_kunde_folder(
            ctx.tid, name, getattr(ctx.employee, "id", None),
            kunde_email=(args.get("kunde_email") or "").strip() or None,
            kunde_telefon=(args.get("kunde_telefon") or "").strip() or None)
    except ValueError:
        return {"ok": False, "error": "Google Drive ist nicht verbunden."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("drive_ordner_anlegen crash: %s", exc)
        return {"ok": False, "error": "Ordner konnte nicht angelegt werden."}
    return {"ok": True, "kunde": name, "link": folder_url}


def _summary_drive_ordner(ctx: Ctx, args: dict) -> str:
    return f"Drive-Ordner für {(args.get('kunde_name') or '—').strip()} anlegen?"


async def _run_drive_notiz_anlegen(ctx: Ctx, args: dict) -> dict:
    import re
    from core.integrations.google_drive import upload_file_to_kunde_folder

    name = (args.get("kunde_name") or "").strip()
    text = (args.get("text") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "Bitte den Kundennamen nennen."}
    if len(text) < 2:
        return {"ok": False, "error": "Bitte den Notiz-Text nennen."}
    titel = (args.get("titel") or "").strip()
    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    body = f"Notiz für {name}\n{stamp}\n\n{text}\n"
    safe = re.sub(r"[^\w\- ]", "", titel)[:60].strip() if titel else ""
    filename = f"{safe or 'Notiz-' + dt.datetime.now().strftime('%Y%m%d-%H%M')}.txt"
    try:
        res = await upload_file_to_kunde_folder(
            ctx.tid, name, body.encode("utf-8"), filename, "text/plain",
            getattr(ctx.employee, "id", None),
            kunde_email=(args.get("kunde_email") or "").strip() or None,
            kunde_telefon=(args.get("kunde_telefon") or "").strip() or None)
    except ValueError:
        return {"ok": False, "error": "Google Drive ist nicht verbunden."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("drive_notiz_anlegen crash: %s", exc)
        return {"ok": False, "error": "Notiz konnte nicht abgelegt werden."}
    return {"ok": True, "kunde": name, "datei": filename,
            "link": (res or {}).get("kunde_folder_url")}


def _summary_drive_notiz(ctx: Ctx, args: dict) -> str:
    t = (args.get("text") or "").strip()
    return (f"Notiz im Drive-Ordner von {(args.get('kunde_name') or '—').strip()} "
            f"ablegen: „{t[:70]}{'…' if len(t) > 70 else ''}\"?")


# ---- WRITE (Termin verschieben) -------------------------------------------
#
# Es gibt KEINE native Reschedule-Funktion im Kalender-Plugin. Wir komponieren
# aus den vorhandenen Primitiven: zuerst den NEUEN Termin buchen (damit bei
# einem Buchungsfehler der alte erhalten bleibt), dann den alten stornieren.
# Bewusst OHNE Storno-Mail an den Kunden (es ist eine Verschiebung, keine
# Absage). Details (Anliegen/Adresse, Dauer) werden vom alten Event uebernommen.

async def _run_termin_verschieben(ctx: Ctx, args: dict) -> dict:
    from core.database.connection import get_session
    from core.models.kundengespraech import Kundengespraech
    from sqlalchemy import select

    name = (args.get("kunde_name") or "").strip()
    neues_datum = (args.get("neues_datum") or "").strip()
    neue_uhrzeit = (args.get("neue_uhrzeit") or "").strip()
    if len(name) < 2 or not neues_datum or not neue_uhrzeit:
        return {"ok": False, "error": "Bitte Kundenname, neues Datum und neue Uhrzeit nennen."}
    try:
        neu_dt = dt.datetime.strptime(f"{neues_datum} {neue_uhrzeit}", "%d.%m.%Y %H:%M")
    except ValueError:
        return {"ok": False, "error": "Datum/Uhrzeit ungültig (Datum TT.MM.JJJJ, Uhrzeit HH:MM)."}

    heute = dt.date.today()
    async with get_session() as s:
        treffer = (await s.execute(
            select(Kundengespraech)
            .where(Kundengespraech.tenant_id == ctx.tid)
            .where(Kundengespraech.kunde_name.ilike(f"%{name}%"))
            .where(Kundengespraech.termin_datum.is_not(None))
            .where(Kundengespraech.termin_datum >= heute)
            .order_by(Kundengespraech.termin_datum.asc())
        )).scalars().all()
    if len(treffer) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger anstehender Termin für '{name}' gefunden "
            f"({len(treffer)} Treffer). Bitte im Kalender direkt verschieben.")}

    k = treffer[0]
    kalender = await _get_kalender(ctx)
    if kalender is None:
        return {"ok": False, "error": "Kalender nicht eingerichtet."}
    tmin = (k.termin_datum - dt.timedelta(days=1)).isoformat()
    tmax = (k.termin_datum + dt.timedelta(days=1)).isoformat()
    found = await kalender.on_webhook("find_events", {
        "kunde_name": k.kunde_name, "time_min": tmin, "time_max": tmax})
    termine = (found or {}).get("termine") or []
    if len(termine) != 1:
        return {"ok": False, "error": (
            f"Kein eindeutiger Kalender-Termin gefunden ({len(termine)} Treffer). "
            "Bitte im Kalender direkt verschieben.")}
    match = termine[0]
    old_event_id = match.get("event_id")

    # Dauer aus args, sonst aus dem alten Event ableiten, sonst 60.
    dauer = None
    if args.get("dauer_minuten"):
        try:
            dauer = int(args["dauer_minuten"])
        except (TypeError, ValueError):
            dauer = None
    if dauer is None:
        try:
            sdt = dt.datetime.fromisoformat(match["start_dt"])
            edt = dt.datetime.fromisoformat(match["end_dt"])
            dauer = max(15, int((edt - sdt).total_seconds() // 60))
        except Exception:  # noqa: BLE001
            dauer = 60

    # 1) Neuen Termin zuerst buchen (alter bleibt erhalten, falls das scheitert).
    book_payload = {
        "name": k.kunde_name, "datum": neues_datum, "uhrzeit": neue_uhrzeit,
        "dauer_minuten": dauer,
        "anliegen": (match.get("summary") or "").strip() or None,
        "adresse": (match.get("location") or "").strip() or None,
    }
    res_book = await kalender.on_webhook("book_appointment", book_payload)
    if (res_book or {}).get("error") or not (res_book or {}).get("event_id"):
        return {"ok": False, "error": (
            (res_book or {}).get("error")
            or "Neuer Termin konnte nicht gebucht werden — der alte bleibt bestehen.")}

    # 2) Alten Termin stornieren (ohne Storno-Mail — es ist eine Verschiebung).
    cancel_payload: dict = {"event_id": old_event_id}
    if match.get("employee_id"):
        try:
            cancel_payload["employee_id"] = uuid.UUID(match["employee_id"])
        except (ValueError, TypeError):
            pass
    res_cancel = await kalender.on_webhook("cancel_appointment", cancel_payload)
    cancel_ok = bool((res_cancel or {}).get("erfolg"))

    # 3) Termin-Datum im Kundengespräch nachziehen.
    alt_iso = k.termin_datum.isoformat()
    try:
        async with get_session() as s:
            obj = await s.get(Kundengespraech, k.id)
            if obj is not None:
                obj.termin_datum = neu_dt
                await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("termin_verschieben: Gespräch-Update fehlgeschlagen: %s", exc)

    return {"ok": True, "kunde": k.kunde_name, "alt": alt_iso,
            "neu": neu_dt.isoformat(), "alter_termin_entfernt": cancel_ok}


def _summary_termin_verschieben(ctx: Ctx, args: dict) -> str:
    return (f"Termin von {(args.get('kunde_name') or '—').strip()} auf "
            f"{(args.get('neues_datum') or '?').strip()} um "
            f"{(args.get('neue_uhrzeit') or '?').strip()} Uhr verschieben?")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

async def _run_anzeige_oeffnen(ctx: Ctx, args: dict) -> dict:
    # Wird in run_command kurzgeschlossen (liefert {"type":"navigate"} an die
    # App); diese Funktion ist nur der ToolSpec-Platzhalter.
    return {"bereich": (args.get("bereich") or "aktuelles")}


_S = "STRING"
_I = "INTEGER"

_REGISTRY: list[ToolSpec] = [
    # ---- READ ----
    ToolSpec(
        name="freie_termine_finden", kind="read", feature="kalender",
        description="Sucht freie Termin-Slots in den nächsten Tagen im Kalender. "
                    "Vor einer Buchung aufrufen, um einen passenden Slot zu finden.",
        parameters={"type": "OBJECT", "properties": {
            "tage": {"type": _I, "description": "Wie viele Tage vorausschauen (Standard 7, max 30)."}}},
        run=_run_freie_slots),
    ToolSpec(
        name="kunde_suchen", kind="read",
        description="Sucht einen Kunden anhand des Namens und liefert seine "
                    "letzten Gespräche/Termine sowie die Anzahl Angebote/Rechnungen.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Kundenname oder Teil davon."}},
            "required": ["name"]},
        run=_run_kunde_suchen),
    ToolSpec(
        name="material_liste", kind="read", feature="material",
        description="Listet das hinterlegte Material des Betriebs (mit IDs). "
                    "Vor einer Bestellung aufrufen, um die richtige material_id zu finden.",
        parameters={"type": "OBJECT", "properties": {
            "suche": {"type": _S, "description": "Optionaler Namensfilter."}}},
        run=_run_material_liste),
    ToolSpec(
        name="material_bestellungen", kind="read", feature="material",
        description="Zeigt die zuletzt ausgelösten Material-Bestellungen des "
                    "Betriebs (was wurde wann in welcher Menge bestellt).",
        parameters={"type": "OBJECT", "properties": {
            "anzahl": {"type": _I,
                       "description": "Wie viele Einträge (Standard 10, max 25)."}}},
        run=_run_material_bestellungen),
    ToolSpec(
        name="offene_rueckrufe", kind="read",
        description="Zeigt die aktuell offenen Rückrufe.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_offene_rueckrufe),
    ToolSpec(
        name="anzeige_oeffnen", kind="read",
        description="Öffnet eine Ansicht/Liste in der App, wenn der Nutzer sie "
                    "SEHEN will (z.B. 'zeig mir die Rechnungen', 'öffne die "
                    "Termine', 'meine offenen Rückrufe', 'Kundenliste', 'geh zu "
                    "Material'). Für ein konkretes Kundenprofil: bereich='kunden_profil' "
                    "und kunde_name=<Name>. Für Ablage-Kategorien eines Kunden: "
                    "bereich='kunden_archiv', kunde_name=<Name>, kategorie=bilder|pdfs|notizen. "
                    "Nur zum Anzeigen/Navigieren — keine Daten ändern.",
        parameters={"type": "OBJECT", "properties": {
            "bereich": {"type": _S, "description":
                "Genau einer von: aktuelles, anfragen, termine, auftraege, "
                "rueckrufe, aufnahmen, buchhaltung, angebote, rechnungen, belege, "
                "kunden, kunden_profil, "
                "kunden_archiv, wissen, material, team, einstellungen. "
                "'buchhaltung' = der Geld-Bereich mit offenen Posten, Rechnungen, "
                "Angeboten und Belegen (nimm den, wenn der Wunsch allgemein ist), "
                "'belege' = Beleg/Quittung erfassen. "
                "'rueckrufe' = offene Rueckrufbitten vom Telefon-Agenten (To-do-"
                "Liste), 'aufnahmen' = aufgezeichnete Kundengespraeche/Diktate."},
            "kunde_name": {"type": _S, "description":
                "Nur bei bereich='kunden_profil' oder 'kunden_archiv': exakter Kundenname."},
            "kategorie": {"type": _S, "description":
                "Nur bei bereich='kunden_archiv': bilder, pdfs oder notizen."}},
            "required": ["bereich"]},
        run=_run_anzeige_oeffnen),

    # ---- WRITE ----
    ToolSpec(
        name="termin_anlegen", kind="write", feature="kalender",
        description="Legt einen Termin im Kalender an. Nur mit konkretem Datum "
                    "(TT.MM.JJJJ) und Uhrzeit (HH:MM) aufrufen.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Name des Kunden."},
            "datum": {"type": _S, "description": "Datum TT.MM.JJJJ."},
            "uhrzeit": {"type": _S, "description": "Uhrzeit HH:MM."},
            "dauer_minuten": {"type": _I, "description": "Dauer in Minuten (Standard 60)."},
            "anliegen": {"type": _S, "description": "Worum geht es (z.B. Heizungswartung)."},
            "adresse": {"type": _S}, "telefon": {"type": _S},
            "kunde_email": {"type": _S},
            "mitarbeiter": {"type": _S, "description":
                "Wer den Termin uebernimmt (Name oder Kuerzel). Weglassen, "
                "wenn der Nutzer selbst hingeht."}},
            "required": ["name", "datum", "uhrzeit"]},
        run=_run_termin_anlegen, summarize=_summary_termin),
    ToolSpec(
        name="termin_stornieren", kind="write", feature="kalender",
        description="Storniert den anstehenden Termin eines Kunden (nur wenn "
                    "eindeutig genau ein Termin gefunden wird).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."}},
            "required": ["kunde_name"]},
        run=_run_termin_stornieren, summarize=_summary_storno),
    ToolSpec(
        name="rueckruf_anlegen", kind="write",
        description="Legt einen Rückruf an, damit der Kunde zurückgerufen wird.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S}, "kunde_telefon": {"type": _S},
            "anliegen": {"type": _S, "description": "Worum geht es."},
            "kunde_email": {"type": _S}},
            "required": ["kunde_name", "kunde_telefon"]},
        run=_run_rueckruf_anlegen, summarize=_summary_rueckruf),
    ToolSpec(
        name="material_bestellen", kind="write", feature="material",
        description="Bestellt Material aus dem Katalog (per material_id aus "
                    "material_liste, oder eindeutigem Namen).",
        parameters={"type": "OBJECT", "properties": {
            "material_id": {"type": _S, "description": "ID aus material_liste."},
            "name": {"type": _S, "description": "Alternativ: eindeutiger Material-Name."},
            "menge": {"type": _I, "description": "Bestellmenge (Standard = Standardmenge)."}}},
        run=_run_material_bestellen, summarize=_summary_material),
    ToolSpec(
        name="abwesenheit_melden", kind="write", permission="team.fuehren",
        description="Meldet einen Mitarbeiter krank, in Urlaub oder sonst "
                    "abwesend. Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "mitarbeiter": {"type": _S, "description": "Name oder Kürzel des Mitarbeiters."},
            "typ": {"type": _S, "description": "krank | urlaub | sonstiges.",
                    "enum": ["krank", "urlaub", "sonstiges"]},
            "start": {"type": _S, "description": "Start YYYY-MM-DD (Standard heute)."},
            "ende": {"type": _S, "description": "Ende YYYY-MM-DD (leer = offen)."},
            "notes": {"type": _S}},
            "required": ["mitarbeiter", "typ"]},
        run=_run_abwesenheit, summarize=_summary_abwesenheit),

    # ---- READ (Erweiterung) ----
    ToolSpec(
        name="anstehende_termine", kind="read",
        description="Zeigt die anstehenden Termine der nächsten Tage "
                    "(aus den erfassten Kundengesprächen).",
        parameters={"type": "OBJECT", "properties": {
            "tage": {"type": _I, "description": "Vorausschau in Tagen (Standard 14, max 60)."}}},
        run=_run_anstehende_termine),
    ToolSpec(
        name="team_status", kind="read", permission="team.sehen",
        description="Zeigt das Team: wer heute abwesend (krank/Urlaub) ist und "
                    "welche Abwesenheiten anstehen.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_team_status),
    ToolSpec(
        name="offene_anfragen", kind="read", feature="mail_intake",
        description="Zeigt die offenen Kundenanfragen (E-Mail-Eingang), die noch "
                    "nicht abgeschlossen sind.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_offene_anfragen),
    ToolSpec(
        name="wissen_suchen", kind="read",
        description="Durchsucht die Wissensdatenbank des Betriebs (Preise, "
                    "Leistungen, Anfahrt, Öffnungszeiten, Besonderheiten …) und "
                    "liefert passende Einträge, um eine Frage zu beantworten.",
        parameters={"type": "OBJECT", "properties": {
            "frage": {"type": _S, "description": "Suchbegriff/Stichwort (optional — leer = alles)."}}},
        run=_run_wissen_suchen),
    ToolSpec(
        name="wissensluecken", kind="read",
        description="Zeigt Fragen, die Kunden am Telefon oder per Mail gestellt "
                    "haben und die Q nicht beantworten konnte, weil nichts in der "
                    "Wissensdatenbank stand. Häufigste zuerst.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_wissensluecken),
    ToolSpec(
        name="ueberschlag", kind="read",
        description="Rechnet einen Preis-Richtwert nach einer hinterlegten Formel "
                    "des Betriebs (z.B. 'Wand streichen' mit qm). Rechnet "
                    "deterministisch — nie selbst schätzen. Fehlen Angaben, sagt "
                    "das Ergebnis welche.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Name der Formel, z.B. 'Wand streichen'."},
            "werte": {"type": "OBJECT", "description":
                      "Die Werte der Variablen als Objekt, z.B. {\"qm\": 30}."}},
            "required": ["name"]},
        run=_run_ueberschlag),

    # ---- WRITE (Erweiterung) ----
    ToolSpec(
        name="wissen_merken", kind="write", permission="wissen.pflegen",
        description="Speichert eine Information dauerhaft in der "
                    "Wissensdatenbank (z.B. Preis, Regel, Besonderheit).",
        parameters={"type": "OBJECT", "properties": {
            "text": {"type": _S, "description": "Der zu merkende Text."},
            "kategorie": {"type": _S,
                          "description": "leistungen | materialien | preise | anfahrt | "
                                         "oeffnungszeiten | notfall | besonderheiten | faq.",
                          "enum": ["leistungen", "materialien", "preise", "anfahrt",
                                   "oeffnungszeiten", "notfall", "besonderheiten", "faq"]},
            "sichtbarkeit": {"type": _S,
                             "description": "'kunde' (Standard) = Q darf es Kunden am "
                                            "Telefon und per Mail sagen. 'intern' = nur "
                                            "der Betrieb sieht es (Einkaufspreise, Margen, "
                                            "Notizen über Kunden). Im Zweifel 'kunde'.",
                             "enum": ["kunde", "intern"]}},
            "required": ["text"]},
        run=_run_wissen_merken, summarize=_summary_wissen),
    ToolSpec(
        name="wissensluecke_beantworten", kind="write", permission="wissen.pflegen",
        description="Beantwortet eine offene Kundenfrage aus der Lücken-Liste: legt "
                    "die Antwort als Wissens-Eintrag an und hakt die Frage ab.",
        parameters={"type": "OBJECT", "properties": {
            "frage": {"type": _S, "description":
                      "Stichwort der offenen Frage (leer, wenn nur eine offen ist)."},
            "antwort": {"type": _S, "description":
                        "Die Antwort, so wie Q sie künftig Kunden sagen soll."},
            "kategorie": {"type": _S,
                          "description": "leistungen | materialien | preise | anfahrt | "
                                         "oeffnungszeiten | notfall | besonderheiten | faq.",
                          "enum": ["leistungen", "materialien", "preise", "anfahrt",
                                   "oeffnungszeiten", "notfall", "besonderheiten", "faq"]}},
            "required": ["antwort"]},
        run=_run_wissensluecke_beantworten,
        summarize=_summary_wissensluecke_beantworten),
    ToolSpec(
        name="rueckruf_erledigt", kind="write",
        description="Hakt den offenen Rückruf eines Kunden als erledigt ab "
                    "(nur bei genau einem eindeutigen offenen Rückruf).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."}},
            "required": ["kunde_name"]},
        run=_run_rueckruf_erledigt, summarize=_summary_rueckruf_erledigt),
    ToolSpec(
        name="mitarbeiter_zurueck", kind="write", permission="team.fuehren",
        description="Meldet einen Mitarbeiter wieder verfügbar (beendet seine "
                    "laufende Abwesenheit). Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "mitarbeiter": {"type": _S, "description": "Name oder Kürzel des Mitarbeiters."}},
            "required": ["mitarbeiter"]},
        run=_run_mitarbeiter_zurueck, summarize=_summary_mitarbeiter_zurueck),
    ToolSpec(
        name="auftrag_status", kind="write", permission="auftraege.fuehren", feature="lexware",
        description="Setzt den Status eines laufenden Auftrags (per Kundenname). "
                    "Mögliche Stufen: accepted (angenommen), arbeit_laeuft, "
                    "arbeit_fertig, abgebrochen. Der Rechnungsversand läuft separat.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "status": {"type": _S, "description": "Neuer Status.",
                       "enum": ["accepted", "arbeit_laeuft", "arbeit_fertig", "abgebrochen"]}},
            "required": ["kunde_name", "status"]},
        run=_run_auftrag_status, summarize=_summary_auftrag_status),
    ToolSpec(
        name="material_anlegen", kind="write", permission="material.verwalten",
        feature="material",
        description="Legt einen neuen Material-Eintrag im Bestell-Katalog an "
                    "(braucht Name und Bestell-Link). Nur für den Inhaber.",
        parameters={"type": "OBJECT", "properties": {
            "name": {"type": _S, "description": "Material-Name."},
            "bestell_link": {"type": _S, "description": "URL zum Bestellen."},
            "lieferant": {"type": _S}, "einheit": {"type": _S, "description": "z.B. Stück, Meter, kg."},
            "standard_menge": {"type": _I}, "notes": {"type": _S}},
            "required": ["name", "bestell_link"]},
        run=_run_material_anlegen, summarize=_summary_material_anlegen),

    # ---- READ (weitere Nachschlage-Tools) ----
    ToolSpec(
        name="archiv_suchen", kind="read", feature="drive_archiv",
        description="Findet den Drive-Archiv-Ordner eines Kunden (mit Link und "
                    "Anzahl Dateien). Ohne Namen: zuletzt genutzte Ordner.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Kundenname (optional)."}}},
        run=_run_archiv_suchen),
    ToolSpec(
        name="rechnungen_pruefen", kind="read", permission="buchhaltung.sehen", feature="lexware",
        description="Gleicht den Bezahl-Status offener Rechnungen mit Lexware ab "
                    "und markiert bezahlte. Verschickt nichts.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_rechnungen_pruefen),
    ToolSpec(
        name="offene_posten", kind="read", permission="buchhaltung.sehen", feature="lexware",
        description="Zeigt, welches Geld noch aussteht: unbezahlte und "
                    "überfällige Rechnungen mit Summe, Rechnungs-Entwürfe die "
                    "noch nicht raus sind, und versendete Angebote ohne "
                    "Rückmeldung. Für Fragen wie 'wer schuldet mir noch was', "
                    "'wie viel ist offen', 'welche Rechnung ist überfällig', "
                    "'wo muss ich nachfassen'. Fragt Lexware NICHT neu ab — "
                    "dafür ist rechnungen_pruefen da.",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_offene_posten),
    ToolSpec(
        name="archiv_dateien", kind="read", feature="drive_archiv",
        description="Listet die Dateien im Drive-Archiv-Ordner eines Kunden "
                    "mit ihrer datei_id. Vor dem Anhaengen einer Archiv-Datei "
                    "an eine E-Mail aufrufen, um die richtige datei_id zu finden.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."}},
            "required": ["kunde_name"]},
        run=_run_archiv_dateien),
    ToolSpec(
        name="formulare_status", kind="read", feature="anfrage_formular",
        description="Zeigt den Status der Kunden-Anfrage-Formulare der letzten "
                    "30 Tage (offen / ausgefüllt / abgelaufen).",
        parameters={"type": "OBJECT", "properties": {}},
        run=_run_formulare_status),

    # ---- WRITE (weitere Aktionen) ----
    ToolSpec(
        name="wissen_loeschen", kind="write", permission="wissen.pflegen",
        description="Löscht einen Eintrag aus der Wissensdatenbank (per "
                    "Suchtext, nur bei eindeutigem Treffer).",
        parameters={"type": "OBJECT", "properties": {
            "suchtext": {"type": _S, "description": "Teil des zu löschenden Eintrags."}},
            "required": ["suchtext"]},
        run=_run_wissen_loeschen, summarize=_summary_wissen_loeschen),

    # ---- WRITE (Kundenzyklus / Beleg-Fluss) ----
    ToolSpec(
        name="angebot_erstellen", kind="write", permission="buchhaltung.fuehren", feature="lexware",
        description="Erstellt ein Angebot für einen Kunden aus einer freien "
                    "Beschreibung der Leistung (KI wandelt sie in Positionen um) "
                    "und legt einen Lexware-Entwurf an. Versendet noch nichts.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "beschreibung": {"type": _S, "description": "Was angeboten wird, inkl. Mengen/Preise."},
            "kunde_email": {"type": _S, "description": "E-Mail des Kunden (optional)."}},
            "required": ["kunde_name", "beschreibung"]},
        run=_run_angebot_erstellen, summarize=_summary_angebot_erstellen),
    ToolSpec(
        name="angebot_senden", kind="write", permission="buchhaltung.fuehren", feature="lexware",
        description="Verschickt ein bereits erstelltes Angebot per Mail an den "
                    "Kunden (findet das jüngste offene Angebot des Kunden).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "to_email": {"type": _S, "description": "Abweichende Empfänger-Mail (optional)."}},
            "required": ["kunde_name"]},
        run=_run_angebot_senden, summarize=_summary_angebot_senden),
    ToolSpec(
        name="rechnung_erstellen", kind="write", permission="buchhaltung.fuehren", feature="lexware",
        description="Erstellt eine Rechnung für einen Kunden aus einer freien "
                    "Beschreibung der Leistung (Lexware-Entwurf). Versendet noch nichts.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "beschreibung": {"type": _S, "description": "Erbrachte Leistung inkl. Beträgen."},
            "kunde_email": {"type": _S, "description": "E-Mail des Kunden (optional)."}},
            "required": ["kunde_name", "beschreibung"]},
        run=_run_rechnung_erstellen, summarize=_summary_rechnung_erstellen),
    ToolSpec(
        name="rechnung_abrechnen", kind="write", permission="buchhaltung.fuehren", feature="lexware",
        description="Schliesst einen fertigen Auftrag ab: finalisiert die Rechnung "
                    "in Lexware und schickt sie als PDF per Mail an den Kunden. "
                    "Nur wenn der Auftrag auf 'fertig' steht.",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden des fertigen Auftrags."}},
            "required": ["kunde_name"]},
        run=_run_rechnung_abrechnen, summarize=_summary_rechnung_abrechnen),
    ToolSpec(
        name="anfrage_beantworten", kind="write", feature="mail_intake",
        permission="anfragen.bearbeiten",
        description="Beantwortet eine offene Kundenanfrage per Mail mit dem "
                    "angegebenen Text (RFC-gethreaded).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name oder Mail des anfragenden Kunden."},
            "antwort_text": {"type": _S, "description": "Der zu sendende Antworttext."},
            "abschliessen": {"type": "BOOLEAN", "description": "Anfrage danach als erledigt schliessen?"}},
            "required": ["kunde_name", "antwort_text"]},
        run=_run_anfrage_beantworten, summarize=_summary_anfrage_beantworten),
    ToolSpec(
        name="email_schreiben", kind="write",
        description="Verfasst eine frei formulierte E-Mail (Empfänger, Betreff, "
                    "Text, optional Anhänge) und legt sie dem Nutzer als Entwurf "
                    "zur Freigabe vor. VERSCHICKT NICHTS — der Aufruf öffnet nur "
                    "den Entwurf, den der Nutzer in der App liest, ändert und "
                    "freigibt. Darum immer direkt aufrufen und vorher NIE um "
                    "Erlaubnis fragen oder den Mail-Inhalt als Text ankündigen. "
                    "Für jede Mail, die keine Antwort auf eine offene Anfrage und "
                    "kein Angebots-/Rechnungsversand ist. Betreff und Text IMMER "
                    "vollständig ausformulieren (Anrede, Inhalt, Grußformel) — "
                    "der Nutzer redigiert nur noch.",
        parameters={"type": "OBJECT", "properties": {
            "empfaenger": {"type": _S, "description":
                "E-Mail-Adresse des Empfängers. Unbekannt? Erst kunde_suchen "
                "aufrufen; findet sich nichts, das Feld leer lassen."},
            "empfaenger_name": {"type": _S, "description": "Name des Empfängers."},
            "betreff": {"type": _S, "description": "Betreffzeile."},
            "text": {"type": _S, "description":
                "Der komplette Mail-Text inkl. Anrede und Grußformel."},
            "kunde_name": {"type": _S, "description":
                "Kunde, um den es geht (für Adress-Suche und Archiv-Anhänge)."},
            "anhang_datei_ids": {"type": "ARRAY", "items": {"type": _S},
                                 "description":
                "Optionale datei_id-Werte aus archiv_dateien, die angehängt werden sollen."}},
            "required": ["betreff", "text"]},
        run=_run_email_senden, summarize=_summary_email),
    ToolSpec(
        name="termin_verschieben", kind="write", feature="kalender",
        description="Verschiebt den anstehenden Termin eines Kunden auf ein "
                    "neues Datum/eine neue Uhrzeit. Bucht den neuen Termin und "
                    "storniert den alten (ohne Storno-Mail an den Kunden).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden, dessen Termin verschoben wird."},
            "neues_datum": {"type": _S, "description": "Neues Datum im Format TT.MM.JJJJ."},
            "neue_uhrzeit": {"type": _S, "description": "Neue Uhrzeit im Format HH:MM."},
            "dauer_minuten": {"type": _I, "description": "Dauer in Minuten (optional; sonst wie beim alten Termin)."}},
            "required": ["kunde_name", "neues_datum", "neue_uhrzeit"]},
        run=_run_termin_verschieben, summarize=_summary_termin_verschieben),
    ToolSpec(
        name="drive_ordner_anlegen", kind="write", feature="drive_archiv",
        description="Legt im Google Drive einen Archiv-Ordner für einen Kunden "
                    "an (oder gibt den bestehenden Ordner-Link zurück).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "kunde_email": {"type": _S, "description": "E-Mail des Kunden (optional, für stabile Zuordnung)."},
            "kunde_telefon": {"type": _S, "description": "Telefon des Kunden (optional)."}},
            "required": ["kunde_name"]},
        run=_run_drive_ordner_anlegen, summarize=_summary_drive_ordner),
    ToolSpec(
        name="drive_notiz_anlegen", kind="write", feature="drive_archiv",
        description="Legt eine Text-Notiz als Datei im Drive-Archiv-Ordner "
                    "eines Kunden ab (z.B. Gesprächsnotiz, Aufmaß, Absprache).",
        parameters={"type": "OBJECT", "properties": {
            "kunde_name": {"type": _S, "description": "Name des Kunden."},
            "text": {"type": _S, "description": "Der Notiz-Inhalt."},
            "titel": {"type": _S, "description": "Optionaler Titel/Dateiname der Notiz."},
            "kunde_email": {"type": _S, "description": "E-Mail des Kunden (optional)."},
            "kunde_telefon": {"type": _S, "description": "Telefon des Kunden (optional)."}},
            "required": ["kunde_name", "text"]},
        run=_run_drive_notiz_anlegen, summarize=_summary_drive_notiz),
]
