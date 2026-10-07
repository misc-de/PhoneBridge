# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""English in the code, German from lang_de.py - complete and consistent."""

import ast
import os
import re
import unittest
from unittest import mock

from phonebridge import i18n, text
from phonebridge.lang_de import TRANSLATIONS

from . import ROOT

PKG = os.path.join(ROOT, "phonebridge")
# “ opens a quote in English and closes one in German - „ is German only.
GERMAN = re.compile(r"[äöüÄÖÜß„]")
NOT_TEXT = {"GSM7", "GSM7_EXT"}
# the same in both languages, and rightly so
SAME_IN_GERMAN = {"Name (optional)"}


def sources():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py") and name != "lang_de.py":
            path = os.path.join(PKG, name)
            with open(path, encoding="utf-8") as f:
                yield name, ast.parse(f.read())


def marked_texts():
    """Every text the code hands to _(), N_() or n_()."""
    found = {}
    for name, tree in sources():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_", "N_", "n_")):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.setdefault(arg.value, "%s:%d" % (name, node.lineno))
    for code, label in i18n.LANGUAGES:
        if code == "system":
            found.setdefault(label, "i18n.LANGUAGES")
    return found


def placeholders(text):
    return sorted(re.findall(r"%[sd%]", text))


class Table(unittest.TestCase):
    def test_every_text_is_translated(self):
        missing = {t: where for t, where in marked_texts().items()
                   if t not in TRANSLATIONS}
        self.assertEqual(missing, {}, "texts without German")

    def test_no_orphans(self):
        used = marked_texts()
        orphans = [t for t in TRANSLATIONS if t not in used]
        self.assertEqual(orphans, [], "German for texts the code no longer has")

    def test_placeholders_match(self):
        for en, de in TRANSLATIONS.items():
            self.assertEqual(placeholders(en), placeholders(de), en)
            # strftime formats keep their fields
            self.assertEqual(sorted(re.findall(r"%[HMYmd]", en)),
                             sorted(re.findall(r"%[HMYmd]", de)), en)

    def test_no_german_in_the_code(self):
        """German belongs in lang_de.py - nowhere else on screen."""
        found = []
        for name, tree in sources():
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
                    and n.body and isinstance(n.body[0], ast.Expr)}
            # character sets, not texts on screen
            docs |= {id(c) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", "") in NOT_TEXT for t in n.targets)
                     for c in ast.walk(n.value)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docs and GERMAN.search(node.value)):
                    found.append("%s:%d %r" % (name, node.lineno, node.value[:40]))
        self.assertEqual(found, [])

    def test_translations_are_german(self):
        same = [en for en, de in TRANSLATIONS.items()
                if en == de and len(en) > 12 and en not in SAME_IN_GERMAN]
        self.assertEqual(same, [], "copied, not translated?")


class Choice(unittest.TestCase):
    def tearDown(self):
        os.environ["LANGUAGE"] = "en"
        i18n.setup("system")

    def test_system_language(self):
        # the desktop's own LANG and LC_* stay out: "C" falls back to them
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("LC_ALL", "LC_MESSAGES", "LANG")}
        with mock.patch.dict(os.environ, clean, clear=True):
            for value, code in (("de_DE.UTF-8", "de"), ("de", "de"), ("en_GB", "en"),
                                ("fr_FR.UTF-8", "en"), ("de:en", "de"), ("C", "en")):
                os.environ["LANGUAGE"] = value
                self.assertEqual(i18n.system_language(), code, value)
            os.environ.update(LANGUAGE="C", LANG="de_DE.UTF-8")
            self.assertEqual(i18n.system_language(), "de")

    def test_setup(self):
        self.assertEqual(i18n.setup("de"), "de")
        self.assertEqual(i18n._("Messages"), "Nachrichten")
        self.assertEqual(i18n._("Not in the table"), "Not in the table")
        self.assertEqual(i18n.setup("en"), "en")
        self.assertEqual(i18n._("Messages"), "Messages")
        os.environ["LANGUAGE"] = "de_AT"
        self.assertEqual(i18n.setup("system"), "de")
        self.assertEqual(i18n.setup("klingon"), "de")      # unknown: system

    def test_plural(self):
        i18n.setup("de")
        self.assertEqual(text.n_unread(1), "1 ungelesene Nachricht")
        self.assertEqual(text.n_unread(3), "3 ungelesene Nachrichten")
        i18n.setup("en")
        self.assertEqual(text.n_unread(2), "2 unread messages")


class Desktop(unittest.TestCase):
    def test_desktop_file_has_german(self):
        with open(os.path.join(ROOT, "data", "io.github.miscde.PhoneBridge.desktop"),
                  encoding="utf-8") as f:
            desktop = f.read()
        self.assertIn("Name=PhoneBridge", desktop)
        self.assertIn("Comment[de]=", desktop)
