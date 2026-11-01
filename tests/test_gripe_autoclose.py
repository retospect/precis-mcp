"""gr519676: the gripe auto-close prod check (workers/gripe_autoclose.py).

A live gripe carrying ``meta.fix.landed`` + ``meta.fix.check`` is
evaluated once its sha is in the recorded prod set: pass →
``resolved:`` + ``STATUS:done``; fail → ``STATUS:done`` plus a linked
``derived-from`` triage gripe. No check line or ``hold:no-autoclose``
→ untouched; the pass is dark until ``service_config`` lights it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis import settings as psettings
from precis.errors import BadInput
from precis.plans import gates
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.gripe_autoclose import (
    HOLD_TAG,
    SERVICE_NAME,
    CheckError,
    CountCheck,
    ReadCheck,
    parse_check,
    run_gripe_autoclose_pass,
)
from precis.workers.scheduler import CADENCES
from precis.workers.service_config import set_service_prio
from tests._refs import insert_gripe

HEAD = "a" * 40
SHA = "b1b2b3b4b5b6b7b8b9b0b1b2b3b4b5b6b7b8b9b0"


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


def _record(store: Store, *, landed: list[str] | None = None) -> None:
    rec = {
        "head": HEAD,
        "deployed_at": "2026-10-31T12:00:00Z",
        "landed": [SHA[:12]] if landed is None else landed,
        "release": "r42",
    }
    psettings.set_setting(gates.PROD_HEAD_KEY, json.dumps(rec), store=store)
    psettings.invalidate(gates.PROD_HEAD_KEY)


def _enable(store: Store) -> None:
    set_service_prio(store, "*", SERVICE_NAME, 1, actor="test")


def _gripe(
    store: Store,
    *,
    check: str | None = 'read gripe %d contains "needle"',
    held: bool = False,
    landed: str | None = SHA,
) -> int:
    fix: dict[str, Any] = {}
    if landed:
        fix["landed"] = landed
    ref = insert_gripe(
        store, "Area: the thing is broken", status="in_review", meta={"fix": fix}
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="the needle body")])
    gid = int(ref.id)
    if check:
        # update_ref's meta_patch is a shallow jsonb merge — rewrite the
        # whole ``fix`` key or ``landed`` would drop out.
        line = check % gid if "%d" in check else check
        store.update_ref(gid, meta_patch={"fix": {**fix, "check": line}})
    if held:
        store.add_tag(gid, Tag.open(HOLD_TAG), set_by="agent")
    return gid


def _status(store: Store, gid: int) -> str | None:
    vals = [str(t) for t in store.tags_for(gid) if str(t).startswith("STATUS:")]
    return vals[0].split(":", 1)[1] if vals else None


def _comments(store: Store, gid: int) -> list[str]:
    return [
        c.text
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


def _gripes(store: Store) -> list[int]:
    return [
        int(r.id) for r in store.list_refs(kind="gripe", limit=50) if r.id is not None
    ]


# ── the check grammar ────────────────────────────────────────────────


def test_parse_check_forms() -> None:
    c = parse_check('read gripe 12 contains "two words"')
    assert isinstance(c, ReadCheck)
    assert c.ref_id == 12 and c.needle == "two words" and c.cmp is None
    c = parse_check("read todo 3 count done >= 2")
    assert isinstance(c, ReadCheck)
    assert c.cmp == ">=" and c.n == 2 and c.needle == "done"
    c = parse_check(
        'count job with tag=STATUS:succeeded and meta.job_type="gripe_validate" >= 1'
    )
    assert isinstance(c, CountCheck)
    assert c.kind == "job"
    assert c.tags == ("STATUS:succeeded",)
    assert c.meta_eq == (("job_type", "gripe_validate"),)
    assert (c.cmp, c.n) == (">=", 1)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "delete refs where kind='gripe'",
        "read gripe x contains foo",
        "read gripe 1",
        "count job",
        "count job with tag= >= 1",
        "count job with tag=STATUS:done meta.a=b >= 1",
        "count job with meta.Bad Path=x >= 1",
        "count job =~ 1",
    ],
)
def test_parse_check_refuses(line: str) -> None:
    with pytest.raises((CheckError, BadInput)):
        parse_check(line)


# ── the pass ─────────────────────────────────────────────────────────


def test_dark_until_service_config_lights(store: Store) -> None:
    _record(store)
    gid = _gripe(store)
    r = run_gripe_autoclose_pass(store)
    assert r == {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}
    assert _status(store, gid) == "in_review"
    _enable(store)
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1
    assert _status(store, gid) == "done"


def test_pass_closes_with_resolved_comment(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store)
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 1 and r["ok"] == 1 and r["failed"] == 0
    assert _status(store, gid) == "done"
    newest = _comments(store, gid)[-1]
    assert newest.startswith(f"resolved: {SHA} (r42)")
    assert "prod check passed" in newest


def test_fail_closes_and_refiles_linked_triage_gripe(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, check='read gripe %d contains "not-present"')
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1 and r["ok"] == 0
    assert _status(store, gid) == "done"
    others = [g for g in _gripes(store) if g != gid]
    assert len(others) == 1
    new = others[0]
    assert _status(store, new) == "triaged"
    # the title is a derived cut; the ruling's "first line" is the body's
    first = store.chunks.list_chunks_for_ref(new)[0].text.split("\n", 1)[0]
    assert SHA in first and f"gr{gid}" in first and "failed its prod check" in first
    with store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (new, gid),
        ).fetchone()
    assert edge is not None and edge[0] == "derived-from"
    newest = _comments(store, gid)[-1]
    assert "prod check FAILED" in newest and f"gr{new}" in newest


def test_no_check_line_is_untouched(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, check=None)
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"
    assert _comments(store, gid) == []


def test_hold_no_autoclose_is_untouched(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store, held=True)
    r = run_gripe_autoclose_pass(store)
    assert r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_not_yet_deployed_waits(store: Store) -> None:
    _enable(store)
    _record(store, landed=["d" * 12])  # SHA not in the prod set
    gid = _gripe(store)
    r = run_gripe_autoclose_pass(store)
    assert r["pending"] == 1 and r["claimed"] == 0
    assert _status(store, gid) == "in_review"


def test_rerun_is_a_noop(store: Store) -> None:
    _enable(store)
    _record(store)
    gid = _gripe(store)
    r1 = run_gripe_autoclose_pass(store)
    assert r1["ok"] == 1
    r2 = run_gripe_autoclose_pass(store)
    assert r2 == {"claimed": 0, "ok": 0, "failed": 0, "pending": 0, "skipped": 0}
    assert len(_comments(store, gid)) == 1


def test_count_check_and_malformed_check(store: Store) -> None:
    _enable(store)
    _record(store)
    ok_gid = _gripe(store, check="count gripe with tag=STATUS:in_review >= 1")
    bad_gid = _gripe(store, check="frobnicate the refs")
    r = run_gripe_autoclose_pass(store)
    assert r["ok"] == 1 and r["skipped"] == 1
    assert _status(store, ok_gid) == "done"
    assert _status(store, bad_gid) == "in_review"


def test_chain_cap_marks_third_refile_held(store: Store) -> None:
    """The refiled gripe of a third consecutive failure on one root
    carries ``hold:no-autoclose`` — the chain hands off to a human."""
    _enable(store)
    _record(store)
    root = _gripe(store, check='read gripe %d contains "absent-1"')
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    mid = next(g for g in _gripes(store) if g != root)
    # mid's own fix lands and fails too — its refile is still plain.
    store.update_ref(
        mid,
        meta_patch={
            "fix": {"landed": SHA, "check": f'read gripe {mid} contains "absent-2"'}
        },
    )
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    third = next(g for g in _gripes(store) if g not in (root, mid))
    assert HOLD_TAG not in {str(t) for t in store.tags_for(third)}
    # third's fix failing makes the chain three deep — the refiled
    # fourth gripe is held out of any further auto-close.
    store.update_ref(
        third,
        meta_patch={
            "fix": {"landed": SHA, "check": f'read gripe {third} contains "absent-3"'}
        },
    )
    r = run_gripe_autoclose_pass(store)
    assert r["failed"] == 1
    fourth = next(g for g in _gripes(store) if g not in (root, mid, third))
    assert HOLD_TAG in {str(t) for t in store.tags_for(fourth)}


def test_cadence_registered_dark_safe(store: Store) -> None:
    cad = next(c for c in CADENCES if c.name == "gripe_autoclose")
    assert 3600 <= cad.interval_s < 2 * 3600
    assert cad.service is None  # gate lives inside the pass, not the lease
    assert not cad.spends
