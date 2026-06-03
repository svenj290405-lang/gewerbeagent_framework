"""PWA-Passwort fuer einen Login (E-Mail) setzen.

Setzt den bcrypt-Hash (app_password_hash) auf dem Employee, dessen
contact_email der angegebenen Mail entspricht. Danach: klassisches
Login mit E-Mail + Passwort unter https://gewerbeagent.de/app.

Nutzung (im Prod-Container, PYTHONPATH=/app):
  docker exec -w /app -e PYTHONPATH=/app gewerbeagent_framework \
      /app/.venv/bin/python /app/scripts/set_app_password.py <email> <passwort>
"""
from __future__ import annotations

import asyncio
import sys

from core.database.connection import get_session
from core.security.app_auth import find_employee_by_email, set_app_password_hash


async def _set(email: str, password: str) -> None:
    if "@" not in email:
        print(f"FEHLER: '{email}' ist keine Mailadresse"); return
    if len(password) < 6:
        print("FEHLER: Passwort zu kurz (min. 6 Zeichen)"); return
    async with get_session() as s:
        emp = await find_employee_by_email(email, session=s)
        if emp is None:
            print(f"FEHLER: kein aktiver Mitarbeiter mit contact_email={email}. "
                  f"Erst die Mail setzen (scripts/set_login_email.py)."); return
        set_app_password_hash(emp, password)
        who = f"{emp.slug} ({emp.name})"
    print(f"OK: Passwort gesetzt fuer {who} <{email}>.")
    print("    -> https://gewerbeagent.de/app  (E-Mail + Passwort eingeben)")


def main() -> None:
    if len(sys.argv) != 3:
        print("Nutzung: set_app_password.py <email> <passwort>")
        sys.exit(1)
    asyncio.run(_set(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
