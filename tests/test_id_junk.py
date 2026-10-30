"""Pasted list-row tails on id kwargs.

Sized list/outline rows (``pa<N>~4..11\\t8kB``, ``gr<N> · 8kB``,
``me<N> · 3kB``, ``pc<N> (5k)``) get copied back wholesale as
``id=``/``target=``. The dispatch boundary trims the size/annotation
tail when the head still names a valid address — a universal handle,
a ``kind:id`` prefix, ref-id digits, or a resolvable slug — and notes
the cleaned spelling on the result. Free-text ids (URLs, paths,
queries, expressions) are never touched.
"""

from __future__ import annotations

import re

from precis.runtime import PrecisRuntime
from precis.store.store import Store
from precis.store.types import ChunkInsert


def _ref_id(runtime: PrecisRuntime, kind: str, text: str = "x") -> int:
    """Create a numeric ref via the real dispatch path; return its id."""
    out = runtime.dispatch("put", {"kind": kind, "text": text})
    code = {"memory": "me", "gripe": "gr"}[kind]
    m = re.search(rf"\b(?:id=|{code})(\d+)\b", out)
    assert m is not None, out
    return int(m.group(1))


def _paper_with_blocks(
    store: Store, slug: str, block_texts: list[str]
) -> tuple[int, list[int]]:
    ref = store.insert_ref(kind="paper", slug=slug, title="T paper")
    rows = store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=i, text=t) for i, t in enumerate(block_texts)]
    )
    return ref.id, [r.id for r in rows]


def test_chunk_handle_with_paren_size(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _, chunk_ids = _paper_with_blocks(
        store, "junkp1", [f"block {i} text" for i in range(12)]
    )
    cid = chunk_ids[4]
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"pc{cid} (5k)"}
    )
    assert is_error is False, body
    assert "block 4 text" in body
    assert f"id 'pc{cid} (5k)' read as 'pc{cid}'" in body


def test_record_handle_with_dot_size(runtime_with_store: PrecisRuntime) -> None:
    gid = _ref_id(runtime_with_store, "gripe", "junk dot row\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"gr{gid} · 3kB"}
    )
    assert is_error is False, body
    assert "junk dot row" in body
    assert f"id 'gr{gid} · 3kB' read as 'gr{gid}'" in body


def test_handle_selector_with_tsv_tail(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id, _ = _paper_with_blocks(
        store, "junkp2", [f"block {i} text" for i in range(12)]
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"pa{ref_id}~4..11\t8kB"}
    )
    assert is_error is False, body
    assert "block 11 text" in body
    assert f"id 'pa{ref_id}~4..11\\t8kB' read as 'pa{ref_id}~4..11'" in body


def test_handle_with_title_tail(runtime_with_store: PrecisRuntime) -> None:
    mid = _ref_id(runtime_with_store, "memory", "junk title row\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"me{mid} — some title"}
    )
    assert is_error is False, body
    assert "junk title row" in body
    assert f"id 'me{mid} — some title' read as 'me{mid}'" in body


def test_kind_prefix_and_bare_digits(runtime_with_store: PrecisRuntime) -> None:
    gid = _ref_id(runtime_with_store, "gripe", "junk digits\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"gripe:{gid} (2kB)"}
    )
    assert is_error is False, body
    assert f"id 'gripe:{gid} (2kB)' read as 'gripe:{gid}'" in body

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"{gid} 8kB"}
    )
    assert is_error is False, body
    assert "junk digits" in body
    assert f"id '{gid} 8kB' read as '{gid}'" in body


def test_trailing_comma_period_quote(runtime_with_store: PrecisRuntime) -> None:
    mid = _ref_id(runtime_with_store, "memory", "junk punct\nbody")
    for junky in (f"me{mid},", f"me{mid}.", f"me{mid}'"):
        body, is_error = runtime_with_store.dispatch_with_status("get", {"id": junky})
        assert is_error is False, (junky, body)
        assert f"{junky!r} read as 'me{mid}'" in body


def test_free_text_ids_untouched(
    runtime: PrecisRuntime, runtime_with_store: PrecisRuntime
) -> None:
    """calc expressions and URL-ish ids keep their whitespace — no
    strip, so no 'read as' note ever lands on the result."""
    body, _ = runtime.dispatch_with_status("get", {"kind": "calc", "id": "2 (3)"})
    assert "read as" not in body
    body, _ = runtime_with_store.dispatch_with_status(
        "get", {"kind": "web", "id": "https://example.com/~user/a page"}
    )
    assert "read as" not in body


def test_bare_second_column_not_stripped(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``pa<N> 2`` — a pasted row's second column that isn't a size —
    leaves the whole id alone rather than silently reading ``pa<N>``."""
    ref_id, _ = _paper_with_blocks(store, "junkp3", ["block 0 text"])
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"pa{ref_id} 2"}
    )
    assert is_error is True
    assert "read as" not in body


def test_hyphen_tail_not_stripped(runtime_with_store: PrecisRuntime) -> None:
    """``42 - x`` — a hyphen tail is arithmetic/op-shaped, never junk —
    so the id passes through to the handler's own error, even on a
    kind whose bare digits are real ref ids (``or`` coded)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "oracle", "id": "42 - x"}
    )
    assert is_error is True
    assert "read as" not in body


def test_non_id_tail_not_stripped(runtime_with_store: PrecisRuntime) -> None:
    """A whitespace tail that isn't size/annotation-shaped must not
    trim — the call keeps its honest not-an-id failure, not a
    rewritten one."""
    mid = _ref_id(runtime_with_store, "memory")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": f"me{mid} extra words"}
    )
    assert is_error is True
    assert "read as" not in body


def test_batch_get_strips_per_element(runtime_with_store: PrecisRuntime) -> None:
    g1 = _ref_id(runtime_with_store, "gripe", "batch junk one\nbody")
    g2 = _ref_id(runtime_with_store, "gripe", "batch junk two\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "gripe", "id": [f"gr{g1} · 3kB", f"gr{g2}"]}
    )
    assert is_error is False, body
    assert "# 2 gripe (batch)" in body
    assert f"id 'gr{g1} · 3kB' read as 'gr{g1}'" in body


def test_link_target_stripped(runtime_with_store: PrecisRuntime) -> None:
    g1 = _ref_id(runtime_with_store, "gripe", "link src\nbody")
    g2 = _ref_id(runtime_with_store, "gripe", "link dst\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "link", {"kind": "gripe", "id": g1, "target": f"gr{g2} · 2kB"}
    )
    assert is_error is False, body
    assert f"target 'gr{g2} · 2kB' read as 'gr{g2}'" in body


def test_tag_id_stripped(runtime_with_store: PrecisRuntime) -> None:
    gid = _ref_id(runtime_with_store, "gripe", "tag junk\nbody")
    body, is_error = runtime_with_store.dispatch_with_status(
        "tag", {"kind": "gripe", "id": f"gr{gid} (5k)", "add": ["topic-x"]}
    )
    assert is_error is False, body
    assert f"id 'gr{gid} (5k)' read as 'gr{gid}'" in body
