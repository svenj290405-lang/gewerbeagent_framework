"""Tests fuer die Kunden-Ordner-Identitaet (_kunde_identity_key).

Kernregel: Ordner werden ueber E-Mail/Telefon identifiziert, nicht ueber
den Namen. Zwei gleichnamige Kunden mit unterschiedlicher Mail bekommen
verschiedene Keys (= verschiedene Ordner); dieselbe Person (gleiche Mail)
denselben Key — auch bei abweichender Schreibweise des Namens.
"""
from __future__ import annotations

from core.integrations.google_drive import _kunde_identity_key, _slugify_kunde


def test_email_gewinnt_und_wird_normalisiert():
    assert _kunde_identity_key(
        "Max Müller", " Max@Example.COM ", "0151 23456789",
    ) == "email:max@example.com"


def test_telefon_wenn_keine_email():
    k = _kunde_identity_key("Max Müller", None, "+49 151 23456789")
    assert k.startswith("tel:")
    assert "23456789" in k  # normalisierte Nummer im Key


def test_name_fallback_ohne_mail_und_telefon():
    assert _kunde_identity_key("Max Müller", None, None) == _slugify_kunde("Max Müller")


def test_gleiche_mail_gleicher_key_trotz_anderem_namen():
    a = _kunde_identity_key("Max Müller", "kunde@x.de")
    b = _kunde_identity_key("M. Mueller", "kunde@x.de")
    assert a == b  # selbe Person -> selber Ordner


def test_gleicher_name_andere_mail_anderer_key():
    a = _kunde_identity_key("Max Müller", "max1@x.de")
    b = _kunde_identity_key("Max Müller", "max2@x.de")
    assert a != b  # zwei verschiedene Personen -> getrennte Ordner


def test_leere_mail_faellt_auf_telefon_zurueck():
    k = _kunde_identity_key("Max Müller", "   ", "0151 222 333")
    assert k.startswith("tel:")
