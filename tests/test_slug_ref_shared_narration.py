"""``resolve_live_slug_ref``'s A1 bare-numeric narration (gr311347 #13).

A slug-addressed kind's own slug CAN be a bare numeral (e.g. a ``plan``
named after its owning project's id). When the digits the caller passed
in ARE that ref's real, registered slug, the A1 fallback's "you meant
the handle, use it next time" hint is a false self-correction — the
caller was already right. The hint should still fire for the genuine
case: bare digits that are NOT the ref's own slug (a real ref_id guess).

A lightweight fake store (no DB) isolates the narration condition from
resolution mechanics — this is pure branch logic in
``handlers/_slug_ref_shared.py``, not something that depends on a real
``ref_identifiers`` lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.hints import Hint


@dataclass
class _FakeRef:
    id: int
    slug: str | None


class _FakeStore:
    """Stands in for ``Store.get_ref`` / ``Store.emit_hint``.

    ``string_ref``/``int_ref`` mirror the two lookup branches
    ``resolve_live_slug_ref`` tries in order: a slug (string) lookup,
    then — only when that misses and the input is all-digits — a
    ref_id (int) lookup.
    """

    def __init__(
        self, *, string_ref: _FakeRef | None, int_ref: _FakeRef | None
    ) -> None:
        self._string_ref = string_ref
        self._int_ref = int_ref
        self.hints: list[Hint] = []

    def get_ref(self, *, kind: str, id: Any) -> _FakeRef | None:
        return self._int_ref if isinstance(id, int) else self._string_ref

    def emit_hint(self, hint: Hint) -> None:
        self.hints.append(hint)


def test_no_hint_when_bare_digits_are_the_refs_own_slug() -> None:
    """The caller's input already was the ref's canonical slug — no
    self-correction narrated (gr311347 #13)."""
    store = _FakeStore(
        string_ref=None,  # the slug lookup "misses" per the shared-cite_key
        int_ref=_FakeRef(id=168773, slug="168773"),  # but IS this ref's slug
    )
    ref = resolve_live_slug_ref(store, kind="plan", id="168773")  # type: ignore[arg-type]
    assert ref.id == 168773
    assert store.hints == []


def test_hint_still_fires_for_a_genuine_ref_id_guess() -> None:
    """Bare digits that are NOT the ref's own slug — the classic A1 case
    (a mistaken ref_id guess) — still gets the corrective hint."""
    store = _FakeStore(
        string_ref=None,
        int_ref=_FakeRef(id=168773, slug="my-plan-name"),
    )
    ref = resolve_live_slug_ref(store, kind="plan", id="168773")  # type: ignore[arg-type]
    assert ref.id == 168773
    assert len(store.hints) == 1
    hint = store.hints[0]
    assert "168773" in hint.text
    assert "po168773" in hint.text
