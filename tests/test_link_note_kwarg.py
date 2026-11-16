"""gr676754 — link's top-level ``note=`` is the kwarg spelling of
``meta={'note': …}`` and is no longer refused for numeric-ref kinds."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.utils.link_tag_ops import merge_link_note


def test_note_folds_into_meta() -> None:
    assert merge_link_note({"a": 1}, "why", mode="add") == {"a": 1, "note": "why"}


def test_note_none_is_passthrough() -> None:
    assert merge_link_note(None, None, mode="add") is None


def test_note_agrees_with_meta_note() -> None:
    meta = {"note": "x"}
    assert merge_link_note(meta, "x", mode="add") is meta


def test_note_disagreeing_with_meta_is_refused() -> None:
    with pytest.raises(BadInput, match="disagree"):
        merge_link_note({"note": "a"}, "b", mode="add")


def test_note_on_remove_is_refused() -> None:
    with pytest.raises(BadInput, match="only applies when adding"):
        merge_link_note(None, "x", mode="remove")


def test_blank_note_is_refused() -> None:
    with pytest.raises(BadInput, match="non-empty"):
        merge_link_note(None, "  ", mode="add")
