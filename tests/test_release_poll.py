"""``release_poll`` (td573430): the pass mints one ``release-pass`` plan
only when GitHub shows main ahead of prod with nothing prod-only, no
``release-pass``-tagged todo is open, no ``release.round`` record is open,
no ``release/r<N>`` head is in flight and the clock is outside
``release.no_release_windows``; else it writes one blocked line. GitHub is
faked at ``pr_class._http_get`` (the seam): no test calls the network."""

from __future__ import annotations

import json
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import lanes as L
from precis.plans import pr_class as P
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers import factory_git as _fg
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import release_poll as rp
from tests._lane_rows import use_lanes
from tests._script import load_script

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO = "o/r"
API = f"https://api.github.com/repos/{REPO}"
#: A Tuesday noon UTC — outside every window these tests set.
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
HEAD_SHA = "a" * 40
MAIN_SHA = "d" * 40


#: The fake ``origin/prod..<pin>`` range the engine-review gate
#: reads (gr597625): tests put ``rp._GitLand`` commits here — the real
#: ``lanes.engine_hits`` filter still applies inside the gate.
_GIT_LANDS: list[Any] = []

#: The pinned ``origin/main`` head the fake scan resolves — the one
#: commit snapshot the pass pins its compare, ordering and recorded
#: target to (td601005); tests move it to model main moving.
_GIT_PIN: list[str] = [MAIN_SHA]

#: The fake release scope — the cut's range plus the commits on
#: ``release/r<N>`` after it (``rp._release_lands``, release train T4).
_REL_LANDS: list[Any] = []


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(_fg, "factory_git_token", lambda: None)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    monkeypatch.delenv("PRECIS_RELEASE_NO_RELEASE_WINDOWS", raising=False)
    monkeypatch.delenv("PRECIS_RELEASE_REPO", raising=False)
    psettings.invalidate()
    _GIT_LANDS.clear()
    _REL_LANDS.clear()
    _GIT_PIN[0] = MAIN_SHA
    monkeypatch.setattr(rp, "_git_lands", lambda: (list(_GIT_LANDS), _GIT_PIN[0]))
    monkeypatch.setattr(
        rp, "_release_lands", lambda rel: list(_REL_LANDS), raising=False
    )


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=UTC)


def _head(n: int, sha: str = HEAD_SHA) -> dict[str, Any]:
    return {"ref": f"refs/heads/release/r{n}", "object": {"sha": sha}}


def _deployed(n: int) -> dict[str, Any]:
    return {"ref": f"refs/tags/deployed/r{n}", "object": {"sha": "b" * 40}}


def _fake_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compare: Any = None,
    heads: Any = (),
    tags: Any = (),
    prod_cmp: Any = None,
    main_cmp: Any = None,
    main_ref: Any = ...,
    runs: Any = ...,
    status: int = 200,
    error: Exception | None = None,
) -> list[str]:
    """Patch ``pr_class._http_get``: route on the URL suffix, record the
    URLs it was asked for."""
    calls: list[str] = []
    if compare is None:
        compare = {"status": "ahead", "ahead_by": 3, "behind_by": 0}

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls.append(url)
        if error is not None:
            raise error
        # td601005: the compare's head is the scan's pin, not ``main``.
        if "/compare/prod..." in url:
            data: Any = compare
        elif url.endswith("/git/matching-refs/heads/release/"):
            data = list(heads) if isinstance(heads, (list, tuple)) else heads
        elif url.endswith("/git/matching-refs/tags/deployed/"):
            data = list(tags) if isinstance(tags, (list, tuple)) else tags
        elif url.endswith("/git/ref/heads/main"):
            if main_ref is ...:
                data = {"ref": "refs/heads/main", "object": {"sha": MAIN_SHA}}
            else:
                data = main_ref
        elif url.endswith("/actions/runs"):
            data = {"workflow_runs": []} if runs is ... else runs
        elif "/compare/" in url and url.endswith("...prod"):
            data = prod_cmp if prod_cmp is not None else {"status": "ahead"}
        elif "/compare/" in url and url.endswith("...main"):
            data = main_cmp if main_cmp is not None else {"status": "behind"}
        else:
            data = {}
        return SimpleNamespace(status_code=status, json=lambda: data)

    monkeypatch.setattr(P, "_http_get", get)
    return calls


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id)}


def _close(store: Store, ref_id: int) -> None:
    store.add_tag(
        ref_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )


def _drop(store: Store, ref_id: int) -> None:
    """A pass dropped mid-flight: the root closes ``won't-do``."""
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", "won't-do"),
        set_by="agent",
        replace_prefix=True,
    )


def _plan_members(store: Store, root: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.meta FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (f"plan:{root}",),
        ).fetchall()
    return [dict(r[0] or {}) for r in rows]


def test_mints_when_ahead_and_idle(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.summary() == f"release_poll {REPO}: minted td{res.minted}"
    # one compare, the heads probe (no tag read with no release heads),
    # then the main-sha read for the stamp
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
    ]
    root = res.minted
    assert root is not None and "release-pass" in _tags(store, root)
    assert f"release-main:{MAIN_SHA[:12]}" in _tags(store, root)
    members = _plan_members(store, root)
    # root, cut, poll, gate, loop, approval, deploy, confirm, merge_back
    assert len(members) == 9
    ops = sorted(str(m["op"]) for m in members if isinstance(m.get("op"), str))
    assert ops == [
        "ci_verdict_poll",
        "release_confirm",
        "release_cut",
        "release_deploy",
    ]
    # the second pass stops on the open root before any GitHub read
    calls = _fake_github(monkeypatch)
    again = rp.run_pass(store, repo=REPO, now=NOW)
    assert calls == [] and again.blocker == f"release-pass td{root} is open"


def test_a_merge_back_alone_mints_no_pass_a_real_land_after_it_does(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """release-train T3: the merge-back commit alone puts main ahead of
    prod with prod's own tree (the compare's ``files`` is empty) — no
    pass for an identical tree. One real land after it, and one mints."""
    merge = {"sha": MAIN_SHA, "parents": [{"sha": "e" * 40}, {"sha": HEAD_SHA}]}
    calls = _fake_github(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "commits": [merge],
            "files": [],
        },
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None
    assert res.blocker is not None and "tree equals prod's" in res.blocker
    assert calls == [f"{API}/compare/prod...{MAIN_SHA}"]
    _fake_github(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "commits": [merge, {"sha": "f" * 40}],
            "files": [{"filename": "src/precis/x.py", "status": "modified"}],
        },
    )
    again = rp.run_pass(store, repo=REPO, now=NOW)
    assert again.minted is not None, again.blocker


def test_closed_pass_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = T.mint_release_pass(store).root_id
    _close(store, first)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None and res.minted != first


def test_skips_on_an_open_pass(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    mint = T.mint_release_pass(store)
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"release-pass td{mint.root_id} is open"
    assert calls == [] and res.minted is None


def test_skips_on_an_in_flight_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a release head with no deployed tag: in flight — no prod compare,
    # and the main compare answers "behind" (main lacks its commits)
    calls = _fake_github(monkeypatch, heads=[_head(7)])
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release/r7 is in flight"
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
        f"{API}/compare/{HEAD_SHA}...main",
    ]
    # head + tag, prod contains the head: not in flight, mints
    calls = _fake_github(
        monkeypatch,
        heads=[_head(7)],
        tags=[_deployed(7)],
        prod_cmp={"status": "ahead"},
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
        f"{API}/compare/{HEAD_SHA}...prod",
    ]
    _close(store, res.minted)
    # head + tag but prod lacks the head: in flight
    _fake_github(
        monkeypatch,
        heads=[_head(7)],
        tags=[_deployed(7)],
        prod_cmp={"status": "diverged"},
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release/r7 is in flight" and res.minted is None


def test_branch_in_flight_case_no_tag_prod_has_head() -> None:
    # a head prod already contains still counts as in flight without the tag
    assert rp.branch_in_flight(False, True) is True
    assert rp.branch_in_flight(False, None) is True


def test_branch_in_flight_case_tag_prod_lacks_head() -> None:
    # a tag with a head prod does not contain is not a deploy
    assert rp.branch_in_flight(True, False) is True
    assert rp.branch_in_flight(True, None) is True
    assert rp.branch_in_flight(True, True) is False


def test_a_stale_head_on_main_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr585785: a superseded release head left on origin — all its
    commits already on main — is a stale ref, not a release in flight."""
    calls = _fake_github(monkeypatch, heads=[_head(7)], main_cmp={"status": "ahead"})
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
        f"{API}/compare/{HEAD_SHA}...main",
    ]
    _close(store, res.minted)
    # identical to main's tip clears the same way (main moved: new sha)
    _GIT_PIN[0] = "e" * 40
    _fake_github(
        monkeypatch,
        heads=[_head(7)],
        main_cmp={"status": "identical"},
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    _close(store, res.minted)
    # a head whose commits main does not contain still blocks
    _GIT_PIN[0] = MAIN_SHA
    calls = _fake_github(monkeypatch, heads=[_head(7)], main_cmp={"status": "behind"})
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release/r7 is in flight" and res.minted is None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
        f"{API}/compare/{HEAD_SHA}...main",
    ]


@pytest.mark.parametrize(
    ("status", "ahead", "behind", "blocker"),
    [
        ("identical", 0, 0, "main is not ahead of prod (identical)"),
        ("behind", 0, 2, "prod has commits main lacks (behind)"),
        ("diverged", 3, 1, "prod has commits main lacks (diverged)"),
        # a hand hotfix on prod must not mint even though ahead_by > 0
        ("diverged", 40, 1, "prod has commits main lacks (diverged)"),
    ],
    ids=["identical", "behind", "diverged", "diverged-ahead"],
)
def test_skips_when_prod_equals_main(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    ahead: int,
    behind: int,
    blocker: str,
) -> None:
    calls = _fake_github(
        monkeypatch,
        compare={"status": status, "ahead_by": ahead, "behind_by": behind},
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == blocker
    assert calls == [f"{API}/compare/prod...{MAIN_SHA}"]


@pytest.mark.parametrize(
    ("at", "mints"),
    [
        (_at(2, 59), True),
        (_at(3, 0), False),
        (_at(4, 19), False),
        (_at(4, 20), True),
    ],
    ids=["before", "start", "inside", "end"],
)
def test_skips_inside_the_window(
    store: Store, monkeypatch: pytest.MonkeyPatch, at: datetime, mints: bool
) -> None:
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=at)
    if mints:
        assert res.minted is not None
    else:
        assert res.blocker == "inside the no-release window 03:00-04:20Z"
        assert calls == [] and res.minted is None


def test_window_list_and_midnight_crossing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.no_release_windows", "03:00-04:20,21:30-23:30", store=store
    )
    for at in (_at(21, 30), _at(23, 29)):
        res = rp.run_pass(store, repo=REPO, now=at)
        assert res.blocker == "inside the no-release window 21:30-23:30Z", at
    res = rp.run_pass(store, repo=REPO, now=_at(23, 30))
    assert res.minted is not None
    _close(store, res.minted)
    # a window whose start is later than its end crosses midnight
    psettings.set_setting("release.no_release_windows", "23:00-01:00", store=store)
    for at in (_at(23, 30), _at(0, 30)):
        res = rp.run_pass(store, repo=REPO, now=at)
        assert res.blocker == "inside the no-release window 23:00-01:00Z", at
    # one pass per main sha: each mint here needs main to have moved —
    # main moving is the scan's pin moving (td601005).
    for at, sha in ((_at(1, 0), "e" * 40), (_at(22, 59), "f" * 40)):
        _GIT_PIN[0] = sha
        _fake_github(monkeypatch)
        res = rp.run_pass(store, repo=REPO, now=at)
        assert res.minted is not None, at
        _close(store, res.minted)


@pytest.mark.parametrize("bad", ["", "25:00-26:00", "03:00-03:00"])
def test_unparsable_windows_block(
    store: Store, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    calls = _fake_github(monkeypatch)
    psettings.set_setting("release.no_release_windows", bad, store=store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == ("release.no_release_windows is unparsable: nothing minted")
    assert calls == [] and res.minted is None


def test_dry_run_reads_and_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, dry_run=True, now=NOW)
    assert res.would_mint and res.summary() == (
        f"release_poll {REPO}: would mint (dry run)"
    )
    assert calls and rp._release_pass_ids(store) == []


@pytest.mark.parametrize(
    ("status", "error"),
    [(200, OSError("network down")), (500, None), (404, None)],
    ids=["network", "http500", "http404"],
)
def test_github_unreadable_mints_nothing(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error: Exception | None,
) -> None:
    _fake_github(monkeypatch, status=status, error=error)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert "GitHub unreadable" in res.blocker and res.minted is None
    assert rp._release_pass_ids(store) == []


def test_default_window_matches_the_backup_defaults() -> None:
    """The setting's compiled default restates the backups role's
    ``backup_window_{start,end}_utc`` lines."""
    text = (REPO_ROOT / "deploy/roles/backups/defaults/main.yml").read_text(
        encoding="utf-8"
    )

    def value(key: str) -> str:
        m = re.search(rf'^{key}: "(\d{{2}}:\d{{2}})"', text, re.MULTILINE)
        assert m is not None, f"{key} not found in the backups defaults"
        return m.group(1)

    want = f"{value('backup_window_start_utc')}-{value('backup_window_end_utc')}"
    assert psettings.REGISTRY["release.no_release_windows"].default == want


def test_no_setting_row_falls_back_to_the_default_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``release.no_release_windows`` row (and no env override, per the
    ``_env`` fixture) resolves to the compiled default — an operator who
    never set the key is still inside the nightly window."""
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=_at(3, 30))
    assert res.blocker == "inside the no-release window 03:00-04:20Z"
    assert calls == [] and res.minted is None


