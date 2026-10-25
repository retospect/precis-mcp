"""Shared ``insert_ref`` wrappers for kinds whose invariants a bare call
misses — each helper exists because the invariant surfaced only as a
runtime failure in a test (gr515818, gr515819).

- ``insert_gripe``: migration 0176 makes "every gripe has exactly one
  valid STATUS tag" a commit-time constraint, so a raw
  ``store.insert_ref(kind='gripe')`` with no tag in the same transaction
  fails at COMMIT on the deferred ``gripe_status_check`` trigger. Tests
  that need a bare gripe row go through here instead of weakening the
  trigger.
- ``insert_draft``: ``draft`` is a slug-addressed kind
  (``kinds.is_numeric = false`` — see ``Store._validate_slug_for_kind``),
  so ``slug=None`` is refused and a slug is required up front.
"""

from __future__ import annotations

from typing import Any

from precis.store import Store
from precis.store.types import Ref, Tag


def insert_gripe(store: Store, title: str, *, status: str = "open", **kw: Any) -> Ref:
    """``insert_ref(kind='gripe')`` + its STATUS tag in one transaction."""
    kw.setdefault("meta", {})
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title=title, conn=conn, **kw)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return ref


def insert_draft(store: Store, slug: str, title: str, **kw: Any) -> Ref:
    """``insert_ref(kind='draft')`` — draft is slug-addressed, slug required."""
    return store.insert_ref(kind="draft", slug=slug, title=title, **kw)
