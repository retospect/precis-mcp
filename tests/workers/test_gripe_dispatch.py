"""gripe_dispatch: the dark factory's intake pass (td564019 cut 3)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.plans.lanes import ANY_CLASS, Lane
from precis.store import Store
from precis.store.types import Tag
from precis.workers import gripe_dispatch as gd
from tests._lane_rows import use_lanes
from tests._refs import insert_gripe

_AGENT = {"filed_by": {"actor": "agent", "client": "claude-code/2.1.308"}}
_HUMAN = {"filed_by": {"actor": "user:rs", "client": "web"}}
_WORKER = {"filed_by": {"actor": "agent", "source": "memory_gardener"}}


def _gripe(
    store: Store,
    title: str,
    *,
    status: str = "open",
    meta: dict[str, Any] | None = None,
    tags: tuple[str, ...] = ("fix:code",),
    age_minutes: float = 120.0,
    prio: int | None = None,
) -> int:
    ref = insert_gripe(store, title, status=status)
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="agent")
    # ``insert_ref`` re-stamps ``filed_by`` from the live filing origin
    # (``filing_origin.stamp_meta``), so the provenance under test is
    # written afterwards, as the creation's own stamp would be.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = %s, "
            "created_at = now() - make_interval(secs => %s), prio = %s "
            "WHERE ref_id = %s",
            (
                Jsonb(dict(_AGENT if meta is None else meta)),
                age_minutes * 60,
                prio,
                ref.id,
            ),
        )
    return int(ref.id)


def _light(store: Store, prio: int = 1, host: str = "*") -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES (%s, 'gripe_dispatch', %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (host, prio),
        )


def _roots_for(store: Store, gripe_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = 'related-to' AND t.kind = 'todo' "
            "ORDER BY 1",
            (gripe_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _comments(store: Store, gripe_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'gripe_comment' "
            "ORDER BY ord",
            (gripe_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


@pytest.fixture
def two_lanes(monkeypatch: pytest.MonkeyPatch) -> tuple[Lane, Lane]:
    codex = Lane(
        "codex", "codex_exec", "codex", 1, frozenset({"code"}), vendor="openai"
    )
    sub = Lane(
        "claude-sub",
        "claude_p",
        "sub",
        2,
        frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=("claude-sonnet-5",),
        model_prefix="claude-",
    )
    use_lanes(monkeypatch, codex, sub)
    return codex, sub


def test_dark_without_service_config(store: Store, two_lanes: Any) -> None:
    _gripe(store, "leaks")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert not gd.gripe_dispatch_enabled(store, host="h1")


def test_lane_capacity_sums_enabled_code_fit_caps(two_lanes: Any) -> None:
    codex, sub = two_lanes
    assert gd.lane_capacity(None) == 3
    off = Lane("devin", "devin_tree", "devin", 4, frozenset({"code"}), enabled=False)
    review_only = Lane("rev", "claude_p", "sub", 9, frozenset({"review"}))
    assert gd.lane_capacity(None, (codex, off, review_only)) == 1


def test_candidates_filter_and_order(store: Store, two_lanes: Any) -> None:
    hot = _gripe(store, "hot", prio=2)
    old = _gripe(store, "old", age_minutes=600)
    newer = _gripe(store, "newer", age_minutes=61)
    _gripe(store, "human", meta=_HUMAN, prio=1)
    _gripe(store, "too young", age_minutes=5)
    _gripe(store, "worker-filed", meta=_WORKER, prio=1)
    _gripe(store, "no provenance", meta={}, prio=1)
    _gripe(store, "not code", tags=("fix:docs",))
    _gripe(store, "held", tags=("fix:code", "hold:no-dispatch"))
    _gripe(store, "never-close", tags=("fix:code", "hold:no-autoclose"))
    _gripe(store, "reto's", tags=("fix:code", "fix:reto"))
    _gripe(store, "disputed", tags=("fix:code", "disagreement"))
    _gripe(store, "closed", status="done")
    _gripe(store, "fixing already", status="fixing")
    picks = gd.candidates(store, 10)
    titles = [c.title for c in picks]
    assert titles == ["human", "hot", "old", "newer"]
    assert [c.gripe_id for c in picks][1:] == [hot, old, newer]
    assert gd.candidates(store, 0) == []


def test_pass_mints_within_free_slots_and_is_idempotent(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    c = _gripe(store, "c", prio=3)
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # capacity 3, no held slots: one fire fills every free build slot
    assert (res.claimed, res.ok, res.failed) == (3, 3, 0)
    assert res.extra["slots"] == 3 and res.extra["lane_capacity"] == 3
    assert len(_roots_for(store, a)) == 1 and len(_roots_for(store, b)) == 1
    assert len(_roots_for(store, c)) == 1
    [note] = _comments(store, a)
    assert note.startswith(
        f"dispatch: minted gripe-fix-direct td{_roots_for(store, a)[0]}"
    )
    assert gd.open_chain_count(store) == 3

    # Second fire: all three are linked (skipped) and no slot is free.
    _gripe(store, "d", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and res.extra["slots"] == 0
    assert len(_roots_for(store, a)) == 1  # never doubled


def test_one_tick_fills_every_free_build_slot(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr584410: the per-tick cap is the free-slot count — devin 6 +
    claude-sub 3 with no live builds mints nine chains in one pass, and
    never more than the free slots."""
    use_lanes(
        monkeypatch,
        Lane(
            "devin",
            "devin_tree",
            "free-promo",
            6,
            frozenset({"code"}),
            vendor="cognition",
        ),
        Lane(
            "claude-sub",
            "claude_p",
            "sub",
            3,
            frozenset({ANY_CLASS}),
            vendor="anthropic",
            models=("claude-sonnet-5",),
            model_prefix="claude-",
        ),
    )
    _light(store)
    gripes = [_gripe(store, f"g{i}", prio=1) for i in range(12)]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (9, 9, 0)
    assert res.extra["slots"] == 9 and res.extra["lane_capacity"] == 9
    # equal prios → oldest first
    assert [m["gripe"] for m in res.extra["minted"]] == gripes[:9]
    assert gd.open_chain_count(store) == 9
    # no free slot left: the next fire mints nothing more
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and res.extra["slots"] == 0


