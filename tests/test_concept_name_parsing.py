"""Name delimiters must not consume punctuation inside scientific terms."""

import pytest

from precis.reading.concepts import split_name_def


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Lennard–Jones", ("Lennard–Jones", "")),
        ("Lennard–Jones — energy", ("Lennard–Jones", "energy")),
        ("Lennard–Jones – energy", ("Lennard–Jones", "energy")),
        ("Lennard–Jones\nenergy — scale", ("Lennard–Jones", "energy — scale")),
        ("term — definition – detail", ("term", "definition – detail")),
        ("term – definition — detail", ("term", "definition — detail")),
        ("term: detail", ("term: detail", "")),
        ("", ("", "")),
    ],
)
def test_split_name_def(text: str, expected: tuple[str, str]) -> None:
    assert split_name_def(text) == expected
