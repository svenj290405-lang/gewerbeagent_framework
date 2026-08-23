"""Ein Ort fuer alles, was mit der Wissensbasis zu tun hat.

Vorher lag die Lade-Logik dreimal dupliziert im Code — im Voice-Plugin
(``_load_knowledge`` + ``_build_knowledge_block``), in der Mail-Pipeline
(``microsoft_inbox``) und in Qs Tool ``wissen_suchen``. Drei Kopien mit
drei verschiedenen Verhalten: die Mail-Variante las jahrelang das falsche
Feld, schnitt bei 3000 Zeichen mitten im Satz ab und kannte keine Filter.

Dieses Modul ist jetzt die einzige Quelle. Es liefert:

  * ``lade(...)``        — Eintraege mit Sichtbarkeits-/Aktiv-Filter
  * ``suche(...)``       — nach Relevanz sortiert (Trigramm + Tokens)
  * ``baue_block(...)``  — fertiger Prompt-Block, an Eintragsgrenzen gekuerzt
  * ``merke_luecke(...)``— unbeantwortete Kundenfrage festhalten
  * ``pruefe_qualitaet(...)`` — Widersprueche + veraltete Preise

Zwei Dinge, die hier bewusst zusammenlaufen:

1. **Preise haben nur eine Wahrheit.** ``tenant_leistungen`` (die Tabelle,
   aus der das Angebot rechnet) wird als virtuelle Kategorie in denselben
   Block gespiegelt. Vorher stand der Stundensatz als Freitext in der
   Wissensbasis *und* strukturiert im Leistungskatalog — Q sagte am
   Telefon die eine Zahl, das Angebot rechnete mit der anderen.

2. **Die Sichtbarkeitsgrenze.** ``nur_kunde=True`` ist der Default fuer
   alles, was den Betrieb verlaesst (Voice-Prompt an ElevenLabs,
   Kundenmails). Interne Eintraege sieht nur die App.

Relevanz-Ranking laeuft absichtlich in Python, nicht ueber pg_trgm: bei
5-100 Snippets pro Tenant ist das mikrosekundenschnell, deterministisch,
ohne DB-Extension testbar — und es kann nicht dadurch ausfallen, dass auf
einer Instanz eine Extension fehlt.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.database.connection import get_session
from core.models.tenant_knowledge import (
    ALLE_KATEGORIEN,
    FRISCHE_KATEGORIEN,
    FRISCHE_TAGE,
    KATEGORIE_ANFAHRT,
    KATEGORIE_BESONDERHEITEN,
    KATEGORIE_LABELS,
    KATEGORIE_LEISTUNGEN,
    KATEGORIE_MATERIALIEN,
    KATEGORIE_NOTFALL,
    KATEGORIE_OEFFNUNGSZEITEN,
    KATEGORIE_PREISE,
    SICHTBARKEIT_KUNDE,
    TenantKnowledge,
)
from core.models.wissensluecke import (
    KANAL_LABELS,
    STATUS_OFFEN,
    Wissensluecke,
)

logger = logging.getLogger(__name__)


# Virtuelle Kategorien: keine Zeilen in tenant_knowledge, sondern
# Spiegelungen strukturierter Tabellen. Read-only.
KATEGORIE_KATALOG = "_katalog"
KATEGORIE_UEBERSCHLAG = "_ueberschlag"

VIRTUELLE_LABELS = {
    KATEGORIE_KATALOG: "Leistungen mit Preis (Katalog — verbindlich)",
    KATEGORIE_UEBERSCHLAG: "Ueberschlags-Formeln",
}

# Reihenfolge im Prompt-Block: der verbindliche Katalog zuerst, damit das
# Modell bei widerspruechlichem Freitext die belastbare Zahl zuerst liest.
BLOCK_REIHENFOLGE = (
    (KATEGORIE_KATALOG,)
    + (KATEGORIE_UEBERSCHLAG,)
    + tuple(ALLE_KATEGORIEN)
)


@dataclass(frozen=True)
class Eintrag:
    """Ein Wissens-Schnipsel, egal aus welcher Quelle."""

    kategorie: str
    text: str
    id: str | None = None       # None bei virtuellen Eintraegen
    quelle: str = "mensch"
    sichtbarkeit: str = SICHTBARKEIT_KUNDE
    virtuell: bool = False

    @property
    def label(self) -> str:
        return VIRTUELLE_LABELS.get(
            self.kategorie, KATEGORIE_LABELS.get(self.kategorie, self.kategorie)
        )


# =====================================================================
# Laden
# =====================================================================

async def lade(
    tenant_id: uuid.UUID,
    *,
    nur_kunde: bool = True,
    nur_aktiv: bool = True,
    mit_katalog: bool = True,
) -> list[Eintrag]:
    """Alle Eintraege eines Tenants, gefiltert.

    ``nur_kunde=True`` (Default) laesst interne Eintraege weg — das ist
    der richtige Default fuer jeden Pfad, der Text nach draussen gibt.
    Die App ruft mit ``nur_kunde=False`` auf.
    """
    eintraege: list[Eintrag] = []
    async with get_session() as s:
        q = select(TenantKnowledge).where(TenantKnowledge.tenant_id == tenant_id)
        if nur_aktiv:
            q = q.where(TenantKnowledge.aktiv.is_(True))
        if nur_kunde:
            q = q.where(TenantKnowledge.sichtbarkeit == SICHTBARKEIT_KUNDE)
        rows = (await s.execute(
            q.order_by(TenantKnowledge.kategorie, TenantKnowledge.created_at)
        )).scalars().all()
        for r in rows:
            eintraege.append(Eintrag(
                id=str(r.id),
                kategorie=r.kategorie,
                text=r.text,
                quelle=r.quelle,
                sichtbarkeit=r.sichtbarkeit,
            ))
    if mit_katalog:
        eintraege = (await _virtuelle(tenant_id)) + eintraege
    return eintraege


async def _virtuelle(tenant_id: uuid.UUID) -> list[Eintrag]:
    """Leistungskatalog + Formeln als Wissens-Eintraege gespiegelt.

    Der Preis, den Q am Telefon nennt, kommt damit aus derselben Zeile,
    aus der das Angebot spaeter rechnet.
    """
    out: list[Eintrag] = []
    try:
        from core.models.tenant_leistung import TenantLeistung
        from core.models.tenant_kalkulation import TenantKalkulation

        async with get_session() as s:
            leistungen = (await s.execute(
                select(TenantLeistung)
                .where(TenantLeistung.tenant_id == tenant_id)
                .where(TenantLeistung.aktiv.is_(True))
                .order_by(TenantLeistung.sortierung, TenantLeistung.name)
            )).scalars().all()
            formeln = (await s.execute(
                select(TenantKalkulation)
                .where(TenantKalkulation.tenant_id == tenant_id)
                .where(TenantKalkulation.aktiv.is_(True))
                .order_by(TenantKalkulation.sortierung, TenantKalkulation.name)
            )).scalars().all()

        for l in leistungen:
            teile = [f"{l.name}: {float(l.preis_eur):.2f} EUR pro {l.einheit}"]
            if l.aliase:
                teile.append("auch genannt: " + ", ".join(l.aliase))
            if l.standard_beschreibung:
                teile.append(l.standard_beschreibung.strip())
            out.append(Eintrag(
                kategorie=KATEGORIE_KATALOG,
                text=". ".join(teile),
                virtuell=True,
            ))
        for f in formeln:
            vars_txt = ", ".join(f.variablen or []) or "keine"
            beschr = f" ({f.beschreibung.strip()})" if f.beschreibung else ""
            out.append(Eintrag(
                kategorie=KATEGORIE_UEBERSCHLAG,
                text=(
                    f"{f.name}{beschr}: Richtwert berechenbar, benoetigte "
                    f"Angaben: {vars_txt}."
                ),
                virtuell=True,
            ))
    except Exception as exc:  # noqa: BLE001
        # Der Katalog ist Kuer — faellt er aus, soll die Wissensbasis
        # trotzdem antworten koennen.
        logger.warning("Katalog-Spiegelung fehlgeschlagen: %s", exc)
    return out


# =====================================================================
# Relevanz
# =====================================================================

_STOPWORDS = frozenset("""
der die das den dem des ein eine einen einem eines und oder aber doch
wie was wer wann wo warum welche welcher welches ist sind war waren
kann koennen sollte muesste ich du er sie wir ihr mich dich uns euch
habt habe haben hat fuer bei mit ohne auf aus von zum zur im am nicht
noch mal bitte danke hallo guten tag eigentlich denn also
""".split())

_WORT = re.compile(r"[a-z0-9]+")


def _normalisiere(text: str) -> str:
    t = (text or "").lower()
    t = (t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
          .replace("ß", "ss"))
    return " ".join(_WORT.findall(t))


def _tokens(text: str) -> list[str]:
    return [w for w in _normalisiere(text).split()
            if len(w) >= 3 and w not in _STOPWORDS]


def _trigramme(text: str) -> set[str]:
    t = f"  {_normalisiere(text)} "
    return {t[i:i + 3] for i in range(len(t) - 2)}


def aehnlichkeit(a: str, b: str) -> float:
    """Jaccard ueber Zeichen-Trigrammen — 0.0 bis 1.0.

    Faengt Wortformen, an denen die alte Substring-Suche scheiterte:
    "Stundenlohn" findet "Stundensatz", "Kosten" findet "Kostet".
    """
    ta, tb = _trigramme(a), _trigramme(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Wonach eine Frage klingt, auch wenn kein Wort aus dem Eintrag vorkommt.
#
# Reines Wort-Matching scheitert an genau den Fragen, die Kunden wirklich
# stellen: "Wann habt ihr auf?" besteht nach Stoppwort-Abzug aus NICHTS,
# und "Kommt ihr nachts bei Wasserschaden?" teilt kein Wort mit dem Eintrag
# "Rohrbruch = sofort, Notdienst 24/7". Diese Karte schlaegt die Bruecke
# von der Alltagssprache zur Kategorie. Bewusst handgepflegt und klein —
# sie muss nicht vollstaendig sein, nur die haeufigen Faelle treffen.
KATEGORIE_STICHWORTE: dict[str, tuple[str, ...]] = {
    KATEGORIE_PREISE: (
        "kost", "preis", "teuer", "guenstig", "stundensatz", "stundenlohn",
        "betrag", "euro", "rechnung", "angebot", "pauschal", "aufschlag",
        "zahlen", "bezahl", "anzahlung", "budget",
    ),
    KATEGORIE_OEFFNUNGSZEITEN: (
        "auf", "offen", "geoeffnet", "zeiten", "uhrzeit", "erreichbar",
        "sprechzeit", "samstag", "sonntag", "wochenende", "feierabend",
        "urlaub", "betriebsferien", "wann",
    ),
    KATEGORIE_NOTFALL: (
        "notfall", "notdienst", "dringend", "sofort", "nachts", "nacht",
        "wochenende", "wasserschaden", "rohrbruch", "ausfall", "defekt",
        "kaputt", "heute noch", "schnell", "eilig",
    ),
    KATEGORIE_ANFAHRT: (
        "anfahrt", "kommt", "kommen", "entfernung", "gebiet", "umkreis",
        "wohne", "wohnen", "adresse", "weit", "fahrt", "einzugs",
    ),
    KATEGORIE_LEISTUNGEN: (
        "macht", "machen", "anbiet", "leistung", "koennt", "uebernehm",
        "montage", "reparatur", "wartung", "einbau", "installation",
    ),
    KATEGORIE_MATERIALIEN: (
        "material", "marke", "hersteller", "fabrikat", "produkt", "modell",
        "ersatzteil", "qualitaet",
    ),
    KATEGORIE_BESONDERHEITEN: (
        "garantie", "gewaehrleistung", "zertifikat", "meister", "innung",
        "versichert", "foerder", "zuschuss", "referenz",
    ),
}


def _kategorie_hinweise(frage: str) -> dict[str, int]:
    """Wie viele Stichworte jeder Kategorie in der Frage vorkommen.

    Die Anzahl zaehlt, nicht nur ob ueberhaupt: "Kommt ihr auch nachts bei
    Wasserschaden?" trifft mit *einem* schwachen Wort ("kommt") die
    Anfahrt und mit *zwei* starken ("nachts", "wasserschaden") den
    Notfall. Ein binaeres ja/nein macht daraus einen Gleichstand.
    """
    norm = _normalisiere(frage)
    if not norm:
        return {}
    treffer: dict[str, int] = {}
    for kat, woerter in KATEGORIE_STICHWORTE.items():
        n = sum(1 for w in woerter if w in norm)
        if n:
            treffer[kat] = n
    return treffer


def _score(
    frage: str, eintrag: Eintrag, hinweise: dict[str, int] | None = None
) -> float:
    """Wie gut passt ein Eintrag zur Frage.

    Drei Signale, absteigend nach Aussagekraft gewichtet:
      1. woertlicher Token-Treffer im Eintragstext (staerkstes Signal)
      2. Kategorie-Hinweis aus der Stichwortkarte (rettet Fragen, die
         nach Stoppwort-Abzug leer sind)
      3. Trigramm-Aehnlichkeit (faengt Wortformen wie Stundenlohn/-satz)
    """
    if hinweise is None:
        hinweise = _kategorie_hinweise(frage)
    frage_tokens = _tokens(frage)
    text_norm = _normalisiere(eintrag.text)
    label_norm = _normalisiere(eintrag.label)

    score = 0.0
    if frage_tokens:
        treffer = sum(1 for t in frage_tokens if t in text_norm)
        label_treffer = sum(1 for t in frage_tokens if t in label_norm)
        score += 2.0 * treffer / len(frage_tokens)
        score += 0.5 * label_treffer / len(frage_tokens)
        score += 1.0 * aehnlichkeit(frage, eintrag.text)
    # Gedeckelt bei 3 Stichworten: ab da ist die Kategorie klar, und ein
    # langer Stichwortkatalog soll nicht alles andere ueberstimmen.
    n_hinweise = min(3, hinweise.get(eintrag.kategorie, 0))
    score += 1.2 * n_hinweise

    # Dasselbe noch einmal gegen den TEXT statt gegen die Kategorie.
    # Grund aus den echten Daten: Eintraege sind haeufig falsch einsortiert
    # ("Oeffnungszeiten: Mo-Fr 8-17" steht unter "Anfahrt") und enthalten
    # Tippfehler. Wer seine Wissensbasis von Hand pflegt, sortiert sie
    # nicht sauber — das Ranking darf daran nicht scheitern. Ein Treffer
    # im Text wiegt weniger als der richtige Ordner, aber mehr als nichts.
    for kat, _ in hinweise.items():
        if kat == eintrag.kategorie:
            continue
        woerter = KATEGORIE_STICHWORTE.get(kat, ())
        if any(wort in text_norm for wort in woerter):
            # Deutlich schwaecher als der Kategorie-Treffer (1.2): ein
            # richtig einsortierter Eintrag soll den falsch einsortierten
            # immer schlagen — der Texttreffer ist nur das Sicherheitsnetz
            # fuer den Fall, dass gar kein richtig einsortierter da ist.
            score += 0.6
            break
    # Der Katalog spiegelt Preise — er profitiert vom Preis-Hinweis mit.
    if eintrag.virtuell:
        score += 1.2 * min(3, hinweise.get(KATEGORIE_PREISE, 0))
    # Der verbindliche Katalog gewinnt Gleichstaende gegen Freitext.
    if eintrag.virtuell:
        score *= 1.15
    return score


# Ab dieser Punktzahl gilt ein Eintrag als echter Treffer. Darunter ist es
# Rauschen — und "ich weiss es nicht" ist am Telefon besser als geraten.
SCHWELLE = 0.35


def sortiere(
    frage: str,
    eintraege: list[Eintrag],
    *,
    limit: int = 8,
    schwelle: float = SCHWELLE,
) -> list[Eintrag]:
    """Ranking ohne DB — damit Aufrufer, die die Liste schon haben, nicht
    ein zweites Mal laden (und damit es ohne DB testbar ist)."""
    if not frage.strip():
        return eintraege[:limit]
    hinweise = _kategorie_hinweise(frage)
    bewertet = [(_score(frage, e, hinweise), e) for e in eintraege]
    bewertet = [(sc, e) for sc, e in bewertet if sc >= schwelle]
    bewertet.sort(key=lambda x: -x[0])
    return [e for _, e in bewertet[:limit]]


async def suche(
    tenant_id: uuid.UUID,
    frage: str,
    *,
    limit: int = 8,
    nur_kunde: bool = True,
    schwelle: float = SCHWELLE,
) -> list[Eintrag]:
    """Die zur Frage passenden Eintraege, bester zuerst."""
    alle = await lade(tenant_id, nur_kunde=nur_kunde)
    return sortiere(frage, alle, limit=limit, schwelle=schwelle)


# =====================================================================
# Prompt-Block
# =====================================================================

# Ab so vielen Eintraegen lohnt Auswahl statt Vollblock. Darunter ist der
# komplette Block billiger als jede Relevanz-Entscheidung.
AUSWAHL_AB = 25

# Wie viele Katalog-Positionen hoechstens in den Vollblock duerfen.
KATALOG_IM_BLOCK = 25


async def baue_block(
    tenant_id: uuid.UUID,
    *,
    frage: str | None = None,
    max_chars: int = 6000,
    nur_kunde: bool = True,
    leer_text: str = "Es liegen noch keine spezifischen Betriebs-Informationen vor.",
    eintraege: list[Eintrag] | None = None,
) -> str:
    """Fertiger Wissens-Block fuer einen System-Prompt.

    Gekuerzt wird an Eintragsgrenzen, nie mitten im Satz — und wenn etwas
    wegfaellt, sagt eine Schlusszeile das, damit das Modell nicht glaubt,
    es habe alles gesehen.
    """
    # ``eintraege`` durchreichen, wenn der Aufrufer sie schon geladen hat —
    # der Voice-Pfad braucht Liste UND Block und wuerde sonst dreimal
    # dieselben Tabellen abfragen, bei jedem eingehenden Anruf.
    alle = eintraege if eintraege is not None else await lade(
        tenant_id, nur_kunde=nur_kunde
    )
    if not alle:
        return leer_text

    if frage and len(alle) > AUSWAHL_AB:
        relevant = sortiere(frage, alle, limit=AUSWAHL_AB)
        gewaehlt = relevant or alle[:AUSWAHL_AB]
    else:
        # Ohne Frage (Voice-Initiation) geht alles rein — aber der
        # Leistungskatalog gedeckelt. Ein Betrieb mit 200 Positionen wuerde
        # sonst mit seiner Preisliste die Notfall-Logik und die FAQ aus dem
        # Prompt draengen, nur weil der Katalog oben steht. Fuer Details
        # ruft der Agent ohnehin das wissensbasis-Tool.
        virtuell = [e for e in alle if e.virtuell][:KATALOG_IM_BLOCK]
        gewaehlt = virtuell + [e for e in alle if not e.virtuell]

    nach_kat: dict[str, list[str]] = {}
    for e in gewaehlt:
        nach_kat.setdefault(e.kategorie, []).append(e.text)

    zeilen: list[str] = []
    laenge = 0
    weggelassen = 0
    for kat in BLOCK_REIHENFOLGE:
        texte = nach_kat.get(kat)
        if not texte:
            continue
        label = VIRTUELLE_LABELS.get(kat, KATEGORIE_LABELS.get(kat, kat))
        kopf = f"## {label}"
        if laenge + len(kopf) > max_chars:
            weggelassen += len(texte)
            continue
        zeilen.append(kopf)
        laenge += len(kopf) + 1
        for t in texte:
            zeile = f"- {t.strip()}"
            if laenge + len(zeile) > max_chars:
                weggelassen += 1
                continue
            zeilen.append(zeile)
            laenge += len(zeile) + 1
        zeilen.append("")
        laenge += 1

    if weggelassen:
        zeilen.append(
            f"(Hinweis: {weggelassen} weitere Eintraege sind hier nicht "
            "abgedruckt — frage bei Bedarf gezielt nach.)"
        )
    return "\n".join(zeilen).strip() or leer_text


async def baue_zeilen(
    tenant_id: uuid.UUID,
    *,
    frage: str | None = None,
    max_chars: int = 4000,
    nur_kunde: bool = True,
    leer_text: str = "(noch keine spezifischen Infos hinterlegt)",
) -> str:
    """Kompakte Ein-Zeilen-Form ``- [Kategorie] Text`` (Mail-Pipeline)."""
    alle = await lade(tenant_id, nur_kunde=nur_kunde)
    eintraege = sortiere(frage, alle, limit=40) if frage else alle
    if not eintraege:
        eintraege = alle
    if not eintraege:
        return leer_text
    zeilen: list[str] = []
    laenge = 0
    weggelassen = 0
    for e in eintraege:
        zeile = f"- [{e.label}] {e.text.strip()}"
        if laenge + len(zeile) > max_chars:
            weggelassen += 1
            continue
        zeilen.append(zeile)
        laenge += len(zeile) + 1
    if weggelassen:
        zeilen.append(f"- ({weggelassen} weitere Eintraege nicht abgedruckt)")
    return "\n".join(zeilen) or leer_text


# =====================================================================
# Wissensluecken
# =====================================================================

# Fragen kuerzer als das sind kein verwertbarer Hinweis ("hallo?", "ja")
MIN_FRAGE_LEN = 8

# Ab dieser Aehnlichkeit gilt eine Frage als "schon mal gefragt".
# Gemessen an echten Formulierungspaaren: dieselbe Frage in anderen
# Worten liegt bei 0.50-0.75, verschiedene Fragen bei 0.00-0.21. 0.45
# liegt mit Abstand in der Luecke dazwischen.
LUECKE_DEDUP_SCHWELLE = 0.45


async def merke_luecke(
    tenant_id: uuid.UUID,
    frage: str,
    kanal: str,
    *,
    kunde: str | None = None,
) -> bool:
    """Haelt eine unbeantwortete Kundenfrage fest.

    Gibt True zurueck, wenn etwas gespeichert wurde. Bewusst best-effort:
    ein Fehler hier darf niemals einen laufenden Anruf oder eine
    Mail-Antwort kippen — der Aufrufer bekommt nur False.

    Der Fragetext ist Fremdtext aus dem Internet bzw. vom Telefon. Er wird
    hier nur gespeichert und spaeter als Daten angezeigt, nie als
    Anweisung an ein Modell weitergereicht.
    """
    frage = re.sub(r"\s+", " ", (frage or "")).strip()[:500]
    if len(frage) < MIN_FRAGE_LEN:
        return False
    try:
        async with get_session() as s:
            offene = (await s.execute(
                select(Wissensluecke)
                .where(Wissensluecke.tenant_id == tenant_id)
                .where(Wissensluecke.status == STATUS_OFFEN)
                .order_by(Wissensluecke.zuletzt_gefragt_am.desc())
                .limit(50)
            )).scalars().all()
            jetzt = dt.datetime.now(dt.timezone.utc)
            for o in offene:
                if aehnlichkeit(frage, o.frage) >= LUECKE_DEDUP_SCHWELLE:
                    o.anzahl += 1
                    o.zuletzt_gefragt_am = jetzt
                    return True
            s.add(Wissensluecke(
                tenant_id=tenant_id,
                frage=frage,
                kanal=kanal,
                kunde=(kunde or None),
                zuletzt_gefragt_am=jetzt,
            ))
        # Der Fragetext stammt vom Kunden und kann alles enthalten —
        # Namen, Adressen, Anliegen. Er gehoert nicht ins Log; die Frage
        # steht ohnehin in der Wissensluecken-Liste in der App.
        logger.info(
            "Wissensluecke erfasst: tenant=%s kanal=%s laenge=%d",
            tenant_id, kanal, len(frage or ""),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wissensluecke konnte nicht gespeichert werden: %s", exc)
        return False


async def offene_luecken(tenant_id: uuid.UUID, *, limit: int = 20) -> list[dict]:
    """Offene Luecken, haeufigste zuerst — fuer 'Aktuelles' und Q."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Wissensluecke)
            .where(Wissensluecke.tenant_id == tenant_id)
            .where(Wissensluecke.status == STATUS_OFFEN)
            .order_by(
                Wissensluecke.anzahl.desc(),
                Wissensluecke.zuletzt_gefragt_am.desc(),
            )
            .limit(limit)
        )).scalars().all()
    return [{
        "id": str(r.id),
        "frage": r.frage,
        "kanal": r.kanal,
        "kanal_label": KANAL_LABELS.get(r.kanal, r.kanal),
        "kunde": r.kunde,
        "anzahl": r.anzahl,
        "zuletzt": r.zuletzt_gefragt_am.isoformat() if r.zuletzt_gefragt_am else None,
    } for r in rows]


