# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The application: one process with the panel icon, the connections to
every phone and - when asked for - the window.

  phonebridge                 icon + window
  phonebridge --background    icon only (autostart)
  phonebridge --messages      window, on the messages (likewise --overview,
                              --phone, --contacts, --calendar, --files,
                              --settings)
  phonebridge --quit          ends the running instance
  phonebridge --send FILE|URL …  to the phone: files to its Downloads, web
                              links into its browser (Thunar's "Send To")
  phonebridge tel:+49…        the number in the dial pad (likewise sms:, callto:
                              - PhoneBridge takes these links on the desktop)

A second start hands its arguments to the running instance."""

import os
import re
import shutil
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, VERSION, config, i18n, icon, phone, text  # noqa: E402
from .avatars import Avatars  # noqa: E402
from .connection import Device  # noqa: E402
from .i18n import _, n_  # noqa: E402
from .localapps import LocalApps  # noqa: E402

RING_SECONDS = 20
PAGES = ("overview", "phone", "messages", "contacts", "calendar", "files", "settings")

CSS = """
.bubble { padding: 6px 10px; border-radius: 14px; }
.bubble-in { background: alpha(currentColor, 0.08); }
.bubble-out { background: @accent_bg_color; color: @accent_fg_color; }
.bubble-out .dim-label { color: alpha(@accent_fg_color, 0.75); opacity: 1; }
.unread-badge { background: @accent_bg_color; color: @accent_fg_color;
                border-radius: 10px; padding: 0 7px; font-weight: bold;
                font-size: smaller; min-width: 8px; }
.thread-unread { font-weight: bold; }
row.fresh { box-shadow: inset 4px 0 #3584e4; }
.call-bar { background: alpha(@accent_bg_color, 0.18); border-radius: 12px;
            padding: 6px 10px; }
"""


class PhoneBridgeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg = None
        self.devices = {}
        self.threads = {}
        self.window = None
        self.tray = None
        self.ringing = {}
        self.avatars = Avatars()
        self.calls = {}
        self.voicebox = {}
        self.pc_audio = {}
        self._ending = False
        self._sms_notes = {}
        self._battery = {}          # device id -> what was last told about the battery        # (device id, thread) -> notification ids shown
        self._save_password = {}    # device id -> password to keep once it worked
        self._login_asked = set()   # devices whose login dialog the user filled in
        self.lines = {}             # device id -> SIM cards and SIP accounts
        self._pc_wanted = {}        # device id -> when a call to talk at the PC was dialled          # device id -> CallAudio while the PC has the sound
        self._vb_known = {}
        self._was_online = {}
        self.media = {}                 # device id -> the phone's players (media_players)
        self.webcams = {}               # device id -> Webcam while the phone is the webcam
        self.music_pc = {}              # device id -> MusicOnPC while its music plays here
        self._music_retry = {}          # device id -> timeout after the stream failed
        self.webcam_listeners = []
        self.rdp_sessions = {}          # device id -> rdp.Desktop while a desktop session runs
        self.rdp_listeners = []
        self.rdp_last_error = {}        # device id -> why the last desktop session ended
        self._clip_last = None          # the clipboard text last passed either way
        self._clip_handler = 0
        self.update_available = None    # {"tag", "sha", "count", "changes"} from update.check
        self.updating = False

    # -- start ------------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        self.cfg = config.load()
        self.local_apps = LocalApps()     # apps of the phone that run here too
        from .callaudio import unload_leftovers
        run_in_thread(unload_leftovers)     # an echo canceller a crash left behind
        i18n.setup(self.cfg["language"])
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)     # window frame, task list
        from . import desktop
        desktop.apply_color_scheme(Adw.StyleManager.get_default())
        self._actions()
        from .backup import Backup
        self.backup = Backup(self)
        try:
            from .tray import Tray
            self.tray = Tray(self.toggle_window, lambda: self.show_window("messages"),
                             self.on_tray_item)
            self.tray.on_hosted = lambda hosted: hosted and self.withdraw_notification("tray")
        except GLib.Error as e:
            print("phonebridge: no panel icon:", e.message, file=sys.stderr)
        GLib.timeout_add_seconds(TRAY_GRACE, lambda: self.check_tray() and False)
        self.sync_devices()
        self.update_tray()
        self._watch_sleep_and_network()
        self._watch_pc_clipboard(self.cfg.get("clipboard_sync", False))
        GLib.timeout_add_seconds(UPDATE_FIRST, lambda: self.look_for_update() and False)
        GLib.timeout_add_seconds(UPDATE_EVERY, lambda: self.look_for_update() or True)
        self.hold()

    def _watch_sleep_and_network(self):
        """After a suspend, or when the network changed, the SSH connections
        may be dead without knowing it: connect anew right away."""
        def reconnect_all(*args):
            for dev in self.devices.values():
                if dev._running:
                    dev.reconnect()

        try:
            system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            system.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager",
                "PrepareForSleep", "/org/freedesktop/login1", None,
                Gio.DBusSignalFlags.NONE,
                lambda c, s, p, i, n, params: (not params.unpack()[0]) and
                GLib.timeout_add_seconds(2, lambda: reconnect_all() and False))
        except GLib.Error:
            pass
        monitor = Gio.NetworkMonitor.get_default()
        self._net_up = monitor.get_network_available()

        def changed(m, available):
            if available and not self._net_up:
                GLib.timeout_add_seconds(2, lambda: reconnect_all() and False)
            self._net_up = available

        monitor.connect("network-changed", changed)

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        pages = [a[2:] for a in args if a[2:] in PAGES]
        links = [a for a in args if parse_link(a) is not None]
        if "--quit" in args:
            self.quit()
        elif "--send" in args:
            from . import send
            rest = args[args.index("--send") + 1:]
            items = [a if send.is_web_link(a) else cmdline.create_file_for_arg(a).get_path()
                     for a in rest]
            send.send(self, items)
            return 0
        elif links:
            self.open_link(links[0])
        elif pages:
            self.show_window(pages[0])
        elif "--background" not in args:
            self.show_window()
        GLib.idle_add(lambda: self.check_dependencies() and False)
        if not self.cfg["devices"] and not getattr(self, "_setup_shown", False):
            # the first start: straight to setting up a phone
            self._setup_shown = True
            GLib.idle_add(lambda: self.show_setup(first=True) and False)
        return 0

    def do_shutdown(self):
        self._ending = True
        for cam in list(self.webcams.values()):
            cam.stop()
        for session in list(self.rdp_sessions.values()):
            session.stop()
        for stream in list(self.music_pc.values()):
            stream.stop()
        for dev_id, audio in list(self.pc_audio.items()):
            self.pc_audio.pop(dev_id, None)
            if audio is not PENDING:
                audio.stop()        # here and now: the app is ending
            dev = self.devices.get(dev_id)
            if dev is not None:
                dev.request("callaudio.mute", {"on": False})    # out before the end
        for dev in self.devices.values():
            dev.stop()
        if self.tray is not None:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def _actions(self):
        def add(name, cb, ptype=None, state=None):
            if state is not None:
                action = Gio.SimpleAction.new_stateful(
                    name, GLib.VariantType(ptype) if ptype else None, state)
            else:
                action = Gio.SimpleAction.new(
                    name, GLib.VariantType(ptype) if ptype else None)
            action.connect("activate" if state is None or ptype else "change-state", cb)
            self.add_action(action)
            return action

        add("show", lambda *a: self.show_window())
        add("quit", lambda *a: self.quit())
        add("open-thread", self._on_open_thread, "(ss)")
        add("call-answer", lambda a, p: self.answer_call(*p.unpack()), "(ss)")
        add("call-hangup", lambda a, p: self.hangup_call(*p.unpack()), "(ss)")
        add("show-phone", lambda *a: self.show_window("phone"))
        add("devices", lambda *a: self.show_devices())
        add("login", lambda a, p: self.ask_password(self.devices.get(p.get_string())), "s")
        add("about", lambda *a: self.show_about())
        add("send", lambda *a: self.ask_send())
        add("search", lambda *a: self.show_search())
        add("screenshot", lambda *a: self.screenshot())
        add("notify", self._on_notify_toggle, None, GLib.Variant("b", self.cfg["notify"]))
        add("autostart", self._on_autostart_toggle, None,
            GLib.Variant("b", config.autostart_enabled()))
        add("phone-notifications", self._on_phone_notifications_toggle, None,
            GLib.Variant("b", bool(self.cfg.get("phone_notifications", True))))
        add("phone-notifications-twice", self._on_phone_notifications_twice_toggle, None,
            GLib.Variant("b", bool(self.cfg.get("phone_notifications_twice", False))))
        add("phone-dismiss", self._on_phone_dismiss, "(su)")
        add("clipboard-sync", self._on_clipboard_sync_toggle, None,
            GLib.Variant("b", bool(self.cfg.get("clipboard_sync", False))))
        add("clipboard-to-phone", lambda *a: self.clipboard_to_phone())
        add("clipboard-from-phone", lambda *a: self.clipboard_from_phone())
        add("updates", self._on_updates_toggle, None,
            GLib.Variant("b", bool(self.cfg["updates"])))
        self._language = add("language", self._on_language, "s",
                             GLib.Variant("s", self.cfg["language"]))
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.search", ["<Control>k"])

    # -- phones -----------------------------------------------------------
    def sync_devices(self):
        """Connections in step with cfg["devices"]."""
        wanted = {d["id"]: d for d in self.cfg["devices"]}
        for dev_id in list(self.devices):
            dev = self.devices[dev_id]
            if dev_id not in wanted or wanted[dev_id] != dev.info:
                dev.stop()
                del self.devices[dev_id]
                self.threads.pop(dev_id, None)
                self._was_online.pop(dev_id, None)
        for dev_id, info in wanted.items():
            if dev_id not in self.devices:
                dev = Device(info)
                dev.connect("changed", self._on_device_changed)
                dev.connect("auth-needed", self._on_auth_needed)
                dev.connect("sms", self._on_sms)
                dev.connect("calls", self._on_calls)
                dev.connect("voicebox", lambda d: self.refresh_voicebox(d))
                dev.connect("media", self._on_media)
                dev.connect("notification", self._on_phone_notification)
                dev.connect("clipboard", self._on_phone_clipboard)
                dev.connect("notification-closed",
                            lambda d, nid: self.withdraw_notification(phone_note_id(d.id, nid)))
                self.devices[dev_id] = dev
                self._start_with_keyring(dev)
        if self.cfg["active"] not in self.devices:
            self.cfg["active"] = next(iter(self.devices), None)
        if self.window is not None:
            self.window.devices_changed()
        self.update_tray()

    def _start_with_keyring(self, dev):
        """Starts a connection - with the password from the keyring, if one
        is kept for this phone (otherwise the key alone)."""
        from . import secrets

        def found(password):
            # the answer may come after the app ended or the phone went
            if self.devices.get(dev.id) is dev and not self._ending:
                dev.password = password
                dev.start()

        secrets.lookup(dev.info, found)

    def set_devices(self, devices):
        from . import secrets
        for old in self.cfg["devices"]:
            same = next((d for d in devices if d["id"] == old["id"]), None)
            if same is None or (same["host"], same["user"], same.get("port")) != (
                    old["host"], old["user"], old.get("port")):
                secrets.clear(old)      # a phone gone (or moved): its password too
        self.cfg["devices"] = devices
        ids = {d["id"] for d in devices}
        # a phone that goes takes its voice messages along from the cache
        try:
            for fn in os.listdir(voicebox_cache()):
                if fn.split("-", 1)[0] not in ids:
                    os.remove(os.path.join(voicebox_cache(), fn))
        except OSError:
            pass
        self.cfg["seen"] = {k: v for k, v in self.cfg["seen"].items() if k in ids}
        if self.cfg["active"] not in ids:
            self.cfg["active"] = devices[0]["id"] if devices else None
        config.save(self.cfg)
        self.sync_devices()

    def active_device(self):
        return self.devices.get(self.cfg["active"])

    def set_active(self, dev_id):
        if dev_id in self.devices and dev_id != self.cfg["active"]:
            self.cfg["active"] = dev_id
            config.save(self.cfg)
            self.update_tray()
            if self.window is not None:
                self.window.active_changed()

    # -- the battery: full, and almost empty -------------------------------------
    def _battery_check(self, dev):
        """Once when the phone is fully charged, once when it falls below 5 %
        (and does not charge). Told again only after the next charge cycle:
        full again counts only once the battery fell below FULL_AGAIN % - on
        the charger a full phone swings between 98 and 100 %, "discharging"
        and "charging", and that is no new charge."""
        bat = (dev.status or {}).get("battery")
        if not bat:
            return
        percent, state = bat.get("percent", 0), bat.get("state")
        charging = state in ("charging", "pending-charge")
        full = state == "full" or (charging and percent >= 100)
        told = self._battery.setdefault(dev.id, {"full": None, "low": False})
        if told["full"] is None:
            told["full"] = full                 # full already when met: nothing to tell
        elif full and not told["full"]:
            told["full"] = True
            self._battery_note(dev, "full", _("%s is fully charged") % dev.name,
                               _("%d %% - the charger can go.") % percent,
                               "battery-full-charged-symbolic")
        elif not full and percent < FULL_AGAIN:
            told["full"] = False                # really used: the next full counts again
        if percent < 5 and not charging and not told["low"]:
            told["low"] = True
            self._battery_note(dev, "low", _("The battery of %s is almost empty") % dev.name,
                               _("Only %d %% left - charge it soon.") % percent,
                               "battery-caution-symbolic")
        elif told["low"] and (charging or percent >= 10):
            told["low"] = False

    def _battery_note(self, dev, kind, title, body, icon_name):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new(icon_name))
        if kind == "low":
            n.set_priority(Gio.NotificationPriority.HIGH)
        self.withdraw_notification("battery-%s-%s" % (dev.id, "low" if kind == "full" else "full"))
        self._send_briefly("battery-%s-%s" % (dev.id, kind), n)

    # -- logging in with a password -------------------------------------------
    def _on_auth_needed(self, dev, wrong):
        """The phone does not take the key (or the password was wrong)."""
        self.update_tray()
        if self.window is not None:
            self.window.device_changed(dev)
        if dev.id in self._login_asked or (
                self.window is not None and self.window.is_visible()
                and self.window.is_active()):
            self.ask_password(dev, wrong)       # the user is right here
            return
        n = Gio.Notification.new(_("Log in to %s") % dev.name)
        n.set_body(_("The phone does not take the SSH key - PhoneBridge needs the password."))
        set_click(n, _("Log in"), "app.login", GLib.Variant("s", dev.id))
        self._send_briefly("login-%s" % dev.id, n)

    def ask_password(self, dev, wrong=False):
        """User name and password for a phone without (working) key; kept in
        the keyring if wanted, once the login has worked."""
        if dev is None:
            return
        self.withdraw_notification("login-%s" % dev.id)
        window = self.show_window()
        info = dev.info
        dialog = Adw.AlertDialog(
            heading=_("Log in to %s") % dev.name,
            body=(_("The password was not accepted.") + "\n\n" if wrong else "")
            + _("%s does not take PhoneBridge's SSH key. Log in with the password "
                "of the phone's user instead.") % info["host"])
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        user = Adw.EntryRow(title=_("User"), text=info["user"])
        password = Adw.PasswordEntryRow(title=_("Password"), activates_default=True)
        keep = Adw.SwitchRow(title=_("Keep in the keyring"), active=True)
        for row in (user, password, keep):
            box.append(row)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("login", _("Log in"))
        dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("login")
        dialog.set_close_response("cancel")

        def check(*args):
            dialog.set_response_enabled("login", bool(password.get_text())
                                        and config.valid_user(user.get_text().strip()))

        user.connect("changed", check)
        password.connect("changed", check)
        check()

        def answered(d, response):
            if response != "login":
                self._login_asked.discard(dev.id)
                return
            self._login_asked.add(dev.id)
            secret = password.get_text()
            target = dev
            name = user.get_text().strip()
            if name != info["user"]:
                devices = [dict(x, user=name) if x["id"] == dev.id else x
                           for x in self.cfg["devices"]]
                self.set_devices(devices)       # a new connection for the new user
                target = self.devices.get(dev.id)
                if target is None:
                    return
            if keep.get_active():
                self._save_password[target.id] = secret
            else:
                self._save_password.pop(target.id, None)
            target.set_password(secret)

        dialog.connect("response", answered)
        dialog.present(window)
        password.grab_focus()

    def forget_password(self, dev):
        from . import secrets
        secrets.clear(dev.info)
        self._save_password.pop(dev.id, None)
        dev.set_password(None)

    def _on_device_changed(self, dev):
        online = dev.online
        if online and dev.id in self._save_password:
            # the password worked: now it goes into the keyring
            from . import secrets
            secret = self._save_password.pop(dev.id)
            secrets.store(dev.info, secret, lambda error: error and self.toast(
                _("The password could not be kept in the keyring: %s") % error))
        if online:
            self._login_asked.discard(dev.id)
            self.withdraw_notification("login-%s" % dev.id)
        if online and not self._was_online.get(dev.id):
            seen = config.seen_for(self.cfg, dev.id)
            if seen["baseline"] is None:
                # what is on the phone already never counts as unread
                seen["baseline"] = (dev.hello or {}).get("sms_last_id", 0)
                config.save(self.cfg)
            self.refresh_threads(dev)
            self.refresh_voicebox(dev)
            self.refresh_lines(dev)
            dev.request("calls.active", {},
                        lambda r, e: e is None and self._on_calls(dev, r))
            dev.request("media.state", {},
                        lambda r, e: e is None and self._on_media(dev, r))
            if self.cfg.get("phone_notifications", True):
                dev.request("notifications.watch", {"on": True})
            if self.cfg.get("clipboard_sync", False):
                dev.request("clipboard.watch", {"on": True})
            self.backup.device_online(dev)
        if not online and self.calls.get(dev.id):
            self._on_calls(dev, [])
        if not online and dev.id in self.pc_audio:
            self.set_pc_audio(dev, False)
        if not online and dev.id in self.webcams:
            self.set_webcam(dev, False)
        if not online and dev.id in self.rdp_sessions:
            self.set_rdp(dev, False)
        if not online and self.media.get(dev.id):
            self._on_media(dev, [])
        if not online:
            self.sync_music(dev)
        if not online:
            self.ringing.pop(dev.id, None)
            self._pc_wanted.pop(dev.id, None)
        if online:
            self._battery_check(dev)
        self._was_online[dev.id] = online
        self.update_tray()
        if self.window is not None:
            self.window.device_changed(dev)

    # -- the phone's notifications ---------------------------------------------
    def _on_phone_notification(self, dev, n):
        """A notification of an app on the phone, here too - briefly, with a
        button that closes it on the phone."""
        if not self.cfg.get("phone_notifications", True):
            return
        app_name = n.get("app") or ""
        if not self.cfg.get("phone_notifications_twice", False) and \
                self.local_apps.running(app_name, n.get("desktop") or ""):
            return          # the app here tells it itself
        title = n.get("title") or app_name or dev.name
        if app_name and app_name != title:
            title = "%s: %s" % (app_name, title)
        note = Gio.Notification.new(title)
        if n.get("body"):
            note.set_body(notification_text(n["body"]))
        note.set_icon(phone_note_icon(n))
        note.add_button_with_target(_("Close on the phone"), "app.phone-dismiss",
                                    GLib.Variant("(su)", (dev.id, int(n["id"]))))
        self._send_briefly(phone_note_id(dev.id, n["id"]), note)

    def _on_phone_notifications_twice_toggle(self, action, value):
        action.set_state(value)
        self.cfg["phone_notifications_twice"] = value.get_boolean()
        config.save(self.cfg)

    def _on_phone_dismiss(self, action, param):
        dev_id, nid = param.unpack()
        self.withdraw_notification(phone_note_id(dev_id, nid))
        dev = self.devices.get(dev_id)
        if dev is not None and dev.online:
            dev.request("notification.close", {"id": nid})

    def _on_phone_notifications_toggle(self, action, value):
        action.set_state(value)
        self.cfg["phone_notifications"] = value.get_boolean()
        config.save(self.cfg)
        for dev in self.devices.values():
            if dev.online:
                dev.request("notifications.watch", {"on": value.get_boolean()})

    # -- the clipboard ----------------------------------------------------------
    def _clipboard(self):
        return Gdk.Display.get_default().get_clipboard()

    def set_pc_clipboard(self, text):
        self._clip_last = text
        self._clipboard().set_content(Gdk.ContentProvider.new_for_value(text))

    def read_pc_clipboard(self, then):
        def done(clipboard, res):
            try:
                value = clipboard.read_text_finish(res)
            except GLib.Error:
                value = None
            then(value)
        self._clipboard().read_text_async(None, done)

    def clipboard_to_phone(self, dev=None):
        dev = dev or self.active_device()
        if dev is None or not dev.online:
            return

        def got(value):
            if not value:
                self.tell(_("The clipboard holds no text"))
                return
            self._clip_last = value
            dev.request("clipboard.set", {"text": value}, lambda r, e: self.tell(
                _("Clipboard sent to %s") % dev.name if e is None
                else _("Not sent: %s") % text.error(e)))

        self.read_pc_clipboard(got)

    def clipboard_from_phone(self, dev=None):
        dev = dev or self.active_device()
        if dev is None or not dev.online:
            return

        def got(result, error):
            if error is not None:
                self.tell(text.error(error))
            elif not result["text"]:
                self.tell(_("The phone's clipboard holds no text"))
            else:
                self.set_pc_clipboard(result["text"])
                self.tell(_("The phone's clipboard is on this PC now"))

        dev.request("clipboard.get", {}, got)

    def _on_phone_clipboard(self, dev, value):
        if self.cfg.get("clipboard_sync", False) and value and value != self._clip_last:
            self.set_pc_clipboard(value)

    def _watch_pc_clipboard(self, on):
        clipboard = self._clipboard()
        if on and not self._clip_handler:
            self._clip_handler = clipboard.connect("changed", self._on_pc_clipboard_changed)
        elif not on and self._clip_handler:
            clipboard.disconnect(self._clip_handler)
            self._clip_handler = 0

    def _on_pc_clipboard_changed(self, clipboard):
        if not clipboard.is_local():        # not what came from the phone
            self.read_pc_clipboard(self.pc_clipboard_text)

    def pc_clipboard_text(self, value):
        """Text copied on the PC: to the phone, while they share it."""
        if (not value or value == self._clip_last or not self.cfg.get("clipboard_sync", False)
                or len(value.encode("utf-8")) > CLIPBOARD_MAX):
            return
        self._clip_last = value
        dev = self.active_device()
        if dev is not None and dev.online:
            dev.request("clipboard.set", {"text": value})

    def _on_clipboard_sync_toggle(self, action, value):
        action.set_state(value)
        on = value.get_boolean()
        self.cfg["clipboard_sync"] = on
        config.save(self.cfg)
        self._watch_pc_clipboard(on)
        for dev in self.devices.values():
            if dev.online:
                dev.request("clipboard.watch", {"on": on})

    # -- the phone as webcam -----------------------------------------------------
    def set_webcam(self, dev, on):
        """Switches the phone's camera on as this PC's webcam (again, with
        the settings as they are) - or off."""
        old = self.webcams.pop(dev.id, None)
        if old is not None:
            old.stop()
        if on and dev.online:
            try:
                from .webcam import Webcam
            except (ImportError, ValueError):
                self.tell(_("GStreamer is missing on this PC"))
                on = False
        if on and dev.online:
            from .webcam import Webcam
            from .webcam_ui import settings
            s = settings(self)
            cam = Webcam(dev, camera=s["camera"], quality=s["quality"], mirror=s["mirror"],
                         **self._webcam_extra)
            cam.connect("stopped", self._webcam_stopped, dev)
            if cam.start():
                self.webcams[dev.id] = cam
                self.tell(_("%(phone)s is the webcam now: %(where)s") % {
                    "phone": dev.name, "where": cam.target if cam.target != "pipewire"
                    else _("a PipeWire camera")})
        for listener in list(self.webcam_listeners):
            listener(dev)
        self.update_tray()

    _webcam_extra = {}      # the tests' camera and sink
    _screen_extra = {}      # the tests' phone side of the screen page
    _rdp_extra = {}         # the tests' phone side and RDP client of the desktop session

    # -- a desktop session of the phone's own (RDP) --------------------------------------
    def set_rdp(self, dev, on, allow_open=False):
        """Starts a GNOME desktop on the phone, shown here in an RDP window -
        or ends it."""
        old = self.rdp_sessions.pop(dev.id, None)
        if old is not None:
            old.stop()
        if on and dev.online:
            from .rdp import Desktop
            from .rdp_ui import settings, state_text
            from .rdp_ui import DEFAULT_SIZE
            size = settings(self).get("desktop_size")
            session = Desktop(dev, size=size or DEFAULT_SIZE, allow_open=allow_open,
                              **self._rdp_extra)
            session.state_text = state_text("starting")
            session.open = False
            session.connect("state", self._rdp_state, dev)
            session.connect("stopped", self._rdp_stopped, dev)
            self.rdp_last_error.pop(dev.id, None)
            if session.start():
                self.rdp_sessions[dev.id] = session
        for listener in list(self.rdp_listeners):
            listener(dev)

    def _rdp_state(self, session, state, dev):
        from .rdp_ui import state_text
        if state == "open":
            session.open = True
        session.state_text = state_text(state)
        for listener in list(self.rdp_listeners):
            listener(dev)

    def _rdp_stopped(self, session, reason, dev):
        if self.rdp_sessions.get(dev.id) is session:
            del self.rdp_sessions[dev.id]
        if reason is not None:
            self.rdp_last_error[dev.id] = text.error(reason)
            self.tell(_("The desktop session ended: %s") % text.error(reason))
        for listener in list(self.rdp_listeners):
            listener(dev)

    def _webcam_stopped(self, cam, reason, dev):
        if self.webcams.get(dev.id) is cam:
            del self.webcams[dev.id]
            if reason is not None:
                self.tell(_("The webcam stopped: %s") % text.error(reason))
            for listener in list(self.webcam_listeners):
                listener(dev)
            self.update_tray()

    # -- music on the phone ---------------------------------------------------
    def _on_media(self, dev, players):
        self.media[dev.id] = players or []
        self.sync_music(dev)
        self.update_tray()
        if self.window is not None and dev is self.active_device():
            self.window.overview.show_media()

    def player(self, dev_id=None):
        """The phone's player that plays (or the first), or None."""
        dev_id = dev_id or (self.active_device().id if self.active_device() else None)
        players = self.media.get(dev_id) or []
        return players[0] if players else None

    def media_control(self, action, dev=None):
        dev = dev or self.active_device()
        p = self.player(dev.id) if dev is not None else None
        if p is None or not dev.online:
            return
        dev.request("media.control", {"bus": p["bus"], "action": action},
                    lambda r, e: e is not None and self.toast(text.error(e)))

    # -- the phone's music on the PC's speakers (music.py) ---------------------
    def music_on_pc(self, dev):
        """Whether the phone's music is to play here - what the user chose."""
        return dev is not None and dev.id in (self.cfg.get("music_on_pc") or [])

    def set_music_on_pc(self, dev, on):
        if dev is None or self.music_on_pc(dev) == bool(on):
            return
        ids = [i for i in (self.cfg.get("music_on_pc") or []) if i != dev.id]
        self.cfg["music_on_pc"] = ids + ([dev.id] if on else [])
        config.save(self.cfg)
        retry = self._music_retry.pop(dev.id, None)
        if retry:
            GLib.source_remove(retry)
        self.sync_music(dev)
        self.update_tray()

    _music_extra = {}       # the tests' phone side and player

    def sync_music(self, dev):
        """The stream there while the phone is online, has a player, and its
        music is to play here; else gone."""
        want = (self.music_on_pc(dev) and dev.online and bool(self.media.get(dev.id))
                and dev.id not in self._music_retry)
        stream = self.music_pc.get(dev.id)
        if want and stream is None:
            from .callaudio import available
            if not available():
                self.tell(_("pw-play is missing on this PC"))
                return
            from .music import MusicOnPC, ending
            if ending():
                # the last stream still hands the players back on the phone
                GLib.timeout_add(150, lambda: self.sync_music(dev) and False)
                return
            stream = MusicOnPC(dev, **self._music_extra)
            stream.connect("stopped", self._music_stopped, dev)
            if stream.start():
                self.music_pc[dev.id] = stream
        elif not want and stream is not None:
            del self.music_pc[dev.id]
            stream.stop()

    def _music_stopped(self, stream, reason, dev):
        if self.music_pc.get(dev.id) is not stream:
            return
        del self.music_pc[dev.id]
        if reason is None or self._ending:
            return
        self.tell(_("The music on the PC stopped: %s") % text.error(reason))

        def again():
            self._music_retry.pop(dev.id, None)
            self.sync_music(dev)
            return False
        # not at once: a phone that keeps failing is asked again in a while
        self._music_retry[dev.id] = GLib.timeout_add_seconds(20, again)

    def refresh_threads(self, dev):
        seen = config.seen_for(self.cfg, dev.id)

        def done(result, error):
            if error is None:
                self.threads[dev.id] = result
                self.update_tray()
                if self.window is not None:
                    self.window.threads_changed(dev)

        dev.request("sms.threads", {"baseline": seen["baseline"] or 0,
                                    "seen": seen["threads"],
                                    "country": self.cfg["country"]}, done)

    def _on_sms(self, dev, new):
        showing = self.window.showing_thread() if self.window is not None else None
        if self.cfg["notify"]:
            for msg in new:
                if showing == (dev.id, msg["thread"]):
                    continue
                self.notify_sms(dev, msg)
        self.refresh_threads(dev)
        if self.window is not None:
            self.window.sms_arrived(dev, new)

    def notify_sms(self, dev, msg):
        title = _("SMS from %s") % msg["title"]
        if len(self.devices) > 1:
            title += " (%s)" % dev.name
        n = Gio.Notification.new(title)
        n.set_body(notification_text(msg["body"]))

        target = GLib.Variant("(ss)", (dev.id, msg["thread"]))
        set_click(n, _("Reply"), "app.open-thread", target)
        nid = "sms-%s-%s" % (dev.id, msg["id"])
        self._sms_notes.setdefault((dev.id, msg["thread"]), []).append(nid)
        self._send_with_picture(dev, nid, n, msg["title"], msg.get("avatar"), msg["thread"])

    def _withdraw_sms(self, dev_id, thread):
        """Read or deleted here: its notifications go from the desktop too."""
        for nid in self._sms_notes.pop((dev_id, thread), []):
            self.withdraw_notification(nid)

    def _send_briefly(self, nid, notification):
        """Sends a notification and takes it back after NOTIFY_SECONDS, so
        it never stays on the desktop (the notification log keeps it)."""
        self.send_notification(nid, notification)
        GLib.timeout_add_seconds(NOTIFY_SECONDS, lambda: self.withdraw_notification(nid)
                                 and False)

    def _send_with_picture(self, dev, nid, notification, name, key=None, thread=None):
        """Sends a notification with the person's picture as its icon: the
        photo from the phone (waiting for it up to 1.5 s), else the look of
        the app's lists - initials on a colour, a silhouette for a number."""
        key = key or next((t.get("avatar") for t in self.threads.get(dev.id, [])
                           if thread and t["thread"] == thread), None)
        sent = []

        def send(texture=None):
            if sent:
                return False
            sent.append(1)
            photo = texture.save_to_png_bytes().get_data() if texture is not None else None
            path = notification_picture(icon.avatar_png(name, 128, photo))
            if path:
                notification.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(path)))
            else:
                notification.set_icon(Gio.ThemedIcon.new(APP_ID))
            self._send_briefly(nid, notification)
            return False

        if key:
            self.avatars.get(dev, key, send)        # at once when it is known
            if not sent:
                GLib.timeout_add(1500, send)
        else:
            send()

    # -- VoiceBox -------------------------------------------------------------
    def refresh_voicebox(self, dev):
        def done(result, error):
            if error is not None:
                return
            known = self._vb_known.get(dev.id)
            if known is not None and self.cfg["notify"]:
                for m in result["messages"]:
                    if m["new"] and m["audio"] and m["id"] not in known:
                        self.notify_voicemail(dev, m)
            self._vb_known[dev.id] = {m["id"] for m in result["messages"]}
            self.voicebox[dev.id] = result
            self.update_tray()
            if self.window is not None:
                self.window.voicebox_changed(dev)

        dev.request("voicebox.list", {"country": self.cfg["country"]}, done)

    def voicemails(self, dev_id):
        """VoiceBox's recordings on that phone (not the calls it answered
        without one)."""
        return [m for m in self.voicebox.get(dev_id, {}).get("messages", []) if m["audio"]]

    def new_voicemails(self, dev_id=None):
        ids = [dev_id] if dev_id else list(self.voicebox)
        return sum(1 for i in ids for m in self.voicemails(i) if m["new"])

    def voicebox_box_name(self, dev_id, box_id):
        if box_id == "global":
            return _("General")
        boxes = self.voicebox.get(dev_id, {}).get("boxes", [])
        return next((b["name"] for b in boxes if b["id"] == box_id), "")

    def notify_voicemail(self, dev, m):
        who = m["name"] or m["number"] or _("Withheld number")
        n = Gio.Notification.new(_("Voice message from %s") % who)
        body = phone.duration(int(round(m["duration"])))
        if len(self.voicebox.get(dev.id, {}).get("boxes", [])) > 1:
            body += " · " + self.voicebox_box_name(dev.id, m["box"])
        n.set_body(body)

        set_click(n, _("Listen"), "app.show-phone")
        self._send_with_picture(dev, "vb-%s-%s" % (dev.id, m["id"]), n, m["name"],
                                m.get("avatar"), m["number"])

    def voicebox_audio(self, dev, mid, callback):
        """callback(path or None, error) - fetched in pieces into a private
        cache, so a long recording never holds up the line."""
        import base64
        from .avatars import private_dir, write_private
        cache = voicebox_cache()
        path = os.path.join(cache, "%s-%s.wav" % (dev.id, mid))
        if os.path.exists(path):
            callback(path, None)
            return
        pieces = []

        def done(result, error):
            if error is not None:
                callback(None, error)
                return
            pieces.append(base64.b64decode(result["data"]))
            got = result.get("offset", 0) + len(pieces[-1])
            if "total" in result and got < result["total"] and pieces[-1]:
                dev.request("voicebox.audio", {"id": mid, "offset": got}, done)
                return
            try:
                private_dir(cache)
                write_private(path, b"".join(pieces))
            except OSError as e:
                callback(None, str(e))
                return
            callback(path, None)

        dev.request("voicebox.audio", {"id": mid}, done)

    def voicebox_read(self, dev, mid):
        for m in self.voicebox.get(dev.id, {}).get("messages", []):
            if m["id"] == mid and m["new"]:
                m["new"] = False
                self.withdraw_notification("vb-%s-%s" % (dev.id, mid))
                self.update_tray()
                if self.window is not None:
                    self.window.voicebox_changed(dev, reload_calls=False)
                dev.request("voicebox.read", {"id": mid})

    def voicebox_delete(self, dev, mid, done=None):
        def deleted(result, error):
            if error is not None:
                self.toast(_("Not deleted: %s") % text.error(error))
            else:
                self.withdraw_notification("vb-%s-%s" % (dev.id, mid))
                cache = os.path.join(voicebox_cache(), "%s-%s.wav" % (dev.id, mid))
                try:
                    os.remove(cache)
                except FileNotFoundError:
                    pass
            self.refresh_voicebox(dev)

        dev.request("voicebox.delete", {"id": mid}, deleted)

    # -- calls --------------------------------------------------------------
    def _on_calls(self, dev, calls):
        old = {c["path"]: c for c in self.calls.get(dev.id, [])}
        now = []
        for c in calls:
            c = dict(c)
            prev = old.get(c["path"])
            if c["state"] == "active":
                c["since"] = prev.get("since") if prev and prev.get("since") else time.time()
            now.append(c)
            if c["state"] in ("incoming", "waiting") and (prev is None or prev["state"] != c["state"]):
                self.notify_call(dev, c)
            elif c["state"] not in ("incoming", "waiting"):
                self.withdraw_notification("call-%s-%s" % (dev.id, c["path"]))
        for path in set(old) - {c["path"] for c in now}:
            self.withdraw_notification("call-%s-%s" % (dev.id, path))
        self.calls[dev.id] = now
        audio = self.pc_audio.get(dev.id)
        wanted = self._pc_wanted.get(dev.id)
        if wanted and time.time() - wanted > 90:
            self._pc_wanted.pop(dev.id, None)       # that call never came about
            wanted = None
        if (wanted and audio is None and any(c["state"] in ("dialing", "alerting", "active")
                                             for c in now)):
            self._pc_wanted.pop(dev.id, None)       # dialled to talk at the PC
            self.set_pc_audio(dev, True)
            audio = self.pc_audio.get(dev.id)
        if audio is not None and not audio.test and not now:
            self.set_pc_audio(dev, False)           # the call is over
        elif (self.cfg["call_audio_auto"] and audio is None and self.call_audio_possible(dev)
              and any(c["state"] == "active" for c in now)
              and not any(prev.get("state") == "active" for prev in old.values())):
            self.set_pc_audio(dev, True)
        if self.window is not None:
            self.window.calls_changed(dev)
        if old and not now and self.window is not None:
            # GNOME Calls writes the history when the call is over
            GLib.timeout_add_seconds(2, self._call_over)
        self.update_tray()

    def _call_over(self):
        if self.window is not None:
            self.window.phone.load(force=True)
            self.window.overview.load(force=True)
        return False

    def notify_call(self, dev, c):
        name = c["name"] or c["number"] or _("Unknown number")
        if c["number"] == "withheld":
            name = _("Withheld number")
        n = Gio.Notification.new(_("Call from %s") % name)
        if len(self.devices) > 1:
            n.set_body(dev.name)
        # not urgent: an urgent notification stays until it is clicked away
        n.set_priority(Gio.NotificationPriority.HIGH)

        target = GLib.Variant("(ss)", (dev.id, c["path"]))
        from . import desktop
        if desktop.click_on_notification():
            n.set_default_action("app.show-phone")
        n.add_button_with_target(_("Answer"), "app.call-answer", target)
        n.add_button_with_target(_("Hang up"), "app.call-hangup", target)
        self._send_with_picture(dev, "call-%s-%s" % (dev.id, c["path"]), n, c["name"],
                                c.get("avatar"), c["number"])

    # -- the call's sound on the PC ---------------------------------------------
    def call_audio_possible(self, dev):
        from . import callaudio
        if dev is None or not dev.online or not callaudio.available():
            return False
        # live from the status (asked every 30 s): PipeWire must run on the
        # phone with its call audio nodes - it can stop or crash any time
        status = dev.status or {}
        if "call_audio" in status:
            return bool(status.get("pipewire", True) and status["call_audio"])
        return bool((dev.hello or {}).get("has", {}).get("call_audio"))

    def call_audio_problem(self, dev):
        """Why calls at the PC are not possible right now, or None."""
        from . import callaudio
        if dev is None or not dev.online:
            return None
        if not callaudio.available():
            return _("pw-record and pw-play are missing on this PC.")
        status = dev.status or {}
        if status.get("pipewire") is False:
            return _("PipeWire is not running on the phone.")
        if not self.call_audio_possible(dev):
            return _("The phone has no call audio channels (droid-call-sink/-source) - "
                     "they come with the patched audio plugin.")
        return None

    def set_pc_audio(self, dev, on, test=False):
        """The call's sound (or, test=True, the phone's own speaker and
        microphone) on the PC - or back on the phone."""
        from .callaudio import CallAudio
        if dev is None:
            return
        current = self.pc_audio.get(dev.id)
        if not on:
            if current is not None:
                del self.pc_audio[dev.id]
                if current is not PENDING:
                    run_in_thread(current.stop)
                if current is PENDING or not current.test:
                    dev.request("callaudio.mute", {"on": False})
            self._pc_audio_changed(dev)
            return
        if current is not None:
            return                      # running, or about to
        self.pc_audio[dev.id] = PENDING

        def go(result=None, error=None):
            if self.pc_audio.get(dev.id) is not PENDING:
                return                  # switched off meanwhile
            if error is None and not test and not (dev.online and self.calls.get(dev.id)):
                error = "the call is over"
            if error is not None:
                del self.pc_audio[dev.id]
                if not test:
                    dev.request("callaudio.mute", {"on": False})
                self.toast(_("The sound stays on the phone: %s") % text.error(error))
                self._pc_audio_changed(dev)
                return
            audio = CallAudio(dev.info, gain=float(self.cfg["call_audio_gain"]),
                              echo_cancel=bool(self.cfg["call_audio_echo"]), test=test,
                              password=dev.password)
            audio.connect("stopped", lambda a, why: self._pc_audio_stopped(dev, a, why))
            self.pc_audio[dev.id] = audio
            # starting means pactl and three programs: not on the GTK thread
            run_in_thread(audio.start, lambda ok: self._pc_audio_changed(dev))

        if test:
            go()
        else:
            # first the phone's microphone off - confirmed - then the stream
            dev.request("callaudio.mute", {"on": True}, go)

    def _pc_audio_stopped(self, dev, audio, why):
        if self.pc_audio.get(dev.id) is audio:
            del self.pc_audio[dev.id]
            if not audio.test:
                dev.request("callaudio.mute", {"on": False})
            if why:
                self.toast(_("Sound on the PC ended: %s") % why)
        self._pc_audio_changed(dev)

    def _pc_audio_changed(self, dev):
        if self.window is not None:
            self.window.calls_changed(dev)
            self.window.settings.quick.update()

    def current_call(self, dev_id):
        calls = self.calls.get(dev_id, [])
        order = ("incoming", "waiting", "active", "dialing", "alerting", "held")
        calls = sorted(calls, key=lambda c: order.index(c["state"]) if c["state"] in order else 9)
        return calls[0] if calls else None

    def answer_call(self, dev_id, call):
        dev = self.devices.get(dev_id)
        path = call["path"] if isinstance(call, dict) else call
        if dev is not None and path:
            dev.request("call.answer", {"path": path},
                        lambda r, e: e is not None and self.toast(text.error(e)))

    def hangup_call(self, dev_id, call):
        dev = self.devices.get(dev_id)
        path = call["path"] if isinstance(call, dict) else call
        if dev is not None and path:
            dev.request("call.hangup", {"path": path},
                        lambda r, e: e is not None and self.toast(text.error(e)))

    # -- lines: SIM cards and SIP accounts -------------------------------------
    def refresh_lines(self, dev):
        def done(result, error):
            if error is None:
                self.lines[dev.id] = result
                if self.window is not None:
                    self.window.lines_changed(dev)

        dev.request("lines.list", {}, done)

    def line_label(self, line):
        if line["kind"] == "sim":
            parts = ["SIM %d" % line["slot"] if line.get("slot") else "SIM"]
            parts += [p for p in (line.get("operator"), line.get("number")) if p]
        else:
            parts = ["SIP", line.get("name") or line.get("address") or line["account"]]
        return " · ".join(parts)

    def chosen_line(self, dev):
        """The line calls go out on: the one chosen for this phone while it
        is there, else the first one."""
        lines = self.lines.get(dev.id, []) if dev else []
        wanted = self.cfg.get("lines", {}).get(dev.id) if dev else None
        return next((line for line in lines if line["id"] == wanted), lines[0] if lines else None)

    def choose_line(self, dev, line_id):
        self.cfg.setdefault("lines", {})[dev.id] = line_id
        config.save(self.cfg)

    def dial(self, number):
        """Calls a number from the phone - after asking whether to talk at
        the PC or at the phone, when the PC can take the sound."""
        dev = self.active_device()
        if dev is None or not number:
            return
        if not self.call_audio_possible(dev) or self.window is None:
            self._dial(dev, number, False)
            return
        dialog = Adw.AlertDialog(heading=_("Call %s") % self._who(dev, number),
                                 body=_("Where do you want to talk?"))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("phone", _("On the phone"))
        dialog.add_response("pc", _("On the PC"))
        dialog.set_response_appearance(self.cfg.get("dial_via", "phone"),
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response(self.cfg.get("dial_via", "phone"))
        dialog.set_close_response("cancel")

        def answered(d, response):
            if response in ("phone", "pc"):
                if self.cfg.get("dial_via") != response:
                    self.cfg["dial_via"] = response
                    config.save(self.cfg)
                self._dial(dev, number, response == "pc")

        dialog.connect("response", answered)
        dialog.present(self.window)

    def _who(self, dev, number):
        contact = self.find_contact(number)
        return contact["name"] if contact and contact.get("name") else number

    def _dial(self, dev, number, on_pc):
        line = self.chosen_line(dev)
        if on_pc:
            # the sound moves to the PC as soon as the call is being set up
            self._pc_wanted[dev.id] = time.time()

        def done(result, error):
            if error is not None:
                self._pc_wanted.pop(dev.id, None)
                self.toast(text.error(error))
            elif line is not None and len(self.lines.get(dev.id, [])) > 1:
                self.toast(_("Calling %s on the phone, over %s …")
                           % (result, self.line_label(line)))
            else:
                self.toast(_("Calling %s on the phone …") % result)

        args = {"number": number, "country": self.cfg["country"]}
        if line is not None:
            args["line"] = line["id"]
        dev.request("call", args, done)

    def open_link(self, uri):
        """tel:, callto: - the number in the dial pad, to call with one
        click (a link clicked by mistake calls nobody); sms: - the
        conversation, with the text the link brings."""
        parsed = parse_link(uri)
        if parsed is None:
            return
        kind, number, body = parsed
        if kind == "sms":
            self.open_sms(number)
            if body and self.window is not None:
                self.window.messages.entry.set_text(body)
                self.window.messages.entry.set_position(-1)
        else:
            self.show_window("phone")
            self.window.phone.show_number(number)

    def open_sms(self, number):
        if number:
            self.show_window("messages")
            self.window.messages.open_thread(number)

    def new_contact(self, number):
        self.show_window("contacts")
        self.window.contacts.load()
        self.window.contacts.edit(None, number=number)

    def open_contact(self, number):
        """The contact with this number on the contacts page - a new one
        with the number when there is none."""
        self.show_window("contacts")
        self.window.contacts.open_number(number)

    def find_contact(self, number):
        if self.window is None:
            return None
        return self.window.contacts.find_number(number)

    def mark_seen(self, dev_id, thread, message_id):
        self._withdraw_sms(dev_id, thread)
        seen = config.seen_for(self.cfg, dev_id)
        if message_id <= seen["threads"].get(thread, 0):
            return
        seen["threads"][thread] = message_id
        config.save(self.cfg)
        changed = False
        for t in self.threads.get(dev_id, []):
            if t["thread"] == thread and t["unread"]:
                t["unread"] = 0
                changed = True
        if changed:
            self.update_tray()
            dev = self.devices.get(dev_id)
            if self.window is not None and dev is not None:
                self.window.threads_changed(dev)

    def forget_thread(self, dev, thread):
        """A conversation deleted on the phone: gone here as well."""
        self._withdraw_sms(dev.id, thread)
        seen = config.seen_for(self.cfg, dev.id)
        if seen["threads"].pop(thread, None) is not None:
            config.save(self.cfg)
        self.threads[dev.id] = [t for t in self.threads.get(dev.id, [])
                                if t["thread"] != thread]
        self.update_tray()
        if self.window is not None:
            self.window.threads_changed(dev)
        self.refresh_threads(dev)

    def unread(self, dev_id=None):
        ids = [dev_id] if dev_id else list(self.threads)
        return sum(t["unread"] for i in ids for t in self.threads.get(i, []))

    def ring(self, dev):
        """Rings the phone, or stops it when it rings."""
        if dev is None:
            return
        rid = self.ringing.pop(dev.id, None)
        if rid is not None:
            dev.request("ring.stop", {"id": rid})
            self._ring_changed(dev)
            return

        def done(result, error):
            if error is not None:
                self.toast(text.error(error))
                return
            self.ringing[dev.id] = result
            self._ring_changed(dev)
            GLib.timeout_add_seconds(RING_SECONDS, self._ring_over, dev, result)

        dev.request("ring", {"seconds": RING_SECONDS}, done)

    def _ring_over(self, dev, rid):
        if self.ringing.get(dev.id) == rid:
            del self.ringing[dev.id]
            self._ring_changed(dev)
        return False

    def _ring_changed(self, dev):
        self.update_tray()
        if self.window is not None:
            self.window.device_changed(dev)

    # -- panel icon -------------------------------------------------------
    def update_tray(self):
        if self.tray is None:
            return
        dev = self.active_device()
        status = dev.status if dev is not None and dev.online else None
        bat = (status or {}).get("battery") or {}
        unread = self.unread() + self.new_voicemails()
        self.tray.set_icon(icon.pixmaps(
            percent=bat.get("percent"), charging=bat.get("state") == "charging",
            online=status is not None, unread=unread))
        title, tip = text.tooltip(dev, self.unread())
        if self.new_voicemails():
            tip += "\n" + text.n_voicemails(self.new_voicemails())
        self.tray.set_tooltip(title, tip)
        self.tray.set_menu(self.menu_items())

    def menu_items(self):
        dev = self.active_device()
        items = []
        if dev is None:
            items.append({"id": "devices", "label": _("Set up a phone …")})
        else:
            head = dev.name
            info = text.battery(dev.status) if dev.online else None
            head += ": " + (info or text.device_state(dev))
            items.append({"label": head, "enabled": False})
            unread = self.unread()
            if unread:
                items.append({"id": "messages", "label": text.n_unread(unread)})
            if self.new_voicemails():
                items.append({"id": "phone", "label": text.n_voicemails(self.new_voicemails())})
            call = self.current_call(dev.id)
            if call is not None:
                who = call["name"] or call["number"] or _("Unknown number")
                items.append({"type": "separator"})
                items.append({"label": "%s: %s" % (phone.call_state(call["state"]), who),
                              "enabled": False})
                if call["state"] in ("incoming", "waiting"):
                    items.append({"id": "answer", "label": _("Answer")})
                items.append({"id": "hangup", "label": _("Hang up")})
        p = self.player(dev.id) if dev is not None and dev.online else None
        if p is not None:
            items.append({"type": "separator"})
            items.append({"label": media_line(p), "enabled": False})
            playing = p["status"] == "Playing"
            items.append({"id": "media:PlayPause", "label": _("Pause") if playing else _("Play"),
                          "enabled": p["can_pause"] if playing else p["can_play"]})
            if p["can_next"]:
                items.append({"id": "media:Next", "label": _("Next track")})
            items.append({"id": "music-output",
                          "label": _("Play on the phone") if self.music_on_pc(dev)
                          else _("Play on this PC")})
        if len(self.devices) > 1:
            items.append({"type": "separator"})
            for d in self.devices.values():
                items.append({"id": "device:" + d.id, "label": d.name,
                              "radio": True, "checked": d is dev})
        items.append({"type": "separator"})
        items.append({"id": "show", "label": _("Open PhoneBridge")})
        if dev is not None:
            online = dev.online
            items += [
                {"id": "messages", "label": _("Messages")},
                {"id": "compose", "label": _("New message …"), "enabled": online},
                {"id": "send", "label": _("Send to phone …"), "enabled": online},
                {"id": "clipboard-to-phone", "label": _("Clipboard to the phone"),
                 "enabled": online},
                {"id": "clipboard-from-phone", "label": _("Clipboard from the phone"),
                 "enabled": online},
                {"id": "screenshot", "label": _("Screenshot of the phone"), "enabled": online},
                {"id": "webcam", "enabled": online,
                 "label": _("Webcam off") if dev.id in self.webcams else _("Phone as webcam")},
                {"id": "ring", "enabled": online,
                 "label": _("Stop ringing") if dev.id in self.ringing
                 else _("Ring the phone")},
            ]
            if dev.needs_password:
                items.append({"id": "login", "label": _("Log in to %s …") % dev.name})
            elif not online:
                items.append({"id": "reconnect", "label": _("Connect now")})
        items += [{"type": "separator"}, {"id": "quit", "label": _("Quit")}]
        return items

    def on_tray_item(self, item_id):
        dev = self.active_device()
        if item_id == "show":
            self.show_window()
        elif item_id == "messages":
            self.show_window("messages")
        elif item_id == "phone":
            self.show_window("phone")
        elif item_id == "compose":
            self.show_window("messages")
            self.window.messages.compose()
        elif item_id in ("answer", "hangup") and dev is not None:
            call = self.current_call(dev.id)
            if call is not None:
                (self.answer_call if item_id == "answer" else self.hangup_call)(dev.id, call)
        elif item_id == "send":
            self.ask_send()
        elif item_id == "webcam" and dev is not None:
            self.set_webcam(dev, dev.id not in self.webcams)
        elif item_id == "screenshot":
            self.screenshot()
        elif item_id == "clipboard-to-phone":
            self.clipboard_to_phone()
        elif item_id == "clipboard-from-phone":
            self.clipboard_from_phone()
        elif item_id == "ring":
            self.ring(dev)
        elif item_id == "reconnect" and dev is not None:
            dev.reconnect()
        elif item_id == "login" and dev is not None:
            self.ask_password(dev)
        elif item_id == "devices":
            self.show_devices()
        elif item_id == "quit":
            self.quit()
        elif item_id.startswith("media:"):
            self.media_control(item_id[6:])
        elif item_id == "music-output" and dev is not None:
            self.set_music_on_pc(dev, not self.music_on_pc(dev))
            if self.window is not None and dev is self.active_device():
                self.window.overview.show_media()
        elif item_id.startswith("device:"):
            self.set_active(item_id[7:])

    # -- window -----------------------------------------------------------
    def check_tray(self):
        """No panel shows the icon: said once per desktop - what would show
        it there; PhoneBridge keeps running in the background meanwhile."""
        from . import desktop
        if self._ending or (self.tray is not None and self.tray.hosted):
            return
        key = desktop.current() or "?"
        if self.cfg.get("tray_hint_told") == key:
            return
        self.cfg["tray_hint_told"] = key
        config.save(self.cfg)
        n = Gio.Notification.new(_("PhoneBridge has no panel icon here"))
        n.set_body(_(desktop.tray_hint(key)) + "\n" + _(
            "PhoneBridge keeps running in the background - open it from the menu of "
            "applications."))
        n.add_button(_("Open PhoneBridge"), "app.show")
        self.send_notification("tray", n)

    def show_window(self, page=None):
        if self.window is None:
            from .window import MainWindow
            self.window = MainWindow(self)
        if page:
            self.window.show_page(page)
        if not self.window.is_visible():
            from . import desktop            # the theme may have changed meanwhile
            desktop.apply_color_scheme(Adw.StyleManager.get_default())
        self.window.present()
        return self.window

    def toggle_window(self):
        if self.window is not None and self.window.is_visible() and self.window.is_active():
            self.window.set_visible(False)
        else:
            self.show_window()

    def toast(self, message):
        if self.window is not None:
            self.window.toast(message)

    def tell(self, message):
        """In the window when it is shown, else as a short notification."""
        if self.window is not None and self.window.is_visible():
            self.window.toast(message)
            return
        n = Gio.Notification.new("PhoneBridge")
        n.set_body(message)
        self._send_briefly("tell-%d" % (hash(message) & 0xffff), n)

    def check_dependencies(self):
        """Optional parts of the PC that are missing: told once per start
        while it is so - again whenever what is missing changes."""
        from . import deps
        if getattr(self, "_deps_checked", False):
            return
        self._deps_checked = True
        missing = deps.missing_optional()
        names = sorted(m[0] for m in missing)
        if not missing or names == self.cfg.get("deps_told"):
            return
        lines = ["• %s: %s" % (m[0], _(m[4])) for m in missing]
        hint = deps.install_hint(missing)
        if hint:
            lines += ["", _("Install with:"), hint]
        body = "\n".join(lines)
        if self.window is not None and self.window.is_visible():
            dialog = Adw.AlertDialog(heading=_("Some parts of PhoneBridge are missing"),
                                     body=body)
            dialog.add_response("later", _("Remind me next time"))
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")

            def answered(d, response):
                if response == "ok":
                    self.cfg["deps_told"] = names
                    config.save(self.cfg)

            dialog.connect("response", answered)
            dialog.present(self.window)
        else:
            n = Gio.Notification.new(_("Some parts of PhoneBridge are missing"))
            n.set_body(body)
            n.add_button(_("Show"), "app.show")
            self._send_briefly("deps", n)

    def show_setup(self, first=False):
        from .setup import SetupDialog
        SetupDialog(self, first=first).present(self.show_window())

    def show_devices(self):
        from .devices import DevicesDialog
        DevicesDialog(self).present(self.show_window())

    def screenshot(self):
        from . import screenshot
        screenshot.take(self)

    def show_search(self):
        from .search import SearchDialog
        dialog = SearchDialog(self)
        dialog.present(self.show_window())
        return dialog

    def ask_send(self):
        from . import send
        return send.ask(self)

    def debug_info(self):
        from . import desktop, update
        return "\n".join((
            "PhoneBridge %s (%s)" % (VERSION, update.installed() or "source tree"),
            "Desktop: %s (%s)" % (desktop.name(), os.environ.get("XDG_CURRENT_DESKTOP", "")),
            "Session: %s" % os.environ.get("XDG_SESSION_TYPE", "?"),
            "Notifications: %s; a click %s" % (
                desktop.notification_server() or "none",
                "is taken" if desktop.click_on_notification() else "shows as a button"),
            "Panel icon: %s" % ("shown" if self.tray is not None and self.tray.hosted
                                else "no panel shows it"),
            "Dark: %s" % Adw.StyleManager.get_default().get_dark()))

    def show_about(self):
        about = Adw.AboutDialog(
            application_name="PhoneBridge", application_icon=APP_ID,
            version=VERSION, developer_name="misc-de",
            license_type=Gtk.License.MIT_X11,
            comments=_("Your Linux phone in the panel: battery, messages and "
                       "settings of your FuriOS/Phosh phone, over SSH."),
            debug_info=self.debug_info())
        about.present(self.show_window())

    def _on_open_thread(self, action, param):
        dev_id, thread = param.unpack()
        self.set_active(dev_id)
        self.show_window("messages")
        self.window.messages.open_thread(thread)

    def _on_notify_toggle(self, action, value):
        action.set_state(value)
        self.cfg["notify"] = value.get_boolean()
        config.save(self.cfg)

    def _on_autostart_toggle(self, action, value):
        action.set_state(value)
        exe = shutil.which("phonebridge") or os.path.realpath(sys.argv[0])
        config.set_autostart(value.get_boolean(), exe)

    def _on_updates_toggle(self, action, value):
        action.set_state(value)
        self.cfg["updates"] = value.get_boolean()
        config.save(self.cfg)
        if self.cfg["updates"]:
            self.look_for_update()
        else:
            self._set_update(None)

    # -- updates ------------------------------------------------------------
    def look_for_update(self):
        """Asks GitHub in a thread; the window shows the result."""
        from . import update
        current = update.installed()
        if current is None or not self.cfg["updates"] or self.updating:
            return

        def ask():
            try:
                return update.check(current)
            except (OSError, ValueError) as e:      # offline, GitHub's limit ...
                print("phonebridge: no update check:", e, file=sys.stderr)
                return False

        run_in_thread(ask, lambda info: info is not False and not self._ending
                      and self._set_update(info))

    def _set_update(self, info):
        self.update_available = info
        if self.window is not None:
            self.window.update_changed()

    def ask_update(self):
        info = self.update_available
        if info is None or self.updating:
            return
        if any(self.current_call(d) for d in self.devices):
            self.toast(_("Update after the call."))
            return
        if info["count"]:
            body = n_("%d change:", "%d changes:", info["count"])
            from .update import SHOWN
            lines = ["• " + c for c in info["changes"][:SHOWN]]
            if len(info["changes"]) > SHOWN:
                lines.append("…")
            body += "\n" + "\n".join(lines)
        else:
            body = _("A new version of PhoneBridge is available.")
        body += "\n\n" + _("PhoneBridge starts anew afterwards.")
        heading = (_("Update PhoneBridge to %s?") % info["tag"] if info.get("tag")
                   else _("Update PhoneBridge?"))
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Not now"))
        dialog.add_response("update", _("Update"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: r == "update" and self.start_update())
        dialog.present(self.show_window())

    def start_update(self):
        from . import update
        info = self.update_available
        if info is None or self.updating:
            return
        self.updating = True
        if self.window is not None:
            self.window.update_changed()
        autostart = config.autostart_enabled()

        def done(error):
            self.updating = False
            if self._ending:
                return
            if error is not None:
                if self.window is not None:
                    self.window.update_changed()
                self.toast(_("The update failed: %s") % error)
                return
            visible = self.window is not None and self.window.is_visible()
            page = self.window.current_page() if visible else None
            update.restart_after(os.getpid(), ["--" + page] if page else ["--background"])
            self.quit()

        run_in_thread(lambda: update.install(info["sha"], autostart), done)

    def _on_language(self, action, value):
        action.set_state(value)
        self.cfg["language"] = value.get_string()
        config.save(self.cfg)
        i18n.setup(self.cfg["language"])
        self.update_tray()
        if self.window is not None:
            page = self.window.current_page()
            visible = self.window.is_visible()
            self.window.destroy()
            self.window = None
            if visible:
                self.show_window(page)


PENDING = object()      # in pc_audio while the phone's microphone is being muted


def phone_note_id(dev_id, nid):
    return "phone-%s-%d" % (dev_id, int(nid))


def phone_note_icon(n):
    """The app's icon when this PC's theme has it, else PhoneBridge's."""
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    except (TypeError, AttributeError):
        theme = None
    for name in (n.get("icon"), n.get("desktop")):
        if name and "/" not in name and theme is not None and theme.has_icon(name):
            return Gio.ThemedIcon.new(name)
    return Gio.ThemedIcon.new(APP_ID)


def media_line(player):
    """♪ title – artist, or the player's name when it tells nothing."""
    what = " – ".join(x for x in (player["title"], player["artist"]) if x)
    return "♪ " + (what or player["identity"])


def parse_link(uri):
    """("tel" | "sms", number, text) of a tel:, callto: or sms: link - or None."""
    from urllib.parse import parse_qs, unquote
    scheme, sep, rest = (uri or "").partition(":")
    scheme = scheme.lower()
    if not sep or scheme not in ("tel", "callto", "sms"):
        return None
    rest = rest.lstrip("/")
    rest, _q, query = rest.partition("?")
    first = unquote(rest).split(",")[0].split(";")[0]
    number = re.sub(r"[^\d+*#]", "", first)
    if not re.search(r"\d", number):
        return None
    body = parse_qs(query).get("body", [""])[0] if scheme == "sms" else ""
    return ("sms" if scheme == "sms" else "tel"), number, body


def run_in_thread(fn, then=None):
    """fn() in a thread; then(result) back on the GTK thread."""
    import threading

    def work():
        result = fn()
        if then is not None:
            GLib.idle_add(lambda: then(result) and False)

    threading.Thread(target=work, daemon=True).start()


NOTIFY_SECONDS = 10
TRAY_GRACE = 15                 # s for a panel to show the icon before the hint
CLIPBOARD_MAX = 1024 * 1024     # bytes of text shared through the clipboard
FULL_AGAIN = 95                 # % the battery must fall below before "full" is told again
UPDATE_FIRST = 60               # s after the start: look for an update
UPDATE_EVERY = 6 * 3600         # and again


def set_click(notification, label, action, target=None):
    """What clicking does. Most notification daemons take the click on the
    notification itself; some (xfce4-notifyd, MATE's) show that as a button
    without text - there it is a button with words only."""
    from . import desktop
    if desktop.click_on_notification():
        if target is None:
            notification.set_default_action(action)
        else:
            notification.set_default_action_and_target(action, target)
    if target is None:
        notification.add_button(label, action)
    else:
        notification.add_button_with_target(label, action, target)


def notification_picture(png, keep=50):
    """A picture for a notification, as a file: GLib hands notification
    servers a file or a theme icon - a picture in memory it silently drops.
    Private, named by its content; only the newest few are kept."""
    import hashlib
    from .avatars import private_dir, write_private
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                     "phonebridge", "notify")
    path = os.path.join(d, hashlib.sha1(png).hexdigest()[:20] + ".png")
    try:
        private_dir(d)
        if not os.path.exists(path):
            write_private(path, png)
        os.utime(path)
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".png")),
                       key=os.path.getmtime)
        for old in files[:-keep]:
            os.remove(old)
    except OSError:
        return None
    return path


