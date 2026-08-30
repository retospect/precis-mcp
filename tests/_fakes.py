"""Shared seed for the per-test-file ``FakeStore`` doubles.

Several test files independently defined a ``class FakeStore`` — the
same name, six genuinely different shapes, because each one duck-types
only the slice of ``precis.store.Store`` its code path touches (a
settings-gate probe, lens sampling, budget SQL cursors, lexical
search, block search, or the sprawling precis_web route surface). That
diversity is real, not accidental duplication, so this module does
*not* try to force one do-everything fake. It holds only the handful
of things that were byte-for-byte identical across two or more of the
originals — a ``pool = None`` default and a canned
``ref_id -> blocks`` map backing ``list_chunks_for_ref`` — and every
per-file fake subclasses it, adding whatever is genuinely specific to
its own tests.

KNOWN LIMITATION — read before trusting a green test built on this:
none of these fakes parse SQL. A method like ``search_refs_lexical``
or ``count_chunks_lexical`` just returns canned Python objects it was
handed; a bug in the *real* query string — a stray ``%`` in a ``LIKE``
pattern, wrong parameter binding, a broken ``WHERE`` clause — passes
silently here. Any code path that executes raw SQL against a real
connection needs a companion real-Postgres test, not just a FakeStore
unit test — see the ``*_sql.py`` pattern used elsewhere in this suite:
``tests/precis_web/test_drive_sql.py``,
``tests/precis_web/test_status_sql.py``,
``tests/precis_web/test_structure_sql.py``.
"""

from __future__ import annotations

from typing import Any


class FakeStore:
    """Minimal shared seed for the per-test-file ``FakeStore`` fakes.

    Subclasses add the settings gate, lens traditions, budget SQL
    cursors, lexical/block search, or full precis_web route surface —
    whatever their own tests actually exercise. Nothing here should be
    read as an authoritative ``Store`` contract; it only needs to
    satisfy the call sites each subclass's tests hit.
    """

    chunks = property(
        lambda self: self
    )  # chunks carve: flat fake doubles as its own sub-store

    #: Advisory-lock / raw-SQL call sites that read ``store.pool``
    #: degrade gracefully against ``None`` — the intended behaviour for
    #: a non-Postgres fixture. Subclasses that exercise a real
    #: SQL-execute path (budget meter, precis_web route SQL) replace
    #: this with a richer fake pool/connection in their own ``__init__``.
    pool: Any = None

    def __init__(self) -> None:
        #: ``ref_id -> [block-like, ...]``, read by
        #: ``list_chunks_for_ref``. Populate in a subclass ``__init__``.
        self._blocks: dict[int, list[Any]] = {}

    def list_chunks_for_ref(self, ref_id: int) -> list[Any]:
        return list(self._blocks.get(ref_id, []))
