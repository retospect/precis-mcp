"""``ci_watch`` (me580555): a completed check.yml run is judged once —
a red ``event=schedule`` nightly on main or a red ``release/**`` run with
a ``soak`` job files ONE gripe per failure signature
(``ci:check.yml:<file>::<test>``, ``ci:check.yml:infra:<job>`` for a job
with no FAILED/ERROR node); an open gripe gets a ``seen again:`` comment,
a terminal one refiles naming its handle under 7 days; two consecutive
green runs close an unresolved gripe as a flake; a ``resolved:`` sha gets
``green on <url>`` only when the newest green head contains it. GitHub is
faked at ``ci_watch._gh_json`` / ``ci_watch._gh_log``: no test calls the
network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import ci_watch
from tests._refs import insert_gripe

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_watch"
REPO = "o/r"

NIGHTLY_ONE = "nightly red: ci:check.yml:tests/test_foo.py::test_one"
NIGHTLY_TWO = "nightly red: ci:check.yml:tests/test_foo.py::test_two"
NIGHTLY_THREE = "nightly red: ci:check.yml:tests/test_bar.py::test_three"
NIGHTLY_INFRA = "nightly red: ci:check.yml:infra:lint"
SOAK_SIG = "soak red: ci:check.yml:tests/test_soak.py::test_leak"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    psettings.invalidate()


def _fake_gh(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, Any]:
    """Patch the gh seams from a fixture: ``endpoints`` maps the exact
    endpoint string to its JSON, ``logs`` maps job id to log text."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def gh_json(endpoint: str) -> Any:
        if endpoint not in data["endpoints"]:
            raise AssertionError(f"unstubbed gh endpoint: {endpoint}")
        return data["endpoints"][endpoint]

    def gh_log(repo: str, job_id: int) -> str:
        key = str(job_id)
        if key not in data["logs"]:
            raise AssertionError(f"unstubbed gh log: {job_id}")
        return data["logs"][key]

    monkeypatch.setattr(ci_watch, "_gh_json", gh_json)
    monkeypatch.setattr(ci_watch, "_gh_log", gh_log)
    return data


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: dict[str, Any],
    *,
    logs: dict[str, str] | None = None,
    calls: list[str] | None = None,
) -> dict[str, Any]:
    """The ``_fake_gh`` seams without a fixture file: ``endpoints`` maps
    the exact endpoint string to its JSON, ``logs`` maps job id to log
    text, ``calls`` (optional) records every ``_gh_json`` call."""

    def gh_json(endpoint: str) -> Any:
        if calls is not None:
            calls.append(endpoint)
        if endpoint not in endpoints:
            raise AssertionError(f"unstubbed gh endpoint: {endpoint}")
        return endpoints[endpoint]

    def gh_log(repo: str, job_id: int) -> str:
        key = str(job_id)
        if logs is None or key not in logs:
            raise AssertionError(f"unstubbed gh log: {job_id}")
        return logs[key]

    monkeypatch.setattr(ci_watch, "_gh_json", gh_json)
    monkeypatch.setattr(ci_watch, "_gh_log", gh_log)
    return endpoints


def _release_run(id_: int, sha: str = "e" * 40) -> dict[str, Any]:
    """One completed green ``release/**`` run dict (gh listing shape)."""
    return {
        "id": id_,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "release/r4",
        "head_sha": sha,
        "html_url": f"https://github.com/o/r/actions/runs/{id_}",
        "updated_at": "2026-11-07T04:00:00Z",
    }


