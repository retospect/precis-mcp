"""``search(kind='paper', view='status', ids=[…])`` — per-input-id rows
(gr608579).

The status table used to resolve ``ids`` as one SET: duplicate inputs
collapsed silently ("178 of 190 ids resolved" meant duplicates, not
misses) and nothing mapped an input to its row. Each asked id now gets
its own row in input order — a ``pc`` chunk handle lands on its owning
paper's row, a repeat reads ``same as <asked>``, a miss reads
``unresolved`` and is named in the header line.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk

pytestmark = pytest.mark.db


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _paper(store: Store, slug: str, *, body: int = 0) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug, year=2024)
    for i in range(body):
        seed_chunk(store, ref_id=ref.id, ord=i, text=f"body {i} of {slug}")
    return ref.id


def _pc(store: Store, ref_id: int, ord_: int, text: str) -> str:
    chunk_id = seed_chunk(store, ref_id=ref_id, ord=ord_, text=text)
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def test_status_maps_inputs_and_flags_duplicates(
    papers: PaperHandler, store: Store
) -> None:
    """gr608579: ``ids=[pc_a, pc_b (same paper), unknown]`` prints three
    rows in input order — the chunk handles map to their owning paper's
    row, the second reads ``same as pc_a``, the miss reads
    ``unresolved`` — under a header counting inputs, papers, duplicate
    inputs and unresolved ids."""
    ref = _paper(store, "map2024a", body=1)
    pc_a = _pc(store, ref, 1, "first body chunk")
    pc_b = _pc(store, ref, 2, "second body chunk")
    body = papers.search(view="status", ids=[pc_a, pc_b, "pa99999999"]).body
    lines = body.splitlines()
    assert "3 ids -> 1 papers (1 duplicate inputs, 1 unresolved)" in lines[0]
    pa = handle_registry.format_handle("paper", ref)
    rows = [ln for ln in lines if ln.startswith("  ")]
    assert rows[0].startswith(f"  {pc_a} -> {pa} ")
    assert "map2024a" in rows[0]
    assert rows[1] == f"  {pc_b}  same as {pc_a}"
    assert rows[2] == "  pa99999999  unresolved"
    # Unresolved inputs are listed by name, not just counted.
    assert "unresolved: pa99999999" in body


def test_status_rows_follow_input_order(papers: PaperHandler, store: Store) -> None:
    """Rows follow the asked order, not a sorted-by-pa order — the point
    of the mapping is that row N answers id N."""
    a = _paper(store, "ord-a2024", body=1)
    b = _paper(store, "ord-b2024", body=1)
    pa_a = handle_registry.format_handle("paper", a)
    pa_b = handle_registry.format_handle("paper", b)
    asked = ["ord-b2024", pa_a]
    body = papers.search(view="status", ids=asked).body
    assert "2 ids -> 2 papers (0 duplicate inputs, 0 unresolved)" in body
    rows = [ln for ln in body.splitlines() if ln.startswith("  ")]
    assert rows[0].startswith(f"  ord-b2024 -> {pa_b} ")
    assert rows[1].startswith(f"  {pa_a} -> {pa_a} ")


def test_status_exact_duplicate_input_is_same_as(
    papers: PaperHandler, store: Store
) -> None:
    """The same id typed twice is a duplicate input too — the repeat
    names the first occurrence."""
    ref = _paper(store, "dup2024a", body=1)
    pa = handle_registry.format_handle("paper", ref)
    body = papers.search(view="status", ids=[pa, pa]).body
    assert "2 ids -> 1 papers (1 duplicate inputs, 0 unresolved)" in body
    assert f"  {pa}  same as {pa}" in body


def _draft_citing(hub: Hub, store: Store, slug: str, *handles: str) -> str:
    """A draft ref whose one paragraph cites ``handles`` — returns its
    ``dr<id>`` handle (a status input resolving to every cited paper)."""
    proj = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
    project = int(proj.body.split("id=")[1].split()[0].rstrip(",.()"))
    draft = DraftHandler(hub=hub)
    draft.put(id=slug, title=f"Draft {slug}", project=project)
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="See " + " and ".join(f"[{h}]" for h in handles) + ".",
        at={"last": True},
    )
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return f"dr{ref.id}"


def test_status_container_input_is_one_row(
    papers: PaperHandler, store: Store, hub: Hub
) -> None:
    """gr608579 review: a ``dr…`` container id resolves to EVERY cited
    paper, yet the mapping stays one row per input — each paper's
    status a ``; ``-separated segment inside that single row."""
    a = _paper(store, "agg-a2024", body=1)
    b = _paper(store, "agg-b2024", body=1)
    pa_a = handle_registry.format_handle("paper", a)
    pa_b = handle_registry.format_handle("paper", b)
    dr = _draft_citing(hub, store, "d-status-agg", pa_a, pa_b)
    body = papers.search(view="status", ids=[dr]).body
    assert "1 ids -> 2 papers (0 duplicate inputs, 0 unresolved)" in body
    rows = [ln for ln in body.splitlines() if ln.startswith("  ")]
    assert len(rows) == 1
    assert rows[0].startswith(f"  {dr} -> {pa_a} ")
    assert f"; {pa_b} " in rows[0]


def test_status_container_partial_duplicate_marks_the_seen_segment(
    papers: PaperHandler, store: Store, hub: Hub
) -> None:
    """A container citing one already-shown paper plus one new one is
    not a duplicate input: its single row shows the new paper's status
    and flags the repeat ``same as <asked>``."""
    a = _paper(store, "part-a2024", body=1)
    b = _paper(store, "part-b2024", body=1)
    pa_a = handle_registry.format_handle("paper", a)
    pa_b = handle_registry.format_handle("paper", b)
    dr = _draft_citing(hub, store, "d-status-part", pa_a, pa_b)
    body = papers.search(view="status", ids=[pa_a, dr]).body
    assert "2 ids -> 2 papers (0 duplicate inputs, 0 unresolved)" in body
    rows = [ln for ln in body.splitlines() if ln.startswith("  ")]
    assert len(rows) == 2
    assert rows[1].startswith(f"  {dr} -> ")
    assert f"{pa_a} same as {pa_a}" in rows[1]
    assert f"{pa_b}  [part-b2024]" in rows[1]
