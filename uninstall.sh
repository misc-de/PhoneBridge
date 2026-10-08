#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
# Removes PhoneBridge. The settings in ~/.config/phonebridge stay unless
# PURGE=1; on the phone, the log of sent messages
# (~/.local/share/phonebridge) stays as well.
set -e
LOCAL=$HOME/.local
ID=io.github.miscde.PhoneBridge
"$LOCAL/bin/phonebridge" --quit 2>/dev/null || true
rm -rf "$LOCAL/lib/phonebridge"
rm -f "$LOCAL/bin/phonebridge" "$LOCAL/share/applications/$ID.desktop" \
      "$LOCAL/share/Thunar/sendto/$ID-sendto.desktop" \
      "$LOCAL/share/icons/hicolor/scalable/apps/$ID.svg" \
      "$LOCAL"/share/icons/hicolor/*x*/apps/$ID.png \
      "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$ID.desktop"
[ -z "$PURGE" ] || rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/phonebridge"
gtk-update-icon-cache -q -t "$LOCAL/share/icons/hicolor" 2>/dev/null || true
update-desktop-database -q "$LOCAL/share/applications" 2>/dev/null || true
echo "PhoneBridge removed."
