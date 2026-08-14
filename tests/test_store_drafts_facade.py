"""Pins the ``store.drafts`` (:class:`DraftStore`) carve finished in
``docs/backlog/codereview-store-decomposition.md``, plus the
``store.drafts.review`` (:class:`DraftReviewStore`) carve started in
``docs/backlog/codereview-handler-size-cleanups.md``.

Not a test of draft *semantics* (``test_draft_handler.py`` owns that) — just
that the carve holds its final shape: drafts is composed, not mixed in, and
the transitional flat delegations on ``Store`` stay deleted. The review
sub-carve is mid-flight — unlike ``drafts``/``blocks``, its transitional
flat delegations stay *on* ``DraftStore`` this round (see that module's
docstring); the checks below pin the delegation shape instead of its
absence.
"""

from __future__ import annotations

import inspect

from precis.store._draft_ops import DraftStore
from precis.store._draft_review_ops import DraftReviewStore
from precis.store.store import Store


def _public_callables(cls: type) -> set[str]:
    """Public, non-underscore, non-``object``-inherited callables on
    ``cls`` — the ``DraftStore`` domain surface."""
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


def test_no_flat_draft_delegations_remain_on_store() -> None:
    """Draft ops are reached only as ``store.drafts.*``: no public
    ``DraftStore`` method may (re)appear under its flat historical name on
    ``Store`` — a same-named method sneaking back in would silently shadow
    the sub-store and resurrect the flat-namespace collision class the
    decomposition exists to kill."""
    draftstore_methods = _public_callables(DraftStore)
    assert len(draftstore_methods) > 50, (
        "sanity check — expected dozens of DraftStore methods, got "
        f"{len(draftstore_methods)}; did the reflection break?"
    )
    leaked = sorted(n for n in draftstore_methods if hasattr(Store, n))
    assert not leaked, f"flat draft methods reappeared on Store: {leaked}"


def test_drafts_substore_reads_its_own_writes(store: Store) -> None:
    """A tiny live smoke test: ``store.drafts`` writes and reads through the
    shared :class:`~precis.store.core.StoreCore` (same pool as the host)."""
    project_id = store.insert_ref(
        kind="todo", slug=None, title="drafts-facade-smoke-project"
    ).id
    ref, _title_chunk = store.drafts.create_draft(
        name="drafts-facade-smoke-draft",
        title="Drafts Facade Smoke Draft",
        project_ref_id=project_id,
    )
    [chunk] = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="written through store.drafts",
    )
    got = store.drafts.get_draft_chunk(chunk.dc)
    assert got is not None


def test_drafts_review_is_composed_not_mixed_in() -> None:
    """The review sub-carve: ``DraftReviewStore`` is composed onto
    ``DraftStore`` (``store.drafts.review``), not mixed into its bases —
    it must not appear in ``DraftStore.__mro__``."""
    assert "DraftReviewStore" not in {c.__name__ for c in DraftStore.__mro__}


def test_drafts_review_property_is_cached(store: Store) -> None:
    """``store.drafts.review`` returns the same :class:`DraftReviewStore`
    instance on every access, same caching contract as :attr:`Store.drafts`
    / :attr:`Store.blocks` themselves."""
    assert isinstance(store.drafts.review, DraftReviewStore)
    assert store.drafts.review is store.drafts.review


def test_draft_review_delegations_have_matching_signatures() -> None:
    """Every transitional ``DraftStore`` review delegation forwards to a
    same-named :class:`DraftReviewStore` method with an identical
    parameter list (minus ``self``) — a delegation drifting out of sync
    with its target would silently swallow/misplace an argument."""
    review_methods = {
        name
        for name in dir(DraftReviewStore)
        if not name.startswith("_") and name not in ("pool", "tx")
    }
    assert review_methods, "sanity check — DraftReviewStore has no public methods?"
    for name in sorted(review_methods):
        assert hasattr(DraftStore, name), (
            f"DraftReviewStore.{name} has no transitional delegation on DraftStore"
        )
        draft_sig = inspect.signature(getattr(DraftStore, name))
        review_sig = inspect.signature(getattr(DraftReviewStore, name))
        assert draft_sig == review_sig, (
            f"DraftStore.{name} delegation signature {draft_sig} != "
            f"DraftReviewStore.{name} {review_sig}"
        )
