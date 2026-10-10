# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""pytest: the tests run only through tests/run-tests.sh - see tests/__init__.py.
Here so the hint is seen before pytest captures the output."""

import os

import pytest


def pytest_configure(config):
    if not os.environ.get("PHONEBRIDGE_TESTS"):
        pytest.exit("The tests run only through tests/run-tests.sh"
                    " (or tests/coverage.sh), e.g. tests/run-tests.sh test_files",
                    returncode=2)
