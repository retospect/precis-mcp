"""``/refs/quest/<id>`` — the dedicated hub dashboard (not the generic
``refs/detail.html.j2`` render every other numeric-ref kind gets).

Covers: the dashboard renders (not the generic template), every panel
degrades cleanly to an empty state with no dossier/paper/servers/log data,
and every panel renders when data is present. Runs entirely against the
fake store/runtime (``tests/precis_web/conftest.py``) — no Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis_web.routes import refs as refs_mod


def test_quest_detail_renders_dashboard_not_generic(client, runtime) -> None:
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    # Dashboard-specific sections that the generic detail template
    # (refs/detail.html.j2) has no equivalent for.
    assert "Happening now" in resp.text
    assert 'id="logbook"' in resp.text
    assert 'id="frontier"' in resp.text
    assert 'id="gaps"' in resp.text
    assert 'id="servers"' in resp.text
    assert "A NO-&gt;NH3 catalyst" in resp.text
    # Rubric line (the split-out "criteria" second line of the title).
    assert "Rubric" in resp.text
    # frontier + gaps panels are populated via the same view= reads the MCP
    # get(view=…) verb would make — not the plain no-view get() the generic
    # template dispatches.
    views = [
        a.get("view")
        for v, a in runtime.calls
        if v == "get" and a.get("kind") == "quest"
    ]
    assert "frontier" in views
    assert "gaps" in views
    assert None not in views  # never the plain logbook-card get()


def test_quest_detail_empty_states_no_500(client, runtime) -> None:
    """A quest with no dossier / paper / servers / logbook degrades to
    empty-state copy in every panel — never a 500."""
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    assert "no dossier yet" in resp.text
    assert "no reader-facing paper yet" in resp.text
    assert "no logbook entries yet" in resp.text
    assert "no activity logged yet" in resp.text
    assert "nothing serves this quest yet" in resp.text
    assert "momentum: quiet" in resp.text


def test_quest_detail_panels_render_with_data(client, runtime, monkeypatch) -> None:
    store = runtime.store

    # A dossier draft (id=501) + a separate reader-facing paper draft
    # (id=502), each resolved via the quest.dossier module's read-only
    # helpers — monkeypatched here rather than faking the raw-SQL
    # `links` lookup those helpers use directly (bypassing `Store`).
    monkeypatch.setattr("precis.quest.dossier.dossier_ref_id", lambda s, qid: 501)
    monkeypatch.setattr(
        "precis.quest.dossier.read_narrative",
        lambda s, qid: "MOF linkers look like the best lead so far.",
    )
    monkeypatch.setattr(
        "precis.quest.dossier.read_ledger",
        lambda s, qid: "## Ruled out\n- zeolite Y\n",
    )
    monkeypatch.setattr("precis.quest.dossier.paper_ref_id", lambda s, qid: 502)

    draft_refs = {
        501: SimpleNamespace(id=501, slug="quest-97-dossier"),
        502: SimpleNamespace(id=502, slug="quest-97-paper"),
    }
    original_fetch = store.fetch_refs_by_ids

    def fetch(ids: Any, **kw: Any) -> dict[int, Any]:
        out = dict(original_fetch(ids, **kw))
        for i in ids:
            if i in draft_refs:
                out[i] = draft_refs[i]
        return out

    monkeypatch.setattr(store, "fetch_refs_by_ids", fetch)

    # Servers-lite: two todos + one paper serve the quest.
    servers = [
        SimpleNamespace(id=1, kind="todo", deleted_at=None, title="Build the rig"),
        SimpleNamespace(id=2, kind="todo", deleted_at=None, title="Screen candidates"),
        SimpleNamespace(id=10, kind="paper", deleted_at=None, title="A paper"),
    ]
    monkeypatch.setattr("precis.quest.gaps._live_servers", lambda s, qid: list(servers))

    # A logbook: one human note, one agent-dispatched observation, one
    # system-measured result (feeds the tote via `cost`). Timestamps are
    # relative to "now" (not a fixed date) so the momentum window read
    # (trailing 14 days) is deterministic regardless of when the test runs.
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    store._conv_blocks[97] = [
        SimpleNamespace(
            pos=0,
            text="Started thinking about this.",
            chunk_kind="quest_log",
            meta={"entry_type": "note", "by": "human"},
            created_at=now - timedelta(days=3),
        ),
        SimpleNamespace(
            pos=1,
            text="Proposed candidate Fe-N4.",
            chunk_kind="quest_log",
            meta={"entry_type": "observation", "by": "agent"},
            created_at=now - timedelta(days=2),
        ),
        SimpleNamespace(
            pos=2,
            text="Relax converged at -4.2 eV.",
            chunk_kind="quest_log",
            meta={"entry_type": "result", "by": "system", "cost": 0.42},
            created_at=now - timedelta(days=1),
        ),
    ]

    def fake_dispatch(verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        runtime.calls.append((verb, dict(args)))
        if verb == "get" and args.get("kind") == "quest":
            if args.get("view") == "frontier":
                return ("★ candidate Fe-N4 — energy=-4.2", False)
            if args.get("view") == "gaps":
                return ("▫ low-mastery: served concept at mastery 0.20", False)
        return (f"[{verb}] ok", False)

    monkeypatch.setattr(runtime, "dispatch_with_status", fake_dispatch)

    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200

    # Dossier panel.
    assert "MOF linkers look like the best lead so far." in resp.text
    assert "zeolite Y" in resp.text
    assert "/smartdraft/quest-97-dossier" in resp.text

    # Paper hub links.
    assert "/smartdraft/quest-97-paper" in resp.text
    assert "/drafts/quest-97-paper/export.docx" in resp.text
    assert "/drafts/quest-97-paper/pdf" in resp.text

    # Happening now — agent/system entries only (the human note is
    # excluded once dispatched/measured facts exist).
    assert "Proposed candidate Fe-N4." in resp.text
    assert "Relax converged at -4.2 eV." in resp.text

    # Logbook tail includes all three, including the human note.
    assert "Started thinking about this." in resp.text
    assert "cost=0.42" in resp.text

    # Momentum flips off "quiet" once there's live logbook + server data.
    assert "momentum: quiet" not in resp.text

    # Servers-lite counts, grouped + linked. The papers count is
    # Drive-scoped to this quest's serving papers (tag=quest:<id>), not
    # the generic /refs/paper browse — every other kind keeps its
    # generic tab.
    assert "2 todos" in resp.text
    assert "1 paper" in resp.text
    assert 'href="/refs/todo"' in resp.text
    assert "/drive?submitted=1&amp;k=paper&amp;tag=quest%3A97" in resp.text
    assert 'href="/refs/paper"' not in resp.text

    # Frontier + gaps render the dispatched view text.
    assert "candidate Fe-N4 — energy=-4.2" in resp.text
    assert "low-mastery: served concept at mastery 0.20" in resp.text


def test_quest_detail_tag_chips_closed_vs_open(client, runtime, monkeypatch) -> None:
    """A closed tag renders as its real ``PREFIX:value`` and is inert; an
    open tag renders bare and gets a × removal form. Guards the namespace
    literal (``"closed"``/``"open"``, not ``"OPEN"``) the chip builder keys
    off — the fake fixture's ``tags_for`` returns ``[]``, so without this
    the chip path is never exercised."""
    from precis.store.types import Tag

    monkeypatch.setattr(
        runtime.store,
        "tags_for",
        lambda rid: [Tag.closed("STATUS", "active"), Tag.open("nitrate-reduction")],
    )
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    # Closed tag chip: its true prefix:value, never the raw "closed:value".
    assert "closed:active" not in resp.text
    # Open tag: bare value + a hidden `remove` input (the × form) proving
    # `deletable` is True; the buggy `== "OPEN"` compare would suppress it.
    assert 'value="nitrate-reduction"' in resp.text


def test_quest_logbook_renders_and_paginates(client, runtime) -> None:
    """``/refs/quest/<id>/logbook`` renders every entry, newest-first, 50/
    page — the hub itself only shows the last 10 (``log_tail``)."""
    from datetime import UTC, datetime, timedelta

    store = runtime.store
    now = datetime.now(UTC)
    # 55 entries: enough to force a second page at the 50/page size.
    store._conv_blocks[97] = [
        SimpleNamespace(
            pos=i,
            text=f"entry number {i}",
            chunk_kind="quest_log",
            meta={"entry_type": "note", "by": "human"},
            created_at=now - timedelta(days=55 - i),
        )
        for i in range(55)
    ]

    resp = client.get("/refs/quest/97/logbook")
    assert resp.status_code == 200
    assert "Logbook" in resp.text
    # Newest-first: entry 54 (most recent) is on page 1, entry 0 is not.
    assert "entry number 54" in resp.text
    assert "entry number 0" not in resp.text
    assert "Next" in resp.text
    assert "back to quest" in resp.text

    resp2 = client.get("/refs/quest/97/logbook?page=2")
    assert resp2.status_code == 200
    assert "entry number 0" in resp2.text
    assert "entry number 54" not in resp2.text
    assert "Prev" in resp2.text


def test_quest_logbook_not_found_for_non_quest_id(client, runtime) -> None:
    """A ``NotFound`` maps to a 400 error page (``PrecisError`` convention —
    ``precis_web/errors.py``), same as the generic ``/{kind}/{ref_id}``
    detail route's own not-found guard."""
    resp = client.get("/refs/quest/1/logbook")  # id=1 is a todo, not a quest
    assert resp.status_code == 400


