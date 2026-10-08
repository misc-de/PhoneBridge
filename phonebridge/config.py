# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Settings of the PC side, in ~/.config/phonebridge/config.json.

  devices   the phones: id, name, host, user, port
  active    id of the phone the panel icon shows
  seen      per phone: "baseline" (newest SMS when PhoneBridge first met
            the phone - older ones never count as unread) and "threads"
            (newest SMS per conversation that PhoneBridge has shown)
  country   calling code for numbers typed with a leading 0
  notify    desktop notification for every new SMS
  language  "system", "en" or "de"
  phone_notifications  the phone's own notifications (all apps) on the desktop
  phone_notifications_twice  them too for apps that run on this PC as well
            (off: those come here once, of their own)
  clipboard_sync  the clipboard shared with the phone by itself (off: by hand)
  updates   look on GitHub for a newer PhoneBridge (installed ones only)
  call_audio_*  the call's sound on the PC: gain for the caller, echo
            cancellation, take every call to the PC by itself
"""

import copy
import json
import os
import re

from . import APP_ID

CONFIG_DIR = os.environ.get("PHONEBRIDGE_CONFIG") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "phonebridge")
AUTOSTART = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "autostart", APP_ID + ".desktop")

DEFAULTS = {
    "language": "system",
    "country": "49",
    "notify": True,
    "active": None,
    "devices": [],
    "seen": {},
    "updates": True,
    "phone_notifications": True,    # the notifications of the phone's apps, here too
    "phone_notifications_twice": False,     # ... also of apps running here (localapps.py)
    "clipboard_sync": False,        # the clipboard (text) shared with the phone, both ways
    "music_on_pc": [],          # devices whose music plays on this PC (music.py)
    "files_zoom": 2,            # size of the icons in the file list (files_page.ZOOM)
    # the call's sound on the PC (callaudio.py)
    "call_audio_gain": 2.0,
    "call_audio_echo": True,
    "call_audio_auto": False,
}


def path():
    return os.path.join(CONFIG_DIR, "config.json")


def load():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    cfg["devices"] = [d for d in cfg["devices"] if valid_device(d)]
    ids = [d["id"] for d in cfg["devices"]]
    if cfg["active"] not in ids:
        cfg["active"] = ids[0] if ids else None
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path())


USER = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")
HOST = re.compile(r"[A-Za-z0-9.:\[\]_][A-Za-z0-9.:\[\]_-]{0,253}")


def valid_user(user):
    return bool(USER.fullmatch(user or ""))


def valid_host(host):
    """A host name or address - nothing ssh could take for an option."""
    return bool(HOST.fullmatch(host or ""))


def valid_port(port):
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


# a device may also carry "hotspot_ssid": the phone's hotspot, kept on this PC

def valid_device(d):
    return (isinstance(d, dict) and bool(d.get("id")) and valid_host(d.get("host"))
            and valid_user(d.get("user")) and valid_port(d.get("port") or 22)
            and re.fullmatch(r"[a-z0-9-]+", d["id"]) is not None)


def new_device_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "phone").lower()).strip("-") or "phone"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = "%s-%d" % (base, n), n + 1
    return candidate


def seen_for(cfg, device_id):
    return cfg["seen"].setdefault(device_id, {"baseline": None, "threads": {}})


def autostart_enabled():
    return os.path.exists(AUTOSTART)


def set_autostart(on, exec_path="phonebridge"):
    if not on:
        try:
            os.remove(AUTOSTART)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    with open(AUTOSTART, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=PhoneBridge\n"
                "Exec=%s --background\nIcon=%s\nX-GNOME-Autostart-enabled=true\n"
                "NoDisplay=true\n" % (exec_path, APP_ID))