# =====================================================================
# Datenschutz-Hinweis
# =====================================================================

# Die Wissensbasis ist ein Freitextfeld, und ihr Inhalt geht als
# System-Prompt an ElevenLabs (USA) und in Gemini-Aufrufe. Personenbezogene
# Daten haben da nichts verloren — aber verbieten kann man es nicht
# sinnvoll (ein Betriebs-Telefon IST eine Telefonnummer). Deshalb ein
# Hinweis beim Speichern statt einer Blockade: der Mensch entscheidet,
# aber er entscheidet bewusst.
_PII_MUSTER = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "eine E-Mail-Adresse"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,28}\b"), "eine IBAN"),
    (re.compile(r"(?<!\d)(?:\+49|0)[\s/-]?\d{2,5}[\s/-]?\d{3,}"), "eine Telefonnummer"),
)


def pruefe_personenbezug(text: str) -> str | None:
    """Gibt einen Hinweis zurueck, wenn der Text nach Personendaten aussieht.

    Kein Fehler, kein Blocken — nur ein Satz, der im richtigen Moment
    daran erinnert, wohin dieser Text spaeter geht.
    """
    gefunden = [label for muster, label in _PII_MUSTER if muster.search(text or "")]
    if not gefunden:
        return None
    return (
        "Hinweis: Der Eintrag enthaelt " + " und ".join(gefunden) + ". "
        "Wissens-Eintraege sagt Q auch Kunden am Telefon — bitte nur "
        "Betriebsdaten hinterlegen, keine Daten einzelner Personen."
    )


