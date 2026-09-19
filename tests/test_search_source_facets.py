"""``search(cited=<draft>)`` / ``search(hubbed=...)`` — the inclusion
mirror of ``uncited=`` and the claim-hub-supporter facet
(the read-for-question loop (skill precis-read-for-question) slice 4), plus the toc readiness
line (docs/backlog/embed-status-hint-three-state.md).

Mirrors ``tests/test_uncited_search.py``'s style: end-to-end through the
runtime dispatcher, real store, real ``DraftHandler``/taproot hub writes.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis.workers.embed import failed_embedding_count, unembedded_chunk_count
from precis.workers.llm_summarize import SUMMARIZER_NAME, unsummarized_chunk_count

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


def _search(rt: PrecisRuntime, **extra: Any):
    args: dict[str, Any] = {"kind": "paper", "q": _Q, "page_size": 20}
    args.update(extra)
    return rt.dispatch_with_status("search", args)


def _pc(chunk_id: int) -> str:
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def _pa(ref_id: int) -> str:
    return handle_registry.format_handle("paper", ref_id)


# ── cited=/uncited= mutual exclusion + non-draft rejection ────────────────


def test_cited_and_uncited_together_raises(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _mk_paper(store, slug="csf-both-p", title="Both", text=f"{_Q} both facets.")
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="csf-both", title="Draft both", project=proj)
    draft_ref_id = _draft_ref_id(store, "csf-both")
    handle = f"dr{draft_ref_id}"

    body, is_error = _search(rt, cited=handle, uncited=handle)
    assert is_error
    assert "mutually exclusive" in body


def test_cited_non_draft_ref_errors(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    paper_id, _chunk = _mk_paper(
        store, slug="csf-not-a-draft", title="Not a draft", text=f"{_Q} irrelevant."
    )
    handle = handle_registry.format_handle("paper", paper_id)
    body, is_error = _search(rt, cited=handle)
    assert is_error
    assert "cannot resolve draft" in body


# ── cited= restricts to the closure (direct cite + hub supporter) ─────────


def test_cited_restricts_to_closure(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    # A: directly cited in the draft.
    a_id, a_chunk = _mk_paper(
        store,
        slug="csf-a-direct",
        title="A direct",
        text=f"{_Q} directly cited paper.",
    )
    # B: not cited directly, but is the supporter of a hub the draft cites.
    b_id, b_chunk = _mk_paper(
        store,
        slug="csf-b-supporter",
        title="B supporter",
        text=f"{_Q} hub supporter paper.",
    )
    # C: cited nowhere — must be excluded by cited=.
    _c_id, c_chunk = _mk_paper(
        store, slug="csf-c-uncited", title="C uncited", text=f"{_Q} never cited."
    )

    claim = CanonicalClaim(sentence="Pd catalyzes nitrate reduction cf", scope={})
    hub_ref_id = mint_hub(store, claim)
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=b_id, role="corroborates"
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="csf-closure", title="Draft closure", project=proj)
    a_handle = handle_registry.format_handle("paper", a_id)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    draft.put(
        id="csf-closure",
        chunk_kind="paragraph",
        text=f"See [{a_handle}] and the settled claim [{fi_handle}].",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "csf-closure")

    body, is_error = _search(rt, cited=f"dr{draft_ref_id}")
    assert not is_error
    assert _pc(a_chunk) in body
    assert _pc(b_chunk) in body
    assert _pc(c_chunk) not in body
    # 3, not 2: the closure now also counts the cited hub's own ref_id
    # (the claim-layer-in-cross-kind-search design (shipped 2026-09-19) — a hub
    # is itself a citeable cross-kind hit now), even though this
    # kind='paper'-only search can never return the hub itself as a hit.
    assert "restricted to 3 already-cited sources" in body


def test_cited_empty_closure_returns_nothing(runtime_with_store: PrecisRuntime) -> None:
    """An empty ``include_ref_ids`` (cited to zero sources) must return zero
    hits — not fall through unfiltered like an empty ``exclude_ref_ids``
    correctly does for ``uncited=``."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _mk_paper(store, slug="csf-empty-p", title="Empty", text=f"{_Q} orphan paper.")
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="csf-empty", title="Draft empty", project=proj)
    draft.put(
        id="csf-empty",
        chunk_kind="paragraph",
        text="No citations here at all.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "csf-empty")

    body, is_error = _search(rt, cited=f"dr{draft_ref_id}")
    assert not is_error
    assert "restricted to 0 already-cited sources" in body
    assert "no paper blocks match" in body.lower()


# ── hubbed= (kind='paper' only) ────────────────────────────────────────────


def test_hubbed_false_excludes_exactly_supporter_set(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    supporter_id, supporter_chunk = _mk_paper(
        store,
        slug="hb-supporter",
        title="Supporter",
        text=f"{_Q} hub supporter for hb test.",
    )
    plain_id, plain_chunk = _mk_paper(
        store, slug="hb-plain", title="Plain", text=f"{_Q} plain unhubbed paper."
    )
    claim = CanonicalClaim(sentence="Pd catalyzes nitrate reduction hb", scope={})
    hub_ref_id = mint_hub(store, claim)
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=supporter_id, role="establishes"
    )

    body, is_error = _search(rt, hubbed=False)
    assert not is_error
    assert _pc(supporter_chunk) not in body
    assert _pc(plain_chunk) in body
    assert "1 hubbed paper" in body
    assert plain_id != supporter_id


def test_hubbed_true_restricts_to_supporter_set(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    supporter_id, supporter_chunk = _mk_paper(
        store,
        slug="hb2-supporter",
        title="Supporter2",
        text=f"{_Q} hub supporter for hb2 test.",
    )
    _plain_id, plain_chunk = _mk_paper(
        store, slug="hb2-plain", title="Plain2", text=f"{_Q} plain unhubbed paper."
    )
    claim = CanonicalClaim(sentence="Pd catalyzes nitrate reduction hb2", scope={})
    hub_ref_id = mint_hub(store, claim)
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=supporter_id, role="corroborates"
    )

    body, is_error = _search(rt, hubbed=True)
    assert not is_error
    assert _pc(supporter_chunk) in body
    assert _pc(plain_chunk) not in body
    assert "restricted to 1 hubbed paper" in body


def test_hubbed_on_memory_kind_raises(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    body, is_error = rt.dispatch_with_status(
        "search", {"kind": "memory", "q": "anything", "hubbed": True}
    )
    assert is_error
    assert "hubbed=" in body
    assert "'memory'" in body or "memory" in body


# ── finding search + uncited=/cited=: verify honoured-or-raised ──────────


def test_finding_search_uncited_raises_loud(runtime_with_store: PrecisRuntime) -> None:
    """``FindingHandler.search`` fully overrides the shared numeric-ref
    ``search`` and never declares ``exclude_ref_ids`` — the gr334695
    caller-kwarg strictness gate (``runtime.dispatch._handler_accepted_kwargs``)
    catches this at the ``_invoke_handler`` boundary and raises BadInput
    rather than silently dropping the filter into ``FindingHandler.search``'s
    ``**_kw``. Pinning this so a future refactor can't quietly regress to
    the silent-swallow shape."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="fs-uncited", title="Draft fs", project=proj)
    draft.put(
        id="fs-uncited",
        chunk_kind="paragraph",
        text="No citations here.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "fs-uncited")

    body, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "finding", "q": "catalysis", "uncited": f"dr{draft_ref_id}"},
    )
    assert is_error
    assert "exclude_ref_ids" in body or "uncited" in body


def test_finding_search_cited_raises_loud(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="fs-cited", title="Draft fs cited", project=proj)
    draft.put(
        id="fs-cited",
        chunk_kind="paragraph",
        text="No citations here.",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "fs-cited")

    body, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "finding", "q": "catalysis", "cited": f"dr{draft_ref_id}"},
    )
    assert is_error
    assert "include_ref_ids" in body or "cited" in body


def test_finding_cross_kind_fanout_honours_uncited(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Unlike the single-kind ``search()`` verb (gr334695-guarded), the
    cross-kind ``search_hits`` fan-out bypasses that gate entirely — the
    known-hazard case docs/backlog/uncited-facet-patent-edgar-wiring.md
    warns about. Since the claim-layer-in-cross-kind-search design (shipped
    2026-09-19), ``FindingHandler.search_hits`` is its own override (not
    the inherited ``NumericRefHandler.search_hits``) — it too declares
    ``exclude_ref_ids=``/``include_ref_ids=`` explicitly and threads them
    into ``fused_ref_hits``: confirm a wildcard fan-out surfaces the hub
    at all (the flag flip that item shipped), then that it actually
    excludes a hub ref that's in the draft's closure."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    claim = CanonicalClaim(
        sentence="Ru accelerates nitrate reduction fanout unique zz", scope={}
    )
    hub_ref_id = mint_hub(store, claim)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="fs-fanout", title="Draft fanout", project=proj)
    draft.put(
        id="fs-fanout",
        chunk_kind="paragraph",
        text=f"Already settled [{fi_handle}].",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "fs-fanout")

    unfiltered_body, unfiltered_is_error = rt.dispatch_with_status(
        "search", {"q": "Ru accelerates nitrate reduction fanout unique zz"}
    )
    assert not unfiltered_is_error
    assert fi_handle in unfiltered_body

    body, is_error = rt.dispatch_with_status(
        "search",
        {
            "q": "Ru accelerates nitrate reduction fanout unique zz",
            "uncited": f"dr{draft_ref_id}",
        },
    )
    assert not is_error
    assert fi_handle not in body


# ── embed backlog scoping (docs/backlog/embed-status-hint-three-state.md) ──


@pytest.mark.db
def test_unembedded_chunk_count_unscoped_unchanged(store: Store) -> None:
    """The unscoped call (no ``ref_id=``) stays byte-identical: a chunk
    whose only row is ``status='failed'`` does NOT count toward the
    unscoped backlog (the existing high-water-threshold contract
    ``materialize``/``embed_batch`` share)."""
    ref = store.insert_ref(kind="paper", slug="ecu-unscoped-p", title="T")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="body text one")])
    chunk_id = store.chunks.list_chunks_for_ref(ref.id)[0].id
    with store.pool.connection() as conn:
        before = unembedded_chunk_count(conn)
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        assert row is not None
        embedder_name = row[0]
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status, content_sha) "
            "VALUES (%s, %s, 'failed', NULL)",
            (chunk_id, embedder_name),
        )
        conn.commit()
        after = unembedded_chunk_count(conn)
    # A failed row satisfies the unscoped NOT EXISTS clause's ``OR
    # o.status = 'failed'`` disjunct — the chunk drops OUT of the
    # backlog (same pre-existing "don't retry" behaviour ``ref_id=``
    # must not perturb).
    assert before == 1
    assert after == before - 1


@pytest.mark.db
def test_scoped_embed_count_reports_failed(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="ecu-scoped-p", title="T")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="body chunk zero text"),
            ChunkInsert(ord=1, text="body chunk one text"),
        ],
    )
    chunks = store.chunks.list_chunks_for_ref(ref.id)
    ok_chunk, failed_chunk = chunks[0], chunks[1]
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        assert row is not None
        embedder_name = row[0]
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, vector, status, content_sha) "
            "VALUES (%s, %s, %s, 'ok', NULL)",
            (ok_chunk.id, embedder_name, [0.0] * store.embedding_dim()),
        )
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status, content_sha) "
            "VALUES (%s, %s, 'failed', NULL)",
            (failed_chunk.id, embedder_name),
        )
        conn.commit()
        scoped_not_embedded = unembedded_chunk_count(conn, ref_id=ref.id)
        failed = failed_embedding_count(conn, ref_id=ref.id)
    # The failed chunk counts as NOT embedded in the scoped form (unlike
    # the unscoped form) and is separately reported as failed.
    assert scoped_not_embedded == 1
    assert failed == 1


#: ``unsummarized_chunk_count``'s eligibility window floors at
#: ``MIN_CHUNK_CHARS`` (200) — a too-short chunk is never eligible and
#: would always read as "summarized" (a false 0), so every fixture chunk
#: feeding that count must clear the floor.
_LONG_TEXT = "long enough body text to clear the summarizer eligibility floor. " * 4


@pytest.mark.db
def test_unsummarized_chunk_count_ref_scope(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="usc-scoped-p", title="T")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text=_LONG_TEXT + "summarized"),
            ChunkInsert(ord=1, text=_LONG_TEXT + "unsummarized"),
        ],
    )
    chunks = store.chunks.list_chunks_for_ref(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, %s, 'a summary', 'ok')",
            (chunks[0].id, SUMMARIZER_NAME),
        )
        conn.commit()
        scoped = unsummarized_chunk_count(conn, ref_id=ref.id)
    assert scoped == 1


# ── toc readiness line ─────────────────────────────────────────────────────


@pytest.mark.db
def test_toc_readiness_line_renders(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    ref = store.insert_ref(kind="paper", slug="toc-readiness-p", title="Readiness")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text=_LONG_TEXT + "embedded and summarised"),
            ChunkInsert(ord=1, text=_LONG_TEXT + "pending not yet done"),
            ChunkInsert(ord=2, text=_LONG_TEXT + "failed embedding"),
        ],
    )
    chunks = store.chunks.list_chunks_for_ref(ref.id)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        assert row is not None
        embedder_name = row[0]
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, vector, status, content_sha) "
            "VALUES (%s, %s, %s, 'ok', NULL)",
            (chunks[0].id, embedder_name, [0.0] * store.embedding_dim()),
        )
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status, content_sha) "
            "VALUES (%s, %s, 'failed', NULL)",
            (chunks[2].id, embedder_name),
        )
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, %s, 'a summary', 'ok')",
            (chunks[0].id, SUMMARIZER_NAME),
        )
        conn.commit()

    body, is_error = rt.dispatch_with_status(
        "get", {"kind": "paper", "id": _pa(ref.id), "view": "toc"}
    )
    assert not is_error
    assert "readiness: embedded 1/3 · summarised 1/3 · failed 1" in body


@pytest.mark.db
def test_toc_readiness_line_excludes_structurally_ineligible_chunks(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A ``table`` chunk and a stub below ``MIN_CHUNK_CHARS`` are both
    summarize-ineligible (``llm_summarize.SKIP_KINDS`` / the length floor)
    but embed-eligible (``EmbedHandler.skip_chunk_kinds`` only excludes
    ``references``). Each fraction's denominator must reflect only its own
    eligibility predicate — before the fix both fractions divided by the
    naive "every body chunk" total (3), silently reading the two
    ineligible chunks as "done" in the summarised count.
    """
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    ref = store.insert_ref(
        kind="paper", slug="toc-readiness-ineligible-p", title="Readiness ineligible"
    )
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text=_LONG_TEXT + "embedded and summarised"),
            ChunkInsert(
                ord=1, text=_LONG_TEXT + "a table", meta={"chunk_kind": "table"}
            ),
            ChunkInsert(ord=2, text="too short"),
        ],
    )
    chunks = store.chunks.list_chunks_for_ref(ref.id)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        assert row is not None
        embedder_name = row[0]
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, vector, status, content_sha) "
            "VALUES (%s, %s, %s, 'ok', NULL)",
            (chunks[0].id, embedder_name, [0.0] * store.embedding_dim()),
        )
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, %s, 'a summary', 'ok')",
            (chunks[0].id, SUMMARIZER_NAME),
        )
        conn.commit()

    body, is_error = rt.dispatch_with_status(
        "get", {"kind": "paper", "id": _pa(ref.id), "view": "toc"}
    )
    assert not is_error
    # embed has no length floor and only skips 'references' — the table
    # and stub chunks stay in its denominator (3). Summarize skips both
    # 'table' and anything below MIN_CHUNK_CHARS, so its denominator drops
    # to just the one eligible chunk, not the naive body-chunk total.
    assert "readiness: embedded 1/3 · summarised 1/1" in body