def _build_node(store: Store, root_id: int) -> int:
    """The unclassed ``op`` child of a plan root — the chain's build."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE parent_id = %s "
            "AND meta ? 'op' AND meta->>'task_class' IS NULL",
            (root_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_landed_build_frees_the_slot_while_the_chain_runs_on(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr584410: a chain parked on the sha-in-prod gate, validate or the
    non-blocking review holds no build slot — the slot frees at land."""
    codex, _sub = two_lanes
    use_lanes(monkeypatch, codex)  # capacity 1
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    [root] = _roots_for(store, a)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full

    # the build lands: its node goes done while gate/validate/review
    # stay open — the chain runs on for hours yet holds no slot
    build = _build_node(store, root)
    store.add_tag(
        build, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, b)
    # a still has its (running-on) chain linked: not re-minted
    assert _roots_for(store, a) == [root]


def test_factory_off_mints_nothing(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _light(store)
    _gripe(store, "a")
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert res.extra["read_only"] == "PRECIS_FACTORY=off"
    with store.pool.connection() as conn:
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'related-to'"
        ).fetchone()
    assert n_links is not None and int(n_links[0]) == 0


@pytest.mark.parametrize("tag", gd.HOLD_TAGS)
def test_every_hold_tag_excludes(store: Store, two_lanes: Any, tag: str) -> None:
    _light(store)
    _gripe(store, "held", tags=("fix:code", tag))
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0)


def test_closed_chain_frees_its_slot_but_its_gripe_stays_out(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _sub = two_lanes
    use_lanes(monkeypatch, codex)  # capacity 1
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    [root] = _roots_for(store, a)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full
    store.add_tag(
        root, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, b)
    # a still has its (closed) chain linked: not re-minted
    assert _roots_for(store, a) == [root]


def test_hand_minted_chain_counts(store: Store, two_lanes: Any) -> None:
    from precis.plans.templates import mint_gripe_fix_direct

    _light(store)
    a = _gripe(store, "a")
    mint_gripe_fix_direct(store, gripe_id=a)
    assert gd.open_chain_count(store) == 1
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0 and len(_roots_for(store, a)) == 1


def test_enabled_resolves_per_host(store: Store) -> None:
    _light(store, prio=1, host="*")
    _light(store, prio=0, host="dim")
    assert gd.gripe_dispatch_enabled(store, host="lit")
    assert not gd.gripe_dispatch_enabled(store, host="dim")


def test_hold_tags_contain_cluster_dark_factory() -> None:
    # td574946 S1 (ruling 10): rescue-filed engine gripes and a gripe
    # rescued at intake stay out of the lane by the candidates tuple.
    assert "cluster:dark-factory" in gd.HOLD_TAGS
    assert "rescue:intake" in gd.HOLD_TAGS


def test_read_only_reason_mints_nothing(store: Store, two_lanes: Any) -> None:
    _light(store)
    _gripe(store, "a")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', 'plan_dispatch', 0)"
        )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert res.extra["read_only"]
    with store.pool.connection() as conn:
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'related-to'"
        ).fetchone()
    assert n_links is not None and int(n_links[0]) == 0


