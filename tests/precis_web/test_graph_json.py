"""Real-PG tests for ``GET /graph/<kind>/<id>.json`` (``routes/graph.py``).

The route is a thin JSON face over ``Store.neighbourhood``; what is pinned
here is the HTTP contract — parity with the store (and so with
``links_for(direction='both')`` plus the inverse rule), the ``groups``
bucketing by ``refeye.ring_group``, query-param plumbing, JSON errors and
the round-trip count — not the store's own semantics
(``tests/test_link_crud.py::TestNeighbourhood``) — and the liaison
contract's JSON-only additions (td470556): ``link_id`` + chunk endpoints,
``dir: "cross"``/``"self"`` on non-focus edges, tombstones,
``meta.native_targets``, the per-group ``{total, returned, next}``
windows and 410 on a retired focus.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.profile import ProfileHandler
from precis.store import ChunkInsert
from precis.store.store import Store
from precis.users import hash_password
from precis.utils import handle_registry
from precis.utils.refeye import ring_group
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.graph import (
    DEFAULT_CAP,
    MAX_CAP,
    PANEL_GROUP_ROWS,
    ring_groups,
)


@pytest.fixture
def graph_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Paper {slug}", provider="manual", meta={}
    ).id


def _memory(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


@pytest.fixture
def fixture_graph(store: Store) -> dict[str, int]:
    """A paper with links both ways across four relations spanning three
    ring buckets: ``cites``/``related-to`` (Notes & links), ``serves``
    (Roadmap), ``supersedes`` + the inverse-presented ``cited-by`` (Other)."""
    focus = _paper(store, "focus2020")
    cited = _paper(store, "cited2019")
    citer = _paper(store, "citer2021")
    note = _memory(store, "a note")
    goal = _memory(store, "a goal")
    old = _memory(store, "an old note")
    store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites")
    store.add_link(src_ref_id=citer, dst_ref_id=focus, relation="cites")
    store.add_link(src_ref_id=note, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=focus, dst_ref_id=goal, relation="serves")
    store.add_link(src_ref_id=focus, dst_ref_id=old, relation="supersedes")
    return {
        "focus": focus,
        "cited": cited,
        "citer": citer,
        "note": note,
        "goal": goal,
        "old": old,
    }


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.format_handle(kind, ref_id)


def _store_view(store: Store, focus: int, **kw: Any) -> dict[str, Any]:
    """What the route returns from the store below every cap: tombstones,
    link ids and intra edges on, depth-2 edges re-read as ``cross``."""
    exp = store.neighbourhood(
        "paper",
        focus,
        cap=DEFAULT_CAP,
        include_retired=True,
        link_ids=True,
        intra_edges=True,
        **kw,
    )
    for e in exp["edges"]:
        if e.get("hop"):
            e["dir"] = "cross"
    return exp


def _core(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in ("groups", "meta")}


@contextmanager
def _query_counter(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> Iterator[dict[str, Any]]:
    counts: dict[str, Any] = {"n": 0, "sql": []}
    original = psycopg.Connection.execute
    # Only threads born after this point (the TestClient portal and its
    # worker): a background thread an earlier test left in the same xdist
    # worker shares the session store's pool and would be counted too
    # (gr477303).
    before = {t.ident for t in threading.enumerate()}

    def counting_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        # The pool's liveness probe (``check_connection`` -> ``execute("")``)
        # is not a query.
        sql = str(args[0]).strip()
        if (
            sql
            and threading.get_ident() not in before
            and getattr(self, "_pool", None) is store.pool
        ):
            counts["n"] += 1
            counts["sql"].append(sql.split("\n", 1)[0][:80])
        return original(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(psycopg.Connection, "execute", counting_execute)
        yield counts


def test_json_matches_store_and_links_for(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()

    assert _core(body) == _store_view(store, focus)
    assert [
        {k: g[k] for k in ("heading", "rels", "n")} for g in body["groups"]
    ] == ring_groups(body["counts"])
    assert body["focus"] == {
        "kind": "paper",
        "id": focus,
        "label": "Paper focus2020",
        "trust": "unknown · unknown",
        "state": "live",
    }
    assert body["truncated"] is False
    assert "counts2" not in body

    # row-for-row parity with links_for(direction='both'), inbound rows
    # presented under the inverse slug (the ``cited-by`` rewrite).
    kind_of = {
        v: ("paper" if k in ("focus", "cited", "citer") else "memory")
        for k, v in fixture_graph.items()
    }
    exp_edges: set[tuple[str, str, str, str]] = set()
    for ln in store.links_for(focus, direction="both"):
        out = ln.src_ref_id == focus
        rel = (
            ln.relation if out else (store.inverse_relation(ln.relation) or ln.relation)
        )
        exp_edges.add(
            (
                _h(kind_of[ln.src_ref_id], ln.src_ref_id),
                _h(kind_of[ln.dst_ref_id], ln.dst_ref_id),
                rel,
                "out" if out else "in",
            )
        )
    assert {
        (e["src"], e["dst"], e["rel"], e["dir"]) for e in body["edges"]
    } == exp_edges
    assert len(body["edges"]) == 5 == len(exp_edges)
    # one physical row per edge: the link ids are links_for's
    assert sorted(e["link_id"] for e in body["edges"]) == sorted(
        ln.id for ln in store.links_for(focus, direction="both")
    )
    assert {(n["kind"], n["id"]) for n in body["nodes"]} == {
        (kind_of[v], v) for k, v in fixture_graph.items() if k != "focus"
    }
    assert (
        _h("paper", fixture_graph["citer"]),
        _h("paper", focus),
        "cited-by",
        "in",
    ) in (exp_edges)


def test_groups_follow_ring_group_with_other(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    body = graph_client.get(f"/graph/paper/{fixture_graph['focus']}.json").json()
    assert ring_group("cited-by") is None and ring_group("supersedes") is None
    w = {"next": None}
    assert body["groups"] == [
        {
            "heading": "Roadmap",
            "rels": ["serves"],
            "n": 1,
            "total": 1,
            "returned": 1,
            **w,
        },
        {
            "heading": "Notes & links",
            "rels": ["cites", "related-to"],
            "n": 2,
            "total": 2,
            "returned": 2,
            **w,
        },
        {
            "heading": "Other",
            "rels": ["cited-by", "supersedes"],
            "n": 2,
            "total": 2,
            "returned": 2,
            **w,
        },
    ]
    # every hop-1 relation lands in exactly one group
    assert sorted(r for g in body["groups"] for r in g["rels"]) == sorted(
        body["counts"]
    )


def test_ring_groups_empty() -> None:
    assert ring_groups({}) == []


def test_query_params_plumb_through(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]

    by_rel = graph_client.get(f"/graph/paper/{focus}.json?rels=cited-by,serves").json()
    assert {n["id"] for n in by_rel["nodes"]} == {
        fixture_graph["citer"],
        fixture_graph["goal"],
    }
    assert [g["heading"] for g in by_rel["groups"]] == ["Roadmap", "Other"]

    by_kind = graph_client.get(f"/graph/paper/{focus}.json?kinds=paper").json()
    assert {n["kind"] for n in by_kind["nodes"]} == {"paper"}

    capped = graph_client.get(f"/graph/paper/{focus}.json?cap=2").json()
    assert len(capped["nodes"]) == 2 and capped["truncated"] is True
    assert capped["counts"] == store.neighbourhood("paper", focus)["counts"]

    # since/until parse as Drive does: ISO date, naive = UTC; garbage = no filter
    assert (
        graph_client.get(f"/graph/paper/{focus}.json?since=2999-01-01").json()["nodes"]
        == []
    )
    assert (
        len(
            graph_client.get(f"/graph/paper/{focus}.json?until=2999-01-01").json()[
                "nodes"
            ]
        )
        == 5
    )
    assert (
        len(
            graph_client.get(f"/graph/paper/{focus}.json?since=not-a-date").json()[
                "nodes"
            ]
        )
        == 5
    )

    # depth=2 adds the second hop (cited -> citer is 2 hops from focus? no:
    # seed one more leaf off ``goal`` so hop 2 is non-empty)
    leaf = _memory(store, "leaf")
    store.add_link(
        src_ref_id=fixture_graph["goal"], dst_ref_id=leaf, relation="related-to"
    )
    deep = graph_client.get(f"/graph/paper/{focus}.json?depth=2").json()
    assert deep["counts2"] == {"related-to": {"memory": 1}}
    assert [n["id"] for n in deep["nodes"] if n.get("hop") == 2] == [leaf]
    assert _core(deep) == _store_view(store, focus, depth=2)


def test_json_errors(graph_client: TestClient, fixture_graph: dict[str, int]) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get("/graph/paper/999999999.json")
    assert r.status_code == 404 and "no paper ref" in r.json()["error"]
    # right id, wrong kind: also a miss
    assert graph_client.get(f"/graph/memory/{focus}.json").status_code == 404
    r = graph_client.get(f"/graph/paper/{focus}.json?depth=3")
    assert r.status_code == 400 and "depth" in r.json()["error"]
    r = graph_client.get(f"/graph/paper/{focus}.json?cap={MAX_CAP + 1}")
    assert r.status_code == 400 and "cap" in r.json()["error"]
    assert graph_client.get(f"/graph/paper/{focus}.json?cap=0").status_code == 400
    assert graph_client.get("/graph/paper/abc.json").status_code == 422


def test_round_trips(
    graph_client: TestClient,
    store: Store,
    fixture_graph: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focus row + one hop-1 statement + one intra-edges statement (+ one
    for hop 2), nothing per node or per edge; the inverse map is warmed
    first so its one-off read does not count."""
    focus = fixture_graph["focus"]
    store.inverse_relation("cites")
    with _query_counter(monkeypatch, store) as c1:
        assert graph_client.get(f"/graph/paper/{focus}.json").status_code == 200
    with _query_counter(monkeypatch, store) as c2:
        assert graph_client.get(f"/graph/paper/{focus}.json?depth=2").status_code == 200
    assert (c1["n"], c2["n"]) == (3, 4), (c1["sql"], c2["sql"])


