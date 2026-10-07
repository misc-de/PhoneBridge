# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The language PhoneBridge speaks: English, or German.

Every text on screen is written in English in the code and passed through
_(). German comes from phonebridge/lang_de.py, keyed by the English text
itself, so the English stays readable where it is used and a missing
translation falls back to it instead of to nothing. tests/test_i18n.py
checks that every _() in the code has its German entry.

The choice lives in the settings ("language": "system", "en" or "de");
"system" follows $LANGUAGE / $LC_ALL / $LC_MESSAGES / $LANG. The app sets
it at start and rebuilds its window and tray menu when it changes."""

import os

LANGUAGES = (("system", "System"), ("en", "English"), ("de", "Deutsch"))
CODES = ("en", "de")

_current = "en"
_table = {}


def system_language():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value and value not in ("C", "POSIX"):
            code = value.split(":")[0][:2].lower()
            return code if code in CODES else "en"
    return "en"


def resolve(choice):
    """"system", "en" or "de" -> "en" or "de"."""
    return system_language() if choice not in CODES else choice


def setup(choice="system"):
    global _current, _table
    _current = resolve(choice)
    if _current == "de":
        from .lang_de import TRANSLATIONS
        _table = TRANSLATIONS
    else:
        _table = {}
    return _current


def current():
    return _current


def _(text):
    return _table.get(text, text)


def N_(text):
    """Marks a text for translation where it is defined; _() comes later."""
    return text


def n_(singular, plural, n):
    """Singular or plural, translated; both carry %d where the number goes."""
    return _(singular if n == 1 else plural) % n


setup(os.environ.get("PHONEBRIDGE_LANGUAGE", "system"))
