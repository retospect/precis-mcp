"""gr524009: ``ref_reads`` counting and the eye header's age/read chip.

One focused ``get`` — a bare read, any ``view=``, any fisheye rung, or a
chunk get inside the ref — bumps ``ref_reads`` once; the same reader
re-reading inside 60s moves ``last``/``prev`` without counting.
Neighbour rows, hub listings and search hits never count: the bump sits
at the verb entry (``DispatchMixin._bump_ref_read``), never in a Store
fetch helper. The head line carries ``[created -Nd · read N× (M human) ·
last -Xh]``; ring rows carry age only.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert
from precis.utils import handle_registry

pytestmark = pytest.mark.db


def _paper(store: Any, slug: str) -> int:
    return int(
        store.insert_ref(
            kind="paper", slug=slug, title=f"Test paper {slug}", meta={}
        ).id
    )


def _reads(store: Any, ref_id: int) -> dict[str, Any] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT n_agent, n_human, last_agent_at, last_human_at, "
            "prev_agent_at, prev_human_at, last_agent_reader, "
            "last_human_reader, first_read_at FROM ref_reads "
            "WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "n_agent": int(row[0]),
        "n_human": int(row[1]),
        "last_agent_at": row[2],
        "last_human_at": row[3],
        "prev_agent_at": row[4],
        "prev_human_at": row[5],
        "last_agent_reader": row[6],
        "last_human_reader": row[7],
        "first_read_at": row[8],
    }


def _get(runtime: Any, **args: Any) -> str:
    body, is_error = runtime.dispatch_with_status("get", args)
    assert not is_error, body
    return body


class TestReadCounting:
    def test_get_bumps_agent_read(self, runtime_with_store: Any, store: Any) -> None:
        ref_id = _paper(store, "rct01")
        _get(runtime_with_store, kind="paper", id=ref_id)
        reads = _reads(store, ref_id)
        assert reads is not None
        assert reads["n_agent"] == 1 and reads["n_human"] == 0
        assert reads["last_agent_at"] is not None
        assert reads["first_read_at"] is not None

    def test_same_reader_within_60s_moves_last_not_count(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        ref_id = _paper(store, "rct02")
        _get(runtime_with_store, kind="paper", id=ref_id)
        _get(runtime_with_store, kind="paper", id=ref_id)
        reads = _reads(store, ref_id)
        assert reads is not None
        assert reads["n_agent"] == 1
        # The debounced re-read still displaced prev/last.
        assert reads["prev_agent_at"] is not None

    def test_different_reader_counts(self, runtime_with_store: Any, store: Any) -> None:
        ref_id = _paper(store, "rct03")
        _get(runtime_with_store, kind="paper", id=ref_id)
        store.bump_ref_read_for(
            "paper", str(ref_id), actor="agent", reader="session-two"
        )
        reads = _reads(store, ref_id)
        assert reads is not None and reads["n_agent"] == 2
        assert reads["last_agent_reader"] == "session-two"

    def test_view_get_counts(self, runtime_with_store: Any, store: Any) -> None:
        memo = store.insert_ref(kind="memory", slug=None, title="rct04", meta={})
        _get(runtime_with_store, kind="memory", id=int(memo.id), view="links")
        reads = _reads(store, int(memo.id))
        assert reads is not None and reads["n_agent"] == 1

    def test_eye_rung_counts(self, runtime_with_store: Any, store: Any) -> None:
        ref_id = _paper(store, "rct05")
        _get(runtime_with_store, kind="paper", id=ref_id, extent="fisheye")
        reads = _reads(store, ref_id)
        assert reads is not None and reads["n_agent"] == 1

    def test_chunk_handle_counts_the_parent_ref(self, store: Any) -> None:
        ref_id = _paper(store, "rct06")
        chunk = store.chunks.insert_chunks(
            ref_id, [ChunkInsert(ord=0, text="body", slug="b0")]
        )[0]
        handle = handle_registry.format_handle("paper", int(chunk.id), chunk=True)
        store.bump_ref_read_for("paper", handle, actor="agent", reader="session-chunk")
        reads = _reads(store, ref_id)
        assert reads is not None and reads["n_agent"] == 1

    def test_neighbour_render_does_not_count(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        focus = _paper(store, "rct07")
        neighbour = _paper(store, "rct08")
        store.add_link(src_ref_id=focus, dst_ref_id=neighbour, relation="related-to")
        _get(
            runtime_with_store,
            kind="paper",
            id=focus,
            extent="fisheye+1hop",
        )
        assert _reads(store, focus) is not None
        assert _reads(store, neighbour) is None

    def test_human_read_via_write_context(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        """The /eye route runs dispatch inside ``human_write_context`` —
        ``trust_origin='human'`` + the ``user:<abbrev>`` actor — which is
        what makes a browser read a human read (and names the reader)."""
        ref_id = _paper(store, "rct09")
        with revision_context(actor="user:reto", trust_origin="human"):
            _get(runtime_with_store, kind="paper", id=ref_id)
        reads = _reads(store, ref_id)
        assert reads is not None
        assert reads["n_human"] == 1 and reads["n_agent"] == 0
        assert reads["last_human_reader"] == "user:reto"


class TestAgeReadChip:
    def test_head_chip_shows_created_then_reads(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        ref_id = _paper(store, "rct10")
        first = _get(runtime_with_store, kind="paper", id=ref_id, extent="fisheye")
        assert re.search(r"\[created -\d+s\]", first)
        assert "read" not in first.splitlines()[0]
        second = _get(runtime_with_store, kind="paper", id=ref_id, extent="fisheye")
        head = second.splitlines()[0]
        assert "read 1×" in head
        assert re.search(r"last -\d+s", head)

    def test_human_share_in_chip(self, runtime_with_store: Any, store: Any) -> None:
        ref_id = _paper(store, "rct11")
        with revision_context(actor="user:reto", trust_origin="human"):
            _get(runtime_with_store, kind="paper", id=ref_id)
        _get(runtime_with_store, kind="paper", id=ref_id)
        body = _get(runtime_with_store, kind="paper", id=ref_id, extent="fisheye")
        assert "read 2× (1 human)" in body.splitlines()[0]

    def test_ring_row_shows_age_only(self, runtime_with_store: Any, store: Any) -> None:
        focus = _paper(store, "rct12")
        neighbour = _paper(store, "rct13")
        store.add_link(src_ref_id=focus, dst_ref_id=neighbour, relation="related-to")
        body = _get(
            runtime_with_store,
            kind="paper",
            id=focus,
            extent="fisheye+1hop",
        )
        row = next(ln for ln in body.splitlines() if f"pa{neighbour}" in ln)
        assert re.search(r"· -\d+s", row)
        assert "read" not in row