def test_edges_carry_link_and_chunk_ids(graph_client: TestClient, store: Store) -> None:
    """Two parallel ``cites`` rows from different chunks of one paper stay
    two edges, told apart by ``link_id`` and ``src_chunk_id``."""
    focus = _paper(store, "chunky2020")
    store.chunks.insert_chunks(
        focus, [ChunkInsert(ord=i, text=f"block {i}", meta={}) for i in range(2)]
    )
    cited = _paper(store, "target2019")
    a = store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites", src_pos=0)
    b = store.add_link(src_ref_id=focus, dst_ref_id=cited, relation="cites", src_pos=1)
    body = graph_client.get(f"/graph/paper/{focus}.json").json()
    got = {(e["link_id"], e["src_chunk_id"], e["dst_chunk_id"]) for e in body["edges"]}
    assert got == {
        (a.id, a.src_chunk_id, None),
        (b.id, b.src_chunk_id, None),
    }
    assert a.src_chunk_id is not None and a.src_chunk_id != b.src_chunk_id
    assert len(body["nodes"]) == 1


def test_depth2_edges_read_cross(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    focus, goal = fixture_graph["focus"], fixture_graph["goal"]
    leaf = _memory(store, "leaf")
    store.add_link(src_ref_id=leaf, dst_ref_id=goal, relation="related-to")
    deep = graph_client.get(f"/graph/paper/{focus}.json?depth=2").json()
    hop2 = [e for e in deep["edges"] if e.get("hop") == 2]
    # stored direction kept: leaf -> goal, though goal is the anchor
    assert [(e["src"], e["dst"], e["dir"]) for e in hop2] == [
        (_h("memory", leaf), _h("memory", goal), "cross")
    ]
    assert {e["dir"] for e in deep["edges"] if not e.get("hop")} == {"in", "out"}


def test_retired_endpoint_is_a_tombstone(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    focus, old = fixture_graph["focus"], fixture_graph["old"]
    store.retire_ref(old)
    body = graph_client.get(f"/graph/paper/{focus}.json").json()
    (node,) = [n for n in body["nodes"] if n["id"] == old]
    assert node["retired"] is True
    assert any(e["dst"] == _h("memory", old) for e in body["edges"])
    assert body["counts"]["supersedes"] == {"memory": 1}


def test_retired_focus_is_410(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    assert graph_client.get(f"/graph/paper/{focus}.json").status_code == 200
    store.retire_ref(focus)
    r = graph_client.get(f"/graph/paper/{focus}.json")
    assert r.status_code == 410
    assert "retired" in r.json()["error"]


def test_intra_edges_and_self_loops(
    graph_client: TestClient, store: Store, fixture_graph: dict[str, int]
) -> None:
    """A link between two hop-1 nodes comes back ``dir: "cross"`` in
    stored direction, and a chunk-level self-link (the only self-link the
    schema admits) ``dir: "self"``. Neither is counted in ``counts`` or
    fills a group window."""
    focus, cited, goal = (fixture_graph[k] for k in ("focus", "cited", "goal"))
    cross = store.add_link(src_ref_id=cited, dst_ref_id=goal, relation="cites")
    store.chunks.insert_chunks(
        focus, [ChunkInsert(ord=i, text=f"block {i}", meta={}) for i in range(2)]
    )
    loop = store.add_link(
        src_ref_id=focus,
        dst_ref_id=focus,
        src_pos=0,
        dst_pos=1,
        relation="see-also",
    )
    body = graph_client.get(f"/graph/paper/{focus}.json").json()
    fh = _h("paper", focus)
    ring = [e for e in body["edges"] if e["dir"] in ("self", "cross")]
    assert {(e["src"], e["dst"], e["rel"], e["dir"]) for e in ring} == {
        (_h("paper", cited), _h("memory", goal), "cites", "cross"),
        (fh, fh, "see-also", "self"),
    }
    assert {e["link_id"] for e in ring} == {cross.id, loop.id}
    # intra edges add no node, no count bucket, no group row
    assert body["counts"] == store.neighbourhood("paper", focus)["counts"]
    assert sum(g["total"] for g in body["groups"]) == sum(
        n for kinds in body["counts"].values() for n in kinds.values()
    )
    assert {n["id"] for n in body["nodes"]} == {
        fixture_graph[k] for k in ("cited", "citer", "note", "goal", "old")
    }
    assert set(body["meta"]["native_targets"]) >= {fh, _h("memory", goal)}


def test_intra_edges_rels_inverse_parity(
    graph_client: TestClient, store: Store
) -> None:
    """``rels=`` admits a ring edge whose stored slug or its inverse is
    listed — ``rels=cited-by`` keeps a stored ``cites`` between two kept
    hop-1 nodes; a list covering neither slug drops it."""
    focus = _paper(store, "hub2021")
    a = _memory(store, "a")
    b = _memory(store, "b")
    store.add_link(src_ref_id=focus, dst_ref_id=a, relation="related-to")
    store.add_link(src_ref_id=focus, dst_ref_id=b, relation="related-to")
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
    body = graph_client.get(
        f"/graph/paper/{focus}.json?rels=related-to,cited-by"
    ).json()
    ring = [e for e in body["edges"] if e["dir"] == "cross"]
    assert [(e["src"], e["dst"], e["rel"]) for e in ring] == [
        (_h("memory", a), _h("memory", b), "cites")
    ]
    gone = graph_client.get(f"/graph/paper/{focus}.json?rels=related-to").json()
    assert all(e["dir"] != "cross" for e in gone["edges"])


def test_native_targets_cover_every_endpoint(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    body = graph_client.get(f"/graph/paper/{focus}.json").json()
    targets = body["meta"]["native_targets"]
    named = {h for e in body["edges"] for h in (e["src"], e["dst"])}
    assert set(targets) == named | {_h("paper", focus)}
    goal = fixture_graph["goal"]
    assert targets[_h("memory", goal)] == {
        "kind": "memory",
        "id": goal,
        "get": {"kind": "memory", "id": _h("memory", goal)},
    }


def test_group_window_continues_past_the_cap(
    graph_client: TestClient, store: Store
) -> None:
    """26 ``related-to`` neighbours: the first window returns 25 and a
    ``next`` URL; following it returns the 26th and ends."""
    focus = _paper(store, "hub2020")
    mems = [_memory(store, f"m{i}") for i in range(PANEL_GROUP_ROWS + 1)]
    for m in mems:
        store.add_link(src_ref_id=m, dst_ref_id=focus, relation="related-to")
    first = graph_client.get(f"/graph/paper/{focus}.json?kinds=memory").json()
    (g,) = first["groups"]
    assert (g["heading"], g["total"], g["returned"]) == (
        "Notes & links",
        PANEL_GROUP_ROWS + 1,
        PANEL_GROUP_ROWS,
    )
    assert first["truncated"] is True and len(first["nodes"]) == PANEL_GROUP_ROWS
    assert g["next"] == (
        f"/graph/paper/{focus}.json?kinds=memory"
        f"&group=Notes%20%26%20links&offset={PANEL_GROUP_ROWS}"
    )
    second = graph_client.get(g["next"]).json()
    (g2,) = second["groups"]
    assert (g2["total"], g2["returned"], g2["next"]) == (PANEL_GROUP_ROWS + 1, 1, None)
    seen = {n["id"] for n in first["nodes"]} | {n["id"] for n in second["nodes"]}
    assert seen == set(mems)
    # counts never move with the window
    assert second["counts"] == first["counts"]


def test_group_params_reject(
    graph_client: TestClient, fixture_graph: dict[str, int]
) -> None:
    focus = fixture_graph["focus"]
    r = graph_client.get(f"/graph/paper/{focus}.json?group=Nope")
    assert r.status_code == 400 and "group" in r.json()["error"]
    r = graph_client.get(f"/graph/paper/{focus}.json?offset=3")
    assert r.status_code == 400 and "offset" in r.json()["error"]
    # a rels filter outside the group leaves the group empty, not unfiltered
    body = graph_client.get(
        f"/graph/paper/{focus}.json?rels=serves&group=Notes%20%26%20links"
    ).json()
    assert body["edges"] == [] and body["groups"] == []


# --- profile owner scoping (gr490266) ---------------------------------------


@pytest.fixture
def authed_graph_client(runtime_with_store, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path, auth_required=True)
    return TestClient(create_app(runtime=runtime_with_store, web_config=cfg))


def _basic(login: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def profile_graph(store: Store) -> dict[str, Any]:
    """reto's live profile ('SecretPalette') and a retired one
    ('RetiredPalette') both linked to a todo any signed-in user can see."""
    store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])
    live = store.insert_ref(
        kind="profile",
        slug=None,
        title="SecretPalette",
        meta={"profile": {"name": "SecretPalette"}},
        owner_login="reto",
    )
    dead = store.insert_ref(
        kind="profile",
        slug=None,
        title="RetiredPalette",
        meta={"profile": {"name": "RetiredPalette"}},
        owner_login="reto",
    )
    todo = store.insert_ref(kind="todo", slug=None, title="Hub")
    store.add_link(src_ref_id=todo.id, dst_ref_id=live.id, relation="related-to")
    store.add_link(src_ref_id=todo.id, dst_ref_id=dead.id, relation="related-to")
    store.retire_ref(dead.id)
    return {"live": live.id, "dead": dead.id, "todo": todo.id}


def test_profile_root_hidden_from_other_owner(
    authed_graph_client: TestClient, profile_graph: dict[str, Any]
) -> None:
    r = authed_graph_client.get(
        f"/graph/profile/{profile_graph['live']}.json", headers=_basic("alice", "pw")
    )
    assert r.status_code == 404
    assert "SecretPalette" not in r.text


def test_profile_redacted_from_other_owners_ring(
    authed_graph_client: TestClient, profile_graph: dict[str, Any]
) -> None:
    r = authed_graph_client.get(
        f"/graph/todo/{profile_graph['todo']}.json", headers=_basic("alice", "pw")
    )
    assert r.status_code == 200
    assert "SecretPalette" not in r.text
    assert "RetiredPalette" not in r.text
    hidden = {
        _h("profile", profile_graph["live"]),
        _h("profile", profile_graph["dead"]),
    }
    body = r.json()
    assert not hidden & {_h(n["kind"], n["id"]) for n in body["nodes"]}
    assert not hidden & {h for e in body["edges"] for h in (e["src"], e["dst"])}
    assert not hidden & set(body["meta"]["native_targets"])


def test_profile_visible_to_its_owner(
    authed_graph_client: TestClient, profile_graph: dict[str, Any]
) -> None:
    hdr = _basic("reto", "pw")
    live = _h("profile", profile_graph["live"])
    root = authed_graph_client.get(
        f"/graph/profile/{profile_graph['live']}.json", headers=hdr
    )
    assert root.status_code == 200
    assert "SecretPalette" in root.text
    body = authed_graph_client.get(
        f"/graph/todo/{profile_graph['todo']}.json", headers=hdr
    ).json()
    (node,) = [n for n in body["nodes"] if n["id"] == profile_graph["live"]]
    assert node["label"] == "SecretPalette"
    assert any(live in (e["src"], e["dst"]) for e in body["edges"])
    assert live in body["meta"]["native_targets"]


def test_intra_edges_stay_owner_filtered(
    authed_graph_client: TestClient,
    store: Store,
    profile_graph: dict[str, Any],
) -> None:
    """A hop-1 ↔ hop-1 edge is emitted only when both endpoints are in
    the returned set: another owner's profile never joins it (gr490266),
    so no ``dir: "cross"`` edge can name it for the wrong viewer — and
    the same edge does appear for the owner."""
    mem = store.insert_ref(kind="memory", slug=None, title="visible").id
    store.add_link(
        src_ref_id=profile_graph["todo"], dst_ref_id=mem, relation="related-to"
    )
    store.add_link(
        src_ref_id=mem, dst_ref_id=profile_graph["live"], relation="related-to"
    )
    other = authed_graph_client.get(
        f"/graph/todo/{profile_graph['todo']}.json", headers=_basic("alice", "pw")
    )
    assert other.status_code == 200
    assert "SecretPalette" not in other.text
    assert not [e for e in other.json()["edges"] if e["dir"] in ("self", "cross")]
    owner = authed_graph_client.get(
        f"/graph/todo/{profile_graph['todo']}.json", headers=_basic("reto", "pw")
    ).json()
    ring = [e for e in owner["edges"] if e["dir"] == "cross"]
    assert [(e["src"], e["dst"], e["rel"]) for e in ring] == [
        (_h("memory", mem), _h("profile", profile_graph["live"]), "related-to")
    ]
