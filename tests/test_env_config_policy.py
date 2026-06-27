"""Guard for the env-var policy (docs/conventions/env-vars.md).

Tier-1 (core config) vars live on ``PrecisConfig`` and must be read via
``load_config()``, not ``os.environ``, in deep handler/worker code. This
test fails when a *new* raw read of a Tier-1 var creeps into
``src/precis/`` outside the bootstrap zone (``src/precis/cli/**``, which
legitimately reads env to construct config).

A small frozen grandfather list carries the pre-policy offenders (deep
code that reads ``PRECIS_ROOT`` raw and does its own path handling). Do
not add to it to silence a new violation — route the read through
``load_config()`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from precis.config import PrecisConfig

_SRC = Path(__file__).parent.parent / "src" / "precis"

# Env names backed by a PrecisConfig field (Tier 1). Derived from the
# model so this stays in sync as fields are added.
_TIER1_VARS: frozenset[str] = frozenset(
    f"PRECIS_{name.upper()}" for name in PrecisConfig.model_fields
)

# Files allowed to read env to *construct* config (bootstrap zone) — the
# CLI entrypoints. Matched as path prefixes relative to ``_SRC``.
_BOOTSTRAP_PREFIXES: tuple[str, ...] = ("cli/",)

# Frozen pre-policy offenders: (relative path under src/precis, var).
# These read PRECIS_ROOT raw and do bespoke path resolution; they're
# cleanup targets, not licence for new raw reads.
_GRANDFATHERED: frozenset[tuple[str, str]] = frozenset(
    {
        ("utils/compile_guard.py", "PRECIS_ROOT"),
        ("handlers/_todo_views.py", "PRECIS_ROOT"),
        ("workers/planner_prompt.py", "PRECIS_ROOT"),
        ("workers/job_types/draft_export.py", "PRECIS_ROOT"),
    }
)


def _raw_read_re(var: str) -> re.Pattern[str]:
    # os.environ["X"], os.environ.get("X"...), os.getenv("X"...), either quote.
    return re.compile(
        rf"""os\.environ(?:\.get)?\[?\(?\s*["']{re.escape(var)}["']"""
        rf"""|getenv\(\s*["']{re.escape(var)}["']"""
    )


def _find_violations() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    patterns = {var: _raw_read_re(var) for var in _TIER1_VARS}
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if any(rel.startswith(p) for p in _BOOTSTRAP_PREFIXES):
            continue
        text = path.read_text(encoding="utf-8")
        for var, pat in patterns.items():
            if pat.search(text):
                found.add((rel, var))
    return found


def test_no_new_raw_reads_of_tier1_config_vars() -> None:
    new = _find_violations() - _GRANDFATHERED
    assert not new, (
        "Tier-1 config vars must be read via load_config(), not os.environ "
        "(see docs/conventions/env-vars.md). New raw reads:\n  "
        + "\n  ".join(f"{rel}: {var}" for rel, var in sorted(new))
    )


def test_grandfather_list_has_no_stale_entries() -> None:
    """Drop entries from _GRANDFATHERED once the raw read is removed."""
    stale = _GRANDFATHERED - _find_violations()
    assert not stale, (
        "These grandfathered raw reads are gone — remove them from "
        f"_GRANDFATHERED: {sorted(stale)}"
    )
