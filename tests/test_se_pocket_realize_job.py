"""``se_pocket_realize`` job_type — the pocket realize loop as a
compute-group job (pocket-from-molecule Q13, gr519389).

Proved without a cluster the way ``test_hexfold_relax_job`` does: a
fake ``DispatchContext`` drives the dispatch against the same posed
``realize`` design ``test_se_pocket_realize`` builds, with tblite
stubbed (``_FakeTBLite``) — the dispatch reloads the tree, re-poses
the candidate structures, runs the loop and stores the report + the
JSON result as job chunks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from precis.dispatch import Hub, _try
from precis.errors import Unsupported
from precis.store import Store
from precis.workers import job_types as jt
from precis.workers.executors._context import DispatchContext
from precis_se import pocket_realize_job
from precis_se.handler import SeHandler
from tests.test_se_pocket_realize import (
    _NO,
    _OPS,
    _RING,
    _WALL_POSE_A,
    _run_migration,
    _save_structure,
    _stub_tblite,
    _wall,
)


@pytest.fixture
def taxonomy(store: Store) -> Store:
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0228_se_measurands_esp_binding.sql")
    return store


@pytest.fixture
def posed(store: Store, taxonomy: Store, monkeypatch: pytest.MonkeyPatch) -> SeHandler:
    """Same posed design as test_se_pocket_realize's fixture: NO over
    the graphene wall's z window, the three candidate structures
    stored, the pocket minted by ``pocket_from_molecule``."""
    _stub_tblite(monkeypatch)
    _save_structure(store, "wall-struct", ["C"] * 7, _RING - _WALL_POSE_A)
    _save_structure(store, "mol-struct", ["N", "O"], _NO)
    for kind in ("graphene", "n-doped", "oh-rim"):
        elements, xyz = _wall(kind)
        _save_structure(store, f"cand-{kind}", elements, xyz - _WALL_POSE_A)
    # `_try` registers the se abilities on the hub, so the job handler
    # sees `se` as a can_own_jobs kind when the op enqueues
    # (test_se_simp_bridge's pattern).
    se = _try(SeHandler, hub=Hub(store=store))
    assert se is not None
    se.put(id="realize", text=json.dumps({"ops": _OPS}))
    se.edit(
        id="realize",
        ops=[
            {
                "op": "pocket_from_molecule",
                "molecule": "mol",
                "wall": "wall",
                "z_window": [4.0, 5.0],
            }
        ],
    )
    return se


@pytest.fixture
def register_job_type() -> Any:
    """Inject ``se_pocket_realize`` into the registry (no entry-point
    discovery at test time — the register_se_simp pattern)."""
    jt._REGISTRY[pocket_realize_job.JOB_TYPE] = pocket_realize_job.SPEC
    yield
    jt._REGISTRY.pop(pocket_realize_job.JOB_TYPE, None)


def _job_params(store: Store, parent_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [dict((r[0] or {}).get("params") or {}) for r in rows]


def _no_gfn2() -> None:
    raise Unsupported("pocket_realize needs GFN2 (tblite): no backend")


def _se_ref(store: Store) -> Any:
    ref = store.get_ref(kind="se", id="realize")
    assert ref is not None
    return ref


def _fake_ctx(
    store: Store,
    params: dict[str, Any],
    is_cancel_requested: Callable[[], bool] = lambda: False,
) -> tuple[DispatchContext, list]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=4242,
        title="se_pocket_realize",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=is_cancel_requested,
    )
    return ctx, events


def _real_ctx(
    store: Store, job_id: int, meta: dict[str, Any]
) -> tuple[DispatchContext, list]:
    """Like ``_fake_ctx`` but writing chunks, status and meta to the real
    job ref — what the executor's ``DispatchContext`` does — so
    ``get(kind='job')`` reads the stored result back."""
    from precis.workers.executors import _common

    events: list[tuple[str, Any]] = []

    def _append(kind: str, text: str) -> None:
        with store.pool.connection() as conn:
            _common.append_chunk(store, job_id, kind, text, conn=conn)
            conn.commit()
        events.append((kind, text))

    def _status(value: str) -> None:
        with store.pool.connection() as conn:
            _common.set_status(store, job_id, value, conn=conn)
            conn.commit()
        events.append(("status", value))

    def _set_meta(**fields: Any) -> None:
        with store.pool.connection() as conn:
            _common.set_meta(conn, job_id, **fields)
            conn.commit()
        events.append(("meta", fields))

    ctx = DispatchContext(
        store=store,
        ref_id=job_id,
        title="se_pocket_realize",
        meta=meta,
        set_status=_status,
        append_chunk=_append,
        set_meta=_set_meta,
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _minted_jobs(store: Store, parent_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _params(ref_id: int, **over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "se_ref_id": ref_id,
        "slug": "realize",
        "wall": "wall",
        "candidates": ["cand-graphene", "cand-n-doped", "cand-oh-rim"],
    }
    params.update(over)
    return params


def test_dispatch_ranks_and_stores_the_report(posed: SeHandler) -> None:
    """End to end: the dispatch reloads the tree, re-poses the three
    candidate structures, runs the loop on stubbed tblite, and stores
    the report text + the full JSON ranking + a meta summary."""
    store = posed.store
    ref = _se_ref(store)
    ctx, events = _fake_ctx(store, _params(int(ref.id)))
    pocket_realize_job._dispatch(ctx, pocket_realize_job.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert not fails, fails
    assert ("status", "succeeded") in events
    summary = next(t for k, t in events if k == "job_summary")
    assert "pocket_realize" in summary
    assert "1. cand-oh-rim" in summary
    payload = json.loads(next(t for k, t in events if k == "job_result"))
    assert payload["evaluated"] == 3
    assert payload["results"][0]["name"] == "cand-oh-rim"
    assert payload["results"][0]["provenance"]["source"] == "structure:cand-oh-rim"
    meta = next(p for k, p in events if k == "meta")
    assert meta["evaluated"] == 3 and meta["best"] == "cand-oh-rim"


def test_dispatch_fails_infra_without_tblite(
    posed: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed node missing tblite fails the job infra, naming the
    backend — never a scored run."""
    from precis_se import pocket_realize

    monkeypatch.setattr(pocket_realize, "_require_gfn2", _no_gfn2)
    store = posed.store
    ref = _se_ref(store)
    ctx, events = _fake_ctx(store, _params(int(ref.id)))
    pocket_realize_job._dispatch(ctx, pocket_realize_job.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "infra"
    assert "tblite" in fails[0]["reason"]


def test_dispatch_fails_by_name_on_stale_params(posed: SeHandler) -> None:
    """A gone wall block, pocket or candidate slug fails by name —
    never a silent empty run."""
    store = posed.store
    ref = _se_ref(store)
    cases = [
        (_params(int(ref.id), wall="gone"), "no block 'gone'"),
        (_params(int(ref.id), pocket="gone"), "no pocket 'gone'"),
        (_params(int(ref.id), candidates=["gone"]), "candidate 'gone'"),
    ]
    for params, needle in cases:
        ctx, events = _fake_ctx(store, params)
        pocket_realize_job._dispatch(ctx, pocket_realize_job.SPEC)
        fails = [p for k, p in events if k == "fail"]
        assert len(fails) == 1, (params, events)
        assert needle in fails[0]["reason"], fails[0]["reason"]


def test_dispatch_malformed_params(posed: SeHandler) -> None:
    store = posed.store
    ctx, events = _fake_ctx(store, {"se_ref_id": _se_ref(store).id})
    pocket_realize_job._dispatch(ctx, pocket_realize_job.SPEC)
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "infra"
    assert "malformed params" in fails[0]["reason"]


def test_validate_submit(posed: SeHandler) -> None:
    store = posed.store
    ref = _se_ref(store)
    assert (
        pocket_realize_job._validate_submit(store, params=_params(int(ref.id))) is None
    )
    assert "se_ref_id" in (pocket_realize_job._validate_submit(store, params={}) or "")
    assert "no se ref" in (
        pocket_realize_job._validate_submit(store, params=_params(424242)) or ""
    )
    assert "wall" in (
        pocket_realize_job._validate_submit(store, params=_params(int(ref.id), wall=""))
        or ""
    )
    assert "candidates" in (
        pocket_realize_job._validate_submit(
            store, params=_params(int(ref.id), candidates=[])
        )
        or ""
    )


def test_op_enqueues_the_job_when_tblite_is_absent(
    posed: SeHandler, monkeypatch: pytest.MonkeyPatch, register_job_type: Any
) -> None:
    """The Q13 path end to end: no local GFN2 + a configured compute
    lane ⇒ the op returns a PocketRealizeRequest, the handler mints an
    se_pocket_realize job post-commit, pinned to a lane host."""
    from precis_se import pocket_realize

    monkeypatch.setattr(pocket_realize, "_require_gfn2", _no_gfn2)
    monkeypatch.setenv("PRECIS_MLIP_NODES", "node-a,node-b")
    store = posed.store
    ref = _se_ref(store)
    resp = posed.edit(
        id="realize",
        ops=[
            {
                "op": "pocket_realize",
                "wall": "wall",
                "candidates": ["cand-graphene", "cand-oh-rim"],
            }
        ],
    )
    body = resp.body if hasattr(resp, "body") else str(resp)
    assert "enqueued" in body
    assert "se_pocket_realize" in body
    params = _job_params(store, int(ref.id))
    assert len(params) == 1
    (p,) = params
    assert p["se_ref_id"] == int(ref.id)
    assert p["wall"] == "wall"
    assert p["pocket"] is not None
    assert p["candidates"] == ["cand-graphene", "cand-oh-rim"]
    assert p["target_node"] in ("node-a", "node-b")


def test_get_reads_back_the_stored_result(
    posed: SeHandler, monkeypatch: pytest.MonkeyPatch, register_job_type: Any
) -> None:
    """The Q13 promise end to end: the op enqueues on the no-tblite
    path, the dispatch writes its chunks to the real job ref, and
    get(kind='job') reads the result back — a capped head on the
    default card, the full JSON on view='result'."""
    from precis.workers.executors import _common
    from precis_se import pocket_realize

    real_require = pocket_realize._require_gfn2
    monkeypatch.setattr(pocket_realize, "_require_gfn2", _no_gfn2)
    monkeypatch.setenv("PRECIS_MLIP_NODES", "node-a,node-b")
    store = posed.store
    ref = _se_ref(store)
    posed.edit(
        id="realize",
        ops=[
            {
                "op": "pocket_realize",
                "wall": "wall",
                "candidates": ["cand-graphene", "cand-oh-rim"],
            }
        ],
    )
    (job,) = _minted_jobs(store, int(ref.id))
    job_id, meta = job
    # The enqueued node DOES carry tblite (the wheelhouse build) — run
    # the dispatch with the stubbed backend restored.
    monkeypatch.setattr(pocket_realize, "_require_gfn2", real_require)
    ctx, events = _real_ctx(store, job_id, meta)
    pocket_realize_job._dispatch(ctx, pocket_realize_job.SPEC)
    assert [p for k, p in events if k == "fail"] == []
    assert ("status", "succeeded") in events

    job_handler = posed.hub.sibling("job")
    card = job_handler.get(id=job_id).body
    assert "## summary" in card
    assert "1. cand-oh-rim" in card
    assert "## result" in card

    full = job_handler.get(id=job_id, view="result").body
    payload = json.loads(full.split("## result ", 1)[1].split("\n", 1)[1])
    assert payload["evaluated"] == 2
    assert payload["results"][0]["name"] == "cand-oh-rim"

    # An oversized result reads as a capped head on the card, in full
    # on view='result'.
    big = "x" * 5000
    with store.pool.connection() as conn:
        _common.append_chunk(store, job_id, "job_result", big, conn=conn)
        conn.commit()
    card = job_handler.get(id=job_id).body
    assert "… (5000 chars total" in card
    assert "view='result'" in card
    assert big not in card
    assert big in job_handler.get(id=job_id, view="result").body


def test_second_identical_op_reuses_the_active_job(
    posed: SeHandler, monkeypatch: pytest.MonkeyPatch, register_job_type: Any
) -> None:
    """Idem dedupe: a second identical ``pocket_realize`` op mints no
    new job — the same idem_key lands on the still-active one."""
    from precis_se import pocket_realize

    monkeypatch.setattr(pocket_realize, "_require_gfn2", _no_gfn2)
    monkeypatch.setenv("PRECIS_MLIP_NODES", "node-a,node-b")
    store = posed.store
    ref = _se_ref(store)
    op = {
        "op": "pocket_realize",
        "wall": "wall",
        "candidates": ["cand-graphene", "cand-oh-rim"],
    }
    posed.edit(id="realize", ops=[op])
    resp = posed.edit(id="realize", ops=[op])
    body = resp.body if hasattr(resp, "body") else str(resp)
    assert "existing job" in body
    assert len(_minted_jobs(store, int(ref.id))) == 1


def test_op_refuses_up_front_when_no_host_has_the_lane(
    posed: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tblite AND no configured compute node ⇒ the same up-front
    Unsupported refusal as before the job half existed — nothing is
    enqueued, nothing half-said."""
    from precis_se import pocket_realize

    monkeypatch.setattr(pocket_realize, "_require_gfn2", _no_gfn2)
    for env in (
        "PRECIS_MLIP_NODES",
        "PRECIS_AUTOCATPATH_ROUTE_NODE",
        "PRECIS_DFT_NODE",
    ):
        monkeypatch.delenv(env, raising=False)
    store = posed.store
    ref = _se_ref(store)
    with pytest.raises(Unsupported, match="nowhere to run"):
        posed.edit(
            id="realize",
            ops=[
                {
                    "op": "pocket_realize",
                    "wall": "wall",
                    "candidates": ["cand-graphene"],
                }
            ],
        )
    assert _job_params(store, int(ref.id)) == []
