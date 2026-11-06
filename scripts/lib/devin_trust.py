#!/usr/bin/env -S uv run python
"""devin_trust.py — CLI for :mod:`precis.utils.devin_trust`.

    devin_trust.py [--file FILE] UNDER [PATH]

The rules (UNDER is a real, non-symlink ``<repo>/.claude/worktrees`` under
a git repo root; PATH equals UNDER or sits inside it; flock + atomic
write; refusal exits 2) live in the package module — ``run_slice``'s
devin-lane preflight reuses the same code (gr570688). The checkout's
``src/`` wins when present; an installed ``precis`` covers any other
invocation context.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if (_SRC / "precis" / "utils" / "devin_trust.py").is_file():
    sys.path.insert(0, str(_SRC))

from precis.utils.devin_trust import main  # noqa: E402  (path setup above)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
