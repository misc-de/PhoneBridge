# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Whether an app of the phone runs on this PC too - made-up launchers and
a made-up /proc."""

import os
import tempfile
import unittest

from phonebridge import localapps


class Running(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.apps = os.path.join(self.tmp.name, "applications")
        self.bin = os.path.join(self.tmp.name, "bin")
        self.proc = os.path.join(self.tmp.name, "proc")
        for d in (self.apps, self.bin, self.proc):
            os.makedirs(d)
        # a launcher script that starts electron (as element-desktop does)
        self.write(self.bin, "element-desktop",
                   "#!/bin/sh\n\nexec electron43 /usr/lib/element/app.asar \"$@\"\n")
        self.launcher("io.element.Element", "Element", os.path.join(self.bin, "element-desktop") + " %u")
        self.launcher("deltachat-desktop", "DeltaChat", "/usr/bin/deltachat-desktop %U")
        self.launcher("org.mozilla.Thunderbird", "Thunderbird", "thunderbird %u")
        self.launcher("org.signal.Signal", "Signal",
                      "/usr/bin/flatpak run --branch=stable org.signal.Signal")
        self.pid = 100

    def write(self, d, name, text):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def launcher(self, entry, name, exec_line):
        self.write(self.apps, entry + ".desktop",
                   "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n" % (name, exec_line))

    def process(self, *argv):
        self.pid += 1
        d = os.path.join(self.proc, str(self.pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")

    def local(self):
        return localapps.LocalApps(dirs=[self.apps], proc=self.proc)

    def test_nothing_runs(self):
        self.assertFalse(self.local().running("Element", "io.element.Element"))

    def test_electron_behind_a_script(self):
        self.process("/usr/lib/electron43/electron", "/usr/lib/element/app.asar")
        self.process("/usr/lib/electron43/electron", "--type=renderer")
        local = self.local()
        self.assertTrue(local.running("Element"))
        self.assertTrue(local.running("", "io.element.Element"))
        self.assertFalse(local.running("Fractal", "org.gnome.Fractal"))

    def test_names_written_differently(self):
        self.process("/usr/bin/deltachat-desktop")
        self.assertTrue(self.local().running("Delta Chat", "chat.delta.desktop"))

    def test_program_by_its_name(self):
        self.process("/usr/lib/thunderbird/thunderbird", "-contentproc")
        local = self.local()
        self.assertTrue(local.running("Thunderbird"))
        self.assertFalse(local.running("Thunder"))

    def test_flatpak(self):
        self.process("bwrap", "--args", "42", "--", "signal-desktop")
        self.assertFalse(self.local().running("Signal"))
        self.process("/usr/bin/flatpak-spawn", "org.signal.Signal")
        self.assertTrue(self.local().running("Signal", "org.signal.Signal"))

    def test_generic_parts_name_no_app(self):
        self.launcher("org.example.Desktop", "Example", "example")
        self.process("example")
        self.assertFalse(self.local().running("", "chat.delta.desktop"))

    def test_looked_at_again_after_a_while(self):
        now = [0.0]
        local = localapps.LocalApps(dirs=[self.apps], proc=self.proc, clock=lambda: now[0])
        self.assertFalse(local.running("Thunderbird"))
        self.process("thunderbird")
        self.assertFalse(local.running("Thunderbird"))          # still the old look
        now[0] += localapps.CACHE_SECONDS + 1
        self.assertTrue(local.running("Thunderbird"))


if __name__ == "__main__":
    unittest.main()
