# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""What the settings page offers of the phone's own settings (GSettings of
GNOME, Phosh, feedbackd, FuriOS and the apps), section by section.

A row: (kind, schema, key, label, extra, options)
  switch  extra None for a boolean, or (value when on, value when off);
          options "invert": the switch says the opposite of the key
  choice  extra ((value, label), ...)
  enum    extra {value: label} for the values of an enum key
  spin    extra (lowest, highest, step, digits); options "unit"
  scale   extra (lowest, highest) - a slider
  flag    extra: one value of a string list (flags or a plain list) - the
          switch adds or removes it
  entry   a text
options "subtitle", "requires" (schema, key) that must be true for the row
to show - e.g. a FuriOS feature the hardware has - and "path" for a
relocatable schema. A key the phone does not have hides its row."""

from .i18n import N_

SECONDS = ((30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min"), (600, "10 min"),
           (900, "15 min"), (1800, "30 min"), (0, N_("Never")))
LOCK_DELAYS = ((0, N_("At once")), (30, "30 s"), (60, "1 min"), (120, "2 min"),
               (300, "5 min"), (900, "15 min"), (1800, "30 min"), (3600, "1 h"))
TEXT_SIZES = ((0.85, N_("Small")), (1.0, N_("Normal")), (1.15, N_("Large")),
              (1.3, N_("Larger")), (1.5, N_("Largest")))
UNITS = {"minutes": N_("minutes"), "hours": N_("hours"), "days": N_("days")}

SECTIONS = (
    {"id": "quick", "title": N_("Quick settings"), "icon": "preferences-system-symbolic",
     "special": "quick"},
    {"id": "pc", "title": N_("Calls at the PC"), "icon": "audio-headset-symbolic",
     "special": "pc"},
    {"id": "appearance", "title": N_("Appearance"), "icon": "preferences-desktop-appearance-symbolic",
     "groups": (
         (None, None, (
             ("switch", "org.gnome.desktop.interface", "color-scheme", N_("Dark style"),
              ("prefer-dark", "default"), {}),
             ("enum", "org.gnome.desktop.interface", "accent-color", N_("Accent colour"), {
                 "blue": N_("Blue"), "teal": N_("Teal"), "green": N_("Green"),
                 "yellow": N_("Yellow"), "orange": N_("Orange"), "red": N_("Red"),
                 "pink": N_("Pink"), "purple": N_("Purple"), "slate": N_("Slate")}, {}),
             ("choice", "org.gnome.desktop.interface", "text-scaling-factor",
              N_("Text size"), TEXT_SIZES, {}),
         )),
         (N_("Top bar"), None, (
             ("switch", "org.gnome.desktop.interface", "show-battery-percentage",
              N_("Battery percentage"), None, {}),
             ("switch", "org.gnome.desktop.interface", "clock-show-weekday",
              N_("Weekday in the clock"), None, {}),
             ("switch", "org.gnome.desktop.interface", "clock-show-date",
              N_("Date in the clock"), None, {}),
             ("switch", "org.gnome.desktop.interface", "clock-show-seconds",
              N_("Seconds in the clock"), None, {}),
             ("enum", "org.gnome.desktop.interface", "clock-format", N_("Clock"),
              {"24h": N_("24 hours"), "12h": N_("12 hours (AM/PM)")}, {}),
         )),
         (N_("Calendar"), None, (
             ("enum", "org.gnome.desktop.calendar", "week-start-day", N_("First day of the week"), {
                 "default": N_("As the language has it"), "monday": N_("Monday"),
                 "tuesday": N_("Tuesday"), "wednesday": N_("Wednesday"),
                 "thursday": N_("Thursday"), "friday": N_("Friday"),
                 "saturday": N_("Saturday"), "sunday": N_("Sunday")}, {}),
             ("switch", "org.gnome.desktop.calendar", "show-weekdate",
              N_("Show week numbers"), None, {}),
         )),
     )},
    {"id": "screen", "title": N_("Screen and power"), "icon": "video-display-symbolic",
     "groups": (
         (N_("Screen"), None, (
             ("switch", "org.gnome.settings-daemon.plugins.power", "ambient-enabled",
              N_("Automatic brightness"), None, {}),
             ("switch", "org.gnome.settings-daemon.plugins.color", "night-light-enabled",
              N_("Night light"), None, {}),
             ("choice", "org.gnome.desktop.session", "idle-delay", N_("Screen off after"),
              SECONDS, {}),
             ("switch", "org.gnome.settings-daemon.plugins.power", "idle-dim",
              N_("Dim the screen first"), None, {}),
             ("switch", "sm.puri.phosh", "automatic-high-contrast",
              N_("High contrast in bright light"), None, {}),
             ("spin", "sm.puri.phosh", "automatic-high-contrast-threshold",
              N_("Bright light from"), (50, 50000, 50, 0), {"unit": "lux"}),
         )),
         (N_("Power"), None, (
             ("switch", "sm.puri.phosh", "enable-suspend", N_("Suspend in the power menu"),
              None, {}),
             ("switch", "org.gnome.settings-daemon.plugins.power",
              "power-saver-profile-on-low-battery", N_("Power saver when the battery runs low"),
              None, {}),
         )),
         (N_("Gestures and sensors"), None, (
             ("switch", "io.furios.gesture", "wake-sensor-enabled", N_("Wake by tapping"),
              None, {}),
             ("switch", "io.furios.gesture", "tilt-sensor-enabled", N_("Wake by lifting"),
              None, {}),
             ("switch", "io.furios.gesture", "glove-mode-enabled", N_("Glove mode"), None,
              {"requires": ("io.furios.gesture", "glove-mode-supported")}),
             ("switch", "io.furios.gesture", "palm-rejection-enabled",
              N_("Ignore the palm of the hand"), None,
              {"requires": ("io.furios.gesture", "palm-rejection-supported")}),
         )),
     )},
    {"id": "lock", "title": N_("Lock screen"), "icon": "system-lock-screen-symbolic",
     "groups": (
         (None, None, (
             ("switch", "org.gnome.desktop.screensaver", "lock-enabled",
              N_("Lock when the screen goes off"), None, {}),
             ("choice", "org.gnome.desktop.screensaver", "lock-delay", N_("Lock after"),
              LOCK_DELAYS, {}),
             ("switch", "sm.puri.phosh.lockscreen", "require-unlock",
              N_("Ask for the PIN to unlock"), None, {}),
             ("switch", "sm.puri.phosh.lockscreen", "shuffle-keypad",
              N_("Shuffle the keypad"), None, {}),
             ("switch", "sm.puri.phosh.emergency-calls", "enabled",
              N_("Emergency calls from the lock screen"), None, {}),
         )),
         (N_("On the lock screen"), None, (
             ("switch", "org.gnome.desktop.notifications", "show-in-lock-screen",
              N_("Notifications"), None, {}),
             ("flag", "sm.puri.phosh.plugins", "lock-screen", N_("Upcoming events"),
              "upcoming-events", {}),
             ("spin", "sm.puri.phosh.plugins.upcoming-events", "days", N_("Days ahead"),
              (1, 31, 1, 0), {}),
             ("switch", "sm.puri.phosh.plugins.upcoming-events", "skip-empty",
              N_("Leave out days without events"), None, {}),
             ("flag", "sm.puri.phosh.plugins", "lock-screen", N_("Tickets"),
              "ticket-box", {}),
             ("flag", "sm.puri.phosh.plugins", "lock-screen", N_("Emergency information"),
              "emergency-info", {}),
         )),
     )},
    {"id": "notifications", "title": N_("Notifications"), "icon": "preferences-system-notifications-symbolic",
     "groups": (
         (None, None, (
             ("switch", "org.gnome.desktop.notifications", "show-banners",
              N_("Notification banners"), None, {}),
             ("switch", "mobi.phosh.shell.cell-broadcast", "enabled",
              N_("Cell broadcast warnings"), None, {}),
         )),
         (N_("Wake the screen"), None, (
             ("flag", "sm.puri.phosh.notifications", "wakeup-screen-triggers",
              N_("For every notification"), "any", {}),
             ("flag", "sm.puri.phosh.notifications", "wakeup-screen-triggers",
              N_("For urgent notifications"), "urgency", {}),
             ("enum", "sm.puri.phosh.notifications", "wakeup-screen-urgency", N_("Urgent is"), {
                 "low": N_("Everything"), "normal": N_("Normal and critical"),
                 "critical": N_("Only critical")}, {}),
         )),
     ),
     "special": "notify_apps"},
    {"id": "sound", "title": N_("Sound and vibration"), "icon": "audio-volume-high-symbolic",
     "groups": (
         (None, None, (
             ("enum", "org.sigxcpu.feedbackd", "profile", N_("Ring profile"), {
                 "full": N_("Sound and vibration"), "quiet": N_("Vibration only"),
                 "silent": N_("Silent")}, {}),
             ("scale", "org.sigxcpu.feedbackd", "max-haptic-strength",
              N_("Vibration strength"), (0.0, 1.0), {}),
             ("switch", "org.sigxcpu.feedbackd", "prefer-flash",
              N_("Camera flash instead of the notification light"), None, {}),
             ("switch", "sm.puri.phosh", "quick-silent", N_("Quick silence"),
              None, {"subtitle": N_("Silence a ringing call with the volume keys")}),
         )),
         (N_("Sounds"), None, (
             ("switch", "org.gnome.desktop.sound", "event-sounds", N_("Event sounds"),
              None, {}),
             ("switch", "org.gnome.desktop.sound", "input-feedback-sounds",
              N_("Key sounds"), None, {}),
             ("switch", "org.gnome.desktop.sound", "allow-volume-above-100-percent",
              N_("Volume above 100 %"), None, {}),
         )),
     )},
    {"id": "calls", "title": N_("Telephone"), "icon": "call-start-symbolic",
     "groups": (
         (N_("Calls (GNOME Calls)"), None, (
             ("switch", "org.gnome.Calls", "auto-use-default-origins",
              N_("Call over the default line by itself"), None, {}),
             ("flag", "org.gnome.Calls", "autoload-plugins", N_("Mobile network calls"),
              "ofono", {}),
             ("flag", "org.gnome.Calls", "autoload-plugins", N_("Internet calls (SIP)"),
              "sip", {}),
             ("switch", "org.gnome.Calls", "always-allow-sdes",
              N_("SIP: allow the unsafe key exchange (SDES)"), None, {}),
         )),
     )},
    {"id": "messages", "title": N_("Messages"), "icon": "mail-unread-symbolic",
     "groups": (
         (N_("Messages (Chatty)"), None, (
             ("switch", "sm.puri.Chatty", "request-sms-delivery-reports",
              N_("Ask for delivery reports"), None, {}),
             ("switch", "sm.puri.Chatty", "return-sends-message", N_("Enter sends"),
              None, {}),
             ("switch", "sm.puri.Chatty", "convert-emoticons",
              N_("Turn :-) into emoji"), None, {}),
             ("switch", "sm.puri.Chatty", "indicate-unknown-contacts",
              N_("Mark unknown senders"), None, {}),
             ("switch", "sm.puri.Chatty", "render-attachments", N_("Show attachments"),
              None, {}),
             ("switch", "sm.puri.Chatty", "strip-url-tracking-id",
              N_("Remove tracking from links"), None, {}),
             ("switch", "sm.puri.Chatty", "clear-out-stuck-sms",
              N_("Clear out stuck SMS"), None, {}),
         )),
         (N_("Chat accounts"), None, (
             ("switch", "sm.puri.Chatty", "send-receipts", N_("Send read receipts"),
              None, {}),
             ("switch", "sm.puri.Chatty", "send-typing", N_("Show when I am typing"),
              None, {}),
             ("switch", "sm.puri.Chatty", "message-carbons",
              N_("Messages on all my devices (carbons)"), None, {}),
             ("switch", "sm.puri.Chatty", "mam-enabled", N_("Load the history from the server"),
              None, {}),
             ("switch", "sm.puri.Chatty", "experimental-features",
              N_("Experimental features"), None, {}),
         )),
     )},
    {"id": "contacts", "title": N_("Contacts"), "icon": "x-office-address-book-symbolic",
     "groups": (
         (N_("Contacts (GNOME Contacts)"), None, (
             ("switch", "org.gnome.Contacts", "sort-on-surname", N_("Sort by last name"),
              None, {}),
         )),
     )},
    {"id": "calendar", "title": N_("Calendar"), "icon": "x-office-calendar-symbolic",
     "groups": (
         (N_("Calendar (GNOME Calendar)"), None, (
             ("enum", "org.gnome.calendar", "active-view", N_("View"), {
                 "month": N_("Month"), "week": N_("Week"), "agenda": N_("Agenda")}, {}),
         )),
         (N_("Reminders"), None, (
             ("switch", "org.gnome.evolution-data-server.calendar", "notify-enable-display",
              N_("Show reminders"), None, {}),
             ("switch", "org.gnome.evolution-data-server.calendar", "notify-enable-audio",
              N_("Play a sound"), None, {}),
             ("switch", "org.gnome.evolution-data-server.calendar", "notify-past-events",
              N_("Also for events already past"), None, {}),
         )),
         (N_("Birthdays and anniversaries"), None, (
             ("switch", "org.gnome.evolution-data-server.calendar",
              "contacts-reminder-enabled", N_("Remind me"), None, {}),
             ("spin", "org.gnome.evolution-data-server.calendar", "contacts-reminder-interval",
              N_("This long before"), (1, 999, 1, 0), {}),
             ("enum", "org.gnome.evolution-data-server.calendar", "contacts-reminder-units",
              N_("Unit"), UNITS, {}),
         )),
         (N_("Default reminder for all events"), None, (
             ("switch", "org.gnome.evolution-data-server.calendar", "defall-reminder-enabled",
              N_("Remind me"), None, {}),
             ("spin", "org.gnome.evolution-data-server.calendar", "defall-reminder-interval",
              N_("This long before"), (1, 999, 1, 0), {}),
             ("enum", "org.gnome.evolution-data-server.calendar", "defall-reminder-units",
              N_("Unit"), UNITS, {}),
         )),
     )},
    {"id": "input", "title": N_("Input"), "icon": "input-keyboard-symbolic",
     "groups": (
         (N_("On-screen keyboard"), None, (
             ("switch", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled",
              N_("On-screen keyboard"), None, {}),
             ("switch", "mobi.phosh.osk", "ignore-hw-keyboards",
              N_("Even with a keyboard attached"), None, {}),
             ("flag", "mobi.phosh.osk", "completion-mode", N_("Word suggestions"),
              "hint", {}),
             ("flag", "mobi.phosh.osk", "osk-features", N_("Enlarge the key pressed"),
              "key-indicator", {}),
             ("flag", "mobi.phosh.osk", "osk-features", N_("Swipe over the keys"),
              "key-drag", {}),
         )),
         (N_("Keyboard size (Squeekboard)"), None, (
             ("spin", "sm.puri.Squeekboard", "scale-in-vertical-screen-orientation",
              N_("Upright"), (0.5, 2.0, 0.1, 1), {}),
             ("spin", "sm.puri.Squeekboard", "scale-in-horizontal-screen-orientation",
              N_("Sideways"), (0.5, 2.0, 0.1, 1), {}),
         )),
     )},
    {"id": "privacy", "title": N_("Privacy and location"), "icon": "preferences-system-privacy-symbolic",
     "groups": (
         (N_("Location"), None, (
             ("switch", "org.gnome.system.location", "enabled", N_("Location services"),
              None, {}),
             ("enum", "org.gnome.system.location", "max-accuracy-level", N_("At most"), {
                 "country": N_("Country"), "city": N_("City"),
                 "neighborhood": N_("Neighbourhood"), "street": N_("Street"),
                 "exact": N_("Exact")}, {}),
         )),
         (N_("Devices"), None, (
             ("switch", "org.gnome.desktop.privacy", "disable-camera",
              N_("Apps may use the camera"), None, {"invert": True}),
             ("switch", "org.gnome.desktop.privacy", "disable-microphone",
              N_("Apps may use the microphone"), None, {"invert": True}),
             ("switch", "io.furios.camera", "camera-background",
              N_("Camera may keep running in the background"), None, {}),
             ("switch", "org.gnome.desktop.privacy", "usb-protection",
              N_("USB protection"), None, {}),
             ("enum", "org.gnome.desktop.privacy", "usb-protection-level",
              N_("Reject new USB devices"), {"lockscreen": N_("While locked"),
                                             "always": N_("Always")}, {}),
         )),
         (N_("History and clean-up"), None, (
             ("switch", "org.gnome.desktop.privacy", "remember-recent-files",
              N_("Remember recently used files"), None, {}),
             ("switch", "org.gnome.desktop.privacy", "remember-app-usage",
              N_("Remember app usage"), None, {}),
             ("switch", "org.gnome.desktop.privacy", "remove-old-trash-files",
              N_("Empty the trash by itself"), None, {}),
             ("switch", "org.gnome.desktop.privacy", "remove-old-temp-files",
              N_("Remove temporary files by themselves"), None, {}),
             ("spin", "org.gnome.desktop.privacy", "old-files-age", N_("After"),
              (1, 365, 1, 0), {"unit": N_("days")}),
         )),
     )},
    {"id": "all", "title": N_("All GNOME settings"), "icon": "system-search-symbolic",
     "special": "browse"},
)


def keys_of(section):
    """[schema, key(, path)] of every row and requirement of a section."""
    out = []
    for _title, _desc, rows in section.get("groups", ()):
        for kind, schema, key, label, extra, opts in rows:
            entry = [schema, key] + ([opts["path"]] if opts.get("path") else [])
            if entry not in out:
                out.append(entry)
            req = opts.get("requires")
            if req and list(req) not in out:
                out.append(list(req))
    return out
