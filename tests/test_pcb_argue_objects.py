"""precis.pcb.argue — board-object handles are the selection catalogue keys,
verbatim (gr479844 follow-up). DB-free."""

from __future__ import annotations

from typing import Any

from precis.pcb import argue

VALID: dict[str, Any] = {
    "parts": ["U1"],
    "pins": {"U1": ["1"]},
    "nets": ["GND"],
    "features": ["outline"],
}

OBJECTS: dict[str, dict[str, Any]] = {
    "via:1.0000:2.0000": {"kind": "via", "key": "via:1.0000:2.0000"},
    "outline:0": {"kind": "outline", "key": "outline:0"},
}


def test_tokenizer_takes_whole_object_handles() -> None:
    text = (
        "move via:1.0000:2.0000 off outline:0, see silk:F.Silk:ref:R1 and fiducial:1."
    )
    assert [c for c in argue.candidates(text) if ":" in c] == [
        "via:1.0000:2.0000",
        "outline:0",
        "silk:F.Silk:ref:R1",
        "fiducial:1",
    ]


def test_feature_outline_and_bare_prose_are_unchanged() -> None:
    assert "feature:outline" in argue.candidates("see feature:outline")
    assert argue.classify("feature:outline", VALID) == "feature"
    assert argue.candidates("the outline: is odd") == ["the", "outline", "is", "odd"]


def test_classify_object_only_when_key_in_catalogue() -> None:
    valid = {**VALID, "objects": OBJECTS}
    assert argue.classify("via:1.0000:2.0000", valid) == "object"
    assert argue.classify("via:9.0000:9.0000", valid) is None
    assert argue.classify("outline:0", VALID) is None  # no catalogue given
    assert argue.is_object_handle("fiducial:1")
    assert not argue.is_object_handle("net:X")
    assert argue.all_handles(valid)["objects"] == ["outline:0", "via:1.0000:2.0000"]
    assert "objects" not in argue.all_handles(VALID)
