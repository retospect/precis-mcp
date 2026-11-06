"""Pins the three release docs a human or agent reads first — shipping.md,
release-cycle.md, round.md — to the settings and job types they must name
(td574366), and refuses an unguarded claim that the factory release loop is
armed, running on prod, or that ``release.auto_deploy`` is on."""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

import pytest

from precis import settings as psettings
from precis.workers.job_types import get_job_type, known_job_types
from tests._script import load_script

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPING = REPO_ROOT / "docs/conventions/shipping.md"
RELEASE_CYCLE = REPO_ROOT / "docs/runbooks/release-cycle.md"
ROUND_CMD = REPO_ROOT / ".claude/commands/round.md"

_SETTINGS = (
    "release.auto_deploy",
    "release.no_release_windows",
    "release.repo",
    "release.health_url",
    "release.stage_deadlines",
)
_JOB_TYPES = (
    "release_poll",
    "release_cut",
    "ci_verdict_poll",
    "ci_red_digest",
    "release_deploy",
    "release_confirm",
)
_ROUND_VERBS = ("open", "cut", "deploy", "close", "supersede")
_ARMED_CLAIMS = ("is armed", "is live on prod", "auto_deploy is on")
_GUARDS = ("when", "if")


def test_docs_name_real_settings_and_ops() -> None:
    shipping = SHIPPING.read_text(encoding="utf-8")
    release_cycle = RELEASE_CYCLE.read_text(encoding="utf-8")
    for key in _SETTINGS:
        assert key in psettings.REGISTRY, f"{key} is not a declared setting"
        assert key in shipping or key in release_cycle, (
            f"{key} is named in neither shipping.md nor release-cycle.md"
        )
    for name in _JOB_TYPES:
        assert get_job_type(name) is not None, f"{name} is not a known job type"
        assert name in known_job_types(), name
        assert name in release_cycle, f"{name} is not named in release-cycle.md"


def test_round_verbs_named_in_docs_exist() -> None:
    if sys.platform == "win32":
        pytest.skip("scripts/round is POSIX release tooling")
    script = load_script("round_script_rel_docs_pin", REPO_ROOT / "scripts" / "round")
    usage = io.StringIO()
    try:
        with contextlib.redirect_stderr(usage):
            script.main([])
    except SystemExit:
        pass
    match = re.search(r"\{([^}]+)\}", usage.getvalue())
    assert match, "scripts/round lost its subcommand usage line"
    subcommands = set(match.group(1).split(","))
    round_md = ROUND_CMD.read_text(encoding="utf-8")
    for verb in _ROUND_VERBS:
        assert verb in subcommands, f"{verb} is not a scripts/round subcommand"
        assert f"scripts/round {verb}" in round_md, (
            f"'scripts/round {verb}' is not named in round.md"
        )

    help_text = io.StringIO()
    try:
        with contextlib.redirect_stdout(help_text):
            script.main(["supersede", "--help"])
    except SystemExit:
        pass
    assert "--partial" in help_text.getvalue(), (
        "scripts/round supersede no longer takes --partial"
    )


def _strip_markers(sentence: str) -> str:
    return re.sub(r"^[\s\-*>#|`]+", "", sentence)


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in text.splitlines():
        sentences.extend(re.split(r"(?<=[.!?])\s+", line))
    return [s for s in sentences if s.strip()]


def test_docs_never_claim_armed() -> None:
    for path in (SHIPPING, RELEASE_CYCLE, ROUND_CMD):
        text = path.read_text(encoding="utf-8")
        for sentence in _sentences(text):
            lowered = sentence.lower()
            if not any(claim in lowered for claim in _ARMED_CLAIMS):
                continue
            stripped = _strip_markers(sentence).lower()
            assert stripped.startswith(_GUARDS), (
                f"{path.name}: unguarded armed claim: {sentence!r}"
            )
