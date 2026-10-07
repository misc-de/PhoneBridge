#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
# Installs PhoneBridge for the current user - nothing outside $HOME, and
# nothing on the phone: the app brings its agent along on every connection.
#
#   ./install.sh              program, launcher, icon, start at login
#   NO_AUTOSTART=1 ./install.sh   without starting at login
set -e
cd "$(dirname "$0")"

[ "$(id -u)" -ne 0 ] || { echo "Not as root - this installs into your home."; exit 1; }
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); gi.require_version('Secret','1'); import cairo" 2>/dev/null \
  || { echo "missing: GTK 4, libadwaita, libsecret and pycairo (Manjaro/Arch: pacman -S python-gobject libadwaita libsecret python-cairo)"; exit 1; }
command -v ssh >/dev/null || { echo "missing: ssh (openssh)"; exit 1; }

LOCAL=$HOME/.local
LIB=$LOCAL/lib/phonebridge
ID=io.github.miscde.PhoneBridge

rm -rf "$LIB/phonebridge"
install -d "$LIB/phonebridge" "$LOCAL/bin" "$LOCAL/share/applications"
install -m644 phonebridge/*.py "$LIB/phonebridge/"
install -m755 bin/phonebridge "$LOCAL/bin/"
install -m644 data/$ID.desktop "$LOCAL/share/applications/"
# the app's icon in every size the theme asks for (the earlier SVG and
# sizes no longer shipped go)
rm -f "$LOCAL/share/icons/hicolor/scalable/apps/$ID.svg" \
      "$LOCAL"/share/icons/hicolor/*x*/apps/$ID.png
for png in data/icons/$ID-*.png; do
    size=${png##*-}; size=${size%.png}
    install -D -m644 "$png" "$LOCAL/share/icons/hicolor/${size}x${size}/apps/$ID.png"
done
gtk-update-icon-cache -q -t "$LOCAL/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$LOCAL/share/applications" 2>/dev/null || true

if [ -z "$NO_AUTOSTART" ]; then
    python3 -c "import sys; sys.path.insert(0, '$LIB'); from phonebridge import config; config.set_autostart(True, '$LOCAL/bin/phonebridge')"
fi

# A running instance keeps the old code - start it anew.
if pgrep -u "$(id -u)" -f "$LOCAL/bin/phonebridge" >/dev/null; then
    "$LOCAL/bin/phonebridge" --quit 2>/dev/null || true
    sleep 1
    setsid -f "$LOCAL/bin/phonebridge" --background >/dev/null 2>&1 < /dev/null
fi

echo "PhoneBridge installed. Start it from the menu or with: phonebridge"
