"""``precis.plans.red_waiver``: the pure parts of the release gate's
red-test waiver — the decision, the hard-stop lists, the digest parse and
the git reads (a throwaway repo). Store-backed parts: ``test_red_waiver_db``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from precis.plans import red_waiver as W

OK_PATHS = ["src/precis/handlers/gripe.py", "tests/test_gripe.py", "docs/x.md"]
OK_TEST = "tests/test_widgets.py::test_blue"


def _decide(
    tests: list[str] | None = None,
    paths: list[str] | None = None,
    prior: set[str] | None = None,
) -> tuple[bool, str]:
    return W.waiver_decision(
        [OK_TEST] if tests is None else tests,
        OK_PATHS if paths is None else paths,
        prior or set(),
    )


# ── the decision ───────────────────────────────────────────────────


def test_cap_is_two() -> None:
    assert W.MAX_WAIVED_TESTS == 2


def test_grants_one_and_two_clean_tests() -> None:
    ok, why = _decide()
    assert ok and "1 failing test(s)" in why and "3 range path(s)" in why
    ok, _ = _decide(tests=[OK_TEST, "tests/test_widgets.py::test_red"])
    assert ok


def test_refuses_no_tests() -> None:
    ok, why = _decide(tests=[])
    assert not ok and "no failing test id" in why
    ok, _ = _decide(tests=["", "  "])
    assert not ok


def test_refuses_over_the_cap() -> None:
    three = [f"tests/test_w.py::test_{i}" for i in range(3)]
    ok, why = _decide(tests=three)
    assert not ok and "3 failing tests exceed the waiver cap of 2" in why


def test_duplicate_ids_count_once() -> None:
    ok, _ = _decide(tests=[OK_TEST, OK_TEST, OK_TEST])
    assert ok


@pytest.mark.parametrize("word", W.TEST_HARD_STOPS)
def test_every_test_hard_stop_word_refuses(word: str) -> None:
    ok, why = _decide(tests=[f"tests/test_x.py::test_{word}_thing"])
    assert not ok and "hard-stop pattern" in why and word in why


def test_test_hard_stop_is_case_insensitive_on_path_and_name() -> None:
    assert not _decide(tests=["tests/test_Migrations.py::test_ok"])[0]
    assert not _decide(tests=["tests/test_ok.py::test_OAuth_flow"])[0]
    assert not _decide(tests=["tests/store/test_ok.py::test_fine"])[0]


def test_test_hard_stops_cover_the_ruling() -> None:
    ruled = {
        "migration",
        "auth",
        "oauth",
        "vault",
        "credential",
        "secret",
        "safe_fetch",
        "ssrf",
        "release",
        "round",
        "ship",
        "deploy",
        "gate",
        "store",
        "chunk",
        "schema",
        "invariant",
        "backup",
        # the review's widening
        "migrat",
        "ci_",
        "bump",
        "waiver",
    }
    assert ruled <= set(W.TEST_HARD_STOPS)


@pytest.mark.parametrize(
    "path",
    [
        "src/precis/migrations/0123_x.sql",
        "db/migrations/0001.sql",
        "migrations/0001.sql",
        "src/precis/utils/safe_fetch.py",
        "src/precis/auth_helper.py",
        "src/precis/oauth/flow.py",
        "src/precis/vault.py",
        "src/precis/credentials.py",
        "src/precis/utils/secret_scan.py",
        "scripts/release",
        "scripts/round",
        "scripts/ship",
        "scripts/lib/ci_run.py",
        ".github/workflows/check.yml",
        "deploy/ansible/site.yml",
        "src/precis/plans/release.py",
        "src/precis/plans/gates.py",
        "src/precis/workers/job_types/release_cut.py",
        "src/precis/workers/job_types/release_confirm.py",
        "src/precis/workers/job_types/ci_red_digest.py",
        "src/precis/workers/job_types/ci_verdict_poll.py",
        "src/precis/store/_refs_ops.py",
        "src/precis/store/store.py",
        "src/precis/plans/red_waiver.py",
        # substrings matched ANYWHERE in the path (the review's widening)
        "docs/runbooks/release-cycle.md",
        "src/precis/anything/deployer.py",
        "src/precis/roundtrip.py",
        "src/precis/tools/shipping.py",
        "ops/backup-nightly.sh",
        "src/precis/gateway.py",
        "db/migrate.sql",
        "tools/bump-version.py",
        "tools/ci-helper.sh",
        "tools/ci_helper.py",
        "src/precis/trust_rules.py",
        "src/precis/x/safe_fetch_extra.py",
        "src/precis/foo/store/x.py",
        "docs/backlog/waiver-notes.md",
    ],
)
def test_every_hard_stop_path_refuses(path: str) -> None:
    assert W.path_hard_stop(path) is not None
    ok, why = _decide(paths=[*OK_PATHS, path])
    assert not ok and path in why


@pytest.mark.parametrize(
    "path",
    [
        "src/precis/handlers/gripe.py",
        "src/precis/workers/job_types/run_slice.py",
        "src/precis/workers/job_types/plan_dispatch.py",
        "tests/test_gripe.py",
        "docs/backlog/x.md",
        "scripts/coderef",
    ],
)
def test_ordinary_paths_pass(path: str) -> None:
    assert W.path_hard_stop(path) is None


def test_refuses_an_empty_range() -> None:
    ok, why = _decide(paths=[])
    assert not ok and "unreadable" in why


def test_refuses_a_second_consecutive_waiver_of_the_same_test() -> None:
    ok, why = _decide(prior={OK_TEST})
    assert not ok and "already waived and still unfixed" in why
    assert OK_TEST in why


def test_a_different_prior_test_does_not_block() -> None:
    assert _decide(prior={"tests/test_other.py::test_x"})[0]


def test_one_repeat_among_two_refuses() -> None:
    ok, why = _decide(tests=[OK_TEST, "tests/test_w.py::test_new"], prior={OK_TEST})
    assert not ok and OK_TEST in why and "test_new" not in why


def test_check_order_names_the_first_refusal() -> None:
    """Count before test names before paths before repeats."""
    ok, why = W.waiver_decision(
        [f"tests/test_migration_{i}.py::t" for i in range(3)],
        ["scripts/ship"],
        {"tests/test_migration_0.py::t"},
    )
    assert not ok and "exceed the waiver cap" in why
    ok, why = W.waiver_decision(
        ["tests/test_migration.py::t"], ["scripts/ship"], {"tests/test_migration.py::t"}
    )
    assert not ok and "failing test" in why
    ok, why = W.waiver_decision([OK_TEST], ["scripts/ship"], {OK_TEST})
    assert not ok and "hard-stop path" in why


def test_many_path_hits_are_summarised() -> None:
    paths = [f"scripts/lib/m{i}.py" for i in range(9)]
    ok, why = _decide(paths=paths)
    assert not ok and "(+5 more)" in why


# ── parse_failures ─────────────────────────────────────────────────

DIGEST = """\
== test-linux (3) ==
  failed ids (2):
    tests/test_widgets.py::test_blue
    tests/test_widgets.py::test_red[a-b]
  FAILED tests/test_widgets.py::test_blue - AssertionError: assert 1 == 2
  FAILED tests/test_widgets.py::test_red[a-b] - ValueError: boom
  E   assert 1 == 2
  =========== 2 failed, 40 passed in 3.2s ===========
