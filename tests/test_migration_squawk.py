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


def test_migrations_pass_squawk() -> None:
    if os.environ.get("PRECIS_SQUAWK", "1") == "0":
        pytest.skip("PRECIS_SQUAWK=0 — the ship's escape hatch, honoured here")
    if shutil.which("uvx") is None:
        pytest.skip("uvx not on PATH — the ship only warns there too")
    proc = subprocess.run(
        [
            "uvx",
            "-q",
            "--from",
            "squawk-cli>=2",
            "squawk",
            "--reporter",
            "gcc",
            *_numbered_migrations(),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    violators = {
        m.group(1)
        for line in proc.stdout.splitlines()
        if (m := _VIOLATION_RE.match(line))
    }
    if proc.returncode != 0 and not violators:
        pytest.skip(
            "squawk-cli could not run (uvx resolve/offline): "
            + (proc.stderr or proc.stdout).strip()[-300:]
        )
    new = sorted(violators - _SEALED_VIOLATORS)
    assert not new, (
        "squawk findings in migrations that postdate the lint — fix the "
        "DDL, never this list (CREATE TABLE/ADD COLUMN/CREATE INDEX with "
        "IF NOT EXISTS, bigint not smallint, seeds ON CONFLICT DO "
        "NOTHING; exemplar: src/precis/migrations/0276_learning_events.sql, "
        "rules: .squawk.toml): "
        f"{new}"
    )
