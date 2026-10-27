"""gr521593: source-only re-anchoring is scoped to the supplied qual."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis_se.traceview import reanchor


def _frame(**changes: Any) -> dict[str, Any]:
    return {
        "key": "stable-key",
        "qual": "main.build",
        "file": "main.py",
        "line": 2,
        "line_in_def": 1,
        "src": "emit('one')",
        **changes,
    }


def test_reanchor_definition_shift_preserves_offset() -> None:
    anchor = reanchor(_frame(), "# inserted\n\ndef build():\n    emit('one')\n")
    assert (anchor.file, anchor.line, anchor.granularity, anchor.note) == (
        "main.py",
        4,
        "line",
        "",
    )


def test_reanchor_insertion_inside_definition_searches_source() -> None:
    anchor = reanchor(_frame(), "def build():\n    # inserted\n    emit('one')\n")
    assert (anchor.line, anchor.granularity) == (3, "line")


@pytest.mark.parametrize(
    "replacement,line,granularity",
    [("call(1256)", 2, "line"), ("call(1567)", 1, "def")],
    ids=["ratio-at-threshold", "ratio-below-threshold"],
)
def test_reanchor_fuzzy_threshold(
    replacement: str, line: int, granularity: str
) -> None:
    # Both strings have ten chars: respectively 8 and 7 match call(1234).
    anchor = reanchor(_frame(src="call(1234)"), f"def build():\n    {replacement}\n")
    assert (anchor.line, anchor.granularity) == (line, granularity)


@pytest.mark.parametrize("offset,line", [(5, 7), (4, 3)], ids=["nearest", "tie"])
def test_reanchor_chooses_nearest_matching_line(offset: int, line: int) -> None:
    script = (
        "def build():\n"
        "    pass\n"
        "    emit('one')\n"
        "    pass\n"
        "    pass\n"
        "    pass\n"
        "    emit('one')\n"
    )
    anchor = reanchor(_frame(line_in_def=offset), script)
    assert (anchor.line, anchor.granularity) == (line, "line")


def test_reanchor_exact_candidate_wins_over_other_matches() -> None:
    script = "def build():\n    emit('one')\n    emit('one')\n"
    anchor = reanchor(_frame(line_in_def=2), script)
    assert (anchor.line, anchor.granularity) == (3, "line")


def test_reanchor_does_not_search_a_different_definition() -> None:
    script = "def build():\n    pass\n\ndef other():\n    emit('one')\n"
    anchor = reanchor(_frame(), script)
    assert (anchor.line, anchor.granularity, anchor.note) == (
        1,
        "def",
        "call moved or was removed",
    )


@pytest.mark.parametrize("src", [None, "a_completely_different_call()"])
def test_reanchor_without_matching_source_falls_back_to_def(src: str | None) -> None:
    script = "# first\n@decorate\ndef build():\n    pass\n"
    anchor = reanchor(_frame(src=src), script)
    assert (anchor.line, anchor.granularity, anchor.note) == (
        3,
        "def",
        "call moved or was removed",
    )


@pytest.mark.parametrize(
    "qual,script,offset,line",
    [
        (
            "main.outer.<locals>.build",
            "def outer():\n    def build():\n        emit('one')\n",
            1,
            3,
        ),
        (
            "main.Worker.build",
            "class Worker:\n    async def build(self):\n        emit('one')\n",
            1,
            3,
        ),
        (
            "main.build",
            "@decorate\ndef build():\n    emit('one')\n",
            2,
            3,
        ),
        ("main.build", "async def build():\n    emit('one')\n", 1, 2),
    ],
    ids=["nested-function", "async-method", "decorated-offset", "async-function"],
)
def test_reanchor_resolves_python_qualified_names(
    qual: str, script: str, offset: int, line: int
) -> None:
    anchor = reanchor(_frame(qual=qual, line_in_def=offset), script)
    assert (anchor.line, anchor.granularity) == (line, "line")


@pytest.mark.parametrize("qual", ["main", "main.<module>"])
def test_reanchor_module_scope(qual: str) -> None:
    anchor = reanchor(_frame(qual=qual), "# first\nemit('one')\n")
    assert (anchor.line, anchor.granularity) == (2, "line")


@pytest.mark.parametrize(
    "qual,script",
    [
        ("main.missing", "def build():\n    pass\n"),
        ("other.build", "def build():\n    pass\n"),
        ("main.build", "def build():\n    pass\ndef build():\n    pass\n"),
        ("main.build", "def build(:\n"),
    ],
    ids=["missing-def", "wrong-module", "ambiguous-def", "invalid-syntax"],
)
def test_reanchor_unresolved_locations_are_explicit(qual: str, script: str) -> None:
    anchor = reanchor(_frame(qual=qual), script)
    assert (anchor.file, anchor.line, anchor.granularity) == (
        "main.py",
        None,
        "unresolved",
    )
    assert anchor.note


def test_reanchor_caps_source_and_preserves_historical_frame() -> None:
    source = f"emit('{130 * 'x'}')"
    frame = _frame(src=source[:120], args={"nested": [1, 2]})
    before = deepcopy(frame)
    anchor = reanchor(frame, f"def build():\n    {source}\n")
    assert (anchor.line, anchor.granularity) == (2, "line")
    assert frame == before
