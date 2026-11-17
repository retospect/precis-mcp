"""Tests for the ``ci-only-red`` skill — the CI-only-red repro recipe (gr663865).

The skill pulls the triage and reprocedure for "CI is red and I cannot
reproduce it" out of eight scattered memory gotcha nodes into one
place. These tests pin the load-bearing phrases the follow-up gripe's
prod check reads for.

Pure file stats — no DB, no embedder.
"""

from __future__ import annotations

from tests._skill_roots import skill_path


def _skill_text() -> str:
    return skill_path("ci-only-red").read_text(encoding="utf-8")


def test_ci_only_red_triage() -> None:
    """Outage triage comes before log reading, and raw gh api logs need
    the escape-sequences flag."""
    text = _skill_text()
    assert "githubstatus" in text
    assert "--allow-escape-sequences" in text


def test_ci_only_red_repro() -> None:
    """The verbatim repro phrase and the ordering-flake seed are present."""
    text = _skill_text()
    assert "reproduce a shard-only red" in text.lower()
    assert "--random" in text
