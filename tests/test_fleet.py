"""kind='fleet': report ingestion diffing, coordinator fields, fisheye, GC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis import fleet
from precis.dispatch import boot
from precis.errors import BadInput
from precis.handlers.fleet import FleetHandler, _age_min, _window_label
from precis.store import Store

HOST = "melchior"
KEY = f"claude:{HOST}:proj:main"


@pytest.fixture
def h(store: Store) -> FleetHandler:
    hub = boot(store=store)
    handler = hub.handlers["fleet"]
    assert isinstance(handler, FleetHandler)
    return handler


def iso(minutes_ago: float = 0.0) -> str:
    t = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def row(tree: str = "main", vendor: str = "claude", **kw: Any) -> dict[str, Any]:
    base = {
        "vendor": vendor,
        "host": HOST,
        "project": "proj",
        "tree": tree,
        "branch": "main",
        "dirty": 0,
        "ahead": 0,
        "behind": 0,
        "purpose": "build the thing",
        "state": "working",
        "quiet_min": 1.0,
        "last_active": iso(1),
        "ctx_pct": 20.0,
        "tmux": {"pane_id": "%7", "session": "main", "window": "3"},
        "attach": "tmux attach -t main:3",
    }
    base.update(kw)
    return base


def rep(rows: list[dict[str, Any]], host: str = HOST, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "host": host,
        "version": 1,
        "generated": iso(),
        "rows": rows,
        "exceptions": [],
        "quota": {},
    }
    out.update(kw)
    return out


def send(h: FleetHandler, report: dict[str, Any]) -> str:
    return h.put(mode="report", host=report["host"], report=report).body


def agent(h: FleetHandler, key: str):
    ref = h.store.find_ref_by_meta(kind="fleet", key="key", value=key)
    assert ref is not None, key
    return ref


def test_first_report_inserts_agent_and_host_rows(h: FleetHandler) -> None:
    out = send(h, rep([row()], quota={"codex": {"primary": {"used_percent": 12.0}}}))
    assert "1 new" in out
    m = agent(h, KEY).meta
    assert (m["type"], m["state"], m["pane"], m["ctx_pct"]) == (
        "agent",
        "working",
        "%7",
        20.0,
    )
    assert m["exceptions"] == []
    hm = agent(h, f"host:{HOST}").meta
    assert hm["type"] == "host" and hm["reporter_version"] == 1
    assert hm["quota"]["codex"]["primary"]["used_percent"] == 12.0


def test_unchanged_report_writes_no_agent_row(h: FleetHandler) -> None:
    r = rep([row()])
    send(h, r)
    before = agent(h, KEY)
    out = send(h, r)
    assert "0 updated, 1 unchanged" in out
    assert agent(h, KEY).updated_at == before.updated_at


def test_only_changed_fields_are_patched(h: FleetHandler) -> None:
    send(h, rep([row()]))
    ref = agent(h, KEY)
    # An out-of-band value on a field the next report reports as "main": the
    # report changes state/ctx_pct and sets branch back, proving each field is
    # compared individually.
    h.store.update_ref(ref.id, meta_patch={"branch": "sentinel"})
    out = send(h, rep([row(state="idle", ctx_pct=33.0, branch="sentinel")]))
    assert "1 updated" in out
    m = agent(h, KEY).meta
    assert (m["state"], m["ctx_pct"], m["branch"]) == ("idle", 33.0, "sentinel")


def test_vanished_pane_and_attach_are_cleared_with_null(h: FleetHandler) -> None:
    send(h, rep([row()]))
    send(h, rep([row(tmux=None, attach=None)]))
    m = agent(h, KEY).meta
    assert m["pane"] is None and m["attach"] is None


def test_vanished_agent_goes_dead_and_revives(h: FleetHandler) -> None:
    other = f"claude:{HOST}:proj:other"
    send(h, rep([row(), row(tree="other")]))
    out = send(h, rep([row()]))
    assert "1 marked dead" in out
    assert agent(h, other).meta["state"] == "dead"
    assert agent(h, KEY).meta["state"] == "working"
    assert "0 marked dead" in send(h, rep([row()]))
    send(h, rep([row(), row(tree="other", state="idle")]))
    assert agent(h, other).meta["state"] == "idle"


def test_other_hosts_rows_are_not_marked_dead(h: FleetHandler) -> None:
    send(h, rep([row()]))
    send(h, rep([row(host="balthasar")], host="balthasar"))
    assert agent(h, KEY).meta["state"] == "working"
    assert agent(h, "claude:balthasar:proj:main").meta["state"] == "working"


def test_same_vendor_tree_sessions_fold_newest_wins(h: FleetHandler) -> None:
    send(
        h,
        rep(
            [
                row(state="idle", last_active=iso(30), ctx_pct=90.0),
                row(state="working", last_active=iso(1), ctx_pct=10.0),
            ]
        ),
    )
    m = agent(h, KEY).meta
    assert (m["state"], m["ctx_pct"]) == ("working", 10.0)
    assert h.store.count_refs(kind="fleet") == 2  # one agent + the host row


def test_fold_keeps_newest_whatever_the_row_order(h: FleetHandler) -> None:
    send(
        h,
        rep(
            [
                row(state="working", last_active=iso(1)),
                row(state="idle", last_active=iso(30)),
            ]
        ),
    )
    assert agent(h, KEY).meta["state"] == "working"


def test_age_minutes_and_quota_window_labels() -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert _age_min("2026-10-09T11:00:00Z", now) == 60.0
    assert _age_min("", now) is None
    assert _age_min(None, now) is None
    assert _age_min("not a time", now) is None
    assert [_window_label(m) for m in (None, 300, 360, 361, 9999, 10000)] == [
        "?",
        "5h",
        "5h",
        "6h",
        "167h",
        "week",
    ]


def test_vendors_share_a_tree_with_separate_rows(h: FleetHandler) -> None:
    send(h, rep([row(), row(vendor="codex")]))
    agent(h, KEY)
    agent(h, f"codex:{HOST}:proj:main")


def test_exceptions_become_codes(h: FleetHandler) -> None:
    who = f"claude@{HOST}/proj/main"
    send(
        h,
        rep(
            [row(state="waiting")],
            exceptions=[
                {"who": who, "reason": "waiting on approval", "attach": None},
                {"who": who, "reason": "context 71%", "attach": None},
            ],
        ),
    )
    assert agent(h, KEY).meta["exceptions"] == ["context", "waiting"]


def test_coordinator_fields_survive_reports(h: FleetHandler) -> None:
    send(h, rep([row()]))
    ref = agent(h, KEY)
    h.edit(id=ref.id, assigned="gr12345", slice="docs/", note="blocked")
    send(h, rep([row(state="idle")]))
    m = agent(h, KEY).meta
    assert (m["assigned"], m["slice"], m["note"], m["state"]) == (
        "gr12345",
        "docs/",
        "blocked",
        "idle",
    )
    h.edit(id=KEY, note="")
    assert agent(h, KEY).meta["note"] is None


def test_put_without_report_mode_is_refused(h: FleetHandler) -> None:
    with pytest.raises(BadInput, match="mode='report'"):
        h.put(text="a fleet row")
    with pytest.raises(BadInput, match="mode='report'"):
        h.put(mode="create", text="x")
    with pytest.raises(BadInput, match="report="):
        h.put(mode="report", host=HOST)
    assert h.store.count_refs(kind="fleet") == 0


def test_edit_of_reporter_owned_field_is_refused(h: FleetHandler) -> None:
    send(h, rep([row()]))
    with pytest.raises(BadInput, match="reporter-owned"):
        h.edit(id=KEY, state="idle")
    with pytest.raises(BadInput, match="at least one"):
        h.edit(id=KEY)
    with pytest.raises(BadInput, match="not an agent row"):
        h.edit(id=f"host:{HOST}", note="x")
    assert agent(h, KEY).meta["state"] == "working"


def test_get_one_row_by_key_and_id(h: FleetHandler) -> None:
    send(h, rep([row()]))
    ref = agent(h, KEY)
    by_key = h.get(id=KEY).body
    assert KEY in by_key and "state: working" in by_key
    assert KEY in h.get(id=ref.id).body


def test_fisheye_blocks_and_filters(h: FleetHandler) -> None:
    who = f"claude@{HOST}/proj/main"
    send(
        h,
        rep(
            [
                row(state="asking"),
                row(tree="side", project="other", purpose="x" * 80, ctx_pct=None),
            ],
            exceptions=[
                {
                    "who": who,
                    "reason": "question for Reto",
                    "attach": "tmux attach -t m:1",
                }
            ],
            quota={
                "codex": {
                    "primary": {"used_percent": 12.0, "window_minutes": 300},
                    "secondary": {"used_percent": 40.0, "window_minutes": 10080},
                }
            },
        ),
    )
    out = h.get().body
    lines = out.splitlines()
    assert lines[0] == "EXCEPTIONS"
    assert f"  {who}  asking   attach: tmux attach -t main:3" in lines
    assert f"  {who} · asking · 1m · build the thing · 20%" in lines
    assert f"  claude@{HOST}/other/side · working · 1m · {'x' * 40} · -" in lines
    assert "QUOTA   codex 5h 12% · codex week 40%" in lines
    assert "STALE" not in out
    only = h.get(project="other").body
    assert "proj/main" not in only and "other/side" in only
    assert "proj/main" not in h.get(host="nowhere").body


def test_stale_host_marks_agents_and_lists_exception(h: FleetHandler) -> None:
    send(h, rep([row()]))
    hm = agent(h, f"host:{HOST}")
    old = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h.store.update_ref(hm.id, meta_patch={"reported_at": old})
    out = h.get().body
    assert "· working · STALE ·" in out
    assert f"host {HOST}  stale (last report 5m ago)" in out


def test_fresh_host_is_not_stale_at_two_minutes(h: FleetHandler) -> None:
    send(h, rep([row()]))
    hm = agent(h, f"host:{HOST}")
    t = (datetime.now(UTC) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h.store.update_ref(hm.id, meta_patch={"reported_at": t})
    assert "STALE" not in h.get().body


def test_gc_retires_only_old_dead_rows(h: FleetHandler) -> None:
    send(h, rep([row(), row(tree="gone"), row(tree="fresh-dead")]))
    send(h, rep([row()]))  # gone + fresh-dead -> dead
    gone = agent(h, f"claude:{HOST}:proj:gone")
    with h.store.tx() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '25 hours' WHERE ref_id = %s",
            (gone.id,),
        )
    assert fleet.gc_dead_rows(h.store) == 1
    assert (
        h.store.find_ref_by_meta(
            kind="fleet", key="key", value="claude:melchior:proj:gone"
        )
        is None
    )
    agent(h, f"claude:{HOST}:proj:fresh-dead")
    agent(h, KEY)
    assert fleet.gc_dead_rows(h.store) == 0


def test_vanished_agent_drops_exceptions_and_attach(h: FleetHandler) -> None:
    who = f"claude@{HOST}/proj/other"
    send(
        h,
        rep(
            [row(), row(tree="other", state="waiting")],
            exceptions=[{"who": who, "reason": "waiting on approval", "attach": None}],
        ),
    )
    send(h, rep([row()]))
    m = agent(h, f"claude:{HOST}:proj:other").meta
    assert m["state"] == "dead"
    assert m["exceptions"] == []
    assert m.get("pane") is None and m.get("attach") is None


def test_host_arg_must_match_report_host(h: FleetHandler) -> None:
    with pytest.raises(BadInput, match="report is from"):
        h.put(mode="report", host="balthasar", report=rep([row()]))


def test_gc_retires_rows_of_a_silent_host(h: FleetHandler) -> None:
    send(h, rep([row()]))
    send(h, rep([row(host="balthasar")], host="balthasar"))
    host_row = agent(h, "host:balthasar")
    with h.store.tx() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '25 hours' WHERE ref_id = %s",
            (host_row.id,),
        )
    assert fleet.gc_dead_rows(h.store) == 2  # balthasar's agent row + host row
    assert (
        h.store.find_ref_by_meta(kind="fleet", key="key", value="host:balthasar")
        is None
    )
    agent(h, KEY)
    agent(h, f"host:{HOST}")
