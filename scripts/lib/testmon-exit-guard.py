"""scripts/lib/testmon-exit-guard.py — decide the real exit code of a
`scripts/test --impacted` run when pytest-testmon's own bookkeeping crashes.

Why: pytest-testmon's ``get_file()`` (``testmon_core.py`` ~93, in
``TestmonData.get_tests_fingerprints``) does ``filename.rsplit(".", 1)[1]``
on every file coverage recorded a hit for, with NO extension guard at that
call site (the ``is_python_file`` check lives on a different code path).
An extensionless file executed in-process during the run — e.g. a test that
loads a ``scripts/*`` executable via ``importlib`` at its literal (dotless)
path, so its ``co_filename`` has no ``.`` — IndexErrors there and pytest
raises ``INTERNALERROR``. That can strike AFTER pytest has already printed
a clean "N passed" summary, so the *raw* pytest exit code no longer means
"tests failed": one sighting turned a fully green run into exit 3
(gr450298), another left a green run at exit 0 with a crash buried mid-log
(gr449802) — same bug, different luck on timing.

This module does not try to fix testmon (see
``tests/test_main_ci_status.py`` for the actual root-cause fix — it was the
one caller in this repo loading an extensionless script in-process without
the copy-to-a-``.py``-suffixed-temp-file workaround already established by
``tests/test_coderef_structural.py``/``tests/test_pcb_dead_exports.py``).
It is the second line of defence: distinguish "tests failed" from "testmon's
own bookkeeping crashed" from the captured pytest log text, so a caller that
only checks the exit code is not misled either way.

gr261537: it also owns the SECOND, unrelated warning `scripts/test
--impacted` needs after a run — testmon's affected-tests selection can
balloon past the point where it's cheaper than a plain full run (a
central-module diff can pull in ~half the suite, which then runs at the
forced ``-n0`` instead of the normal ``-n6``). pytest's own end-of-run
summary line already carries the counts to catch this: pytest-testmon's
``pytest_deselected`` hook (``pytest_testmon.py``) makes every deselected
test show up in that line as an ordinary ``"N deselected"`` category
alongside pytest's own ``passed``/``failed``/etc — the same line
:func:`decide_exit_code` already parses for a clean summary. See
:func:`testmon_selection_warning`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The traceback testmon prints starts with pytest's own "INTERNALERROR>"
# prefix (one per line, including the frames) and, for this exact bug,
# always bottoms out in testmon_core.py's get_file. re.S so the frame
# lines between the two anchors (usually just a few) match.
_INTERNALERROR_SIG = re.compile(r"INTERNALERROR>.*testmon_core\.py.*get_file", re.S)

# pytest's own end-of-run banner, e.g.:
#   ======== 10222 passed, 28 skipped in 3180.86s ========
#   ==== 3 failed, 10219 passed, 28 skipped in 42.10s ====
# Only categories that actually occurred are listed — a zero-failure run
# never prints literal "0 failed", it just omits "failed" entirely.
_SUMMARY_RE = re.compile(r"^=+ (?P<body>.*? in [\d.]+s(?: \([^)]*\))?) =+\s*$", re.M)


def testmon_signature_present(log_text: str) -> bool:
    """Whether the captured log contains testmon's get_file INTERNALERROR."""
    return bool(_INTERNALERROR_SIG.search(log_text))


def decide_exit_code(log_text: str, pytest_exit_code: int) -> tuple[int, str | None]:
    """Return ``(exit_code, warning_or_none)`` for a captured pytest run.

    ``pytest_exit_code`` — what the pytest process itself returned — is
    trusted verbatim unless the testmon INTERNALERROR signature (see
    :func:`testmon_signature_present`) is present in the log. When it is:

    - a clean trailing summary line (has "passed", no "failed"/"error")
      means the suite itself was green — force exit 0 with a WARNING naming
      the testmon crash, so a caller trusting the exit code isn't told a
      green suite failed (gr450298).
    - no such summary line (the crash truncated the run before pytest could
      print one) means the real test outcome is unknown — return a non-zero
      exit code with a message that says so explicitly, rather than a bare
      pytest-internal-error code that reads like an ordinary crash
      (gr449802).
    """
    if not testmon_signature_present(log_text):
        return pytest_exit_code, None

    clean_summary = None
    for body in _SUMMARY_RE.findall(log_text):
        if "passed" in body and "failed" not in body and "error" not in body:
            clean_summary = body

    if clean_summary is not None:
        return 0, (
            "WARNING: pytest-testmon crashed with its own INTERNALERROR "
            f"(get_file IndexError) but the suite itself was green ({clean_summary}) "
            "— forcing exit 0. See gr450298/gr449802."
        )

    return (
        pytest_exit_code or 1,
        "ERR: pytest-testmon crashed (INTERNALERROR in get_file) with no clean "
        "pytest summary in the log — testmon crashed, result unknown, NOT a "
        "trustworthy pass or fail. Re-run. See gr450298/gr449802.",
    )


