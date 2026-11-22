"""A memory-review todo has a named fallback when the python kind is off (gr667983)."""

from tests._skill_roots import skill_path


def _section() -> str:
    text = skill_path("precis-memory-help").read_text(encoding="utf-8")
    section = text.split("## Working a memory-review todo", 1)[1]
    section = section.split("\n## ", 1)[0]
    return " ".join(section.split())


def test_python_off_names_the_fallback() -> None:
    """A planner tick's host may serve no python kind, so the section that
    prescribes the repo check must say what replaces it: no retry, a
    cross-check against neighbouring memories, and a disclosed gap."""
    section = _section()
    for phrase in (
        "`Unsupported … disabled in this build`",
        "do not retry",
        "[[precis-kinds-disabled-help]]",
        "sibling hub",
        "linked memories",
        "closing comment that the repo check was not run and why",
    ):
        assert phrase in section


def test_python_off_review_records_no_stamp() -> None:
    """Agreement between memories is weaker evidence than the repo check;
    the section must state which kg-review verdict such a review records so
    the `ok` stamp does not read as allowed on it by default."""
    section = _section()
    for phrase in (
        "corroboration, not a repo check",
        "records no `kg-review` verdict, neither `ok` nor `fixed`",
        "Skip step 2",
        "or step 2 was skipped",
    ):
        assert phrase in section
