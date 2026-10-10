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
    GRAPH_MARKER,
    HOOK_CUT_CHARS,
    SPACE_TAG,
    ImportRefused,
    ImportReport,
    _created_id,
    export_memory_nodes,
    import_memory_dir,
    parse_index,
    render_memory_index,
    strip_frontmatter,
)
from precis.cli.memory import run as memory_run
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
        if key is None:  # a native write (neither slug nor section)
            continue
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

    (mem / "commit-style.md").write_text(
        "---\nname: x\n---\nfile changed\n", encoding="utf-8"
    )
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
    rendered = render_memory_index(store, full=True)
    assert rendered.startswith("# Memory index\n")
    got = _structure(rendered)
    want = _fixture_index_lines()
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        if w.startswith("## "):
            assert g == w
            continue
        m = re.fullmatch(r"- \[(?P<title>[^\]]+)\]\([^)]+\)(?: — (?P<hook>.*))?", w)
        assert m is not None, w
        pat = rf"- {re.escape(m['title'])} \(me\d+\)"
        if m["hook"]:
            pat += rf" — {re.escape(m['hook'])}"
        assert re.fullmatch(pat, g), (g, pat)


def test_render_orders_by_meta_order_not_by_id(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    ids = _nodes(store)
    # Move the first bullet of Threads to the end of the order range.
    store.update_ref(ids["alpha-campaign"], meta_patch={"order": 99})
    lines = [
        ln for ln in _structure(render_memory_index(store, full=True)) if ln[:2] == "- "
    ]
    assert lines[:3][-1].startswith("- Alpha campaign (me")


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
    lines = _structure(render_memory_index(store, full=True))
    h1 = handle_registry.format_handle("memory", first)
    h2 = handle_registry.format_handle("memory", second)
    ids = _nodes(store)
    restart = handle_registry.format_handle("memory", ids["restart-worker"])
    rotate = handle_registry.format_handle("memory", ids["rotate-token"])
    i = lines.index("## Runbooks")
    assert lines[i + 1 : i + 5] == [
        f"- Restart the worker ({restart}) — "
        "stop, drain the queue, start; never kill mid-batch",
        f"- Rotate the token ({rotate}) — new token first, then revoke the old one",
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
    lines = _structure(render_memory_index(store, full=True))
    handle = handle_registry.format_handle("memory", ref)
    assert lines[-2:] == ["## Unfiled", f"- Loose ({handle})"]


def test_render_ignores_research_space_memories(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    MemoryHandler(hub=hub).put(text="a research note", title="Research note")
    assert "Research note" not in render_memory_index(store, full=True)


def test_render_over_budget_cuts_hooks_and_names_the_overage(
    store: Store, hub: Hub
) -> None:
    import_memory_dir(store, FIXTURE)
    full = render_memory_index(store, full=True)
    out = render_memory_index(store, budget_tok=10, full=True)
    body, _, tail = out.rstrip("\n").rpartition("\n")
    assert tail.startswith("(memory index over budget:") and "budget 10 tok" in tail
    # the long Alpha hook is cut to 60 chars, ellipsis included
    alpha = next(ln for ln in body.splitlines() if "- Alpha campaign (me" in ln)
    hook = alpha.split(" — ", 1)[1]
    assert len(hook) == HOOK_CUT_CHARS and hook.endswith("…")
    # short hooks stay whole
    assert re.search(r"- Commit style \(me\d+\) — one-line subject, no body\n", body)
    assert len(body) < len(full)
    assert re.search(r"~\d+ tok full", tail)


def test_render_within_budget_is_unchanged(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    assert render_memory_index(
        store, budget_tok=8000, full=True
    ) == render_memory_index(store, full=True)


def test_render_native_node_with_a_hook(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    ref = id_of(
        MemoryHandler(hub=hub)
        .put(text="loose", title="Loose", tags=["SPACE:repo-dev"])
        .body
    )
    store.update_ref(ref, meta_patch={"hook": "now with a hook"})
    handle = handle_registry.format_handle("memory", ref)
    assert _structure(render_memory_index(store, full=True))[-1] == (
        f"- Loose ({handle}) — now with a hook"
    )


# ── live-thread session-start render ────────────────────────────────────


def _put_thread(
    hub: Hub,
    store: Store,
    title: str,
    body: str,
    *,
    age_days: float,
    hook: str | None = None,
    tags: tuple[str, ...] = ("SPACE:repo-dev", "section:threads"),
) -> int:
    ref_id = id_of(
        MemoryHandler(hub=hub).put(text=body, title=title, tags=list(tags)).body
    )
    if hook:
        store.update_ref(ref_id, meta_patch={"hook": hook})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - (%s || ' hours')::interval "
            "WHERE ref_id = %s",
            (str(age_days * 24), ref_id),
        )
    return ref_id


def test_live_threads_cut_at_14_days_newest_first_with_older_count(
    store: Store, hub: Hub
) -> None:
    new = _put_thread(hub, store, "Fresh", "Left: ship it\nmore", age_days=1)
    mid = _put_thread(hub, store, "Mid", "Left: wait\n", age_days=13.5)
    _put_thread(hub, store, "Old", "Left: nothing\n", age_days=15)
    _put_thread(hub, store, "Older", "Left: nothing\n", age_days=40)
    _put_thread(hub, store, "Gotcha", "Left: no\n", age_days=1, tags=(SPACE_TAG,))
    _put_thread(
        hub, store, "Elsewhere", "Left: no\n", age_days=1, tags=("section:threads",)
    )
    out = render_memory_index(store)
    h_new = handle_registry.format_handle("memory", new)
    h_mid = handle_registry.format_handle("memory", mid)
    assert out.splitlines() == [
        "## Live threads (edited ≤14 days)",
        f"- {h_new} Fresh — Left: ship it",
        f"- {h_mid} Mid — Left: wait",
        "(+2 older thread nodes: search(kind='memory', "
        "tags=['SPACE:repo-dev','section:threads']))",
    ]
    wide = render_memory_index(store, days=30)
    assert "Old —" in wide and "(+1 older thread nodes" in wide


def test_live_threads_exclude_part_of_children(store: Store, hub: Hub) -> None:
    parent = _put_thread(hub, store, "Parent", "Left: a\n", age_days=1)
    child = _put_thread(hub, store, "Child", "Left: b\n", age_days=1)
    store.add_link(src_ref_id=child, dst_ref_id=parent, relation="part-of")
    out = render_memory_index(store)
    assert "Parent" in out and "Child" not in out
    assert "older" not in out


def test_live_threads_lead_falls_back_to_hook_then_first_line(
    store: Store, hub: Hub
) -> None:
    hooked = _put_thread(
        hub, store, "Hooked", "no marker here\nrest", age_days=1, hook="the hook"
    )
    bare = _put_thread(hub, store, "Bare", "\n\nfirst real line\nrest", age_days=2)
    left = _put_thread(
        hub, store, "Lefty", "Left: " + "x" * 300, age_days=3, hook="ignored hook"
    )
    lines = render_memory_index(store).splitlines()
    by = {ln.split(" ")[1]: ln for ln in lines if ln.startswith("- me")}
    assert by[handle_registry.format_handle("memory", hooked)].endswith("— the hook")
    assert by[handle_registry.format_handle("memory", bare)].endswith(
        "— first real line"
    )
    lead = by[handle_registry.format_handle("memory", left)].split(" — ", 1)[1]
    assert lead.startswith("Left: x") and len(lead) == 160 and lead.endswith("…")
    assert not any("older thread nodes" in ln for ln in lines)


def test_live_threads_budget_drops_oldest_lines_into_the_older_count(
    store: Store, hub: Hub
) -> None:
    for i in range(6):
        _put_thread(hub, store, f"T{i}", f"Left: {'y' * 100}", age_days=i + 1)
    out = render_memory_index(store, budget_tok=80)
    assert len(out.encode("utf-8")) // 4 <= 80
    assert "T0" in out and "T5" not in out
    assert re.search(r"\(\+\d+ older thread nodes", out)
    assert (
        render_memory_index(store, budget_tok=0).splitlines()[0].startswith("## Live")
    )


def test_cli_index_days_option_and_full_flag(
    store: Store,
    hub: Hub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_memory_dir(store, FIXTURE)
    _put_thread(hub, store, "Mid", "Left: a\n", age_days=20)
    assert _build_parser().parse_args(["memory", "index"]).days == 14
    _run_index_cli(store, monkeypatch, "--days", "30")
    assert "Mid — Left: a" in capsys.readouterr().out
    _run_index_cli(store, monkeypatch)
    assert "Mid — Left: a" not in capsys.readouterr().out
    _run_index_cli(store, monkeypatch, "--full")
    assert capsys.readouterr().out.startswith("# Memory index\n")


# ── --sync ──────────────────────────────────────────────────────────────

GAMMA_BULLET = (
    "- [Gamma redesign](gamma-redesign.md) — design settled, build not started\n"
)
CLOCK_BULLET = (
    "- [Clock skew](clock-skew.md) — timestamps from the second host run "
    "ahead by a few seconds\n"
)


def _fixture_copy(tmp_path: Path) -> Path:
    mem = tmp_path / "mem"
    shutil.copytree(FIXTURE, mem)
    return mem


def _edit_index(mem: Path, old: str, new: str) -> None:
    path = mem / "MEMORY.md"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _tag_values(store: Store, ref_id: int) -> set[str]:
    return {v for _ns, v in store.ref_tags_bulk([ref_id]).get(ref_id, [])}


def test_sync_converges_the_graph_on_the_changed_files(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    ids = _nodes(store)
    native = id_of(
        MemoryHandler(hub=hub)
        .put(
            text="written natively",
            title="Native only",
            tags=["SPACE:repo-dev", "section:runbooks"],
            meta={"hook": "native hook"},
        )
        .body
    )

    # hook change; Clock skew moved Gotchas -> Workflow; Gamma deleted;
    # Fresh idea added to Threads; a body change.
    _edit_index(mem, "shipped and verified; next step", "SHIPPED; next step")
    _edit_index(mem, CLOCK_BULLET, "")
    _edit_index(
        mem,
        "- [Review habit](review-habit.md)",
        CLOCK_BULLET + "- [Review habit](review-habit.md)",
    )
    _edit_index(mem, GAMMA_BULLET, "")
    _edit_index(
        mem,
        "- [Beta rollout](beta-rollout.md)",
        "- [Fresh idea](fresh-idea.md) — brand new bullet\n"
        "- [Beta rollout](beta-rollout.md)",
    )
    (mem / "fresh-idea.md").write_text(
        "---\nname: f\n---\nfresh body\n", encoding="utf-8"
    )
    (mem / "commit-style.md").write_text(
        "---\nname: x\n---\nA new commit style.\n", encoding="utf-8"
    )

    report = import_memory_dir(store, mem, sync=True)

    assert (report.topics_created, report.sections_created) == (1, 0)
    assert report.retired == 1
    # changed nodes: alpha (hook), commit-style (body), clock-skew (section +
    # order), beta-rollout (order 2 -> 3), review-habit (order 2 -> 3) and
    # lost-notes (order 3 -> 2, Clock skew left Gotchas); nothing else.
    assert report.updated == 6

    nodes = _nodes(store)
    live = {r.id: r for r in store.list_refs(kind="memory", limit=1000)}
    assert live[nodes["alpha-campaign"]].meta["hook"].startswith("SHIPPED; next")
    assert _body(store, hub, nodes["commit-style"]) == "A new commit style."
    assert "gamma-redesign" not in nodes
    assert store.get_ref(kind="memory", id=ids["gamma-redesign"]) is None
    assert _body(store, hub, nodes["fresh-idea"]) == "fresh body"
    assert live[nodes["fresh-idea"]].meta["order"] == 2
    assert live[nodes["beta-rollout"]].meta["order"] == 3

    # moved section: tag swapped, part-of re-pointed (old link gone)
    clock = nodes["clock-skew"]
    tags = _tag_values(store, clock)
    assert "section:workflow" in tags and "section:gotchas" not in tags
    assert live[clock].meta["order"] == 2
    part_of = _link_pairs(store, "part-of")
    assert (clock, nodes["workflow"]) in part_of
    assert (clock, nodes["gotchas"]) not in part_of
    assert (nodes["fresh-idea"], nodes["threads"]) in part_of

    # the native write is untouched
    ref = store.get_ref(kind="memory", id=native)
    assert ref is not None and ref.title == "Native only"
    assert ref.meta == {"hook": "native hook"}
    assert _tag_values(store, native) >= {"repo-dev", "section:runbooks"}
    assert native not in {a for a, _b in part_of}


def test_sync_second_run_is_a_no_op(store: Store, hub: Hub, tmp_path: Path) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _edit_index(mem, "one-line subject, no body", "a different hook")
    _edit_index(mem, GAMMA_BULLET, "")
    first = import_memory_dir(store, mem, sync=True)
    assert (first.updated, first.retired) == (1, 1)
    before = (len(store.list_refs(kind="memory", limit=1000)), _all_link_count(store))
    again = import_memory_dir(store, mem, sync=True)
    assert (again.updated, again.retired) == (0, 0)
    assert (again.topics_created, again.sections_created) == (0, 0)
    after = (len(store.list_refs(kind="memory", limit=1000)), _all_link_count(store))
    assert after == before


def test_sync_on_unchanged_files_changes_nothing(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    report = import_memory_dir(store, FIXTURE, sync=True)
    assert (report.updated, report.retired) == (0, 0)
    assert (report.topics_created, report.sections_created) == (0, 0)
    assert "updated: 0; retired: 0" in report.summary()


def test_sync_updates_and_retires_section_nodes(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    ids = _nodes(store)
    # case-only rename keeps the slug; dropping the last header retires the
    # section node and the bullet under it.
    _edit_index(mem, "## Gotchas", "## GOTCHAS")
    text = (mem / "MEMORY.md").read_text(encoding="utf-8")
    head, _, rest = text.partition("## Reference")
    assert rest
    (mem / "MEMORY.md").write_text(head, encoding="utf-8")

    report = import_memory_dir(store, mem, sync=True)

    live = {r.id: r for r in store.list_refs(kind="memory", limit=1000)}
    assert live[ids["gotchas"]].title == "GOTCHAS"
    assert ids["reference"] not in live
    assert ids["glossary-pointer"] not in live
    assert (report.retired, report.updated) == (2, 1)
    assert "Reference" not in render_memory_index(store, full=True)


def test_plain_import_never_overwrites_even_when_files_changed(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _edit_index(mem, "one-line subject, no body", "something else")
    _edit_index(mem, GAMMA_BULLET, "")
    report = import_memory_dir(store, mem)
    assert (report.updated, report.retired) == (0, 0)
    nodes = _nodes(store)
    live = {r.id: r for r in store.list_refs(kind="memory", limit=1000)}
    assert live[nodes["commit-style"]].meta["hook"] == "one-line subject, no body"
    assert "gamma-redesign" in nodes


def test_sync_flag_is_wired_into_the_cli() -> None:
    assert _build_parser().parse_args(["memory", "import", "d", "--sync"]).sync
    assert not _build_parser().parse_args(["memory", "import", "d"]).sync


# ── --dry-run, graph marker, retire cap ────────────────────────────────


def _snapshot(store: Store) -> tuple[list[tuple[int, str, str]], int]:
    """Every live memory (id, title, meta) plus the links count — a write shows."""
    refs = store.list_refs(kind="memory", limit=1000)
    return (
        [(r.id, r.title or "", repr(sorted((r.meta or {}).items()))) for r in refs],
        _all_link_count(store),
    )


def _truncate_index_at(mem: Path, header: str) -> None:
    path = mem / "MEMORY.md"
    head, _, rest = path.read_text(encoding="utf-8").partition(header)
    assert rest, header
    path.write_text(head, encoding="utf-8")


def _counts(report: ImportReport) -> dict[str, object]:
    out = dict(vars(report))
    out.pop("dry_run")
    return out


def test_dry_run_on_an_empty_graph_writes_nothing_and_matches_the_real_run(
    store: Store, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    before = _snapshot(store)
    dry = import_memory_dir(store, mem, dry_run=True)
    assert _snapshot(store) == before
    real = import_memory_dir(store, mem)
    assert dry.dry_run and not real.dry_run
    assert _counts(dry) == _counts(real)
    assert dry.topics_created == N_BULLETS and dry.links_ensured > 0
    assert dry.summary().startswith("DRY RUN (nothing written): ")
    assert not real.summary().startswith("DRY RUN")


def test_sync_dry_run_writes_nothing_and_matches_the_real_run(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _edit_index(mem, "shipped and verified; next step", "SHIPPED; next step")
    _edit_index(mem, CLOCK_BULLET, "")
    _edit_index(mem, GAMMA_BULLET, "")
    _edit_index(
        mem,
        "- [Beta rollout](beta-rollout.md)",
        "- [Fresh idea](fresh-idea.md) — brand new bullet\n"
        "- [Beta rollout](beta-rollout.md)",
    )
    (mem / "fresh-idea.md").write_text(
        "---\nname: f\n---\nsee [[alpha-campaign]]\n", encoding="utf-8"
    )
    (mem / "commit-style.md").write_text("new body\n", encoding="utf-8")

    before = _snapshot(store)
    dry = import_memory_dir(store, mem, sync=True, dry_run=True)
    assert _snapshot(store) == before
    assert _body(store, hub, _nodes(store)["commit-style"]) != "new body"

    real = import_memory_dir(store, mem, sync=True)
    assert _counts(dry) == _counts(real)
    assert sorted(dry.would_retire) == ["clock-skew", "gamma-redesign"]
    assert (dry.retired, dry.topics_created) == (2, 1)
    assert dry.updated > 0 and dry.links_ensured > 0
    assert "would retire: " in dry.summary()
    assert "clock-skew" in dry.summary()


def test_marker_memory_md_is_refused_with_no_writes(
    store: Store, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    (mem / "MEMORY.md").write_text(
        f"{GRAPH_MARKER}\n# Memory index\n\nSee the graph.\n", encoding="utf-8"
    )
    before = _snapshot(store)
    with pytest.raises(ImportRefused, match="memory-index: graph"):
        import_memory_dir(store, mem)
    with pytest.raises(ImportRefused, match="memory-index: graph"):
        import_memory_dir(store, mem, sync=True)
    with pytest.raises(ImportRefused, match="memory-index: graph"):
        import_memory_dir(store, mem, sync=True, dry_run=True)
    assert _snapshot(store) == before


def test_marker_refusal_is_a_nonzero_cli_exit(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(GRAPH_MARKER + "\n", encoding="utf-8")
    monkeypatch.setattr("precis.cli.memory.resolve_dsn", lambda *a, **k: "unused")
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn: store))
    monkeypatch.setattr(store, "close", lambda: None)
    args = _build_parser().parse_args(["memory", "import", str(mem), "--sync"])
    with pytest.raises(SystemExit) as exc:
        memory_run(args)
    assert "memory-index: graph" in str(exc.value)


def test_sync_over_the_retire_cap_is_refused_with_no_writes(
    store: Store, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _truncate_index_at(mem, "## Gotchas")  # 3 sections + 6 bullets gone; cap is 5
    before = _snapshot(store)
    with pytest.raises(ImportRefused) as exc:
        import_memory_dir(store, mem, sync=True)
    msg = str(exc.value)
    assert "retire 9 of 16" in msg and "cap of 5" in msg
    assert "cache-poisoning" in msg and "--dry-run" in msg
    assert _snapshot(store) == before

    # a dry run reports the plan instead of refusing
    dry = import_memory_dir(store, mem, sync=True, dry_run=True)
    assert (dry.retired, len(dry.would_retire)) == (9, 9)
    assert _snapshot(store) == before


def test_allow_retire_lets_an_over_cap_sync_through(
    store: Store, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _truncate_index_at(mem, "## Gotchas")
    before = _snapshot(store)
    with pytest.raises(ImportRefused):  # N below the planned count still refuses
        import_memory_dir(store, mem, sync=True, allow_retire=8)
    assert _snapshot(store) == before
    report = import_memory_dir(store, mem, sync=True, allow_retire=9)
    assert report.retired == 9
    assert set(_nodes(store)) == {
        "threads",
        "runbooks",
        "alpha-campaign",
        "beta-rollout",
        "gamma-redesign",
        "restart-worker",
        "rotate-token",
    }


def test_sync_retiring_exactly_the_cap_still_works(
    store: Store, tmp_path: Path
) -> None:
    mem = _fixture_copy(tmp_path)
    import_memory_dir(store, mem)
    _truncate_index_at(mem, "## Workflow")  # Workflow + 2, Reference + 1 = 5
    report = import_memory_dir(store, mem, sync=True)
    assert report.retired == 5
    assert "commit-style" not in _nodes(store)


def test_dry_run_and_allow_retire_flags_are_wired_into_the_cli() -> None:
    ns = _build_parser().parse_args(
        ["memory", "import", "d", "--sync", "--dry-run", "--allow-retire", "7"]
    )
    assert ns.dry_run and ns.allow_retire == 7
    ns = _build_parser().parse_args(["memory", "import", "d"])
    assert not ns.dry_run and ns.allow_retire is None


# ---------------------------------------------------------------------------
# precis memory index --export-dir
# ---------------------------------------------------------------------------


def _run_index_cli(store: Store, monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr("precis.cli.memory.resolve_dsn", lambda *a, **k: "unused")
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn: store))
    monkeypatch.setattr(store, "close", lambda: None)
    memory_run(_build_parser().parse_args(["memory", "index", *argv]))


def _handle(ref_id: int) -> str:
    h = handle_registry.try_format("memory", ref_id)
    assert h is not None
    return h


def test_export_dir_flag_is_wired_into_the_cli() -> None:
    ns = _build_parser().parse_args(["memory", "index", "--export-dir", "d"])
    assert ns.export_dir == "d"
    assert _build_parser().parse_args(["memory", "index"]).export_dir is None


def test_export_dir_writes_one_file_per_topic_node_by_handle(
    store: Store,
    hub: Hub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_memory_dir(store, FIXTURE)
    handler = MemoryHandler(hub=hub)
    native_id = _created_id(
        handler.put(
            text="A native body.\n\n```\ncode\n```",
            title="Native note",
            tags=[SPACE_TAG],
        )
    )
    dest = tmp_path / "cache" / "memory-nodes"
    dest.mkdir(parents=True)
    (dest / "me1.md").write_text("stale set member", encoding="utf-8")

    _run_index_cli(store, monkeypatch, "--export-dir", str(dest))
    out = capsys.readouterr()

    assert out.out == render_memory_index(store)  # export never alters the index
    assert out.err == ""
    # topic nodes = every live node but the section nodes (meta.section, no slug)
    topics = [
        r
        for r in store.list_refs(kind="memory", tags=[SPACE_TAG], limit=1000)
        if (r.meta or {}).get("slug") or not (r.meta or {}).get("section")
    ]
    assert len(topics) == N_BULLETS + 1  # the fixture's bullets + the native node
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        [f"{_handle(r.id)}.md" for r in topics] + ["_sections.tsv"]
    )  # one per topic, no section node, the stale file gone
    # a row per node, section in column 2: imported topics carry their
    # section slug, the native node (no section tag) an empty one, and the
    # section nodes — rows but no files — `index`
    rows = [
        line.split("\t")
        for line in (dest / "_sections.tsv").read_text(encoding="utf-8").splitlines()
    ]
    assert {len(r) for r in rows} == {14}
    manifest = {r[0]: r[1] for r in rows}
    topic_ids = {r.id for r in topics}
    sections = [
        r.id
        for r in store.list_refs(kind="memory", tags=[SPACE_TAG], limit=1000)
        if r.id not in topic_ids
    ]
    assert sorted(manifest) == sorted(_handle(i) for i in [*topic_ids, *sections])
    assert [manifest.pop(_handle(i)) for i in sections] == ["index"] * len(SECTIONS)
    assert manifest.pop(_handle(native_id)) == ""
    assert all(manifest.values()), manifest
    for r in topics:
        assert (dest / f"{_handle(r.id)}.md").read_text(encoding="utf-8") == (
            f"# {r.title}\n\n{handler._body_text(r)}"
        )
    assert (
        (dest / f"{_handle(native_id)}.md")
        .read_text(encoding="utf-8")
        .startswith("# Native note\n\nA native body.")
    )
    # no temp / old sibling dirs left behind
    assert sorted(p.name for p in dest.parent.iterdir()) == ["memory-nodes"]


def test_export_manifest_records_the_fisheye_shape(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    # What a fisheye+1hop read shows, per node: body chars against the eye's
    # cap, live ring neighbours, those the per-relation cap hides, dead ends.
    from precis.utils.eye_render import _NEIGHBOR_GROUP_CAP, _VERBATIM_CAP

    handler = MemoryHandler(hub=hub)

    def note(title: str, body: str = "body") -> int:
        return _created_id(handler.put(text=body, title=title, tags=[SPACE_TAG]))

    hub_id = note("Hub", "x" * (_VERBATIM_CAP + 500))
    spokes = [note(f"Spoke {i}") for i in range(_NEIGHBOR_GROUP_CAP + 2)]
    gone = note("Gone")
    orphan = note("Orphan")
    for spoke in [*spokes, gone]:
        store.add_link(src_ref_id=hub_id, dst_ref_id=spoke, relation="related-to")
    store.retire_ref(gone)

    export_memory_nodes(store, tmp_path / "nodes")

    manifest = (tmp_path / "nodes" / "_sections.tsv").read_text(encoding="utf-8")
    rows = {r[0]: r[2:] for r in (line.split("\t") for line in manifest.splitlines())}
    today = rows[_handle(orphan)][0]
    # updated, chars, eye chars, live links, hidden by the group cap, dead,
    # hub handle, qualified-by count, accessed, parent, review, gotchas_none
    assert rows[_handle(hub_id)] == [
        today,
        str(_VERBATIM_CAP + 500),
        str(_VERBATIM_CAP),
        str(len(spokes)),
        "2",
        "1",
        "",
        "0",
        "",
        "",
        "",
        "0",
    ]
    assert rows[_handle(spokes[0])][3:] == ["1", "0", "0", "", "0", "", "", "", "0"]
    assert rows[_handle(orphan)][1:] == [
        "4",
        "4",
        "0",
        "0",
        "0",
        "",
        "0",
        "",
        "",
        "",
        "0",
    ]
    assert _handle(gone) not in rows


def test_export_failure_leaves_the_index_output_and_exit_code_alone(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_memory_dir(store, FIXTURE)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so mkdir under it fails", encoding="utf-8")
    dest = blocker / "nodes"

    _run_index_cli(store, monkeypatch, "--export-dir", str(dest))  # must not raise
    out = capsys.readouterr()

    assert out.out == render_memory_index(store)
    assert len(out.err.strip().splitlines()) == 1
    assert "node export" in out.err and str(dest) in out.err


def test_export_failure_keeps_the_previous_node_set(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_memory_dir(store, FIXTURE)
    dest = tmp_path / "nodes"
    assert export_memory_nodes(store, dest) == N_BULLETS
    before = {p.name: p.read_text(encoding="utf-8") for p in dest.iterdir()}

    def boom(self: MemoryHandler, ref: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(MemoryHandler, "_body_text", boom)
    with pytest.raises(RuntimeError):
        export_memory_nodes(store, dest)

    assert {p.name: p.read_text(encoding="utf-8") for p in dest.iterdir()} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nodes"]


# ---------------------------------------------------------------------------
# precis memory index --q … --k N (memory-recall-walk-keep slice 2)
# ---------------------------------------------------------------------------


def test_q_flag_is_wired_into_the_cli() -> None:
    ns = _build_parser().parse_args(["memory", "index", "--q", "x", "--k", "3"])
    assert (ns.q, ns.k) == ("x", 3)
    ns = _build_parser().parse_args(["memory", "index"])
    assert (ns.q, ns.k) == (None, 5)


def test_q_prints_the_handlers_index_view_lines(store: Store, hub: Hub) -> None:
    import_memory_dir(store, FIXTURE)
    want = (
        MemoryHandler(hub=hub)
        .search(q="token", tags=[SPACE_TAG], page_size=5, view="index")
        .body
    )
    assert want.startswith("- ")
    assert render_memory_index(store, q="token", k=5, embedder=hub.embedder) == (
        want + "\n"
    )
    assert len(want.splitlines()) <= 5


def test_without_q_the_cli_prints_the_plain_index(
    store: Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import_memory_dir(store, FIXTURE)
    before = render_memory_index(store, full=True)
    _run_index_cli(store, monkeypatch, "--full")
    assert capsys.readouterr().out == before
    assert re.search(r"\(me\d+, ", before) is None  # no filename suffix
