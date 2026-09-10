"""The ``precis_se.notes`` -> ``precis.utils.notes`` lift
(docs/backlog/checklist-kind.md slice 1) is a pure re-export: every name
``precis_se.notes`` exposes must be the *same object* as
``precis.utils.notes``'s, so existing ``from precis_se.notes import ...``
/ ``from precis_se import notes`` call sites (handler.py,
test_se_notes_freedom.py) keep working unchanged.
"""

from __future__ import annotations

import precis.utils.notes as core_notes
import precis_se.notes as se_notes


def test_reexported_names_are_the_same_objects() -> None:
    for name in core_notes.__dict__.get("__all__", []) or [
        "NOTE_KINDS",
        "NoteError",
        "NoteSpec",
        "validate_about",
        "settled_by",
        "open_questions",
        "dangling_anchors",
    ]:
        assert getattr(se_notes, name) is getattr(core_notes, name), name


def test_notespec_from_either_module_is_the_same_class() -> None:
    from precis.utils.notes import NoteSpec as CoreNoteSpec
    from precis_se.notes import NoteSpec as SeNoteSpec

    assert SeNoteSpec is CoreNoteSpec


def test_pure_helpers_behave_identically() -> None:
    q = core_notes.NoteSpec(name="q1", kind="question", body="bore?")
    a = core_notes.NoteSpec(name="a1", kind="answer", body="4mm", re="q1")
    assert core_notes.open_questions([q, a]) == []
    assert se_notes.open_questions([q, a]) == []
