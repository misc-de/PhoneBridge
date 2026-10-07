#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
# Test coverage of the whole suite, the agent's process included (the
# stand-in ssh runs it under coverage). Report on the terminal and as HTML
# in htmlcov/.
set -e
cd "$(dirname "$0")/.."
rc=$(mktemp)
cat > "$rc" <<RC
[run]
source = $PWD/phonebridge
parallel = true
data_file = $PWD/.coverage
[report]
show_missing = false
skip_empty = true
RC
rm -f .coverage .coverage.*
export PHONEBRIDGE_COVERAGE="$rc" COVERAGE_RCFILE="$rc"
# run-tests.sh with unittest under coverage (and without its cd: we are there)
sed -e 's/^    set -- python3 -X faulthandler -m unittest/    set -- python3 -X faulthandler -m coverage run -p --rcfile="$rc" -m unittest/' \
    -e 's/^cd "$(dirname "$0")\/.."$//' tests/run-tests.sh > "$rc.run"
rc="$rc" bash "$rc.run" "$@" || true
python3 -m coverage combine --rcfile="$rc" -q
python3 -m coverage report --rcfile="$rc"
python3 -m coverage html --rcfile="$rc" -q -d htmlcov
rm -f "$rc" "$rc.run"
