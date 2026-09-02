"""``search(uncited=<draft>)`` — the query-driven discovery-facet complement
to ``get(kind='draft', view='backfill')``.

End-to-end through the runtime dispatcher (real store, real
``DraftHandler``/taproot hub writes — the exclusion set is exactly
``precis.backfill.candidates.draft_cited_ref_ids``, mirrors
``tests/test_exclude_closure.py``'s style for the sibling ``exclude=``
container resolver).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry

# Shared query for every test below — chosen so a plain lexical match hits
# every seeded paper (all share these three words), letting the tests
# isolate the ``uncited=`` filter's effect rather than query-relevance noise.
_Q = "nitrate reduction catalyst"


def _proj(hub: Hub) -> int:
    t = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
    return int(t.body.split("id=")[1].split()[0].rstrip(",.()"))


def _mk_paper(store: Store, *, slug: str, title: str, text: str) -> tuple[int, int]:
    """Insert a paper with one searchable body chunk. Returns (ref_id, chunk_id)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])
    chunk_id = store.chunks.list_chunks_for_ref(ref.id)[0].id
    return ref.id, chunk_id


def _draft_ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def _search(rt: PrecisRuntime, *, uncited: str | None = None, **extra: Any):
    args: dict[str, Any] = {"kind": "paper", "q": _Q, "page_size": 20}
    if uncited is not None:
        args["uncited"] = uncited
    args.update(extra)
    return rt.dispatch_with_status("search", args)


def _pc(chunk_id: int) -> str:
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


# ── directly-cited paper excluded ───────────────────────────────────────


