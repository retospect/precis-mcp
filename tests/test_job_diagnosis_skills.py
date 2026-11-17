"""Job-diagnosis skill content tests — pinned load-bearing phrases.

Each test module here belongs to a slice that turned a scattered
memory-only checklist into a shipped skill; the tests pin the verbatim
lines the follow-up gripe's prod check reads for, so a prose rewrite
cannot silently drop them.

- ``ci-only-red`` (gr663865): the CI-only-red triage and reprocedure.
- ``host-play`` (gr663866): the hand-run host play checklist.

Pure file stats — no DB, no embedder.
"""

from __future__ import annotations

from tests._skill_roots import skill_path


def _skill_text(slug: str) -> str:
    return skill_path(slug).read_text(encoding="utf-8")


def test_ci_only_red_triage() -> None:
    """Outage triage comes before log reading, and raw gh api logs need
    the escape-sequences flag."""
    text = _skill_text("ci-only-red")
    assert "githubstatus" in text
    assert "--allow-escape-sequences" in text


def test_ci_only_red_repro() -> None:
    """The verbatim repro phrase and the ordering-flake seed are present."""
    text = _skill_text("ci-only-red")
    assert "reproduce a shard-only red" in text.lower()
    assert "--random" in text


def test_host_play_run_discipline() -> None:
    """The wrapper rule and the dry-run sequence survive prose edits."""
    text = _skill_text("host-play")
    assert "host-ops/play pins the ref" in text
    assert "--check --diff" in text


def test_host_play_traps() -> None:
    """The per-play traps are named, not implied."""
    text = _skill_text("host-play")
    assert "pgbouncer" in text
    assert "bootout" in text


def test_host_play_overlay() -> None:
    """The overlay discipline keeps the vault-header check."""
    text = _skill_text("host-play")
    assert "vault header" in text
