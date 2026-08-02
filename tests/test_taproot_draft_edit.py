"""``edit(kind='draft', id=<scope>, taproot=True, ...)`` — the draft-level
Taproot backfill door (ADR 0073/0074), ``DraftHandler._taproot_backfill`` /
``_render_backfill``.

Handler-level: scope resolution, apply-gating, the ``ref_level`` forward,
the ``dry_run`` conflict guard, the stateless-embedder guard, and the
table/figure derived-kind skip. ``precis.taproot.backfill.plan_chunk`` /
``apply_chunk`` are monkeypatched to fakes that record the ``(store,
embedder, chunk_id, ref_level)`` they were called with — the cascade
internals (extract/block/judge/merge/locate) are already covered by
``tests/test_taproot_backfill.py``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.store.store import Store


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


def _registered(hub: Hub) -> DraftHandler:
    """Build the handler AND register it with the hub — ``_register_with``
    is what stashes ``self.hub`` (dispatch does this on load; a bare
    ``DraftHandler(hub=...)`` leaves ``self.hub`` unset, the "stateless
    build" shape the embedder guard reads)."""
    h = DraftHandler(hub=hub)
    h._register_with(hub)
    return h


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(hub: Hub, slug: str) -> list[Any]:
    ref = hub.store.get_ref(kind="draft", id=slug)
    return hub.store.reading_order(ref.id)


def _seed_sectioned_draft(draft: DraftHandler, hub: Hub) -> dict[str, str]:
    """Build a draft ``nt``:

        T (title)
        Section A
          Para A1 [pc1].
          <table chunk>       -- derived kind, always skipped
        Section B
          Para B1 [pc2].

    Returns the ``dc<id>`` handle of every node, keyed by role.
    """
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    title_dc = _order(hub, "nt")[0].dc

    r = draft.put(
        id="nt", chunk_kind="heading", text="Section A", at={"after": title_dc}
    )
    sec_a_dc = _dc(r.body)
    r = draft.put(
        id="nt", chunk_kind="paragraph", text="Para A1 [pc1].", at={"into": sec_a_dc}
    )
    para_a1_dc = _dc(r.body)
    # A table chunk seeded directly at the store layer (derived text — put()
    # table validation isn't the point here, only that it gets skipped).
    ref = hub.store.get_ref(kind="draft", id="nt")
    table_chunks = hub.store.add_chunks(
        ref_id=ref.id,
        chunk_kind="table",
        text="| x |\n|---|\n| 1 |",
        at={"into": sec_a_dc},
        split=False,
    )
    table_dc = table_chunks[0].dc

    r = draft.put(
        id="nt", chunk_kind="heading", text="Section B", at={"after": sec_a_dc}
    )
    sec_b_dc = _dc(r.body)
    r = draft.put(
        id="nt", chunk_kind="paragraph", text="Para B1 [pc2].", at={"into": sec_b_dc}
    )
    para_b1_dc = _dc(r.body)

    return {
        "title": title_dc,
        "sec_a": sec_a_dc,
        "para_a1": para_a1_dc,
        "table": table_dc,
        "sec_b": sec_b_dc,
        "para_b1": para_b1_dc,
    }


def _chunk_id(dc: str) -> int:
    return int(dc[2:])


# ── fakes recording (store, embedder, chunk_id, ref_level) ──────────────


def _recorder() -> tuple[list[tuple[Any, Any, int, bool]], Any]:
    calls: list[tuple[Any, Any, int, bool]] = []

    def _fake_plan_chunk(
        store: Any, embedder: Any, chunk_id: int, *, ref_level: bool = False, **_kw: Any
    ) -> Any:
        calls.append((store, embedder, chunk_id, ref_level))
        return SimpleNamespace(plans=[], n_ungrounded=0)

    return calls, _fake_plan_chunk


def _apply_recorder() -> tuple[list[tuple[Any, Any, Any, int, str, bool]], Any]:
    calls: list[tuple[Any, Any, Any, int, str, bool]] = []

    def _fake_apply_chunk(
        store: Any,
        embedder: Any,
        draft_handler: Any,
        chunk_id: int,
        *,
        set_by: str = "agent",
        ref_level: bool = False,
        **_kw: Any,
    ) -> Any:
        calls.append((store, embedder, draft_handler, chunk_id, set_by, ref_level))
        return SimpleNamespace(plans=[], n_ungrounded=0)

    return calls, _fake_apply_chunk


def _never_called(name: str) -> Any:
    def _f(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"{name} should not have been called")

    return _f


# ── scope resolution + apply-gating ──────────────────────────────────────


def test_preview_default_calls_plan_chunk_not_apply_chunk(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)
    monkeypatch.setattr(
        "precis.taproot.backfill.apply_chunk", _never_called("apply_chunk")
    )

    out = draft.edit(id="nt", taproot=True)

    assert "DRY-RUN" in out.body
    got_ids = {c[2] for c in calls}
    expected_ids = {
        _chunk_id(handles[k]) for k in ("title", "sec_a", "para_a1", "sec_b", "para_b1")
    }
    assert got_ids == expected_ids
    assert _chunk_id(handles["table"]) not in got_ids  # derived kind skipped
    # store/embedder threaded through to the cascade.
    assert all(c[0] is hub.store for c in calls)
    assert all(c[1] is hub.embedder for c in calls)
    assert all(c[3] is False for c in calls)  # ref_level default


def test_apply_true_calls_apply_chunk_not_plan_chunk(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    apply_calls, fake_apply = _apply_recorder()
    monkeypatch.setattr("precis.taproot.backfill.apply_chunk", fake_apply)
    monkeypatch.setattr(
        "precis.taproot.backfill.plan_chunk", _never_called("plan_chunk")
    )

    out = draft.edit(id="nt", taproot=True, apply=True)

    assert "applied" in out.body
    assert "DRY-RUN" not in out.body
    got_ids = {c[3] for c in apply_calls}
    expected_ids = {
        _chunk_id(handles[k]) for k in ("title", "sec_a", "para_a1", "sec_b", "para_b1")
    }
    assert got_ids == expected_ids
    assert _chunk_id(handles["table"]) not in got_ids
    # draft_handler positional arg is this same handler (apply_chunk
    # re-enters edit(text=...) through it).
    assert all(c[2] is draft for c in apply_calls)
    assert all(c[4] == "agent" for c in apply_calls)  # set_by


def test_ref_level_forwarded_to_the_cascade(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _registered(hub)
    _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)

    draft.edit(id="nt", taproot=True, ref_level=True)

    assert calls  # at least one chunk in scope
    assert all(c[3] is True for c in calls)


def test_scope_heading_addresses_only_its_subtree(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)

    draft.edit(id=handles["sec_a"], taproot=True)

    got_ids = {c[2] for c in calls}
    assert got_ids == {_chunk_id(handles["sec_a"]), _chunk_id(handles["para_a1"])}
    assert _chunk_id(handles["table"]) not in got_ids  # derived kind
    assert _chunk_id(handles["title"]) not in got_ids
    assert _chunk_id(handles["sec_b"]) not in got_ids
    assert _chunk_id(handles["para_b1"]) not in got_ids


def test_scope_leaf_chunk_addresses_only_itself(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)

    draft.edit(id=handles["para_b1"], taproot=True)

    got_ids = {c[2] for c in calls}
    assert got_ids == {_chunk_id(handles["para_b1"])}


# ── guards ────────────────────────────────────────────────────────────


def test_dry_run_with_taproot_is_rejected(store: Store) -> None:
    """``dry_run=`` is a redundant/ambiguous flag alongside ``taproot=`` —
    the op previews by default (``apply=False``) and commits on
    ``apply=True``; same shape as the ``sub=`` guard just above it."""
    draft = DraftHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="dry_run is not used with taproot"):
        draft.edit(id="nt", taproot=True, dry_run=True)


def test_stateless_build_needs_an_embedder(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler whose ``self.hub`` has no embedder (a stateless server, or
    — as here — a hub registered with none) refuses to run the cascade:
    it makes in-process ANN + LLM calls."""
    draft = _registered(hub_no_embedder)
    monkeypatch.setattr(
        "precis.taproot.backfill.plan_chunk", _never_called("plan_chunk")
    )
    monkeypatch.setattr(
        "precis.taproot.backfill.apply_chunk", _never_called("apply_chunk")
    )
    proj = draft.hub.store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)

    with pytest.raises(BadInput, match="needs an embedder"):
        draft.edit(id="nt", taproot=True)


def test_omitted_id_refuses_a_corpus_wide_sweep(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``allow_all=False`` (the fix): an OMITTED ``id=`` must never mean
    "every draft in the corpus" for a Taproot backfill — mirrors the
    ``sub=`` substitute guard. A missing-kwarg slip must not trigger a
    corpus-wide LLM-driven rewrite."""
    draft = _registered(hub)
    monkeypatch.setattr(
        "precis.taproot.backfill.plan_chunk", _never_called("plan_chunk")
    )
    monkeypatch.setattr(
        "precis.taproot.backfill.apply_chunk", _never_called("apply_chunk")
    )

    with pytest.raises(BadInput, match="no corpus-wide"):
        draft.edit(id=None, taproot=True, apply=True)


def test_slug_id_still_resolves_the_whole_single_draft(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft slug (as opposed to an omitted ``id=``) still means "this
    whole (single) draft" — the guard narrows only the corpus-wide-sweep
    case, not the ordinary whole-draft scope."""
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)

    out = draft.edit(id="nt", taproot=True)

    assert "DRY-RUN" in out.body
    got_ids = {c[2] for c in calls}
    expected_ids = {
        _chunk_id(handles[k]) for k in ("title", "sec_a", "para_a1", "sec_b", "para_b1")
    }
    assert got_ids == expected_ids


def test_dc_id_still_resolves_a_section(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``dc<id>`` heading still scopes to just its subtree (a section) —
    same narrowing as the slug case above, for the section-scope shape."""
    draft = _registered(hub)
    handles = _seed_sectioned_draft(draft, hub)

    calls, fake_plan = _recorder()
    monkeypatch.setattr("precis.taproot.backfill.plan_chunk", fake_plan)

    draft.edit(id=handles["sec_a"], taproot=True)

    got_ids = {c[2] for c in calls}
    assert got_ids == {_chunk_id(handles["sec_a"]), _chunk_id(handles["para_a1"])}


def test_unregistered_handler_also_needs_an_embedder(store: Store) -> None:
    """A bare ``DraftHandler(hub=...)`` never went through
    ``_register_with`` — ``self.hub`` is the class-default ``None`` — same
    class of stateless-build refusal, hit via a different route."""
    draft = DraftHandler(hub=Hub(store=store, embedder=object()))
    proj = draft.store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)

    with pytest.raises(BadInput, match="needs an embedder"):
        draft.edit(id="nt", taproot=True)
