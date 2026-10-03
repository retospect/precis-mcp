"""Tests for ``scripts/last-gated-main-sha`` — the range start check.yml's lane
picker uses on a main push (backlog/main-stays-gated.md).

The defect it closes: the concurrency group cancels superseded main runs, so a
qland burst leaves pushes with no verdict; scoping the lane decision to
``github.event.before`` then lets a docs-only push take the docs lane while main
carries src no shard ran. So the assertions here are all about what is
*refused*: a partially-cancelled matrix, a docs-lane sha with no shards at all,
a red lint, and an unanswerable ``gh`` — each must decline to name a gated sha,
since an empty answer makes check.yml gate fully.

``gh`` is replaced at the module boundary (``_run``) with fixture JSON shaped
like the real ``gh api graphql`` response (UPPERCASE conclusions, null while a
run is in flight); ``git`` is stubbed too, except in the tests that pin the
window and ``--start`` behaviour against a real temp repo.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "last-gated-main-sha"

GATED = "aaaa111100000000000000000000000000000000"
BURST = "bbbb222200000000000000000000000000000000"
DOCS = "cccc333300000000000000000000000000000000"
OLDER = "dddd444400000000000000000000000000000000"


def _load() -> ModuleType:
    # Loaded from a `.py`-suffixed copy, not the dotless executable: testmon
    # fingerprints by extension and IndexErrors on a dotless file, which
    # INTERNALERRORs any `--impacted` run touching this test (gr450298) — same
    # convention as tests/test_main_ci_status.py.
    tmp_copy = (
        Path(tempfile.mkdtemp(prefix="last_gated_py_")) / "last_gated_main_sha.py"
    )
    shutil.copyfile(SCRIPT, tmp_copy)
    loader = importlib.machinery.SourceFileLoader("last_gated_main_sha", str(tmp_copy))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _run_node(name: str, conclusion: str | None, hours_ago: float) -> dict[str, Any]:
    return {"name": name, "conclusion": conclusion, "completedAt": _iso(hours_ago)}


def _shards(
    conclusion: str | None = "SUCCESS",
    n: int = 6,
    *,
    hours_ago: float = 1.0,
    lint: str | None = "SUCCESS",
) -> list[dict[str, Any]]:
    """`lint` plus `n` test-linux check-runs, as one commit's runs.

    Shards finish at slightly different times, as a real matrix does — spread
    backwards from `hours_ago` so the newest is exactly `hours_ago` old. The
    age of a matrix is the age of its SLOWEST member; staggering them is what
    makes that assertion mean something. Lint finishes an hour earlier than the
    first shard; `lint=None` omits it.
    """
    runs = [
        _run_node(f"test-linux (3.13, {i + 1})", conclusion, hours_ago + i / 30)
        for i in range(n)
    ]
    if lint is not None:
        runs.append(_run_node("lint", lint, hours_ago + 1.0))
    return runs


def _commit(oid: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """A GraphQL history node: runs sit in one check suite beside an unrelated
    app's empty one, as on GitHub."""
    return {
        "oid": oid,
        "checkSuites": {
            "nodes": [
                {"checkRuns": {"nodes": runs}},
                {"checkRuns": {"nodes": []}},
            ]
        },
    }


def _response(nodes: list[dict[str, Any]], more: str | None = None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "history": {
                                "pageInfo": {
                                    "hasNextPage": more is not None,
                                    "endCursor": more,
                                },
                                "nodes": nodes,
                            }
                        }
                    }
                }
            }
        }
    )


def _stub(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    checks: dict[str, list[dict[str, Any]]],
    *,
    pages: int = 1,
    calls: list[tuple[str, ...]] | None = None,
) -> None:
    """Feed a fixed first-parent walk (dict order = newest first) and a GraphQL
    history answer. With `pages` == 2 the first commit is on page one and the
    rest on page two (cursor `"1"`)."""
    order = list(checks)
    nodes = [_commit(sha, checks[sha]) for sha in order]
    chunks = [nodes] if pages == 1 else [nodes[:1], nodes[1:]]

    def fake_run(*args: str) -> str | None:
        if calls is not None:
            calls.append(args)
        if args[0] != "gh":
            return "\n".join(order)
        after = next((a[6:] for a in args if a.startswith("after=")), None)
        idx = int(after) if after else 0
        return _response(chunks[idx], "1" if idx + 1 < len(chunks) else None)

    monkeypatch.setattr(mod, "_run", fake_run)