# ── the shared release.round record (td573430 fix round 1) ──────────


def _round_row(state: str, n: int = 5) -> str:
    return json.dumps(
        {"n": n, "branch": f"release/r{n}", "base": "c" * 40, "state": state}
    )


@pytest.mark.parametrize(
    "state", ["cut", "gating", "fixing", "green", "deploying", "deployed"]
)
def test_an_open_round_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    calls = _fake_github(monkeypatch)
    psettings.set_setting("release.round", _round_row(state), store=store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"release round r5 in flight ({state})"
    assert calls == [] and res.minted is None


@pytest.mark.parametrize("state", ["confirmed", "superseded", "abandoned"])
def test_a_closed_round_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting("release.round", _round_row(state), store=store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None


@pytest.mark.parametrize(
    ("raw", "blocker"),
    [
        ("not json", "release.round is unparsable: nothing minted"),
        ("[1,2]", "release.round is not a record: nothing minted"),
        ('{"n":5}', "release.round carries no state: nothing minted"),
        ('{"n":5,"state":""}', "release.round carries no state: nothing minted"),
    ],
    ids=["not-json", "not-a-dict", "no-state", "empty-state"],
)
def test_an_unreadable_round_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch, raw: str, blocker: str
) -> None:
    calls = _fake_github(monkeypatch)
    psettings.set_setting("release.round", raw, store=store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == blocker
    assert calls == [] and res.minted is None


def test_a_failed_round_read_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error on the row is not "no round" — the pass blocks.
    (``settings._db_read`` would return ``None`` for the same failure.)"""
    calls = _fake_github(monkeypatch)

    def boom(s: Store) -> str | None:
        raise OSError("db gone")

    monkeypatch.setattr(rp, "_round_raw", boom)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release.round unreadable (OSError): nothing minted"
    assert calls == [] and res.minted is None


def test_the_job_written_row_blocks_then_the_confirm_clears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row ``release_cut.poll`` writes blocks the mint (``cut``,
    then ``deployed``); the confirm's ``record_confirm`` moves it to a
    terminal state and the pass mints again (gr575362)."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", HEAD_SHA, f"{'9' * 12}..{HEAD_SHA[:12]}")
    _close(store, mint.root_id)  # the open-pass gate would stop first
    calls = _fake_github(monkeypatch)
    R.update_round(  # what release_cut.poll writes after record_cut
        store,
        {
            "n": 7,
            "branch": "release/r7",
            "base": HEAD_SHA,
            "state": "cut",
            "owner": {"host": "node-a", "checkout": "/srv/repo"},
        },
        create=True,
        now=NOW,
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release round r7 in flight (cut)"
    R.update_round(  # what release_deploy.poll writes on exit 0/3
        store,
        {
            "n": 7,
            "state": "deployed",
            "deploy": {"sha": HEAD_SHA, "rollout_at": "2026-11-03T11:55:00Z"},
        },
        now=NOW,
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release round r7 in flight (deployed)"
    assert calls == []  # the round gate stops before any GitHub read
    R.record_confirm(store, mint.ids["deploy"], HEAD_SHA, "evidence", now=NOW)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
    ]


def _cut_row_for(store: Store, mint: T.PlanMint, **over: Any) -> dict[str, Any]:
    """The row ``release_cut.poll`` writes: ``state: cut`` with the pass
    root on it (``update_round(create=True)``)."""
    row: dict[str, Any] = {
        "n": 7,
        "branch": "release/r7",
        "base": "c" * 40,
        "state": "cut",
        "owner": {"host": "node-a", "checkout": "/srv/repo"},
        "pass": f"td{mint.root_id}",
    }
    row.update(over)
    R.update_round(store, row, create=True, now=NOW)
    return row


@pytest.mark.parametrize("status", ["won't-do", "done"], ids=["wont-do", "done"])
def test_a_dropped_pass_abandons_its_cut_row(
    store: Store, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """gr575362 review: a pass dropped after its cut left the shared row
    at ``cut`` and blocked every later poll. The row records the pass
    root; a terminal pass with no deploy evidence means nothing will
    move the row — the poll abandons it and mints."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    if status == "done":
        _close(store, mint.root_id)
    else:
        _drop(store, mint.root_id)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None and res.blocker == ""
    row = R.read_round(store)
    assert row is not None and row["state"] == "abandoned"
    assert row["state_at"] == "2026-11-03T12:00:00Z"
    assert f"td{mint.root_id}" in row["abandoned_why"]
    assert status in row["abandoned_why"]


def test_a_cut_row_whose_pass_closed_after_a_rollout_keeps_blocking(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done *with* a deploy: the prod head names the cut sha, so the row
    stays — a human supersedes it where the publish works."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _close(store, mint.root_id)
    R.record_prod_head(store, "c" * 40, [], now=NOW)
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None and calls == []
    assert f"release round r7 in flight (cut) but pass td{mint.root_id} is done" in (
        res.blocker
    )
    assert "scripts/round supersede r7" in res.blocker
    assert "prod-precis settings set release.round" in res.blocker
    row = R.read_round(store)
    assert row is not None and row["state"] == "cut"


def test_a_cut_row_whose_pass_is_confirmed_keeps_blocking(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``meta.release.confirmed`` on the root is deploy evidence too."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _close(store, mint.root_id)
    store.stamp_ref_meta(
        mint.root_id,
        {
            "release": {
                "rN": "r7",
                "confirmed": {"sha": "c" * 40, "at": "t", "evidence": "e"},
            }
        },
    )
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None and calls == []
    assert "in flight (cut) but pass" in res.blocker


@pytest.mark.parametrize("state", ["deploying", "deployed"])
def test_a_rolled_out_row_whose_pass_is_terminal_keeps_blocking(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """``deploying``/``deployed`` with a terminal pass: something may have
    rolled out, so the row stays — the blocker names the hand verb."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint, state=state)
    _drop(store, mint.root_id)
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None and calls == []
    assert f"release round r7 in flight ({state})" in res.blocker
    assert "scripts/round supersede r7" in res.blocker
    row = R.read_round(store)
    assert row is not None and row["state"] == state


def test_a_cut_row_with_an_open_pass_blocks_normally(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open pass is caught by the open-pass gate first; the row is
    untouched either way."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"release-pass td{mint.root_id} is open"
    row = R.read_round(store)
    assert row is not None and row["state"] == "cut"


def test_a_dropped_pass_abandon_honours_dry_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run reports the would-abandon and writes nothing."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _drop(store, mint.root_id)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, dry_run=True, now=NOW)
    assert res.would_mint is True
    assert any("would abandon" in n for n in res.notes)
    row = R.read_round(store)
    assert row is not None and row["state"] == "cut"


def test_a_dropped_pass_abandon_honours_factory_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch holds the write the same way: report, no write."""
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _drop(store, mint.root_id)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert any("would abandon" in n for n in res.notes)
    row = R.read_round(store)
    assert row is not None and row["state"] == "cut"


def _live_job(store: Store, todo_id: int, job_type: str) -> None:
    store.insert_ref(
        kind="job",
        slug=None,
        title=f"{job_type} job",
        meta={"job_type": job_type},
        parent_id=todo_id,
    )


def _jobs_under(store: Store, todo_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def test_a_cut_row_is_not_abandoned_while_a_deploy_job_runs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root was dropped but the deploy job is still live: the rollout
    may be in flight and the job's own ``deployed`` write is coming —
    the row stays."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _live_job(store, mint.ids["deploy"], "release_deploy")
    _drop(store, mint.root_id)
    calls = _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None and calls == []
    assert "release_deploy job is still live" in res.blocker
    row = R.read_round(store)
    assert row is not None and row["state"] == "cut"


def test_a_finished_deploy_job_does_not_save_the_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal deploy job is not deploy evidence on its own."""
    mint = T.mint_release_pass(store)
    _cut_row_for(store, mint)
    _live_job(store, mint.ids["deploy"], "release_deploy")
    _live_job(store, mint.ids["cut"], "release_cut")
    _drop(store, mint.root_id)
    for job_id, _meta in _jobs_under(store, mint.ids["deploy"]):
        store.add_tag(
            job_id,
            Tag.closed("STATUS", "succeeded"),
            set_by="system",
            replace_prefix=True,
        )
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    row = R.read_round(store)
    assert row is not None and row["state"] == "abandoned"


def test_round_terminal_states_match_the_script() -> None:
    """``_TERMINAL_STATES`` is the same literal scripts/round and its
    strict reader validate — an unedited copy, pinned. The ``is_terminal``
    predicate is pinned the same way: all three implementations must agree
    on every state × journal shape — ``failed`` is terminal only while its
    journal shows ``scripts/deploy`` never launched (no ``started_epoch``,
    no ``rollout_at``)."""
    if sys.platform == "win32":
        pytest.skip("scripts/round is POSIX release tooling")
    script = load_script("round_script_rel_poll_pin", REPO_ROOT / "scripts" / "round")
    reader = load_script(
        "round_state_rel_poll_pin", REPO_ROOT / "scripts" / "lib" / "round_state.py"
    )
    assert script._TERMINAL_STATES == reader.TERMINAL_STATES
    assert script._TERMINAL_STATES == R._CONFIRM_TERMINAL
    states = sorted(reader.RELEASE_STATES)
    for state in states:
        for key in (None, "deployment", "deploy"):
            for started in (False, True):
                for rollout in (False, True):
                    rec: dict[str, Any] = {"state": state}
                    if key is not None:
                        rec[key] = {"sha": "a" * 40}
                        if started:
                            rec[key]["started_epoch"] = 1762000000
                        if rollout:
                            rec[key]["rollout_at"] = "2026-11-03T12:00:00Z"
                    launched = key is not None and (started or rollout)
                    expected = state in reader.TERMINAL_STATES and not (
                        state == "failed" and launched
                    )
                    verdict = (
                        R.is_terminal(rec),
                        reader.is_terminal(rec),
                        script._is_terminal(rec),
                    )
                    assert verdict == (expected,) * 3, (
                        f"is_terminal disagreement on {rec!r}: {verdict}"
                    )


def test_a_failed_round_row_blocks_only_while_its_rollout_launched(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr593418 review: ``failed`` + ``deploy.started_epoch`` is a live
    partial rollout — it blocks the mint; ``failed`` with a pre-launch
    death (no ``started_epoch``, no ``rollout_at``) or no journal is
    terminal and the pass mints."""
    _fake_github(monkeypatch)
    row = json.loads(_round_row("failed"))
    row["deploy"] = {"sha": "b" * 40, "started_epoch": 1762000000}
    psettings.set_setting("release.round", json.dumps(row), store=store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release round r5 in flight (failed)"
    assert res.minted is None
    for dep in ({"sha": "b" * 40}, None):
        row = json.loads(_round_row("failed"))
        if dep is not None:
            row["deploy"] = dep
        psettings.set_setting("release.round", json.dumps(row), store=store)
        res = rp.run_pass(store, repo=REPO, now=NOW)
        # The round gate passes: a mint, or a later gate's blocker (the
        # first mint's open pass) — never the round row.
        assert "release round" not in (res.blocker or "")


# ── matching-refs bounds (td573430 fix round 1) ─────────────────────


def test_over_cap_release_heads_are_unreadable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(
        monkeypatch, heads=[_head(n) for n in range(1, rp._HEAD_CAP + 2)]
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert "GitHub unreadable" in res.blocker and res.minted is None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
    ]


def test_a_non_list_matching_refs_payload_is_unreadable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch, heads={"ref": "refs/heads/release/r7"})
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert "GitHub unreadable" in res.blocker and res.minted is None
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
    ]


def test_over_cap_deployed_tags_read_as_in_flight(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More deployed tags than the bound: no head can prove its deploy, so
    every head reads in flight — the safe direction, never a mint."""
    _fake_github(
        monkeypatch,
        heads=[_head(7)],
        tags=[_deployed(n) for n in range(1, rp._TAG_CAP + 2)],
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release/r7 is in flight" and res.minted is None


# ── one pass per main sha, under one advisory lock (fix round 1) ────


def test_a_pass_for_the_same_sha_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first pass closes (done, failed or abandoned — the sha claim
    counts every state); main unmoved, the next tick names the claim.
    When main moves, a new pass mints."""
    _fake_github(monkeypatch)
    first = rp.run_pass(store, repo=REPO, now=NOW)
    assert first.minted is not None
    _close(store, first.minted)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"pass for {MAIN_SHA[:12]} already ran: done"
    assert res.minted is None
    _GIT_PIN[0] = "e" * 40
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert f"release-main:{'e' * 12}" in _tags(store, res.minted)


def test_dry_run_reports_an_existing_claim(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    first = rp.run_pass(store, repo=REPO, now=NOW)
    assert first.minted is not None
    _close(store, first.minted)
    res = rp.run_pass(store, repo=REPO, dry_run=True, now=NOW)
    assert res.blocker == f"pass for {MAIN_SHA[:12]} already ran: done"
    assert not res.would_mint


def test_two_interleaved_runners_mint_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check-then-mint is one transaction under the advisory lock: two
    runners that both pass every gate mint one pass between them — the
    loser's re-check inside the lock sees the winner's root."""
    _fake_github(monkeypatch)
    barrier = threading.Barrier(2)
    real_in_flight = rp._in_flight

    def synced_in_flight(repo: str) -> str | None:
        barrier.wait(timeout=30)  # both runners reach the claim together
        return real_in_flight(repo)

    monkeypatch.setattr(rp, "_in_flight", synced_in_flight)
    results: list[rp.PollResult] = []

    def run() -> None:
        results.append(rp.run_pass(store, repo=REPO, now=NOW))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    minted = [r for r in results if r.minted is not None]
    blocked = [r for r in results if r.blocker]
    assert len(results) == 2 and len(minted) == 1 and len(blocked) == 1
    assert blocked[0].blocker == f"release-pass td{minted[0].minted} is open"
    assert len(rp._release_pass_ids(store)) == 1


# ── the observation-only stuck watchdog (td574370) ───────────────────
#
# The ``release.round`` row's ``state_at`` is judged against
# ``release.stage_deadlines``; an overdue stage gets one ``ask-user``
# ruling todo per (round, stage), offering only the hand verbs. GitHub
# is faked at ``pr_class._http_get`` like the mint tests.

STUCK_BASE = "c" * 40


def _stuck_row(
    state: str,
    *,
    n: int = 5,
    state_at: str | None = None,
    base: str = STUCK_BASE,
    deploy: dict[str, Any] | None = None,
) -> str:
    row: dict[str, Any] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": base,
        "state": state,
    }
    if state_at is not None:
        row["state_at"] = state_at
    if deploy is not None:
        row["deploy"] = deploy
    return json.dumps(row)


def _verdict_row(sha: str, verdict: str, at: str) -> dict[str, str]:
    return {
        "sha": sha[:12],
        "range": f"aa..{sha[:12]}",
        "verdict": verdict,
        "run_id": "42",
        "at": at,
    }


def _stuck_asks(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value LIKE 'release-stuck:%'
             ORDER BY r.ref_id
            """
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ask_body(store: Store, ref_id: int) -> str:
    return "\n".join(
        str(c.text or "") for c in store.chunks.list_chunks_for_ref(ref_id)
    )


def _stamp(minutes_ago: int) -> str:
    from datetime import timedelta

    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _watch(store: Store) -> list[str]:
    return rp.watch_stuck(store, now=NOW, dry_run=False)


def test_stage_deadlines_default_is_db_only() -> None:
    spec = psettings.REGISTRY[rp.STUCK_SETTING]
    assert spec.env_var is None
    assert json.loads(str(spec.default)) == {
        "cut": 30,
        "gating": 180,
        "fixing": 240,
        "deploying": 60,
        "confirm": 45,
    }


def test_deploying_overdue_files_one_ask_with_the_named_content(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    notes = _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert any("filed" in n for n in notes)
    ref = store.get_ref(kind="todo", id=asks[0])
    assert ref is not None
    ask = ref.meta["ask"]
    assert ask["kind"] == "ruling"
    assert ask["options"] == ["acknowledged", "false alarm"]
    assert ask["blocks"] == "nothing"
    assert "60m past the 60m deadline" in ask["context"]
    assert STUCK_BASE[:12] in ask["context"]
    assert "executes nothing" in ask["context"]
    tags = _tags(store, asks[0])
    assert {"ask-user", "showstopper", "release-stuck:r5:deploying"} <= tags
    body = _ask_body(store, asks[0])
    assert "scripts/round deploy" in body
    assert "supersede --partial" in body
    assert "executes nothing" in body


def test_confirm_overdue_files_an_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row(
            "deployed",
            state_at=_stamp(60),
            deploy={"sha": STUCK_BASE, "rollout_at": _stamp(65)},
        ),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:confirm" in _tags(store, asks[0])


def test_deployed_with_confirmation_never_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row(
            "deployed",
            state_at=_stamp(300),
            deploy={
                "sha": STUCK_BASE,
                "rollout_at": _stamp(300),
                "confirmed_at": _stamp(200),
            },
        ),
        store=store,
    )
    assert _watch(store) == []
    assert _stuck_asks(store) == []


def test_cut_no_verdict_no_run_files_cut(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(monkeypatch, runs={"workflow_runs": []})
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(45)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:cut" in _tags(store, asks[0])
    assert calls == [f"{API}/actions/runs"]


def test_cut_no_verdict_with_a_run_is_gating_past_its_own_deadline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    psettings.set_setting("release.repo", REPO, store=store)
    runs = {"workflow_runs": [{"status": "in_progress"}]}
    _fake_github(monkeypatch, runs=runs)
    # 60m in: past the 30m cut mark so the probe runs, but well inside
    # gating's 180m — no ask.
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(60)), store=store
    )
    _watch(store)
    assert _stuck_asks(store) == []
    # 240m in: the run is still in flight and gating's own deadline passed.
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(240)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:gating" in _tags(store, asks[0])


def test_red_verdict_past_fixing_files_fixing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)
    psettings.set_setting(
        "release.ci_verdicts",
        json.dumps([_verdict_row(STUCK_BASE, "red", _stamp(300))]),
        store=store,
    )
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(50)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:fixing" in _tags(store, asks[0])
    assert calls == []  # a verdict answers the stage; no GitHub read


def test_green_verdict_never_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.ci_verdicts",
        json.dumps([_verdict_row(STUCK_BASE, "green", _stamp(400))]),
        store=store,
    )
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(400)), store=store
    )
    assert _watch(store) == []
    assert _stuck_asks(store) == []


def test_red_on_an_older_head_is_fixing_until_the_current_head_has_a_verdict(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """meta.release.sha names the fix head; a red on the row's base (the
    round's older head) is fixing until the new head has a verdict."""
    calls = _fake_github(monkeypatch)
    new_head = "d" * 40
    root = T.mint_release_pass(store).root_id
    store.stamp_ref_meta(
        root, {"release": {"rN": "r5", "sha": new_head, "range": "aa..dd"}}
    )
    psettings.set_setting(
        "release.ci_verdicts",
        json.dumps([_verdict_row(STUCK_BASE, "red", _stamp(300))]),
        store=store,
    )
    psettings.set_setting(
        "release.round",
        _stuck_row("cut", state_at=_stamp(50), base=STUCK_BASE),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:fixing" in _tags(store, asks[0])
    assert calls == []


def test_a_factory_cut_row_is_judged_like_a_script_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row ``release_cut.poll`` writes carries ``state_at`` — the
    watchdog judges a factory-cut round like a script-published one
    (gr575362)."""
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(monkeypatch, runs={"workflow_runs": []})
    R.update_round(
        store,
        {
            "n": 7,
            "branch": "release/r7",
            "base": STUCK_BASE,
            "state": "cut",
            "owner": {"host": "node-a", "checkout": "/srv/repo"},
        },
        create=True,
        now=NOW - timedelta(minutes=45),
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r7:cut" in _tags(store, asks[0])
    assert calls == [f"{API}/actions/runs"]


def test_no_state_at_never_asks(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record cut before state_at existed is never judged."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", deploy={"sha": STUCK_BASE}),
        store=store,
    )
    assert _watch(store) == []
    assert _stuck_asks(store) == []


def test_future_state_at_never_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at="2026-11-03T13:00:00Z"),
        store=store,
    )
    assert _watch(store) == []
    assert _stuck_asks(store) == []


@pytest.mark.parametrize("state", ["confirmed", "superseded", "abandoned"])
def test_terminal_state_never_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round", _stuck_row(state, state_at=_stamp(9999)), store=store
    )
    assert _watch(store) == []
    assert _stuck_asks(store) == []


def test_second_tick_files_no_second_ask_even_after_the_first_is_answered(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    _close(store, asks[0])  # answered and closed
    assert _stuck_asks(store) == [asks[0]]
    _watch(store)
    assert _stuck_asks(store) == [asks[0]]  # the tag dedupes any status


def test_a_new_round_or_another_stage_files_its_own(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    assert len(_stuck_asks(store)) == 1
    # a different stage of the same round files its own ask
    psettings.set_setting(
        "release.round",
        _stuck_row(
            "deployed",
            state_at=_stamp(120),
            deploy={"sha": STUCK_BASE},
        ),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 2
    assert "release-stuck:r5:confirm" in _tags(store, asks[-1])
    # and a new round files its own
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", n=6, state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 3
    assert "release-stuck:r6:deploying" in _tags(store, asks[-1])


def test_cut_zero_still_watches_gating(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cut: 0` disables only `cut`: with a run in flight the probe
    fires on the enabled gating mark and files gating past it."""
    psettings.set_setting("release.repo", REPO, store=store)
    _fake_github(
        monkeypatch,
        runs={"workflow_runs": [{"status": "in_progress", "head_sha": "d" * 40}]},
    )
    psettings.set_setting(
        rp.STUCK_SETTING, json.dumps({"cut": 0, "gating": 180}), store=store
    )
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(240)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:gating" in _tags(store, asks[0])


def test_gating_zero_disables_only_gating(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gating: 0` disables only `gating`: a run in flight files
    nothing, and no run still files `cut` past its own mark."""
    psettings.set_setting("release.repo", REPO, store=store)
    _fake_github(
        monkeypatch,
        runs={"workflow_runs": [{"status": "in_progress", "head_sha": "d" * 40}]},
    )
    psettings.set_setting(
        rp.STUCK_SETTING, json.dumps({"cut": 30, "gating": 0}), store=store
    )
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(45)), store=store
    )
    _watch(store)
    assert _stuck_asks(store) == []
    _fake_github(monkeypatch, runs={"workflow_runs": []})
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:cut" in _tags(store, asks[0])


def test_the_probe_reads_branch_runs_and_names_their_head(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one GitHub read asks for runs on branch ``release/r<N>``
    (newest first), never a head sha from the record — a hand
    ``ship --release`` head move cannot produce a wrong stage. The ask
    names the head the run was on and flags it may differ from the
    record's base."""
    psettings.set_setting("release.repo", REPO, store=store)
    probed: list[dict[str, Any]] = []

    def fake_get_json(url: str, params: dict[str, Any]) -> Any:
        probed.append({"url": url, **dict(params)})
        return {"workflow_runs": [{"status": "in_progress", "head_sha": "d" * 40}]}

    monkeypatch.setattr(P, "_get_json", fake_get_json)
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(240)), store=store
    )
    _watch(store)
    assert probed and probed[0].get("branch") == "release/r5"
    assert "head_sha" not in probed[0]
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:gating" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert ("d" * 40)[:12] in body
    assert "may differ from the record's base" in body
    assert STUCK_BASE[:12] in body


def test_a_filed_stage_costs_no_more_github_reads(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the (round, stage) ask exists the dedupe tag is checked
    first — the probe never runs again (the shared 60/h budget)."""
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(
        monkeypatch,
        runs={"workflow_runs": [{"status": "in_progress", "head_sha": "d" * 40}]},
    )
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(240)), store=store
    )
    _watch(store)
    assert calls == [f"{API}/actions/runs"]
    _watch(store)
    _watch(store)
    assert calls == [f"{API}/actions/runs"]


def test_a_stage_set_to_zero_disables_only_that_stage(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    psettings.set_setting("release.repo", REPO, store=store)
    _fake_github(monkeypatch, runs={"workflow_runs": []})
    psettings.set_setting(
        rp.STUCK_SETTING,
        json.dumps({"cut": 30, "deploying": 0}),
        store=store,
    )
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    assert _stuck_asks(store) == []  # deploying: 0 → not watched
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(45)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:cut" in _tags(store, asks[0])


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"x"'])
def test_unparsable_or_non_object_deadlines_ask_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(rp.STUCK_SETTING, bad, store=store)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    notes = _watch(store)
    assert any("watching nothing" in n for n in notes)
    assert _stuck_asks(store) == []


def test_dry_run_files_nothing(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    notes = rp.watch_stuck(store, now=NOW, dry_run=True)
    assert any("would file" in n for n in notes)
    assert _stuck_asks(store) == []


def test_factory_off_files_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    notes = _watch(store)
    assert any("would file" in n for n in notes)
    assert _stuck_asks(store) == []


def test_unreadable_record_github_error_and_db_error_ask_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # unparsable / non-object record: a note, no ask
    _fake_github(monkeypatch)
    for raw in ("not json", "[1,2]", '{"n":5,"state":null}'):
        psettings.set_setting("release.round", raw, store=store)
        notes = _watch(store)
        assert _stuck_asks(store) == []
        assert notes
    # GitHub error on the runs probe: a note, no ask
    psettings.set_setting("release.repo", REPO, store=store)
    _fake_github(monkeypatch, error=OSError("network down"))
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(45)), store=store
    )
    notes = _watch(store)
    assert any("GitHub unreadable" in n for n in notes)
    assert _stuck_asks(store) == []

    # a database error on the round row: a note, no ask, no raise
    def boom(s: Store) -> str | None:
        raise OSError("db gone")

    monkeypatch.setattr(rp, "_round_raw", boom)
    notes = _watch(store)
    assert any("unreadable" in n for n in notes)
    assert _stuck_asks(store) == []


def test_a_watch_failure_never_blocks_the_mint(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """watch_stuck raising inside run_pass is one note; the gates run."""

    def boom(store: Store, **kw: Any) -> list[str]:
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(rp, "watch_stuck", boom)
    _fake_github(monkeypatch)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None
    assert any("raised" in n for n in res.notes)
    assert _stuck_asks(store) == []


def test_dry_run_pass_writes_no_ask_but_still_reports(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    res = rp.run_pass(store, repo=REPO, dry_run=True, now=NOW)
    assert _stuck_asks(store) == []
    assert any("would file" in n for n in res.notes)


@pytest.mark.parametrize("auto_deploy", ["true", "false"])
def test_asks_are_identical_with_auto_deploy_on_or_off(
    store: Store, monkeypatch: pytest.MonkeyPatch, auto_deploy: str
) -> None:
    _fake_github(monkeypatch)
    psettings.set_setting("release.auto_deploy", auto_deploy, store=store)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    ref = store.get_ref(kind="todo", id=asks[0])
    assert ref is not None
    assert ref.meta["ask"]["options"] == ["acknowledged", "false alarm"]


def test_the_ask_parents_under_the_matching_pass_root_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    # hand release: no pass root → parentless ask, no plan tag
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    ref = store.get_ref(kind="todo", id=asks[0])
    assert ref is not None and ref.parent_id is None
    assert not any(t.startswith("plan:") for t in _tags(store, asks[0]))
    # an open pass whose release_of names r5 → parent + plan:<root> tag
    _close(store, asks[0])
    root = T.mint_release_pass(store).root_id
    store.stamp_ref_meta(
        root,
        {"release": {"rN": "r5", "sha": STUCK_BASE, "range": "aa..cc"}},
    )
    psettings.set_setting(
        "release.round",
        _stuck_row(
            "deployed",
            state_at=_stamp(120),
            deploy={"sha": STUCK_BASE},
        ),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 2
    ref = store.get_ref(kind="todo", id=asks[-1])
    assert ref is not None and ref.parent_id == root
    assert f"plan:{root}" in _tags(store, asks[-1])


def _pass_leg_job(
    store: Store,
    rn: str,
    job_type: str,
    *,
    status: str = "queued",
    job_age: int = 120,
    event_age: int | None = None,
) -> int:
    """An open release-pass root for ``rn`` carrying one job of
    ``job_type`` under a ``plan:<root>`` member — the leg a stuck stage
    waits on. The ref stamps sit ``job_age`` minutes back so only
    ``event_age`` — a ``job_event`` chunk's stamp — makes the leg read
    alive."""
    root = T.mint_release_pass(store).root_id
    store.stamp_ref_meta(
        root, {"release": {"rN": rn, "sha": STUCK_BASE, "range": "aa..cc"}}
    )
    member = store.insert_ref(
        kind="todo",
        slug=None,
        title=f"{job_type} leg",
        meta={"op": job_type},
        parent_id=root,
    )
    store.add_tag(member.id, Tag.open(f"plan:{root}"), set_by="agent")
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"{job_type} td{member.id}",
        meta={"job_type": job_type},
        parent_id=int(member.id),
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    age = NOW - timedelta(minutes=job_age)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s, updated_at = %s WHERE ref_id = %s",
            (age, age, ref.id),
        )
    if event_age is not None:
        store.chunks.insert_chunks(
            int(ref.id),
            [
                ChunkInsert(
                    ord=0,
                    text="wake_runner: re-queued (wake_when)",
                    meta={"chunk_kind": "job_event"},
                )
            ],
        )
        at = NOW - timedelta(minutes=event_age)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET created_at = %s WHERE ref_id = %s",
                (at, int(ref.id)),
            )
    return int(ref.id)


def test_a_live_leg_job_event_suppresses_the_stage_clock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td583852/td584153: a ``ci_verdict_poll`` re-queued behind a drain
    backlog keeps writing ``job_event`` lines — the leg is waiting, not
    stuck, so a 60-minute ``cut`` files nothing on a 5-minute event."""
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(monkeypatch, runs={"workflow_runs": []})
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(60)), store=store
    )
    _pass_leg_job(store, "r5", "ci_verdict_poll", event_age=5)
    assert _watch(store) == []
    assert _stuck_asks(store) == []
    # A live leg holding the stage under every mark spares the probe —
    # no GitHub read at all this tick.
    assert calls == []


def test_a_leg_event_older_than_the_deadline_still_files(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The newest leg event is the clock: 45 minutes is past the
    30-minute ``cut`` mark — the ask files and names the clock used."""
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(monkeypatch, runs={"workflow_runs": []})
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(60)), store=store
    )
    _pass_leg_job(store, "r5", "ci_verdict_poll", event_age=45)
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:cut" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert "15m past the mark" in body
    assert (
        f"clock: last leg job event {_stamp(45)} (stage entered {_stamp(60)})" in body
    )
    assert calls == [f"{API}/actions/runs"]  # a filing case still probes


def test_no_live_leg_job_files_as_today(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal leg job is no leg — the stage clock alone decides."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _pass_leg_job(store, "r5", "release_deploy", status="succeeded", event_age=5)
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:deploying" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert f"clock: stage entered {_stamp(120)}" in body


def test_another_stages_leg_job_does_not_count(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``deploying`` waits on ``release_deploy``: a live
    ``ci_verdict_poll`` under the pass is not this stage's leg."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _pass_leg_job(store, "r5", "ci_verdict_poll", event_age=5)
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:deploying" in _tags(store, asks[0])


def test_an_unreadable_leg_read_files_no_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job-activity read that errors mints no ask — one note, the same
    fail-quiet rule as an unreadable row. Inside the deadline the read
    never runs (it can only lower the judged elapsed), so a broken read
    emits no note during a healthy stage."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    root = T.mint_release_pass(store).root_id
    store.stamp_ref_meta(
        root, {"release": {"rN": "r5", "sha": STUCK_BASE, "range": "aa..cc"}}
    )

    def boom(s: Store, root_id: int, stage: str) -> Any:
        raise OSError("job read gone")

    monkeypatch.setattr(rp, "_leg_job_activity", boom)
    notes = _watch(store)
    assert _stuck_asks(store) == []
    assert any("job activity unreadable" in n for n in notes)
    # 30m in on a 60m mark — the read is skipped, the broken read silent.
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(30), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    notes = _watch(store)
    assert notes == []
    assert _stuck_asks(store) == []


def test_a_queued_leg_job_with_stale_ref_stamps_still_files(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jo584043's case: a QUEUED leg job that never ran writes no
    ``job_event`` chunks — the fallback is the job ref's own
    ``created_at``/``updated_at``, fixed stamps. Older than the mark
    they cannot hold the stage: the elapsed grows and the ask files."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(180), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _pass_leg_job(store, "r5", "release_deploy", job_age=120)
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:deploying" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert "capped:" not in body
    assert (
        f"clock: last leg job event {_stamp(120)} (stage entered {_stamp(180)})" in body
    )


def test_activity_extension_is_capped_at_three_marks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leg re-queued forever without progress must still file: past
    3x the stage's mark the stage clock judges and the ask says
    ``capped: leg active but stage exceeded 3× its mark``."""
    psettings.set_setting("release.repo", REPO, store=store)
    calls = _fake_github(monkeypatch, runs={"workflow_runs": []})
    # cut's mark is 30m; 80m in is inside the 90m cap — a live leg
    # still suppresses (and spends no runs probe).
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(80)), store=store
    )
    _pass_leg_job(store, "r5", "ci_verdict_poll", event_age=5)
    assert _watch(store) == []
    assert _stuck_asks(store) == []
    assert calls == []
    # 100m in — past the cap — the same fresh leg can no longer hold it.
    psettings.set_setting(
        "release.round", _stuck_row("cut", state_at=_stamp(100)), store=store
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:cut" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert "capped: leg active but stage exceeded 3× its mark" in body
    assert "clock: stage entered" in body
    assert calls == [f"{API}/actions/runs"]


def test_the_cap_uses_the_stages_own_mark(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2h-mark stage caps at 6h: a live leg at 5h59 still holds it,
    at 6h1 the ask files with the ``capped:`` line."""
    _fake_github(monkeypatch)
    psettings.set_setting(rp.STUCK_SETTING, json.dumps({"deploying": 120}), store=store)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(359), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _pass_leg_job(store, "r5", "release_deploy", event_age=5)
    assert _watch(store) == []
    assert _stuck_asks(store) == []
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(361), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _watch(store)
    asks = _stuck_asks(store)
    assert len(asks) == 1
    assert "release-stuck:r5:deploying" in _tags(store, asks[0])
    body = _ask_body(store, asks[0])
    assert "capped: leg active but stage exceeded 3× its mark" in body


def test_a_deploying_leg_waiting_files_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live ``release_deploy`` whose job ref moved minutes ago counts
    as activity even with no event chunk. A claim does NOT stamp
    ``refs.updated_at`` — claim/lease/set_meta are raw meta UPDATEs —
    so the fixture stamps the ref's ``created_at``/``updated_at`` by
    hand to model ref-update activity."""
    _fake_github(monkeypatch)
    psettings.set_setting(
        "release.round",
        _stuck_row("deploying", state_at=_stamp(120), deploy={"sha": STUCK_BASE}),
        store=store,
    )
    _pass_leg_job(store, "r5", "release_deploy", job_age=10)
    assert _watch(store) == []
    assert _stuck_asks(store) == []


# ── the engine-review gate (td574937 E1) ───────────────────────────
#
# A release never deploys an engine land its reviewer has not read (the
# release train, T4: the gate holds the deploy, never the pass or its
# cut — ``_gate`` below reads its lines as the poll writes them):
# the gate enumerates every engine-stamped ``land_slice`` job that
# SUCCEEDED (``meta.landed_sha`` not null — a failed land has no key, a
# no-diff land a JSON null) and asks whether a reviewer covers the landed
# todo — ``generate.from`` names the todo or its parent, matching
# ``plan_dispatch._review_targets``. A covering reviewer closed ``LAND``
# or ``FIX`` with a landed fix clears the land; an open one under a live
# root waits (a ``release-review-wait:td<id>`` ask files after 24 h);
# anything else — no covering reviewer, a dead verdict or a dead root —
# can never resolve, so it blocks and files the ask at once. An answered
# ask clears that one land. The gate sits with the DB gates, before any
# GitHub read.


def _reviewer(
    store: Store,
    from_id: int,
    *,
    status: str = "open",
    review: dict[str, Any] | None = None,
    parent_id: int | None = None,
) -> int:
    meta: dict[str, Any] = {"generate": {"role": "reviewer", "from": f"td{from_id}"}}
    if review is not None:
        meta["review"] = review
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=f"review td{from_id}",
        meta=meta,
        parent_id=parent_id,
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    return int(ref.id)


def _engine_land(
    store: Store,
    todo_id: int,
    *,
    paths: list[str] | None = None,
    hours_ago: float | None = None,
    landed: bool | None = True,
    sha: str | None = None,
) -> int:
    """A ``land_slice`` job for ``td<todo_id>``; ``paths`` is the
    ``meta.engine_paths`` stamp (``None`` = a pre-E1 job, no stamp).
    ``landed``: True stamps ``landed_sha = sha`` (a succeeded land — the
    only kind that can block), None a JSON null (a no-diff land) and
    False no key at all (a failed land). A succeeded land also puts its
    commit in the fake cut range: the gate reads lands from git
    (gr597625) — the job is only the sha → todo lookup. ``sha`` defaults
    to a realistic, per-todo sha; pass one to position the land on a
    fake compare's commit list (gr592271)."""
    sha = sha or (f"{todo_id:x}" + "f" * 40)[:40]
    meta: dict[str, Any] = {
        "job_type": "land_slice",
        "params": {"todo_id": todo_id},
    }
    if paths is not None:
        meta["engine_paths"] = paths
    if landed is not False:
        meta["landed_sha"] = sha if landed else None
    ref = store.insert_ref(kind="job", slug=None, title="land_slice", meta=meta)
    if landed is True:
        _GIT_LANDS.append(
            rp._GitLand(
                sha=sha,
                subject="",
                committed_at=NOW - timedelta(hours=hours_ago or 0),
                paths=tuple(paths or []),
            )
        )
    if hours_ago is not None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = %s WHERE ref_id = %s",
                (NOW - timedelta(hours=hours_ago), ref.id),
            )
    return int(ref.id)


def _landed_comment(store: Store, todo_id: int, sha: str = "abc1234") -> None:
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=1,
                text=f"landed: {sha} — fix",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )


def _review_wait_asks(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value LIKE 'release-review-wait:%'
             ORDER BY r.ref_id
            """
        ).fetchall()
    return [int(r[0]) for r in rows]


def _slicer_child(store: Store) -> tuple[int, int]:
    """A slicer todo and one child slice — the reviewer's ``from`` names
    the slicer; the land job names the child."""
    s = store.insert_ref(kind="todo", slug=None, title="slicer", meta=None)
    c = store.insert_ref(
        kind="todo", slug=None, title="slice", meta=None, parent_id=int(s.id)
    )
    return int(s.id), int(c.id)


def _gate(
    store: Store, *, now: datetime = NOW, dry_run: bool = False
) -> tuple[str, list[str]]:
    """The engine-review gate as the poll writes it — the mint-and-ask
    read (``file_asks``) over main ahead of prod: ``(the joined blocker
    lines, notes)``, ``""`` when every land is cleared. These lines hold
    the deploy of the release carrying the lands (T4), never a pass."""
    lines, notes = rp.engine_review_blockers(
        store, now=now, file_asks=True, dry_run=dry_run
    )
    return "; ".join(lines), notes


def test_an_open_engine_reviewer_holds_the_gate_but_not_the_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release train T4: a first cut cuts origin/main's tip and needs no
    reviews — the pending land is a note on the mint; the gate's line
    (the deploy's hold) still names it."""
    calls = _fake_github(monkeypatch)
    _engine_land(store, 42, paths=["scripts/ship"])
    rid = _reviewer(store, 42)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None, res.blocker
    target = R.release_target_of(store, res.minted)
    assert target is not None and target["sha"] == MAIN_SHA
    assert any("td42" in n and "hold the deploy, not the cut" in n for n in res.notes)
    assert calls == [
        f"{API}/compare/prod...{MAIN_SHA}",
        f"{API}/git/matching-refs/heads/release/",
    ]
    blocker, _notes = _gate(store)
    assert f"td{rid}" in blocker and "td42" in blocker and "next:" in blocker


def test_a_slicer_reviewer_covers_its_childs_land(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``generate.from`` names the slicer; the land job names the slice —
    one level up ``parent_id`` is still coverage."""
    slicer, child = _slicer_child(store)
    _engine_land(store, child, paths=["scripts/ship"])
    rid = _reviewer(store, slicer)
    blocker, _notes = _gate(store)
    assert f"td{rid}" in blocker and f"td{child}" in blocker


def test_reviewer_closed_land_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine_land(store, 42, paths=["scripts/ship"])
    _reviewer(store, 42, status="done", review={"verdict": "LAND"})
    assert _gate(store)[0] == ""


def test_the_pass_cuts_the_pinned_tip_over_a_pending_review(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release train T4 (replaces gr592271's cut below the oldest
    pending land): main = prod + [A reviewed, B pending] — the pass mints
    at the pinned tip B, recording the whole window; B's review holds
    the deploy instead."""
    a, b = "a" * 40, "b" * 40
    _GIT_PIN[0] = b
    _fake_github(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "commits": [{"sha": a}, {"sha": b}],
        },
    )
    _engine_land(store, 42, paths=["scripts/ship"], sha=a)
    _reviewer(store, 42, status="done", review={"verdict": "LAND"})
    _engine_land(store, 43, paths=["scripts/ship"], sha=b)
    _reviewer(store, 43)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None, res.blocker
    assert f"release-main:{b[:12]}" in _tags(store, res.minted)
    target = R.release_target_of(store, res.minted)
    assert target is not None
    assert target["sha"] == b and target["upto"] == [a[:12], b[:12]]
    assert any("td43" in n for n in res.notes)


def test_the_pass_pins_the_cut_to_the_scanned_head(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td601005: main moved after the scan — the recorded target is
    the scanned head, never the newer tip, so a land reaching main
    mid-pass cannot ride the recorded sha. The pass never reads the
    main ref at all."""
    a = "a" * 40
    _GIT_PIN[0] = a
    calls = _fake_github(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "commits": [{"sha": a}],
        },
        # a newer head than the pin — must go unread
        main_ref={"ref": "refs/heads/main", "object": {"sha": "e" * 40}},
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None, res.blocker
    assert f"release-main:{a[:12]}" in _tags(store, res.minted)
    target = R.release_target_of(store, res.minted)
    assert target is not None
    assert target["sha"] == a and target["upto"] == [a[:12]]
    assert f"{API}/git/ref/heads/main" not in calls
    assert f"{API}/compare/prod...{a}" in calls


def test_an_empty_compare_records_the_scans_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td601285: no commit list in the compare — the scan's own
    ordering stands in for the recorded ``upto`` window."""
    a, c = "a" * 40, "c" * 40
    _GIT_PIN[0] = c
    _fake_github(
        monkeypatch,
        compare={"status": "ahead", "ahead_by": 2, "behind_by": 0},
    )
    _engine_land(store, 42, paths=["scripts/ship"], sha=a)
    _GIT_LANDS.append(
        rp._GitLand(sha=c, subject="", committed_at=None, paths=("docs/a.md",))
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None, res.blocker
    target = R.release_target_of(store, res.minted)
    assert target is not None
    assert target["sha"] == c and target["upto"] == [a[:12], c[:12]]


def test_fix_reviewer_with_an_unlanded_fix_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine_land(store, 42, paths=["src/precis/plans/lanes.py"])
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    rid = _reviewer(
        store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)}
    )
    blocker, _notes = _gate(store)
    assert f"td{rid}" in blocker and f"td{fix.id}" in blocker


def test_fix_reviewer_with_a_landed_fix_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's own verdict; whether the release carries the fix is
    the deploy hold's check (``rp.deploy_review_blockers``)."""
    _engine_land(store, 42, paths=["src/precis/plans/lanes.py"])
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _landed_comment(store, int(fix.id))
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)})
    assert _gate(store)[0] == ""


def test_fix_reviewer_without_a_task_asks_at_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIX naming no task can never resolve — the wait ask files at
    once, not after 24 h."""
    _engine_land(store, 42, paths=["scripts/ship"])
    _reviewer(store, 42, status="done", review={"verdict": "FIX"})
    blocker, _notes = _gate(store)
    assert "without a usable verdict" in blocker
    assert len(_review_wait_asks(store)) == 1


def test_a_reviewer_closed_without_a_verdict_asks_at_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i, status in enumerate(("won't-do", "auto-timeout", "done")):
        _engine_land(store, 42 + i, paths=["scripts/ship"])
        _reviewer(store, 42 + i, status=status)  # no meta.review
    blocker, _notes = _gate(store)
    assert "without a usable verdict" in blocker
    assert len(_review_wait_asks(store)) == 3


def test_a_failed_land_never_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a SUCCEEDED land holds the release: a failed job has no
    ``landed_sha`` key and a no-diff land stamps it JSON null."""
    _engine_land(store, 42, paths=["scripts/ship"], landed=False)
    _engine_land(store, 43, paths=["scripts/ship"], landed=None)
    _reviewer(store, 42)
    _reviewer(store, 43)
    assert _gate(store)[0] == ""


def test_a_later_failed_land_does_not_mask_an_engine_land(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate scans every job, not the newest: a failed re-land on top
    of a succeeded engine land leaves the engine land blocking."""
    _engine_land(store, 42, paths=["scripts/ship"])
    _engine_land(store, 42, paths=["scripts/ship"], landed=False)
    rid = _reviewer(store, 42)
    assert f"td{rid}" in _gate(store)[0]


def test_the_fix_tasks_own_engine_land_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fix task's own engine land has no reviewer covering it (its
    parent is the plan, not the slicer): the gate mints the sha-keyed
    reviewer for it and blocks, even though the review it answered is
    satisfied."""
    _engine_land(store, 42, paths=["scripts/ship"])
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _engine_land(store, int(fix.id), paths=["src/precis/plans/lanes.py"])
    _landed_comment(store, int(fix.id))
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)})
    blocker, _notes = _gate(store)
    assert "no reviewer" in blocker and f"td{fix.id}" in blocker
    keys = [
        str((m.get("generate") or {}).get("from")) for _r, m in rp._reviewer_rows(store)
    ]
    assert sorted(keys) == [f"sha:{(f'{int(fix.id):x}' + 'f' * 40)[:12]}", "td42"]
    assert _review_wait_asks(store) == []


def test_an_engine_land_with_no_reviewer_mints_one_at_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No covering reviewer at all: the gate mints the sha-keyed
    reviewer at once — the land still holds, on the minted node."""
    job = _engine_land(store, 42, paths=["scripts/ship"])
    blocker, _notes = _gate(store)
    assert "no reviewer" in blocker
    assert "td42" in blocker and f"jo{job}" in blocker
    [(rid, meta)] = rp._reviewer_rows(store)
    assert str((meta.get("generate") or {}).get("from")).startswith("sha:")
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id is not None
    assert _review_wait_asks(store) == []


# ── the gate mints its own reviewer (td584381 S1b) ────────────────
#
# Fix, revert and plain lands mint no reviewer, so "no covering
# reviewer" used to mean "ask a human". Now the gate mints one itself —
# a plan reviewer node under the landed todo's live plan root, run by
# ``plan_dispatch``'s review machinery on a lane of a vendor that did
# not build the land — and files the ask only when no minted reviewer
# could run: no enabled review-fit lane or a mint that raised. A land
# with no live plan root to hang the reviewer under (a dead root this
# gate would itself judge hopeless, or no ``plan:`` tag at all) gets
# the sha-keyed reviewer under a fresh root instead (td599062).


def _plan_landed(store: Store) -> tuple[int, int]:
    """An open plan root with one landed child todo -> ``(root, todo)``:
    the build is ``done`` (the land closed it) and carries the
    ``plan:<root>`` tag every plan-minted node gets."""
    root = store.insert_ref(kind="todo", slug=None, title="the plan", meta=None)
    build = store.insert_ref(
        kind="todo",
        slug=None,
        title="the build",
        meta=None,
        parent_id=int(root.id),
    )
    store.add_tag(int(build.id), Tag.open(f"plan:{int(root.id)}"), set_by="agent")
    _close(store, int(build.id))
    return int(root.id), int(build.id)


def _todo_text(store: Store, todo_id: int) -> str:
    return "\n".join(
        str(c.text) for c in store.chunks.list_chunks_for_ref(todo_id) if c.text
    )


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _children(store: Store, todo_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _blocked_by_ids(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'blocked-by' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _created_at(store: Store, ref_id: int, stamp: datetime) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (stamp, ref_id),
        )


def _review_job(store: Store, todo_id: int) -> int:
    """A succeeded ``run_slice`` job under the todo — the one a review
    verdict is read off by the real judge."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="run_slice",
        meta={"job_type": "run_slice", "params": {"todo_id": todo_id}},
        parent_id=todo_id,
    )
    store.add_tag(
        int(ref.id),
        Tag.closed("STATUS", "succeeded"),
        set_by="system",
        replace_prefix=True,
    )
    return int(ref.id)


def _todo_comment(store: Store, todo_id: int, text: str) -> None:
    ords = [int(c.ord) for c in store.chunks.list_chunks_for_ref(todo_id)]
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=max(ords, default=0) + 1,
                text=text,
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )


def test_an_engine_land_with_no_reviewer_mints_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine land under a live plan root with an enabled review
    lane gets its reviewer minted — one node, no ask."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)  # the seed table carries review-fit lanes
    root, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    _landed_comment(store, build)
    lines, notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, meta)] = rp._reviewer_rows(store)
    assert meta["generate"] == {"role": "reviewer", "from": f"td{build}"}
    assert meta["task_class"] == T.REVIEW_TASK_CLASS
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id == root
    assert f"plan:{root}" in _tags(store, rid)
    assert _review_wait_asks(store) == []
    blocker = "; ".join(lines)
    assert f"td{rid}" in blocker and f"td{build}" in blocker
    assert any(f"minted reviewer td{rid} for td{build}" in n for n in notes)
    # The dispatch tick picks it up — a generator with no children under
    # an open root, pinned to a review lane; no plan_dispatch change.
    res = pd.run_pass(store, dry_run=True)
    assert any(f"td{rid}: would dispatch run_slice" in n for n in res.notes)


def test_the_reviewer_mint_is_idempotent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The minted reviewer is covering on the next tick: no second
    node, no ask."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 1
    assert _review_wait_asks(store) == []


def test_no_enabled_review_lane_files_the_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No enabled lane fits ``review``: the minted node could never
    run, so the gate asks — the body names the reason."""
    _fake_github(monkeypatch)
    use_lanes(
        monkeypatch,
        L.Lane("code-only", "claude_p", "s", 1, frozenset({"code"}), vendor="x"),
    )
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert rp._reviewer_rows(store) == []
    [ask_id] = _review_wait_asks(store)
    assert "no enabled lane" in _todo_text(store, ask_id)


def test_a_land_under_no_plan_root_mints_a_sha_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone landed todo tops its own parent chain — no plan
    root to hang a td-keyed reviewer under, so the gate mints the
    sha-keyed one under a fresh root, keyed on the commit (td599062)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    lone = store.insert_ref(kind="todo", slug=None, title="bare build", meta=None)
    _engine_land(store, int(lone.id), paths=["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    sha = (f"{int(lone.id):x}" + "f" * 40)[:12]
    [(rid, meta)] = rp._reviewer_rows(store)
    assert meta["generate"] == {"role": "reviewer", "from": f"sha:{sha}"}
    assert _review_wait_asks(store) == []
    # The sha key covers the td unit next tick: judged, no second
    # node, no ask.
    lines, _notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 1
    assert _review_wait_asks(store) == []
    assert f"td{rid}" in "; ".join(lines)


def test_a_land_under_a_dead_root_mints_a_sha_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A td-keyed reviewer hung under the closed plan root would be
    judged hopeless by this same gate — minting it buys nothing, so
    the gate mints the sha-keyed reviewer under a FRESH root instead
    (td599062)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    root, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    _close(store, root)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    sha = (f"{build:x}" + "f" * 40)[:12]
    [(rid, meta)] = rp._reviewer_rows(store)
    assert meta["generate"]["from"] == f"sha:{sha}"
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id is not None
    assert ref.parent_id != root  # a fresh root, not the dead one
    assert _review_wait_asks(store) == []


def test_no_mint_without_file_asks_dry_run_or_factory_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    lines, notes = rp.engine_review_blockers(store, now=NOW)  # release_cut's read
    assert rp._reviewer_rows(store) == []
    assert _review_wait_asks(store) == []
    lines, notes = rp.engine_review_blockers(
        store, now=NOW, file_asks=True, dry_run=True
    )
    assert rp._reviewer_rows(store) == []
    assert _review_wait_asks(store) == []
    assert any("would mint" in n for n in notes)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    lines, notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert rp._reviewer_rows(store) == []
    assert _review_wait_asks(store) == []
    # plan_dispatch's own switch is off: the pre-S1b path — the ask.
    assert any("would file release-review-wait" in n for n in notes)


def test_the_minted_reviewers_land_verdict_clears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real judge (``plan_dispatch._step_reviews``): the
    minted reviewer's LAND verdict closes it done and the gate clears."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    _landed_comment(store, build)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    _review_job(store, rid)
    _todo_comment(store, rid, "review: LAND")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, rid) == "done"
    assert _gate(store)[0] == ""


def test_a_minted_reviewers_revert_does_not_loop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REVERT on the minted reviewer mints ONE revert task across two
    consecutive ticks: the reviewer is ``blocked-by`` the landed todo —
    ``_review_revert`` reopens it behind the revert task — so the
    reviewer is not re-dispatched while the rebuild is open (B1)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    root, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    _landed_comment(store, build)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    assert _blocked_by_ids(store, rid) == [build]
    _review_job(store, rid)
    _todo_comment(store, rid, "review: REVERT — breaks the public API")
    res = pd.run_pass(store)
    assert res.reviews == 1
    reverts = [c for c in _children(store, root) if c not in (build, rid)]
    assert len(reverts) == 1
    # The land is reopened behind the revert; only the revert runs.
    assert _status(store, build) == "open"
    assert _blocked_by_ids(store, build) == [reverts[0]]
    assert res.dispatched == 1
    res = pd.run_pass(store)
    assert res.reviews == 0
    assert [c for c in _children(store, root) if c not in (build, rid)] == reverts
    assert len(_jobs_under(store, rid)) == 1  # not re-dispatched


def test_a_minted_reviewers_fix_landed_fix_clears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX through the real judge: one fix task mints and the reviewer
    closes; once the fix carries a ``landed:`` comment the gate reads
    the land satisfied."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    root, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    _landed_comment(store, build)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    _review_job(store, rid)
    _todo_comment(store, rid, "review: FIX — a.py misses the empty case")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, rid) == "done"
    (fix,) = [c for c in _children(store, root) if c not in (build, rid)]
    _landed_comment(store, fix)
    blockers, _notes = rp.engine_review_blockers(store, now=NOW)
    assert blockers == []


def test_the_wait_clock_counts_from_the_newest_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A land first seen 30 h after it landed gets its reviewer minted
    this tick: the 24 h clock runs from the newer of the land and the
    reviewer, so the next tick files no ask — and the same reviewer
    aged 25 h does file it (S1)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"], hours_ago=30)
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    _created_at(store, rid, NOW - timedelta(minutes=1))
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert _review_wait_asks(store) == []
    _created_at(store, rid, NOW - timedelta(hours=25))
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(_review_wait_asks(store)) == 1


def test_a_land_under_a_non_plan_epic_mints_a_sha_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landed todo whose parent is a plain epic — no ``plan:<id>``
    tag of its own — has no plan root: the gate mints the sha-keyed
    reviewer under a FRESH root; the epic never becomes a plan root
    and its other leaf stays outside every plan subtree (S2)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    epic = store.insert_ref(kind="todo", slug=None, title="the epic", meta=None)
    build = store.insert_ref(
        kind="todo",
        slug=None,
        title="the build",
        meta=None,
        parent_id=int(epic.id),
    )
    leaf = store.insert_ref(
        kind="todo",
        slug=None,
        title="another leaf",
        meta=None,
        parent_id=int(epic.id),
    )
    _engine_land(store, int(build.id), paths=["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, meta)] = rp._reviewer_rows(store)
    assert str((meta.get("generate") or {}).get("from")).startswith("sha:")
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id != int(epic.id)
    assert _review_wait_asks(store) == []
    assert not any(v.startswith("plan:") for v in _tags(store, int(epic.id)))
    # The minted reviewer is the only thing the tick dispatches — the
    # epic's other leaf is no plan's task.
    res = pd.run_pass(store, dry_run=True)
    dispatch = [n for n in res.notes if "would dispatch" in n]
    assert len(dispatch) == 1 and f"td{rid}" in dispatch[0]
    assert all(f"td{int(leaf.id)}" not in n for n in res.notes)


def test_the_plan_dispatch_kill_switch_files_the_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan_dispatch off by its own switch — the service_config prio-0
    row or PRECIS_FACTORY=off — mints nothing and takes the pre-S1b
    path: the review-wait ask (S3)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) VALUES (%s, %s, 0)",
            ("*", pd.SERVICE),
        )
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert rp._reviewer_rows(store) == []
    assert len(_review_wait_asks(store)) == 1
    _root2, build2 = _plan_landed(store)
    _engine_land(store, build2, paths=["scripts/ship"])
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    _lines, notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert rp._reviewer_rows(store) == []
    # The env switch makes the filing itself a would-file; the path is
    # the ask's, not the mint's.
    assert any("would file release-review-wait" in n for n in notes)
    assert len(_review_wait_asks(store)) == 1


def test_the_gate_mints_at_most_two_reviewers_per_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The historical backlog drains two mints a tick, not a burst:
    five uncovered lands, one pass mints two, the next two more, and
    no ask files meanwhile."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    for _ in range(5):
        _root_id, build = _plan_landed(store)
        _engine_land(store, build, paths=["scripts/ship"])
    blockers, _notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 2
    assert _review_wait_asks(store) == []
    assert sum("reviewer mint deferred" in b for b in blockers) == 3
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 4
    assert _review_wait_asks(store) == []


def test_the_cut_read_names_the_gate_not_an_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``file_asks=False`` (the deploy's read): no ask exists to answer —
    the blocker names the gate, not a filing that never happened."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _root_id, build = _plan_landed(store)
    _engine_land(store, build, paths=["scripts/ship"])
    blockers, _notes = rp.engine_review_blockers(store, now=NOW)
    [line] = blockers
    assert "the review gate holds the deploy" in line
    assert "review-wait ask" not in line


def test_an_answered_wait_ask_clears_the_land(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ruling answered on the ask — an ``answer:`` comment — clears
    that one land; the gate's next read is clear."""
    _engine_land(store, 42, paths=["scripts/ship"], hours_ago=25)
    rid = _reviewer(store, 42)
    # The reviewer has sat open since the land: the 24 h clock (the
    # newer of land and reviewer) is due.
    _created_at(store, rid, NOW - timedelta(hours=25))
    assert _gate(store)[0] != ""
    [ask_id] = _review_wait_asks(store)
    store.chunks.insert_chunks(
        ask_id,
        [
            ChunkInsert(
                ord=1,
                text="answer: acknowledged",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )
    _close(store, ask_id)
    assert _gate(store)[0] == ""


def test_a_wait_ask_answered_in_its_body_clears_the_land(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr645663: ``precis-todo-tree-help``'s ask close records the answer
    in the ask's details body (``edit(mode='replace', body='…\\nAnswer:
    …')``), not as an ``answer:`` comment — that clears the land too:
    the ask stays deduped and the gate's next read is clear."""
    _engine_land(store, 42, paths=["scripts/ship"], hours_ago=25)
    rid = _reviewer(store, 42)
    # The reviewer has sat open since the land: the 24 h clock (the
    # newer of land and reviewer) is due.
    _created_at(store, rid, NOW - timedelta(hours=25))
    assert _gate(store)[0] != ""
    [ask_id] = _review_wait_asks(store)
    # The documented body close: details retained, answer a later line.
    store.chunks.replace_body_chunk(
        ask_id,
        "Existing details retained here.\n\nAnswer: acknowledged",
        chunk_kind="todo_body",
    )
    _close(store, ask_id)
    assert _gate(store)[0] == ""
    assert _review_wait_asks(store) == [ask_id]


def test_a_wait_ask_body_without_an_answer_line_does_not_clear(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr645663: the body read is line-level — the ask's own filed body
    (which mentions answering) does not self-answer, and a closed ask
    whose body holds no ``answer:`` line is still refiled."""
    use_lanes(
        monkeypatch,
        L.Lane("code-only", "claude_p", "s", 1, frozenset({"code"}), vendor="x"),
    )
    _engine_land(store, 42, paths=["scripts/ship"])
    assert _gate(store)[0] != ""
    [first] = _review_wait_asks(store)
    _close(store, first)  # gate-written body — mentions answering, no answer: line
    assert _gate(store)[0] != ""
    asks = _review_wait_asks(store)
    assert len(asks) == 2 and first in asks


def test_a_terminal_unlanded_fix_asks_at_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIX whose fix task ended without landing can never resolve — the
    ask files without the 24 h wait, naming what to do."""
    _engine_land(store, 42, paths=["scripts/ship"])  # fresh land
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    store.add_tag(
        fix.id,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )
    rid = _reviewer(
        store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)}
    )
    blocker, _notes = _gate(store)
    assert f"td{rid}" in blocker and f"td{fix.id}" in blocker
    assert "no `landed: <sha>` record" in blocker
    assert len(_review_wait_asks(store)) == 1


def test_an_open_reviewer_under_a_dead_root_asks_at_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open reviewer whose plan root is cancelled can never finish —
    ask at once, not after 24 h."""
    _engine_land(store, 42, paths=["scripts/ship"])  # fresh land
    root = store.insert_ref(kind="todo", slug=None, title="the plan", meta=None)
    store.add_tag(
        root.id,
        Tag.closed("STATUS", "cancelled"),
        set_by="system",
        replace_prefix=True,
    )
    rid = _reviewer(store, 42, parent_id=int(root.id))
    blocker, _notes = _gate(store)
    assert "cancelled root" in blocker and f"td{rid}" in blocker
    assert len(_review_wait_asks(store)) == 1


def test_non_engine_land_does_not_block(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine_land(store, 42, paths=["docs/a.md", "src/precis/foo.py"])
    _reviewer(store, 42)
    assert _gate(store)[0] == ""


def test_a_land_with_no_engine_stamp_counts_as_non_engine(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-E1 ``land_slice`` job carries no ``engine_paths`` stamp; it
    reads as non-engine and never blocks."""
    _engine_land(store, 42)  # no paths → no stamp
    _reviewer(store, 42)
    assert _gate(store)[0] == ""


def test_a_review_wait_under_24h_blocks_without_an_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine_land(store, 42, paths=["scripts/ship"], hours_ago=2)
    _reviewer(store, 42)
    assert _gate(store)[0] != ""
    assert _review_wait_asks(store) == []


def test_a_review_wait_over_24h_files_one_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engine_land(store, 42, paths=["scripts/ship"], hours_ago=25)
    rid = _reviewer(store, 42)
    # Open since the land: the wait clock (the newer stamp) is due.
    _created_at(store, rid, NOW - timedelta(hours=25))
    assert _gate(store)[0] != ""
    asks = _review_wait_asks(store)
    assert len(asks) == 1
    ref = store.get_ref(kind="todo", id=asks[0])
    assert ref is not None
    ask = ref.meta["ask"]
    assert ask["kind"] == "ruling"
    assert ask["options"] == ["acknowledged", "false alarm"]
    assert ask["blocks"] == "nothing"
    assert f"td{rid}" in ask["context"] and "td42" in ask["context"]
    assert "holds the deploy" in ask["context"]
    tags = _tags(store, asks[0])
    # The ask keys on the LAND's todo: answering it clears that one land.
    assert {"ask-user", "showstopper", "release-review-wait:td42"} <= tags
    # Deduped while the ask is open: a second pass files nothing new.
    _gate(store)
    assert _review_wait_asks(store) == asks
    # Closed WITHOUT an ``answer:`` comment the ask does not hold: the
    # next pass refiles it and the blocker names the new ask's id.
    _close(store, asks[0])
    blocker, _notes = _gate(store)
    more = _review_wait_asks(store)
    assert len(more) == 2 and asks[0] in more
    second = next(a for a in more if a != asks[0])
    assert f"td{second}" in blocker


def test_a_wait_ask_closed_unanswered_is_refiled(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the ruling ask without an ``answer:`` comment does not
    clear the land: the gate files a new ask for it and the blocker
    names that ask's id. The land is unmintable on a code-only lane
    table, so the ask path is what runs."""
    use_lanes(
        monkeypatch,
        L.Lane("code-only", "claude_p", "s", 1, frozenset({"code"}), vendor="x"),
    )
    _engine_land(store, 42, paths=["scripts/ship"])
    _gate(store)
    [first] = _review_wait_asks(store)
    _close(store, first)  # closed with no ``answer:`` comment
    blocker, _notes = _gate(store)
    asks = _review_wait_asks(store)
    assert len(asks) == 2 and first in asks
    second = next(a for a in asks if a != first)
    assert f"td{second}" in blocker
    assert "release-review-wait:td42" in _tags(store, second)
    # The still-open ask dedupes the next pass, and the no-filing read
    # (the deploy's) names the live ask's id too.
    _gate(store)
    assert _review_wait_asks(store) == asks
    lines, _notes = rp.engine_review_blockers(store, now=NOW)
    assert lines and f"td{second}" in "; ".join(lines)


def test_review_wait_ask_not_filed_under_dry_run_or_factory_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    _engine_land(store, 42, paths=["scripts/ship"], hours_ago=25)
    rid = _reviewer(store, 42)
    _created_at(store, rid, NOW - timedelta(hours=25))
    res = rp.run_pass(store, repo=REPO, dry_run=True, now=NOW)
    assert _review_wait_asks(store) == []
    assert any("would file" in n for n in res.notes)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert _review_wait_asks(store) == []
    assert any("would file" in n for n in res.notes)


def test_engine_review_gate_unreadable_fails_closed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)

    def boom(s: Store) -> list[tuple[str, int, int]]:
        raise OSError("db gone")

    monkeypatch.setattr(rp, "_landed_jobs", boom)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is None
    assert res.blocker is not None and "unreadable" in res.blocker
    assert "fail closed" in res.blocker
    assert calls == []  # the gate fails closed before any GitHub read


# ── the cut range comes from git, not jobs (gr597625) ─────────────
#
# A ``scripts/ship`` run from a tmux tree or the coordinator seat mints
# no ``land_slice`` job, so the stamped-job scan alone let a hand-landed
# engine commit ride the cut unseen (186513b6e into r257 with two FIX
# reviewers open). The gate now reads ``origin/prod..origin/main``: an
# engine commit links its todos through the stamp or the subject's
# trailing ``td`` handles; a commit nothing links — or whose linked
# todo has no live plan root (td599062) — gets a sha-keyed reviewer
# minted and holds the cut until it closes LAND.


def _hand_land(sha: str, subject: str, paths: list[str]) -> None:
    """A commit on main in the cut range with no ``land_slice`` job —
    a hand land."""
    _GIT_LANDS.append(
        rp._GitLand(
            sha=sha,
            subject=subject,
            committed_at=NOW,
            paths=tuple(paths),
        )
    )


def test_a_hand_engine_land_with_a_fix_reviewer_blocks_the_cut(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident shape: an engine commit on main, no ``land_slice``
    job, the todo its subject names carrying a FIX reviewer whose fix
    has not landed — the gate holds it."""
    _hand_land(
        "186513b6e12f" + "0" * 28,
        "fix(plan): strictest verdict wins (gr574380, td42)",
        ["src/precis/plans/lanes.py"],
    )
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    rid = _reviewer(
        store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)}
    )
    blocker, _notes = _gate(store)
    assert f"td{rid}" in blocker and "td42" in blocker


def test_a_hand_engine_land_with_a_land_reviewer_passes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hand_land(
        "186513b6e12f" + "0" * 28,
        "fix(plan): strictest verdict wins (td42)",
        ["src/precis/plans/lanes.py"],
    )
    _reviewer(store, 42, status="done", review={"verdict": "LAND"})
    assert _gate(store)[0] == ""


def test_a_non_engine_hand_land_never_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hand_land(
        "9" * 40,
        "docs(backlog): tidy up",
        ["docs/a.md", "src/precis/foo.py"],
    )
    assert _gate(store)[0] == ""


def test_a_hand_land_naming_a_rootless_todo_gets_a_sha_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The td599062 brief verbatim: a hand land whose subject names a
    todo with no live plan root — nothing to hang a td-keyed reviewer
    under — used to file only the ask. The gate mints the sha-keyed
    reviewer under a fresh root, and that key covers the td unit."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    lone = store.insert_ref(kind="todo", slug=None, title="bare build", meta=None)
    sha = "186513b6e12f" + "0" * 28
    _hand_land(
        sha,
        f"fix(ship): tighten the gate (td{int(lone.id)})",
        ["scripts/ship"],
    )
    lines, notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, meta)] = rp._reviewer_rows(store)
    assert meta["generate"] == {"role": "reviewer", "from": f"sha:{sha[:12]}"}
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id is not None
    assert _review_wait_asks(store) == []
    blocker = "; ".join(lines)
    assert f"td{int(lone.id)}" in blocker and f"td{rid}" in blocker
    assert any(f"minted reviewer td{rid} for {sha[:12]}" in n for n in notes)
    # The sha key covers the td unit next tick: judged, no re-mint,
    # no ask.
    lines, _notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 1
    assert _review_wait_asks(store) == []
    assert f"td{rid}" in "; ".join(lines)


def test_a_malformed_sha_from_does_not_shadow_the_mint(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer whose ``generate.from`` is no well-formed sha key
    covers nothing — and must not shadow the mint's dedupe read either
    (the unanchored prefix match would have wedged every sha mint)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    store.insert_ref(
        kind="todo",
        slug=None,
        title="odd reviewer",
        meta={"generate": {"role": "reviewer", "from": "sha"}},
    )
    _hand_land("a" * 40, "fix(ship): tighten the gate", ["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    keys = [
        str((m.get("generate") or {}).get("from")) for _r, m in rp._reviewer_rows(store)
    ]
    assert sorted(keys) == ["sha", f"sha:{'a' * 12}"]
    assert _review_wait_asks(store) == []


def test_a_stamp_less_engine_commit_gets_one_reviewer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine commit no job stamp or subject handle links a todo to:
    the gate mints one sha-keyed reviewer under a fresh phase root —
    and exactly one, across ticks."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    sha = "186513b6e12f34567890abcdef1234567890abcd"
    _hand_land(sha, "fix(ship): tighten the gate", ["scripts/ship"])
    lines, notes = rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, meta)] = rp._reviewer_rows(store)
    assert meta["generate"] == {"role": "reviewer", "from": f"sha:{sha[:12]}"}
    assert meta["task_class"] == T.REVIEW_TASK_CLASS
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id is not None
    root = store.get_ref(kind="todo", id=ref.parent_id)
    assert root is not None and root.meta.get("auto_close") is True
    assert f"plan:{ref.parent_id}" in _tags(store, rid)
    assert _review_wait_asks(store) == []
    blocker = "; ".join(lines)
    assert sha[:12] in blocker and f"td{rid}" in blocker
    assert any(f"minted reviewer td{rid} for {sha[:12]}" in n for n in notes)
    # Next tick the minted reviewer covers: no second node, no ask.
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    assert len(rp._reviewer_rows(store)) == 1
    assert _review_wait_asks(store) == []
    # The dispatch tick picks it up on a review lane.
    res = pd.run_pass(store, dry_run=True)
    assert any(f"td{rid}: would dispatch run_slice" in n for n in res.notes)


def test_a_sha_reviewers_land_verdict_clears_the_hand_land(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real judge: the sha-keyed reviewer's LAND verdict
    closes it done and the gate clears."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _hand_land("a" * 40, "fix(ship): tighten the gate", ["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    _review_job(store, rid)
    _todo_comment(store, rid, "review: LAND")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, rid) == "done"
    assert _gate(store)[0] == ""


def test_a_sha_reviewers_revert_closes_and_the_revert_task_gates(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sha-keyed land has no rebuild to re-read: REVERT closes the
    reviewer like a FIX — no re-review of the same commit, no second
    revert — and the gate waits on the minted revert task, clearing
    once it lands (gr597625)."""
    _fake_github(monkeypatch)
    use_lanes(monkeypatch)
    _hand_land("a" * 40, "fix(ship): tighten the gate", ["scripts/ship"])
    rp.engine_review_blockers(store, now=NOW, file_asks=True)
    [(rid, _meta)] = rp._reviewer_rows(store)
    _review_job(store, rid)
    _todo_comment(store, rid, "review: REVERT - the diff is wrong")
    res = pd.run_pass(store)
    assert res.reviews == 1
    assert _status(store, rid) == "done"
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    task = (ref.meta.get("review") or {}).get("task")
    assert isinstance(task, int)
    # The reviewer stays closed — no re-dispatch, no second verdict —
    # while the gate holds the land on the unlanded revert task.
    pd.run_pass(store)
    assert _status(store, rid) == "done"
    assert len(rp._reviewer_rows(store)) == 1
    assert f"td{task}" in _gate(store)[0]
    _todo_comment(store, task, "landed: b9c8d7e6f5 — revert")
    assert _gate(store)[0] == ""


# ── gr611217: reviewers while a pass is open; FIX correctives clear ──
#
# r261: with pass td610703 open the poll returned before the gate, so
# hand lands never got a reviewer; and the FIX's own fix land read as a
# fresh uncleared unit whose span chained with the FIX span, so the
# re-cut recomputed a sha hours below origin/main with no blocker line.


def _reviewer_keys(store: Store) -> list[str]:
    return sorted(
        str((m.get("generate") or {}).get("from")) for _r, m in rp._reviewer_rows(store)
    )


def test_an_open_pass_still_mints_engine_reviewers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open pass still blocks the cut — no GitHub read, no mint —
    but the hand land on main gets its sha-keyed reviewer this tick,
    once: the next tick finds it covering and mints nothing more."""
    mint = T.mint_release_pass(store)
    calls = _fake_github(monkeypatch)
    _hand_land("d5225e775" + "0" * 31, "fix(poll): gate", ["scripts/ship"])
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"release-pass td{mint.root_id} is open"
    assert res.minted is None and calls == []
    assert _reviewer_keys(store) == ["sha:d5225e775000"]
    assert any("minted reviewer" in n for n in res.notes)
    again = rp.run_pass(store, repo=REPO, now=NOW)
    assert again.blocker == f"release-pass td{mint.root_id} is open"
    assert _reviewer_keys(store) == ["sha:d5225e775000"]


def test_the_no_release_window_still_mints_engine_reviewers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the window no pass mints and GitHub is not read, but the
    unreviewed hand land gets its reviewer now — paid before the window
    opens."""
    calls = _fake_github(monkeypatch)
    _hand_land("3919bddf6" + "0" * 31, "fix(skills): cap", ["scripts/ship"])
    res = rp.run_pass(store, repo=REPO, now=_at(4, 0))  # the default window
    assert res.blocker == "inside the no-release window 03:00-04:20Z"
    assert res.minted is None and calls == []
    assert _reviewer_keys(store) == ["sha:3919bddf6000"]
    assert not rp._release_pass_ids(store)


def test_an_open_pass_dry_run_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    T.mint_release_pass(store)
    _fake_github(monkeypatch)
    _hand_land("d5225e775" + "0" * 31, "fix(poll): gate", ["scripts/ship"])
    res = rp.run_pass(store, repo=REPO, now=NOW, dry_run=True)
    assert res.blocker is not None and "is open" in res.blocker
    assert rp._reviewer_rows(store) == []
    assert any("would mint" in n for n in res.notes)


def _fix_chain(
    store: Store, a: str, b: str, *, a_on_window: bool = True
) -> tuple[int, int]:
    """[A FIX] whose fix todo landed B — B is itself an engine land
    linked to the fix todo (the 519c3d44f / 261aa5f34 shape). Returns
    ``(fix id, FIX reviewer id)``."""
    if a_on_window:
        _engine_land(store, 42, paths=["scripts/ship"], sha=a)
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _landed_comment(store, int(fix.id), sha=b)
    rid = _reviewer(
        store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)}
    )
    _engine_land(store, int(fix.id), paths=["src/precis/plans/lanes.py"], sha=b)
    return int(fix.id), rid


def test_an_open_cut_pass_mints_reviewers_for_its_release_branch_fixes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release train T4: a fix landed on ``release/r7`` is not on main,
    so the main scan never sees it — the open pass's tick also reads
    its own release (``rp._release_lands``) and mints the fix's
    reviewer, which r7's deploy then waits on."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", "c" * 40, "9" * 12 + ".." + "c" * 12)
    _fake_github(monkeypatch)
    _REL_LANDS.append(
        rp._GitLand(
            sha="7e1ea5e00" + "0" * 31,
            subject="fix(gate): on the branch",
            committed_at=NOW,
            paths=("scripts/ship",),
        )
    )
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == f"release-pass td{mint.root_id} is open"
    assert _reviewer_keys(store) == ["sha:7e1ea5e00000"]
    [line] = rp.deploy_review_blockers(store, mint.ids["deploy"])
    assert "7e1ea5e00000" in line


def test_a_fix_chain_with_its_corrective_clears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[A FIX, B = A's corrective, C LAND, D LAND] — B rides A's
    chain: no reviewer minted for it, nothing holds, the cut is the
    pinned tip D."""
    a, b, c, d = "a" * 40, "b" * 40, "c" * 40, "e" * 40
    _GIT_PIN[0] = d
    _fake_github(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 4,
            "behind_by": 0,
            "commits": [{"sha": s} for s in (a, b, c, d)],
        },
    )
    _fix_chain(store, a, b)
    _engine_land(store, 44, paths=["scripts/ship"], sha=c)
    _reviewer(store, 44, status="done", review={"verdict": "LAND"})
    _engine_land(store, 45, paths=["scripts/ship"], sha=d)
    _reviewer(store, 45, status="done", review={"verdict": "LAND"})
    before = _reviewer_keys(store)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.minted is not None, res.blocker
    target = R.release_target_of(store, res.minted)
    assert target is not None and target["sha"] == d
    assert _reviewer_keys(store) == before  # no reviewer minted for B
    assert _gate(store)[0] == ""


def test_a_corrective_whose_fixed_land_is_on_prod_still_clears(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A already shipped; only its fix B sits ahead of prod — the FIX
    reviewer naming B's todo as its task clears B (no in-window A)."""
    b, c = "b" * 40, "c" * 40
    _fix_chain(store, "a" * 40, b, a_on_window=False)
    _engine_land(store, 44, paths=["scripts/ship"], sha=c)
    _reviewer(store, 44, status="done", review={"verdict": "LAND"})
    assert _gate(store)[0] == ""


def test_a_correctives_own_verdict_wins(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B has its own reviewer with a FIX verdict whose fix has not
    landed — that verdict, not A's chain, judges B: it holds."""
    a, b = "a" * 40, "b" * 40
    fix, _rid = _fix_chain(store, a, b)
    fix2 = store.insert_ref(kind="todo", slug=None, title="fix of fix", meta=None)
    own = _reviewer(
        store, fix, status="done", review={"verdict": "FIX", "task": int(fix2.id)}
    )
    assert f"td{own}" in _gate(store)[0]


def test_a_corrective_clears_only_its_fix_todos_unit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B's subject links ``(td<fix>, td<other>)``: B is A's recorded fix,
    but td<other> is an unreviewed engine change riding the same
    commit — the (B, td<fix>) unit clears on A's chain, (B, td<other>)
    does not, and it holds."""
    a, b = "a" * 40, "b" * 40
    _engine_land(store, 42, paths=["scripts/ship"], sha=a)
    fix = int(store.insert_ref(kind="todo", slug=None, title="fix", meta=None).id)
    other = int(store.insert_ref(kind="todo", slug=None, title="other", meta=None).id)
    _landed_comment(store, fix, sha=b)
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": fix})
    _hand_land(b, f"fix(plan): both (td{fix}, td{other})", ["scripts/ship"])
    verdicts, _n, fatal, _pin, _scanned = rp._engine_verdicts(store, now=NOW)
    assert fatal is None and verdicts is not None
    by_todo = {v.unit.todo_id: v.cleared for v in verdicts if v.unit.sha12 == b[:12]}
    assert by_todo == {fix: True, other: False}
    assert f"td{other}" in _gate(store)[0]


def test_a_running_reviewer_on_the_corrective_holds_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer still running on B (no verdict yet) judges B itself:
    a late FIX/REVERT must not land after the release deployed. Once
    that reviewer is cancelled the chain clears B again."""
    a, b = "a" * 40, "b" * 40
    fix, _rid = _fix_chain(store, a, b)
    running = _reviewer(store, fix)  # open, no verdict
    assert f"td{running}" in _gate(store)[0]
    store.add_tag(
        running, Tag.closed("STATUS", "cancelled"), set_by="agent", replace_prefix=True
    )
    assert _gate(store)[0] == ""


def test_an_open_round_still_mints_engine_reviewers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)
    psettings.set_setting("release.round", _round_row("fixing"), store=store)
    _hand_land("0e5fde093" + "0" * 31, "fix(x): y", ["scripts/ship"])
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release round r5 in flight (fixing)"
    assert calls == [] and res.minted is None
    assert _reviewer_keys(store) == ["sha:0e5fde093000"]


def _stuck_ask_ids(store: Store, tag: str) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (tag,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.mark.parametrize(
    ("cls", "line"),
    [
        (
            "tip-only",
            "scripts/round: cut refused — the next release cuts at origin/main "
            "(a1b2c3d4e), not 9f8e7d6c5",
        ),
        (
            "gated-behind",
            "scripts/round: cut refused — origin/gated cannot fast-forward to "
            "the cut SHA",
        ),
        ("in-flight", "release in flight: release/r262"),
        ("holding", "holding: cut abc sits below origin/main: td43 pending"),
    ],
)
def test_a_first_cut_hold_past_an_hour_files_one_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch, cls: str, line: str
) -> None:
    """A first cut a refusal held (no ``meta.release`` yet) gets one
    hour-long ask, keyed on the pass root and quoting
    the class and the refusal line; nothing before the hour is up."""
    _fake_github(monkeypatch)
    mint = T.mint_release_pass(store)
    assert R.release_of(store, mint.ids["cut"]) is None
    rp.note_cut_hold(store, mint.ids["cut"], [line], now=NOW)
    store.stamp_ref_meta(
        mint.root_id,
        {"cut_retarget": {"class": cls, "line": line, "pace_until": None}},
    )
    tag = f"release-stuck:td{mint.root_id}:recut-hold"
    rp.run_pass(store, repo=REPO, now=NOW + timedelta(minutes=59))
    assert _stuck_ask_ids(store, tag) == []
    res = rp.run_pass(store, repo=REPO, now=NOW + timedelta(minutes=61))
    [ask] = _stuck_ask_ids(store, tag)
    assert any(f"td{ask}" in n for n in res.notes)
    text = _todo_text(store, ask)
    assert f"cut refused ({cls}): {line}" in text
    # gr620480: never suggested `cut --abandon` — the pass cut nothing
    # yet, so the abandon would land on the other, live release; the ask
    # may only name it to warn against it.
    assert "or `scripts/round cut --abandon`" not in text
    assert "would abandon the other, live release" in text
    rp.run_pass(store, repo=REPO, now=NOW + timedelta(minutes=90))
    assert _stuck_ask_ids(store, tag) == [ask]  # deduped


def test_a_hold_without_a_release_or_a_retarget_marker_asks_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch)
    mint = T.mint_release_pass(store)
    rp.note_cut_hold(store, mint.ids["cut"], ["x"], now=NOW)
    rp.run_pass(store, repo=REPO, now=NOW + timedelta(minutes=120))
    assert _stuck_ask_ids(store, f"release-stuck:td{mint.root_id}:recut-hold") == []


def test_a_hold_on_a_cut_release_asks_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release train T4: no review hold re-cuts a release, so a hold
    stamped beside ``meta.release`` files no recut-hold ask, under or
    past the hour."""
    _fake_github(monkeypatch)
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", "c" * 40, "p..c", repo=REPO)
    hold = ["engine land td43 has no reviewer; the release waits"]
    rp.note_cut_hold(store, mint.ids["cut"], hold, now=NOW - timedelta(minutes=90))
    rp.run_pass(store, repo=REPO, now=NOW)
    assert _stuck_ask_ids(store, "release-stuck:r7:recut-hold") == []
    assert _stuck_ask_ids(store, f"release-stuck:td{mint.root_id}:recut-hold") == []
    rp.note_cut_hold(store, mint.ids["cut"], None)
    ref = store.get_ref(kind="todo", id=mint.root_id)
    assert ref is not None and not (ref.meta or {}).get("cut_hold")


# ── gr651885: the hold keeps its holders ───────────────────────────


def _cut_hold(store: Store, root: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=root)
    assert ref is not None
    return dict((ref.meta or {}).get("cut_hold") or {})


def test_the_hold_keeps_a_holder_behind_six_carry_notes(store: Store) -> None:
    """r290 → r291: five "cleared on a corrective commit" notes filled
    the five kept lines and hid the land that actually held the cut. The
    holders come first, the carry-notes after them."""
    mint = T.mint_release_pass(store)
    carry = [
        f"engine land td{40 + i} cleared on a corrective commit "
        f"({str(i) * 12}) the cut must carry, so it holds from its own "
        "commit up to that one"
        for i in range(6)
    ]
    holder = "engine land td99 has no reviewer; the release waits — next: let td100 run"
    rp.note_cut_hold(store, mint.ids["cut"], [*carry, holder], now=NOW)
    assert _cut_hold(store, mint.root_id)["lines"] == [holder, *carry]


def test_the_hold_caps_at_twenty_lines_plus_a_count(store: Store) -> None:
    mint = T.mint_release_pass(store)
    lines = [f"engine land td{i} has no reviewer; the release waits" for i in range(25)]
    rp.note_cut_hold(store, mint.ids["cut"], lines, now=NOW)
    assert _cut_hold(store, mint.root_id)["lines"] == [*lines[:20], "+5 more"]
