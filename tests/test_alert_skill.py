"""Content contract for the precis-alert-help skill (gr635727).

2026-11-11: five alerts resolved with ``alert-state:resolved``
re-fired within minutes under new ids (al635390-93, al635240) because
the underlying state — a parked todo, a dark host — had not changed.
The skill's Triage section now states the rule those resolutions ran
into: resolving does nothing unless the cause is cleared, otherwise
the detector re-mints the alert on its next pass. This test pins that
contract so a later edit cannot silently soften the warning again.
"""

from __future__ import annotations

from precis.utils.skill_common import parse_frontmatter
from tests._skill_roots import BUILTIN_SKILLS_DIR

SKILL_PATH = BUILTIN_SKILLS_DIR / "precis-alert-help.md"

#: One verbatim marker per clause of the gr635727 ask.
_REQUIRED_MARKERS = (
    "does nothing unless the cause is cleared",  # the headline rule
    "re-mints",  # the detector re-mints a still-live condition
    "new row under a new id",  # the re-fire is a fresh row, not a reopen
    "unpark",  # clear the flagged todo (close or unpark it)
    "intermittent",  # mark the dark host intermittent
    "al635390",  # the 2026-11-11 incident the rule was written from
    "alert-state:resolved",  # the resolution verb the incident used
)


def test_alert_skill_declares_itself() -> None:
    fm = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    assert fm.id == "precis-alert-help"
    assert fm.family == "work"
    assert fm.status == "active"
    assert "alert" in (fm.kinds or ())


def test_alert_skill_pins_the_resolve_refire_warning() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    missing = [m for m in _REQUIRED_MARKERS if m not in text]
    assert not missing, (
        f"precis-alert-help lost the resolve re-fires warning (gr635727): {missing}"
    )