def _stub_raw(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, gh_out: str | None
) -> None:
    """Fixed first-parent walk of [GATED]; `gh` answers `gh_out` verbatim."""

    def fake_run(*args: str) -> str | None:
        return GATED if args[0] != "gh" else gh_out

    monkeypatch.setattr(mod, "_run", fake_run)


def test_all_shards_green_is_the_gated_sha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_partially_cancelled_matrix_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One green shard beside five cancelled ones is the exact state a burst
    produces. Counting it would hand check.yml a range start that skips
    everything the cancelled shards never ran."""
    mod = _load()
    mixed = _shards("CANCELLED")
    mixed[0]["conclusion"] = "SUCCESS"
    _stub(mod, monkeypatch, {BURST: mixed, GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_docs_lane_sha_has_no_shards_and_is_walked_past(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A docs-only push runs `lint` + `test-docs`, no `test-linux` at all. An
    empty shard list is not unanimous success — it is no verdict."""
    mod = _load()
    docs = [_run_node("lint", "SUCCESS", 1), _run_node("test-docs", "SUCCESS", 1)]
    _stub(mod, monkeypatch, {DOCS: docs, GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_red_lint_with_green_shards_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards(lint="FAILURE"), GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_missing_lint_run_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards(lint=None), GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_five_green_shards_are_not_enough(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards(n=5), GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_nightly_shape_all_green_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sha the nightly also ran carries 2 lint + 18 test-linux runs. "At
    least six", not "exactly six"."""
    mod = _load()
    nightly = _shards(n=18) + [_run_node("lint", "SUCCESS", 2)]
    _stub(mod, monkeypatch, {GATED: nightly})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_one_red_nightly_leg_disqualifies_the_sha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    nightly = _shards(n=18)
    nightly[17]["conclusion"] = "FAILURE"
    _stub(mod, monkeypatch, {BURST: nightly, GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_in_flight_run_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A still-running shard reports `conclusion: null`."""
    mod = _load()
    running = _shards()
    running[2]["conclusion"] = None
    running[2]["completedAt"] = None
    _stub(mod, monkeypatch, {BURST: running, GATED: _shards()})
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_nothing_gated_in_the_window_is_exit_two_and_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Looked, found none" is exit 2 with empty stdout — empty is still what
    makes check.yml gate fully, but the exit code tells the drift guard this is
    an answer and not an outage."""
    mod = _load()
    docs = [_run_node("lint", "SUCCESS", 1)]
    _stub(mod, monkeypatch, {BURST: _shards("CANCELLED"), DOCS: docs})
    assert mod.main([]) == 2
    assert capsys.readouterr().out == ""


def test_exit_two_also_in_age_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards("CANCELLED"), DOCS: _shards(n=0)})
    assert mod.main(["--age-hours"]) == 2
    assert capsys.readouterr().out == ""


def test_exit_two_needs_every_page_the_walk_needed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure on page two, after a clean page one with nothing green, is
    unknowable (exit 0), not "found none"."""
    mod = _load()
    nodes = [_commit(BURST, _shards("CANCELLED"))]

    def fake_run(*args: str) -> str | None:
        if args[0] != "gh":
            return f"{BURST}\n{GATED}"
        if any(a.startswith("after=") for a in args):
            return None
        return _response(nodes, more="1")

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main([]) == 0
    assert capsys.readouterr().out == ""


def test_unresolvable_start_is_unknowable_not_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()

    def fake_run(*args: str) -> str | None:
        return None if args[0] == "git" else _response([])

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main(["--start", "no-such-ref"]) == 0
    assert capsys.readouterr().out == ""


def test_graphql_errors_are_unknowable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A response carrying `errors` is "we don't know", even when partial data
    came back alongside it."""
    mod = _load()
    good = json.loads(_response([_commit(GATED, _shards())]))
    good["errors"] = [{"message": "Something went wrong"}]
    _stub_raw(mod, monkeypatch, json.dumps(good))
    assert mod.main([]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "gh_out",
    [
        None,
        "",
        "not json",
        "[]",
        '{"data": null}',
        '{"data": {"repository": {"ref": null}}}',
        '{"data": {"repository": {"ref": {"target": {"history": {"nodes": 3}}}}}}',
    ],
)
def test_unusable_gh_answers_print_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gh_out: str | None,
) -> None:
    mod = _load()
    _stub_raw(mod, monkeypatch, gh_out)
    assert mod.main([]) == 0
    assert capsys.readouterr().out == ""
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out == ""


def test_pagination_finds_a_green_sha_on_page_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    calls: list[tuple[str, ...]] = []
    _stub(
        mod,
        monkeypatch,
        {BURST: _shards("CANCELLED"), GATED: _shards()},
        pages=2,
        calls=calls,
    )
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED
    gh_calls = [c for c in calls if c[0] == "gh"]
    assert len(gh_calls) == 2
    assert "after=1" not in gh_calls[0]
    assert "after=1" in gh_calls[1]


def test_green_on_first_page_stops_paging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    calls: list[tuple[str, ...]] = []
    _stub(
        mod,
        monkeypatch,
        {GATED: _shards(), BURST: _shards("CANCELLED")},
        pages=2,
        calls=calls,
    )
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED
    assert len([c for c in calls if c[0] == "gh"]) == 1


def test_older_green_does_not_win_over_an_unfetched_newer_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Page one covers only the second-newest local sha; the newest (green) is
    on page two. The walk must wait for it, not settle for the older one."""
    mod = _load()
    nodes = [_commit(OLDER, _shards()), _commit(GATED, _shards())]

    def fake_run(*args: str) -> str | None:
        if args[0] != "gh":
            return f"{GATED}\n{OLDER}"
        if any(a.startswith("after=") for a in args):
            return _response([nodes[1]])
        return _response([nodes[0]], more="1")

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_page_cap_follows_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never more than ceil(limit / 100) pages, even if GitHub says more."""
    mod = _load()
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str | None:
        calls.append(args)
        return GATED if args[0] != "gh" else _response([], more="x")

    monkeypatch.setattr(mod, "_run", fake_run)
    # Cut by the cap with nothing green in what was covered: still "looked,
    # found none" (exit 2) — the whole bounded window was looked at.
    assert mod.main([]) == 2
    assert len([c for c in calls if c[0] == "gh"]) == 5
    calls.clear()
    assert mod.main(["--limit", "100"]) == 2
    assert len([c for c in calls if c[0] == "gh"]) == 1


def test_sha_absent_from_main_history_is_walked_past(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--start` on a branch: its local-only commits are not in main's history
    and so are not green; the walk continues down to a main sha."""
    mod = _load()

    def fake_run(*args: str) -> str | None:
        if args[0] != "gh":
            return f"{DOCS}\n{GATED}"
        return _response([_commit(GATED, _shards())])

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main(["--start", "feature"]) == 0
    assert capsys.readouterr().out.strip() == GATED


def test_the_query_is_batched_graphql_with_a_since_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load()
    calls: list[tuple[str, ...]] = []
    _stub(mod, monkeypatch, {GATED: _shards()}, calls=calls)
    assert mod.main(["--hours", "6"]) == 0
    git_call = next(c for c in calls if c[0] == "git")
    gh_call = next(c for c in calls if c[0] == "gh")
    assert gh_call[:3] == ("gh", "api", "graphql")
    assert "owner={owner}" in gh_call and "name={repo}" in gh_call
    since = next(a for a in gh_call if a.startswith("since="))[6:]
    age = datetime.now(UTC) - datetime.fromisoformat(since)
    assert abs(age.total_seconds() / 3600 - 6) < 0.1
    assert f"--since={since}" in git_call


def test_limit_bounds_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    seen: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str | None:
        seen.append(args)
        return "" if args[0] != "gh" else None

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main(["--limit", "3", "--start", "deadbeef"]) == 0
    assert seen[0][:3] == ("git", "rev-list", "--first-parent")
    assert "-n3" in seen[0] and seen[0][-1] == "deadbeef"


def test_default_bound_is_48h_and_500_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load()
    assert (mod.WINDOW_HOURS, mod.MAX_COMMITS) == (48, 500)
    seen: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str | None:
        seen.append(args)
        return ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main([]) == 0
    assert "-n500" in seen[0]
    since = next(a for a in seen[0] if a.startswith("--since="))[8:]
    age = datetime.now(UTC) - datetime.fromisoformat(since)
    assert abs(age.total_seconds() / 3600 - 48) < 0.1


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_docstring(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    mod = _load()
    assert mod.main([flag]) == 0
    out = capsys.readouterr().out
    assert "newest `completedAt` across the chosen sha's `lint` + `test-linux`" in out
    assert "Empty output means unknowable" in out
    assert "never refuses" in out
    assert "exit 2, empty" in out and "not a crash" in out


# -- the 6 is spelled in check.yml; the constant must follow it ---------------


def test_gate_shards_matches_the_check_yml_matrix() -> None:
    """GATE_SHARDS is the test-linux `shard:` list length in check.yml, and the
    `pytest --shard K/N` denominator. Parsed from the workflow's YAML, not a
    literal: add a shard there and this reddens until the constant follows."""
    mod = _load()
    wf = yaml.safe_load(
        (REPO / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    )
    job = wf["jobs"]["test-linux"]
    shards = job["strategy"]["matrix"]["shard"]
    assert isinstance(shards, list)
    assert len(shards) == mod.GATE_SHARDS
    runs = [s.get("run", "") for s in job["steps"] if isinstance(s, dict)]
    assert any(f"--shard ${{{{ matrix.shard }}}}/{mod.GATE_SHARDS}" in r for r in runs)


# -- real git: the window and --start --------------------------------------


def _git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        env |= {"GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return cp.stdout.strip()


def _history(repo: Path, ages_hours: list[float]) -> list[str]:
    """A linear temp repo, one commit per age; returns shas newest first.
    `ages_hours` must be given newest first."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i, age in reversed(list(enumerate(ages_hours))):
        when = datetime.now(UTC) - timedelta(hours=age)
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}", when=when)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return shas[::-1]


def _real_git_stub(
    mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    checks: dict[str, list[dict[str, Any]]],
) -> None:
    """Real `git` against the cwd; `gh` answers all of `checks` as main's
    history regardless of `since` (the cutoff is git's job here)."""
    real = mod._run

    def fake_run(*args: str) -> str | None:
        if args[0] == "git":
            return real(*args)
        return _response([_commit(s, r) for s, r in checks.items()])

    monkeypatch.setattr(mod, "_run", fake_run)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_commit_older_than_the_window_is_not_considered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A green sha 60 h old is outside the 48 h window — empty, not stale."""
    mod = _load()
    newest, old = _history(tmp_path, [1.0, 60.0])
    monkeypatch.chdir(tmp_path)
    _real_git_stub(mod, monkeypatch, {newest: _shards("CANCELLED"), old: _shards()})
    assert mod.main([]) == 2
    assert capsys.readouterr().out == ""
    # ...and the window is a knob.
    assert mod.main(["--hours", "100"]) == 0
    assert capsys.readouterr().out.strip() == old


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("flags", [[], ["--age-hours"]])
def test_empty_local_window_is_nothing_to_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    """Every commit older than --hours: nothing to judge, exit 0 and empty —
    not exit 2, which would refuse a ship on a repo that simply is quiet."""
    mod = _load()
    (only,) = _history(tmp_path, [10.0])
    monkeypatch.chdir(tmp_path)
    _real_git_stub(mod, monkeypatch, {only: _shards("CANCELLED")})
    assert mod.main(["--hours", "2", *flags]) == 0
    assert capsys.readouterr().out == ""
    assert mod.main(["--hours", "20", *flags]) == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_start_at_an_older_commit_skips_newer_shas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load()
    newest, middle, oldest = _history(tmp_path, [1.0, 5.0, 9.0])
    monkeypatch.chdir(tmp_path)
    _real_git_stub(
        mod,
        monkeypatch,
        {newest: _shards(), middle: _shards("CANCELLED"), oldest: _shards()},
    )
    assert mod.main([]) == 0
    assert capsys.readouterr().out.strip() == newest
    assert mod.main(["--start", middle]) == 0
    assert capsys.readouterr().out.strip() == oldest


@pytest.mark.skipif(sys.platform == "win32", reason="stub gh is a POSIX sh script")
def test_missing_gh_is_silent_and_exits_zero(tmp_path: Path) -> None:
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "--limit", "2"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )
    assert cp.returncode == 0
    assert cp.stdout == ""


# ── --age-hours: the number scripts/ship --quick refuses past ───────────────
#
# Reto 2026-09-30, "a day or two" → warn at 24h, refuse at 48h. These pin the
# arithmetic and, more importantly, the direction of every unknown: a drift
# guard that refuses on a lookup it could not answer would let a GitHub outage
# stop the whole fleet from landing, which is worse than the ungated main it
# is guarding against.


def test_age_hours_reports_the_newest_verdict_age(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    # Newest stamp is the first shard's: 30 h old (lint finished an hour before).
    _stub(mod, monkeypatch, {GATED: _shards(hours_ago=30.0)})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 30.0) < 0.2


def test_age_is_the_slowest_job_not_the_fastest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate is not done until its last job reports. Taking the oldest stamp
    would age the verdict by however long the slowest job took and could tip a
    healthy main over the refusal line."""
    mod = _load()
    runs = _shards(hours_ago=50.0)
    runs[0]["completedAt"] = _iso(2.0)
    _stub(mod, monkeypatch, {GATED: runs})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 2.0) < 0.2


def test_age_counts_lint_when_it_finishes_last(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    runs = _shards(hours_ago=30.0, lint=None) + [_run_node("lint", "SUCCESS", 4.0)]
    _stub(mod, monkeypatch, {GATED: runs})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 4.0) < 0.2


def test_age_ignores_runs_outside_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresher unrelated check-run (a bot, say) does not make the verdict
    younger."""
    mod = _load()
    runs = _shards(hours_ago=30.0) + [_run_node("some-bot", "SUCCESS", 0.1)]
    _stub(mod, monkeypatch, {GATED: runs})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 30.0) < 0.2


def test_age_hours_prints_nothing_when_nothing_is_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No answer, not a large one. scripts/ship reads empty as "do not refuse
    on this" — the guard only ever blocks on a number it actually has."""
    mod = _load()
    _stub(mod, monkeypatch, {BURST: _shards("CANCELLED")})
    assert mod.main(["--age-hours"]) == 2
    assert capsys.readouterr().out.strip() == ""


def test_age_hours_prints_nothing_when_gh_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load()
    _stub_raw(mod, monkeypatch, None)
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_an_unreadable_timestamp_is_unknown_not_older(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green sha whose stamps will not parse ends the walk empty-handed
    rather than falling through to an older gated sha. Reporting the older
    one's age would overstate the drift and could refuse a ship that should
    have gone through."""
    mod = _load()
    unreadable = _shards()
    for r in unreadable:
        r["completedAt"] = "not-a-timestamp"
    _stub(
        mod,
        monkeypatch,
        {BURST: unreadable, GATED: _shards(hours_ago=40.0)},
    )
    assert mod.main(["--age-hours"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_age_mode_does_not_change_which_sha_counts_as_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both modes walk with the same predicate; only the thing printed differs.
    A partially-cancelled matrix is no more a verdict for the drift guard than
    it is for the lane picker."""
    mod = _load()
    mixed = _shards("CANCELLED", hours_ago=3.0)
    mixed[0]["conclusion"] = "SUCCESS"
    _stub(mod, monkeypatch, {BURST: mixed, GATED: _shards(hours_ago=12.0)})
    assert mod.main(["--age-hours"]) == 0
    assert abs(float(capsys.readouterr().out.strip()) - 12.0) < 0.2