def test_quest_hub_links_to_full_logbook(client, runtime) -> None:
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    assert "/refs/quest/97/logbook" in resp.text


def test_quest_hub_frontier_scatter_renders_points(
    client, runtime, monkeypatch
) -> None:
    """Two-or-more measured candidates ⇒ an inline SVG scatter with one
    ``<circle>`` per plottable candidate, hover ``<title>``, and a clickable
    ``open_url`` per point. `quest_frontier` is monkeypatched directly (the
    fake store has no `structure_runs`) — mirrors how `_live_servers` is
    stubbed in the panels-render test above."""
    from precis.quest.frontier import Candidate, FrontierResult

    frontier = FrontierResult(
        objectives=[("barrier", "min")],
        frontier=[
            Candidate(1, "st1", "Fe-N4", {"barrier": 0.3, "energy": -20.0}, True),
            Candidate(2, "st2", "Cu-N4", {"barrier": 0.9, "energy": -10.0}, True),
        ],
        dominated=[
            Candidate(3, "st3", "Ni-N4", {"barrier": 1.2, "energy": -5.0}, True),
        ],
        unevaluated=[Candidate(4, "st4", "Pd-N4", {}, False)],
    )
    monkeypatch.setattr(
        "precis.quest.frontier.quest_frontier", lambda store, qid: frontier
    )

    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    # 3 plottable (frontier + dominated); the unevaluated candidate has no
    # measures and is excluded.
    assert resp.text.count("<circle") == 3
    assert "/refs/structure/1" in resp.text
    assert "Fe-N4" in resp.text
    assert "converged" in resp.text
    assert "not enough simulated candidates to plot yet." not in resp.text


