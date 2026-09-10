"""A name-keyed, append-only argument-thread ledger shape.

Lifted from ``precis_se/notes.py`` (se-kind.md slice 4 "The
propose/interrogate loop", migration ``precis_se/migrations/
0005_se_notes_freedom.sql``) to ``src/precis/utils/notes.py`` so the
``checklist`` kind (docs/backlog/checklist-kind.md, slice 1) can reuse
the same shape verbatim instead of forking it. ``precis_se.notes``
re-exports this module so existing ``precis_se`` imports (and its
migration/table names, which stay ``se_notes``) are unaffected — this
is a pure dataclass/helper module, no DB import, so the lift changes
nothing behaviourally.

A note is name-keyed (like everything else in these kinds) and one of
three kinds: a **question** ("what bearing bore?"), an **answer**, or
a **decision**. ``re`` links an answer/decision to the note it
responds to; ``about`` anchors a note to the objects it concerns
(caller-defined anchor names — se uses ``'block'`` / ``'block.measure'``
names; checklist uses item names or target subobjects — name-keyed
text, resolved only at read time: a dangling anchor is the read-time
honest annotation, never a write-time error, the relation-source
posture).

A question's open/settled state is **derived, never stored**: a live
answer or decision whose ``re`` names it settles it. That keeps the
ledger append-shaped (corrections are new notes; a caller's
``remove_note`` exists for genuine retractions) and makes a timeline
view truthful — callers that persist across a retire-all/reinsert-all
save model (se) carry ``created_at`` across saves precisely so this
ordering survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

NOTE_KINDS = ("question", "answer", "decision")


class NoteError(ValueError):
    """A malformed note payload (bad kind/origin vocab, bad anchor list)."""


@dataclass
class NoteSpec:
    """One ledger entry. ``re`` is another note's name (answers/decisions);
    ``about`` is a list of caller-defined anchor names.
    ``created_at`` is ``None`` only for a note minted this batch — persist
    stamps it on first save and carries it afterwards."""

    name: str
    kind: str
    body: str
    re: str | None = None
    about: list[str] = field(default_factory=list)
    origin: str = "user"
    created_at: datetime | None = None


def validate_about(raw: Any) -> list[str]:
    """Vet an ``about`` anchor list's *shape* — a list of non-empty
    strings (a bare string is accepted as a one-element list). Anchor
    **existence** is deliberately not checked (module docstring)."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise NoteError(f"'about' must be a list of anchor names, got {raw!r}")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise NoteError(f"'about' anchors must be non-empty strings, got {item!r}")
        out.append(item.strip())
    return out


def settled_by(notes: list[NoteSpec], question: NoteSpec) -> list[NoteSpec]:
    """The live answers/decisions whose ``re`` names ``question``."""
    return [
        n for n in notes if n.kind in ("answer", "decision") and n.re == question.name
    ]


def open_questions(notes: list[NoteSpec]) -> list[NoteSpec]:
    """Questions with no live answer/decision — the interview view's
    lead section, and what a propose job reads first."""
    return [n for n in notes if n.kind == "question" and not settled_by(notes, n)]


def dangling_anchors(notes: list[NoteSpec], resolve: Any) -> list[tuple[str, str]]:
    """``(note-name, anchor)`` pairs whose anchor doesn't resolve —
    ``resolve(anchor) -> bool`` is supplied by the caller (the handler
    knows the target). Read-time honesty, not an error."""
    out: list[tuple[str, str]] = []
    for n in notes:
        for anchor in n.about:
            if not resolve(anchor):
                out.append((n.name, anchor))
    return out
