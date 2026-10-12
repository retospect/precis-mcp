"""``put(kind='draft', chunk_kind='term', terms=[...])`` — the batch form.

One call files many registry ``term`` leaves in a single transaction
(``store.atomic()``): per-term outcome lines in entry order, and the
first rejected term rolls every entry — including a minted registry
heading — back, naming its ``terms[i]`` position. Verb-level ``meta=`` /
``at=`` are per-batch defaults an entry overrides. Sized by
:data:`~precis.handlers._numeric_ref.BATCH_ID_CAP` (the same 50 the
``id=[...]`` batches use). Real Postgres (the ``hub`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import BATCH_ID_CAP
from precis.handlers.draft import DraftHandler
from precis.runtime import PrecisRuntime


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _mk(hub: Hub, draft: DraftHandler) -> int:
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    return ref.id


def _terms(hub: Hub, ref_id: int) -> list[Any]:
    return [
        c for c in hub.live_store.drafts.reading_order(ref_id) if c.chunk_kind == "term"
    ]


def _term_dcs(body: str) -> list[str]:
    return re.findall(r"dc\d+", body)


# ── the happy path ─────────────────────────────────────────────────


def test_terms_batch_files_under_glossary_with_per_term_lines(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    body = draft.put(
        id="nt",
        chunk_kind="term",
        terms=[
            {"text": "polyethylenimine", "meta": {"short": "PEI"}},
            {"text": "metal-organic framework", "meta": {"short": "MOF"}},
            "a third term with no meta",
        ],
    ).body
    assert body.startswith("added 3 terms to 'nt' in one transaction")
    dcs = _term_dcs(body)
    assert len(dcs) == 3
    chunks = _terms(hub, ref_id)
    assert len(chunks) == 3
    # all filed under the auto-created Glossary home heading
    gloss = hub.live_store.drafts.ensure_registry_heading(ref_id, "glossary")
    home = hub.live_store.drafts.get_draft_chunk(gloss)
    assert home is not None
    assert all(c.parent_chunk_id == home.chunk_id for c in chunks)
    # shorts register as resolvable surfaces
    terms = hub.live_store.drafts.defined_terms(ref_id)
    assert terms["PEI"]["definition"] == "polyethylenimine"
    assert terms["MOF"]["definition"] == "metal-organic framework"


def test_terms_batch_per_entry_meta_overrides_verb_meta(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    draft.put(
        id="nt",
        chunk_kind="term",
        meta={"source": "batch-default"},
        terms=[
            {"text": "alpha", "meta": {"short": "A"}},
            {"text": "beta", "meta": {"short": "B", "source": "entry"}},
        ],
    )
    chunks = _terms(hub, ref_id)
    by_text = {c.text: c for c in chunks}
    assert by_text["alpha"].meta["source"] == "batch-default"
    assert by_text["beta"].meta["source"] == "entry"
    assert by_text["alpha"].meta["registry"] == "glossary"


def test_terms_batch_components_assigns_consecutive_callouts(
    draft: DraftHandler, hub: Hub
) -> None:
    """``assign="insert"`` registries freeze a callout at add time — a batch
    must number consecutively (each entry's ``registry_callouts`` read sees
    the earlier in-transaction inserts)."""
    ref_id = _mk(hub, draft)
    draft.put(
        id="nt",
        chunk_kind="term",
        terms=[
            {"text": f"part {i}", "meta": {"registry": "components"}} for i in range(3)
        ],
    )
    callouts = sorted(
        int(c.meta["callout"])
        for c in _terms(hub, ref_id)
        if (c.meta or {}).get("registry") == "components"
    )
    assert callouts == [1, 2, 3]


def test_terms_batch_explicit_at_places_entries(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    head = draft.put(id="nt", chunk_kind="heading", text="Custom home")
    anchor = _term_dcs(head.body)[0]
    draft.put(
        id="nt",
        chunk_kind="term",
        at={"into": anchor},
        terms=[{"text": "one", "meta": {"short": "ONE"}}, {"text": "two"}],
    )
    parent = hub.live_store.drafts.get_draft_chunk(anchor)
    assert parent is not None
    chunks = _terms(hub, ref_id)
    assert all(c.parent_chunk_id == parent.chunk_id for c in chunks)
    # an entry's own at= beats the batch default
    draft.put(
        id="nt",
        chunk_kind="term",
        at={"into": anchor},
        terms=[
            {
                "text": "loose",
                "meta": {"short": "LOOSE"},
                "at": {"first": True},
            }
        ],
    )
    loose = next(c for c in _terms(hub, ref_id) if c.text == "loose")
    assert loose.parent_chunk_id is None or loose.parent_chunk_id != parent.chunk_id


# ── all-or-nothing ─────────────────────────────────────────────────


def test_terms_batch_rejected_term_rolls_back_all_and_names_it(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    with pytest.raises(BadInput, match=r"terms\[1\] \(BAD\): .* rolled back"):
        draft.put(
            id="nt",
            chunk_kind="term",
            terms=[
                {"text": "polyethylenimine", "meta": {"short": "PEI"}},
                {
                    "text": "cites a dead ref [dc999999]",
                    "meta": {"short": "BAD"},
                },
            ],
        )
    # nothing landed — not even the auto-minted Glossary heading
    assert _terms(hub, ref_id) == []
    headings = [
        c
        for c in hub.live_store.drafts.reading_order(ref_id)
        if c.chunk_kind == "heading" and (c.meta or {}).get("registry")
    ]
    assert headings == []


def test_terms_batch_bad_at_rolls_back_all(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    with pytest.raises(NotFound, match=r"terms\[1\].*unknown parent"):
        draft.put(
            id="nt",
            chunk_kind="term",
            terms=[
                {"text": "fine", "meta": {"short": "FINE"}},
                {"text": "orphan", "at": {"into": "dc999999999"}},
            ],
        )
    assert _terms(hub, ref_id) == []


# ── shape and combination refusals ─────────────────────────────────


def test_terms_batch_shape_errors(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    not_a_list: Any = "not-a-list"
    with pytest.raises(BadInput, match="takes a list"):
        draft.put(id="nt", chunk_kind="term", terms=not_a_list)
    with pytest.raises(BadInput, match="empty"):
        draft.put(id="nt", chunk_kind="term", terms=[])
    with pytest.raises(BadInput, match=f"at most {BATCH_ID_CAP}"):
        draft.put(
            id="nt",
            chunk_kind="term",
            terms=[{"text": f"t{i}"} for i in range(BATCH_ID_CAP + 1)],
        )
    with pytest.raises(BadInput, match=r"terms\[0\] must be a dict"):
        draft.put(id="nt", chunk_kind="term", terms=[42])
    with pytest.raises(BadInput, match=r"terms\[1\].*unknown keys"):
        draft.put(
            id="nt",
            chunk_kind="term",
            terms=[{"text": "ok"}, {"text": "x", "bogus": 1}],
        )
    with pytest.raises(BadInput, match=r"terms\[0\].*non-empty 'text'"):
        draft.put(id="nt", chunk_kind="term", terms=[{"text": "  "}])
    with pytest.raises(BadInput, match=r"terms\[0\].*'meta'.*dict"):
        draft.put(id="nt", chunk_kind="term", terms=[{"text": "x", "meta": "s"}])


def test_terms_batch_conflicting_kwargs_refused(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    good = [{"text": "x", "meta": {"short": "X"}}]
    with pytest.raises(BadInput, match="batch form of chunk_kind='term'"):
        draft.put(id="nt", chunk_kind="paragraph", terms=good)
    with pytest.raises(BadInput, match="does not combine with text="):
        draft.put(id="nt", chunk_kind="term", terms=good, text="dup")
    with pytest.raises(BadInput, match="does not combine with.*title="):
        draft.put(id="nt", chunk_kind="term", terms=good, title="T2")
    with pytest.raises(BadInput, match="copy_of= does not combine"):
        draft.put(id="nt", chunk_kind="term", terms=good, copy_of="nt")


def test_terms_batch_missing_draft_is_not_found(draft: DraftHandler, hub: Hub) -> None:
    _mk(hub, draft)
    with pytest.raises(NotFound):
        draft.put(id="no-such-draft", chunk_kind="term", terms=[{"text": "x"}])


# ── through the verb boundary ──────────────────────────────────────


def test_terms_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    _mk(hub, DraftHandler(hub=hub))
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "nt",
            "chunk_kind": "term",
            "terms": [
                {"text": "polyethylenimine", "meta": {"short": "PEI"}},
                {"text": "metal-organic framework", "meta": {"short": "MOF"}},
            ],
        },
    )
    assert "added 2 terms to 'nt' in one transaction" in body
    assert "[error" not in body


def test_terms_batch_secret_in_entry_refused_at_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    """``terms`` rides the ``_SECRET_GATED_CONTAINERS`` scan — a credential
    nested inside a batch entry's text is refused like any put text."""
    _mk(hub, DraftHandler(hub=hub))
    secret = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "nt",
            "chunk_kind": "term",
            "terms": [{"text": "fine"}, {"text": f"key is {secret}"}],
        },
    )
    assert "[error" in body
    assert secret not in body
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None and _terms(hub, ref.id) == []
