# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Whether an app of the phone runs on this PC too - then its notifications
come here twice (once of its own, once from the phone), and PhoneBridge
leaves the phone's out.

An app of the phone is known by the name it notifies with ("Element",
"Delta Chat") and its desktop entry ("chat.delta.desktop"); one of this PC
by its launcher (.desktop file): Name, file name and program. It runs when
a process's command line holds its program - or, when the program is only
a script that starts another one (element-desktop: electron with
/usr/lib/element/app.asar), a path the script names."""

import configparser
import os
import re
import shlex
import time

from gi.repository import GLib

CACHE_SECONDS = 30
SCRIPT_BYTES = 4096
# words of a launcher script that tell nothing about the app
SCRIPT_NOISE = {"exec", "env", "sh", "bash", "flatpak", "run", "electron", "python3",
                "python", "java", "true", "false", "then", "else", "fi", "if", "export"}
# last parts of desktop entry ids that name no app (chat.delta.desktop)
GENERIC = {"desktop", "app", "application", "client", "gtk", "qt"}


def key(name):
    """'Delta Chat', 'DeltaChat', 'deltachat' - all one app."""
    return re.sub(r"[^0-9a-z]", "", (name or "").casefold())


def entry_keys(entry):
    """Keys of a desktop entry id: whole ('chatdeltadesktop') and its last
    part ('io.element.Element' -> 'element')."""
    entry = re.sub(r"\.desktop$", "", entry or "")
    keys = {key(entry)}
    if "." in entry:
        keys.add(key(entry.rsplit(".", 1)[1]))
    return {k for k in keys if len(k) > 2 and k not in GENERIC}


def application_dirs():
    data = [GLib.get_user_data_dir()] + list(GLib.get_system_data_dirs())
    data += [os.path.expanduser("~/.local/share/flatpak/exports/share"),
             "/var/lib/flatpak/exports/share"]
    seen, out = set(), []
    for d in data:
        d = os.path.join(d, "applications")
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def launchers(dirs=None):
    """[(keys, needles)] of this PC's launchers: keys name the app, needles
    are what its running process's command line holds."""
    out = []
    for d in application_dirs() if dirs is None else dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".desktop"):
                continue
            parser = configparser.RawConfigParser(strict=False, interpolation=None)
            try:
                parser.read(os.path.join(d, fname), encoding="utf-8")
                section = parser["Desktop Entry"]
            except (configparser.Error, UnicodeDecodeError, KeyError, OSError):
                continue
            if section.get("Type", "Application") != "Application":
                continue
            needles = program_needles(section.get("Exec", ""))
            if not needles:
                continue
            keys = entry_keys(fname) | {key(section.get("Name"))}
            keys |= {key(os.path.basename(n)) for n in needles if "/" not in n}
            for wm in (section.get("StartupWMClass"),):
                if wm:
                    keys.add(key(wm))
            out.append(({k for k in keys if len(k) > 2}, needles))
    return out


def program_needles(exec_line):
    """What a running process of this launcher holds in its command line:
    the program - and, for a launcher script, the paths it names."""
    try:
        words = shlex.split(exec_line)
    except ValueError:
        words = exec_line.split()
    words = [w for w in words if not w.startswith("%")]
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if "=" not in w]
    if not words:
        return set()
    if os.path.basename(words[0]) == "flatpak":
        # flatpak run [options] app.id: the sandbox's process holds the id
        ids = [w for w in words[1:] if not w.startswith("-") and w != "run"]
        return {ids[0]} if ids else set()
    program = words[0]
    path = program if "/" in program else GLib.find_program_in_path(program)
    needles = {os.path.basename(program)}
    if path:
        needles |= script_needles(path)
    return {n for n in needles if n and n not in SCRIPT_NOISE}


def script_needles(path):
    """Paths a launcher script names (exec electron /usr/lib/element/app.asar)."""
    try:
        with open(path, "rb") as f:
            head = f.read(SCRIPT_BYTES)
    except OSError:
        return set()
    if not head.startswith(b"#!"):
        return set()
    text = head.decode("utf-8", "replace")
    out = set()
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line.startswith("exec "):
            continue
        out |= {w for w in re.findall(r"/[\w.+@-]+(?:/[\w.+@-]+)+", line)
                if os.path.basename(w) not in SCRIPT_NOISE}
    return out


def command_lines(proc="/proc"):
    """The command line of every process of this user, words joined by spaces."""
    uid = os.getuid()
    out = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            if os.stat(os.path.join(proc, pid)).st_uid != uid:
                continue
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if raw:
            out.append(raw.replace(b"\0", b" ").decode("utf-8", "replace"))
    return out


def holds(cmdline, needle):
    """Whether a command line holds the program: a whole word or path of it
    ('thunderbird' in '/usr/lib/thunderbird/thunderbird -contentproc')."""
    if "/" in needle:
        return needle in cmdline
    return any(os.path.basename(w) == needle for w in cmdline.split())


class LocalApps:
    """Asked for every notification of the phone; the launchers and the
    processes are looked at again after CACHE_SECONDS."""

    def __init__(self, dirs=None, proc="/proc", clock=time.monotonic):
        self.dirs, self.proc, self.clock = dirs, proc, clock
        self._launchers = self._lines = None
        self._when = -CACHE_SECONDS - 1

    def _fresh(self):
        now = self.clock()
        if now - self._when > CACHE_SECONDS:
            self._launchers = launchers(self.dirs)
            self._lines = command_lines(self.proc)
            self._when = now

    def running(self, app_name, entry=""):
        """Whether the phone's app (its notification's name and desktop
        entry) runs on this PC too."""
        wanted = entry_keys(entry)
        if len(key(app_name)) > 2:
            wanted.add(key(app_name))
        if not wanted:
            return False
        self._fresh()
        for keys, needles in self._launchers:
            if keys & wanted and any(holds(line, n) for line in self._lines for n in needles):
                return True
        return False