def _gripes(store: Store) -> dict[str, int]:
    """``{title: ref_id}`` for every live gripe."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title FROM refs WHERE kind = 'gripe' AND retired_at IS NULL"
        ).fetchall()
    return {str(r[1]): int(r[0]) for r in rows}


def _comments(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'gripe_comment' ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _body(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'gripe_body' ORDER BY ord LIMIT 1",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else ""


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _open_tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"}


def _seen(store: Store) -> dict[str, Any]:
    raw = psettings.get_str(ci_watch.SEEN_SETTING, store=store)
    return json.loads(raw) if raw else {}


def _seed_gripe(
    store: Store,
    title: str,
    *,
    status: str = "open",
    tags: tuple[str, ...] = (),
) -> int:
    ref = insert_gripe(store, title, status=status)
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="agent")
    return int(ref.id)


def _age_status_tag(store: Store, ref_id: int, days: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - make_interval(days => %s) "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS'",
            (days, ref_id),
        )


def _comment(store: Store, ref_id: int, text: str) -> None:
    with store.tx() as conn:
        row = conn.execute(
            "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        store.chunks.insert_chunks(
            ref_id,
            [
                ChunkInsert(
                    ord=int(row[0]) if row else 0,
                    text=text,
                    meta={"chunk_kind": "gripe_comment"},
                )
            ],
            conn=conn,
        )


class _Ctx:
    """The DispatchContext surface _dispatch uses."""

    def __init__(self, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []
        self.stamped: dict[str, Any] = {}

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.stamped.update(kw)

    def record_failure(self, msg: str, **_kw: Any) -> None:
        self.failures.append(msg)


def test_green_nightly_records_seen_and_files_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fake_gh(monkeypatch, "green_nightly.json")
    # A run without updated_at falls back to now — still Z-suffixed.
    data["endpoints"][
        "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3"
    ]["workflow_runs"][0].pop("updated_at")
    res = ci_watch.run_pass(store, repo=REPO)
    assert res.gh_error is None
    assert res.filed == [] and _gripes(store) == {}
    seen = _seen(store)
    assert set(seen) == {"100", "101"}
    assert seen["101"]["verdict"] == "green" and seen["101"]["wf"] == "nightly"
    assert seen["101"]["at"].endswith("Z")


def test_red_nightly_files_one_gripe_per_signature(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(monkeypatch, "red_nightly.json")
    res = ci_watch.run_pass(store, repo=REPO)
    assert res.gh_error is None
    gripes = _gripes(store)
    # test_two[slow-path] has its parametrize stripped; the lint job has
    # no FAILED/ERROR node so it lands as the infra signature.
    assert set(gripes) == {
        NIGHTLY_ONE,
        NIGHTLY_TWO,
        NIGHTLY_THREE,
        NIGHTLY_INFRA,
    }
    assert len(res.filed) == 4
    infra = _open_tags(store, gripes[NIGHTLY_INFRA])
    assert "fix:infra" in infra and "fix:code" not in infra
    code = _open_tags(store, gripes[NIGHTLY_ONE])
    assert {"fix:code", "ci:nightly", "origin:ci-watch"} <= code
    for gid in gripes.values():
        assert _status(store, gid) == "open"
    seen = _seen(store)
    assert seen["201"]["verdict"] == "red" and seen["201"]["wf"] == "nightly"


def test_open_gripe_gets_seen_again_comment(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly"))
    _fake_gh(monkeypatch, "red_nightly.json")
    res = ci_watch.run_pass(store, repo=REPO)
    assert res.seen_again == [gid]
    assert len(res.filed) == 3
    assert _comments(store, gid) == [
        "seen again: run https://github.com/o/r/actions/runs/201 on ddddddddd"
    ]


def test_ci_mute_silences_the_open_gripe(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "ci:mute")
    )
    _fake_gh(monkeypatch, "red_nightly.json")
    res = ci_watch.run_pass(store, repo=REPO)
    assert res.seen_again == []
    assert _comments(store, gid) == []
    assert len(res.filed) == 3  # the other signatures still file


def test_terminal_gripe_under_7d_refiles_naming_old_handle(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _seed_gripe(store, NIGHTLY_ONE, status="done")
    _age_status_tag(store, old, days=2)
    _fake_gh(monkeypatch, "red_nightly.json")
    ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    new = gripes[NIGHTLY_ONE]
    assert new != old
    assert f"regressed after gr{old}" in _body(store, new)


def test_terminal_gripe_over_7d_refiles_plain(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _seed_gripe(store, NIGHTLY_ONE, status="done")
    _age_status_tag(store, old, days=10)
    _fake_gh(monkeypatch, "red_nightly.json")
    ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    new = gripes[NIGHTLY_ONE]
    assert new != old
    assert "regressed" not in _body(store, new)


def test_two_green_runs_close_unresolved_gripe_as_flake(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "fix:code")
    )
    _fake_gh(monkeypatch, "green_nightly.json")
    ci_watch.run_pass(store, repo=REPO)
    assert _status(store, gid) == "done"
    assert _comments(store, gid) == [
        "closed by ci_watch: 2 consecutive green runs, no fix landed (flake)"
    ]


def test_live_todo_link_blocks_flake_close(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "fix:code")
    )
    todo = store.insert_ref(kind="todo", slug=None, title="fix it")
    store.add_link(src_ref_id=int(todo.id), dst_ref_id=gid, set_by="agent")
    _fake_gh(monkeypatch, "green_nightly.json")
    ci_watch.run_pass(store, repo=REPO)
    assert _status(store, gid) == "open"
    assert _comments(store, gid) == []


def test_one_green_run_does_not_close(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "fix:code")
    )
    data = _fake_gh(monkeypatch, "green_nightly.json")
    ep = "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3"
    data["endpoints"][ep]["workflow_runs"] = data["endpoints"][ep]["workflow_runs"][:1]
    ci_watch.run_pass(store, repo=REPO)
    assert _status(store, gid) == "open"


def test_resolved_sha_commented_green_when_contained(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "fix:code")
    )
    sha = "a" * 40
    _comment(store, gid, f"resolved: {sha[:9]} (main, release pending) — fix")
    data = _fake_gh(monkeypatch, "green_nightly.json")
    head = "c" * 40
    data["endpoints"][f"repos/o/r/compare/{sha[:9]}...{head}"] = {"status": "identical"}
    ci_watch.run_pass(store, repo=REPO)
    # newest green run (id 101) head contains the resolved sha
    assert _comments(store, gid)[-1] == (
        "green on https://github.com/o/r/actions/runs/101"
    )
    assert _status(store, gid) == "open"


def test_resolved_sha_not_contained_gets_no_comment(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "fix:code")
    )
    sha = "a" * 40
    _comment(store, gid, f"resolved: {sha[:9]} (main, release pending) — fix")
    data = _fake_gh(monkeypatch, "green_nightly.json")
    head = "c" * 40
    data["endpoints"][f"repos/o/r/compare/{sha[:9]}...{head}"] = {"status": "behind"}
    ci_watch.run_pass(store, repo=REPO)
    assert _comments(store, gid) == [
        f"resolved: {sha[:9]} (main, release pending) — fix"
    ]


def test_seen_run_is_not_reprocessed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    psettings.set_setting(
        ci_watch.SEEN_SETTING,
        json.dumps(
            {"201": {"at": "2026-11-07T03:10:00Z", "verdict": "red", "wf": "nightly"}}
        ),
        store=store,
    )
    _fake_gh(monkeypatch, "red_nightly.json")
    res = ci_watch.run_pass(store, repo=REPO)
    assert res.filed == [] and _gripes(store) == {}


def test_soak_run_files_soak_red_gripes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(monkeypatch, "red_soak.json")
    res = ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    assert set(gripes) == {SOAK_SIG}
    assert "ci:soak" in _open_tags(store, gripes[SOAK_SIG])
    assert len(res.filed) == 1
    seen = _seen(store)
    # Only the release/** run carrying a soak job is tracked — the main
    # push run is filtered out of the soak scope entirely.
    assert set(seen) == {"301"}
    assert seen["301"]["wf"] == "soak"


def test_precis_factory_off_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    _fake_gh(monkeypatch, "red_nightly.json")
    ctx = _Ctx(store, {"repo": REPO})
    ci_watch._dispatch(ctx, ci_watch.SPEC)
    assert _gripes(store) == {}
    assert _seen(store) == {}
    assert not ctx.failures
    counts = ctx.stamped["ci_watch"]
    assert counts["dry_run"] is True
    assert counts["would"] >= 4


def test_gh_failure_is_one_job_event_no_traceback(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(endpoint: str) -> Any:
        raise ci_watch.GhUnavailable(f"gh api {endpoint}: exit 1: auth expired")

    monkeypatch.setattr(ci_watch, "_gh_json", boom)
    ctx = _Ctx(store, {"repo": REPO})
    ci_watch._dispatch(ctx, ci_watch.SPEC)
    events = [t for k, t in ctx.chunks if k == "job_event"]
    assert len(events) == 1 and "exit 1" in events[0] and "auth expired" in events[0]
    assert len(ctx.failures) == 1 and "Traceback" not in ctx.failures[0]


def test_gripe_body_is_actionable_without_the_mcp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fake_gh(monkeypatch, "red_nightly.json")
    # One node with a >5 KB reason carrying an address early — the strip,
    # not the truncation, is what keeps it out of the body.
    data["logs"]["9001"] += (
        "2026-11-07T03:00:09.0000000Z FAILED tests/test_big.py::test_huge"
        " - dial tcp 10.1.2.3:443 refused; " + "x" * 5000 + "\n"
    )
    ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    body = _body(store, gripes[NIGHTLY_ONE])
    assert "Goal: the test passes on main" in body
    assert "node: tests/test_foo.py::test_one" in body
    assert "reason: assert 1 == 2" in body
    assert "scripts/ci-failed 201" in body
    assert "run: https://github.com/o/r/actions/runs/201" in body
    assert "job: pytest (shard 1)" in body
    # Sibling handles of the same run, and a related-to link.
    assert f"gr{gripes[NIGHTLY_TWO]}" in body
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE relation = 'related-to' AND ("
            "(src_ref_id = %s AND dst_ref_id = %s) OR "
            "(src_ref_id = %s AND dst_ref_id = %s)) LIMIT 1",
            (
                gripes[NIGHTLY_ONE],
                gripes[NIGHTLY_TWO],
                gripes[NIGHTLY_TWO],
                gripes[NIGHTLY_ONE],
            ),
        ).fetchone()
    assert row is not None
    # The huge reason is bounded and its address never reaches the body.
    big = _body(store, gripes["nightly red: ci:check.yml:tests/test_big.py::test_huge"])
    reasons = [l for l in big.splitlines() if l.startswith("reason: ")]
    assert reasons and max(len(l) for l in reasons) <= len("reason: ") + 200
    assert "10.1.2.3" not in big
    # The infra signature carries the step name.
    infra = _body(store, gripes[NIGHTLY_INFRA])
    assert "step: lint" in infra
    assert "infra" in infra


def test_timed_out_job_files_an_infra_signature(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fake_gh(monkeypatch, "red_nightly.json")
    data["endpoints"]["repos/o/r/actions/runs/201/jobs"]["jobs"][3]["conclusion"] = (
        "timed_out"
    )
    ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    infra = "nightly red: ci:check.yml:infra:typecheck"
    assert infra in gripes
    assert "fix:infra" in _open_tags(store, gripes[infra])


def test_red_run_that_filed_nothing_is_not_marked_seen(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fake_gh(monkeypatch, "red_nightly.json")
    for job in data["endpoints"]["repos/o/r/actions/runs/201/jobs"]["jobs"]:
        job["conclusion"] = "success"
    ci_watch.run_pass(store, repo=REPO)
    assert _gripes(store) == {}
    assert "201" not in _seen(store)


def test_api_budget_caps_jobs_calls_on_a_quiet_night(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet night never exceeds the call budget: release/ candidates
    already in ``ci_watch.seen`` cost no jobs call, and an over-long
    candidate list is cut at the cap with the rest left unseen."""
    release_runs = [
        {
            "id": 400 + i,
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "head_branch": "release/r4",
            "head_sha": "e" * 40,
            "html_url": f"https://github.com/o/r/actions/runs/{400 + i}",
            "updated_at": "2026-11-07T04:00:00Z",
        }
        for i in range(18)
    ]
    endpoints: dict[str, Any] = {
        "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
            "workflow_runs": []
        },
        "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
            "workflow_runs": release_runs
        },
    }
    for i in range(18):
        endpoints[f"repos/o/r/actions/runs/{400 + i}/jobs"] = {
            "jobs": [
                {
                    "id": 9100 + i,
                    "name": "soak",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
    calls: list[str] = []

    def gh_json(endpoint: str) -> Any:
        calls.append(endpoint)
        if endpoint not in endpoints:
            raise AssertionError(f"unstubbed gh endpoint: {endpoint}")
        return endpoints[endpoint]

    monkeypatch.setattr(ci_watch, "_gh_json", gh_json)
    # The oldest 15 candidates are already judged: they must not cost a
    # jobs call. That leaves 3 unseen + 2 list calls = 5 calls.
    psettings.set_setting(
        ci_watch.SEEN_SETTING,
        json.dumps(
            {
                str(400 + i): {
                    "at": "2026-11-07T04:00:00Z",
                    "verdict": "green",
                    "wf": "soak",
                }
                for i in range(15)
            }
        ),
        store=store,
    )
    ci_watch.run_pass(store, repo=REPO)
    assert len(calls) == 5

    # Same night with nothing seen and no soak jobs anywhere: probes
    # cap at SOAK_PROBE_CAP — 2 list + 5 jobs calls, well under budget —
    # and each probed run is ledgerecorded so it is not probed again.
    calls.clear()
    for i in range(18):
        endpoints[f"repos/o/r/actions/runs/{400 + i}/jobs"]["jobs"][0]["name"] = (
            "test-linux"
        )
    psettings.set_setting(ci_watch.SEEN_SETTING, "{}", store=store)
    ci_watch.run_pass(store, repo=REPO)
    probed = [c for c in calls if c.endswith("/jobs")]
    assert len(probed) == ci_watch.SOAK_PROBE_CAP
    assert len(calls) <= ci_watch._API_BUDGET
    assert set(_seen(store)) == {str(400 + i) for i in range(ci_watch.SOAK_PROBE_CAP)}


def test_gh_log_retries_without_the_flag_on_an_old_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh <2.97 has no ``--allow-escape-sequences``: one retry without it,
    ANSI escapes stripped in Python on the retry path."""
    attempts: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> Any:
        attempts.append(list(argv))
        if "--allow-escape-sequences" in argv:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="unknown flag: --allow-escape-sequences\n",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="\x1b[31mFAILED\x1b[0m tests/test_x.py::test_y - boom\n",
            stderr="",
        )

    monkeypatch.setattr(ci_watch.subprocess, "run", fake_run)
    text = ci_watch._gh_log(REPO, 42)
    assert len(attempts) == 2
    assert "\x1b" not in text
    assert "FAILED tests/test_x.py::test_y" in text


def test_dry_run_honours_ci_mute(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    gid = _seed_gripe(
        store, NIGHTLY_ONE, tags=("origin:ci-watch", "ci:nightly", "ci:mute")
    )
    _fake_gh(monkeypatch, "red_nightly.json")
    res = ci_watch.run_pass(store, repo=REPO, dry_run=True)
    assert not any("seen again" in w and f"gr{gid}" in w for w in res.would)
    assert not any(f"gr{gid}" in w for w in res.would)


def test_seen_setting_capped_at_50(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    psettings.set_setting(
        ci_watch.SEEN_SETTING,
        json.dumps(
            {
                str(1000 + i): {
                    "at": f"2026-11-0{1 + i % 7}T00:00:00Z",
                    "verdict": "green",
                    "wf": "nightly",
                }
                for i in range(60)
            }
        ),
        store=store,
    )
    _fake_gh(monkeypatch, "green_nightly.json")
    ci_watch.run_pass(store, repo=REPO)
    assert len(_seen(store)) <= 50


def test_soak_seen_green_runs_count_toward_the_flake_streak(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soak run already in ``seen`` still counts for the streak: a
    green run on tick 1 plus a green run on tick 2 flake-closes the
    unresolved gripe."""
    gid = _seed_gripe(store, SOAK_SIG, tags=("origin:ci-watch", "ci:soak", "fix:code"))
    ep_list = "repos/o/r/actions/workflows/check.yml/runs?per_page=20"
    endpoints = _stub_gh(
        monkeypatch,
        {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": []
            },
            ep_list: {"workflow_runs": [_release_run(401)]},
            "repos/o/r/actions/runs/401/jobs": {
                "jobs": [
                    {
                        "id": 9101,
                        "name": "soak",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            "repos/o/r/actions/runs/402/jobs": {
                "jobs": [
                    {
                        "id": 9102,
                        "name": "soak",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        },
    )
    ci_watch.run_pass(store, repo=REPO)
    assert _seen(store)["401"]["verdict"] == "green"
    assert _status(store, gid) == "open"
    endpoints[ep_list]["workflow_runs"] = [_release_run(402), _release_run(401)]
    ci_watch.run_pass(store, repo=REPO)
    assert _status(store, gid) == "done"
    assert _comments(store, gid) == [
        "closed by ci_watch: 2 consecutive green runs, no fix landed (flake)"
    ]


def test_resolved_soak_gripe_gets_green_on_when_nothing_new(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``newest_green`` reads the seen ledger too: a tick whose soak
    listing holds only already-judged runs still comments ``green on``."""
    gid = _seed_gripe(store, SOAK_SIG, tags=("origin:ci-watch", "ci:soak", "fix:code"))
    sha = "a" * 40
    endpoints = _stub_gh(
        monkeypatch,
        {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": []
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": [_release_run(401)]
            },
            "repos/o/r/actions/runs/401/jobs": {
                "jobs": [
                    {
                        "id": 9101,
                        "name": "soak",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            f"repos/o/r/compare/{sha[:9]}...{'e' * 40}": {"status": "ahead"},
        },
    )
    ci_watch.run_pass(store, repo=REPO)
    _comment(store, gid, f"resolved: {sha[:9]} — soak fix landed")
    ci_watch.run_pass(store, repo=REPO)
    assert _comments(store, gid)[-1] == (
        "green on https://github.com/o/r/actions/runs/401"
    )


def test_no_soak_probes_are_capped_ledgerecorded_and_not_repeated(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release/**`` runs without a ``soak*`` job must not starve the
    red-run reads: probes cap at :data:`SOAK_PROBE_CAP` per tick and a
    probed run is ledgerecorded so the next tick does not probe it."""
    endpoints_dict: dict[str, Any] = {
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
            "workflow_runs": [_release_run(400 + i) for i in range(15)]
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
    for i in range(15):
        endpoints_dict[f"repos/o/r/actions/runs/{400 + i}/jobs"] = {
            "jobs": [
                {
                    "id": 9100 + i,
                    "name": "test-linux",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
    calls: list[str] = []
    _stub_gh(
        monkeypatch,
        endpoints_dict,
        logs={
            "9001": "2026-11-07T03:00:01.0000000Z FAILED tests/test_x.py::test_y - boom\n"
        },
        calls=calls,
    )
    res = ci_watch.run_pass(store, repo=REPO)
    assert "nightly red: ci:check.yml:tests/test_x.py::test_y" in _gripes(store)
    assert len(calls) <= ci_watch._API_BUDGET
    nosoak = {rid for rid, v in _seen(store).items() if v["verdict"] == "no-soak"}
    assert nosoak == {str(400 + i) for i in range(5)}
    # Tick 2: the recorded runs are not re-probed — probing moves on.
    calls.clear()
    res = ci_watch.run_pass(store, repo=REPO)
    probed = {c for c in calls if c.endswith("/jobs")}
    assert probed == {f"repos/o/r/actions/runs/{400 + i}/jobs" for i in range(5, 10)}


def test_cancelled_matrix_siblings_file_one_infra_gripe(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fail-fast matrix's cancelled siblings share one infra gripe:
    the first cancelled job files, the rest are named in its body."""
    data = _fake_gh(monkeypatch, "red_nightly.json")
    jobs = data["endpoints"]["repos/o/r/actions/runs/201/jobs"]["jobs"]
    for job in jobs[1:]:
        job["conclusion"] = "cancelled"
    ci_watch.run_pass(store, repo=REPO)
    gripes = _gripes(store)
    infras = [t for t in gripes if ":infra:" in t]
    assert infras == ["nightly red: ci:check.yml:infra:pytest (shard 2)"]
    body = _body(store, gripes[infras[0]])
    assert "also down: lint (cancelled), typecheck (cancelled)" in body


def test_non_soak_failure_on_release_files_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``release/**`` run is judged on ``soak*`` jobs only — a failing
    non-soak job is the release loop's signal, so it files nothing."""
    _stub_gh(
        monkeypatch,
        {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": []
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": [_release_run(401)]
            },
            "repos/o/r/actions/runs/401/jobs": {
                "jobs": [
                    {
                        "id": 9101,
                        "name": "soak",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 9102,
                        "name": "test-linux",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ]
            },
        },
    )
    ci_watch.run_pass(store, repo=REPO)
    assert _gripes(store) == {}
    assert _seen(store)["401"]["verdict"] == "green"


# The one job a skipped-before-expansion soak matrix reports (gr586293,
# run 42425741923): the literal ``name:`` expression, unexpanded — there
# are no ``soak (3.13, K)`` legs for a name-matching reader to find.
_SKIPPED_MATRIX_NAME = (
    "${{ (((needs.plan.outputs.full == 'true') && 'test-linux') || 'soak') }} "
    "(${{ matrix.python-version }}, ${{ matrix.shard }})"
)


def test_a_skipped_soak_matrix_records_skipped_not_absent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr586293: the skipped soak matrix is ONE placeholder job named by
    the unrendered template, beside a skipped ``verdict-soak``. The run
    still carries the soak — it is ledgered ``skipped`` (the legs exist
    but did not run), not ``no-soak`` and never ``green``."""
    _stub_gh(
        monkeypatch,
        {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": []
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": [_release_run(401)]
            },
            "repos/o/r/actions/runs/401/jobs": {
                "jobs": [
                    {
                        "id": 9101,
                        "name": _SKIPPED_MATRIX_NAME,
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                    {
                        "id": 9102,
                        "name": "verdict-soak",
                        "status": "completed",
                        "conclusion": "skipped",
                    },
                    {
                        "id": 9103,
                        "name": "gate-fast (3.13, 1)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ]
            },
        },
    )
    res = ci_watch.run_pass(store, repo=REPO)
    assert _gripes(store) == {}
    assert res.processed == 1
    seen = _seen(store)["401"]
    assert seen["verdict"] == "skipped"
    assert seen["wf"] == "soak"


def test_a_skipped_soak_does_not_count_toward_the_flake_streak(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped soak is not a green soak: a ``skipped`` verdict between
    two green soak runs breaks the flake-close streak rather than
    padding it — the run stays in soak scope (its matrix exists) but
    delivers no verdict."""
    gid = _seed_gripe(store, SOAK_SIG, tags=("origin:ci-watch", "ci:soak", "fix:code"))
    _stub_gh(
        monkeypatch,
        {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": []
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": [
                    _release_run(403),
                    _release_run(402),
                    _release_run(401),
                ]
            },
            "repos/o/r/actions/runs/401/jobs": {
                "jobs": [
                    {
                        "id": 9101,
                        "name": "soak (3.13, 1)",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            "repos/o/r/actions/runs/402/jobs": {
                "jobs": [
                    {
                        "id": 9102,
                        "name": _SKIPPED_MATRIX_NAME,
                        "status": "completed",
                        "conclusion": "skipped",
                    }
                ]
            },
            "repos/o/r/actions/runs/403/jobs": {
                "jobs": [
                    {
                        "id": 9103,
                        "name": "soak (3.13, 1)",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
        },
    )
    ci_watch.run_pass(store, repo=REPO)
    assert _seen(store)["402"]["verdict"] == "skipped"
    assert _status(store, gid) == "open"
    assert _comments(store, gid) == []


def test_sanitize_log_strips_tabs_and_masks_hosts() -> None:
    """Body hygiene: tabs become spaces, clock times survive, and real
    addresses, home paths and dotted hostnames never reach a body."""
    s = ci_watch._sanitize_log
    assert s("connect node-a.cluster.internal:5432 refused", 400) == (
        "connect <host> refused"
    )
    assert s("dial 10.1.2.3:443 refused", 400) == "dial <addr>:443 refused"
    assert s("bind fe80::1 failed", 400) == "bind <addr> failed"
    assert s("FAILED tests/test_x.py::test_y - assert\ta == b", 400) == (
        "FAILED tests/test_x.py::test_y - assert a == b"
    )
    assert s("see pyproject.toml and check.yml", 400) == (
        "see pyproject.toml and check.yml"
    )
    assert s("took 12:34:56", 400) == "took 12:34:56"
    assert s("ran /Users/reto/bin/x", 400) == "ran <home>"
    assert s("localhost refused", 400) == "localhost refused"


def test_sanitize_log_keeps_version_shapes_and_scope_operators() -> None:
    """Dotted digit strings and ``name<digit>.<digit>`` versions are not
    hostnames; an underscored label still masks; ``::`` inside a word
    (scope operators) is not an IPv6 tail."""
    s = ci_watch._sanitize_log
    assert s("assert 1.5 == 2.0", 400) == "assert 1.5 == 2.0"
    assert s("python3.12 not found", 400) == "python3.12 not found"
    assert s("gh v0.8.37 required", 400) == "gh v0.8.37 required"
    assert s("db_host.cluster.internal", 400) == "<host>"
    assert s("connect node-a.cluster.internal:5432 refused", 400) == (
        "connect <host> refused"
    )
    assert s("FAILED tests/x.py::decode_thing", 400) == (
        "FAILED tests/x.py::decode_thing"
    )
    assert s("see std::cerr", 400) == "see std::cerr"


def test_matrix_job_names_get_distinct_infra_signatures() -> None:
    """The hostname strip never runs on job names (``_sanitize_name``):
    a matrix's ``3.12``/``3.13`` legs file DIFFERENT infra signatures —
    a runner kill on one leg must not dedupe onto the other."""
    a = ci_watch._infra_sig("test-linux (3.12, 1)")
    b = ci_watch._infra_sig("test-linux (3.13, 1)")
    assert a != b
    assert a == "ci:check.yml:infra:test-linux (3.12, 1)"
    assert ci_watch._sanitize_name("test-linux (3.12, 1)\t", 200) == (
        "test-linux (3.12, 1)"
    )