def test_quest_hub_frontier_scatter_falls_back_when_underpopulated(
    client, runtime, monkeypatch
) -> None:
    """Fewer than 2 plottable candidates ⇒ no SVG, muted fallback note,
    text frontier still renders below it."""
    from precis.quest.frontier import Candidate, FrontierResult

    frontier = FrontierResult(
        objectives=[("barrier", "min")],
        frontier=[
            Candidate(1, "st1", "Fe-N4", {"barrier": 0.3, "energy": -20.0}, True)
        ],
        dominated=[],
        unevaluated=[Candidate(2, "st2", "Cu-N4", {}, False)],
    )
    monkeypatch.setattr(
        "precis.quest.frontier.quest_frontier", lambda store, qid: frontier
    )

    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    assert "<circle" not in resp.text
    assert "not enough simulated candidates to plot yet." in resp.text


def test_quest_hub_exploration_queue_links_to_drive_stubs(client, runtime) -> None:
    """The Gaps panel's "exploration queue" affordance points at Drive,
    scoped to this quest's tag *and* chunkless (papers acquired but
    not yet ingested) — not the generic gaps=text alone."""
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    assert "exploration queue" in resp.text
    assert (
        "/drive?submitted=1&amp;k=paper&amp;tag=quest%3A97&amp;paper_chunks=without"
        in resp.text
    )


