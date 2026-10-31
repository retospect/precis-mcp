"""``gripe_validate`` job type (td542754): the DB-only twin of
scripts/gripe-validate. A ``STATUS:verify`` gripe whose ``resolved: <sha>``
names a sha in the ``release.prod_head`` record (head or ``landed``) goes
``STATUS:done`` through the handler; no sha / not deployed stays."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.handlers.gripe import GripeHandler
from precis.plans import gates
from precis.store import Store
from precis.store.types import Tag
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import gripe_validate as gv

HEAD = "a" * 40
LANDED = ["b" * 40, "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1"]

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


def _record(store: Store, head: str = HEAD, landed: list[str] | None = None) -> None:
    rec = {
        "head": head,
        "deployed_at": "2026-10-30T12:00:00Z",
        "landed": LANDED if landed is None else landed,
    }
    psettings.set_setting(gates.PROD_HEAD_KEY, json.dumps(rec), store=store)


def _status(store: Store, gid: int) -> str:
    vals = [str(t) for t in store.tags_for(gid) if str(t).startswith("STATUS:")]
    assert len(vals) == 1, vals
    return vals[0].split(":", 1)[1]


def _verify_gripe(hub: Any, comment: str | None) -> int:
    h = GripeHandler(hub=hub)
    h.put(text=f"validate probe {comment}")
    gid = int(hub.store.list_refs(kind="gripe", limit=1)[0].id)
    if comment is not None:
        h.put(id=gid, text=comment)
    hub.store.add_tag(
        gid, Tag.closed("STATUS", "verify"), replace_prefix=True, set_by="agent"
    )
    return gid


def test_registry_and_known_types() -> None:
    spec = get_job_type("gripe_validate")
    assert spec is not None
    assert spec.dedupe_pending is True
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "gripe_validate" in known_job_types()


def test_sha_live_matches_head_and_landed_by_prefix() -> None:
    rec = {"head": HEAD, "landed": LANDED}
    assert gv.sha_live(rec, HEAD[:9])
    assert gv.sha_live(rec, "B" * 12)
    assert gv.sha_live(rec, LANDED[1])
    assert not gv.sha_live(rec, "d" * 12)
    assert not gv.sha_live(rec, "aaaaaa")  # below the 7-char minimum


def test_live_gripes_close_and_the_rest_stay(hub: Any) -> None:
    store = hub.store
    _record(store)
    on_head = _verify_gripe(hub, f"resolved: {HEAD[:9]} (main, release pending)")
    landed = _verify_gripe(hub, f"resolved: {LANDED[0][:12]} (main, release pending)")
    pending = _verify_gripe(hub, "resolved: " + "d" * 9 + " (main, release pending)")
    no_sha = _verify_gripe(hub, "resolved: observation")
    res = gv.run_pass(store)
    assert (res.verify, len(res.live), len(res.pending), len(res.no_sha)) == (
        4,
        2,
        1,
        1,
    )
    assert _status(store, on_head) == "done"
    assert _status(store, landed) == "done"
    assert _status(store, pending) == "verify"
    assert _status(store, no_sha) == "verify"
    closer = [
        c.text
        for c in store.chunks.list_chunks_for_ref(on_head)
        if c.chunk_kind == "gripe_comment"
    ][-1]
    assert closer.startswith(f"resolved: {HEAD[:9]} (on prod {HEAD[:9]})")
    assert "done gr" in res.closed
    # Idempotent: the closed ones are no longer listed.
    again = gv.run_pass(store)
    assert (again.verify, len(again.live)) == (2, 0)


def test_dry_run_lists_without_writing(hub: Any) -> None:
    store = hub.store
    _record(store)
    gid = _verify_gripe(hub, f"resolved: {HEAD[:9]} (main, release pending)")
    res = gv.run_pass(store, dry_run=True)
    assert res.live == [(gid, HEAD[:9])]
    assert res.closed == ""
    assert _status(store, gid) == "verify"
    assert "would flip verify → done" in res.render()


def test_no_recorded_head_closes_nothing(hub: Any) -> None:
    store = hub.store
    gid = _verify_gripe(hub, f"resolved: {HEAD[:9]} (main, release pending)")
    res = gv.run_pass(store)
    assert res.head is None
    assert res.live == [] and len(res.pending) == 1
    assert _status(store, gid) == "verify"
    assert "none recorded" in res.summary()


def test_dispatch_writes_meta_and_summary(hub: Any) -> None:
    store = hub.store
    _record(store)
    gid = _verify_gripe(hub, f"resolved: {HEAD[:9]} (main, release pending)")
    meta_seen: dict[str, Any] = {}
    chunks: list[tuple[str, str]] = []
    ctx = SimpleNamespace(
        store=store,
        meta={"params": {}},
        set_meta=lambda **kw: meta_seen.update(kw),
        append_chunk=lambda kind, text: chunks.append((kind, text)),
        record_failure=lambda msg: pytest.fail(msg),
    )
    gv._dispatch(ctx, gv.SPEC)
    assert meta_seen["gripe_validate"]["live"] == 1
    assert chunks[0][0] == "job_summary"
    assert chunks[0][1].startswith("gripe_validate (prod head aaaaaaaaa): verify=1")
    assert _status(store, gid) == "done"
