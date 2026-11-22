"""``get(kind=K, id=…, extent=<rung>)`` on every kind — the one door of
``docs/backlog/fisheye-everywhere.md`` in-scope 2, through the runtime
(the dispatcher routes ``extent=`` and the ladder labels on ``view=`` to
``Handler.eye``). AC 1: every registered kind renders a neighbourhood or
raises ``Unsupported`` with a one-sentence reason; AC 2/3: the structural
relations land under their ring headings on taxon, concept, component,
todo and memory; the doc eye on a paper; the skill eye."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._eye import (
    EYE_LADDER,
    EYE_NEXT,
    eye_body,
    eye_handle,
    eye_response,
    is_eye_view,
)
from precis.store import Tag
from precis.store.types import ChunkInsert
from precis.utils import handle_registry, kind_facts
from precis.utils.eye_render import _TREE_KINDS

pytestmark = pytest.mark.db


def _get(runtime: Any, **args: Any) -> tuple[str, bool]:
    return runtime.dispatch_with_status("get", args)


def _ref(store: Any, kind: str, title: str, *, slug: str | None = None) -> Any:
    return store.insert_ref(kind=kind, slug=slug, title=title)


class TestTheDoor:
    def test_is_eye_view_names_the_ladder_and_recall(self) -> None:
        assert EYE_LADDER == (
            "kwd",
            "summary",
            "verbatim",
            "fisheye",
            "fisheye+1hop",
            "fisheye+2hop",
        )
        assert all(is_eye_view(label) for label in EYE_LADDER)
        assert is_eye_view("fisheye+1hop+recall") and is_eye_view("+recall")
        assert not is_eye_view("links") and not is_eye_view(None)

    def test_extent_and_the_view_label_render_the_same(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Wire the eye")
        memo = _ref(store, "memory", "Why the eye is one door")
        store.add_link(src_ref_id=memo.id, dst_ref_id=todo.id, relation="related-to")
        via_extent, err1 = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+1hop"
        )
        via_view, err2 = _get(
            runtime_with_store, kind="todo", id=todo.id, view="fisheye+1hop"
        )
        assert not err1 and not err2
        # gr568808: the view= ladder label is the deprecated spelling —
        # it announces its reading on the interpreted: line, then
        # renders exactly what extent= renders.
        assert via_view.startswith(
            "interpreted: 'view='fisheye+1hop'' read as 'extent='fisheye+1hop''"
        ), via_view
        via_view = via_view.split("\n", 1)[1]
        # The head's read chip legitimately differs: the first call's own
        # bump shows on the second (gr524009). Strip it before comparing.
        chip = re.compile(r"  \[created [^\]]*\]")
        assert chip.sub("", via_extent) == chip.sub("", via_view)
        assert "— linked (1 hop) —" in via_extent
        assert f"related-to: me{memo.id} — Why the eye is one door" in via_extent

    def test_extent_on_a_bare_handle_routes_by_the_handle(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Handle-routed")
        out, err = _get(runtime_with_store, id=f"td{todo.id}", extent="fisheye")
        assert not err and f"td{todo.id} [todo] Handle-routed" in out

    def test_extent_and_a_different_view_is_one_argument_too_many(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Two doors")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye", view="links"
        )
        assert err and "[error:BadInput]" in out and "one argument" in out

    def test_unknown_rung_names_the_ladder(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Bad rung")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+9hop"
        )
        assert err and "[error:BadInput]" in out
        assert "fisheye+2hop" in out  # the next= line lists the ladder

    def test_dead_ref_is_not_found(self, runtime_with_store: Any, store: Any) -> None:
        todo = _ref(store, "todo", "Gone soon")
        store.retire_ref(todo.id)
        out, err = _get(runtime_with_store, kind="todo", id=todo.id, extent="fisheye")
        assert err and ("[error:Gone]" in out or "[error:NotFound]" in out)

    def test_ring_arg_narrows_the_rings_via_the_dispatch_door(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        """``fisheye-everywhere.md`` in-scope 1: the ``ring=`` filter the
        ``/eye`` page sends rides the same door — top-level (the page's
        dispatch args) and inside ``args=`` (the agent's extras) both land."""
        focus = _ref(store, "memory", "Focus")
        note = _ref(store, "memory", "A note")
        paper = _ref(store, "paper", "A paper", slug="p1")
        for near in (note, paper):
            store.add_link(
                src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to"
            )
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=focus.id,
            extent="fisheye+1hop",
            ring={"kinds": "memory"},
        )
        assert not err
        assert f"related-to: me{note.id}" in out and "related-to: pa" not in out
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=focus.id,
            extent="fisheye+1hop",
            # the agent-facing `args={'ring': …}` form arrives as __extras__
            # (tools/core.py's get wrapper)
            __extras__={"ring": {"kinds": "paper"}},
        )
        assert not err
        assert f"related-to: pa{paper.id}" in out and "related-to: me" not in out
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=focus.id,
            extent="fisheye+1hop",
            ring={"kinds": "nosuchkind"},
        )
        assert err and "[error:BadInput]" in out and "unknown ring kind" in out


