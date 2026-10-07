# SPDX-FileCopyrightText: Copyright (c) 2026 misc-de
# SPDX-License-Identifier: MIT
"""For coverage only (tests/coverage.sh): stands in for the bootstrap on the
"phone" - skips the agent's code the PC sends and runs phonebridge/agent.py
from the source tree instead, so coverage sees which lines run."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
n = int(sys.stdin.buffer.readline())
sys.stdin.buffer.read(n)
from phonebridge import agent  # noqa: E402

agent.main()
