"""A stand-in for ``gh`` in the scratch world: one canned answer, nothing else.

The lander's drift guard (``land_slice._drift_preflight``, and ``scripts/ship
--quick`` after it) runs ``scripts/lib/last_gated_main_sha.py``, which reads
main's CI verdicts through ``gh api graphql`` and fails closed
(``UNKNOWN_GATE_CROSS``) when it cannot. The scratch remote has no CI, so
this stub answers that one query: every commit of the local first-parent
history of the tree it runs in carries a complete green matrix (``lint`` and
``test-linux (3.13, 1..6)``) completed an hour ago. Any other ``gh`` call
exits 1: the scratch world has no GitHub.

What is NOT covered by this: the drift guard's refusal paths (an old or red
main) and every other GitHub read. Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta


def history_nodes() -> list[dict[str, object]]:
    proc = subprocess.run(
        ["git", "rev-list", "--first-parent", "-n", "100", "origin/main"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    shas = proc.stdout.split() if proc.returncode == 0 else []
    done = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [{"name": "lint", "conclusion": "SUCCESS", "completedAt": done}] + [
        {
            "name": f"test-linux (3.13, {i})",
            "conclusion": "SUCCESS",
            "completedAt": done,
        }
        for i in range(1, 7)
    ]
    return [
        {"oid": sha, "checkSuites": {"nodes": [{"checkRuns": {"nodes": runs}}]}}
        for sha in shas
    ]


def main(argv: list[str]) -> int:
    if argv[:2] == ["api", "graphql"] and not any(
        "object(expression" in a for a in argv
    ):
        page = {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "history": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": history_nodes(),
                            }
                        }
                    }
                }
            }
        }
        print(json.dumps(page))
        return 0
    print(
        "rehearsal: gh is a stand-in; only the drift guard's graphql read is answered",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
