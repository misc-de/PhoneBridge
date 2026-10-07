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
export PYTHONWARNINGS=ignore::DeprecationWarning
export PHONEBRIDGE_LANGUAGE=en
# never your own address book or picture cache
export PHONEBRIDGE_ADDRESSBOOKS=/nonexistent PHONEBRIDGE_CACHE=/nonexistent
# settings the tests change live in memory - never in your dconf
export GSETTINGS_BACKEND=memory
# and passwords in a keyring in memory - never in yours
export PHONEBRIDGE_KEYRING=memory
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
