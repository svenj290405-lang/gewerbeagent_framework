"""Verifizierung von Volltext-Treffern bei find_events.

Wenn der Calendar-Provider per `q=`/`$search` einen Event liefert,
heisst das nur: irgendwo im durchsuchten Feld kommt der String vor.
Bei Telefonnummern oder E-Mail-Substrings kann das ein Zufallstreffer
sein (z.B. "0123" trifft auch auf "Hausnr. 0123"). Diese Helfer
verifizieren den Treffer durch eine zweite Pruefung der description —
nur Events bei denen die Telefon-/Mail-Zeile wirklich strukturiert
vorkommt zaehlen als Match.

Wird in beiden Adaptern (Google + Microsoft) genutzt damit die
Verifikations-Logik nicht doppelt lebt.
"""
from __future__ import annotations

from core.utils.phone import normalize_phone, phone_match_key


def verify_fulltext_phone_match(
    needle_normalized: str, description: str,
) -> bool:
    """True wenn description eine Telefon-Zeile mit passender Nummer enthaelt.

    Wir scannen Zeilen die mit "Telefon" beginnen (wie vom alten
    _book_appointment-Format produziert) und normalisieren beide Seiten,
    dann vergleichen wir den letzte-8-Ziffern-Suffix-Key. Damit matched
    "+49 30 1234" gegen "0301234" trotz unterschiedlicher Schreibweise.

    Fallback wenn keine "Telefon:"-Zeile gefunden wurde: Suffix-Suche
    im rohen description-Text (immer noch besser als q=-Substring).
    """
    if not needle_normalized or not description:
        return False
    needle_key = phone_match_key(needle_normalized)
    # Zeilenweise nach "Telefon:" oder "Tel:" suchen (Mail-Pipeline-
    # Formate). Vergleich via Suffix-Key.
    for line in description.splitlines():
        low = line.strip().lower()
        if low.startswith("telefon:") or low.startswith("tel:") or low.startswith("telefon "):
            value = line.split(":", 1)[-1] if ":" in line else line
            cand_key = phone_match_key(normalize_phone(value))
            if cand_key and cand_key == needle_key:
                return True
    # Allgemeiner Suffix-Fallback: normalisiere den ganzen description-
    # Text und pruefe ob der Match-Key als Substring vorkommt. Weniger
    # zuverlaessig (kann Postleitzahl + Hausnr. matchen) — daher nur
    # wenn Match-Key >= 8 Ziffern lang.
    if len(needle_key) >= 8:
        full_digits = normalize_phone(description)
        if needle_key in full_digits:
            return True
    return False


def verify_fulltext_email_match(
    needle_email_lower: str, description: str,
) -> bool:
    """True wenn description die Mail-Adresse als Substring enthaelt.

    Mails sind eindeutig genug — ein einfacher Lowercase-Substring-
    Match reicht zur Verifikation. (Mailadressen kommen in
    description nicht zufaellig vor wie Ziffern in Adressen.)
    """
    if not needle_email_lower or not description:
        return False
    return needle_email_lower in description.lower()
