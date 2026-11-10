#!/usr/bin/env -S uv run python
"""``scripts/ship --quick``'s mypy leg: type-check the changed files only.

``mypy <files>`` follows imports, so it also reports errors in modules the
files merely import. A quick land must not be blocked by a red it did not
write (those wait for the release gate's full ``mypy src tests``), so this
runs mypy with the project config as-is and fails only on diagnostics whose
path is one of the files named on the command line.

Usage: ``python scripts/lib/mypy_changed.py FILE...`` (run from the repo
root, as the gate does). Exit 0: no error in the named files. Exit 1: at
least one (printed, with its ``note:`` / source-context lines). Exit 2: mypy
itself failed (crash / usage error / timeout) -- not a verdict, never swallowed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable

#: ``path:line: error: ...`` / ``path:line:col: note: ...``.
_DIAG = re.compile(r"^(?P<path>[^\s:][^:]*):\d+(?::\d+)?: (?P<sev>error|note|warning):")


#: Hard cap on the mypy subprocess; a timeout is exit 2, never a verdict.
_MYPY_TIMEOUT_S = 600


def _norm(path: str) -> str:
    return os.path.normpath(path)


def filter_diagnostics(output: str, changed: Iterable[str]) -> list[str]:
    """The mypy output lines that belong to ``changed`` files.

    A diagnostic line is kept when its path is in ``changed``, with the
    indented continuation lines under it. Errors (and their notes) in
    imported, unchanged modules and the ``Found N errors`` summary are
    dropped. Returns lines only when at least one ``error:`` survives; a
    stray note alone is not a red.
    """
    want = {_norm(p) for p in changed}
    kept: list[str] = []
    errors = 0
    keeping = False
    for line in output.splitlines():
        m = _DIAG.match(line)
        if m is None:
            if keeping and line.startswith((" ", "\t")):
                kept.append(line)
            continue
        keeping = _norm(m.group("path")) in want
        if keeping:
            kept.append(line)
            errors += m.group("sev") == "error"
    return kept if errors else []


def main(argv: list[str]) -> int:
    files = argv[1:]
    if not files:
        print("mypy_changed: no files given -- nothing to check")
        return 0
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", *files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_MYPY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"mypy_changed: mypy timed out after {_MYPY_TIMEOUT_S}s")
        return 2
    out = proc.stdout + proc.stderr
    if proc.returncode not in (0, 1):
        sys.stdout.write(out)
        print(f"mypy_changed: mypy itself failed (exit {proc.returncode})")
        return 2
    mine = filter_diagnostics(out, files)
    if mine:
        print("\n".join(mine))
        print(
            "mypy_changed: type errors in the changed files -- reproduce: "
            f"uv run mypy {' '.join(files)}"
        )
        return 1
    others = sum(1 for ln in out.splitlines() if ": error:" in ln)
    note = f"; {others} error(s) in unchanged imported files ignored" if others else ""
    print(f"mypy_changed: {len(files)} changed file(s) clean{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
