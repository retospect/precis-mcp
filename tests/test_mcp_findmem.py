"""Tests for the MCP_FINDMEM gripe batch.

- gr538358: ``put(kind='finding', cited_in='pk<chunk_id>')`` accepts the
  patent chunk handle the way ``pc<chunk_id>`` is accepted.
- gr538466: the put ack echoes the meta keys it stored, and the default
  ``get`` render shows the writer annotation (full meta: view='raw').
- gr538071: ``q='*'`` (and a missing q with tags=) is match-all on
  ``view='count'``/``view='index'``, not a literal full-text query.
- gr538076: the weak-match fold hint names the kind's accepted
  handle-list view (memory: 'index', not the refused 'ids').
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.store.types import ChunkInsert, Tag


def _finding_handler(store) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _memory_handler(store) -> MemoryHandler:
    return MemoryHandler(hub=Hub(store=store))


def _chunk_id(store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_patent(store) -> tuple[int, int]:
    """Insert a patent ref + one chunk; return ``(ref_id, chunk_id)``."""
    ref = store.insert_ref(kind="patent", slug="ep1234567b1", title="Patent", meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="Patent body chunk.", meta={})]
    )
    return ref.id, _chunk_id(store, ref.id)


def _seed_paper_chunk(store, *, cite_key: str = "miller23a") -> tuple[int, int]:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=f"Body of {cite_key}.", meta={})]
    )
    return ref.id, _chunk_id(store, ref.id)


def _seed_memory(store, *, text: str = "a repo-dev note") -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=text[:80], meta={})
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text, meta={})])
    return ref.id


# ── gr538358: pk<chunk> in cited_in ─────────────────────────────────


class TestCitedInPatentHandle:
    def test_pk_chunk_handle_resolves(self, store) -> None:
        _ref_id, chunk_id = _seed_patent(store)
        h = _finding_handler(store)
        tgt = h._resolve_cited_in(f"pk{chunk_id}")
        assert tgt.kind == "patent"
        assert tgt.pos == 0

    def test_put_with_pk_handle(self, store) -> None:
        _, chunk_id = _seed_patent(store)
        h = _finding_handler(store)
        resp = h.put(
            title="a claim read in a patent",
            body="claim text from the patent chunk",
            cited_in=f"pk{chunk_id}",
        )
        assert "created finding id=" in resp.body

    def test_pc_handle_still_resolves(self, store) -> None:
        _ref_id, chunk_id = _seed_paper_chunk(store)
        h = _finding_handler(store)
        tgt = h._resolve_cited_in(f"pc{chunk_id}")
        assert tgt.kind == "paper"
        assert tgt.pos == 0


# ── gr538466: meta echo on ack + get ────────────────────────────────


class TestMetaProvenance:
    def test_put_ack_echoes_meta_keys(self, store) -> None:
        _seed_paper_chunk(store)
        h = _finding_handler(store)
        resp = h.put(
            title="an annotated claim",
            body="claim text",
            cited_in="miller23a",
            meta={"model": "test-model", "run_id": "run-1", "why": "check"},
        )
        assert "meta: stored" in resp.body
        assert "model='test-model'" in resp.body
        assert "run_id='run-1'" in resp.body
        assert "view='raw'" in resp.body

    def test_get_shows_writer_annotation(self, store) -> None:
        _seed_paper_chunk(store)
        h = _finding_handler(store)
        resp = h.put(
            title="an annotated claim",
            body="claim text",
            cited_in="miller23a",
            meta={"model": "test-model", "prompt_version": "v2", "run_id": "r1"},
        )
        fid = int(resp.body.split("id=")[1].split()[0])
        got = h.get(id=fid)
        assert "written by:" in got.body
        assert "model=test-model" in got.body
        assert "prompt_version=v2" in got.body

    def test_get_without_annotation_omits_line(self, store) -> None:
        _seed_paper_chunk(store)
        h = _finding_handler(store)
        resp = h.put(title="plain claim", body="claim text", cited_in="miller23a")
        fid = int(resp.body.split("id=")[1].split()[0])
        assert "written by:" not in h.get(id=fid).body


# ── gr538071: q='*' is match-all ────────────────────────────────────


class TestStarQueryMatchAll:
    def test_index_view_star_lists_tagged_memories(self, store) -> None:
        mid = _seed_memory(store)
        store.add_tag(
            mid, Tag.parse_strict("SPACE:repo-dev", kind="memory"), set_by="agent"
        )
        h = _memory_handler(store)
        resp = h.search(q="*", tags=["SPACE:repo-dev"], view="index")
        assert f"me{mid}" in resp.body

    def test_count_view_star_counts_tagged_memories(self, store) -> None:
        mid = _seed_memory(store)
        store.add_tag(
            mid, Tag.parse_strict("SPACE:repo-dev", kind="memory"), set_by="agent"
        )
        _seed_memory(store, text="an untagged scratch note")
        h = _memory_handler(store)
        resp = h.search(q="*", tags=["SPACE:repo-dev"], view="count")
        assert resp.body.startswith("# 1 memory entry")

    def test_missing_q_with_tags_is_match_all(self, store) -> None:
        mid = _seed_memory(store)
        store.add_tag(
            mid, Tag.parse_strict("SPACE:repo-dev", kind="memory"), set_by="agent"
        )
        h = _memory_handler(store)
        resp = h.search(tags=["SPACE:repo-dev"], view="index")
        assert f"me{mid}" in resp.body

    def test_normal_q_still_ranks(self, store) -> None:
        hit = _seed_memory(store, text="quixotic zebra migration notes")
        miss = _seed_memory(store, text="unrelated database vacuum notes")
        h = _memory_handler(store)
        resp = h.search(q="quixotic zebra")
        assert f"## memory {hit}:" in resp.body
        assert f"## memory {miss}:" not in resp.body

    def test_paper_star_counts_tagged_papers(self, store) -> None:
        ref_id, _ = _seed_paper_chunk(store, cite_key="zebra24a")
        store.add_tag(
            ref_id,
            Tag.parse_strict("studytype:review", kind="paper"),
            set_by="agent",
        )
        _seed_paper_chunk(store, cite_key="other24b")
        h = PaperHandler(hub=Hub(store=store))
        resp = h.search(q="*", tags=["studytype:review"], view="count")
        assert resp.body.startswith("# 1 paper entry")

    def test_paper_star_lists_tagged_papers(self, store) -> None:
        ref_id, _ = _seed_paper_chunk(store, cite_key="zebra24a")
        store.add_tag(
            ref_id,
            Tag.parse_strict("studytype:review", kind="paper"),
            set_by="agent",
        )
        _seed_paper_chunk(store, cite_key="other24b")
        h = PaperHandler(hub=Hub(store=store))
        resp = h.search(q="*", tags=["studytype:review"])
        assert "zebra24a" in resp.body
        assert "other24b" not in resp.body


# ── gr538076: weak-match hint names the kind's accepted view ─────────


class TestWeakMatchHintView:
    def test_memory_names_index_not_ids(self, store) -> None:
        h = _memory_handler(store)
        assert h.search_ids_view == "index"
        with pytest.raises(Exception, match="index"):
            h.search(q="x", view="ids")

    def test_base_default_is_ids(self) -> None:
        from precis.handlers._numeric_ref import NumericRefHandler

        assert NumericRefHandler.search_ids_view == "ids"
