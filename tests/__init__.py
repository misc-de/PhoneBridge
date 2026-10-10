# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# without tests/run-tests.sh there is no stand-in phone (ssh), and the tests
# would touch your own settings, keyring and home: they fail, or worse.
# os._exit: neither unittest nor pytest catch it and bury the hint
if not os.environ.get("PHONEBRIDGE_TESTS"):
    sys.stderr.write("The tests run only through tests/run-tests.sh"
                     " (or tests/coverage.sh), e.g.\n"
                     "    tests/run-tests.sh\n    tests/run-tests.sh test_files\n")
    sys.stderr.flush()
    os._exit(2)
