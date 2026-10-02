"""``precis memory import`` / ``precis memory index`` over the synthetic
``tests/fixtures/file_mirror/`` tree (docs/backlog/memory-native-authoring.md
in-scope 2 + 3, AC 2).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from precis.cli import _build_parser
from precis.cli.memory import (
    HOOK_CUT_CHARS,
    import_memory_dir,
    parse_index,
    render_memory_index,
    strip_frontmatter,
)
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.utils import handle_registry
from tests.conftest import id_of

FIXTURE = Path(__file__).parent / "fixtures" / "file_mirror"
SECTIONS = ["Threads", "Runbooks", "Gotchas", "Workflow", "Reference"]
N_BULLETS = 11
MISSING = "lost-notes"


def _nodes(store: Store) -> dict[str, int]:
    """meta.slug / meta.section -> ref id over live SPACE:repo-dev memories."""
    out: dict[str, int] = {}
    for r in store.list_refs(kind="memory", tags=["SPACE:repo-dev"], limit=1000):
        key = r.meta.get("slug") or r.meta.get("section")
        assert key is not None
        out[str(key)] = r.id
    return out


def _link_pairs(store: Store, relation: str) -> set[tuple[int, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links WHERE relation = %s",
            (relation,),
        ).fetchall()
    return {(int(a), int(b)) for a, b in rows}


def _body(store: Store, hub: Hub, ref_id: int) -> str:
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    return MemoryHandler(hub=hub)._body_text(ref)


def test_parser_wired_into_the_cli() -> None:
    args = _build_parser().parse_args(["memory", "index", "--budget-tok", "5"])
    assert (args.cmd, args.memory_cmd, args.budget_tok) == ("memory", "index", 5)
    args = _build_parser().parse_args(["memory", "import", "somewhere"])
    assert (args.memory_cmd, args.dir) == ("import", "somewhere")


def test_parse_index_sections_bullets_and_hooks() -> None:
    sections = parse_index((FIXTURE / "MEMORY.md").read_text(encoding="utf-8"))
    assert [s.title for s in sections] == SECTIONS
    assert sum(len(s.bullets) for s in sections) == N_BULLETS
    first = sections[0].bullets[0]
    assert (first.title, first.slug, first.order) == (
        "Alpha campaign",
        "alpha-campaign",
        1,
    )
    assert first.hook.startswith("shipped and verified; next step")


def test_strip_frontmatter() -> None:
    assert strip_frontmatter("---\nname: x\n---\nbody\n") == "body\n"
    assert strip_frontmatter("no frontmatter\n") == "no frontmatter\n"


def test_import_creates_one_node_per_bullet_and_per_header(
    store: Store, hub: Hub
) -> None:
    report = import_memory_dir(store, FIXTURE)
    assert (report.sections_created, report.topics_created) == (5, N_BULLETS)
    assert report.missing_files == [MISSING]
    live = store.list_refs(kind="memory", limit=1000)
    assert len(live) == len(SECTIONS) + N_BULLETS
    # every ref is SPACE:repo-dev (the default research stamp is replaced)
    tags = store.ref_tags_bulk([r.id for r in live])
    for r in live:
        assert ("SPACE", "repo-dev") in tags[r.id]
        assert ("SPACE", "research") not in tags[r.id]


def test_import_section_and_topic_meta_and_tags(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    live = {r.id: r for r in store.list_refs(kind="memory", limit=1000)}
    ids = _nodes(store)
    tags = store.ref_tags_bulk(list(live))

    runbooks = live[ids["runbooks"]]
    assert runbooks.title == "Runbooks"
    assert runbooks.meta["order"] == 2
    assert any(v == "section:index" for _ns, v in tags[runbooks.id])

    rotate = live[ids["rotate-token"]]
    assert rotate.title == "Rotate the token"
    assert rotate.meta["hook"] == "new token first, then revoke the old one"
    assert rotate.meta["order"] == 2
    assert any(v == "section:runbooks" for _ns, v in tags[rotate.id])

    assert live[ids["restart-worker"]].meta["order"] == 1


def test_import_body_is_the_file_minus_frontmatter(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    body = _body(store, hub, _nodes(store)["commit-style"])
    assert body == "One-line subject, no body."
    assert "name:" not in _body(store, hub, _nodes(store)["alpha-campaign"])


def test_import_missing_file_falls_back_to_the_bullet_text(
    store: Store, hub: Hub
) -> None:
    import_memory_dir(store, FIXTURE)
    body = _body(store, hub, _nodes(store)[MISSING])
    assert body.startswith("[Lost notes](lost-notes.md) — this file was never")


def test_import_part_of_links_every_topic_to_its_section(
    store: Store, hub: Hub
) -> None:
    import_memory_dir(store, FIXTURE)
    ids = _nodes(store)
    part_of = _link_pairs(store, "part-of")
    assert len(part_of) == N_BULLETS
    assert (ids["alpha-campaign"], ids["threads"]) in part_of
    assert (ids["glossary-pointer"], ids["reference"]) in part_of
    assert (ids[MISSING], ids["gotchas"]) in part_of


def test_import_related_to_from_md_links_and_wikilinks(store: Store, hub: Hub) -> None:
    report = import_memory_dir(store, FIXTURE)
    ids = _nodes(store)
    pairs = {
        (a, b)
        for a, b in _link_pairs(store, "related-to")
        if {a, b} <= set(ids.values())
    }
    assert pairs == {
        (ids["alpha-campaign"], ids["beta-rollout"]),  # [X](other.md)
        (ids["alpha-campaign"], ids["clock-skew"]),  # [X](other.md)
        (ids["rotate-token"], ids["restart-worker"]),  # [X](other.md)
        (ids["beta-rollout"], ids["alpha-campaign"]),  # [[other]]
    }
    assert report.links_ensured == 4
    assert report.unresolved_links == []


def test_second_run_creates_nothing(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    before = (len(store.list_refs(kind="memory", limit=1000)), _all_link_count(store))
    report = import_memory_dir(store, FIXTURE)
    after = (len(store.list_refs(kind="memory", limit=1000)), _all_link_count(store))
    assert (report.sections_created, report.topics_created) == (0, 0)
    assert (report.sections_existing, report.topics_existing) == (5, N_BULLETS)
    assert after == before


def _all_link_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM links").fetchone()
    assert row is not None
    return int(row[0])


def test_graph_edit_between_runs_is_not_overwritten(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    mem = tmp_path / "mem"
    shutil.copytree(FIXTURE, mem)
    import_memory_dir(store, mem)
    ref_id = _nodes(store)["commit-style"]
    MemoryHandler(hub=hub).edit(id=ref_id, text="edited in the graph", title="Graph")

    (mem / "commit-style.md").write_text("---\nname: x\n---\nfile changed\n", "utf-8")
    import_memory_dir(store, mem)

    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None and ref.title == "Graph"
    assert _body(store, hub, ref_id) == "edited in the graph"


def test_orphan_from_an_interrupted_run_is_adopted_not_duplicated(
    store: Store, hub: Hub
) -> None:
    # A run killed between put and the meta patch leaves a titled
    # SPACE:repo-dev node with no meta.slug; the next run patches it.
    sections = parse_index((FIXTURE / "MEMORY.md").read_text(encoding="utf-8"))
    first = sections[0].bullets[0]
    orphan = id_of(
        MemoryHandler(hub=hub)
        .put(text="half-imported", title=first.title, tags=["SPACE:repo-dev"])
        .body
    )
    import_memory_dir(store, FIXTURE)
    nodes = _nodes(store)
    assert nodes[first.slug] == orphan
    assert len(nodes) == len(SECTIONS) + N_BULLETS


def test_import_requires_memory_md(store: Store, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        import_memory_dir(store, tmp_path)


# ── index render ────────────────────────────────────────────────────────


def _fixture_index_lines() -> list[str]:
    text = (FIXTURE / "MEMORY.md").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith(("## ", "- ["))]


def _structure(rendered: str) -> list[str]:
    return [ln for ln in rendered.splitlines() if ln.startswith(("## ", "- "))]


def test_render_matches_the_fixture_index_in_order(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    rendered = render_memory_index(store)
    assert rendered.startswith("# Memory index\n")
    assert _structure(rendered) == _fixture_index_lines()


def test_render_orders_by_meta_order_not_by_id(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    ids = _nodes(store)
    # Move the first bullet of Threads to the end of the order range.
    store.update_ref(ids["alpha-campaign"], meta_patch={"order": 99})
    lines = [
        ln for ln in _structure(render_memory_index(store)) if ln.startswith("- [")
    ]
    assert lines[:3][-1].startswith("- [Alpha campaign]")


def test_render_native_node_line_and_trailing_position(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    h = MemoryHandler(hub=hub)
    first = id_of(
        h.put(
            text="native note",
            title="Native note",
            tags=["SPACE:repo-dev", "section:runbooks"],
        ).body
    )
    second = id_of(
        h.put(
            text="later note",
            title="Later note",
            tags=["SPACE:repo-dev", "section:runbooks"],
        ).body
    )
    lines = _structure(render_memory_index(store))
    h1 = handle_registry.format_handle("memory", first)
    h2 = handle_registry.format_handle("memory", second)
    i = lines.index("## Runbooks")
    assert lines[i + 1 : i + 5] == [
        "- [Restart the worker](restart-worker.md) — "
        "stop, drain the queue, start; never kill mid-batch",
        "- [Rotate the token](rotate-token.md) — "
        "new token first, then revoke the old one",
        f"- Native note ({h1})",
        f"- Later note ({h2})",
    ]


def test_render_unfiled_native_node(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    ref = id_of(
        MemoryHandler(hub=hub)
        .put(text="loose", title="Loose", tags=["SPACE:repo-dev"])
        .body
    )
    lines = _structure(render_memory_index(store))
    handle = handle_registry.format_handle("memory", ref)
    assert lines[-2:] == ["## Unfiled", f"- Loose ({handle})"]


def test_render_ignores_research_space_memories(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    MemoryHandler(hub=hub).put(text="a research note", title="Research note")
    assert "Research note" not in render_memory_index(store)


def test_render_over_budget_cuts_hooks_and_names_the_overage(
    store: Store, hub: Hub
) -> None:
    import_memory_dir(store, FIXTURE)
    full = render_memory_index(store)
    out = render_memory_index(store, budget_tok=10)
    body, _, tail = out.rstrip("\n").rpartition("\n")
    assert tail.startswith("(memory index over budget:") and "budget 10 tok" in tail
    # the long Alpha hook is cut to 60 chars, ellipsis included
    alpha = next(ln for ln in body.splitlines() if "[Alpha campaign]" in ln)
    hook = alpha.split(" — ", 1)[1]
    assert len(hook) == HOOK_CUT_CHARS and hook.endswith("…")
    # short hooks stay whole
    assert "- [Commit style](commit-style.md) — one-line subject, no body" in body
    assert len(body) < len(full)
    assert re.search(r"~\d+ tok full", tail)


def test_render_within_budget_is_unchanged(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    assert render_memory_index(store, budget_tok=8000) == render_memory_index(store)
