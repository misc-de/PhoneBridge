#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
# Runs every test. No phone, no network, no root: the "phone" is the agent
# running here (tests/fakebin/ssh), with a chatty store made up by the
# tests, and nmcli/wpctl are stand-ins. sudo, pkexec and systemctl fail on
# purpose. Everything runs on a private session bus, so neither a panel
# icon nor a notification reaches your desktop. The window tests build the
# window without showing it and are skipped without a display.
#
#   tests/run-tests.sh                 # everything
#   tests/run-tests.sh test_agent      # one module
#   tests/run-tests.sh -v              # verbose
set -e
cd "$(dirname "$0")/.."
export PATH="$PWD/tests/fakebin:$PATH"
# tests/__init__.py refuses to run without this script
export PHONEBRIDGE_TESTS=1
export PYTHONWARNINGS=ignore::DeprecationWarning
export PHONEBRIDGE_LANGUAGE=en
# never your own address book or picture cache
export PHONEBRIDGE_ADDRESSBOOKS=/nonexistent PHONEBRIDGE_CACHE=/nonexistent
# nor a call history GNOME Calls may have on this PC
export PHONEBRIDGE_CALLS_DB=/nonexistent
# the "phone's" files (the agent runs here): never your home or thumbnails
export PHONEBRIDGE_FILES_HOME="${TMPDIR:-/tmp}/phonebridge-test-phone-$(id -u)"
export PHONEBRIDGE_THUMBNAILS="$PHONEBRIDGE_FILES_HOME/.cache/thumbnails"
rm -rf "$PHONEBRIDGE_FILES_HOME"
mkdir -p "$PHONEBRIDGE_FILES_HOME/Downloads"
# settings the tests change live in memory - never in your dconf
export GSETTINGS_BACKEND=memory
# and passwords in a keyring in memory - never in yours
export PHONEBRIDGE_KEYRING=memory
# caches (pictures for notifications, voice messages) never in yours
export XDG_CACHE_HOME="${TMPDIR:-/tmp}/phonebridge-test-cache-$(id -u)"
rm -rf "$XDG_CACHE_HOME"
# and never your settings: the config (and autostart entry) live in the test
# cache from the start - not only where a test redirects them
export XDG_CONFIG_HOME="$XDG_CACHE_HOME/config"
# and data PhoneBridge keeps on the PC (what the photo backup fetched ...)
export XDG_DATA_HOME="$XDG_CACHE_HOME/data"
export PHONEBRIDGE_CONFIG="$XDG_CONFIG_HOME/phonebridge"
# and never your ~/.ssh: neither the PC's keys nor authorized_keys (the
# agent runs here in the tests)
export PHONEBRIDGE_SSH_DIR="$XDG_CACHE_HOME/ssh"
export PHONEBRIDGE_AUTHORIZED_KEYS="$XDG_CACHE_HOME/phone-ssh/authorized_keys"
# no desktop of its own: nothing probes the desktop's settings services
# (xfconf ...) - they would be started on the private bus
export XDG_CURRENT_DESKTOP=PhoneBridgeTests DESKTOP_SESSION=
# no desktop portals, no gvfs on the private bus - quieter and faster
export GDK_DEBUG=no-portals ADW_DISABLE_PORTAL=1 GIO_USE_VFS=local GTK_A11Y=none
args=()
for a in "$@"; do
    case "$a" in
    test_*) args+=("tests.$a") ;;
    *) args+=("$a") ;;
    esac
done
if [ ${#args[@]} -eq 0 ] || [ "${args[0]}" = -v ]; then
    set -- python3 -X faulthandler -m unittest discover -s tests -t . "${args[@]}"
else
    set -- python3 -X faulthandler -m unittest "${args[@]}"
fi
if command -v dbus-run-session >/dev/null; then
    exec dbus-run-session -- "$@"
fi
exec "$@"