"""


def test_parse_reads_the_failed_ids_block_and_reasons() -> None:
    failures, problems = W.parse_failures(DIGEST)
    assert problems == []
    assert list(failures) == [
        "tests/test_widgets.py::test_blue",
        "tests/test_widgets.py::test_red[a-b]",
    ]
    assert failures["tests/test_widgets.py::test_blue"] == (
        "AssertionError: assert 1 == 2"
    )
    assert failures["tests/test_widgets.py::test_red[a-b]"] == "ValueError: boom"


def test_parse_two_jobs_merge_and_dedupe() -> None:
    text = DIGEST + (
        "== test-linux (4) ==\n"
        "  failed ids (1):\n"
        "    tests/test_widgets.py::test_blue\n"
        "  ===== 1 failed, 9 passed in 1s =====\n"
    )
    failures, problems = W.parse_failures(text)
    assert problems == []
    assert "tests/test_widgets.py::test_blue" in failures


def test_parse_falls_back_to_summary_lines_without_a_block() -> None:
    text = (
        "== t ==\n  FAILED tests/test_a.py::test_x - AssertionError: no\n"
        "  === 1 failed in 1s ===\n"
    )
    failures, problems = W.parse_failures(text)
    assert problems == []
    assert failures == {"tests/test_a.py::test_x": "AssertionError: no"}


def test_parse_refuses_a_count_mismatch_or_a_missing_count() -> None:
    """3 failed but only 2 ids listed (a cap or a cut); errors count too;
    no summary line at all cannot be checked."""
    three = DIGEST.replace("2 failed, 40", "3 failed, 39")
    assert any("summary counts 3" in p for p in W.parse_failures(three)[1])
    plus_err = DIGEST.replace("2 failed, 40 passed", "2 failed, 40 passed, 1 error")
    assert any("summary counts 3" in p for p in W.parse_failures(plus_err)[1])
    fewer = DIGEST.replace("2 failed, 40", "1 failed, 41")
    assert any("summary counts 1" in p for p in W.parse_failures(fewer)[1])
    nocount = DIGEST.replace(
        "=========== 2 failed, 40 passed in 3.2s ===========\n", ""
    )
    assert any("no pytest 'N failed' count" in p for p in W.parse_failures(nocount)[1])
    exact = DIGEST.replace("2 failed, 40 passed", "1 failed, 1 error, 40 passed")
    assert W.parse_failures(exact)[1] == []


def test_parse_refuses_a_header_that_disagrees_with_its_ids() -> None:
    text = DIGEST.replace("failed ids (2):", "failed ids (3):")
    assert any("header says 3" in p for p in W.parse_failures(text)[1])


def test_parse_refuses_when_the_full_matrix_never_ran() -> None:
    first = (
        "note: 6 test-linux shard job(s) skipped by design — the matrix runs "
        "only after a green failed-first\n"
        "== failed-first ==\n  failed ids (1):\n    tests/a.py::t\n"
        "  FAILED tests/a.py::t - AssertionError: x\n  === 1 failed in 1s ===\n"
    )
    problems = W.parse_failures(first)[1]
    assert any("skipped by design" in p for p in problems)
    assert any("full test matrix never ran" in p for p in problems)
    named = first.replace("skipped by design", "x").replace("note:", "n:")
    assert any("never ran" in p for p in W.parse_failures(named)[1])
    note_only = DIGEST + "note: 1 job skipped by design\n"
    assert any("skipped by design" in p for p in W.parse_failures(note_only)[1])


@pytest.mark.parametrize(
    "poison",
    [
        "  ERROR tests/test_a.py - ImportError: no module",  # file id, no ::
        "  !!!! Interrupted: 1 error during collection !!!!",
        "  Interrupted: 2 errors during collection",
        "  ERROR collecting tests/test_a.py",
        "  1 error during collection",
    ],
)
def test_parse_refuses_a_collection_error(poison: str) -> None:
    failures, problems = W.parse_failures(DIGEST + poison + "\n")
    assert problems, poison
    # the same lines arriving through the jobs-logs rescue count too
    blind = "== gate-fast ==\n  (no signal lines; tail)\n"
    rescue = {
        "gate-fast": [
            "FAILED tests/test_a.py::test_x - AssertionError: y",
            "=== 1 failed in 1s ===",
            poison.strip(),
        ]
    }
    assert W.parse_failures(blind, rescue)[1], poison


def test_parse_refuses_an_id_with_no_test_name() -> None:
    text = "== t ==\n  failed ids (1):\n    tests/test_a.py\n  === 1 failed in 1s ===\n"
    assert any("without a test name" in p for p in W.parse_failures(text)[1])


def test_parse_refuses_a_rescue_cut_at_the_cap() -> None:
    blind = "== gate-fast ==\n  (no signal lines; tail)\n"
    lines = [
        "FAILED tests/test_a.py::test_x - AssertionError: y",
        "=== 1 failed in 1s ===",
    ]
    assert W.parse_failures(blind, {"gate-fast": lines})[1] == []
    cut = {"gate-fast": [*lines, W.RESCUE_TRUNCATED]}
    assert any("cut at the cap" in p for p in W.parse_failures(blind, cut)[1])


def _job(
    name: str, conclusion: str | None, status: str = "completed"
) -> dict[str, str]:
    return {"name": name, "conclusion": conclusion or "", "status": status}


def test_partial_run_problem() -> None:
    ok = {
        "status": "completed",
        "jobs": [_job("lint", "success"), _job("test (1)", "failure")],
    }
    assert W.partial_run_problem(ok) is None
    assert "unreadable" in (W.partial_run_problem(None) or "")
    assert "not completed" in (
        W.partial_run_problem({"status": "in_progress", "jobs": ok["jobs"]}) or ""
    )
    assert "no jobs" in (W.partial_run_problem({"status": "completed"}) or "")
    for odd in (
        "cancelled",
        "timed_out",
        "startup_failure",
        "action_required",
        "neutral",
        "stale",
        "",
    ):
        state = {"status": "completed", "jobs": [*ok["jobs"], _job("shard 3", odd)]}
        why = W.partial_run_problem(state) or ""
        assert "shard 3" in why, odd
    # skipped is normal: check.yml always skips its conditional jobs
    skipped = {
        "status": "completed",
        "jobs": [*ok["jobs"], _job("test-docs", "skipped")],
    }
    assert W.partial_run_problem(skipped) is None


#: The real r266 red run (check.yml run 42843464447, `gh run view --json
#: jobs`): two gate-fast shards failed; every conditional job was skipped.
R266_JOBS = [
    ("plan", "success"),
    ("lint", "success"),
    ("coverage-db", "skipped"),
    ("gate-fast (3.13, 4)", "failure"),
    ("gate-fast (3.13, 5)", "success"),
    ("gate-fast (3.13, 7)", "success"),
    ("gate-fast (3.13, 1)", "success"),
    ("gate-fast (3.13, 3)", "success"),
    ("gate-fast (3.13, 2)", "success"),
    ("gate-fast (3.13, 8)", "failure"),
    ("gate-fast (3.13, 9)", "success"),
    ("gate-fast (3.13, 6)", "success"),
    ("gate-fast (3.13, 10)", "success"),
    ("test-other", "skipped"),
    ("failed-first", "skipped"),
    ("test-docs", "skipped"),
    (
        "${{ (((needs.plan.outputs.full == 'true') && 'test-linux') || 'soak') }} "
        "(${{ matrix.python-version }}, ${{ matrix.shard }})",
        "skipped",
    ),
    ("verdict-soak", "skipped"),
    ("publish", "skipped"),
]


def r266_state() -> dict[str, Any]:
    return {
        "status": "completed",
        "conclusion": "failure",
        "attempt": 1,
        "jobs": [
            {"name": n, "status": "completed", "conclusion": c} for n, c in R266_JOBS
        ],
    }


def test_the_real_r266_red_run_shape_is_a_complete_picture() -> None:
    """N1: the waiver must grant on a real check.yml red, whose skipped
    conditional jobs are not holes."""
    assert W.partial_run_problem(r266_state()) is None
    assert not W.run_in_progress(r266_state())
    # one shard that never reported is still a hole
    state = r266_state()
    state["jobs"][4]["conclusion"] = "cancelled"
    assert "gate-fast (3.13, 5)=cancelled" in (W.partial_run_problem(state) or "")


def test_run_in_progress() -> None:
    assert W.run_in_progress({"status": "in_progress", "jobs": []})
    assert W.run_in_progress({"status": "queued"})
    assert not W.run_in_progress({"status": "completed"})
    assert not W.run_in_progress(None)  # unreadable is a refusal, not a wait


def test_parse_flags_a_lint_job_as_not_a_test_red() -> None:
    text = DIGEST + "== lint ==\n  Found 1 error.\n"
    _failures, problems = W.parse_failures(text)
    assert any("job lint: no failing test ids" in p for p in problems)


def test_parse_flags_a_truncated_id_list() -> None:
    text = (
        "== t ==\n  failed ids (25):\n    tests/a.py::t1\n"
        "    … +5 more in the full log\n"
    )
    failures, problems = W.parse_failures(text)
    assert list(failures) == ["tests/a.py::t1"]
    assert any("truncated (+5 more)" in p for p in problems)


def test_parse_blind_section_uses_the_rescue_or_flags_it() -> None:
    blind = "== gate-fast ==\n  (no signal lines; tail)\n    step header\n"
    failures, problems = W.parse_failures(blind)
    assert failures == {} and any("blind" in p for p in problems)
    failures, problems = W.parse_failures(
        blind,
        {
            "gate-fast": [
                "FAILED tests/test_a.py::test_x - AssertionError: y",
                "=== 1 failed in 2s ===",
            ]
        },
    )
    assert problems == []
    assert failures == {"tests/test_a.py::test_x": "AssertionError: y"}


def test_parse_no_section_is_a_problem() -> None:
    assert W.parse_failures("ci-failed: nothing")[1] == [
        "the digest has no failed-job section"
    ]


# ── imports / paths / git ──────────────────────────────────────────


def test_imported_src_paths() -> None:
    src = (
        "import os\nimport precis.plans.gates\nfrom precis.workers import x\n"
        "from precis import settings\nfrom other import precis_thing\n"
    )
    paths = W.imported_src_paths(src)
    assert "src/precis/plans/gates.py" in paths
    assert "src/precis/workers/x.py" in paths
    assert "src/precis/workers/__init__.py" in paths
    assert "src/precis/settings.py" in paths
    assert not any("other" in p or p == "os" for p in paths)
    assert W.imported_src_paths("def broken(:") == []


def test_file_of_test() -> None:
    assert W.file_of_test("tests/a.py::test_x[1]") == "tests/a.py"
    assert W.file_of_test("tests/a.py") == "tests/a.py"


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return cp.stdout.strip()


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        msg,
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


def test_range_paths_and_culprits_follow_the_test_imports(repo: Path) -> None:
    base = _commit(
        repo,
        {
            "src/precis/widgets.py": "A = 1\n",
            "src/precis/other.py": "B = 1\n",
            "tests/test_widgets.py": "from precis import widgets\n\n\ndef test_a(): ...\n",
        },
        "base",
    )
    _commit(repo, {"src/precis/widgets.py": "A = 2\n"}, "touch widgets")
    _commit(repo, {"src/precis/other.py": "B = 2\n"}, "touch other")
    tip = _commit(repo, {"docs/x.md": "hi\n"}, "docs only")
    rng = f"{base[:12]}..{tip[:12]}"
    assert sorted(W.range_paths(repo, rng) or []) == [
        "docs/x.md",
        "src/precis/other.py",
        "src/precis/widgets.py",
    ]
    commits = W.culprit_commits(repo, rng, ["tests/test_widgets.py::test_a"])
    assert [s for _sha, s in commits] == ["touch widgets"]


def test_no_attributable_culprit_is_an_empty_list_never_the_whole_range(
    repo: Path,
) -> None:
    base = _commit(
        repo,
        {"src/precis/widgets.py": "A = 1\n", "tests/test_w.py": "import json\n"},
        "base",
    )
    _commit(repo, {"src/precis/widgets.py": "A = 2\n"}, "one")
    tip = _commit(repo, {"docs/x.md": "hi\n"}, "two")
    rng = f"{base[:12]}..{tip[:12]}"
    # the test imports nothing the range touched
    assert W.culprit_commits(repo, rng, ["tests/test_w.py::test_a"]) == []
    # an unreadable / absent test file attributes nothing either
    assert W.culprit_commits(repo, rng, ["tests/missing.py::test_a"]) == []
    assert W.culprit_commits(repo, rng, []) == []


def test_a_commit_that_edits_the_failing_test_file_is_a_culprit(repo: Path) -> None:
    base = _commit(repo, {"tests/test_w.py": "import json\n"}, "base")
    _commit(repo, {"docs/x.md": "hi\n"}, "unrelated")
    tip = _commit(repo, {"tests/test_w.py": "import json\nX = 1\n"}, "edit the test")
    commits = W.culprit_commits(
        repo, f"{base[:12]}..{tip[:12]}", ["tests/test_w.py::test_a"]
    )
    assert [s for _sha, s in commits] == ["edit the test"]


def test_range_paths_unreadable_is_none(repo: Path, tmp_path: Path) -> None:
    _commit(repo, {"a.txt": "x\n"}, "base")
    assert W.range_paths(repo, "deadbeef") is None  # not a two-sided range
    assert W.range_paths(repo, "aaaa1111..bbbb2222") is None  # unknown shas
    assert W.range_paths(tmp_path / "nope", "a..b") is None  # no repo


def test_range_paths_are_not_c_quoted(repo: Path) -> None:
    """``-z`` + ``core.quotePath=false``: a non-ASCII or spaced name comes
    back verbatim, so the hard-stop substring match sees the real path."""
    base = _commit(repo, {"a.txt": "x\n"}, "base")
    tip = _commit(
        repo,
        {"docs/naïve release notes.md": "x\n", "src/ünï/backup.py": "y\n"},
        "odd names",
    )
    paths = W.range_paths(repo, f"{base[:12]}..{tip[:12]}") or []
    assert sorted(paths) == ["docs/naïve release notes.md", "src/ünï/backup.py"]
    assert W.path_hard_stop("docs/naïve release notes.md") == "*release*"


def test_range_paths_fetches_origin_before_diffing(tmp_path: Path) -> None:
    """A cut pushed from another checkout is absent here until the fetch;
    a head that stays missing after the fetch refuses (None)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    base = _commit(seed, {"a.txt": "x\n"}, "base")
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    tip = _commit(seed, {"docs/new.md": "n\n"}, "later")
    _git(seed, "push", "-q", "origin", "main")
    # the clone has never seen `tip`
    probe = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{tip}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    assert probe.returncode != 0
    paths = W.range_paths(clone, f"{base[:12]}..{tip[:12]}")
    assert paths == ["docs/new.md"]
    # a sha origin never had stays missing -> refuse
    assert W.range_paths(clone, f"{base[:12]}..{'f' * 12}") is None


