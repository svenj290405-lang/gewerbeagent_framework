"""Probe: die drei Modi der Objekt-Suche gegen das echte Gemini.

Fragt nacheinander „was ist das", „gibt es eine Anleitung" und „wo kaufe ich
das" zum selben Bild — genau die Reihenfolge, die der Handwerker in der App
geht. Zeigt pro Schritt, ob wirklich gesucht wurde (Suchanfragen) und was an
Quellen zurueckkommt; genau daran haengt, ob die Antwort geerdet ist oder aus
dem Gedaechtnis erfunden.

    docker exec -e PYTHONPATH=/app -w /app gewerbeagent_framework \\
        uv run python scripts/probe_objekt_suche.py foto.jpg

Ohne Pfad wird ein Typenschild als Testbild erzeugt.
"""
import asyncio
import sys

from core.ai.gemini import objekt_anleitung, objekt_erkennen, objekt_kaufen

_ZEILEN = [
    "Vaillant GmbH  Remscheid",
    "ecoTEC plus",
    "VC 20 CS/1-5",
    "Art.-Nr. 0010021982",
    "Gas-Brennwertgeraet",
    "max. Betriebsdruck 3 bar",
]


def _testbild() -> bytes:
    """Ein Typenschild zeichnen — reicht, um den Lesepfad zu pruefen."""
    import io

    from PIL import Image, ImageDraw

    bild = Image.new("RGB", (700, 320), "white")
    zeichner = ImageDraw.Draw(bild)
    zeichner.rectangle([10, 10, 690, 310], outline="black", width=3)
    y = 45
    for zeile in _ZEILEN:
        zeichner.text((40, y), zeile, fill="black")
        y += 42
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    return puffer.getvalue()


async def _zeigen(titel: str, ergebnis: dict) -> None:
    print(f"\n{'=' * 70}\n{titel}\n{'=' * 70}")
    if not ergebnis.get("ok"):
        print(f"FEHLER: {ergebnis.get('error')}")
        return
    print(ergebnis.get("text", "")[:1200])
    anfragen = ergebnis.get("suchanfragen") or []
    quellen = ergebnis.get("quellen") or []
    print(f"\n  gesucht wurde : {anfragen or 'NICHTS — Antwort ungeerdet!'}")
    for q in quellen:
        print(f"  Quelle        : {q['domain']} — {q['titel'][:70]}")
    if not quellen:
        print("  Quelle        : keine")


async def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            bild = f.read()
        mime = "image/png" if sys.argv[1].lower().endswith(".png") else "image/jpeg"
        print(f"Foto: {sys.argv[1]} ({len(bild)} Bytes)")
    else:
        bild, mime = _testbild(), "image/png"
        print("Erzeugtes Typenschild als Testbild")

    # Verlauf mitfuehren wie die App — der zweite und dritte Schritt sollen
    # wissen, was im ersten bestimmt wurde.
    verlauf: list[dict] = []

    schritt1 = await objekt_erkennen(bild, mime, "Was ist das für ein Teil?",
                                     branche="Heizungsbau")
    await _zeigen("1. Was ist das für ein Teil?", schritt1)
    if schritt1.get("ok"):
        verlauf += [{"role": "user", "text": "Was ist das für ein Teil?"},
                    {"role": "model", "text": schritt1["text"][:900]}]

    schritt2 = await objekt_anleitung(bild, mime, "Gibt es dazu eine Anleitung?",
                                      verlauf=verlauf)
    await _zeigen("2. Gibt es dazu eine Anleitung?", schritt2)

    schritt3 = await objekt_kaufen(bild, mime,
                                   "Wo bekomme ich genau das, und was kostet es?",
                                   verlauf=verlauf)
    await _zeigen("3. Wo bekomme ich das zu kaufen?", schritt3)


if __name__ == "__main__":
    asyncio.run(main())
