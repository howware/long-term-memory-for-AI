"""Entry point of the project memory helper.

The code lives in the memkit package next to this file (base, rules, schema,
capture, processes, query, reading, core, cli). This script keeps the command
line and the names that hooks, tests and scripts import from `memory`.
"""

from __future__ import annotations

from memkit import *  # noqa: F401,F403
from memkit import Memory, MemoryError  # noqa: F401
from memkit.cli import main

if __name__ == "__main__":
    main()
