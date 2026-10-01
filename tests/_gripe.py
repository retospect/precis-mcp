"""Shared fixture helper: insert a gripe ref that satisfies the STATUS invariant.

Migration 0175 makes "every gripe has exactly one valid STATUS tag" a
commit-time constraint, so a raw ``store.insert_ref(kind='gripe')`` with
no tag in the same transaction fails. Tests that need a bare gripe row go
through here instead of weakening the trigger.
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