# =====================================================================
# Qualitaet: Widersprueche + Frische
# =====================================================================

# Deutsche Schreibweise: "1.250,50 EUR", "75 EUR", "35 Euro", "89,90 €".
# Die Tausendertrennung muss mit in den Match — sonst wird aus 1.250,50
# der Betrag 250,50, und die Widerspruchs-Erkennung meldet einen
# Widerspruch, den es nicht gibt.
_EURO = re.compile(
    r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:eur|euro|€)",
    re.I,
)


def _betraege(text: str) -> set[float]:
    out = set()
    for m in _EURO.finditer(text or ""):
        roh = m.group(1)
        # Punkt ist Tausendertrenner nur, wenn danach genau 3 Ziffern
        # stehen — "75.50 EUR" (englisch getippt) bleibt 75,50.
        if re.search(r"\.\d{3}(?!\d)", roh):
            roh = roh.replace(".", "")
        roh = roh.replace(",", ".")
        try:
            out.add(float(roh))
        except ValueError:
            continue
    return out


async def pruefe_qualitaet(tenant_id: uuid.UUID) -> dict:
    """Findet Widersprueche und veraltete Eintraege.

    Widerspruch = zwei Preis-Eintraege, die dasselbe Thema betreffen
    (Trigramm-Aehnlichkeit hoch) aber verschiedene Betraege nennen. Genau
    der Fall, in dem Q am Telefon eine andere Zahl sagt als das Angebot
    rechnet — und der Kunde das merkt.
    """
    eintraege = await lade(tenant_id, nur_kunde=False, mit_katalog=False)
    preis_eintraege = [
        e for e in eintraege
        if e.kategorie in (KATEGORIE_PREISE, KATEGORIE_LEISTUNGEN)
    ]

    widersprueche: list[dict] = []
    for i, a in enumerate(preis_eintraege):
        for b in preis_eintraege[i + 1:]:
            ba, bb = _betraege(a.text), _betraege(b.text)
            if not ba or not bb or ba == bb:
                continue
            if aehnlichkeit(a.text, b.text) < 0.35:
                continue
            widersprueche.append({
                "a_id": a.id, "a_text": a.text,
                "b_id": b.id, "b_text": b.text,
                "betraege": sorted(ba | bb),
            })

    # Katalog gegen Freitext: die haeufigste echte Abweichung.
    katalog = [e for e in await _virtuelle(tenant_id)
               if e.kategorie == KATEGORIE_KATALOG]
    for k in katalog:
        kb = _betraege(k.text)
        for e in preis_eintraege:
            eb = _betraege(e.text)
            if not kb or not eb or kb & eb:
                continue
            if aehnlichkeit(k.text, e.text) < 0.30:
                continue
            widersprueche.append({
                "a_id": None, "a_text": f"Katalog: {k.text}",
                "b_id": e.id, "b_text": e.text,
                "betraege": sorted(kb | eb),
            })

    veraltet = await _veraltete(tenant_id)
    return {
        "widersprueche": widersprueche[:10],
        "veraltet": veraltet,
    }


async def _veraltete(tenant_id: uuid.UUID) -> list[dict]:
    """Preis-/Material-Eintraege, die seit FRISCHE_TAGE nicht bestaetigt sind."""
    grenze = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=FRISCHE_TAGE)
    async with get_session() as s:
        rows = (await s.execute(
            select(TenantKnowledge)
            .where(TenantKnowledge.tenant_id == tenant_id)
            .where(TenantKnowledge.aktiv.is_(True))
            .where(TenantKnowledge.kategorie.in_(FRISCHE_KATEGORIEN))
        )).scalars().all()
    out = []
    for r in rows:
        stand = r.zuletzt_bestaetigt_am or r.created_at
        if stand and stand < grenze:
            out.append({
                "id": str(r.id),
                "kategorie": r.kategorie,
                "kategorie_label": KATEGORIE_LABELS.get(r.kategorie, r.kategorie),
                "text": r.text,
                "stand": stand.isoformat(),
                "tage": (dt.datetime.now(dt.timezone.utc) - stand).days,
            })
    out.sort(key=lambda x: -x["tage"])
    return out[:10]