def test_three_failed_mints_rescue_the_gripe(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _light(store)
    g = _gripe(store, "always fails")
    calls: list[int] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("mint blew up")

    monkeypatch.setattr(gd, "mint_gripe_fix_direct", boom)
    for _ in range(3):
        res = gd.run_gripe_dispatch_pass(store, host="h1")
        assert (res.claimed, res.ok, res.failed) == (1, 0, 1)
    assert len(calls) == 3

    open_tags = [str(t.value) for t in store.tags_for(g) if t.namespace == "open"]
    assert "rescue:intake" in open_tags
    fails = [c for c in _comments(store, g) if c.startswith("dispatch: mint failed:")]
    assert len(fails) == 3
    assert any(c.startswith("rescue: intake") for c in _comments(store, g))

    # rescue:intake is a hold tag: the gripe leaves candidates, so a
    # fourth pass never reaches the mint
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0 and len(calls) == 3


def test_cadence_and_service_spec() -> None:
    from precis.workers.registry import SERVICES_BY_NAME, ServiceKind
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "gripe_dispatch")
    assert cad.interval_s == 10 * 60
    # No service= gate: a dark pass must still win its lease, else the
    # cadence-staleness check reads it as "stopped firing" (al567129).
    assert cad.service is None and cad.spends is False
    spec = SERVICES_BY_NAME["gripe_dispatch"]
    assert spec.kind is ServiceKind.PASS
    assert not spec.default_profiles and spec.ref_pass is False
    assert spec.uses_model is False


# ── the ci_watch door: meta.ci.sig + filed_by.principal = worker:ci_watch ──
#
# The door key is the write principal, never the agent-supplied client
# or actor: a stdio ``precis serve`` client binds no principal and falls
# through to ``worker:<PRECIS_CURRENT_SOURCE|cli>``, so only the
# ``job_inproc`` tick's own bound source lands ``worker:ci_watch`` —
# ``ci_watch.WORKER_PRINCIPAL``, the one constant both sides name.


def _age(store: Store, ref_id: int, minutes: float = 120.0) -> int:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (minutes * 60, ref_id),
        )
    return ref_id


def test_inproc_ci_watch_gripe_is_a_candidate(store: Store, two_lanes: Any) -> None:
    """A gripe written under the inproc tick context ``ci_watch`` with
    ``meta.ci.sig`` stamps ``filed_by.principal = worker:ci_watch`` and
    is a candidate."""
    from precis.utils.inproc_context import TickContext, tick_context
    from precis.workers.job_types import ci_watch

    with tick_context(TickContext(source=ci_watch.NAME)):
        ref = insert_gripe(
            store,
            "nightly red: ci:check.yml:tests/test_x.py::test_y",
            meta={"ci": {"sig": "ci:check.yml:tests/test_x.py::test_y"}},
        )
    meta = ref.meta or {}
    assert (meta.get("filed_by") or {}).get("principal") == (ci_watch.WORKER_PRINCIPAL)
    gid = int(ref.id)
    store.add_tag(gid, Tag.open("fix:code"), set_by="agent")
    _age(store, gid)
    assert gid in [c.gripe_id for c in gd.candidates(store, 10)]