class TestMemoryBatchEye:
    def test_each_handle_keeps_its_context_and_scalar_body_in_caller_order(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        focuses = [_ref(store, "memory", title) for title in ("First", "Second")]
        for focus in focuses:
            neighbor = _ref(store, "memory", f"Neighbor of {focus.title}")
            store.add_link(
                src_ref_id=focus.id, dst_ref_id=neighbor.id, relation="related-to"
            )
            store.chunks.insert_chunks(
                focus.id, [ChunkInsert(ord=0, text=f"Body of {focus.title}.")]
            )
        ids = [f"me{focus.id}" for focus in reversed(focuses)]
        singles = []
        for ident in ids:
            body, err = _get(
                runtime_with_store, kind="memory", id=ident, extent="fisheye+1hop"
            )
            assert not err
            singles.append(body)
        batch, err = _get(
            runtime_with_store, kind="memory", id=ids, extent="fisheye+1hop"
        )
        assert not err
        chip = re.compile(r"  \[created [^\]]*\]")
        assert chip.sub("", batch).split("\n\n", 1)[1] == "\n\n".join(
            chip.sub("", body) for body in singles
        )
        assert "Neighbor of First" in batch and "Neighbor of Second" in batch

    @pytest.mark.parametrize("selector", ["extent", "view"])
    @pytest.mark.parametrize(
        "rung",
        list(EYE_LADDER) + [f"{rung}+recall" for rung in EYE_LADDER] + ["+recall"],
    )
    def test_all_rungs_accept_a_memory_list(
        self, runtime_with_store: Any, store: Any, selector: str, rung: str
    ) -> None:
        first = _ref(store, "memory", "Batch rung first")
        second = _ref(store, "memory", "Batch rung second")
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=[f"me{second.id}", f"me{first.id}"],
            **{selector: rung},
        )
        assert not err, out
        assert out.index(f"me{second.id}") < out.index(f"me{first.id}")

    def test_missing_and_deleted_ids_are_blocks_between_live_eyes(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        first = _ref(store, "memory", "Live before")
        deleted = _ref(store, "memory", "Deleted batch member")
        last = _ref(store, "memory", "Live after")
        store.retire_ref(deleted.id)
        missing = last.id + 100_000
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=[first.id, missing, deleted.id, last.id],
            extent="fisheye+1hop",
        )
        assert not err
        markers = [
            f"me{first.id} [memory] Live before",
            f"me{missing}  not found",
            f"me{deleted.id}  deleted",
            f"me{last.id} [memory] Live after",
        ]
        positions = [out.index(marker) for marker in markers]
        assert positions == sorted(positions)

    @pytest.mark.parametrize("invalid", ["empty", "over-cap", "malformed"])
    def test_list_validation_matches_plain_get_and_refuses_the_entire_call(
        self, runtime_with_store: Any, store: Any, invalid: str
    ) -> None:
        from precis.handlers._numeric_ref import BATCH_ID_CAP

        focus = _ref(store, "memory", "Must not render on invalid batch")
        ids = {
            "empty": [],
            "over-cap": [focus.id] * (BATCH_ID_CAP + 1),
            "malformed": [focus.id, "not-a-memory-id"],
        }[invalid]
        plain, plain_err = _get(runtime_with_store, kind="memory", id=ids)
        eye, eye_err = _get(
            runtime_with_store, kind="memory", id=ids, extent="fisheye+1hop"
        )
        assert plain_err and eye_err
        assert "[error:BadInput]" in eye
        assert eye == plain
        assert "Must not render on invalid batch" not in eye

    def test_large_eyes_page_whole_blocks_and_keep_query_and_ring(
        self, runtime_with_store: Any, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import precis.handlers._eye as eye_mod
        import precis.handlers.memory as memory_mod

        focuses = [_ref(store, "memory", f"Page {i}") for i in range(3)]
        ids = [focus.id for focus in focuses]
        ring = {"kinds": "memory"}
        calls: list[tuple[str, Any, Any, Any]] = []

        def render(
            store: Any, handle: str, extent: Any, *, q: Any = None, ring: Any = None
        ) -> str:
            calls.append((handle, extent, q, ring))
            return f"BEGIN {handle}\n" + "x" * 150 + f"\nEND {handle}"

        monkeypatch.setattr(eye_mod, "render_eye", render)
        monkeypatch.setattr(memory_mod, "BATCH_VIEW_PAGE_CHARS", 250)
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=ids,
            view="fisheye+1hop+recall",
            q="Notes & links",
            ring=ring,
        )
        assert not err
        assert f"BEGIN me{ids[0]}\n" in out and f"\nEND me{ids[0]}" in out
        assert f"BEGIN me{ids[1]}" not in out and f"BEGIN me{ids[2]}" not in out
        assert (
            f"2 more — next: get(kind='memory', id={ids[1:]!r}, "
            "extent='fisheye+1hop+recall', q='Notes & links', "
            "args={'ring': {'kinds': 'memory'}})"
        ) in out
        assert calls
        assert all(
            (extent, q, filt) == ("fisheye+1hop+recall", "Notes & links", ring)
            for _, extent, q, filt in calls
        )

    @pytest.mark.parametrize("is_hub", [True, False])
    def test_oversized_first_eye_keeps_whole_read_and_batch_continuation(
        self,
        runtime_with_store: Any,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
        is_hub: bool,
    ) -> None:
        import precis.handlers._eye as eye_mod
        from precis.handlers._numeric_ref import BATCH_VIEW_PAGE_CHARS
        from precis.handlers.memory import MemoryHandler

        first = _ref(store, "memory", "Oversized first eye")
        second = _ref(store, "memory", "Ordinary second eye")
        if is_hub:
            store.add_tag(first.id, Tag.parse("section:index"), set_by="agent")
        oversized = f"BEGIN me{first.id}\n" + "whole line " * 7 + "\n"
        oversized += ("member detail " * 6 + "\n") * 1000
        assert len(oversized) > BATCH_VIEW_PAGE_CHARS

        def render(
            store: Any, handle: str, extent: Any, *, q: Any = None, ring: Any = None
        ) -> str:
            if handle == f"me{first.id}":
                return oversized
            return f"BEGIN {handle}\n" + "ordinary detail\n" * 20

        monkeypatch.setattr(eye_mod, "render_eye", render)
        handler = MemoryHandler(hub=runtime_with_store.hub)
        scalar = handler.eye(
            id=first.id,
            extent="fisheye+1hop",
            q="Notes & links",
            ring={"kinds": "memory"},
        )
        assert scalar.body == oversized
        selectors = ", q='Notes & links', args={'ring': {'kinds': 'memory'}}"
        members = f"get(kind='memory', id={first.id}, view='members')"
        whole_read = (
            f"get(kind='memory', id={first.id}, extent='fisheye+1hop'{selectors})"
        )
        assert scalar.result_budget_hint == (members if is_hub else None)
        out, err = _get(
            runtime_with_store,
            kind="memory",
            id=[first.id, second.id],
            extent="fisheye+1hop",
            q="Notes & links",
            ring={"kinds": "memory"},
        )
        assert not err, out
        continuation = (
            f"\n\n1 more — next: get(kind='memory', id={[second.id]!r}, "
            f"extent='fisheye+1hop'{selectors})"
        )
        assert out.endswith(continuation)
        assert len(out) <= BATCH_VIEW_PAGE_CHARS + len(continuation)
        block = out.removesuffix(continuation).split("\n\n", 1)[1]
        lines = block.splitlines()
        assert lines[-1] == f"… cut — full read: {whole_read}"
        assert lines[:-1] == oversized.splitlines()[: len(lines) - 1]
        assert f"BEGIN me{second.id}" not in out


