"""Skill file size guard — transport-cap hygiene for the runtime skills.

``src/precis/data/skills/*.md`` are served to agents via ``get(kind='skill')``.
An oversized whole-skill read used to paginate into ~14KB frames; since
04ef78736 the pagination layer is gone — a rendered response over
``DEFAULT_MAX_BODY_BYTES`` *refuses* with the handler's
``result_budget_hint`` as the next step (``precis/_result_budget.py`` +
``runtime/dispatch.py``). A skill file that big is a dead end for
``get(kind='skill', id='<slug>')`` — the agent has to know to ask for
``<slug>/toc`` or ``<slug>~N`` first. Nothing else in the ship gate
(ruff/mypy/pytest) watches skill size — this test does.

Two checks over the same corpus:

- a hard cap (``FAIL_BYTES``) that fails a file unless its slug is in
  ``_ALLOWLIST`` with a justification. The fix for an allowlisted file is
  restructuring it for ``/toc``-style section access (page by section, not
  the whole doc), not raising the cap. The list is shrink-only: an entry
  whose file fits again fails the ratchet until it's removed.
- a soft cap (``WARN_BYTES``) that surfaces drift via a plain
  ``warnings.warn`` so growth is visible in the pytest summary well before
  it trips the hard cap.

Per-block structure (the ~4k-token section cap, heading shape, xref
validity) lives next door in ``test_skill_blocks.py``.

Pure file stats — no DB, no embedder.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from precis._result_budget import DEFAULT_MAX_BODY_BYTES
from tests._skill_roots import skill_files

#: Hard cap in bytes — the one knob. A skill over this fails the gate
#: unless allowlisted. Sits 2 KiB under the response cap because a serve
#: adds ~0.6–1.3 KB on top of the file (family header, graph footer,
#: misled line, ``skill rev``, banner, ``[[wikilink]]`` expansion) —
#: measured 2026-10-29. (gr515088: derive, don't magic-number, the rest.)
FAIL_BYTES = DEFAULT_MAX_BODY_BYTES - 2048

#: Soft cap in bytes. A skill over this (but under FAIL_BYTES, or allowlisted)
#: only warns — visible drift before it becomes a failure. Derived from
#: ``FAIL_BYTES`` so the two thresholds can't drift into each other.
WARN_BYTES = FAIL_BYTES // 2

#: slug (filename stem, no ``.md``) -> justification. Every entry here is
#: exempt from ``FAIL_BYTES`` but still counted toward the ``WARN_BYTES``
#: drift warning, so its shrink-back is visible too. Shrink-only:
#: ``test_allowlist_is_shrink_only`` fails an entry whose file fits
#: again until it's removed — new violations fail outright.
_ALLOWLIST: dict[str, str] = {
    slug: "grandfathered pending the slice-5 split (docs/backlog/block-navigation.md)"
    for slug in (
        "precis-finding-help",
        "precis-gripe-help",
        "precis-hexfold-help",
        "precis-job-help",
        "precis-measure-help",
        "precis-memory-help",
        "precis-nanopub-help",
        "precis-overview",
        "precis-paper-help",
        "precis-paper-ref-help",
        "precis-pathway-help",
        "precis-pcb-help",
        "precis-python-help",
        "precis-quest-help",
        "precis-se-atomic-help",
        "precis-se-chain-help",
        "precis-se-help",
        "precis-se-print-help",
        "precis-se-regions-help",
        "precis-search-help",
        "precis-structure-help",
        "precis-taproot-mint-help",
    )
}


#: Floor on the swept corpus — parametrization over an empty glob
#: collects nothing, so a misrooted corpus would pass vacuously. Well
#: under the real count (~220 built-ins, 2026-10) to leave room for
#: plugin-split moves.
SKILL_CORPUS_MIN_FILES = 100


def _skill_files() -> list[Path]:
    return skill_files(recursive=False)


def test_skill_corpus_is_non_empty() -> None:
    """The parametrize sweep only works on a corpus that exists."""
    files = _skill_files()
    assert len(files) >= SKILL_CORPUS_MIN_FILES, (
        f"only {len(files)} skill files found (floor "
        f"{SKILL_CORPUS_MIN_FILES}) — an empty or misrooted corpus sweeps "
        "nothing and every parametrized check passes vacuously"
    )


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.stem)
def test_skill_file_under_size_cap(path: Path) -> None:
    slug = path.stem
    size = path.stat().st_size
    if slug in _ALLOWLIST:
        return
    assert size <= FAIL_BYTES, (
        f"{slug} is {size} bytes (cap {FAIL_BYTES}) — a whole-skill read "
        f"refuses once the rendered response tops {DEFAULT_MAX_BODY_BYTES} "
        "bytes, so the file must leave room for serve overhead. The fix is "
        "restructuring the skill for /toc + ~N section access, not raising "
        "this cap. If it genuinely needs more room short-term, add it to "
        "_ALLOWLIST in this test with a justification."
    )


def test_allowlist_entries_have_justification() -> None:
    for slug, justification in _ALLOWLIST.items():
        assert justification and justification.strip(), (
            f"_ALLOWLIST entry for {slug!r} must carry a non-empty justification"
        )


def test_allowlist_is_shrink_only() -> None:
    """A grandfathered slug whose file fits again must leave the list —
    the allowlist only shrinks."""
    files = _skill_files()
    existing = {p.stem for p in files}
    for slug in _ALLOWLIST:
        assert slug in existing, (
            f"allowlisted skill {slug} no longer exists; remove it from the list"
        )
    fits = {p.stem for p in files if p.stat().st_size <= FAIL_BYTES}
    stale = sorted(set(_ALLOWLIST) & fits)
    assert not stale, (
        f"{stale} now fit under {FAIL_BYTES} bytes — remove them from _ALLOWLIST"
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
