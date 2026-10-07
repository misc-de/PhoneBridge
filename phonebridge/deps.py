# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""What PhoneBridge needs on the PC - checked at every start.

Required: without it the app cannot run, so bin/phonebridge says so before
any window (zenity, notify-send or xmessage - whatever is there - and the
terminal). Optional: a part of the app goes without it; the app tells at
start which, and what to install.

Only the standard library at the top: this runs before GTK is known to be
there."""

import importlib
import os
import shutil
import subprocess
import sys

from .i18n import N_, _

# (what, how to check, Arch/Manjaro package, Debian/Ubuntu package)
REQUIRED = (
    ("PyGObject (GTK 4)", ("gi", "Gtk", "4.0"), "python-gobject gtk4", "python3-gi gir1.2-gtk-4.0"),
    ("libadwaita", ("gi", "Adw", "1"), "libadwaita", "gir1.2-adw-1"),
    ("pycairo", ("module", "cairo"), "python-cairo", "python3-cairo"),
    ("OpenSSH (ssh)", ("program", "ssh"), "openssh", "openssh-client"),
)
# (what, check, Arch, Debian, what goes without it)
OPTIONAL = (
    ("libsecret", ("gi", "Secret", "1"), "libsecret", "gir1.2-secret-1",
     N_("Passwords for phones without SSH key cannot be kept in the keyring.")),
    ("pw-record / pw-play", ("program", "pw-record", "pw-play"), "pipewire", "pipewire-bin",
     N_("No calls at the PC: the call's sound cannot be played and recorded here.")),
    ("pactl", ("program", "pactl"), "libpulse", "pulseaudio-utils",
     N_("No echo cancellation for calls at the PC.")),
    ("ssh-keygen", ("program", "ssh-keygen"), "openssh", "openssh-client",
     N_("No SSH key can be made when setting up a phone.")),
)


def _ok(check):
    kind = check[0]
    if kind == "program":
        return all(shutil.which(p) for p in check[1:])
    if kind == "module":
        try:
            importlib.import_module(check[1])
            return True
        except ImportError:
            return False
    try:                                        # "gi", namespace, version
        import gi
        gi.require_version(check[1], check[2])
        importlib.import_module("gi.repository." + check[1])
        return True
    except (ImportError, ValueError):
        return False


def distro():
    """"arch" (Manjaro too), "debian" (Ubuntu, Mobian ...) or None."""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            text = f.read().lower()
    except OSError:
        return None
    if any(w in text for w in ("arch", "manjaro", "endeavouros")):
        return "arch"
    if any(w in text for w in ("debian", "ubuntu", "mobian", "furios")):
        return "debian"
    return None


def missing_required():
    return [r for r in REQUIRED if not _ok(r[1])]


def missing_optional():
    return [r for r in OPTIONAL if not _ok(r[1])]


def install_hint(entries):
    """The command that installs them, for this distribution - or None."""
    d = distro()
    if not entries or d is None:
        return None
    packages = []
    for e in entries:
        for p in (e[2] if d == "arch" else e[3]).split():
            if p not in packages:
                packages.append(p)
    return ("sudo pacman -S " if d == "arch" else "sudo apt install ") + " ".join(packages)


def required_message(missing):
    lines = [_("PhoneBridge cannot start - missing:"), ""]
    lines += ["  - " + m[0] for m in missing]
    hint = install_hint(missing)
    if hint:
        lines += ["", _("Install with:"), "  " + hint]
    return "\n".join(lines)


def tell(message):
    """Says it without GTK: a dialog of whatever there is, and the terminal."""
    print(message, file=sys.stderr)
    for argv in (["zenity", "--error", "--title=PhoneBridge", "--no-markup",
                  "--text=" + message],
                 ["notify-send", "-a", "PhoneBridge", "-u", "critical", "PhoneBridge", message],
                 ["xmessage", "-center", message]):
        if shutil.which(argv[0]) and (os.environ.get("DISPLAY")
                                      or os.environ.get("WAYLAND_DISPLAY")):
            try:
                subprocess.run(argv, timeout=600)
                return
            except (OSError, subprocess.TimeoutExpired):
                continue


def check_required_or_exit():
    missing = missing_required()
    if missing:
        tell(required_message(missing))
        sys.exit(1)
