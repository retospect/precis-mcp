"""Pins the ``Store`` ↔ ``store.drafts`` (:class:`DraftStore`) facade wiring
carved in ``docs/backlog/codereview-store-decomposition.md``.

Not a test of draft *semantics* (``test_draft_handler.py`` owns that) — just
that every transitional flat delegation on ``Store`` exists, has the exact
same signature as its ``DraftStore`` counterpart, and actually reaches the
same underlying data as calling ``store.drafts`` directly.
"""

from __future__ import annotations

import inspect

from precis.store._draft_ops import DraftStore
from precis.store.store import Store


def _public_callables(cls: type) -> set[str]:
    """Public, non-underscore, non-``object``-inherited callables on
    ``cls`` — the ``DraftStore`` surface the ``Store`` facade must mirror.
    Excludes ``pool``/``tx`` (lifecycle plumbing, not domain ops) and any
    ``property`` (there are none besides ``pool`` today, but a future one
    shouldn't silently need a same-named ``Store`` method — a property
    delegation is a different, deliberate change)."""
    names: set[str] = set()
    for name in dir(cls):
        if name.startswith("_") or name in ("pool", "tx"):
            continue
        if hasattr(object, name):
            continue
        member = inspect.getattr_static(cls, name)
        if isinstance(member, property):
            continue
        if not callable(getattr(cls, name)):
            continue
        names.add(name)
    return names


def test_store_no_longer_mixes_drafts_into_its_own_mro() -> None:
    """The carve: ``drafts`` is composed (``store.drafts``), not mixed into
    ``Store``'s bases — ``DraftMixin``/``_AbbrevMixin`` must be gone from
    ``Store.__mro__``."""
    mro_names = {c.__name__ for c in Store.__mro__}
    assert "DraftMixin" not in mro_names
    assert "_AbbrevMixin" not in mro_names


def test_every_draftstore_public_method_has_a_matching_store_delegation() -> None:
    """Every public ``DraftStore`` method (including ones inherited from
    ``_AbbrevMixin``) has a same-named, same-signature transitional
    delegation on ``Store`` — the facade the ``docs/backlog`` item promises
    "ZERO call sites change" against."""
    draftstore_methods = _public_callables(DraftStore)
    assert len(draftstore_methods) > 50, (
        "sanity check — expected dozens of DraftStore methods, got "
        f"{len(draftstore_methods)}; did the reflection break?"
    )
    for name in sorted(draftstore_methods):
        assert hasattr(Store, name), f"Store has no flat delegation for drafts.{name}"
        store_sig = inspect.signature(getattr(Store, name))
        draft_sig = inspect.signature(getattr(DraftStore, name))
        assert store_sig == draft_sig, (
            f"{name}: Store delegation signature {store_sig} != "
            f"DraftStore signature {draft_sig}"
        )


def test_drafts_substore_and_flat_facade_share_the_same_data(store: Store) -> None:
    """A tiny live-store smoke test — not draft semantics, just that
    ``store.drafts.*`` and the flat ``store.*`` delegation read/write the
    same rows through the same shared :class:`~precis.store.core.StoreCore`."""
    project_id = store.insert_ref(
        kind="todo", slug=None, title="drafts-facade-smoke-project"
    ).id
    ref, _title_chunk = store.drafts.create_draft(
        name="drafts-facade-smoke-draft",
        title="Drafts Facade Smoke Draft",
        project_ref_id=project_id,
    )

    # Write via the sub-store, read back via both paths.
    [chunk] = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="written through store.drafts",
    )
    via_substore = store.drafts.get_draft_chunk(chunk.dc)
    via_facade = store.get_draft_chunk(chunk.dc)
    assert via_substore is not None
    assert via_facade is not None
    assert via_substore == via_facade

    # Write via the flat facade delegation, read back via both paths.
    [chunk2] = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="written through the flat store.add_chunks delegation",
    )
    assert store.get_draft_chunk(chunk2.dc) == store.drafts.get_draft_chunk(chunk2.dc)