# A summary-line part is "<count> <category>", e.g. "6200 passed",
# "28 skipped", "7302 deselected" — only categories that occurred appear at
# all (pytest never prints "0 failed").
_SUMMARY_PART_RE = re.compile(r"(\d+) ([a-z]+)")

# gr261537: warn once testmon's selection crosses this fraction of the suite
# it evaluated — past this point the forced -n0 serial run routinely takes
# longer than a plain `scripts/test`'s full -n6 run.
_SELECTION_WARN_THRESHOLD = 0.5


def testmon_selection_counts(log_text: str) -> tuple[int, int] | None:
    """Return ``(selected, total)`` test counts from the run's LAST summary
    line, or ``None`` if no summary line is present (e.g. a crash before
    pytest could print one, or a run with no tests collected at all).

    ``selected`` sums every category testmon actually ran or attempted
    (passed/failed/skipped/error/xfailed/xpassed); ``deselected`` is
    testmon's own category (via its ``pytest_deselected`` hook) for tests it
    chose to skip. ``total`` — selected + deselected — is the size of the
    suite testmon evaluated, i.e. what a non-impacted run would collect.
    """
    bodies = _SUMMARY_RE.findall(log_text)
    if not bodies:
        return None
    selected = 0
    deselected = 0
    for count_s, category in _SUMMARY_PART_RE.findall(bodies[-1]):
        if category == "deselected":
            deselected += int(count_s)
        else:
            selected += int(count_s)
    total = selected + deselected
    return (selected, total) if total else None


def testmon_selection_warning(log_text: str) -> str | None:
    """Return the gr261537 WARNING when testmon selected more than
    ``_SELECTION_WARN_THRESHOLD`` of the suite it evaluated, or ``None``.

    Names the counts and the cheaper alternative rather than a bare
    "selection is large" — a caller can act on "6200/13500 tests" in a way
    they can't act on a generic warning (gr261537 comment 2).
    """
    counts = testmon_selection_counts(log_text)
    if counts is None:
        return None
    selected, total = counts
    if selected <= total * _SELECTION_WARN_THRESHOLD:
        return None
    return (
        f"WARNING: testmon selected {selected}/{total} tests "
        f"({selected * 100 // total}% of the suite) — --impacted forces "
        "-n0 (serial); a plain `scripts/test` runs the full suite at -n6 "
        "and likely finishes sooner on a diff this broad. See gr261537."
    )


def clear_testmon_datafiles(worktree: Path) -> list[str]:
    """Delete the (possibly now-inconsistent) testmon map so the next
    ``--impacted`` run rebuilds it from scratch, instead of layering new
    selections on state a mid-run crash may have left half-written."""
    removed = []
    for f in sorted(worktree.glob(".testmondata*")):
        f.unlink(missing_ok=True)
        removed.append(f.name)
    return removed


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: testmon-exit-guard.py <log-file> <pytest-exit-code> [worktree]",
            file=sys.stderr,
        )
        return 2
    log_path, raw_code = argv[0], argv[1]
    worktree = Path(argv[2]) if len(argv) == 3 else None

    log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    exit_code, message = decide_exit_code(log_text, int(raw_code))
    if message:
        print(message, file=sys.stderr)

    # gr261537: independent of the exit-code decision above — a run can be
    # a genuine pass/fail AND still have selected most of the suite.
    selection_warning = testmon_selection_warning(log_text)
    if selection_warning:
        print(selection_warning, file=sys.stderr)

    if worktree is not None and testmon_signature_present(log_text):
        removed = clear_testmon_datafiles(worktree)
        if removed:
            print(
                f"cleared corrupt testmon map ({', '.join(removed)}) — the "
                "next --impacted run rebuilds it (a full run, not incremental)",
                file=sys.stderr,
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