# ── the gripe body ─────────────────────────────────────────────────


def test_waiver_body_masks_secrets_and_lists_every_part() -> None:
    token = "ghp_" + "A1b2C3d4" * 4 + "E5f6"
    body = W.waiver_body(
        rn="r41",
        sha="c0ffee" + "1" * 34,
        rng="aaaa..c0ffee111111",
        run_id="777",
        failures={"tests/test_w.py::test_a": f"AssertionError: leaked {token}"},
        culprits=[("abc1234", "touch widgets")],
    )
    assert token not in body and "<redacted:github token>" in body
    assert "run: 777" in body and "scripts/ci-failed 777" in body
    assert "tests/test_w.py::test_a" in body
    assert "- abc1234 touch widgets" in body
    assert "touched a failing test file or a src module it imports" in body


# ── the verdict row: a waived row survives a later red (store twin) ─


def test_a_red_never_overwrites_a_waived_row_store_side() -> None:
    from datetime import UTC, datetime

    from precis.plans import release as R

    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    sha = "c0ffee" + "1" * 34
    marker = {"gripe": 7, "tests": ["t"], "run_id": "31", "attempt": 1}
    waived = R.ci_verdict_rows(sha, "a..b", "green", "31", [], now, marker)
    assert waived[0]["waived"] == marker
    # a red from the SAME run leaves the waiver ...
    assert R.ci_verdict_rows(sha, "a..b", "red", "31", waived, now) == waived
    # ... a red from a NEWER run on the head is its own verdict
    newer = R.ci_verdict_rows(sha, "a..b", "red", "32", waived, now)
    assert [(r["verdict"], r["run_id"]) for r in newer] == [("red", "32")]
    assert "waived" not in newer[0]
    # a marker with no run id protects nothing; neither does a missing run
    loose = R.ci_verdict_rows(sha, "a..b", "green", "31", [], now, {"gripe": 7})
    assert (
        R.ci_verdict_rows(sha, "a..b", "red", "31", loose, now)[0]["verdict"] == "red"
    )
    assert R.ci_verdict_rows(sha, "a..b", "red", "", waived, now)[0]["verdict"] == "red"
    green = R.ci_verdict_rows(sha, "a..b", "green", "33", waived, now)
    assert len(green) == 1 and "waived" not in green[0]
    other = R.ci_verdict_rows("2" * 40, "a..b", "red", "5", waived, now)
    assert len(other) == 2 and other[0] == waived[0]
