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

# Kalkulations-Wizard (mathematische Formeln fuers Angebot)
STATE_KALK_KATEGORIE = "kalk_kategorie"
STATE_KALK_NAME = "kalk_name"
STATE_KALK_FORMEL = "kalk_formel"
STATE_KALK_EINHEIT = "kalk_einheit"
STATE_KALK_BESCHREIBUNG = "kalk_beschreibung"
STATE_KALK_LOESCHEN = "kalk_loeschen"
STATE_KALK_EXCEL_WAITING = "kalk_excel_waiting"
STATE_KALK_EXCEL_CONFIRM = "kalk_excel_confirm"

# Visualisierung-Wizard
STATE_VIZ_WAITING_PHOTO = "viz_waiting_photo"
STATE_VIZ_WAITING_DESCRIPTION = "viz_waiting_description"
STATE_VIZ_WAITING_KUNDE = "viz_waiting_kunde"
# Post-Generation: User entscheidet was mit dem Bild passieren soll
# (Mail an Kunde / Drive-Archiv / fertig). Inline-Buttons setzen den
# Folge-State der dann auf Email-Adresse oder Kunden-Name wartet.
STATE_VIZ_POST_ACTION = "viz_post_action"
STATE_VIZ_POST_MAIL_EMAIL = "viz_post_mail_email"
STATE_VIZ_POST_DRIVE_KUNDE = "viz_post_drive_kunde"
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

# /angebot-Wizard: Text/Voice → Gemini → Kalkulation → Lexware-Angebot → Mail → Auto-Rechnung
STATE_ANGEBOT_WAITING_INPUT = "angebot_waiting_input"
STATE_ANGEBOT_PREVIEWING = "angebot_previewing"
STATE_ANGEBOT_AWAITING_INSTRUCTIONS = "angebot_awaiting_instructions"
STATE_ANGEBOT_AWAITING_MAIL = "angebot_awaiting_mail"
STATE_ANGEBOT_AWAITING_KUNDE_NAME = "angebot_awaiting_kunde_name"

# /onboarding-Tutorial: Wizard fuehrt den Handwerker Schritt-fuer-
# Schritt durch das Setup. Der aktuelle Schritt steht im Tenant
# (onboarding_step), der State markiert dass wir gerade auf User-Input
# warten. Solange dieser State aktiv ist, blockiert der Dispatcher
# andere Slash-Befehle (Game-Tutorial-Stil).
STATE_ONBOARDING_ACTIVE = "onboarding_active"

STATE_LEISTUNG_WAITING_NAME = "leistung_waiting_name"
STATE_LEISTUNG_WAITING_PREIS = "leistung_waiting_preis"
STATE_LEISTUNG_WAITING_BESCHREIBUNG = "leistung_waiting_beschreibung"
STATE_LEISTUNG_PREVIEWING = "leistung_previewing"

# Werkstatt-Setup (Heimat-Adresse fuer Smart-Termine)
STATE_WERKSTATT_WAITING_ADDRESS = "werkstatt_waiting_address"
STATE_WERKSTATT_CONFIRMING = "werkstatt_confirming"

# Mitarbeiter-Wizard (Phase 4 Multi-Mitarbeiter)
STATE_MITARBEITER_NEU_NAME = "mitarbeiter_neu_name"
STATE_MITARBEITER_NEU_SKILLS = "mitarbeiter_neu_skills"
# Phase 6: Job-Titel + Arbeitszeit
STATE_MITARBEITER_JOB_TITLE_INPUT = "mitarbeiter_job_title_input"
STATE_MITARBEITER_ARBEITSZEIT_PRESET = "mitarbeiter_arbeitszeit_preset"
STATE_MITARBEITER_ARBEITSZEIT_CUSTOM_DAYS = "mitarbeiter_arbeitszeit_custom_days"
STATE_MITARBEITER_ARBEITSZEIT_CUSTOM_HOURS = "mitarbeiter_arbeitszeit_custom_hours"

# Phase 6: Krank/Urlaub
STATE_KRANK_AWAIT_EMPLOYEE = "krank_await_employee"
STATE_KRANK_AWAIT_DURATION = "krank_await_duration"
STATE_KRANK_AWAIT_CUSTOM_DATE = "krank_await_custom_date"
STATE_URLAUB_AWAIT_EMPLOYEE = "urlaub_await_employee"
STATE_URLAUB_AWAIT_START = "urlaub_await_start"
STATE_URLAUB_AWAIT_END = "urlaub_await_end"

# Kalender-Verbinden-Wizard (Outlook-Calendar-Support)
STATE_KALENDER_PROVIDER_CHOICE = "kalender_provider_choice"

# Material-Wizard (Verbrauchsartikel-Nachbestellung)
STATE_MATERIAL_NEU_NAME = "material_neu_name"
STATE_MATERIAL_NEU_LINK = "material_neu_link"
STATE_MATERIAL_NEU_LIEFERANT = "material_neu_lieferant"
STATE_MATERIAL_NEU_PREVIEWING = "material_neu_previewing"
STATE_BESTELLEN_MENGE = "bestellen_menge"

# Archiv-Wizard (Google-Drive-Upload pro Kunde)
STATE_ARCHIV_WAITING_FILES = "archiv_waiting_files"

# Storno-Wizard (Termin per Telegram absagen)
STATE_STORNO_AWAIT_QUERY = "storno_await_query"
STATE_STORNO_AWAIT_CHOICE = "storno_await_choice"
STATE_STORNO_AWAIT_CONFIRM = "storno_await_confirm"

# Formular-Editor-Wizard
STATE_FORMULAR_TYP_WAEHLEN = "formular_typ_waehlen"
STATE_FORMULAR_HAUPTMENU = "formular_hauptmenu"
STATE_FORMULAR_NEU_NAME = "formular_neu_name"
STATE_FORMULAR_NEU_LABEL = "formular_neu_label"
STATE_FORMULAR_NEU_TYP = "formular_neu_typ"
STATE_FORMULAR_NEU_OPTIONEN = "formular_neu_optionen"
STATE_FORMULAR_NEU_REQUIRED = "formular_neu_required"
STATE_FORMULAR_LOESCHEN = "formular_loeschen"
STATE_FORMULAR_RESET_CONFIRM = "formular_reset_confirm"




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
