"""Tests for :func:`precis.quest.weave_review.mint_weave_reviews` — rung
6f of the paper-writing pipeline (docs/backlog/paper-writing-pipeline.md
§"Review — the memoized approval ledger") — plus its wiring into
:func:`precis.quest.weave_tick.weave_tick`.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import ensure_dossier
from precis.quest.weave_review import mint_weave_reviews
from precis.quest.weave_tick import weave_tick
from precis.store.types import Tag
from tests.workers._helpers import seed_chunk

_DEFAULT_EMBEDDER = "bge-m3"
_DIM = 1024


def _onehot(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


def _embed(store: Any, chunk_id: int, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _DEFAULT_EMBEDDER, vec),
        )


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _dossier_with_topic(store: Any, qid: int, *, topic: str = "mof") -> int:
    did = ensure_dossier(store, qid)
    store.add_tag(did, Tag.open(f"topic:{topic}"), set_by="agent")
    return did


def _scaffold_section(store: Any, did: int, title: str, vec: list[float]) -> str:
    dc_handle = store.scaffold_sections(did, [(title, None)])[0]
    chunk = store.get_draft_chunk(dc_handle)
    assert chunk is not None
    _embed(store, chunk.chunk_id, vec)
    return str(chunk.handle)


def _paper(
    store: Any,
    slug: str,
    title: str,
    *,
    topic: str = "mof",
    claim_text: str = "A claim.",
    vec: list[float] | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta={})
    store.add_tag(ref.id, Tag.open(f"topic:{topic}"), set_by="agent")
    seed_chunk(store, ref_id=ref.id, text=claim_text, ord=0)
    store.add_tag(ref.id, Tag.closed("ROLE3", "own"), pos=0, set_by="agent")
    if vec is not None:
        cid = store.upsert_card_combined(ref.id, "gist")
        _embed(store, cid, vec)
    return ref.id


class _FixedClaimsClient:
    def complete(self, messages: list[dict[str, str]]) -> Any:
        text = json.dumps([{"claim": "A generic own-contribution claim.", "source": 0}])
        return SimpleNamespace(text=text, total_tokens=7)


def _weave_payload_one(disposition: str = "cited-in") -> str:
    return json.dumps(
        {
            "section_text": f"Woven prose for a single paper ({disposition}).",
            "papers": [
                {
                    "index": 0,
                    "disposition": disposition,
                    "claims_used": [
                        {"text": "A claim.", "source_handle": None, "source_ord": 0}
                    ],
                }
            ],
        }
    )


class _FixedWeaveClient:
    """A single fixed weave-section response — every call in these tests
    drives exactly one prompt shape (no section-title judgment, unlike
    ``test_weave_tick.py``'s Make leg)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=7)


def _setup_one_maintain_section(store: Any) -> tuple[int, int, str, int]:
    """Quest + dossier (topic:mof) + one scaffolded/embedded section + one
    aligned paper. Returns ``(qid, did, section_handle, paper_id)``."""
    qid = _mk_quest(store, "A dossier-owning quest")
    did = _dossier_with_topic(store, qid)
    handle = _scaffold_section(store, did, "Existing Section", _onehot(0))
    pid = _paper(
        store,
        "wr-aligned",
        "Aligned Paper",
        claim_text="Aligned finding.",
        vec=_onehot(0),
    )
    return qid, did, handle, pid


class TestMintWeaveReviews:
    def test_mints_one_todo_per_lens(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest for review-todo minting")
        anchor = "dc999"

        ids = mint_weave_reviews(store, qid, anchor)

        assert len(ids) == 2
        for todo_id in ids:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert ref.parent_id == qid
            assert ref.meta.get("anchor") == anchor
            assert ref.meta.get("review") in ("flow", "cites")

        lenses = {store.get_ref(kind="todo", id=i).meta.get("review") for i in ids}
        assert lenses == {"flow", "cites"}

    def test_minted_todos_are_dispatchable(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest for review-todo dispatchability")
        anchor = "dc1000"

        ids = mint_weave_reviews(store, qid, anchor)

        for todo_id in ids:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None and ref.meta.get("llm_tier") == "sonnet"
            tags = {(t.namespace, t.prefix, t.value) for t in store.tags_for(todo_id)}
            assert ("closed", "STATUS", "open") in tags

    def test_custom_lenses(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest for a single-lens review")
        ids = mint_weave_reviews(store, qid, "dc42", lenses=("flow",))
        assert len(ids) == 1
        ref = store.get_ref(kind="todo", id=ids[0])
        assert ref is not None
        assert ref.meta.get("review") == "flow"

    def test_idempotent_on_repeat_call(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest for review-todo idempotency")
        anchor = "dc2000"

        first = mint_weave_reviews(store, qid, anchor)
        assert len(first) == 2

        second = mint_weave_reviews(store, qid, anchor)
        assert second == []

        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'todo' AND parent_id = %s "
                "AND meta ? 'review'",
                (qid,),
            ).fetchone()[0]
        assert n == 2

    def test_distinct_anchor_mints_again(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest with two woven sections")
        mint_weave_reviews(store, qid, "dc3000")
        second = mint_weave_reviews(store, qid, "dc3001")
        assert len(second) == 2


class TestWeaveTickReviewTrigger:
    def test_successful_weave_mints_reviews(self, store: Any) -> None:
        qid, did, handle, pid = _setup_one_maintain_section(store)
        client = _FixedWeaveClient(_weave_payload_one())

        result = weave_tick(store, client, qid, claims_client=_FixedClaimsClient())

        assert result["ok"] is True
        assert len(result["woven"]) == 1
        assert result["woven"][0]["ok"] is True
        body_handle = result["woven"][0]["body_handle"]
        assert len(result["review_todos"]) == 2
        for todo_id in result["review_todos"]:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            assert ref.meta.get("anchor") == body_handle
            assert ref.parent_id == qid

    def test_dry_run_mints_no_reviews(self, store: Any) -> None:
        qid, did, handle, pid = _setup_one_maintain_section(store)
        client = _FixedWeaveClient(_weave_payload_one())

        result = weave_tick(
            store, client, qid, claims_client=_FixedClaimsClient(), dry_run=True
        )

        assert result["ok"] is True
        assert result["applied"] is False
        assert result["review_todos"] == []

    def test_failed_weave_mints_no_reviews(self, store: Any, monkeypatch: Any) -> None:
        qid, did, handle, pid = _setup_one_maintain_section(store)
        client = _FixedWeaveClient(_weave_payload_one())

        import precis.quest.weave_tick as weave_tick_module

        def _conflicted_weave_section(
            store_: Any,
            client_: Any,
            did_: int,
            handle_: str,
            pids_: list[int],
            **kw: Any,
        ) -> Any:
            return {
                "ok": False,
                "error": "conflict",
                "applied": False,
                "section_handle": handle_,
            }

        monkeypatch.setattr(
            weave_tick_module, "weave_section", _conflicted_weave_section
        )

        result = weave_tick(store, client, qid, claims_client=_FixedClaimsClient())

        assert result["ok"] is True
        assert len(result["woven"]) == 1
        assert result["woven"][0]["ok"] is False
        assert result["review_todos"] == []

    def test_repeat_tick_over_unchanged_body_does_not_stack_reviews(
        self, store: Any
    ) -> None:
        qid, did, handle, pid = _setup_one_maintain_section(store)
        client = _FixedWeaveClient(_weave_payload_one())

        first = weave_tick(store, client, qid, claims_client=_FixedClaimsClient())
        assert len(first["review_todos"]) == 2
        body_handle = first["woven"][0]["body_handle"]

        # A second weave_tick over the same already-woven section directly
        # (bypassing the "nothing unintegrated" early return, which real
        # re-weaves never hit since the paper is now dispositioned) —
        # mint_weave_reviews itself must not stack duplicates.
        second_ids = mint_weave_reviews(store, qid, body_handle)
        assert second_ids == []
