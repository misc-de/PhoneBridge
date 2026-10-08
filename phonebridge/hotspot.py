# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The phone's hotspot on the PC's side.

On the road there is no shared Wi-Fi, and so no way to the phone to
switch its hotspot on - that is done on the phone. What the PC can do:
keep the hotspot among its own networks with a low priority, so that
NetworkManager joins it by itself whenever it is on and no other known
Wi-Fi is there; and reach the phone through it - at the hotspot's
gateway address instead of the phone's usual one."""

import re
import subprocess
import time
import uuid

from gi.repository import Gio, GLib

PREFIX = "PhoneBridge: "
_cache = {"at": 0.0, "ssid": None, "gateway": None}
CACHE_SECONDS = 10


def profile_name(ssid):
    return PREFIX + ssid


def _run(*argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def pc_has_profile(ssid):
    names = _run("nmcli", "-t", "-f", "NAME", "connection", "show").splitlines()
    return profile_name(ssid) in [n.replace("\\:", ":") for n in names]


def pc_add_profile(ssid, password, done):
    """The hotspot among this PC's networks (NetworkManager over D-Bus - the
    password never on a command line), autoconnect with a low priority.
    done(error or None)."""
    def s(v):
        return GLib.Variant("s", v)

    settings = {
        "connection": {"id": s(profile_name(ssid)), "type": s("802-11-wireless"),
                       "uuid": s(str(uuid.uuid4())), "autoconnect": GLib.Variant("b", True),
                       "autoconnect-priority": GLib.Variant("i", -10)},
        "802-11-wireless": {"ssid": GLib.Variant("ay", ssid.encode("utf-8")),
                            "mode": s("infrastructure")},
        "802-11-wireless-security": {"key-mgmt": s("wpa-psk"), "psk": s(password)},
        "ipv4": {"method": s("auto")}, "ipv6": {"method": s("auto")},
    }
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    except GLib.Error as e:
        done(e.message)
        return

    def answered(conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            Gio.DBusError.strip_remote_error(e)
            done(e.message)
            return
        done(None)

    bus.call("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings",
             "org.freedesktop.NetworkManager.Settings", "AddConnection",
             GLib.Variant("(a{sa{sv}})", (settings,)), GLib.VariantType("(o)"),
             Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 60000, None, answered)


def pc_wifi_ssid():
    """The Wi-Fi this PC is in, or None."""
    for line in _run("nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list").splitlines():
        fields = re.split(r"(?<!\\):", line)
        if fields and fields[0] == "yes":
            return fields[-1].replace("\\:", ":")
    return None


def default_gateway():
    m = re.search(r"^default via (\S+)", _run("ip", "route", "show", "default"), re.M)
    return m.group(1) if m else None


def reach(info):
    """The device as ssh should reach it: through the hotspot's gateway
    while this PC is in the phone's hotspot."""
    ssid = info.get("hotspot_ssid")
    if not ssid:
        return info
    now = time.monotonic()
    if now - _cache["at"] > CACHE_SECONDS:
        _cache.update(at=now, ssid=pc_wifi_ssid(), gateway=default_gateway())
    if _cache["ssid"] == ssid and _cache["gateway"]:
        return dict(info, host=_cache["gateway"])
    return info
