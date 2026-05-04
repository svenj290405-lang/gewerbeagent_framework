"""
TelegramState: Conversation-State-Machine fuer Telegram-Bot.

Pro Chat-ID wird der aktuelle State gespeichert (z.B. "wissen_kategorie_waehlen").
Wenn der Bot vom User Eingabe erwartet, wird der State gesetzt; Folge-Messages
werden im State-Kontext interpretiert.

Cleanup: states mit expires_at < now werden vom periodischen Job entfernt
(noch nicht implementiert - aber expires_at wird beim Setzen mitgegeben).
"""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


# State-Konstanten (lose, kein DB-Constraint)
STATE_WISSEN_KATEGORIE = "wissen_kategorie"
STATE_WISSEN_TEXT = "wissen_text"
STATE_WISSEN_LOESCHEN = "wissen_loeschen"

# Visualisierung-Wizard
STATE_VIZ_WAITING_PHOTO = "viz_waiting_photo"
STATE_VIZ_WAITING_DESCRIPTION = "viz_waiting_description"
STATE_VIZ_WAITING_KUNDE = "viz_waiting_kunde"
# Beleg-Wizard (Lexware)
STATE_BELEG_WAITING_PHOTO = "beleg_waiting_photo"
STATE_BELEG_CONFIRMING = "beleg_confirming"

# Lexware-Setup-Wizard
STATE_LEXWARE_SETUP_TOKEN = "lexware_setup_token"
# Rechnung-Wizard
STATE_RECHNUNG_WAITING_INPUT = "rechnung_waiting_input"
STATE_RECHNUNG_CONFIRMING = "rechnung_confirming"
STATE_RECHNUNG_AWAITING_MAIL = "rechnung_awaiting_mail"

STATE_AUFNAHME_WAITING_AUDIO = "aufnahme_waiting_audio"
STATE_AUFNAHME_PREVIEWING = "aufnahme_previewing"




class TelegramState(Base):
    """Aktueller Conversation-State pro Chat."""

    __tablename__ = "telegram_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state_key: Mapped[str] = mapped_column(String(50), nullable=False)
    state_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<TelegramState chat={self.chat_id} key={self.state_key}>"
