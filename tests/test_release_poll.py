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
from precis.plans import pr_class as P
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import Tag
from precis.workers.job_types import release_poll as rp
from tests._script import load_script

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO = "o/r"
API = f"https://api.github.com/repos/{REPO}"
#: A Tuesday noon UTC — outside every window these tests set.
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
HEAD_SHA = "a" * 40
MAIN_SHA = "d" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    monkeypatch.delenv("PRECIS_RELEASE_NO_RELEASE_WINDOWS", raising=False)
    monkeypatch.delenv("PRECIS_RELEASE_REPO", raising=False)
    psettings.invalidate()


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
        if url.endswith("/compare/prod...main"):
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
        f"{API}/compare/prod...main",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/ref/heads/main",
    ]
    root = res.minted
    assert root is not None and "release-pass" in _tags(store, root)
    assert f"release-main:{MAIN_SHA[:12]}" in _tags(store, root)
    members = _plan_members(store, root)
    # root, cut, poll, gate, loop, approval, deploy, confirm
    assert len(members) == 8
    ops = sorted(str(m["op"]) for m in members if "op" in m)
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
    # a release head with no deployed tag: in flight, no prod compare made
    calls = _fake_github(monkeypatch, heads=[_head(7)])
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert res.blocker == "release/r7 is in flight"
    assert calls == [
        f"{API}/compare/prod...main",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
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
        f"{API}/compare/prod...main",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/matching-refs/tags/deployed/",
        f"{API}/compare/{HEAD_SHA}...prod",
        f"{API}/git/ref/heads/main",
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
    assert calls == [f"{API}/compare/prod...main"]


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
    # one pass per main sha: each mint here needs main to have moved
    for at, sha in ((_at(1, 0), "e" * 40), (_at(22, 59), "f" * 40)):
        _fake_github(
            monkeypatch,
            main_ref={"ref": "refs/heads/main", "object": {"sha": sha}},
        )
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
        f"{API}/compare/prod...main",
        f"{API}/git/matching-refs/heads/release/",
        f"{API}/git/ref/heads/main",
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
    """``_ROUND_TERMINAL_STATES`` is the same literal scripts/round and its
    strict reader validate — an unedited copy, pinned."""
    if sys.platform == "win32":
        pytest.skip("scripts/round is POSIX release tooling")
    script = load_script("round_script_rel_poll_pin", REPO_ROOT / "scripts" / "round")
    reader = load_script(
        "round_state_rel_poll_pin", REPO_ROOT / "scripts" / "lib" / "round_state.py"
    )
    assert rp._ROUND_TERMINAL_STATES == script._TERMINAL_STATES
    assert rp._ROUND_TERMINAL_STATES == reader.TERMINAL_STATES


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
        f"{API}/compare/prod...main",
        f"{API}/git/matching-refs/heads/release/",
    ]


def test_a_non_list_matching_refs_payload_is_unreadable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch, heads={"ref": "refs/heads/release/r7"})
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert "GitHub unreadable" in res.blocker and res.minted is None
    assert calls == [
        f"{API}/compare/prod...main",
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
    _fake_github(
        monkeypatch,
        main_ref={"ref": "refs/heads/main", "object": {"sha": "e" * 40}},
    )
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


@pytest.mark.parametrize(
    "main_ref",
    [{"ref": "refs/heads/main", "object": {}}, [], [{"ref": "refs/heads/mainx"}]],
    ids=["no-sha", "empty-list", "prefix-only"],
)
def test_an_unreadable_main_sha_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch, main_ref: Any
) -> None:
    _fake_github(monkeypatch, main_ref=main_ref)
    res = rp.run_pass(store, repo=REPO, now=NOW)
    assert "GitHub unreadable" in res.blocker and res.minted is None
    assert rp._release_pass_ids(store) == []


def test_two_interleaved_runners_mint_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check-then-mint is one transaction under the advisory lock: two
    runners that both pass every gate mint one pass between them — the
    loser's re-check inside the lock sees the winner's root."""
    _fake_github(monkeypatch)
    barrier = threading.Barrier(2)
    real_main_sha = rp._main_sha

    def synced_main_sha(base: str) -> str:
        barrier.wait(timeout=30)  # both runners reach the claim together
        return real_main_sha(base)

    monkeypatch.setattr(rp, "_main_sha", synced_main_sha)
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
