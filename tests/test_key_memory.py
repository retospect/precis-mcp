"""The key-memory hub size cap stays wired to its enforcement points.

The key-memory skill (``precis-key-memory-help``) tells agents to keep the
hub body within the 4,000-character fisheye excerpt. Review finding on
gr741446 (brief item 7): that guidance named a cap nothing checked. These
tests pin the chain together — the constant the skill cites, the fisheye
body cut it derives from, and the write-path warning that uses the same
number — so the wording cannot drift from the enforced limit.
"""

from __future__ import annotations

from precis.key_memory import HELP_SKILL, HUB_BODY_MAX_CHARS
from precis.utils import eye_render, memory_hubs
from tests._skill_roots import skill_path


def test_hub_body_max_chars_is_the_fisheye_verbatim_cap() -> None:
    """The budget the skill cites is the fisheye body rung's cut."""
    assert HUB_BODY_MAX_CHARS == eye_render._VERBATIM_CAP
    assert HUB_BODY_MAX_CHARS == 4000


def test_write_path_warns_at_the_same_cap() -> None:
    """The "Memory too large" hint the write path emits uses the same
    constant, so a body over the budget is flagged where it is written."""
    assert memory_hubs._VERBATIM_CAP is eye_render._VERBATIM_CAP


def test_key_memory_skill_cites_the_enforced_cap() -> None:
    """The skill's size guidance names the cap the code enforces, so a
    rewording that drops the anchor fails here instead of drifting."""
    text = skill_path(HELP_SKILL).read_text(encoding="utf-8")
    assert "HUB_BODY_MAX_CHARS" in text
    assert f"{HUB_BODY_MAX_CHARS:,}-character fisheye excerpt" in text
