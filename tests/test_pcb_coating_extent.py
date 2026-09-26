"""The conformal-coating extent screen (gr414481).

An EWOD board is only finished once it is parylene-coated, and the coater's
chamber caps the part independently of anything the fab can etch. Before
this there was NO board-extent cap of any kind in the package — the gripe's
premise of "a second limit beside the fab cap" was generous.

The figure itself is a physical fact about the coater in use and has not
been supplied, so it ships null. These tests therefore do two things: pin
the unconfigured behaviour (which must say "unchecked", never "clean"), and
exercise the configured path by monkeypatching the accessor, so the check
is reachable rather than dead code waiting on a number.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb import capabilities, generators


def _grid_params(**over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"grid": [4, 4]}
    params.update(over)
    return params


def test_coating_extent_ships_unconfigured() -> None:
    """Null means UNKNOWN. If someone fills the figure in, this test is the
    one that should be updated deliberately rather than quietly."""
    assert capabilities.coating_extent_mm() is None


def test_the_data_file_declares_the_key_with_both_dimensions() -> None:
    key = capabilities._load_raw()["coating_extent_mm"]
    assert set(key) >= {
        "source",
        "retrieved",
        "process",
        "note",
        "max_width_mm",
        "max_height_mm",
    }
    assert key["process"] == "parylene"


def test_a_half_configured_limit_reads_as_unconfigured(monkeypatch) -> None:
    """One dimension is not a limit — refusing to guess the other beats
    screening against half a constraint."""
    monkeypatch.setattr(
        capabilities,
        "_load_raw",
        lambda: {"coating_extent_mm": {"max_width_mm": 100.0, "max_height_mm": None}},
    )
    assert capabilities.coating_extent_mm() is None


def test_unconfigured_verdict_says_unchecked_rather_than_passing() -> None:
    verdict = generators._coating_verdict("ARR1", 18.1, 18.1)
    assert verdict["checked"] is False
    assert "NOT been screened" in verdict["reason"]
    assert verdict["array_mm"] == [18.1, 18.1]


def test_an_array_inside_the_configured_limit_passes_but_scopes_its_claim(
    monkeypatch,
) -> None:
    monkeypatch.setattr(generators, "coating_extent_mm", lambda: (100.0, 100.0))
    verdict = generators._coating_verdict("ARR1", 18.1, 18.1)
    assert verdict["checked"] is True
    assert verdict["scope"] == "array"
    assert verdict["limit_mm"] == [100.0, 100.0]
    # The board is larger than its array, so a pass here must not be
    # reported as "the board will coat".
    assert "finished board is larger" in verdict["note"]


@pytest.mark.parametrize(
    ("w", "h"),
    [(120.0, 50.0), (50.0, 120.0), (120.0, 120.0)],
)
def test_an_array_past_the_configured_limit_refuses_by_name(
    monkeypatch, w: float, h: float
) -> None:
    monkeypatch.setattr(generators, "coating_extent_mm", lambda: (100.0, 100.0))
    with pytest.raises(ValueError, match="the coating step accepts"):
        generators._coating_verdict("ARR1", w, h)


def test_the_refusal_is_decisive_because_the_board_is_never_smaller(
    monkeypatch,
) -> None:
    """Polarity, spelled out: the array under-states the board, so 'array
    too big' proves 'board too big'. The message has to say so, because the
    reader's next move is to shrink the design, not to re-measure."""
    monkeypatch.setattr(generators, "coating_extent_mm", lambda: (10.0, 10.0))
    with pytest.raises(ValueError) as exc:
        generators._coating_verdict("ARR1", 18.1, 18.1)
    assert "finished board is larger still" in str(exc.value)


def test_the_generator_ledger_carries_the_coating_verdict() -> None:
    """The check has to be REACHABLE from a real expansion, not only from a
    unit test — an unreachable check reads as a clean one."""
    expansion = generators.expand("ewod_pad_array", "ARR1", _grid_params())
    coating = expansion.ledger["coating"]
    assert coating["checked"] is False
    assert coating["array_mm"][0] > 0.0
