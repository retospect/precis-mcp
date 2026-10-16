"""STEER slices 1+2: kept alternative routes + LLM strategy rerank, then a
strategy recorded at ``put`` reranked inside the retrosynth job (or inline
in the put without a route node). ``view='steer'`` is read-only (gr491367).

No model is called: every test injects a fake ``ask`` / patches
``steer.router_ask``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from precis.dispatch import Hub, _try
from precis.errors import BadInput
from precis.store import Store
from precis.store.types import Tag
from precis_chem import jobs as chem_jobs
from precis_chem import steer
from precis_chem.aizynth import parse_aizynth_trees
from precis_chem.engine import StubEngine
from precis_chem.ir import RouteGraph, RouteStep
from precis_chem.jobs import RETROSYNTH_SPEC
from precis_chem.persist import apply_route_result
from precis_chem.route import RouteHandler
from tests.test_route_plugin import (  # noqa: F401 — fixtures
    _FakeCtx,
    register_retrosynth,
    route_store,
)


def _tree(product: str, reactants: list[str], score: float) -> dict[str, Any]:
    return {
        "type": "mol",
        "smiles": product,
        "in_stock": False,
        "scores": {"state score": score},
        "children": [
            {
                "type": "reaction",
                "smiles": ".".join(reactants) + ">>" + product,
                "metadata": {"template": "t", "policy_probability": 0.5},
                "children": [
                    {"type": "mol", "smiles": r, "in_stock": True, "children": []}
                    for r in reactants
                ],
            }
        ],
    }


_TREES = [
    _tree("CC(=O)NC", ["CC(=O)O", "CN"], 0.9),
    _tree("CC(=O)NC", ["CC(=O)Cl", "CN"], 0.8),
    _tree("CC(=O)NC", ["CC(=O)OC(C)=O", "CN"], 0.7),
]


def _graph() -> RouteGraph:
    return parse_aizynth_trees(json.dumps(_TREES), target="CC(=O)NC")


def test_aizynth_parser_keeps_alternatives_and_roundtrips() -> None:
    g = _graph()
    assert g.steps[0].reactants == ["CC(=O)O", "CN"]
    assert [a.steps[0].reactants[0] for a in g.alternatives] == [
        "CC(=O)Cl",
        "CC(=O)OC(C)=O",
    ]
    back = RouteGraph.from_json(g.to_json())
    assert len(back.alternatives) == 2
    assert back.alternatives[0].alternatives == []
    assert "2 lower-ranked alternative route(s) kept" in g.render()
    only_top = parse_aizynth_trees(json.dumps(_TREES), max_alternatives=0)
    assert only_top.alternatives == []


def test_old_blob_without_alternatives_still_parses() -> None:
    blob = _graph().to_json()
    blob.pop("alternatives")
    assert RouteGraph.from_json(blob).alternatives == []


def test_parse_reply() -> None:
    assert steer.parse_reply("SCORE: 7\nRATIONALE: late amide.") == (7.0, "late amide.")
    assert steer.parse_reply("score: 14 rationale: x")[0] == 10.0
    assert steer.parse_reply("no idea")[0] is None


def test_strategy_validation() -> None:
    with pytest.raises(ValueError):
        steer.normalise_strategy("   ")
    with pytest.raises(ValueError):
        steer.normalise_strategy("x" * 600)
    assert steer.normalise_strategy(" avoid  acid chlorides ") == "avoid acid chlorides"


def _fake_ask(calls: list[str]) -> steer.Ask:
    """Scores the acid-chloride route low, fails on the anhydride route."""

    def ask(prompt: str) -> tuple[str, str | None]:
        calls.append(prompt)
        if "CC(=O)OC(C)=O" in prompt:
            return "", "model unavailable"
        if "CC(=O)Cl" in prompt:
            return "SCORE: 1\nRATIONALE: uses an acid chloride in step 1.", None
        return "SCORE: 9\nRATIONALE: direct coupling, no acid chloride.", None

    return ask


def test_steer_ranks_and_marks_unscored_last() -> None:
    calls: list[str] = []
    scores = steer.steer("avoid acid chlorides", _graph(), ask=_fake_ask(calls))
    assert len(calls) == 3
    assert "avoid acid chlorides" in calls[0]
    assert [(s.engine_rank, s.score) for s in scores] == [(1, 9.0), (2, 1.0), (3, None)]
    assert scores[2].error == "model unavailable"


def test_route_text_carries_step_classes() -> None:
    g = RouteGraph(
        target="T",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(
                id=1,
                product="T",
                reactants=["A", "B"],
                reaction_class="RXNO:0000357",
                class_name="N-acylation to amide",
                in_stock=True,
            )
        ],
    )
    assert "step 1: T <= A + B [N-acylation to amide] (in stock)" in steer.route_text(g)


def _raising_ask(prompt: str) -> tuple[str, str | None]:
    raise AssertionError("view='steer' called the model")


def test_view_steer_is_read_only(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr491367: a get never calls the model or writes the ref. A miss names
    the exact put that computes the rerank; a hit renders the cache."""
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    h = RouteHandler(hub=Hub(store=route_store))
    h.put(id="amide", target="CC(=O)NC", engine="stub")
    before = _route_meta(route_store, "amide")
    h.steer_ask = _raising_ask
    monkeypatch.setattr(steer, "router_ask", _raising_ask)

    def _no_write(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("view='steer' wrote to the store")

    monkeypatch.setattr(route_store, "stamp_ref_meta", _no_write)
    monkeypatch.setattr(route_store, "insert_ref", _no_write)

    body = h.get(id="amide", view="steer", q="avoid  acid chlorides").body
    assert "no rerank yet for strategy 'avoid acid chlorides'" in body
    # The hint repeats the plan's content address so the put is a cache hit.
    assert "put(kind='route', id='amide', target='CC(=O)NC', engine=" in body
    assert "strategy='avoid acid chlorides')" in body
    assert _route_meta(route_store, "amide") == before
    with pytest.raises(BadInput):
        h.get(id="amide", view="steer")


def test_put_strategy_inline_scores_caches_and_refreshes(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No route node: the put is the explicit write that reranks inline;
    view='steer' then reads the cache, and put(refresh=True) re-asks."""
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    h = RouteHandler(hub=Hub(store=route_store))
    calls: list[str] = []
    h.steer_ask = _fake_ask(calls)

    body = h.put(
        id="amide", target="CC(=O)NC", engine="stub", strategy="avoid acid chlorides"
    ).body
    assert "1. engine route #1: score 9" in body
    assert "2. engine route #2: score 1" in body
    assert "UNSCORED (model unavailable)" in body
    assert len(calls) == 3
    assert len(_route_meta(route_store, "amide").get("steer") or {}) == 1

    again = h.get(id="amide", view="steer", q="avoid  acid chlorides").body
    assert "cached" in again and "1. engine route #1: score 9" in again
    assert len(calls) == 3

    r = h.put(
        id="amide", target="CC(=O)NC", engine="stub", strategy="avoid acid chlorides"
    )
    assert "already ranked" in r.body and len(calls) == 3

    h.put(
        id="amide",
        target="CC(=O)NC",
        engine="stub",
        strategy="avoid acid chlorides",
        refresh=True,
    )
    assert len(calls) == 6
    cached = _route_meta(route_store, "amide").get("steer") or {}
    assert len(cached) == 1

    # A refresh that scores nothing keeps the previous ranking.
    h.steer_ask = lambda prompt: ("", "model unavailable")
    h.put(
        id="amide",
        target="CC(=O)NC",
        engine="stub",
        strategy="avoid acid chlorides",
        refresh=True,
    )
    assert _route_meta(route_store, "amide").get("steer") == cached


def test_put_strategy_all_unscored_stays_uncached(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rerank whose every candidate fails is never cached (no default
    score), so the next put retries."""
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    h = RouteHandler(hub=Hub(store=route_store))
    h.steer_ask = lambda prompt: ("", "model unavailable")
    h.put(id="amide", target="CC(=O)NC", engine="stub", strategy="fewest steps")
    assert not _route_meta(route_store, "amide").get("steer")


# ── slice 2: strategy recorded at put, reranked inside the job ─────────


def _job_rows(store: Store, route_ref_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' "
            "AND parent_id = %s AND retired_at IS NULL ORDER BY ref_id",
            (route_ref_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def test_put_records_strategy_and_returns_at_once(
    route_store: Store,  # noqa: F811
    register_retrosynth: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compute lane: the put records the strategy and returns at once — no
    LLM call in the request (the rerank runs in the job), and the page notes
    the pending rerank."""
    monkeypatch.setenv("PRECIS_CHEM_ROUTE_NODE", "spark")
    h = _try(RouteHandler, hub=Hub(store=route_store))
    assert h is not None
    calls: list[str] = []
    h.steer_ask = _fake_ask(calls)
    monkeypatch.setattr(steer, "router_ask", _fake_ask(calls))
    resp = h.put(
        id="amide", target="CC(=O)NC", engine="stub", strategy=" avoid  acid chlorides "
    )
    assert "strategy 'avoid acid chlorides' recorded" in resp.body
    assert calls == []  # the put dispatches; the job calls the model
    meta = _route_meta(route_store, "amide")
    assert meta["steer_strategies"] == ["avoid acid chlorides"]
    [(_, job_meta)] = _job_rows(
        route_store, h.store.get_ref(kind="route", id="amide").id
    )
    assert job_meta["params"]["strategy"] == "avoid acid chlorides"
    # Plan landed, rerank not yet — the page says so instead of hiding it.
    apply_route_result(
        route_store, job_meta["params"]["route_ref_id"], _graph(), cache_key="k"
    )
    assert "not landed yet" in h.get(id="amide").body
    assert calls == []


def test_put_strategy_validates(
    route_store: Store,  # noqa: F811
) -> None:
    h = RouteHandler(hub=Hub(store=route_store))
    with pytest.raises(BadInput):
        h.put(id="a", target="CCO", strategy="   ")
    with pytest.raises(BadInput):
        h.put(id="a", target="CCO", strategy="x" * 600)
    assert route_store.get_ref(kind="route", id="a") is None


def test_dispatch_reranks_fresh_plan_and_summary_names_pick(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan job that lands with a recorded strategy steers the candidates
    and its summary names the top-ranked route + score."""
    calls: list[str] = []
    monkeypatch.setattr(steer, "router_ask", _fake_ask(calls))
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    ref = route_store.insert_ref(
        kind="route",
        slug="amide",
        title="amide",
        meta={
            "target": "CC(=O)NC",
            "status": "planning",
            "steer_strategies": ["avoid acid chlorides"],
        },
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CC(=O)NC",
        "engine": "stub",
        "engine_version": "stub-v1",
        "cache_key": "retrosynth:k",
        "strategy": "avoid acid chlorides",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    assert len(calls) == 3  # one routed call per candidate, in the job
    meta = _route_meta(route_store, "amide")
    assert len(meta.get("steer") or {}) == 1
    summary = "\n".join(t for k, t in ctx.chunks if k == "job_summary")
    assert "engine route #1 best, score 9/10" in summary


def test_dispatch_skips_engine_when_plan_already_landed(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steer-only re-entry: same cache_key + a landed route ⇒ no engine
    call, just the rerank."""
    calls: list[str] = []
    monkeypatch.setattr(steer, "router_ask", _fake_ask(calls))
    plans: list[str] = []

    def _plan(self: Any, t: str, **kw: Any) -> RouteGraph:
        plans.append(t)
        return _graph()

    monkeypatch.setattr(StubEngine, "plan", _plan)
    ref = route_store.insert_ref(
        kind="route",
        slug="amide",
        title="amide",
        meta={
            "target": "CC(=O)NC",
            "status": "solved",
            "cache_key": "retrosynth:k",
            "steer_strategies": ["fewest steps"],
        },
    )
    apply_route_result(route_store, ref.id, _graph(), cache_key="retrosynth:k")
    params = {
        "route_ref_id": ref.id,
        "target": "CC(=O)NC",
        "engine": "stub",
        "engine_version": "stub-v1",
        "cache_key": "retrosynth:k",
        "strategy": "fewest steps",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    assert plans == []  # the engine never re-ran
    assert len(calls) == 3
    meta = _route_meta(route_store, "amide")
    assert len(meta.get("steer") or {}) == 1
    summary = "\n".join(t for k, t in ctx.chunks if k == "job_summary")
    assert "steer 'fewest steps': engine route #1 best, score 9/10" in summary


def test_two_puts_differing_only_in_strategy_share_one_run(
    route_store: Store,  # noqa: F811
    register_retrosynth: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """put s1 → job plans + ranks; put s2 → cache hit mints a steer-only job
    that ranks s2 without re-running the engine."""
    monkeypatch.setenv("PRECIS_CHEM_ROUTE_NODE", "spark")
    calls: list[str] = []
    monkeypatch.setattr(steer, "router_ask", _fake_ask(calls))
    plans: list[str] = []

    def _plan(self: Any, t: str, **kw: Any) -> RouteGraph:
        plans.append(t)
        return _graph()

    monkeypatch.setattr(StubEngine, "plan", _plan)
    h = _try(RouteHandler, hub=Hub(store=route_store))
    assert h is not None

    r1 = h.put(
        id="amide", target="CC(=O)NC", engine="stub", strategy="avoid acid chlorides"
    )
    assert "rerank runs in the job" in r1.body
    route_ref = route_store.get_ref(kind="route", id="amide")
    assert route_ref is not None
    job1_id, job1_meta = _job_rows(route_store, route_ref.id)[0]
    ctx = _FakeCtx(store=route_store, params=job1_meta["params"])
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status == "succeeded"
    route_store.add_tag(job1_id, Tag.closed("STATUS", "succeeded"), set_by="system")

    r2 = h.put(id="amide", target="CC(=O)NC", engine="stub", strategy="fewest steps")
    assert "cache hit" in r2.body
    job2_id, job2_meta = _job_rows(route_store, route_ref.id)[-1]
    assert job2_id != job1_id  # a fresh, steer-only job
    assert job2_meta["params"]["strategy"] == "fewest steps"
    ctx2 = _FakeCtx(store=route_store, params=job2_meta["params"])
    chem_jobs._dispatch(ctx2, RETROSYNTH_SPEC)
    assert ctx2.status == "succeeded"

    assert plans == ["CC(=O)NC"]  # one engine run total
    assert len(calls) == 6  # three candidates scored per strategy
    meta = _route_meta(route_store, "amide")
    assert meta["steer_strategies"] == ["avoid acid chlorides", "fewest steps"]
    assert len(meta.get("steer") or {}) == 2  # two rankings
    summary2 = "\n".join(t for k, t in ctx2.chunks if k == "job_summary")
    assert "steer 'fewest steps': engine route #1 best, score 9/10" in summary2

    # A third put with an already-ranked strategy is a pure cache hit — no
    # new job, no new calls.
    before = len(_job_rows(route_store, route_ref.id))
    r3 = h.put(id="amide", target="CC(=O)NC", engine="stub", strategy="fewest steps")
    assert "already ranked" in r3.body
    assert len(_job_rows(route_store, route_ref.id)) == before
    assert len(calls) == 6

    # refresh=True on a ranked strategy mints a steer-only job that re-asks
    # for that strategy alone; the engine still does not re-run.
    route_store.add_tag(job2_id, Tag.closed("STATUS", "succeeded"), set_by="system")
    h.put(
        id="amide",
        target="CC(=O)NC",
        engine="stub",
        strategy="fewest steps",
        refresh=True,
    )
    job4_id, job4_meta = _job_rows(route_store, route_ref.id)[-1]
    assert len(_job_rows(route_store, route_ref.id)) == before + 1
    assert job4_meta["params"]["steer_refresh"] is True
    ctx4 = _FakeCtx(store=route_store, params=job4_meta["params"])
    chem_jobs._dispatch(ctx4, RETROSYNTH_SPEC)
    assert ctx4.status == "succeeded"
    assert len(calls) == 9 and plans == ["CC(=O)NC"]
    assert len(_route_meta(route_store, "amide").get("steer") or {}) == 2


def _fake_ask_reorder(calls: list[str]) -> steer.Ask:
    """Ranks engine route #2 first — the strategy pick differs from the
    engine's own top."""

    def ask(prompt: str) -> tuple[str, str | None]:
        calls.append(prompt)
        if "<= CC(=O)Cl" in prompt:
            return "SCORE: 8\nRATIONALE: chloride fits the strategy.", None
        if "<= CC(=O)OC(C)=O" in prompt:
            return "SCORE: 5\nRATIONALE: anhydride is middling.", None
        return "SCORE: 3\nRATIONALE: plain coupling.", None

    return ask


def test_route_page_leads_with_strategy_pick(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scored strategy makes the page lead with its pick; the engine's own
    top route is shown beside it with its strategy rank."""
    calls: list[str] = []
    monkeypatch.setattr(steer, "router_ask", _fake_ask_reorder(calls))
    ref = route_store.insert_ref(
        kind="route",
        slug="amide",
        title="amide",
        meta={
            "target": "CC(=O)NC",
            "status": "planning",
            "steer_strategies": ["fastest chloride route"],
        },
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CC(=O)NC",
        "engine": "stub",
        "engine_version": "stub-v1",
        "cache_key": "retrosynth:k",
        "target_node": "spark",
    }
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status == "succeeded"

    body = RouteHandler(hub=Hub(store=route_store)).get(id="amide").body
    pick = body.index("## strategy pick — engine route #2, score 8/10")
    top = body.index("## engine's top route — ranked #3 for this strategy")
    assert pick < top
    assert "strategy: fastest chloride route" in body
    # view='steer' with the recorded strategy is now a cache hit.
    h = RouteHandler(hub=Hub(store=route_store))
    h.steer_ask = _raising_ask
    assert "cached" in h.get(id="amide", view="steer", q="fastest chloride route").body
    assert len(calls) == 3  # the get made no new call


# ── deterministic pre-rank (gr491450) ──────────────────────────────────


def _one_step(reactant: str, *, n_steps: int = 1) -> RouteGraph:
    steps = [
        RouteStep(
            id=i + 1, product="CC(=O)NC", reactants=[reactant, "CN"], in_stock=True
        )
        for i in range(n_steps)
    ]
    return RouteGraph(
        target="CC(=O)NC",
        engine="stub",
        engine_version="0",
        steps=steps,
        solved=True,
    )


def test_prerank_is_deterministic_and_keeps_every_route() -> None:
    first = [p.to_json() for p in steer.prerank(_graph())]
    second = [p.to_json() for p in steer.prerank(_graph())]
    assert first == second
    assert sorted(p["engine_rank"] for p in first) == [1, 2, 3]
    assert [p["position"] for p in first] == [1, 2, 3]


def test_prerank_high_hazard_sorts_below_equivalent_safer_route() -> None:
    for reactant, name in (("c1ccccc1", "benzene"), ("O=C(Cl)Cl", "phosgene")):
        g = replace(_one_step(reactant), alternatives=[_one_step("CC(=O)O")])
        by_engine = {p.engine_rank: p for p in steer.prerank(g)}
        assert by_engine[1].position == 2 and by_engine[2].position == 1
        assert name in by_engine[1].factors["hazards"]
        assert by_engine[2].factors["hazards"] == []


def test_steer_scores_only_top_k_and_never_drops_a_route() -> None:
    # Engine route k has 9-k steps, so the pre-rank order is 9, 8, ..., 1.
    routes = [_one_step("CC(=O)O", n_steps=9 - k) for k in range(9)]
    g = replace(routes[0], alternatives=routes[1:])
    calls: list[str] = []

    def ask(prompt: str) -> tuple[str, str | None]:
        calls.append(prompt)
        return "SCORE: 5\nRATIONALE: same.", None

    scores = steer.steer("fewest steps", g, ask=ask)
    assert len(calls) == steer.LLM_TOP_K
    assert sorted(s.engine_rank for s in scores) == list(range(1, 10))
    scored = [s for s in scores if s.score is not None]
    folded = [s for s in scores if s.below_fold]
    assert len(scored) == steer.LLM_TOP_K
    assert len(folded) == 9 - steer.LLM_TOP_K
    assert all(s.score is None and s.error is None for s in folded)
    assert scores[: len(scored)] == scored  # below-fold routes come last
    positions = [s.pre.position for s in folded if s.pre]
    assert positions == sorted(positions)
    assert all(s.pre.position > steer.LLM_TOP_K for s in folded if s.pre)


def test_model_only_called_on_put_path_prerank_cached_both_orders(
    route_store: Store,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The put asks the model; neither view='steer' nor the plain page does.
    The cache holds the pre-rank order next to the final one."""
    monkeypatch.setattr(StubEngine, "plan", lambda self, t, **kw: _graph())
    h = RouteHandler(hub=Hub(store=route_store))
    calls: list[str] = []
    h.steer_ask = _fake_ask(calls)
    h.put(id="amide", target="CC(=O)NC", engine="stub", strategy="avoid acid chlorides")
    assert len(calls) == 3

    [entry] = (_route_meta(route_store, "amide").get("steer") or {}).values()
    assert entry["prerank"] == [1, 3, 2]
    assert [s["engine_rank"] for s in entry["scores"]] == [1, 2, 3]

    h.steer_ask = _raising_ask
    monkeypatch.setattr(steer, "router_ask", _raising_ask)
    body = h.get(id="amide", view="steer", q="avoid acid chlorides").body
    assert "pre-rank order" in body and "#1, #3, #2" in body
    assert "pre-rank #3:" in body and "acyl halide" in body
    assert "amide" in h.get(id="amide").body  # plain page: no model call


def _route_meta(store: Store, id: str) -> dict[str, Any]:
    ref = store.get_ref(kind="route", id=id)
    assert ref is not None
    return ref.meta or {}