def _quest(**kw: Any) -> SimpleNamespace:
    """A duck-typed quest ``Ref`` — ``deleted_at`` set explicitly since the
    shared ``make_ref`` fixture doesn't carry it and the real
    ``_live_servers`` (unlike this route's own defensive ``getattr``
    reads) does a bare ``r.deleted_at is None`` check."""
    from tests.precis_web.conftest import make_ref

    kw.setdefault("deleted_at", None)
    kw.setdefault("prio", None)
    return make_ref(kind="quest", **kw)


def test_quest_index_renders_tree(client, runtime) -> None:
    """``/refs/quest`` renders a forest: a sub-quest nests under (and after)
    the parent it serves, and an orphan quest still appears as a root."""
    store = runtime.store
    store.quests.append(_quest(id=200, title="Grand catalysis quest"))
    store.quests.append(_quest(id=201, title="NH3 selectivity sub-quest"))
    store.quests.append(_quest(id=202, title="An unrelated orphan quest"))
    # The serves edge is child→parent: src=child(201), dst=parent(200).
    store.serves_links.append(SimpleNamespace(src_ref_id=201, dst_ref_id=200))

    resp = client.get("/refs/quest")
    assert resp.status_code == 200
    assert "/refs/quest/200" in resp.text
    assert "/refs/quest/201" in resp.text
    assert "/refs/quest/202" in resp.text
    assert "Grand catalysis quest" in resp.text
    assert "NH3 selectivity sub-quest" in resp.text
    assert "An unrelated orphan quest" in resp.text
    # The sub-quest is nested — it renders after its parent in document
    # order (indented under it, not as a sibling root).
    assert resp.text.index("Grand catalysis quest") < resp.text.index(
        "NH3 selectivity sub-quest"
    )


def test_quest_detail_shows_lineage_both_directions(client, runtime) -> None:
    """The child's page shows "Serves →" with the parent's handle; the
    parent's page shows the child under "Sub-quests"."""
    store = runtime.store
    store.quests.append(_quest(id=200, title="Grand catalysis quest"))
    store.quests.append(_quest(id=201, title="NH3 selectivity sub-quest"))
    store.serves_links.append(SimpleNamespace(src_ref_id=201, dst_ref_id=200))

    child_resp = client.get("/refs/quest/201")
    assert child_resp.status_code == 200
    assert "Serves" in child_resp.text
    assert "qu200" in child_resp.text
    assert "/refs/quest/200" in child_resp.text

    parent_resp = client.get("/refs/quest/200")
    assert parent_resp.status_code == 200
    assert "Sub-quests" in parent_resp.text
    assert "qu201" in parent_resp.text
    assert "/refs/quest/201" in parent_resp.text


def test_quest_index_cycle_guard_does_not_500_or_hang(client, runtime) -> None:
    """Two quests serving each other must render, not 500 or hang — the
    ``/refs/quest`` tree's visited-set guard (gripe 161912)."""
    store = runtime.store
    store.quests.append(_quest(id=210, title="Quest A"))
    store.quests.append(_quest(id=211, title="Quest B"))
    store.serves_links.append(SimpleNamespace(src_ref_id=210, dst_ref_id=211))
    store.serves_links.append(SimpleNamespace(src_ref_id=211, dst_ref_id=210))

    resp = client.get("/refs/quest")
    assert resp.status_code == 200
    # Neither cycle member qualifies as a root (each serves the other),
    # so both ride the fallback-root promotion — the pair must stay
    # visible, not silently vanish from the tree.
    assert "/refs/quest/210" in resp.text
    assert "/refs/quest/211" in resp.text

    # The detail pages (whose Lineage panel reads the same edges, just
    # non-recursively) must also render cleanly for both sides of the loop.
    assert client.get("/refs/quest/210").status_code == 200
    assert client.get("/refs/quest/211").status_code == 200


def test_quest_draft_url_prefers_slug_falls_back_to_id() -> None:
    store = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {
            501: SimpleNamespace(id=501, slug="quest-97-dossier"),
        }
    )
    assert refs_mod._quest_draft_url(store, 501) == "/smartdraft/quest-97-dossier"

    store_no_slug = SimpleNamespace(
        fetch_refs_by_ids=lambda ids: {502: SimpleNamespace(id=502, slug=None)}
    )
    assert refs_mod._quest_draft_url(store_no_slug, 502) == "/smartdraft/502"
