"""The se interrogation ledger — structured, linkable design notes
(se-kind.md slice 4 "The propose/interrogate loop", migration
``0005_se_notes_freedom.sql``).

Lifted to :mod:`precis.utils.notes` (checklist-kind.md slice 1, shared
by the ``checklist`` kind) — this module re-exports the lifted shape so
every existing ``from precis_se.notes import ...`` / ``from precis_se
import notes`` call site keeps working unchanged. Table names
(``se_notes``) and se-specific call sites are untouched; only the pure
dataclass/helper implementation moved.
"""

from __future__ import annotations

from precis.utils.notes import (
    NOTE_KINDS as NOTE_KINDS,
)
from precis.utils.notes import (
    NoteError as NoteError,
)
from precis.utils.notes import (
    NoteSpec as NoteSpec,
)
from precis.utils.notes import (
    dangling_anchors as dangling_anchors,
)
from precis.utils.notes import (
    open_questions as open_questions,
)
from precis.utils.notes import (
    settled_by as settled_by,
)
from precis.utils.notes import (
    validate_about as validate_about,
)

__all__ = [
    "NOTE_KINDS",
    "NoteError",
    "NoteSpec",
    "dangling_anchors",
    "open_questions",
    "settled_by",
    "validate_about",
]
