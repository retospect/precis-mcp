"""Tests for the claims-v0 extractor (``quest/claims.py``).

DB-backed (real ``chunks``/``chunk_tags`` via the ``store`` fixture) with a
fake LLM client — no network. Covers the ``ROLE3:own`` selector + the
extractor's parse/map/drop behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.quest.claims import extract_claims, own_chunks
from precis.store.types import Tag
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk, seed_ref


class _FakeClient:
    """Records calls; returns a fixed completion text (like ``LlmClient``)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=7)


def _tag(store: Any, ref_id: int, ord_: int, value: str) -> None:
    store.add_tag(ref_id, Tag.closed("ROLE3", value), pos=ord_, set_by="agent")


def _seed_paper_with_role3(
    store: Any, chunks: list[tuple[str, str]]
) -> tuple[int, list[int]]:
    """Seed a paper with ``chunks = [(text, role3_value), ...]``; ROLE3-tag
    each chunk at its ord. Returns ``(ref_id, chunk_ids)``."""
    ref_id = seed_ref(store, title="claims test paper")
    chunk_ids = []
    for i, (text, value) in enumerate(chunks):
        cid = seed_chunk(store, ref_id=ref_id, text=text, ord=i)
        chunk_ids.append(cid)
        _tag(store, ref_id, i, value)
    return ref_id, chunk_ids


class TestOwnChunks:
    def test_selects_only_role3_own(self, store: Any) -> None:
        ref_id, chunk_ids = _seed_paper_with_role3(
            store,
            [
                ("This is background prior work.", "background"),
                ("We show a 12% improvement over baseline.", "own"),
                ("Figure captions and acknowledgments.", "furniture"),
                ("We further demonstrate a novel catalyst design.", "own"),
            ],
        )

        rows = own_chunks(store, ref_id)

        assert [r["ord"] for r in rows] == [1, 3]
        assert rows[0]["text"] == "We show a 12% improvement over baseline."
        assert rows[0]["handle"] == handle_registry.format_handle(
            "paper", chunk_ids[1], chunk=True
        )
        assert rows[1]["handle"] == handle_registry.format_handle(
            "paper", chunk_ids[3], chunk=True
        )

    def test_excludes_other_papers_chunks(self, store: Any) -> None:
        ref_id, _ = _seed_paper_with_role3(
            store, [("We show a 12% improvement over baseline.", "own")]
        )
        other_ref_id, _ = _seed_paper_with_role3(
            store, [("A different paper's own claim entirely.", "own")]
        )

        rows = own_chunks(store, ref_id)

        assert len(rows) == 1
        assert rows[0]["text"] == "We show a 12% improvement over baseline."
        # sanity: the other paper's own chunk really is retrievable on its own
        assert len(own_chunks(store, other_ref_id)) == 1

    def test_no_role3_tags_yields_empty(self, store: Any) -> None:
        ref_id = seed_ref(store, title="unclassified paper")
        seed_chunk(store, ref_id=ref_id, text="Untagged paragraph.", ord=0)

        assert own_chunks(store, ref_id) == []


class TestExtractClaims:
    def test_maps_source_index_to_ord_and_handle(self, store: Any) -> None:
        ref_id, chunk_ids = _seed_paper_with_role3(
            store,
            [
                ("We show a 12% improvement over baseline.", "own"),
                ("We further demonstrate a novel catalyst design.", "own"),
                ("This is background prior work.", "background"),
            ],
        )
        client = _FakeClient(
            '[{"claim": "12% improvement over baseline.", "source": 0}, '
            '{"claim": "A novel catalyst design.", "source": 1}]'
        )

        claims = extract_claims(store, client, ref_id)

        assert len(client.calls) == 1
        assert claims == [
            {
                "text": "12% improvement over baseline.",
                "source_ord": 0,
                "source_handle": handle_registry.format_handle(
                    "paper", chunk_ids[0], chunk=True
                ),
            },
            {
                "text": "A novel catalyst design.",
                "source_ord": 1,
                "source_handle": handle_registry.format_handle(
                    "paper", chunk_ids[1], chunk=True
                ),
            },
        ]

    def test_no_own_chunks_returns_empty_and_never_calls_client(
        self, store: Any
    ) -> None:
        ref_id, _ = _seed_paper_with_role3(
            store, [("This is background prior work.", "background")]
        )
        client = _FakeClient('[{"claim": "should not be reached", "source": 0}]')

        claims = extract_claims(store, client, ref_id)

        assert claims == []
        assert client.calls == []

    def test_unparseable_model_output_returns_empty_no_raise(self, store: Any) -> None:
        ref_id, _ = _seed_paper_with_role3(
            store, [("We show a 12% improvement over baseline.", "own")]
        )
        client = _FakeClient("sorry, I cannot help with that")

        claims = extract_claims(store, client, ref_id)

        assert claims == []

    def test_out_of_range_source_index_is_dropped(self, store: Any) -> None:
        ref_id, chunk_ids = _seed_paper_with_role3(
            store,
            [
                ("We show a 12% improvement over baseline.", "own"),
            ],
        )
        client = _FakeClient(
            '[{"claim": "12% improvement over baseline.", "source": 0}, '
            '{"claim": "a hallucinated claim", "source": 5}]'
        )

        claims = extract_claims(store, client, ref_id)

        assert claims == [
            {
                "text": "12% improvement over baseline.",
                "source_ord": 0,
                "source_handle": handle_registry.format_handle(
                    "paper", chunk_ids[0], chunk=True
                ),
            }
        ]
