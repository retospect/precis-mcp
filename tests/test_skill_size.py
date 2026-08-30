"""Skill file size guard — pagination-frame hygiene for the runtime skills.

``src/precis/data/skills/*.md`` are served to agents via ``get(kind='skill')``
in ~14KB paginated frames. Oversized skills silently regrow (precis-draft-help
crept 29KB -> 51KB unnoticed) and burn pagination round-trips before an agent
reaches the answer. Nothing in the ship gate (ruff/mypy/pytest) watches skill
size — this test does.

Two checks over the same corpus:

- a hard cap (``FAIL_BYTES``) that fails a file unless its slug is in
  ``_ALLOWLIST`` with a justification. The fix for an allowlisted file is
  restructuring it for ``/toc``-style section access (page by section, not
  the whole doc), not raising the cap.
- a soft cap (``WARN_BYTES``) that surfaces drift via a plain
  ``warnings.warn`` so growth is visible in the pytest summary well before
  it trips the hard cap.

Pure file stats — no DB, no embedder, no imports beyond stdlib + pytest.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"

#: Hard cap in bytes. A skill over this fails the gate unless allowlisted.
FAIL_BYTES = 32 * 1024

#: Soft cap in bytes. A skill over this (but under FAIL_BYTES, or allowlisted)
#: only warns — visible drift before it becomes a failure.
WARN_BYTES = 16 * 1024

#: slug (filename stem, no ``.md``) -> justification. Every entry here is
#: exempt from ``FAIL_BYTES`` but still counted toward the ``WARN_BYTES``
#: drift warning, so its shrink-back is visible too.
_ALLOWLIST: dict[str, str] = {
    "precis-draft-help": (
        "restructured 2026-08 (51KB -> 40KB -> ~36KB after a second prose "
        "pass); the residual ~4.5KB over-cap is dense verb/arg contract "
        "material (figures/tables/citations/export), accepted for now. "
        "Remove when <= 32KB."
    ),
}


def _skill_files() -> list[Path]:
    return sorted(_SKILLS_DIR.glob("*.md"))


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_file_under_size_cap(path: Path) -> None:
    slug = path.stem
    size = path.stat().st_size
    if slug in _ALLOWLIST:
        return
    assert size <= FAIL_BYTES, (
        f"{slug} is {size} bytes (cap {FAIL_BYTES}) — skills are served in "
        "~14KB paginated frames; an oversized skill burns pagination "
        "round-trips before an agent reaches the answer it came for. The "
        "fix is restructuring the skill for /toc + ~N section access, not "
        "raising this cap. If it genuinely needs more room short-term, add "
        "it to _ALLOWLIST in this test with a justification."
    )


def test_allowlist_entries_have_justification() -> None:
    for slug, justification in _ALLOWLIST.items():
        assert justification and justification.strip(), (
            f"_ALLOWLIST entry for {slug!r} must carry a non-empty justification"
        )


def test_skill_files_over_warn_threshold_emit_drift_warning() -> None:
    """Surfaces size drift before it trips ``FAIL_BYTES`` — checked for every
    skill, including allowlisted ones, so progress toward an allowlisted
    file's eventual exemption removal is visible too."""
    oversized = [
        (p.stem, p.stat().st_size)
        for p in _skill_files()
        if p.stat().st_size > WARN_BYTES
    ]
    if oversized:
        listing = ", ".join(f"{slug} ({size}B)" for slug, size in sorted(oversized))
        warnings.warn(
            f"{len(oversized)} skill(s) over the {WARN_BYTES}B soft cap: "
            f"{listing}. Consider restructuring before this reaches the "
            f"{FAIL_BYTES}B hard cap.",
            stacklevel=1,
        )
