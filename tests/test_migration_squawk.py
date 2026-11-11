"""Squawk over every numbered migration — the ship's lint, run early.

``scripts/ship``'s ``_squawk_new_migrations`` lints only the migration
files a ship ADDS against ``origin/main`` — a red there is a lost land
(gr570072: ``CREATE TABLE`` without ``IF NOT EXISTS``, a ``smallint``
column). This runs the same pinned ``squawk-cli`` and the same
``.squawk.toml`` (picked up from the repo root, so the single-writer
exclusions apply) over every ``NNNN_*.sql`` under ``src/*/migrations/``,
so ``scripts/test`` surfaces the finding in the builder's worktree
before the land, not at it.

Files sealed before the lint existed can't be fixed — a sealed
migration is never edited (docs/conventions/invariants.md) — so they
are grandfathered in :data:`_SEALED_VIOLATORS`. A finding in any other
file fails: fix the DDL, never widen the set.

Failure handling splits three ways (td636400 — a nonzero exit used to
skip wholesale when no finding parsed, and to pass unread once every
parsed finding was sealed):

- *Dependency unavailable* → skip, the ship's warn-not-block posture:
  a ``squawk --version`` probe first proves uvx can resolve, fetch and
  launch the pinned package.
- *Tool error* → fail: squawk sends operational errors (unmatched
  paths, unreadable files, CLI misuse) to stderr while findings go to
  stdout, so a nonzero run with anything on stderr — or with no parsed
  findings at all — means the lint pass didn't complete and its output
  can't be trusted, however the sealed set parses.
- *Findings* → the lint verdict itself: sealed ones pass, new ones
  fail.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_PREFIX_RE = re.compile(r"^\d{4}_")
_VIOLATION_RE = re.compile(r"^(src/\S+\.sql):\d+:\d+:")

_UVX_SQUAWK = ["uvx", "-q", "--from", "squawk-cli>=2", "squawk"]

#: Numbered migrations that carry findings under the ship's squawk rule
#: set as of the lint's introduction (gr570072). All are sealed —
#: pre-squawk DDL that can never be edited — so this set only shrinks.
_SEALED_VIOLATORS = frozenset(
    {
        "src/precis/migrations/0001_initial.sql",
        "src/precis/migrations/0003_drop_legacy_segments.sql",
        "src/precis/migrations/0004_drop_quest_kind.sql",
        "src/precis/migrations/0013_todo_tree.sql",
        "src/precis/migrations/0014_refs_prio.sql",
        "src/precis/migrations/0020_claude_quota_snapshot.sql",
        "src/precis/migrations/0022_kind_provider.sql",
        "src/precis/migrations/0023_migrations_plugin.sql",
        "src/precis/migrations/0031_draft_kind.sql",
        "src/precis/migrations/0035_chunk_blobs.sql",
        "src/precis/migrations/0042_structure_kind.sql",
        "src/precis/migrations/0052_pdf_locations.sql",
        "src/precis/migrations/0076_email_scan.sql",
        "src/precis/migrations/0128_nanopub_publish.sql",
        "src/precis/migrations/0132_doi_validation.sql",
        "src/precis/migrations/0138_pcb_boards_routes.sql",
        "src/precis/migrations/0149_refs_retired_at_rename.sql",
        "src/precis/migrations/0150_struct_atoms_charge.sql",
        "src/precis/migrations/0181_nanopub_composite_artifact_type.sql",
        "src/precis/migrations/0187_measures.sql",
        "src/precis_se/migrations/0003_se_bom.sql",
        "src/precis_se/migrations/0007_se_atomic.sql",
        "src/precis_se/migrations/0008_se_drop_nm_tables.sql",
        "src/precis_se/migrations/0009_se_block_uid.sql",
        "src/precis_se/migrations/0011_se_port_pose.sql",
        "src/precis_se/migrations/0014_se_port_frame_atoms.sql",
        "src/precis_se/migrations/0015_se_chain.sql",
        "src/precis_se/migrations/0018_se_regions.sql",
        "src/precis_se/migrations/0023_se_bound_kind_pcb.sql",
    }
)


def _numbered_migrations() -> list[str]:
    """Every top-level ``NNNN_*.sql`` in a package migrations dir (archive
    and baseline subdirs are not migrations)."""
    return sorted(
        str(p.relative_to(REPO_ROOT))
        for d in sorted(REPO_ROOT.glob("src/*/migrations"))
        for p in d.glob("*.sql")
        if _PREFIX_RE.match(p.name)
    )


def _violator_paths(stdout: str) -> set[str]:
    """Files carrying at least one gcc-format finding — lint violations
    and syntax errors share the ``src/x.sql:LINE:COL:`` shape."""
    return {
        m.group(1) for line in stdout.splitlines() if (m := _VIOLATION_RE.match(line))
    }


def _squawk_unavailable(proc: subprocess.CompletedProcess[str]) -> str | None:
    """Skip reason when the ``squawk --version`` probe couldn't launch
    the tool — uvx resolve/fetch failure, offline, or a broken cached
    environment — versus ``None`` when squawk ran and answered."""
    if proc.returncode == 0 and proc.stdout.startswith("squawk"):
        return None
    detail = (proc.stderr or proc.stdout).strip()
    return detail[-300:] or f"rc={proc.returncode}, no output"


def _squawk_failure(
    proc: subprocess.CompletedProcess[str], violators: set[str]
) -> str | None:
    """Failure description when the lint run errored as a *tool*;
    ``None`` when the run is clean or its nonzero exit is explained by
    the findings alone.

    Squawk exits 1 when it emits findings, so a nonzero run whose stderr
    stayed quiet is the lint verdict — even if every finding turns out
    sealed. Anything on stderr is an operational error riding alongside
    (or instead of) the findings, and a silent nonzero run produced no
    verdict at all."""
    if proc.returncode == 0:
        return None
    if err := proc.stderr.strip():
        return f"rc={proc.returncode}, stderr: {err[-300:]}"
    if not violators:
        tail = proc.stdout.strip()[-300:]
        return f"rc={proc.returncode}, no findings" + (f": {tail}" if tail else "")
    return None


def test_migrations_pass_squawk() -> None:
    if os.environ.get("PRECIS_SQUAWK", "1") == "0":
        pytest.skip("PRECIS_SQUAWK=0 — the ship's escape hatch, honoured here")
    if shutil.which("uvx") is None:
        pytest.skip("uvx not on PATH — the ship only warns there too")
    try:
        probe = subprocess.run(
            [*_UVX_SQUAWK, "--version"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("uvx did not deliver squawk-cli within 120s")
    if why := _squawk_unavailable(probe):
        pytest.skip(f"squawk-cli unavailable — uvx could not run it: {why}")
    proc = subprocess.run(
        [*_UVX_SQUAWK, "--reporter", "gcc", *_numbered_migrations()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    violators = _violator_paths(proc.stdout)
    failure = _squawk_failure(proc, violators)
    assert failure is None, f"squawk errored during the lint pass — {failure}"
    new = sorted(violators - _SEALED_VIOLATORS)
    assert not new, (
        "squawk findings in migrations that postdate the lint — fix the "
        "DDL, never this list (CREATE TABLE/ADD COLUMN/CREATE INDEX with "
        "IF NOT EXISTS, bigint not smallint, seeds ON CONFLICT DO "
        "NOTHING; exemplar: src/precis/migrations/0276_learning_events.sql, "
        "rules: .squawk.toml): "
        f"{new}"
    )


def _proc(
    rc: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def test_violator_paths_matches_gcc_lines() -> None:
    """Warnings and syntax errors share the ``path:line:col:`` shape and
    both count; squawk chatter around them does not."""
    stdout = (
        "src/precis/migrations/0001_initial.sql:9:1: warning: prefer-big-int over int\n"
        "src/precis/migrations/0001_initial.sql:1532:0: error: syntax-error expected command\n"
        "src/precis_se/migrations/0003_se_bom.sql:4:2: warning: x\n"
        "2 violations across 1 file\n"
    )
    assert _violator_paths(stdout) == {
        "src/precis/migrations/0001_initial.sql",
        "src/precis_se/migrations/0003_se_bom.sql",
    }


def test_squawk_failure_clean_run() -> None:
    assert _squawk_failure(_proc(0), set()) is None


def test_squawk_failure_findings_explain_the_nonzero() -> None:
    """rc=1 with a quiet stderr is squawk's findings exit — the verdict
    stands even when every parsed finding is sealed."""
    assert _squawk_failure(_proc(1), set(_SEALED_VIOLATORS)) is None


def test_squawk_failure_stderr_is_a_tool_error() -> None:
    """td636400: squawk's own errors ('Failed to find files…', CLI
    misuse, a crash message) go to stderr — a nonzero run that reports
    one fails instead of skipping on the empty finding set."""
    failure = _squawk_failure(
        _proc(1, stderr='Failed to find files for provided patterns: ["x"]\n'),
        set(),
    )
    assert failure is not None and "Failed to find files" in failure


def test_squawk_failure_stderr_unmasked_by_sealed_findings() -> None:
    """td636400: an operational error used to hide behind a fully
    grandfathered finding set — the nonzero exit was ignored once every
    parsed finding was sealed. stderr content fails the run regardless."""
    failure = _squawk_failure(_proc(1, stderr="boom\n"), set(_SEALED_VIOLATORS))
    assert failure is not None and "boom" in failure


def test_squawk_failure_silent_nonzero_is_a_tool_error() -> None:
    """td636400: a nonzero run emitting neither findings nor stderr is
    squawk dying silently — a tool error, not a verdict (the probe has
    already answered unavailability)."""
    assert _squawk_failure(_proc(1), set()) is not None
    assert _squawk_failure(_proc(2), set()) is not None


def test_squawk_unavailable_probe() -> None:
    """The probe calls squawk available only when it actually answered —
    a bare rc=0 without the version banner is not a running squawk."""
    assert _squawk_unavailable(_proc(0, stdout="squawk 2.71.0\n")) is None
    assert "Failed to fetch" in (
        _squawk_unavailable(_proc(1, stderr="error: Failed to fetch x\n")) or ""
    )
    assert _squawk_unavailable(_proc(1)) == "rc=1, no output"
    assert _squawk_unavailable(_proc(0, stdout="")) == "rc=0, no output"
