"""Shard partition and CI job-budget invariants for .github/workflows/check.yml."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests._script import load_script
from tests.conftest import shard_index

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")

SCRIPT = REPO / "scripts" / "ci-shard-plan"
LIB = REPO / "scripts" / "lib"
PARTITION = load_script("shard_partition", LIB / "shard_partition.py")
CAP = 40  # GitHub Pro standard-runner concurrency


def _plan() -> ModuleType:
    # `.py` copies: testmon IndexErrors on dotless files (see
    # test_last_gated_main_sha). The copy's runpy import resolves
    # ``<tmp>/lib/shard_partition.py``, so stage the lib file beside it.
    tmp = Path(tempfile.mkdtemp(prefix="ci_shard_plan_"))
    (tmp / "lib").mkdir()
    shutil.copyfile(SCRIPT, tmp / "ci_shard_plan.py")
    shutil.copyfile(LIB / "shard_partition.py", tmp / "lib" / "shard_partition.py")
    spec = importlib.util.spec_from_file_location(
        "ci_shard_plan", tmp / "ci_shard_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The copy's ROOT resolves off the tmpdir; point it back at the repo so
    # its git ls-files list is the real one. Its shard_partition copy reads
    # no pins file there — fine: FORCE runs at untabled counts partition by
    # plain crc32 anyway.
    setattr(mod, "ROOT", REPO)
    return mod


_PLAN = _plan()
COUNTS = (_PLAN.SHARDS,)


def test_shard_count_is_fixed_10() -> None:
    """Fixed 10 for every shard run (Reto 2026-10-23: "no fewer" — the
    release-train spec's job-budget arithmetic assumes it)."""
    assert _PLAN.SHARDS == 10


@pytest.mark.parametrize(
    ("forced", "expected"),
    [("", 10), ("6", 10), ("10", 10), ("12", 12), ("20", 20)],
)
def test_force_shards_floors_at_10(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forced: str,
    expected: int,
) -> None:
    """FORCE_SHARDS stays as a measurement lever but never goes below 10.
    A forced N with no table in shard_weights.json (e.g. 20) partitions by
    plain crc32 on the file path (conftest.shard_index); tabled counts
    (GATE_SHARD_COUNTS) run pinned."""
    monkeypatch.setenv("FORCE_SHARDS", forced)
    capsys.readouterr()
    assert _PLAN.main() == 0
    out = capsys.readouterr().out
    n_line = next(line for line in out.splitlines() if line.startswith("n="))
    assert int(n_line.split("=", 1)[1]) == expected
    list_line = next(line for line in out.splitlines() if line.startswith("list="))
    assert len(json.loads(list_line.split("=", 1)[1])) == expected
    files_line = next(
        line for line in out.splitlines() if line.startswith("shard_files=")
    )
    files = json.loads(files_line.split("=", 1)[1])
    assert sorted(files, key=int) == [str(k) for k in range(1, expected + 1)]


def test_test_files_falls_back_to_walk_when_git_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the ship container the worktree's `.git` points outside the mount,
    so `git ls-files` exits 128; test_files() must walk tests/ instead."""
    real_run = _PLAN.subprocess.run
    try:
        real = real_run(
            ["git", "ls-files", "-z", "--", "tests"],
            cwd=REPO,
            check=True,
            capture_output=True,
            encoding="utf-8",
        ).stdout
        tracked: list[str] | None = sorted(
            p for p in real.split("\0") if _PLAN.TEST_FILE.search(p)
        )
    except (subprocess.CalledProcessError, OSError):
        tracked = None  # git unavailable here too; skip the equality check

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(_PLAN.subprocess, "run", boom)
    walked = _PLAN.test_files()
    assert walked
    assert "tests/test_ci_shards.py" in walked
    assert all(_PLAN.TEST_FILE.search(p) for p in walked)
    if tracked is not None:
        # Superset, not equality: the walk also sees untracked test files.
        assert set(tracked) <= set(walked)


def test_workflow_follows_the_plan_output() -> None:
    assert "shard: ${{ fromJSON(needs.plan.outputs.shard_list) }}" in WORKFLOW
    assert "--shard ${{ matrix.shard }}/${{ needs.plan.outputs.shards }}" in WORKFLOW
    # Each shard collects only its own files: plan emits the partition map,
    # test-linux reads its bucket out of it (jq, never word-splitting).
    assert "shard_files: ${{ steps.shards.outputs.shard_files }}" in WORKFLOW
    assert "SHARD_FILES: ${{ needs.plan.outputs.shard_files }}" in WORKFLOW
    assert "jq -r --arg k" in WORKFLOW


def test_gate_fast_leg_wiring() -> None:
    """me580555: the blocking ~5-min leg is `gate-fast` — plan emits its
    own LPT shard map (a --shard flag would deselect it), the matrix fans
    out over it, and test-linux becomes the non-blocking soak behind it."""
    # plan's gatefast step -> job outputs -> matrix + env.
    assert "gate_fast_files: ${{ steps.gatefast.outputs.gate_fast_files }}" in WORKFLOW
    assert (
        "gate_fast_shard_list: ${{ steps.gatefast.outputs.gate_fast_shard_list }}"
        in WORKFLOW
    )
    assert "gate_fast_full: ${{ steps.gatefast.outputs.gate_fast_full }}" in WORKFLOW
    assert "shard: ${{ fromJSON(needs.plan.outputs.gate_fast_shard_list) }}" in WORKFLOW
    assert "GATE_FAST_FILES: ${{ needs.plan.outputs.gate_fast_files }}" in WORKFLOW
    # FULL runs the non-nightly suite with test-linux's 40-minute budget;
    # ordinary selections retain their 25-minute cap. Pin the condition
    # and both branches on the parsed job, not an unrelated timeout line.
    fast_job = yaml.safe_load(WORKFLOW)["jobs"]["gate-fast"]
    assert fast_job["timeout-minutes"] == (
        "${{ needs.plan.outputs.gate_fast_full == 'true' && 40 || 25 }}"
    )
    # gate-fast collects its pack with `-m "not nightly"` and no --shard
    # flag — the pack IS the partition, a crc32 net would deselect it.
    fast = WORKFLOW.split("gate-fast:", 1)[1].split("test-linux:", 1)[0]
    pytest_line = next(ln for ln in fast.splitlines() if ln.strip().startswith("P=("))
    assert '-m "not nightly"' in pytest_line
    assert "--shard" not in pytest_line
    # argv is the pack's list verbatim — it is emitted path-sorted, and
    # pytest file args under a package dir must never interleave with the
    # dir's own files (test_pytest_file_args_must_keep_package_subtrees_
    # contiguous pins the upstream behavior this guards).
    assert '"${P[@]}" "${files[@]}"' in fast
    assert "needs: [plan, failed-first]" in fast
    assert "needs.plan.outputs.full != 'true'" in fast
    # The soak: test-linux keeps its id, renames to `soak` on the gate
    # shape only, needs gate-fast green, and never blocks the run
    # conclusion — its red is debt (release.soak_red), not a veto.
    soak = WORKFLOW.split("test-linux:", 1)[1]
    assert "needs: [plan, failed-first, gate-fast]" in soak
    assert "&& 'test-linux' || 'soak' }}" in soak
    assert "needs.gate-fast.result == 'success'" in soak
    assert "needs.plan.outputs.gate_fast_full != 'true'" in soak
    assert "continue-on-error: ${{ needs.plan.outputs.full != 'true' }}" in soak
    # On the full shape there is no gate-fast — the whole matrix is the
    # verdict and soaks stay blocking.
    assert "max-parallel: ${{ needs.plan.outputs.full == 'true' && 20 || 5 }}" in soak
    # The gatefast step tolerates only exit 3 (FULL) — every other
    # non-zero is a real failure.
    gatefast = WORKFLOW.split("- id: gatefast", 1)[1].split("lint:", 1)[0]
    assert 'if [[ $rc -ne 0 && $rc -ne 3 ]]; then exit "$rc"; fi' in gatefast
    # plan's timeout is 10, not 5: the impacted-tests selector may take
    # up to 300 s, and a slow selector must fall back to FULL — not kill
    # `plan` and the whole gate with it.
    plan = WORKFLOW.split("\n  plan:", 1)[1].split("\n  lint:", 1)[0]
    assert "timeout-minutes: 10" in plan
    # verdict-soak is the soak's legible check-run: it runs under the
    # matrix's own run condition specialized to the soak shape — the
    # `full != 'true'` guard picks the soak display name and reduces the
    # matrix's last disjunction to its right-hand branch — so it is
    # SKIPPED on docs-only, lint-only, harness-FULL (gate_fast_full)
    # and release-FULL shapes where no soak legs exist, instead of
    # concluding `success` where a stale debt row could read green. Its
    # leading `!cancelled()` is the status check that keeps it
    # evaluating past red/skipped needs (a cancelled RUN still skips it
    # — wanted). It reads the legs' REAL conclusions through the jobs
    # API — needs.*.result is masked by the soak's own
    # continue-on-error — and fails loudly when the matrix ran but no
    # `soak (` rows came back. It carries no continue-on-error itself,
    # which would re-mask the red it exists to show.
    verdict = WORKFLOW.split("\n  verdict-soak:", 1)[1].split("\n  publish:", 1)[0]
    assert "needs: [plan, failed-first, gate-fast, test-linux]" in verdict
    assert "actions: read" in verdict
    assert "GH_TOKEN: ${{ github.token }}" in verdict
    assert "--paginate" in verdict and "--method GET" in verdict
    assert "$1 ~ /^soak \\(/" in verdict
    assert "no soak legs" in verdict
    assert '$2 == "failure" || $2 == "cancelled"' in verdict
    assert 'echo "verdict-soak: red legs:"; echo "$bad"; exit 1' in verdict
    assert "continue-on-error" not in verdict
    # Mechanical, not substring (review round 4, item 2): the verdict's
    # `if:` must equal the matrix's top-level clauses with ONE
    # specialization — under `full != 'true'` the last disjunction's
    # left branch is dead, so the right-hand branch's two clauses plus
    # the `full != 'true'` guard replace it, in place.
    sv = load_script("soak_verdict", LIB / "soak_verdict.py")
    jobs = yaml.safe_load(WORKFLOW)["jobs"]
    matrix_if = _top_level_and(jobs["test-linux"]["if"])
    verdict_if = _top_level_and(jobs[sv.VERDICT_JOB]["if"])
    disjunction = (
        "(needs.plan.outputs.full == 'true'"
        " || (needs.gate-fast.result == 'success'"
        " && needs.plan.outputs.gate_fast_full != 'true'))"
    )
    assert disjunction in matrix_if, "the specialization's target moved"
    expected = [
        c
        for clause in matrix_if
        for c in (
            [
                "needs.plan.outputs.full != 'true'",
                "needs.gate-fast.result == 'success'",
                "needs.plan.outputs.gate_fast_full != 'true'",
            ]
            if clause == disjunction
            else [clause]
        )
    ]
    assert verdict_if == expected


def _top_level_and(expr: str) -> list[str]:
    """A GitHub ``if:`` expression as its top-level ``&&`` clauses,
    whitespace-normalised: operators inside parentheses stay with their
    clause. A top-level ``||`` outgrows this split — fail on it."""
    clauses: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(expr):
        two = expr[i : i + 2]
        if depth == 0 and two in ("&&", "||"):
            assert two == "&&", f"top-level || in {expr!r}"
            clauses.append("".join(buf))
            buf = []
            i += 2
            continue
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
        buf.append(expr[i])
        i += 1
    clauses.append("".join(buf))
    return [re.sub(r"\s+", " ", clause).strip() for clause in clauses]


def _job_blocks() -> dict[str, str]:
    """check.yml job id -> its block, parsed off the ``jobs:`` map — ids
    are the 2-space keys; a block ends at the next one."""
    jobs_yaml = WORKFLOW.split("\njobs:\n", 1)[1]
    out: dict[str, str] = {}
    for part in re.split(r"\n(?= {2}[a-z][\w-]*:\n)", jobs_yaml):
        m = re.match(r" {2}([a-z][\w-]*):\n", part)
        if m:
            out[m.group(1)] = part
    return out


def test_pytest_file_args_must_keep_package_subtrees_contiguous(
    tmp_path: Path,
) -> None:
    """Functional pin for the ordering pack() and check.yml promise (the
    leg-5/leg-7 CI failure): pytest 9.1 defers a directory's conftest
    fixture binding to the first Directory node collected for it, and a
    bare file arg under a package dir re-collects that dir with fresh
    children — so a ``tests/test_*.py`` argv entry between two files
    under the same package unbinds the package's conftest ("fixture
    'client' not found"). Path-sorted argv keeps every subtree contiguous
    and is safe. The interleaved run asserts the bug still reproduces —
    if a pytest upgrade makes it pass, the sort contract can go."""
    tests = tmp_path / "tests"
    pkg = tests / "pkg"
    pkg.mkdir(parents=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef client():\n    return 1\n",
        encoding="utf-8",
    )
    for name in ("test_a", "test_b"):
        (pkg / f"{name}.py").write_text(
            f"def {name}(client):\n    assert client == 1\n", encoding="utf-8"
        )
    (tests / "test_r.py").write_text("def test_r():\n    pass\n", encoding="utf-8")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:xdist",
                *args,
            ],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
        )

    sorted_run = run("tests/pkg/test_a.py", "tests/pkg/test_b.py", "tests/test_r.py")
    assert sorted_run.returncode == 0, sorted_run.stdout + sorted_run.stderr
    interleaved = run("tests/pkg/test_a.py", "tests/test_r.py", "tests/pkg/test_b.py")
    # The deferred-binding shape landed in pytest 9.1; the local gate
    # image may still bake 9.0.x, where interleaved file args are
    # harmless (conftest fixtures bound to the plugin, not the first
    # Directory node). On >=9.1 the interleaved run MUST still error —
    # if a pytest upgrade makes it pass, the sort contract can go.
    if tuple(int(v) for v in pytest.__version__.split(".")[:2]) >= (9, 1):
        assert interleaved.returncode != 0, (
            "pytest no longer unbinds the package conftest on interleaved "
            "file args: the gate_fast.pack() path-sort contract and this "
            "test can be removed (gate-s2, pytest 9.1.1 behaviour)"
        )
        assert "fixture 'client' not found" in interleaved.stdout + interleaved.stderr
    else:
        assert interleaved.returncode == 0


def test_verdict_soak_name_and_no_other_soak_job() -> None:
    """Review round 3, item 3: the soak's legible verdict check-run is
    named ``soak_verdict.VERDICT_JOB`` — a rename must go red here, not
    blind the readers (round/release/lgm/ci_watch all share the
    constant). And no other job id or job `name:` may start with
    `soak`: ci_watch counts any `soak*` job as a soak leg, which would
    gripe it separately and count soakless runs as soak runs."""
    sv = load_script("soak_verdict", LIB / "soak_verdict.py")
    jobs = _job_blocks()
    assert sv.VERDICT_JOB in jobs, f"{sv.VERDICT_JOB}: no such job in check.yml"
    # The id IS the check-run name (no `name:` key today); an explicit
    # one must equal VERDICT_JOB.
    name = re.search(r"^ {4}name:\s*(.*)$", jobs[sv.VERDICT_JOB], re.M)
    assert not name or name.group(1).strip() == sv.VERDICT_JOB
    for job_id, block in jobs.items():
        assert not job_id.startswith("soak"), f"job id starts with soak: {job_id}"
        m = re.search(r"^ {4}name:\s*(.*)$", block, re.M)
        if not m or job_id == "test-linux":
            continue
        assert "soak" not in m.group(1), f"{job_id}: a soak-named check-run"
    # The exemption is not vacuous: the matrix's name expression is the
    # one allowed `soak` producer — its legs ARE `soak (PY, K)`.
    matrix_name = re.search(r"^ {4}name:\s*>-", jobs["test-linux"], re.M)
    assert matrix_name, "test-linux lost its soak display name"
    assert "'test-linux' || 'soak'" in jobs["test-linux"]


def _verdict_step_script() -> str:
    """The ``run:`` script of verdict-soak's ``gh api`` step, parsed out
    of the YAML — never regexed."""
    sv = load_script("soak_verdict", LIB / "soak_verdict.py")
    job = yaml.safe_load(WORKFLOW)["jobs"][sv.VERDICT_JOB]
    step = next(s for s in job["steps"] if "gh api" in s.get("run", ""))
    assert isinstance(step["run"], str)
    return step["run"]


def _run_verdict_step(tmp_path: Path, jobs_tsv: str) -> subprocess.CompletedProcess:
    """Execute the real step script under a stub ``gh`` on PATH: the stub
    answers the paged jobs-API call with ``jobs_tsv``; the ``${{ }}``
    contexts are filled the way the runner does (inert values — the stub
    ignores the api arguments anyway)."""
    script = re.sub(r"\$\{\{.*?\}\}", "ctx", _verdict_step_script())
    tsv = tmp_path / "jobs.tsv"
    tsv.write_text(jobs_tsv, encoding="utf-8")
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\ncat "$GH_STUB_TSV"\n', encoding="utf-8")
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    step_file = tmp_path / "step.sh"
    step_file.write_text(script, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "GH_TOKEN": "stub",
        "GH_STUB_TSV": str(tsv),
    }
    return subprocess.run(
        ["bash", "-e", str(step_file)],
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: spawns bash for the verdict step"
)
@pytest.mark.parametrize(
    ("jobs_tsv", "ok", "needle"),
    [
        (
            "soak (3.13, 1)\tsuccess\n"
            "soak (3.13, 2)\tsuccess\n"
            "lint\tsuccess\n"
            "verdict-soak\tsuccess\n",
            True,
            None,
        ),
        ("soak (3.13, 1)\tsuccess\nsoak (3.13, 2)\tfailure\n", False, "soak (3.13, 2)"),
        (
            "soak (3.13, 1)\tsuccess\nsoak (3.13, 2)\tcancelled\n",
            False,
            "soak (3.13, 2)",
        ),
        ("soak (3.13, 1)\tsuccess\nsoak (3.13, 2)\tskipped\n", True, None),
        ("verdict-soak\tsuccess\nlint\tsuccess\n", False, "no soak legs"),
    ],
    ids=["all-green", "one-failure", "one-cancelled", "skipped-leg", "no-soak-rows"],
)
def test_verdict_soak_step_executes_under_a_stub_gh(
    tmp_path: Path, jobs_tsv: str, ok: bool, needle: str | None
) -> None:
    """Review round 4, item 1: EXECUTE the workflow's step, not just
    grep its text — round 3's guard grepped ``$'^soak \\('`` under a
    basic regex, where ``\\(`` opens a group, so grep exited 2 and
    ``! grep`` read EVERY run, green legs included, as "no soak legs"."""
    proc = _run_verdict_step(tmp_path, jobs_tsv)
    if ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0
        assert needle is not None
        assert needle in proc.stdout + proc.stderr


def test_gate_shards_deselect_nightly_full_shape_runs_them() -> None:
    """The `nightly` marker (release-train §3, Reto 2026-10-24: the >240 s
    tests that don't split move to the nightly run) is deselected in every
    gate-shape shard run — but only via this conditional, so the nightly
    full matrix still runs them."""
    assert (
        "needs.plan.outputs.full != 'true' && ' -m \"not nightly\"' || ''" in WORKFLOW
    )


def test_shards_partition_every_test_exactly_once() -> None:
    ids = [f"tests/test_mod{i % 97}.py::test_case_{i}[p{i % 7}]" for i in range(6000)]
    for n in COUNTS:
        buckets = [{t for t in ids if shard_index(t, n) == k} for k in range(n)]
        assert sum(len(b) for b in buckets) == len(ids)
        assert set().union(*buckets) == set(ids)


def test_partition_is_total_and_file_cohesive() -> None:
    """Every id maps to exactly one shard, and every test FILE to exactly
    one: two node ids of one file — different tests, classes, parametrized
    ids with spaces — always agree (me580555: module fixtures build once
    per gate, not once per shard)."""
    ids = [
        f"tests/test_mod{i % 97}.py::TestK{j}::test_case_{i}[p {i % 7}]"
        for i in range(6000)
        for j in range(2)
    ]
    for n in COUNTS:
        by_file: dict[str, int] = {}
        for t in ids:
            idx = shard_index(t, n)
            assert 0 <= idx < n
            path = t.split("::", 1)[0]
            assert by_file.setdefault(path, idx) == idx


def test_ci_shard_files_matches_conftest_partition() -> None:
    """scripts/lib/shard_partition.py is conftest's shard_index duplicated
    for the workflow side (pytest can't import scripts/): pinned and
    unpinned ids agree, and the script's emitted file map puts every test
    file in exactly the bucket conftest would."""
    n = _PLAN.SHARDS
    pins = PARTITION.load_pins().get(n, {})
    samples = [f"{p}::test_x" for p in list(pins)[:25]] + [
        f"tests/test_unpinned_{i}.py::test_y" for i in range(25)
    ]
    assert len(samples) == 50
    for nid in samples:
        assert PARTITION.shard_index(nid, n) == shard_index(nid, n)
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "FORCE_SHARDS": ""},
    ).stdout
    line = next(line for line in out.splitlines() if line.startswith("shard_files="))
    files_map = json.loads(line.split("=", 1)[1])
    for k_s, files in files_map.items():
        assert files, f"shard {k_s} got no files"
        for f in files:
            assert shard_index(f, n) == int(k_s) - 1
    all_files = sorted(f for files in files_map.values() for f in files)
    assert len(all_files) == len(set(all_files))
    assert len(all_files) > 2000  # the suite's ~2450 test files


def test_weights_table_balances_within_tolerance() -> None:
    """The shipped table's predicted shard seconds (from its embedded
    per-file totals) stay within 5% of the mean — the LPT pack's target;
    no network needed."""
    data = json.loads(
        (REPO / "tests" / "shard_weights.json").read_text(encoding="utf-8")
    )
    seconds = data["seconds"]
    for n_s, table in data["tables"].items():
        n = int(n_s)
        load = [0.0] * n
        for path, k in table.items():
            load[k] += seconds[path]
        mean = sum(load) / n
        assert max(load) <= mean * 1.05, (
            f"n={n} predicted max {max(load):.0f}s vs mean {mean:.0f}s — "
            "regenerate tests/shard_weights.json (scripts/shard-weights)"
        )


def test_shard_weights_regenerates_from_junit(tmp_path: Path) -> None:
    """Two junit files are one run; per-file sums drive a predictable
    longest-first pack (10→0, 8→1, 4→1, 2→0 = 12/12 at n=2)."""
    out = tmp_path / "weights.json"
    fixtures = sorted((REPO / "tests" / "fixtures").glob("junit-shard-*.xml"))
    assert len(fixtures) == 2, "add the two tiny junit fixtures"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "shard-weights"),
            "--out",
            str(out),
            "--ns",
            "2",
            *[str(f) for f in fixtures],
        ],
        cwd=REPO,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tables"]["2"] == {
        "tests/test_ci_shards.py": 0,
        "tests/test_posix_only_guards.py": 1,
        "tests/test_text_io_encoding.py": 1,
        "tests/test_type_ignore_ratchet.py": 0,
    }
    assert data["seconds"]["tests/test_ci_shards.py"] == 10.0


def test_job_budget_fits_the_cap() -> None:
    # plan + lint + 10 gate-fast shards = 12 blocking jobs on the gate
    # shape (me580555); the soak reuses the same matrix behind it, capped
    # at max-parallel 5.
    gate = 2 + _PLAN.SHARDS
    assert gate == 12
    soak_parallel = 5
    # plan + lint + coverage-db + test-other (2 Pythons x 2 macOS shards)
    # + test-linux (2 Pythons x 10 shards) = 27.
    nightly = 1 + 1 + 1 + 2 * 2 + 2 * _PLAN.SHARDS
    assert nightly == 27
    # A main push runs the lint_only lane = plan + lint only; grounded by
    # test-linux's `if` skipping lint_only rather than a bare literal.
    assert "needs.plan.outputs.lint_only != 'true'" in WORKFLOW
    # Two concurrent gates with both soaks live: 2 x (12 + 5) = 34.
    assert 2 * (gate + soak_parallel) == 34 <= CAP
    # nightly (27) + one gate's blocking leg (12) = 39.
    assert nightly + gate == 39 <= CAP
    # Three gates' blocking legs at once is the max; a fourth queues.
    assert 3 * gate <= CAP < 4 * gate


def test_pinned_weights_cover_every_shard_count() -> None:
    from tests.conftest import _SHARD_PINS

    assert set(_SHARD_PINS) == set(COUNTS), (
        "regenerate tests/shard_weights.json (scripts/shard-weights)"
    )
    for n, pins in _SHARD_PINS.items():
        assert all(0 <= v < n for v in pins.values())
        for path, v in pins.items():
            assert shard_index(path, n) == v
        paths = list(pins) + [f"tests/test_new_{i}.py" for i in range(500)]
        assert {shard_index(t, n) for t in paths} == set(range(n))
    # a count without a table ignores the pins and still covers every file once
    assert {shard_index(t, 5) for t in paths} == set(range(5))


def test_no_single_file_eats_a_shard() -> None:
    """A test FILE is the partition's atomic unit: one fatter than a shard's
    fair share (mean test-seconds at n=10) can never be packed under the
    mean, so it must be split per module, not pinned whole — the file-level
    form of gr495187's 31-minute single-test pin."""
    data = json.loads(
        (REPO / "tests" / "shard_weights.json").read_text(encoding="utf-8")
    )
    seconds = data.get("seconds")
    assert seconds, "regenerate tests/shard_weights.json (scripts/shard-weights)"
    mean = sum(seconds.values()) / _PLAN.SHARDS
    over = {f: s for f, s in seconds.items() if s > mean}
    assert not over, (
        f"files over one shard's fair share ({mean:.0f}s); split the module "
        f"per case or speed it up: {over}"
    )
