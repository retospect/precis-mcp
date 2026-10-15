"""Find-in-ref — ``get(kind=K, id=N, q='…')`` (gr486840).

``q`` on a concrete-id get scans one ref's chunks and returns only the
matching passages (context lines, chunk/selector handle, line numbers,
match count). Literal ``q`` is case-insensitive plain text; ``re:<pat>``
opts into a bounded regex. Kinds that can't honour ``q`` refuse rather
than silently ignoring it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.todo import TodoHandler
from precis.runtime.core import PrecisRuntime
from precis.store.store import Store
from precis.store.types import ChunkInsert
from tests.conftest import id_of


@pytest.fixture
def memory(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


@pytest.fixture
def todo(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


@pytest.fixture
def paper(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture
def md_root(tmp_path: Path) -> Path:
    root = tmp_path / "mdroot"
    root.mkdir()
    return root


@pytest.fixture
def markdown(hub: Hub, md_root: Path) -> MarkdownHandler:
    return MarkdownHandler(hub=hub, root=md_root)


_BODY = (
    "first line of prose\n"
    "second line here\n"
    "third line mentions plaza packing\n"
    "fourth line\n"
    "fifth line\n"
    "sixth line\n"
    "seventh line also has plaza packing\n"
    "eighth line\n"
    "ninth line\n"
    "tenth and last\n"
)


def test_match_with_context(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    out = memory.get(id=rid, q="plaza packing").body
    assert f"# find 'plaza packing' in me{rid}" in out
    assert "2 matches in 1 chunk" in out
    # ±3 lines of context, '>' marks the matching lines.
    assert ">    3: third line mentions plaza packing" in out
    assert "     4: fourth line" in out
    assert "     2: second line here" in out
    assert ">    7: seventh line also has plaza packing" in out
    assert "     8: eighth line" in out
    # Each passage header carries a chunk/selector handle + line numbers.
    assert re.search(rf"## me{rid}~\d+ · lines 3, 7", out)


def test_case_insensitive_literal(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    out = memory.get(id=rid, q="PLAZA PACKING").body
    assert "2 matches" in out


def test_no_match(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    out = memory.get(id=rid, q="zebra crossing").body
    assert "no match" in out
    assert "scanned" in out


def test_literal_not_regex_escaping(memory: MemoryHandler) -> None:
    """A literal q with regex metachars matches only the literal text."""
    rid = id_of(memory.put(text="use a.c here\nabc somewhere\n").body)
    out = memory.get(id=rid, q="a.c").body
    assert "1 match" in out
    assert ">    1: use a.c here" in out
    # 'abc' may appear as context, but never as a marked match line.
    assert "> " not in "\n".join(
        ln for ln in out.splitlines() if "abc" in ln and ln.startswith(">")
    )


def test_multiple_chunks_and_comment_tag(gripe: GripeHandler) -> None:
    rid = id_of(gripe.put(text="body mentions plaza packing\n").body)
    gripe.put(id=rid, text="a comment also about plaza packing")
    out = gripe.get(id=rid, q="plaza packing").body
    assert "2 matches in 2 chunks" in out
    # The comment chunk is labelled so the caller can tell body hits
    # from commentary hits, and carries its flat chunk handle.
    assert re.search(r"## gc\d+ \(gripe_comment\)", out)


def test_regex_mode(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    out = memory.get(id=rid, q="re:plaza|fifth").body
    assert "3 matches" in out
    assert ">    5: fifth line" in out


def test_regex_bad_pattern_refused(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    with pytest.raises(BadInput):
        memory.get(id=rid, q="re:(a+)+$")
    with pytest.raises(BadInput):
        memory.get(id=rid, q="re:(unclosed")


def test_q_and_view_mutual_exclusion(memory: MemoryHandler) -> None:
    rid = id_of(memory.put(text=_BODY).body)
    with pytest.raises(BadInput, match="doesn't combine"):
        memory.get(id=rid, q="plaza", view="raw")
    with pytest.raises(BadInput, match="doesn't combine"):
        memory.get(id=rid, q="plaza", view="links")


def test_q_without_id_refuses(memory: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="find-in-ref"):
        memory.get(q="plaza")


def test_q_on_list_view_refuses(todo: TodoHandler) -> None:
    with pytest.raises(BadInput, match="list view"):
        todo.get(id="/recent", q="plaza")


def test_q_on_batch_refuses(gripe: GripeHandler) -> None:
    rid = id_of(gripe.put(text="x").body)
    with pytest.raises(BadInput, match="one ref at a time"):
        gripe.get(id=[rid], q="plaza")


# ── draft ─────────────────────────────────────────────────────────────


def _make_draft(draft: DraftHandler, hub: Hub) -> tuple[int, str]:
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="P").id
    ref, _title = hub.live_store.drafts.create_draft(
        name="findme", title="T", project_ref_id=proj
    )
    hub.live_store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="prose about plaza packing\nwith a second line",
        at={"last": True},
    )
    return ref.id, f"dr{ref.id}"


def test_draft_find_whole_and_section(draft: DraftHandler, hub: Hub) -> None:
    ref_id, handle = _make_draft(draft, hub)
    out = draft.get(id=handle, q="plaza packing").body
    assert f"# find 'plaza packing' in {handle}" in out
    assert re.search(r"## dc\d+", out)
    # A dc<N> chunk id scopes the find to that section's subtree.
    dc = re.search(r"dc\d+", out)
    assert dc is not None
    scoped = draft.get(id=dc.group(0), q="plaza packing").body
    assert f"in {dc.group(0)}" in scoped
    assert "> " in scoped


# ── paper ─────────────────────────────────────────────────────────────


def test_paper_find(paper: PaperHandler, store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="wu22c", title="T")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="nothing here"),
            ChunkInsert(ord=1, text="the plaza packing result\nis significant"),
        ],
    )
    out = paper.get(id="wu22c", q="plaza packing").body
    assert "# find 'plaza packing' in pa" in out
    assert re.search(r"## pc\d+ · lines 1", out)
    # ~A..B scopes the scan to a chunk range.
    scoped = paper.get(id="wu22c~0..0", q="plaza").body
    assert "no match" in scoped
    # Bare-q get keeps its older meaning: rank papers via search.
    assert paper.find_in_ref(id=None, q="plaza").body


# ── markdown (file kind) ─────────────────────────────────────────────


def test_markdown_find_file_lines(markdown: MarkdownHandler, md_root: Path) -> None:
    (md_root / "notes.md").write_text(
        "# Notes\n\nintro\n\n## Details\n\nplaza packing data\nmore\n",
        encoding="utf-8",
    )
    out = markdown.get(id="notes", q="plaza packing").body
    assert "# find 'plaza packing' in notes" in out
    # Blocks carry meta.line_start, so passage numbers are file lines.
    assert ">    7: plaza packing data" in out
    # The passage header carries the flat block handle (mc<N>).
    assert re.search(r"## mc\d+", out)
    # A ~selector narrows the scan to one block.
    miss = markdown.get(id="notes~notes", q="plaza packing").body
    assert "no match" in miss


# ── refusal on an unsupported kind (dispatch) ────────────────────────


def test_unsupported_kind_refuses(runtime_with_store: PrecisRuntime) -> None:
    """A kind whose get never declared q refuses it — never drops it."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "cad", "id": "x", "q": "plaza"}
    )
    assert is_error is True
    assert "does not accept ['q']" in body or "not accepted" in body


def test_find_kind_via_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    store.insert_ref(kind="memory", slug=None, title="m")
    ref_id = store.list_refs(kind="memory", limit=1)[0].id
    store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=0, text="plaza packing lives here")]
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": ref_id, "q": "plaza packing"}
    )
    assert is_error is False
    assert "# find 'plaza packing' in me" in body


# ── paging through more() ────────────────────────────────────────────


def test_long_result_pages_via_more(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch
) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m")
    text = "\n".join(f"line {i} about plaza packing" for i in range(1, 200))
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "2000")
    runtime_with_store.long_lived = True
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": ref.id, "q": "plaza"}
    )
    assert is_error is False
    assert "more(cursor='" in body
    cursor = re.search(r"more\(cursor='([^']+)'", body)
    assert cursor is not None
    page2, is_error2 = runtime_with_store.fetch_more(cursor.group(1))
    assert is_error2 is False
    assert "plaza" in page2
