# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The desktop PhoneBridge runs on, and what follows from it:

  - the notification daemon: whether a click on a notification is taken
    (GNOME, KDE, Cinnamon, dunst, mako ...) or shown as a button without
    words (xfce4-notifyd, MATE's) - then only buttons with words;
  - light or dark: desktops that do not speak the color-scheme setting
    (Xfce, MATE, LXDE, LXQt ...) have a dark theme instead - its name or
    gtk-application-prefer-dark-theme decides;
  - the panel: where status icons come from when none show (GNOME needs an
    extension, others a tray applet in the panel)."""

import configparser
import os
import subprocess

from gi.repository import Gio, GLib

from .i18n import N_

KNOWN = {"XFCE": "xfce", "KDE": "kde", "PLASMA": "kde", "GNOME": "gnome",
         "GNOME-CLASSIC": "gnome", "UBUNTU": "gnome", "X-CINNAMON": "cinnamon",
         "CINNAMON": "cinnamon", "MATE": "mate", "BUDGIE": "budgie", "BUDGIE-DESKTOP": "budgie",
         "LXQT": "lxqt", "LXDE": "lxde", "PANTHEON": "pantheon", "UNITY": "unity",
         "COSMIC": "cosmic", "DEEPIN": "deepin", "SWAY": "sway", "HYPRLAND": "hyprland",
         "WAYFIRE": "wayfire", "RIVER": "river", "ENLIGHTENMENT": "enlightenment"}
NAMES = {"xfce": "Xfce", "kde": "KDE Plasma", "gnome": "GNOME", "cinnamon": "Cinnamon",
         "mate": "MATE", "budgie": "Budgie", "lxqt": "LXQt", "lxde": "LXDE",
         "pantheon": "Pantheon", "unity": "Unity", "cosmic": "COSMIC", "deepin": "Deepin",
         "sway": "Sway", "hyprland": "Hyprland", "wayfire": "Wayfire", "river": "River",
         "enlightenment": "Enlightenment"}
# these follow the color-scheme setting themselves; the others have dark themes
COLOR_SCHEME_NATIVE = {"gnome", "kde", "cinnamon", "budgie", "pantheon", "cosmic", "unity"}
# notification daemons that show the default action as a button without text
DEFAULT_AS_BUTTON = ("xfce", "mate", "notification-daemon", "notification daemon", "lxqt")
# where status icons come from, when the panel shows none
TRAY_HINTS = {
    "gnome": N_("GNOME shows no status icons by itself. The extension “AppIndicator and "
                "KStatusNotifierItem Support” shows PhoneBridge in the top bar."),
    "xfce": N_("Add the “Status Tray Plugin” to a panel to see PhoneBridge there."),
    "mate": N_("Add the “Notification Area” to a panel to see PhoneBridge there."),
    "lxqt": N_("Add the “Status Notifier” widget to the panel to see PhoneBridge there."),
    "budgie": N_("Add the “Tray” applet to a panel to see PhoneBridge there."),
    "pantheon": N_("Install the “Ayatana Indicators” panel plugin to see PhoneBridge there."),
}
GENERIC_TRAY_HINT = N_("Your panel shows no status icons - add a tray (status notifier) "
                       "to it to see PhoneBridge there.")


def current(env=None):
    """"xfce", "kde", "gnome" ... or "" when unknown."""
    env = os.environ if env is None else env
    for part in (env.get("XDG_CURRENT_DESKTOP") or "").split(":"):
        key = KNOWN.get(part.strip().upper())
        if key:
            return key
    session = (env.get("DESKTOP_SESSION") or "").lower()
    for word, key in (("plasma", "kde"), ("kde", "kde"), ("xfce", "xfce"), ("gnome", "gnome"),
                      ("cinnamon", "cinnamon"), ("mate", "mate"), ("lxqt", "lxqt"),
                      ("lxde", "lxde"), ("budgie", "budgie")):
        if word in session:
            return key
    return ""


def name(key=None):
    key = current() if key is None else key
    return NAMES.get(key, key or "?")


# -- notifications --------------------------------------------------------------
_server = {}


def notification_server():
    """The notification daemon's name, lower case ("" when none answers)."""
    if "name" not in _server:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            v = bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                              "org.freedesktop.Notifications", "GetServerInformation", None,
                              GLib.VariantType("(ssss)"), Gio.DBusCallFlags.NO_AUTO_START,
                              1500, None)
            _server["name"] = v.unpack()[0].lower()
        except GLib.Error:
            _server["name"] = ""
    return _server["name"]


def click_on_notification(desktop=None, server=None):
    """Whether a click on the notification itself is taken - else it shows
    as a button without words, and only buttons with words are used."""
    desktop = current() if desktop is None else desktop
    if desktop == "gnome":
        return True             # GNOME Shell takes GNotifications itself
    server = notification_server() if server is None else server
    return bool(server) and not any(s in server for s in DEFAULT_AS_BUTTON)


# -- light or dark ------------------------------------------------------------------
def _run(*argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _gsetting(schema, key):
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(schema, True) is None:
        return None
    try:
        return Gio.Settings.new(schema).get_value(key).unpack()
    except (TypeError, GLib.Error):
        return None


def _gtk_ini():
    """gtk-theme-name and gtk-application-prefer-dark-theme of settings.ini."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for version in ("gtk-4.0", "gtk-3.0"):
        ini = configparser.ConfigParser(interpolation=None)
        try:
            ini.read(os.path.join(base, version, "settings.ini"), encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        if ini.has_section("Settings"):
            s = ini["Settings"]
            return (s.get("gtk-theme-name", ""),
                    s.get("gtk-application-prefer-dark-theme", "").strip().lower()
                    in ("1", "true", "yes"))
    return "", False


def theme(desktop=None):
    """(the desktop's GTK theme name, prefer dark)."""
    desktop = current() if desktop is None else desktop
    name_ = ""
    if desktop == "xfce":
        name_ = _run("xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName")
    elif desktop == "mate":
        name_ = _gsetting("org.mate.interface", "gtk-theme") or ""
    ini_name, prefer_dark = _gtk_ini()
    name_ = name_ or ini_name or _gsetting("org.gnome.desktop.interface", "gtk-theme") or ""
    return name_, prefer_dark


def wants_dark(desktop=None, scheme=None, theme_info=None):
    """On a desktop without the color-scheme setting: True for a dark theme,
    False for a light one; None where the color-scheme setting decides."""
    desktop = current() if desktop is None else desktop
    if desktop in COLOR_SCHEME_NATIVE:
        return None
    scheme = _gsetting("org.gnome.desktop.interface", "color-scheme") if scheme is None \
        else scheme
    if scheme in ("prefer-dark", "prefer-light"):
        return None             # set on purpose (Manjaro does): it decides
    theme_name, prefer_dark = theme(desktop) if theme_info is None else theme_info
    return prefer_dark or "dark" in theme_name.lower()


def apply_color_scheme(style_manager):
    """Follows the desktop's dark theme where libadwaita would not see it."""
    from gi.repository import Adw
    dark = wants_dark()
    if dark is not None:
        style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK if dark
                                       else Adw.ColorScheme.DEFAULT)
    return dark


# -- the panel ------------------------------------------------------------------------
def tray_hint(desktop=None):
    desktop = current() if desktop is None else desktop
    return TRAY_HINTS.get(desktop, GENERIC_TRAY_HINT)
