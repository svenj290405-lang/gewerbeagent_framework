#!/usr/bin/env python3
"""Holt die Schriften einmalig von Google und legt sie lokal ab.

Warum das Skript existiert: die Website und das Admin-Tool haben die
Schriften frueher bei jedem Seitenaufruf von fonts.googleapis.com /
fonts.gstatic.com nachgeladen. Das uebertraegt die IP-Adresse des
Besuchers in die USA — und widerspricht damit unserer eigenen Zusage
auf der Landingpage ("kein Tracking durch Dritte"). Seit dem
LG-Muenchen-Urteil (3 O 17493/20) ist die Einbindung ohne Einwilligung
auch abmahnfaehig.

Die Dateien liegen deshalb ausgeliefert im Repo bzw. im Website-Ordner
und werden von uns selbst serviert. Das Skript ist nur noetig, wenn
eine Schrift/ein Schnitt DAZUKOMMT — im Normalbetrieb laeuft es nie.

Aufruf (Host, braucht Internet):
    python3 scripts/fetch_fonts.py <zielordner>

Erzeugt <zielordner>/fonts.css + <zielordner>/files/*.woff2.
Nur die Subsets latin + latin-ext; Google liefert je Familie eine
Variable-Font-Datei, die sich alle Schnitte teilen — deshalb wird
ueber den Inhalts-Hash dedupliziert (6 Dateien statt 18).

Schriften: Inter + Inter Tight + JetBrains Mono, alle SIL OFL 1.1
(freie Weitergabe inkl. Selbsthosting ausdruecklich erlaubt).
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys
import urllib.request

FAMILIES = [
    "Inter:wght@400;500;600;700",        # Website + Admin
    "Inter+Tight:wght@500;600;700",      # Website-Headlines
    "JetBrains+Mono:wght@400;500",       # Admin-Tabellen
]

# Ohne Desktop-UA liefert Google ttf statt woff2 (halb so gut komprimiert).
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# cyrillic/greek/vietnamese braucht keine unserer Seiten.
KEEP_SUBSETS = ("latin", "latin-ext")

HEADER = """/* Lokal ausgelieferte Schriften (SIL Open Font License 1.1).
   Bewusst KEIN Google-Fonts-CDN: der Abruf uebertraegt die IP des
   Besuchers in die USA und widerspricht unserer Zusage "kein Tracking
   durch Dritte". Neu erzeugen mit scripts/fetch_fonts.py. */

"""


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read()


def main(target: pathlib.Path) -> int:
    files_dir = target / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    by_hash: dict[str, str] = {}   # Inhalts-Hash -> Dateiname
    blocks: list[str] = []

    for family in FAMILIES:
        css = _get(
            f"https://fonts.googleapis.com/css2?family={family}&display=swap"
        ).decode()
        fam_name = family.split(":")[0].replace("+", " ")
        fam_slug = family.split(":")[0].replace("+", "-").lower()

        # Google gruppiert als "/* subset */ @font-face {...}"
        for match in re.finditer(
            r"/\*\s*([a-z0-9-]+)\s*\*/\s*(@font-face\s*\{[^}]*\})", css
        ):
            subset, block = match.group(1), match.group(2)
            if subset not in KEEP_SUBSETS:
                continue
            url_match = re.search(r"url\((https://[^)]+\.woff2)\)", block)
            weight_match = re.search(r"font-weight:\s*(\d+)", block)
            if not url_match or not weight_match:
                continue

            data = _get(url_match.group(1))
            digest = hashlib.sha256(data).hexdigest()
            name = by_hash.get(digest)
            if name is None:
                name = f"{fam_slug}-{subset}.woff2"
                (files_dir / name).write_bytes(data)
                by_hash[digest] = name
                print(f"  {name}  {len(data) // 1024} KB")

            block = block.replace(url_match.group(1), f"files/{name}")
            blocks.append(
                f"/* {fam_name} {weight_match.group(1)} — {subset} */\n{block}"
            )

    (target / "fonts.css").write_text(HEADER + "\n\n".join(blocks) + "\n")
    print(f"{target/'fonts.css'}: {len(blocks)} @font-face, "
          f"{len(by_hash)} Dateien")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(pathlib.Path(sys.argv[1])))
