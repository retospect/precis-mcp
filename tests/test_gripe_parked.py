"""``STATUS:parked`` (gr543711): parking needs ``meta.until``; the
``gripe_parked`` pass flips met conditions back to ``open`` with one
comment. /open and the live cohort hide parked gripes; /parked lists
them with condition and parked age."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.plans import gates
from precis.store import Store
from precis.store.types import Tag
from precis.utils.parked import describe_until, until_shorthand
from precis.workers import gripe_parked as gp

pytestmark = pytest.mark.db

HEAD = "a" * 40


def _gripe(hub: Any, text: str = "parked probe") -> int:
    h = GripeHandler(hub=hub)
    h.put(text=text)
    return int(hub.store.list_refs(kind="gripe", limit=1, order_by="id_desc")[0].id)


def _status(store: Store, gid: int) -> str:
    vals = [str(t) for t in store.tags_for(gid) if str(t).startswith("STATUS:")]
    assert len(vals) == 1, vals
    return vals[0].split(":", 1)[1]


def _park(hub: Any, gid: int, until: Any) -> None:
    h = GripeHandler(hub=hub)
    h.tag(id=gid, add=["STATUS:parked"], meta={"until": until})


def test_park_without_until_refused(hub: Any) -> None:
    gid = _gripe(hub)
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput):
        h.tag(id=gid, add=["STATUS:parked"])
    with pytest.raises(BadInput):
        h.put(id=gid, tags=["STATUS:parked"])
    assert _status(hub.store, gid) == "open"


def test_shorthand_parses_to_types(hub: Any) -> None:
    dep = _gripe(hub)
    assert until_shorthand(f"gr{dep}") == {"type": "ref_done", "value": f"gr{dep}"}
    assert until_shorthand("td7") == {"type": "ref_done", "value": "td7"}
    assert until_shorthand("a1b2c3d4e") == {
        "type": "sha_in_prod",
        "value": "a1b2c3d4e",
    }
    assert until_shorthand("2026-12-01") == {"type": "date", "value": "2026-12-01"}
    assert until_shorthand("when the fleet is green") == {
        "type": "text",
        "value": "when the fleet is green",
    }


def test_park_stamps_meta_until_and_unpark_clears(hub: Any) -> None:
    dep = _gripe(hub)
    gid = _gripe(hub)
    h = GripeHandler(hub=hub)
    _park(hub, gid, {"type": "ref_done", "value": f"gr{dep}", "note": "waits"})
    ref = hub.store.get_ref(kind="gripe", id=gid)
    until = ref.meta["until"]
    assert until["type"] == "ref_done" and until["value"] == f"gr{dep}"
    assert until["note"] == "waits" and "parked_at" in until
    h.tag(id=gid, add=["STATUS:open"])
    assert "until" not in (hub.store.get_ref(kind="gripe", id=gid).meta or {})


def test_wake_met_flips_status_and_comments(hub: Any) -> None:
    store = hub.store
    dep = _gripe(hub)
    gid = _gripe(hub)
    _park(hub, gid, {"type": "ref_done", "value": f"gr{dep}"})
    res = gp.run_gripe_parked_pass(store)
    assert res.woke == [] and _status(store, gid) == "parked"
    comments_before = sum(
        1
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    )
    with store.tx() as conn:
        store.add_tag(
            dep,
            Tag.closed("STATUS", "done"),
            set_by="agent",
            replace_prefix=True,
            conn=conn,
        )
    res = gp.run_gripe_parked_pass(store)
    assert res.woke == [gid]
    assert _status(store, gid) == "open"
    comments = [
        c.text
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]
    assert len(comments) == comments_before + 1
    assert comments[-1].startswith(f"unparked: ref_done gr{dep} met")


def test_wake_not_met_writes_nothing(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _park(hub, gid, {"type": "date", "value": "2999-01-01"})
    before = len(store.chunks.list_chunks_for_ref(gid))
    res = gp.run_gripe_parked_pass(store)
    assert res.woke == [] and res.waiting == 1
    assert _status(store, gid) == "parked"
    assert len(store.chunks.list_chunks_for_ref(gid)) == before


def test_date_met(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _park(hub, gid, "2001-01-01")
    res = gp.run_gripe_parked_pass(store)
    assert res.woke == [gid] and _status(store, gid) == "open"


def test_sha_in_prod(hub: Any) -> None:
    store = hub.store
    psettings.invalidate()
    psettings.set_setting(
        gates.PROD_HEAD_KEY,
        json.dumps({"head": HEAD, "deployed_at": "x", "landed": []}),
        store=store,
    )
    gid = _gripe(hub)
    _park(hub, gid, {"type": "sha_in_prod", "value": HEAD[:12]})
    res = gp.run_gripe_parked_pass(store)
    assert res.woke == [gid] and _status(store, gid) == "open"


def test_text_with_stubbed_llm(hub: Any) -> None:
    store = hub.store
    gid = _gripe(hub)
    _park(hub, gid, {"type": "text", "value": "the fleet is green"})
    unsure = SimpleNamespace(text="unsure")
    res = gp.run_gripe_parked_pass(store, llm=lambda p: unsure)
    assert res.woke == [] and _status(store, gid) == "parked"
    met = SimpleNamespace(text="met")
    res = gp.run_gripe_parked_pass(store, llm=lambda p: met)
    assert res.woke == [gid] and _status(store, gid) == "open"


def test_open_excludes_and_parked_lists(hub: Any) -> None:
    h = GripeHandler(hub=hub)
    live = _gripe(hub)
    gid = _gripe(hub)
    _park(hub, gid, {"type": "date", "value": "2999-01-01"})
    body = h.get(id="/open").body
    assert f"gr{live}" in body and f"gr{gid}" not in body
    parked = h.get(id="/parked").body
    assert f"gr{gid}" in parked
    assert "until date 2999-01-01" in parked and "parked" in parked


def test_describe_until() -> None:
    assert describe_until({"type": "ref_done", "value": "gr1"}) == "ref_done gr1"
    assert (
        describe_until({"type": "sha_in_prod", "value": "a" * 20})
        == "sha_in_prod aaaaaaaaa"
    )
    assert (
        describe_until({"type": "text", "value": "x", "note": "why"}) == "text x (why)"
    )
