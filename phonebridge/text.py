# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""Status of a phone in words - for the tooltip, the menu and the window."""

import re
import time

from .i18n import N_, _

BATTERY_STATES = {
    "charging": N_("charging"),
    "discharging": N_("on battery"),
    "full": N_("full"),
    "empty": N_("empty"),
    "pending-charge": N_("not charging"),
    "pending-discharge": N_("on battery"),
}
TECHNOLOGIES = {"gsm": "2G", "edge": "EDGE", "umts": "3G", "hspa": "3G+",
                "lte": "LTE", "nr": "5G"}
# what the agent and the connection report, in the user's language
ERRORS = (N_("not connected"), N_("connection lost"), N_("connection closed"),
          N_("no modem"), N_("no modem with SMS support"),
          N_("number and text are needed"), N_("value out of range"),
          N_("the setting is read only"),
          N_("the phone's microphone could not be muted"), N_("chatty did not stop"))
DEVICE_STATES = {"online": N_("Connected"), "connecting": N_("Connecting …"),
                 "offline": N_("Not connected")}


def duration(seconds):
    minutes = int(seconds) // 60
    if minutes < 60:
        return _("%d min") % max(1, minutes)
    return _("%d h %d min") % (minutes // 60, minutes % 60)


def battery(status):
    b = (status or {}).get("battery")
    if not b:
        return None
    parts = ["%d %%" % b["percent"], _(BATTERY_STATES.get(b["state"], b["state"]))]
    if b["state"] == "charging" and b.get("time_to_full"):
        parts.append(_("full in %s") % duration(b["time_to_full"]))
    elif b["state"] == "discharging" and b.get("time_to_empty"):
        parts.append(_("%s left") % duration(b["time_to_empty"]))
    return " · ".join(parts)


def network(status):
    n = (status or {}).get("network")
    if not n:
        return None
    if n.get("status") not in ("registered", "roaming"):
        return _("No network")
    parts = [n.get("operator") or "?"]
    if n.get("technology"):
        parts.append(TECHNOLOGIES.get(n["technology"], n["technology"].upper()))
    if n.get("strength") is not None:
        parts.append(_("signal %d %%") % n["strength"])
    if n.get("status") == "roaming":
        parts.append(_("roaming"))
    return " · ".join(parts)


def wifi(status):
    w = (status or {}).get("wifi")
    if not w:
        return None
    if not w.get("enabled"):
        return _("Off")
    if not w.get("ssid"):
        return _("Not connected")
    if w.get("signal") is not None:
        return "%s · %d %%" % (w["ssid"], w["signal"])
    return w["ssid"]


def error(message):
    message = str(message)
    return _(message) if message in ERRORS else message


def device_state(device):
    text = _(DEVICE_STATES.get(device.state, device.state))
    if device.state != "online" and device.error:
        text += " – " + error(device.error)
    return text


def tooltip(device, unread):
    if device is None:
        return "PhoneBridge", _("No phone set up")
    lines = []
    if device.online and device.status:
        for label, value in ((_("Battery"), battery(device.status)),
                             (_("Mobile"), network(device.status)),
                             (_("Wi-Fi"), wifi(device.status))):
            if value:
                lines.append("%s: %s" % (label, value))
    else:
        lines.append(device_state(device))
    if unread:
        lines.append(n_unread(unread))
    return "PhoneBridge – %s" % device.name, "\n".join(lines)


def n_unread(n):
    return (_("%d unread message") if n == 1 else _("%d unread messages")) % n


def activity(timestamp, now=None):
    """When a conversation last moved: today, yesterday, or the date."""
    now = now or time.time()
    t = time.localtime(timestamp)
    today = time.localtime(now)
    yesterday = time.localtime(now - 86400)
    clock = time.strftime("%H:%M", t)
    if t[:3] == today[:3]:
        return _("Today, %s") % clock
    if t[:3] == yesterday[:3]:
        return _("Yesterday, %s") % clock
    return time.strftime(_("%Y-%m-%d, %H:%M"), t)


def n_voicemails(n):
    return (_("%d new voice message") if n == 1 else _("%d new voice messages")) % n


def when_long(timestamp):
    return time.strftime(_("%Y-%m-%d, %H:%M"), time.localtime(timestamp))


def normalize(number, country="49"):
    """The same as agent.normalize - the agent has to stand alone."""
    n = re.sub(r"[\s\-/().]", "", number or "")
    if not re.fullmatch(r"\+?\d+", n):
        return (number or "").strip()
    if n.startswith("00"):
        return "+" + n[2:]
    if n.startswith("0") and country:
        return "+" + country + n[1:]
    return n


def sms_parts(body):
    """(characters, SMS) - 160/153 characters in GSM 7-bit, 70/67 in UCS-2."""
    gsm = all(c in GSM7 for c in body)
    single, multi = (160, 153) if gsm else (70, 67)
    n = len(body) + (sum(1 for c in body if c in GSM7_EXT) if gsm else 0)
    if n <= single:
        return n, 1 if n else 0
    return n, -(-n // multi)


GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
           "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
           "^{}\\[~]|€\f")
GSM7_EXT = set("^{}\\[~]|€\f")
