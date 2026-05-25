"""Admin-Login anlegen ODER dessen Passwort neu setzen (idempotent).

Laeuft IM Container (nutzt die Container-venv + DATABASE_URL):
    docker exec gewerbeagent_framework uv run \
        python scripts/set_admin_password.py <email> [passwort]

Ohne <passwort> wird ein sicheres 16-Zeichen-Passwort generiert und
einmalig ausgegeben (so landet der Klartext nicht in der Shell-History).
Nutzt die echte Auth-Logik (hash_password / bcrypt, Cost 12). Existiert
der Account schon, wird nur das Passwort neu gesetzt + is_active=True.
"""
import asyncio
import secrets
import string
import sys

sys.path.insert(0, "/app")

from sqlalchemy import select

from core.admin.auth import hash_password
from core.database.connection import get_session
from core.models.admin import AdminUser


def _gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: set_admin_password.py <email> [passwort]")
        raise SystemExit(2)
    email = sys.argv[1].lower().strip()
    password = sys.argv[2] if len(sys.argv) > 2 else _gen_password()
    if len(password) < 10:
        print("Passwort muss mind. 10 Zeichen lang sein")
        raise SystemExit(2)

    async with get_session() as s:
        user = (await s.execute(
            select(AdminUser).where(AdminUser.email == email)
        )).scalar_one_or_none()
        if user:
            user.password_hash = hash_password(password)
            user.is_active = True
            action = "Passwort aktualisiert"
        else:
            user = AdminUser(
                email=email,
                password_hash=hash_password(password),
                is_active=True,
            )
            s.add(user)
            action = "neu angelegt"
        await s.commit()

    print("=" * 50)
    print(f"Admin {action}: {email}")
    print(f"Passwort:       {password}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
