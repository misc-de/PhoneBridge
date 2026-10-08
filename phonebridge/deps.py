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


# Fedora and openSUSE, by what the entries above call the thing
OTHER = {
    "PyGObject (GTK 4)": ("python3-gobject gtk4", "python3-gobject typelib-1_0-Gtk-4_0"),
    "libadwaita": ("libadwaita", "typelib-1_0-Adw-1"),
    "pycairo": ("python3-cairo", "python3-pycairo"),
    "OpenSSH (ssh)": ("openssh-clients", "openssh-clients"),
    "libsecret": ("libsecret", "typelib-1_0-Secret-1"),
    "pw-record / pw-play": ("pipewire-utils", "pipewire-tools"),
    "pactl": ("pulseaudio-utils", "pulseaudio-utils"),
    "ssh-keygen": ("openssh", "openssh-clients"),
}
COMMANDS = {"arch": "sudo pacman -S ", "debian": "sudo apt install ",
            "fedora": "sudo dnf install ", "suse": "sudo zypper install "}


def distro(path="/etc/os-release"):
    """"arch" (Manjaro too), "debian" (Ubuntu, Mobian ...), "fedora",
    "suse" or None."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().lower()
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    ids = " ".join(fields.get(k, "").strip('"') for k in ("id", "id_like"))
    for words, key in ((("arch", "manjaro", "endeavouros"), "arch"),
                       (("debian", "ubuntu", "mobian", "furios"), "debian"),
                       (("fedora", "rhel", "centos"), "fedora"),
                       (("suse", "opensuse"), "suse")):
        if any(w in ids.split() or w in ids for w in words):
            return key
    return None


def packages(entry, d):
    if d == "arch":
        return entry[2]
    if d == "debian":
        return entry[3]
    pair = OTHER.get(entry[0])
    return (pair[0] if d == "fedora" else pair[1]) if pair else ""


def missing_required():
    return [r for r in REQUIRED if not _ok(r[1])]


def missing_optional():
    return [r for r in OPTIONAL if not _ok(r[1])]


def install_hint(entries):
    """The command that installs them, for this distribution - or None."""
    d = distro()
    if not entries or d is None:
        return None
    wanted = []
    for e in entries:
        for p in packages(e, d).split():
            if p not in wanted:
                wanted.append(p)
    return COMMANDS[d] + " ".join(wanted)


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
    kde = "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    dialogs = [["zenity", "--error", "--title=PhoneBridge", "--no-markup", "--text=" + message],
               ["kdialog", "--title", "PhoneBridge", "--error", message]]
    if kde:
        dialogs.reverse()           # KDE's own dialog first
    for argv in dialogs + [
                 ["notify-send", "-a", "PhoneBridge", "-u", "critical", "PhoneBridge", message],
                 ["xmessage", "-center", message]]:
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
