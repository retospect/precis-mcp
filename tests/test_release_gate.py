"""Shared cut/deploy policy (gr702332); no network or database required."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis import settings
from precis.plans import pr_class as P
from precis.plans import release as R
from precis.store import Store
from precis.workers.job_types import release_poll as rp

SHA = "a" * 40
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "window": "03:00-04:20",
        "pass": None,
        "round": None,
        "compare": {"status": "ahead", "ahead_by": 1, "behind_by": 0},
        "branch": None,
        "reviews": [],
        "head": SHA,
        "ci": [{"sha": SHA, "verdict": "green"}],
    }
    monkeypatch.setattr(settings, "get_str", lambda *a, **kw: facts["window"])
    monkeypatch.setattr(
        R, "release_of", lambda *a: {"rN": "r7", "sha": SHA, "repo": "o/r"}
    )
    monkeypatch.setattr(R, "plan_root", lambda *a: 10)
    monkeypatch.setattr(R, "read_ci_verdicts", lambda *a: facts["ci"])
    monkeypatch.setattr(P, "resolve_repo", lambda *a: "o/r")
    monkeypatch.setattr(P, "_get_json", lambda *a: facts["compare"])
    monkeypatch.setattr(rp, "_open_pass_id", lambda *a, **kw: facts["pass"])
    monkeypatch.setattr(rp, "_round_raw", lambda *a: None)
    monkeypatch.setattr(rp, "_round_blocker", lambda *a, **kw: facts["round"])
    monkeypatch.setattr(rp, "_in_flight", lambda *a, **kw: facts["branch"])
    monkeypatch.setattr(rp, "deploy_review_blockers", lambda *a: facts["reviews"])
    monkeypatch.setattr(rp, "release_head", lambda *a: facts["head"])
    return facts


@pytest.mark.parametrize(
    ("fact", "value", "reason"),
    [
        ("window", "11:00-13:00", "inside the no-release window 11:00-13:00Z"),
        ("pass", 42, "release-pass td42 is open"),
        (
            "round",
            "release round r8 in flight (cut)",
            "release round r8 in flight (cut)",
        ),
        (
            "compare",
            {"status": "diverged", "behind_by": 1},
            "prod has commits main lacks (diverged)",
        ),
        ("branch", "release/r8", "release/r8 is in flight"),
    ],
)
def test_same_facts_refuse_both_stages(
    policy: dict[str, Any], fact: str, value: Any, reason: str
) -> None:
    policy[fact] = value
    store = Store.__new__(Store)
    assert R.may_release(store, "cut", now=NOW) == (False, reason)
    assert R.may_release(store, "deploy", todo_id=11, now=NOW) == (False, reason)


def test_malformed_windows_preserve_stage_policy(policy: dict[str, Any]) -> None:
    policy["window"] = "garbage"
    store = Store.__new__(Store)
    assert R.may_release(store, "cut", now=NOW) == (
        False,
        "release.no_release_windows is unparsable: nothing minted",
    )
    assert R.may_release(store, "deploy", todo_id=11, now=NOW) == (True, "")


@pytest.mark.parametrize("verdict", [None, "red", "waived", "other-head"])
def test_deploy_requires_green_on_exact_head(
    policy: dict[str, Any], verdict: str | None
) -> None:
    policy["ci"] = (
        []
        if verdict is None
        else [
            {
                "sha": "b" * 40 if verdict == "other-head" else SHA,
                "verdict": "green" if verdict == "other-head" else verdict,
            }
        ]
    )
    store = Store.__new__(Store)
    assert R.may_release(store, "cut", now=NOW) == (True, "")
    assert R.may_release(store, "deploy", todo_id=11, now=NOW) == (
        False,
        f"CI verdict is not green on {SHA[:12]}",
    )


def test_deploy_refuses_a_head_the_branch_moved_past(policy: dict[str, Any]) -> None:
    """gr741486: a corrective pushed after the last verdict leaves the
    recorded sha green and the branch on another head; if this fails the
    deploy is dispatched on the old head's verdict (r341, 18 s after the
    push)."""
    moved = "b" * 40
    policy["head"] = moved
    store = Store.__new__(Store)
    assert R.may_release(store, "cut", now=NOW) == (True, "")
    assert R.may_release(store, "deploy", todo_id=11, now=NOW) == (
        False,
        f"release/r7 head {moved[:9]} is not the recorded {SHA[:9]} — "
        "the pass polls the new head first",
    )
    # A green row on the new head does not help while the plan still
    # records the old one: the restamp is what moves it.
    policy["ci"] = [{"sha": moved, "verdict": "green"}]
    assert R.may_release(store, "deploy", todo_id=11, now=NOW)[0] is False


def test_deploy_refuses_an_unreadable_release_head(policy: dict[str, Any]) -> None:
    policy["head"] = None
    assert R.may_release(Store.__new__(Store), "deploy", todo_id=11, now=NOW) == (
        False,
        "release/r7 head unreadable; next tick retries",
    )


def test_only_deploy_waits_on_reviews(policy: dict[str, Any]) -> None:
    policy["reviews"] = ["review td42 is open"]
    store = Store.__new__(Store)
    assert R.may_release(store, "cut", now=NOW) == (True, "")
    assert R.may_release(store, "deploy", todo_id=11, now=NOW) == (
        False,
        "r7 waits on its engine reviews — review td42 is open",
    )


def test_deploy_excludes_only_its_own_pass_and_branch(
    policy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def open_pass(store: Store, *, exclude: int | None = None) -> None:
        seen["pass"] = exclude

    def branch(store: Store, repo: str, *, exclude_n: int | None = None) -> None:
        seen["branch"] = exclude_n

    monkeypatch.setattr(rp, "_open_pass_id", open_pass)
    monkeypatch.setattr(rp, "_in_flight", branch)

    def round_blocker(
        store: Store,
        now: datetime,
        *,
        dry_run: bool,
        notes: list[str],
        exclude_n: int | None = None,
    ) -> None:
        seen["round"] = exclude_n

    monkeypatch.setattr(rp, "_round_blocker", round_blocker)
    assert R.may_release(Store.__new__(Store), "deploy", todo_id=11, now=NOW) == (
        True,
        "",
    )
    assert seen == {"pass": 10, "branch": 7, "round": 7}


def test_submit_leaves_a_new_window_to_the_script(
    policy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window opening after dispatch must reach the attempt-free script guard."""
    from types import SimpleNamespace

    from precis.workers.job_types import release_deploy as rd

    store = Store.__new__(Store)
    policy["window"] = "11:00-13:00"
    monkeypatch.setattr(rd, "_utcnow", lambda: NOW)
    monkeypatch.setattr(rd._rc, "factory_refusal", lambda *a: None)
    monkeypatch.setattr(rd._rc, "todo_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(R, "approval", lambda *a: ("r7", "approved"))
    monkeypatch.setattr(R, "pin_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(rd._rs, "slice_root", lambda: "/checkout")
    monkeypatch.setattr(R, "mark_deploy_started", lambda *a: "started")
    monkeypatch.setattr(rd._rc, "pass_env", lambda *a: {})
    monkeypatch.setattr(rd._rc, "launch_script", lambda *a, **kw: "launched")
    failures: list[str] = []
    ctx = SimpleNamespace(
        store=store,
        meta={"params": {"todo_id": 11}},
        record_failure=failures.append,
        set_meta=lambda **kw: None,
        append_chunk=lambda *a: None,
    )
    assert "backup window" in (rd.validate_submit(store, params={"todo_id": 11}) or "")
    assert rd.submit(ctx, None) == "launched"
    assert failures == []


def test_submit_refuses_when_the_head_moved_as_it_stamped_the_start(
    policy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr741486: a push lands between the launch check and the
    deploy-started stamp. The second read after the stamp sees it: no
    launch, and the stamp is cleared so the release takes fixes again."""
    from types import SimpleNamespace

    from precis.workers.job_types import release_deploy as rd

    store = Store.__new__(Store)
    monkeypatch.setattr(rd, "_utcnow", lambda: NOW)
    monkeypatch.setattr(rd._rc, "factory_refusal", lambda *a: None)
    monkeypatch.setattr(rd._rc, "todo_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(R, "approval", lambda *a: ("r7", "approved"))
    monkeypatch.setattr(R, "pin_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(rd._rs, "slice_root", lambda: "/checkout")
    monkeypatch.setattr(rd._rc, "pass_env", lambda *a: {})
    launched: list[str] = []
    monkeypatch.setattr(
        rd._rc, "launch_script", lambda *a, **kw: launched.append("launched")
    )
    stamps: list[str] = []

    def start(*a: Any) -> str:
        policy["head"] = "b" * 40  # the push lands as the stamp is written
        stamps.append("started")
        return "started"

    def clear(*a: Any) -> str:
        stamps.append("cleared")
        return "cleared"

    monkeypatch.setattr(R, "mark_deploy_started", start)
    monkeypatch.setattr(R, "clear_deploy_started", clear)
    failures: list[str] = []
    ctx = SimpleNamespace(
        store=store,
        meta={"params": {"todo_id": 11}},
        record_failure=failures.append,
        set_meta=lambda **kw: None,
        append_chunk=lambda *a: None,
    )
    assert rd.submit(ctx, None) is None
    assert launched == [] and stamps == ["started", "cleared"]
    assert len(failures) == 1 and "is not the recorded" in failures[0]


def test_github_outage_holds_deploy(
    policy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any) -> Any:
        raise P.GitHubUnreadable("unavailable")

    monkeypatch.setattr(P, "_get_json", unavailable)
    ok, reason = R.may_release(Store.__new__(Store), "deploy", todo_id=11, now=NOW)
    assert not ok
    assert reason.startswith("GitHub unreadable (unavailable)")