#: The read chip on a head line counts reads, so it differs between calls.
_CHIP = re.compile(r"  \[created [^\]]*\]")


class TestBatchEye:
    """gr659493 comment 7 (td760288): ``get(kind=K, id=[...], extent=…)`` is
    ONE combined render on every kind — requested items on one parent
    grouped under it, a neighbour reached from several requested items
    shown once naming them — never one block per id, never the ``str()``
    of the list looked up as one slug, never a refusal."""

    def _paper_chunks(self, store: Any, n: int = 4) -> tuple[Any, list[str]]:
        paper = _ref(store, "paper", "Rigidity percolation", slug="mao18")
        store.chunks.insert_chunks(
            paper.id, [ChunkInsert(ord=i, text=f"body of chunk {i}") for i in range(n)]
        )
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord",
                (paper.id,),
            ).fetchall()
        return paper, [f"pc{row[0]}" for row in rows]

    def test_finding_list_is_one_combined_render(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        a = _ref(store, "finding", "Claim alpha")
        b = _ref(store, "finding", "Claim beta")
        for focus, title in ((a, "Note on alpha"), (b, "Note on beta")):
            note = _ref(store, "memory", title)
            store.add_link(
                src_ref_id=focus.id, dst_ref_id=note.id, relation="related-to"
            )
        out, err = _get(
            runtime_with_store, kind="finding", id=[a.id, b.id], extent="fisheye+1hop"
        )
        assert not err, out
        assert out.startswith("# 2 finding (batch, extent='fisheye+1hop')\n\n")
        assert out.index(f"fi{a.id} [finding] Claim alpha") < out.index(
            f"fi{b.id} [finding] Claim beta"
        )
        # one ring for the whole batch, each row naming who reaches it
        assert out.count("— linked (1 hop) —") == 1
        assert re.search(rf"Note on alpha.*  ← fi{a.id}$", out, re.M), out
        assert re.search(rf"Note on beta.*  ← fi{b.id}$", out, re.M), out

    @pytest.mark.parametrize("with_kind", [True, False])
    def test_five_chunks_of_one_paper_render_under_one_paper_heading(
        self, runtime_with_store: Any, store: Any, with_kind: bool
    ) -> None:
        paper, chunks = self._paper_chunks(store, n=7)
        ids = [chunks[5], chunks[1], chunks[3], chunks[0], chunks[6]]
        kind = {"kind": "paper"} if with_kind else {}
        out, err = _get(runtime_with_store, id=ids, extent="fisheye", **kind)
        assert not err, out
        assert out.startswith("# 5 paper (batch, extent='fisheye')\n\n")
        assert out.count(f"pa{paper.id} [paper] Rigidity percolation") == 1
        for ident in ids:
            assert out.count(f"▸ {ident} ") == 1, ident
        # the split is the doc's own reading order, so every focus sits
        # in place among its unfocused neighbours
        focused = [out.index(f"▸ {ident} ") for ident in sorted(ids, key=chunks.index)]
        assert focused == sorted(focused)
        for unfocused in (chunks[2], chunks[4]):
            assert re.search(rf"^  · {unfocused}\b", out, re.M), unfocused
        assert out.index(f"pa{paper.id} [paper]") < min(focused)

    def test_a_neighbour_of_three_requested_items_shows_once_naming_all(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        foci = [_ref(store, "finding", f"Claim {name}") for name in "abc"]
        d = _ref(store, "memory", "Shared context d")
        for focus in foci:
            store.add_link(src_ref_id=focus.id, dst_ref_id=d.id, relation="related-to")
        ids = [f"fi{focus.id}" for focus in foci]
        out, err = _get(
            runtime_with_store, kind="finding", id=ids, extent="fisheye+1hop"
        )
        assert not err, out
        assert out.count(f"me{d.id}") == 1, out
        (row,) = [line for line in out.splitlines() if f"me{d.id}" in line]
        assert "Shared context d" in row
        assert row.endswith(f"  ← {', '.join(ids)}"), row
        for focus in foci:
            assert f"fi{focus.id} [finding] {focus.title}" in out

    def test_missing_and_deleted_ids_are_one_line_entries_in_caller_order(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        first = _ref(store, "finding", "Live before")
        deleted = _ref(store, "finding", "Deleted batch member")
        last = _ref(store, "finding", "Live after")
        store.retire_ref(deleted.id)
        missing = last.id + 100_000
        out, err = _get(
            runtime_with_store,
            kind="finding",
            id=[first.id, missing, deleted.id, last.id],
            extent="fisheye+1hop",
        )
        assert not err, out
        lines = out.splitlines()
        assert f"fi{missing}  not found" in lines
        assert f"fi{deleted.id}  deleted" in lines
        assert "Deleted batch member" not in out
        markers = [
            f"fi{first.id} [finding] Live before",
            f"fi{missing}  not found",
            f"fi{deleted.id}  deleted",
            f"fi{last.id} [finding] Live after",
        ]
        positions = [out.index(marker) for marker in markers]
        assert positions == sorted(positions)

    def test_a_missing_chunk_is_a_one_line_entry_after_its_paper_group(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        _, chunks = self._paper_chunks(store)
        missing = f"pc{int(chunks[-1][2:]) + 100_000}"
        out, err = _get(
            runtime_with_store,
            kind="paper",
            id=[chunks[0], missing, chunks[3]],
            extent="fisheye",
        )
        assert not err, out
        assert f"{missing}  not found" in out.splitlines()
        # chunks[3] joins chunks[0]'s paper group, ahead of the entry
        markers = [f"▸ {chunks[0]}", f"▸ {chunks[3]}", f"{missing}  not found"]
        positions = [out.index(marker) for marker in markers]
        assert positions == sorted(positions)

    def test_the_id_cap_refuses_a_finding_batch(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        from precis.handlers._numeric_ref import BATCH_ID_CAP

        focus = _ref(store, "finding", "Must not render over the cap")
        out, err = _get(
            runtime_with_store,
            kind="finding",
            id=[focus.id] * (BATCH_ID_CAP + 1),
            extent="fisheye+1hop",
        )
        assert err and "[error:BadInput]" in out
        assert f"takes at most {BATCH_ID_CAP} ids" in out
        assert "Must not render over the cap" not in out

    def test_a_single_id_keeps_the_single_eye(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        focus = _ref(store, "finding", "Single claim")
        note = _ref(store, "memory", "Single note")
        store.add_link(src_ref_id=focus.id, dst_ref_id=note.id, relation="related-to")
        scalar, err = _get(
            runtime_with_store, kind="finding", id=focus.id, extent="fisheye+1hop"
        )
        assert not err, scalar
        assert not scalar.startswith("# ")
        assert f"related-to: me{note.id} — Single note" in scalar
        assert "←" not in scalar
        listed, err = _get(
            runtime_with_store, kind="finding", id=[focus.id], extent="fisheye+1hop"
        )
        assert not err, listed
        assert _CHIP.sub("", listed) == _CHIP.sub("", scalar)

    def test_q_has_no_batch_meaning_and_refuses(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        a = _ref(store, "finding", "Alpha")
        b = _ref(store, "finding", "Beta")
        out, err = _get(
            runtime_with_store,
            kind="finding",
            id=[a.id, b.id],
            extent="fisheye+1hop",
            q="related-to",
        )
        assert err and "[error:BadInput]" in out and "batch eye takes no q=" in out

    def test_each_focus_keeps_its_own_rows_and_overflow_names_its_call(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        """5 papers x 10 cites each: the pooled label must not cap the whole
        batch at one focus's 8 rows — each paper keeps the 8 its own eye
        shows, and the overflow names a per-paper call that lists the rest."""
        papers = [_ref(store, "paper", f"Hub {i}", slug=f"hub{i}") for i in range(5)]
        for i, paper in enumerate(papers):
            for j in range(10):
                cited = _ref(store, "paper", f"Cited {i}-{j}", slug=f"cited{i}x{j}")
                store.add_link(
                    src_ref_id=paper.id, dst_ref_id=cited.id, relation="cites"
                )
        out, err = _get(
            runtime_with_store,
            kind="paper",
            id=[paper.id for paper in papers],
            extent="fisheye+1hop",
        )
        assert not err, out
        rows = [line for line in out.splitlines() if " — Cited " in line]
        assert len(rows) == 40, out
        for i, paper in enumerate(papers):
            mine = [row for row in rows if row.endswith(f"  ← pa{paper.id}")]
            assert len(mine) == 8, (i, out)
            assert all(f"Cited {i}-" in row for row in mine)
        (overflow,) = [line for line in out.splitlines() if "… +10 more" in line]
        assert "showing 40 of 50" in overflow
        calls = re.findall(r"get\(kind='paper', id='(pa\d+)', [^)]*\)", overflow)
        assert calls == [f"pa{paper.id}" for paper in papers], overflow
        call = f"get(kind='paper', id='pa{papers[2].id}', extent='fisheye+1hop', q='cites')"
        assert call in overflow
        expanded, err = _get(
            runtime_with_store,
            kind="paper",
            id=f"pa{papers[2].id}",
            extent="fisheye+1hop",
            q="cites",
        )
        assert not err, expanded
        assert all(f"Cited 2-{j}" in expanded for j in range(10)), expanded

    def test_long_bodies_are_excerpted_so_the_ring_survives(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        from precis.utils.eye_batch import BATCH_BODY_CAP

        foci = [_ref(store, "finding", f"Long claim {i}") for i in range(7)]
        for i, focus in enumerate(foci):
            store.chunks.insert_chunks(
                focus.id, [ChunkInsert(ord=0, text=f"claim {i} " + "x" * 3992)]
            )
            note = _ref(store, "memory", f"Context note {i}")
            store.add_link(
                src_ref_id=focus.id, dst_ref_id=note.id, relation="related-to"
            )
        out, err = _get(
            runtime_with_store,
            kind="finding",
            id=[focus.id for focus in foci],
            extent="fisheye+1hop",
        )
        assert not err, out
        assert "— linked (1 hop) —" in out, out[-500:]
        for i, focus in enumerate(foci):
            assert f"Context note {i}" in out
            assert (
                f"… cut: {BATCH_BODY_CAP} of 4000 chars — full body: "
                f"get(kind='finding', id='fi{focus.id}')"
            ) in out
        # the single eye still reads the body to its own cap
        single, err = _get(
            runtime_with_store, kind="finding", id=foci[0].id, extent="fisheye+1hop"
        )
        assert not err and "… cut:" not in single and "x" * 3000 in single

    def test_batch_second_hop_names_the_single_id_call(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        foci = [_ref(store, "finding", f"Claim {i}") for i in range(2)]
        for focus in foci:
            near = _ref(store, "memory", f"Near {focus.title}")
            far = _ref(store, "memory", f"Far {focus.title}")
            store.add_link(
                src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to"
            )
            store.add_link(src_ref_id=near.id, dst_ref_id=far.id, relation="related-to")
        out, err = _get(
            runtime_with_store,
            kind="finding",
            id=[focus.id for focus in foci],
            extent="fisheye+2hop",
        )
        assert not err, out
        (heading,) = [line for line in out.splitlines() if "— second hop" in line]
        handles = ", ".join(f"fi{focus.id}" for focus in foci)
        assert (
            f"expand one with get(id=<one of {handles}>, extent='fisheye+2hop', "
            "q='<kind>:<label>')"
        ) in heading
        assert "expand one with q=" not in out

    def test_a_todo_batch_with_tree_rings_and_no_links_prints_no_empty_ring(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todos = [_ref(store, "todo", f"Lone task {i}") for i in range(2)]
        out, err = _get(
            runtime_with_store,
            kind="todo",
            id=[todo.id for todo in todos],
            extent="fisheye+1hop",
        )
        assert not err, out
        assert out.count("— tree —") == 2, out
        assert "no structural neighbours" not in out

    @pytest.mark.parametrize("invalid", ["empty", "over-cap", "window"])
    def test_an_invalid_list_refuses_the_whole_call_with_bad_input(
        self, runtime_with_store: Any, store: Any, invalid: str
    ) -> None:
        from precis.handlers._numeric_ref import BATCH_ID_CAP

        _, chunks = self._paper_chunks(store)
        ids = {
            "empty": [],
            "over-cap": [chunks[0]] * (BATCH_ID_CAP + 1),
            "window": [chunks[0], "mao18~0..2"],
        }[invalid]
        out, err = _get(runtime_with_store, kind="paper", id=ids, extent="fisheye")
        assert err and "[error:BadInput]" in out
        assert "body of chunk 0" not in out


class TestPerKind:
    def test_taxon_eye_groups_its_hierarchy_under_taxonomy(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        genus = _ref(store, "taxon", "Faradaic efficiency")
        species = _ref(store, "taxon", "NH3 Faradaic efficiency")
        instance = _ref(store, "memory", "FE measured at -0.5 V")
        store.add_link(
            src_ref_id=species.id, dst_ref_id=genus.id, relation="specialises"
        )
        store.add_link(
            src_ref_id=instance.id, dst_ref_id=species.id, relation="instance-of"
        )
        out, err = _get(
            runtime_with_store, kind="taxon", id=species.id, extent="fisheye+1hop"
        )
        assert not err
        assert f"tn{species.id} [taxon] NH3 Faradaic efficiency" in out
        assert "Taxonomy:" in out
        assert f"  specialises: tn{genus.id} — Faradaic efficiency" in out
        assert f"  has-instance: me{instance.id} — FE measured at -0.5 V" in out
        assert "Notes & links:" not in out  # nothing fell into the bucket

    def test_concept_eye_groups_prerequisites_under_concepts(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        concept = _ref(store, "concept", "Rigidity percolation")
        prereq = _ref(store, "concept", "Maxwell counting")
        store.add_link(
            src_ref_id=concept.id, dst_ref_id=prereq.id, relation="has-prerequisite"
        )
        out, err = _get(
            runtime_with_store, kind="concept", id=concept.id, extent="fisheye+1hop"
        )
        assert not err and "Concepts:" in out
        assert f"  has-prerequisite: cn{prereq.id} — Maxwell counting" in out

    def test_component_eye_groups_parts_under_parts(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        assembly = _ref(store, "component", "Electrolyser stack", slug="stack")
        part = _ref(store, "component", "Bipolar plate", slug="plate")
        store.add_link(src_ref_id=assembly.id, dst_ref_id=part.id, relation="contains")
        out, err = _get(
            runtime_with_store, kind="component", id=assembly.id, extent="fisheye+1hop"
        )
        assert not err and "Parts:" in out
        assert f"  contains: cp{part.id} — Bipolar plate" in out
        # and from the part's side the same row reads as its inverse
        back, err = _get(
            runtime_with_store, kind="component", id=part.id, extent="fisheye+1hop"
        )
        assert not err and f"  part-of: cp{assembly.id} — Electrolyser stack" in back

    def test_memory_eye_renders_body_links_and_mirror_name(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        memo = store.insert_ref(
            kind="memory",
            slug=None,
            title="Worker busy vs starved",
            meta={"file_mirror": {"filename": "worker_busy_vs_starved.md"}},
        )
        store.chunks.insert_chunks(
            memo.id,
            [
                ChunkInsert(
                    ord=0,
                    text="A busy worker has a queue; a starved one has none.",
                    meta={"chunk_kind": "memory_body"},
                )
            ],
        )
        section = _ref(store, "memory", "Diagnosis section")
        store.add_link(src_ref_id=section.id, dst_ref_id=memo.id, relation="part-of")
        out, err = _get(
            runtime_with_store, kind="memory", id=memo.id, extent="fisheye+1hop"
        )
        assert not err
        assert (
            f"me{memo.id} (worker_busy_vs_starved.md) [memory] Worker busy vs starved"
            in out
        )
        assert "A busy worker has a queue" in out
        assert (
            "Parts:" in out and f"  contains: me{section.id} — Diagnosis section" in out
        )

    def test_paper_eye_is_the_cluster_map_and_a_chunk_eye_the_split(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        paper = _ref(store, "paper", "Rigidity percolation", slug="mao18")
        store.chunks.insert_chunks(
            paper.id, [ChunkInsert(ord=i, text=f"body of chunk {i}") for i in range(4)]
        )
        note = _ref(store, "memory", "Read this for the floppy modes")
        store.add_link(src_ref_id=note.id, dst_ref_id=paper.id, relation="related-to")
        whole, err = _get(
            runtime_with_store, kind="paper", id="mao18", extent="fisheye+1hop"
        )
        assert not err
        assert f"pa{paper.id} [paper] Rigidity percolation" in whole
        assert "clusters" in whole and "▸" not in whole  # the map, no verbatim eye
        assert f"related-to: me{note.id} — Read this for the floppy modes" in whole
        # the chunk handle — rewritten by the dispatcher to slug~ord — is the split
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 2", (paper.id,)
            ).fetchone()[0]
        split, err = _get(runtime_with_store, id=f"pc{chunk_id}", extent="fisheye")
        assert not err and f"▸ pc{chunk_id}" in split and "body of chunk 2" in split

    def test_todo_eye_shows_its_quest_and_notes(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Ship the ring")
        quest = _ref(store, "quest", "Grow the mesh")
        store.add_link(src_ref_id=todo.id, dst_ref_id=quest.id, relation="serves")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+1hop"
        )
        assert not err and "Roadmap:" in out
        assert f"  serves: qu{quest.id} — Grow the mesh" in out

    def test_skill_eye_is_the_section_map_plus_its_wikilink_ring(
        self, runtime_with_store: Any
    ) -> None:
        """gr568475: a whole-skill eye at fisheye+1hop is the SECTION MAP
        (never the verbatim body — a page is what `verbatim` is for) plus
        the wikilink ring; the body-only shape this test used to assert
        was the two-rung special case the gripe deletes."""
        out, err = _get(
            runtime_with_store,
            kind="skill",
            id="precis-fisheye-help",
            extent="fisheye+1hop",
        )
        assert not err
        assert out.startswith("sk:precis-fisheye-help [skill]")
        assert "sections (drill:" in out  # the section map, one ~N row each
        # gr572706: the ring derives from [[slug]] wikilinks, both
        # directions — precis-fisheye-help's own See-also list out and
        # the skills linking back in.
        assert "— linked (1 hop) —" in out
        assert "see-also" in out and "sk:precis-get-help" in out
        assert "see-also-by" in out
        kwd, err = _get(
            runtime_with_store, kind="skill", id="precis-fisheye-help", extent="kwd"
        )
        assert not err and kwd.startswith("· sk:precis-fisheye-help [skill]")

    def test_skill_section_eye_takes_the_ladder(self, runtime_with_store: Any) -> None:
        """gr568475/gr568808: ``id='<slug>~N'`` through ``extent=`` is the
        section eye — the id must not reach the corpus as a literal slug
        (it did: ``eye: no live skill for 'sk:precis-get-help~1'``)."""
        out, err = _get(
            runtime_with_store,
            kind="skill",
            id="precis-get-help~1",
            extent="fisheye",
        )
        assert not err, out
        assert out.startswith("sk:precis-get-help~1 [skill]")
        assert "▸ precis-get-help~1 [section]" in out
        # A section range is not a section: an eye focuses one node.
        out, err = _get(
            runtime_with_store,
            kind="skill",
            id="precis-get-help~0..2",
            extent="fisheye",
        )
        assert err and "[error:BadInput]" in out, out

    def test_whole_draft_says_the_eye_is_a_section(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        draft = _ref(store, "draft", "A draft", slug="a-draft")
        out, err = _get(
            runtime_with_store, kind="draft", id="a-draft", extent="fisheye"
        )
        assert err and "[error:Unsupported]" in out and "dc<id>" in out
        assert f"dr{draft.id}" not in out or "section" in out

    def test_codeless_and_file_backed_kinds_refuse_with_a_reason(
        self, runtime_with_store: Any
    ) -> None:
        for kind, ident in (("calc", "2+2"), ("tag", "SPACE:repo-dev")):
            out, err = _get(runtime_with_store, kind=kind, id=ident, extent="fisheye")
            assert err and "[error:Unsupported]" in out, (kind, out)
            assert "graph node" in out, (kind, out)


class TestEyeHandle:
    def test_resolves_every_address_form(self, store: Any) -> None:
        paper = _ref(store, "paper", "P", slug="p-slug")
        store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="t")])
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper.id,)
            ).fetchone()[0]
        assert eye_handle(store, kind="paper", id="p-slug") == f"pa{paper.id}"
        assert eye_handle(store, kind="paper", id=f"pa{paper.id}") == f"pa{paper.id}"
        assert eye_handle(store, kind="paper", id=str(paper.id)) == f"pa{paper.id}"
        assert (
            eye_handle(store, kind="paper", id=f"paper:{paper.id}") == f"pa{paper.id}"
        )
        assert eye_handle(store, kind="paper", id="p-slug~0") == f"pc{chunk_id}"
        assert eye_handle(store, kind="paper", id=f"pc{chunk_id}") == f"pc{chunk_id}"
        with pytest.raises(BadInput, match="one node"):
            eye_handle(store, kind="paper", id="p-slug~0..2")
        with pytest.raises(NotFound):
            eye_handle(store, kind="paper", id="p-slug~7")
        with pytest.raises(NotFound):
            eye_handle(store, kind="paper", id="nope")
        _ref(store, "draft", "D", slug="d-slug")
        with pytest.raises(Unsupported, match="section"):
            eye_handle(store, kind="draft", id="d-slug")
        for kind in ("calc", "python", "md", "tag"):
            with pytest.raises(Unsupported):
                eye_handle(store, kind=kind, id="x")


_SPECS = {s.kind: s for s in kind_facts.all_declared_specs()}


@pytest.mark.parametrize("kind", sorted(_SPECS))
def test_every_kind_renders_or_refuses_with_a_reason(
    kind: str, runtime_with_store: Any, store: Any
) -> None:
    """AC 1: ``get(kind=K, id=<a live ref>, extent='fisheye')`` is a render
    or ``Unsupported`` for every declared kind — never a bare chunk, never
    a kwargs-gate rejection of ``extent=``, never an internal error."""
    if kind == "skill":
        ident: Any = "precis-fisheye-help"
    elif handle_registry.try_format(kind, 0) is None or kind in ("python", "md", "tag"):
        ident = "anything"
    else:
        slug = None if _SPECS[kind].is_numeric else f"{kind}-node"
        try:
            if kind == "gripe":  # migration 0176: a gripe needs its STATUS tag
                with store.tx() as conn:
                    ref = store.insert_ref(
                        kind=kind, slug=None, title="a gripe", conn=conn
                    )
                    store.add_tag(
                        ref.id, Tag.closed("STATUS", "open"), set_by="agent", conn=conn
                    )
                ident = ref.id
            elif kind == "checklist":
                # A checklist definition is a registry row, not a ref; its
                # graph endpoint ref exists only with a checklist_graph_refs
                # row (td750177), so seed it the way the product mints it.
                ident = store.checklist_ensure_graph_ref(
                    store.checklist_create(name="checklist-node")["id"]
                )
            else:
                ident = _ref(store, kind, f"a {kind} node", slug=slug).id
        except Exception:
            ident = 1
    out, err = _get(runtime_with_store, kind=kind, id=ident, extent="fisheye")
    if not err:
        assert out.strip(), kind
        assert "does not accept" not in out
        return
    assert "[error:Unsupported]" in out, (kind, out)
    cause = out.splitlines()[0]
    # one sentence: a reason, not a shrug
    assert len(cause) > 40 and ":" in cause, (kind, cause)
    if kind in _TREE_KINDS:
        assert "section" in cause


def test_missing_chunk_ord_names_the_record_get(store: Any, hub: Any) -> None:
    """M3 burn-down (gr494831): a ~ord miss carries next= to the record get."""
    from precis.errors import NotFound
    from precis.handlers.memory import MemoryHandler
    from tests.conftest import id_of

    mid = id_of(MemoryHandler(hub=hub).put(text="eye next= probe").body)
    with pytest.raises(NotFound) as exc:
        eye_handle(store, kind="memory", id=f"me{mid}~99")
    assert exc.value.next is not None
    assert f"get(kind='memory', id='me{mid}')" in str(exc.value.next)


def test_eye_wraps_reraise_badinput_and_wrap_valueerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr515473: BadInput is a ValueError — the ``except ValueError`` wrap
    in ``eye_response``/``eye_body`` must re-raise a renderer BadInput with
    its next= intact, not re-wrap it under the generic ``EYE_NEXT``."""
    import precis.handlers._eye as eye_mod

    bomb = BadInput("renderer refused", next="the renderer's own way out")
    no_store = cast(Any, None)  # render_eye/eye_handle are patched out

    def _boom(*a: Any, **k: Any) -> str:
        raise bomb

    monkeypatch.setattr(eye_mod, "render_eye", _boom)
    with pytest.raises(BadInput) as exc:
        eye_body(no_store, "pa1", "fisheye")
    assert exc.value is bomb

    monkeypatch.setattr(eye_mod, "eye_handle", lambda *a, **k: "pa1")
    with pytest.raises(BadInput) as exc:
        eye_response(no_store, kind="paper", id=1, extent="fisheye")
    assert exc.value is bomb

    def _raw(*a: Any, **k: Any) -> str:
        raise ValueError("raw renderer complaint")

    monkeypatch.setattr(eye_mod, "render_eye", _raw)
    with pytest.raises(BadInput) as exc:
        eye_body(no_store, "pa1", "fisheye")
    assert exc.value.next == EYE_NEXT
