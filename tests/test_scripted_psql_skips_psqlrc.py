"""Every psql invocation whose STDOUT a machine parses must pass ``-X``.

Found 2026-08-22. ``psql`` sources the invoking user's ``~/.psqlrc`` unless
``-X`` is given, and on the cluster's DB node that file is written for humans::

    \\timing on
    \\x auto
    \\pset null '∅'
    \\pset linestyle unicode
    \\pset border 2

All of that lands on **stdout**, so a scripted ``-tAc`` scalar comes back as
``"0\\nTime: 0.382 ms"`` rather than ``"0"``. The deploy's long-job drain
compared exactly that against ``'0'`` in an ``until:``, so it could never
succeed — every full-bounce deploy sat 30 minutes, printed "timed out
(proceeding anyway)", and killed the in-flight jobs it existed to protect.
The drain had never once run.

The failure mode is what makes this worth a test rather than a fix: with
``failed_when: false``, "the query could not answer" and "there is no work to
drain" are the same observation, so the breakage is invisible at every layer
above it. Same trap reaches ``scripts/prod-psql`` (whose ``-At`` output feeds
``scripts/deploy``'s canary heartbeat check), ``scripts/gen-schema`` (whose TSV
becomes ``docs/reference/schema.md`` — ``\\pset null '∅'`` would silently
rewrite every NULL), and the backup restore test.

Interactive psql is deliberately exempt: a human at a prompt wants ``.psqlrc``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files carrying at least one machine-parsed psql invocation, and how many.
#: Listed explicitly so that *adding* a scripted psql call to one of them (or
#: to a new file) is a deliberate act that updates this test.
_SCRIPTED_PSQL_FILES: dict[str, int] = {
    "deploy/redeploy-precis.yml": 2,  # drain preflight + drain wait
    "scripts/prod-psql": 1,  # REMOTE_SCRIPTED (REMOTE stays interactive)
    "scripts/gen-schema": 1,
    "deploy/roles/backups/templates/restore_test.sh.j2": 1,
}

#: A psql invocation, up to the first flag-ish token after the binary name.
#: Matches both bare `psql -X ...` and argv-list `[psql, -X, ...]` forms.
_PSQL_CALL = re.compile(r"\bpsql\b[,\s]+(?P<flags>(?:-{1,2}[\w-]+[,\s]+)*)")

#: psql's own name for "do not read ~/.psqlrc" — accept either spelling.
_NO_PSQLRC = ("-X", "--no-psqlrc")


def _scripted_psql_calls(text: str) -> list[str]:
    """Return the flag run of every psql call that is not interactive.

    A call is treated as interactive — and skipped — when the line assigns the
    non-scripted ``REMOTE`` variable in ``scripts/prod-psql``, the one place
    where inheriting ``.psqlrc`` is the point.
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") or line.lstrip().startswith("REMOTE="):
            continue
        for m in _PSQL_CALL.finditer(line):
            out.append(m.group("flags"))
    return out


@pytest.mark.parametrize(("rel", "expected"), sorted(_SCRIPTED_PSQL_FILES.items()))
def test_scripted_psql_passes_dash_x(rel: str, expected: int) -> None:
    path = _REPO_ROOT / rel
    assert path.is_file(), f"{rel} moved — update _SCRIPTED_PSQL_FILES"

    calls = _scripted_psql_calls(path.read_text(encoding="utf-8"))
    assert len(calls) == expected, (
        f"{rel}: found {len(calls)} scripted psql call(s), expected {expected}. "
        "A new one needs -X (or this map needs updating)."
    )
    for flags in calls:
        assert any(f in flags for f in _NO_PSQLRC), (
            f"{rel}: scripted psql call is missing -X, so it will source the "
            f"remote user's ~/.psqlrc and corrupt its own stdout. Flags: {flags!r}"
        )


def test_prod_psql_keeps_psqlrc_for_the_interactive_path() -> None:
    """The human-facing branch must NOT get -X — losing the prompt is a regression."""
    text = (_REPO_ROOT / "scripts" / "prod-psql").read_text(encoding="utf-8")

    remote = [ln for ln in text.splitlines() if ln.startswith("REMOTE=")]
    assert len(remote) == 1, (
        "scripts/prod-psql: expected exactly one REMOTE= assignment"
    )
    assert " -X " not in remote[0], (
        "scripts/prod-psql: the interactive REMOTE must keep ~/.psqlrc; only the "
        "scripted branches take -X"
    )
    # And the interactive branch must actually still use it.
    assert '"$REMOTE"' in text, (
        "scripts/prod-psql: interactive branch no longer uses REMOTE"
    )


def test_drain_wait_still_compares_a_bare_scalar() -> None:
    """The `until:` that -X exists to unbreak, pinned in place.

    If this comparison ever grows a `split`/`last`-style salvage filter, the
    -X fix has been papered over rather than kept.
    """
    text = (_REPO_ROOT / "deploy" / "redeploy-precis.yml").read_text(encoding="utf-8")
    assert "until: (precis_drain_wait.stdout | default('') | trim) == '0'" in text, (
        "the drain's until: changed shape — re-check that it still compares psql's "
        "raw scalar, and that -X is what keeps that scalar clean"
    )
