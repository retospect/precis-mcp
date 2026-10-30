"""Enclosure screw dimensions and structured refusals for unsupported sizes."""

import pytest

from precis.enclosure_screws import enclosure_screw
from precis.errors import BadInput


@pytest.mark.parametrize(
    "size,expected",
    [
        ("M2", (1.6, 2.2, 3.8, 2.0)),
        ("M2.5", (2.05, 2.7, 4.5, 2.5)),
        ("M3", (2.5, 3.2, 5.5, 3.0)),
        ("M4", (3.3, 4.2, 7.0, 4.0)),
    ],
)
def test_supported_screw_dimensions(
    size: str, expected: tuple[float, float, float, float]
) -> None:
    screw = enclosure_screw(size)
    assert (
        screw.pilot,
        screw.clearance,
        screw.head_diameter,
        screw.head_height,
    ) == expected
    assert "house rule" in screw.source
    assert "not a supplier specification" in screw.source
    assert enclosure_screw(f" {size.lower()} ") == screw


@pytest.mark.parametrize("size", ["M99", "M5", "", " ", None, 2, True, [], {}])
def test_unknown_screw_size_has_structured_recovery(size: object) -> None:
    with pytest.raises(BadInput) as error:
        enclosure_screw(size)
    assert "screw_size" in error.value.cause
    assert error.value.options == ["M2", "M2.5", "M3", "M4"]
    assert isinstance(error.value.next, str)
    assert "M2, M2.5, M3, M4" in error.value.next
