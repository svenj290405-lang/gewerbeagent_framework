"""
Symmetrische Verschluesselung fuer sensible Daten in der DB.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from config.settings import settings


logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    key_bytes = hashlib.sha256(settings.encryption_key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def try_decrypt(value: str | None) -> str | None:
    """Entschluesselt einen Wert; gibt ihn UNVERAENDERT zurueck, wenn er
    nicht verschluesselt ist.

    Brueckt den Alt-Bestand ab: Werte, die vor der Umstellung auf
    Verschluesselung-at-rest im Klartext gespeichert wurden (z.B. der
    globale Bot-Token). So kann ein Lesepfad sofort auf verschluesselte
    Speicherung umgestellt werden, ohne dass eine Daten-Migration noetig
    ist. Ein echter Klartext-Wert ist kein gueltiges Fernet-Token (kein
    passender HMAC unter unserem Key) -> decrypt() wirft -> Fallback.

    Der Fallback ist bewusst still WIRKSAM, aber nicht mehr still SICHTBAR:
    nach einer misslungenen Schluessel-Rotation sieht er wie Alt-Bestand
    aus und wuerde Ciphertext als Klartext-Token an eine Fremd-API geben.
    Das gehoert ins Log, damit es einmal jemand sieht — abbrechen wollen
    wir hier nicht, sonst waere eine halb migrierte Tabelle sofort ein
    Ausfall."""
    if not value:
        return None
    try:
        return decrypt(value) or None
    except Exception:
        # Sieht der Wert wie ein Fernet-Token aus (Version 0x80, base64url),
        # war es KEIN Alt-Bestand, sondern ein Schluesselproblem.
        if value.startswith("gAAAAA"):
            logger.error(
                "try_decrypt: Wert sieht verschluesselt aus, laesst sich aber "
                "nicht entschluesseln — falscher ENCRYPTION_KEY? Der Rohwert "
                "wird durchgereicht.",
            )
        return value