def test_uncited_excludes_directly_cited_paper(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    cited_id, cited_chunk = _mk_paper(
        store,
        slug="uncited-cited-p",
        title="Cited Paper",
        text="Nitrate reduction catalyst boosts selectivity on copper.",
    )
    other_id, other_chunk = _mk_paper(
        store,
        slug="uncited-other-p",
        title="Other Paper",
        text="Nitrate reduction catalyst studies with palladium sites.",
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc1", title="Draft one", project=proj)
    handle = handle_registry.format_handle("paper", cited_id)
    draft.put(
        id="uc1", chunk_kind="paragraph", text=f"See [{handle}].", at={"last": True}
    )
    draft_ref_id = _draft_ref_id(store, "uc1")

    baseline, is_error = _search(rt)
    assert not is_error
    assert _pc(cited_chunk) in baseline
    assert _pc(other_chunk) in baseline

    body, is_error = _search(rt, uncited=f"dr{draft_ref_id}")
    assert not is_error
    assert _pc(cited_chunk) not in body
    assert _pc(other_chunk) in body
    assert "1 already-cited source excluded" in body
    assert other_id != cited_id  # sanity: two distinct papers


# ── hub-supporter closure excluded (Build 2 §G1, reused not reimplemented) ──


def test_uncited_excludes_hub_supporter_via_closure(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    supporter_id, supporter_chunk = _mk_paper(
        store,
        slug="uncited-supporter-p",
        title="Supporter",
        text="Nitrate reduction catalyst established on palladium film.",
    )
    claim = CanonicalClaim(sentence="Pd catalyzes nitrate reduction", scope={})
    hub_ref_id = mint_hub(store, claim)
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=supporter_id, role="corroborates"
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc2", title="Draft two", project=proj)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    draft.put(
        id="uc2",
        chunk_kind="paragraph",
        text=f"Well established [{fi_handle}].",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "uc2")

    body, is_error = _search(rt, uncited=f"dr{draft_ref_id}")
    assert not is_error
    assert _pc(supporter_chunk) not in body
    assert "1 already-cited source excluded" in body


# ── a contradicting paper is NOT "already cited for this point" ────────────


def test_uncited_still_returns_contradicting_paper(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The one most likely to regress: a hub's evidence closure unions only
    originators + corroborators, never contradictors — a paper that
    contradicts a cited claim must keep surfacing as a fresh gap."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    supporter_id, _supporter_chunk = _mk_paper(
        store,
        slug="uncited-supporter-p2",
        title="Supporter2",
        text="Nitrate reduction catalyst confirmed on palladium.",
    )
    contra_id, contra_chunk = _mk_paper(
        store,
        slug="uncited-contra-p",
        title="Contradictor",
        text="Nitrate reduction catalyst fails on palladium under acid.",
    )
    claim = CanonicalClaim(
        sentence="Pd catalyzes nitrate reduction efficiently", scope={}
    )
    hub_ref_id = mint_hub(store, claim)
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=supporter_id, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=contra_id, role="contradicts"
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc3", title="Draft three", project=proj)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    draft.put(
        id="uc3",
        chunk_kind="paragraph",
        text=f"Settled [{fi_handle}].",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "uc3")

    body, is_error = _search(rt, uncited=f"dr{draft_ref_id}")
    assert not is_error
    assert _pc(contra_chunk) in body  # still surfaces — not "already cited"


# ── loud failure, never a silent empty exclusion set ────────────────────


def test_uncited_unresolvable_handle_errors(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    body, is_error = _search(rt, uncited="dr999999999")
    assert is_error
    assert "cannot resolve draft" in body


def test_uncited_non_draft_ref_errors(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    paper_id, _chunk = _mk_paper(
        store,
        slug="uncited-not-a-draft",
        title="Not a draft",
        text="Nitrate reduction catalyst text, irrelevant content.",
    )
    handle = handle_registry.format_handle("paper", paper_id)
    body, is_error = _search(rt, uncited=handle)
    assert is_error
    assert "cannot resolve draft" in body


# ── an empty draft filters nothing (same hits as unfiltered) ──────────────


def test_uncited_empty_draft_matches_unfiltered_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _mk_paper(
        store,
        slug="uncited-alone-p",
        title="Alone",
        text="Nitrate reduction catalyst review of copper sites.",
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc6", title="Draft six", project=proj)
    draft.put(
        id="uc6",
        chunk_kind="paragraph",
        text="No citations here at all.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "uc6")

    baseline, is_error = _search(rt)
    assert not is_error
    filtered, is_error = _search(rt, uncited=f"dr{draft_ref_id}")
    assert not is_error
    # The note is prepended (Fix 2), not appended — see
    # ``test_uncited_note_is_prepended_not_appended`` below for why.
    assert filtered.startswith("_(uncited=")
    note_end = filtered.index("\n\n")
    assert filtered[note_end + 2 :] == baseline
    assert "0 already-cited sources excluded" in filtered[:note_end]


# ── kinds with no exclude-by-ref_id wiring: loud, never silent ────────────


def test_uncited_explicit_unsupported_kind_raises(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``edgar`` has no SQL-level exclusion wiring (unlike the paper/cfp/
    datasheet family) — an EXPLICIT request for it combined with
    ``uncited=`` must fail loudly rather than silently return unfiltered
    hits. Skipped when this build doesn't load ``edgar`` at all."""
    rt = runtime_with_store
    if "edgar" not in rt.hub.kinds:
        pytest.skip("edgar kind not loaded in this build")
    store = rt.hub.store
    assert store is not None

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc7", title="Draft seven", project=proj)
    draft.put(id="uc7", chunk_kind="paragraph", text="No citations.", at={"last": True})
    draft_ref_id = _draft_ref_id(store, "uc7")

    body, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "edgar", "q": _Q, "uncited": f"dr{draft_ref_id}"},
    )
    assert is_error
    assert "uncited=" in body
    assert "edgar" in body


def test_uncited_wildcard_drops_unsupported_kind_with_footer(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The default (unscoped) cross-kind fan-out drops a kind lacking
    exclude-by-ref_id wiring instead of raising — the common
    ``search(q=..., uncited=...)`` call must still work — but says so in
    the footer rather than silently returning that kind's hits unfiltered."""
    rt = runtime_with_store
    if "edgar" not in rt.hub.kinds:
        pytest.skip("edgar kind not loaded in this build")
    store = rt.hub.store
    assert store is not None

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc8", title="Draft eight", project=proj)
    draft.put(id="uc8", chunk_kind="paragraph", text="No citations.", at={"last": True})
    draft_ref_id = _draft_ref_id(store, "uc8")

    body, is_error = rt.dispatch_with_status(
        "search", {"q": _Q, "uncited": f"dr{draft_ref_id}"}
    )
    assert not is_error
    assert "uncited=: skipped" in body
    assert "edgar" in body


# ── shapes that pick their own seed/target set: refused with uncited= ─────
#
# ``view='dreamable'`` / ``view='stubs'`` / ``view='chase-queue'`` and the
# ``angle=``/``like=`` spray are intercepted by ``_dispatch_inner_core``
# before ``exclude_ref_ids`` is ever consulted, and their default target
# set includes ``paper`` — the exclusion set's own kind. Left alone they'd
# return fully unfiltered hits underneath a note claiming a filter ran.


def _mk_empty_draft(rt: PrecisRuntime, draft_id: str) -> str:
    """A citation-free draft, just enough for ``uncited=`` to resolve past
    the handle lookup — isolates the shape guard from handle resolution."""
    store = rt.hub.store
    assert store is not None
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id=draft_id, title="Shape guard draft", project=proj)
    draft.put(
        id=draft_id,
        chunk_kind="paragraph",
        text="No citations here.",
        at={"last": True},
    )
    return f"dr{_draft_ref_id(store, draft_id)}"


_UNFILTERED_SHAPES = [
    pytest.param({"view": "dreamable"}, id="view=dreamable"),
    pytest.param({"view": "stubs"}, id="view=stubs"),
    pytest.param({"view": "chase-queue"}, id="view=chase-queue"),
    pytest.param({"angle": 0.5}, id="angle="),
    pytest.param({"like": "paper:1"}, id="like="),
]


@pytest.mark.parametrize("shape_args", _UNFILTERED_SHAPES)
def test_uncited_rejects_unfiltered_search_shapes(
    runtime_with_store: PrecisRuntime, shape_args: dict[str, Any]
) -> None:
    rt = runtime_with_store
    token = _mk_empty_draft(rt, "uc-reject")
    body, is_error = rt.dispatch_with_status("search", {"uncited": token, **shape_args})
    assert is_error
    assert "uncited=" in body


_STILL_WORKS_WITHOUT_UNCITED = [
    pytest.param({"view": "dreamable"}, id="view=dreamable"),
    pytest.param({"view": "stubs"}, id="view=stubs"),
    pytest.param({"view": "chase-queue"}, id="view=chase-queue"),
    pytest.param({"angle": 0.5}, id="angle="),
]


@pytest.mark.parametrize("shape_args", _STILL_WORKS_WITHOUT_UNCITED)
def test_unfiltered_search_shapes_still_work_without_uncited(
    runtime_with_store: PrecisRuntime, shape_args: dict[str, Any]
) -> None:
    """The complement — the guard must not overreach. Absent ``uncited=``
    each shape must keep dispatching exactly as before; a future refactor
    that widens the guard would otherwise silently break these callers.
    (``like=`` isn't parametrized here — it needs an existing embedded
    target to resolve at all, already covered by ``test_angle_dispatch.py``;
    nothing about that resolution is guard-related.)"""
    rt = runtime_with_store
    body, is_error = rt.dispatch_with_status(
        "search", {"q": _Q, "kind": "paper", **shape_args}
    )
    assert not is_error


# ── shapes that DO honour exclude_ref_ids: must keep working WITH uncited= ─


def test_uncited_still_works_with_keywords_view(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    token = _mk_empty_draft(rt, "uc-keywords")
    body, is_error = rt.dispatch_with_status(
        "search", {"kind": "paper", "q": _Q, "uncited": token, "view": "keywords"}
    )
    assert not is_error


def test_uncited_still_works_with_folder_scope(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    fid = int(
        FolderHandler(hub=rt.hub)
        .put(text="Uncited scope")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    token = _mk_empty_draft(rt, "uc-folder")
    body, is_error = rt.dispatch_with_status(
        "search", {"kind": "paper", "q": _Q, "uncited": token, "folder": fid}
    )
    assert not is_error


def test_uncited_still_works_with_source_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    token = _mk_empty_draft(rt, "uc-sort")
    body, is_error = rt.dispatch_with_status(
        "search", {"kind": "paper", "q": _Q, "uncited": token, "sort": "recency"}
    )
    assert not is_error


def test_uncited_still_works_with_plain_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    token = _mk_empty_draft(rt, "uc-plain")
    body, is_error = _search(rt, uncited=token)
    assert not is_error


# ── the note is prepended, not appended (pagination safety) ──────────────


def test_uncited_note_is_prepended_not_appended(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``dispatch_with_status`` hands the body to
    ``PaginationCache.split``, which keeps the largest LEADING run that
    fits the byte cap. A trailing note on a result set big enough to
    paginate would strand the one signal that the filter ran on a page
    the caller never reads — so the note must come first."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _mk_paper(
        store,
        slug="uncited-note-position-p",
        title="Note position",
        text="Nitrate reduction catalyst review of ruthenium sites.",
    )
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc-note", title="Draft note", project=proj)
    draft.put(
        id="uc-note",
        chunk_kind="paragraph",
        text="No citations here at all.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "uc-note")

    body, is_error = _search(rt, uncited=f"dr{draft_ref_id}")
    assert not is_error
    assert body.startswith("_(uncited=")
    # the note must precede any body content, not trail it
    note_end = body.index("\n\n")
    assert "already-cited sources excluded" in body[:note_end]


def test_uncited_note_visible_on_first_page_when_result_paginates(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix matters precisely when the body is big enough that
    ``PaginationCache.split`` actually stashes a tail behind a
    ``more()`` cursor — the note must land on the page the caller
    actually reads, not the one behind the cursor."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    for i in range(150):
        _mk_paper(
            store,
            slug=f"uncited-page-p{i}",
            title=f"Paginating paper {i}",
            text="Nitrate reduction catalyst " * 30 + f"distinct variant {i}.",
        )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="uc-paginate", title="Draft paginate", project=proj)
    draft.put(
        id="uc-paginate",
        chunk_kind="paragraph",
        text="No citations here at all.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "uc-paginate")

    # Cursor pagination only engages on long-lived runtimes (gr267466);
    # this test is about which page the note lands on, so opt in.
    rt.long_lived = True
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "500")
    body, is_error = _search(rt, uncited=f"dr{draft_ref_id}", page_size=150)
    assert not is_error
    assert "more(cursor=" in body, "test setup didn't actually paginate"
    assert "_(uncited=" in body, body
    more_idx = body.index("more(cursor=")
    note_idx = body.index("_(uncited=")
    assert note_idx < more_idx
    assert body.startswith("_(uncited=")
