"""Real-PG tests for ``GET /graph/<kind>/<id>.json`` (``routes/graph.py``).

The route is a thin JSON face over ``Store.neighbourhood``; what is pinned
here is the HTTP contract — parity with the store (and so with
``links_for(direction='both')`` plus the inverse rule), the ``groups``
bucketing by ``refeye.ring_group``, query-param plumbing, JSON errors and
the round-trip count — not the store's own semantics
(``tests/test_link_crud.py::TestNeighbourhood``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis.utils import handle_registry
from precis.utils.refeye import ring_group
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.graph import DEFAULT_CAP, MAX_CAP, ring_groups


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


@contextmanager
def _query_counter(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    counts = {"n": 0}
    original = psycopg.Connection.execute

    def counting_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        # the pool's liveness probe (``check_connection`` -> ``execute("")``)
        # is not a query
        if str(args[0]).strip():
            counts["n"] += 1
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

    expected = store.neighbourhood("paper", focus, cap=DEFAULT_CAP)
    expected["groups"] = ring_groups(expected["counts"])
    assert body == expected
    assert body["focus"] == {"kind": "paper", "id": focus, "label": "Paper focus2020"}
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
    assert body["groups"] == [
        {"heading": "Roadmap", "rels": ["serves"], "n": 1},
        {"heading": "Notes & links", "rels": ["cites", "related-to"], "n": 2},
        {"heading": "Other", "rels": ["cited-by", "supersedes"], "n": 2},
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
    assert deep == {
        **store.neighbourhood("paper", focus, depth=2, cap=DEFAULT_CAP),
        "groups": deep["groups"],
    }


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
    """Focus row + one hop-1 statement (+ one for hop 2), nothing per node
    or per edge; the inverse map is warmed first so its one-off read does
    not count."""
    focus = fixture_graph["focus"]
    store.inverse_relation("cites")
    with _query_counter(monkeypatch) as c1:
        assert graph_client.get(f"/graph/paper/{focus}.json").status_code == 200
    with _query_counter(monkeypatch) as c2:
        assert graph_client.get(f"/graph/paper/{focus}.json?depth=2").status_code == 200
    assert (c1["n"], c2["n"]) == (2, 3)
