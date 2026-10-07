# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""The application: one process with the panel icon, the connections to
every phone and - when asked for - the window.

  phonebridge                 icon + window
  phonebridge --background    icon only (autostart)
  phonebridge --messages      window, on the messages (likewise --overview,
                              --phone, --contacts, --calendar, --settings)
  phonebridge --quit          ends the running instance

A second start hands its arguments to the running instance."""

import os
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
from .i18n import _  # noqa: E402

RING_SECONDS = 20
PAGES = ("overview", "phone", "messages", "contacts", "calendar", "settings")

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
        self._sms_notes = {}        # (device id, thread) -> notification ids shown
        self._save_password = {}    # device id -> password to keep once it worked
        self._login_asked = set()   # devices whose login dialog the user filled in
        self.lines = {}             # device id -> SIM cards and SIP accounts
        self._pc_wanted = {}        # device id -> when a call to talk at the PC was dialled          # device id -> CallAudio while the PC has the sound
        self._vb_known = {}
        self._was_online = {}

    # -- start ------------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        self.cfg = config.load()
        from .callaudio import unload_leftovers
        run_in_thread(unload_leftovers)     # an echo canceller a crash left behind
        i18n.setup(self.cfg["language"])
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._actions()
        try:
            from .tray import Tray
            self.tray = Tray(self.toggle_window, lambda: self.show_window("messages"),
                             self.on_tray_item)
        except GLib.Error as e:
            print("phonebridge: no panel icon:", e.message, file=sys.stderr)
        self.sync_devices()
        self.update_tray()
        self._watch_sleep_and_network()
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
        if "--quit" in args:
            self.quit()
        elif pages:
            self.show_window(pages[0])
        elif "--background" not in args:
            self.show_window()
        return 0

    def do_shutdown(self):
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
        add("notify", self._on_notify_toggle, None, GLib.Variant("b", self.cfg["notify"]))
        add("autostart", self._on_autostart_toggle, None,
            GLib.Variant("b", config.autostart_enabled()))
        self._language = add("language", self._on_language, "s",
                             GLib.Variant("s", self.cfg["language"]))
        self.set_accels_for_action("app.quit", ["<Control>q"])

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
            if self.devices.get(dev.id) is dev:
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
        n.set_default_action_and_target("app.login", GLib.Variant("s", dev.id))
        self.send_notification("login-%s" % dev.id, n)

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
        if not online and self.calls.get(dev.id):
            self._on_calls(dev, [])
        if not online and dev.id in self.pc_audio:
            self.set_pc_audio(dev, False)
        if not online:
            self.ringing.pop(dev.id, None)
            self._pc_wanted.pop(dev.id, None)
        self._was_online[dev.id] = online
        self.update_tray()
        if self.window is not None:
            self.window.device_changed(dev)

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
        n.set_default_action_and_target("app.open-thread", target)
        n.add_button_with_target(_("Reply"), "app.open-thread", target)
        nid = "sms-%s-%s" % (dev.id, msg["id"])
        self._sms_notes.setdefault((dev.id, msg["thread"]), []).append(nid)
        self._send_with_picture(dev, nid, n, msg["title"], msg.get("avatar"), msg["thread"])

    def _withdraw_sms(self, dev_id, thread):
        """Read or deleted here: its notifications go from the desktop too."""
        for nid in self._sms_notes.pop((dev_id, thread), []):
            self.withdraw_notification(nid)

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
            self.send_notification(nid, notification)
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

        n.set_default_action("app.show-phone")
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
        n.set_priority(Gio.NotificationPriority.URGENT)

        target = GLib.Variant("(ss)", (dev.id, c["path"]))
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
        # the agent finds the nodes later, too (PipeWire after a boot)
        return bool((dev.hello or {}).get("has", {}).get("call_audio")
                    or (dev.status or {}).get("call_audio"))

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
        return next((l for l in lines if l["id"] == wanted), lines[0] if lines else None)

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

    def open_sms(self, number):
        if number:
            self.show_window("messages")
            self.window.messages.open_thread(number)

    def new_contact(self, number):
        self.show_window("contacts")
        self.window.contacts.load()
        self.window.contacts.edit(None, number=number)

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
        elif item_id.startswith("device:"):
            self.set_active(item_id[7:])

    # -- window -----------------------------------------------------------
    def show_window(self, page=None):
        if self.window is None:
            from .window import MainWindow
            self.window = MainWindow(self)
        if page:
            self.window.show_page(page)
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

    def show_devices(self):
        from .devices import DevicesDialog
        DevicesDialog(self).present(self.show_window())

    def show_about(self):
        about = Adw.AboutDialog(
            application_name="PhoneBridge", application_icon=APP_ID,
            version=VERSION, developer_name="misc-de",
            license_type=Gtk.License.MIT_X11,
            comments=_("Your Linux phone in the panel: battery, messages and "
                       "settings of your FuriOS/Phosh phone, over SSH."))
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


def run_in_thread(fn, then=None):
    """fn() in a thread; then(result) back on the GTK thread."""
    import threading

    def work():
        result = fn()
        if then is not None:
            GLib.idle_add(lambda: then(result) and False)

    threading.Thread(target=work, daemon=True).start()


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
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
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
