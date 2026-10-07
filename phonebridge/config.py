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


def valid_device(d):
    return (isinstance(d, dict) and d.get("id") and d.get("host")
            and d.get("user"))


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