def test_real_run_pass_stamps_the_door_principal(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a queued ``ci_watch`` job claimed by
    ``run_job_inproc_pass`` files a gripe whose stored
    ``meta.filed_by.principal`` equals ``ci_watch.WORKER_PRINCIPAL`` —
    the executor's tick binding is what the dispatch door keys on — and
    the dispatch SQL admits it."""
    from precis.workers.executors import job_inproc
    from precis.workers.job_types import ci_watch

    def gh_json(endpoint: str) -> Any:
        endpoints = {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": [
                    {
                        "id": 201,
                        "status": "completed",
                        "conclusion": "failure",
                        "event": "schedule",
                        "head_branch": "main",
                        "head_sha": "d" * 40,
                        "html_url": "https://github.com/o/r/actions/runs/201",
                        "updated_at": "2026-11-07T03:10:00Z",
                    }
                ]
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": []
            },
            "repos/o/r/actions/runs/201/jobs": {
                "jobs": [
                    {
                        "id": 9001,
                        "name": "pytest",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            },
        }
        if endpoint not in endpoints:
            raise AssertionError(f"unstubbed gh endpoint: {endpoint}")
        return endpoints[endpoint]

    monkeypatch.setattr(ci_watch, "_gh_json", gh_json)
    monkeypatch.setattr(
        ci_watch,
        "_gh_log",
        lambda repo, job_id: (
            "2026-11-07T03:00:01.0000000Z FAILED "
            "tests/test_x.py::test_y - assert 1 == 2\n"
        ),
    )
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="ci_watch tick",
        meta={
            "executor": "job_inproc",
            "job_type": ci_watch.NAME,
            "params": {"repo": "o/r"},
        },
    )
    store.add_tag(job.id, Tag.closed("STATUS", "queued"), set_by="agent")
    assert job_inproc.run_job_inproc_pass(store, limit=1)["ok"] == 1

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'gripe'"
        ).fetchall()
    assert len(rows) == 1
    gid, meta = int(rows[0][0]), dict(rows[0][1] or {})
    assert (meta.get("ci") or {}).get("sig") == ("ci:check.yml:tests/test_x.py::test_y")
    assert (meta.get("filed_by") or {}).get("principal") == (ci_watch.WORKER_PRINCIPAL)
    _age(store, gid)
    assert gid in [c.gripe_id for c in gd.candidates(store, 10)]


def test_mcp_put_handler_cannot_forge_the_ci_door(
    store: Store, hub: Any, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gripe put HANDLER swallows ``meta=`` (``accepts_put_meta`` is
    False for kind=gripe): a caller-supplied ``meta.ci`` /
    ``meta.filed_by`` never reaches the ref — no ``ci.sig``, and
    ``filed_by.principal`` is the server stamp. This test is the red a
    future handler must hit if it starts accepting ``put_meta`` — the
    ci_watch door key would then be re-ruled against that surface, not
    silently widened."""
    from precis.handlers.gripe import GripeHandler
    from precis.workers.job_types import ci_watch

    monkeypatch.delenv("PRECIS_CURRENT_SOURCE", raising=False)
    GripeHandler(hub=hub).put(
        text="forged ci gripe — never a candidate",
        meta={
            "filed_by": {"principal": ci_watch.WORKER_PRINCIPAL},
            "ci": {"sig": "ci:check.yml:tests/x.py::t"},
        },
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'gripe' "
            "ORDER BY ref_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    gid, meta = int(row[0]), dict(row[1] or {})
    assert meta.get("ci") is None
    assert (meta.get("filed_by") or {}).get("principal") != (ci_watch.WORKER_PRINCIPAL)
    store.add_tag(gid, Tag.open("fix:code"), set_by="agent")
    _age(store, gid)
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]


def test_worker_cli_with_ci_sig_is_not_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    """A ``worker:cli``-stamped gripe carrying ``meta.ci.sig`` is NOT a
    candidate — the old ``actor = 'agent'`` clause is gone: the
    agent-declared actor no longer opens the door."""
    from precis.workers.job_types import ci_watch

    gid = _gripe(
        store,
        "nightly red: ci:check.yml:tests/test_x.py::test_y",
        meta={
            "ci": {"sig": "ci:check.yml:tests/test_x.py::test_y"},
            "filed_by": {"actor": "agent", "principal": "worker:cli"},
        },
    )
    ref = store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    meta = ref.meta or {}
    assert meta["ci"]["sig"] == "ci:check.yml:tests/test_x.py::test_y"
    assert meta["filed_by"]["principal"] != ci_watch.WORKER_PRINCIPAL
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]


def test_worker_gripe_without_ci_sig_is_not_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    gid = _gripe(store, "worker-filed gripe", meta=_WORKER)
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]