def voicebox_cache():
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                        "phonebridge", "voicebox")


def notification_text(body):
    """An SMS as a notification body: notification servers outside GNOME
    (KDE, xfce4-notifyd, dunst ...) read body markup - a stranger's SMS
    must not bring links or formatting of its own."""
    from . import desktop
    if desktop.current() == "gnome":
        return body
    return GLib.markup_escape_text(body)


def quit_running():
    """Ends a running instance without starting one."""
    path = "/" + APP_ID.replace(".", "/")
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            APP_ID, path, "org.gtk.Actions", "Activate",
            GLib.Variant("(sava{sv})", ("quit", [], {})), None,
            Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
    except GLib.Error:
        return 1
    return 0


def enable_stack_dumps():
    """kill -USR1 <pid> writes every thread's Python stack to
    ~/.cache/phonebridge/stacks.txt - to see what a running instance does."""
    import faulthandler
    import signal
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                     "phonebridge")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        f = open(os.path.join(d, "stacks.txt"), "a", encoding="utf-8")
        faulthandler.register(signal.SIGUSR1, file=f, all_threads=True)
        return f
    except (OSError, AttributeError, ValueError):
        return None


def main():
    if "--quit" in sys.argv[1:]:
        return quit_running()
    _dumps = enable_stack_dumps()  # noqa: F841 - kept open for the handler
    # the sender's name in desktop notifications (else the script's name)
    GLib.set_application_name("PhoneBridge")
    return PhoneBridgeApp().run(sys.argv)
