"""The gate-fast selection (``scripts/lib/gate_fast.py`` + the two
entry-point scripts) stays honest (me580555).

The selection is the blocking ~5-minute leg's contract: floor ∪ history ∪
contract ∪ impacted, minus gone and all-nightly files, under the shard cap
and the total budget. Pinned here:

- every tier offers its files and ``explain`` names the tiers that claimed
  each;
- a ``full`` (or unreadable) impacted answer widens to the whole suite —
  under-gating is the wrong failure, gate-fast IS the gate then;
- a file that does not exist is never emitted: history outlives the tree
  (a deleted test's record would red every shard as "file not found");
- the 300 s shard cap drops any non-impacted claim (only ``impacted`` may
  pull a fat file in, flagged via ``over_cap_impacted``);
- over budget, history-only files drop lowest-hit-first — never a file
  floor/contract/impacted also claims;
- all-nightly files never reach ``files`` (``-m "not nightly"`` collects
  nothing from them);
- ``pack`` LPTs the selection into N buckets exactly once each;
- ``tests/gate_fast.json``'s ``nightly_tests`` names functions that
  actually carry the mark (AST check — the file is generated, the marks
  are the source of truth);
- ``file_is_all_nightly`` reads fn/class/module-level marks;
- the refit's FAILED/ERROR regex matches mid-line ids (``gh --log-failed``
  prefixes ``job<TAB>step<TAB>ts``).
"""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests._script import load_script

REPO = Path(__file__).resolve().parents[1]
GF: Any = load_script("gate_fast", REPO / "scripts" / "lib" / "gate_fast.py")


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The dotless entry scripts exec'd as .py copies under a clean root
    (testmon fingerprints .py), laid out exactly as in the repo —
    ``<root>/scripts/<name>.py`` beside ``<root>/scripts/lib/`` — so each
    script's ``ROOT = Path(__file__).resolve().parents[1]`` lands on the
    staged root and its reads hit the committed tables copied under
    ``<root>/tests/``, never a leaked /tmp dir."""
    root = tmp_path_factory.mktemp("gate_fast_scripts")
    scripts = root / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(
        REPO / "scripts" / "lib" / "gate_fast.py",
        scripts / "lib" / "gate_fast.py",
    )
    for name in ("shard_weights.json", "gate_fast.json"):
        shutil.copyfile(REPO / "tests" / name, root / "tests" / name)
    sel_copy = scripts / "gate_fast_select.py"
    shutil.copyfile(REPO / "scripts" / "gate-fast-select", sel_copy)
    refit_copy = scripts / "gate_fast_refit.py"
    shutil.copyfile(REPO / "scripts" / "gate-fast-refit", refit_copy)
    return type(
        "Staged",
        (),
        {
            "root": root,
            "sel": load_script("gate_fast_select", sel_copy),
            "refit": load_script("gate_fast_refit", refit_copy),
        },
    )


_NOTHING = object()


def _sel(
    *,
    seconds: dict[str, float] | None = None,
    gate: dict[str, Any] | None = None,
    contract: set[str] | None = None,
    impacted_result: dict[str, Any] | None = None,
    exists: Any = _NOTHING,
    all_nightly: Any = None,
    disk_files: list[str] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """``select`` with every external input faked — hermetic and fast."""
    return GF.select(
        REPO,
        "base",
        "head",
        seconds={} if seconds is None else seconds,
        gate={} if gate is None else gate,
        contract=set() if contract is None else contract,
        impacted_result={"mode": "selected", "files": {}}
        if impacted_result is None
        else impacted_result,
        exists=(lambda f: True) if exists is _NOTHING else exists,
        all_nightly=all_nightly or (lambda f: False),
        disk_files=[] if disk_files is None else disk_files,
        **kw,
    )


# ── tiers ────────────────────────────────────────────────────────────


def test_every_tier_offers_and_explains() -> None:
    res = _sel(
        seconds={"tests/floor.py": 1.0, "tests/slow.py": 50.0},
        gate={"history_files": ["tests/hist.py"], "history_hits": {}},
        contract={"tests/contract.py"},
        impacted_result={
            "mode": "selected",
            "files": {"tests/imp.py::test_x": {}},
        },
    )
    assert res["files"] == [
        "tests/contract.py",
        "tests/floor.py",
        "tests/hist.py",
        "tests/imp.py",
    ]
    assert res["explain"]["tests/floor.py"] == ["floor"]
    assert res["explain"]["tests/hist.py"] == ["history"]
    assert res["explain"]["tests/contract.py"] == ["contract"]
    assert res["explain"]["tests/imp.py"] == ["impacted"]
    # A measured file no tier claims is not selected: measurement alone
    # is not an offer — floor is capped at floor_seconds.
    assert "tests/slow.py" not in res["explain"]


def test_floor_is_measured_seconds_at_or_under_the_floor() -> None:
    res = _sel(seconds={"tests/a.py": 2.0, "tests/b.py": 2.01}, floor=2.0)
    assert res["files"] == ["tests/a.py"]


def test_a_file_claimed_by_two_tiers_names_both() -> None:
    res = _sel(
        seconds={"tests/f.py": 1.0},
        gate={"history_files": ["tests/f.py"], "history_hits": {}},
    )
    assert res["explain"]["tests/f.py"] == ["floor", "history"]


# ── FULL / unreadable ────────────────────────────────────────────────


def test_full_mode_offers_every_measured_file_and_says_why() -> None:
    res = _sel(
        seconds={"tests/a.py": 1.0, "tests/b.py": 400.0},
        disk_files=["tests/a.py", "tests/b.py"],
        impacted_result={
            "mode": "full",
            "full_because": ["harness diff: scripts/test"],
        },
    )
    assert res["full"] is True
    assert "harness diff" in res["why"]
    assert set(res["files"]) == {"tests/a.py", "tests/b.py"}
    # The 400 s file is over the shard cap — only impacted could keep it.
    assert res["over_cap_impacted"] == ["tests/b.py"]


def test_full_selects_disk_files_the_seconds_table_never_measured() -> None:
    """FULL is the disk, not the weights table: a brand-new ``test_*.py``
    has no seconds row yet, and a harness release must still run it —
    measured files alone would silently under-gate it."""
    res = _sel(
        seconds={"tests/measured.py": 300.0},
        disk_files=["tests/measured.py", "tests/brand_new.py"],
        impacted_result={"mode": "full", "full_because": ["harness diff"]},
    )
    assert res["full"] is True
    assert set(res["files"]) == {"tests/measured.py", "tests/brand_new.py"}
    assert res["explain"]["tests/brand_new.py"] == ["impacted"]


def test_an_unmeasured_disk_file_gets_the_median_weight() -> None:
    """No seconds row → the median measured file's weight, so packing and
    floor/cap accounting still work instead of pricing it at zero."""
    res = _sel(
        seconds={"tests/a.py": 1.0, "tests/b.py": 3.0, "tests/c.py": 50.0},
        disk_files=["tests/new.py"],
    )
    assert res["seconds"]["tests/new.py"] == pytest.approx(3.0)
    # The median lands under a generous floor: an unmeasured file is not
    # priced out of the cheap tier either.
    res = _sel(
        seconds={"tests/a.py": 1.0, "tests/b.py": 3.0, "tests/c.py": 50.0},
        disk_files=["tests/new.py"],
        floor=5.0,
    )
    assert res["explain"]["tests/new.py"] == ["floor"]


def test_an_impacted_error_widens_to_full() -> None:
    """The select script's contract: "we cannot select" is FULL, never a
    degraded gate — under-gating is the wrong failure."""
    res = _sel(
        seconds={"tests/a.py": 1.0},
        impacted_result={
            "mode": "error",
            "files": {},
            "full_because": [],
            "notes": ["impacted-tests exit 2: bad ref"],
        },
    )
    assert res["full"] is True
    assert set(res["files"]) == {"tests/a.py"}
    assert res["notes"] == ["impacted-tests exit 2: bad ref"]


# ── files that are gone ──────────────────────────────────────────────


def test_a_deleted_test_file_is_never_emitted() -> None:
    """gate_fast.json is regenerated weekly; a test deleted since the last
    refit must not reach pytest as "file or directory not found" — that
    would red every gate-fast shard on a stale record."""
    res = _sel(
        seconds={"tests/gone.py": 1.0, "tests/live.py": 1.0},
        gate={"history_files": ["tests/gone.py", "tests/live.py"], "history_hits": {}},
        exists=lambda f: f != "tests/gone.py",
    )
    assert res["files"] == ["tests/live.py"]
    assert "tests/gone.py" not in res["explain"]


def test_a_deleted_file_consumes_no_budget() -> None:
    res = _sel(
        seconds={"tests/gone.py": 4000.0, "tests/h.py": 50.0},
        gate={
            "history_files": ["tests/gone.py", "tests/h.py"],
            "history_hits": {"tests/h.py": 1},
        },
        exists=lambda f: f != "tests/gone.py",
        budget=100.0,
    )
    # Without the existence drop, gone.py's stale 4000 s would evict h.py.
    assert res["files"] == ["tests/h.py"]
    assert res["dropped"] == []


# ── shard cap ────────────────────────────────────────────────────────


def test_cap_drops_an_over_cap_history_file() -> None:
    res = _sel(
        seconds={"tests/fat.py": 350.0, "tests/tiny.py": 1.0},
        gate={"history_files": ["tests/fat.py"], "history_hits": {}},
        cap=300.0,
    )
    assert res["files"] == ["tests/tiny.py"]
    assert "tests/fat.py" not in res["explain"]


def test_cap_keeps_the_fat_file_only_when_impacted_claims_it() -> None:
    res = _sel(
        seconds={"tests/fat.py": 350.0},
        gate={"history_files": ["tests/fat.py"], "history_hits": {}},
        impacted_result={
            "mode": "selected",
            "files": {"tests/fat.py::test_x": {}},
        },
        cap=300.0,
    )
    assert res["files"] == ["tests/fat.py"]
    assert res["over_cap_impacted"] == ["tests/fat.py"]


def test_nightly_seconds_reduce_a_files_cost_below_the_cap() -> None:
    res = _sel(
        seconds={"tests/f.py": 310.0},
        gate={
            "history_files": ["tests/f.py"],
            "history_hits": {},
            "nightly_seconds": {"tests/f.py": 20.0},
        },
        cap=300.0,
    )
    assert res["files"] == ["tests/f.py"]


# ── budget ───────────────────────────────────────────────────────────


def test_budget_drops_history_lowest_hit_first() -> None:
    res = _sel(
        seconds={
            "tests/h1.py": 50.0,
            "tests/h2.py": 50.0,
            "tests/h3.py": 50.0,
            "tests/floor.py": 1.0,
        },
        gate={
            "history_files": ["tests/h1.py", "tests/h2.py", "tests/h3.py"],
            "history_hits": {"tests/h1.py": 1, "tests/h2.py": 5, "tests/h3.py": 3},
        },
        budget=110.0,
    )
    assert res["dropped"] == ["tests/h1.py"]  # hits 1 first, then h3 (3)
    assert set(res["files"]) == {
        "tests/floor.py",
        "tests/h2.py",
        "tests/h3.py",
    }
    assert res["seconds_pred"] == pytest.approx(101.0)


def test_budget_drops_in_hit_order_until_it_fits() -> None:
    res = _sel(
        seconds={"tests/h1.py": 50.0, "tests/h2.py": 50.0},
        gate={
            "history_files": ["tests/h1.py", "tests/h2.py"],
            "history_hits": {"tests/h1.py": 1, "tests/h2.py": 2},
        },
        budget=10.0,
    )
    assert res["dropped"] == ["tests/h1.py", "tests/h2.py"]
    assert res["files"] == []


def test_budget_never_drops_a_file_another_tier_claims() -> None:
    res = _sel(
        seconds={"tests/shared.py": 100.0, "tests/h.py": 100.0},
        gate={
            "history_files": ["tests/shared.py", "tests/h.py"],
            "history_hits": {"tests/shared.py": 1, "tests/h.py": 2},
        },
        contract={"tests/shared.py"},
        budget=110.0,
    )
    # shared.py has the lowest hits but contract claims it; only h.py may go.
    assert res["dropped"] == ["tests/h.py"]
    assert res["files"] == ["tests/shared.py"]
    assert "contract" in res["explain"]["tests/shared.py"]


def test_an_over_budget_contract_or_floor_file_is_never_dropped() -> None:
    res = _sel(
        seconds={"tests/floor.py": 1.0, "tests/c.py": 5000.0},
        contract={"tests/c.py"},
        cap=100000.0,
        budget=100.0,
    )
    assert res["dropped"] == []
    assert set(res["files"]) == {"tests/floor.py", "tests/c.py"}


# ── all-nightly files ────────────────────────────────────────────────


def test_all_nightly_files_never_reach_the_list() -> None:
    res = _sel(
        seconds={"tests/night.py": 1.0, "tests/day.py": 1.0},
        all_nightly=lambda f: f == "tests/night.py",
    )
    assert res["files"] == ["tests/day.py"]
    # It stays in the explain map — the caller sees why it was offered.
    assert res["explain"]["tests/night.py"] == ["floor"]
    assert res["seconds_pred"] == pytest.approx(1.0)


# ── determinism ──────────────────────────────────────────────────────


def test_selection_is_deterministic_across_input_orders() -> None:
    a = _sel(
        seconds={"tests/b.py": 1.0, "tests/a.py": 1.0, "tests/c.py": 1.0},
        gate={"history_files": ["tests/h2.py", "tests/h1.py"], "history_hits": {}},
        contract={"tests/z.py", "tests/y.py"},
    )
    b = _sel(
        seconds={"tests/c.py": 1.0, "tests/a.py": 1.0, "tests/b.py": 1.0},
        gate={"history_files": ["tests/h1.py", "tests/h2.py"], "history_hits": {}},
        contract={"tests/y.py", "tests/z.py"},
    )
    assert a == b
    assert a["files"] == sorted(a["files"])


# ── pack ─────────────────────────────────────────────────────────────


def test_pack_partitions_every_file_exactly_once() -> None:
    files = [f"tests/f{i}.py" for i in range(7)]
    seconds = {f: float(10 - i) for i, f in enumerate(files)}
    shards, load = GF.pack(files, seconds, {}, n=3)
    assert sorted(shards) == ["1", "2", "3"]
    all_files = [f for bucket in shards.values() for f in bucket]
    assert sorted(all_files) == sorted(files)
    # LPT on 10,9,…,4 over 3 buckets lands within one file of even.
    assert max(load) - min(load) <= max(seconds.values())


def test_pack_balances_on_non_nightly_seconds() -> None:
    shards, load = GF.pack(
        ["tests/a.py", "tests/b.py"],
        {"tests/a.py": 100.0, "tests/b.py": 50.0},
        {"tests/a.py": 90.0},  # a.py's non-nightly cost is 10, not 100
        n=2,
    )
    assert sorted(load) == [10.0, 50.0]


def test_pack_emits_each_shard_path_sorted() -> None:
    """check.yml hands each bucket to pytest as bare file args; pytest
    9.1's deferred conftest binding attaches a directory's conftest
    fixtures to the first Directory node collected for it, and every bare
    file arg under a package dir re-collects that dir with fresh children
    — so a ``tests/test_*.py`` argv entry between two
    ``tests/<pkg>/*.py`` entries unbinds the pkg conftest for the second
    ("fixture 'x' not found" — the leg-5/leg-7 CI failure). Sorted argv
    keeps every subtree contiguous: the LPT order decides the bucket, the
    emission order must not."""
    files = [
        "tests/pkg/test_p1.py",
        "tests/test_a.py",
        "tests/pkg/test_p2.py",
        "tests/other/test_o.py",
        "tests/test_b.py",
        "tests/pkg/deep/test_d.py",
    ]
    # Distinct seconds: with uniform costs the LPT order is already
    # path-sorted and the pin passes on an unsorted pack() — vacuous.
    seconds = {
        "tests/test_a.py": 9.0,
        "tests/pkg/test_p2.py": 8.0,
        "tests/other/test_o.py": 7.0,
        "tests/test_b.py": 3.0,
        "tests/pkg/deep/test_d.py": 2.0,
        "tests/pkg/test_p1.py": 1.0,
    }
    nightly_s: dict[str, float] = {}
    shards, _load = GF.pack(files, seconds, nightly_s, n=2)
    for bucket in shards.values():
        assert bucket == sorted(bucket)
    # Self-proving: replay pack()'s LPT loop without the emission sort —
    # at least one bucket's assignment order must not be path-sorted, so
    # the fixture can never degrade back into a vacuous one.
    load = [0.0, 0.0]
    unsorted: dict[str, list[str]] = {"1": [], "2": []}
    for f in sorted(files, key=lambda f: (-seconds[f], f)):
        k = min(range(2), key=lambda i: (load[i], i))
        load[k] += seconds[f]
        unsorted[str(k + 1)].append(f)
    assert any(b != sorted(b) for b in unsorted.values())


# ── file_is_all_nightly (AST) ────────────────────────────────────────


def _write(tmp_path: Path, body: str, name: str = "test_x.py") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_file_is_all_nightly_function_marks(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.nightly(reason='slow')\n"
        "def test_a(): pass\n"
        "@pytest.mark.nightly\n"
        "async def test_b(): pass\n",
    )
    assert GF.file_is_all_nightly(p)


def test_file_is_all_nightly_module_and_class_marks(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "import pytest\n"
        "pytestmark = pytest.mark.nightly(reason='slow')\n"
        "def test_a(): pass\n"
        "class TestK:\n"
        "    def test_b(self): pass\n",
    )
    assert GF.file_is_all_nightly(p)
    q = _write(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.nightly(reason='slow')\n"
        "class TestK:\n"
        "    def test_a(self): pass\n"
        "    def test_b(self): pass\n",
        name="test_y.py",
    )
    assert GF.file_is_all_nightly(q)


def test_file_is_all_nightly_rejects_partial_and_empty(tmp_path: Path) -> None:
    partial = _write(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.nightly(reason='slow')\n"
        "def test_a(): pass\n"
        "def test_b(): pass\n",
    )
    assert not GF.file_is_all_nightly(partial)
    empty = _write(tmp_path, "def helper(): pass\n", name="test_z.py")
    assert not GF.file_is_all_nightly(empty)


# ── committed inputs ─────────────────────────────────────────────────


def _marked_functions(path: Path) -> set[str]:
    """``test_*`` names carrying the nightly mark — fn, class or module
    ``pytestmark`` level (same walk as scripts/gate-fast-refit)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    def visit(node: ast.AST, inherited: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_") and (
                    inherited or GF._fn_nightly(child)
                ):
                    found.add(child.name)
            elif isinstance(child, ast.ClassDef):
                visit(child, inherited or GF._fn_nightly(child))

    visit(tree, GF._module_pytestmark_nightly(tree))
    return found


def test_gate_json_nightly_tests_carry_the_mark() -> None:
    """The generated file claims marks; the tree is truth. A renamed or
    unmarked function turns this red instead of silently skewing the
    selection's non-nightly cost model."""
    gate = GF.load_gate_file(REPO)
    nightly_tests = gate.get("nightly_tests") or {}
    assert nightly_tests, "tests/gate_fast.json lost its nightly_tests map"
    for f, fns in nightly_tests.items():
        marked = _marked_functions(REPO / f)
        missing = set(fns) - marked
        assert not missing, f"{f}: nightly_tests names unmarked fns {missing}"
        # nightly_seconds is the marked functions' summed measured time.
        assert sum(fns.values()) == pytest.approx(
            gate["nightly_seconds"][f], abs=0.05 * len(fns) + 0.01
        )


def test_gate_json_names_real_test_files() -> None:
    """Every file the generated data can select exists — a rename lands
    here first (the selector also defends, this catches the stale data)."""
    gate = GF.load_gate_file(REPO)
    missing = [
        f
        for f in list(gate.get("contract_files") or [])
        + list(gate.get("nightly_seconds") or {})
        if not (REPO / f).is_file()
    ]
    assert not missing


def test_corrupt_or_empty_weights_refuses_loudly(tmp_path: Path) -> None:
    """An unreadable or empty seconds table must fail loudly — a silently
    empty selection would under-gate, which is the wrong failure."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    weights = root / "tests" / "shard_weights.json"
    weights.write_text("{corrupt", encoding="utf-8")
    res = GF.select(root, "base", "head")
    assert "error" in res and "refusing to select" in res["error"]
    weights.write_text('{"seconds": {}}', encoding="utf-8")
    res = GF.select(root, "base", "head")
    assert "error" in res and "refusing to select" in res["error"]


def test_select_script_exits_two_on_corrupt_weights(
    staged: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through the staged script: the committed-table refusal
    reaches stderr and exit 2, not a silent empty file list."""
    weights = staged.root / "tests" / "shard_weights.json"
    good = weights.read_bytes()
    try:
        weights.write_text("{corrupt", encoding="utf-8")
        assert staged.sel.main(["base", "head"]) == 2
        err = capsys.readouterr().err
        assert "unreadable or empty" in err and "refusing to select" in err
    finally:
        weights.write_bytes(good)


def test_full_covers_every_test_file_on_disk() -> None:
    """The committed inputs: FULL selects every ``test_*.py`` present
    minus all-nightly files — the review found 16 files the weights
    table never measured (a file nobody measured yet must still run on
    a harness release)."""
    res = GF.select(
        REPO,
        "base",
        "head",
        impacted_result={"mode": "full", "full_because": ["harness diff"]},
    )
    disk = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "tests").rglob("test_*.py")
        if p.is_file()
    )
    nightly = {
        f
        for f in disk
        if "nightly" in (REPO / f).read_text(encoding="utf-8")
        and GF.file_is_all_nightly(REPO / f)
    }
    assert set(res["files"]) == set(disk) - nightly


def test_every_marked_function_is_listed_in_nightly_tests() -> None:
    """The inverse of the carry-the-mark check: a nightly mark the table
    doesn't know about would bill the file's full ``seconds`` to the
    gate — catch a mark added without a refit."""
    gate = GF.load_gate_file(REPO)
    nightly_tests = gate.get("nightly_tests") or {}
    missing: dict[str, set[str]] = {}
    for p in sorted(REPO.glob("tests/**/test_*.py")):
        if "nightly" not in p.read_text(encoding="utf-8"):
            continue
        f = p.relative_to(REPO).as_posix()
        unlisted = _marked_functions(p) - set(nightly_tests.get(f) or {})
        if unlisted:
            missing[f] = unlisted
    assert not missing, (
        "nightly marks missing from tests/gate_fast.json nightly_tests — "
        "regenerate it (scripts/gate-fast-refit --junit <dir> --write)"
    )


#: Over-cap files allowed to stay unmarked, each with a one-line reason
#: (review round 3, item 1: marks follow the measured cap, not the
#: original audit list). The four guards check harness rules, not
#: product code — unmarked by ruling. The rest are whole-file over-cap
#: by SPREAD: no single test over the cap in audit run 42354225792, and
#: the committed tables carry no per-test seconds for unmarked files, so
#: a whole-file mark cannot be decided here — the round-3 done line
#: lists them and a fresh refit audit re-derives the set.
_SPREAD = "spread — no test over the cap in audit run 42354225792"
NIGHTLY_MARK_ALLOWLIST = {
    "tests/precis_web/test_secret_registry_complete.py": (
        "gate guard: audits that every secret the code reads has a spec — "
        "unmarked by ruling (review round 2, item 5.2)"
    ),
    "tests/test_outbound_polite_ratchet.py": (
        "gate guard: the outbound politeness ratchet — unmarked by "
        "ruling (review round 2, item 5.2)"
    ),
    "tests/test_skill_listing_retired.py": (
        "gate guard: the skill index/table audit — unmarked by ruling "
        "(review round 2, item 5.2)"
    ),
    "tests/test_text_io_encoding.py": (
        "gate guard: the utf-8 naming audit — unmarked by ruling "
        "(review round 2, item 5.2)"
    ),
    "tests/precis_web/test_account.py": _SPREAD,
    "tests/precis_web/test_blocktree_view.py": _SPREAD,
    "tests/precis_web/test_claim_routes.py": _SPREAD,
    "tests/precis_web/test_nanopub_routes.py": _SPREAD,
    "tests/precis_web/test_pcb.py": _SPREAD,
    "tests/precis_web/test_pourbaix.py": _SPREAD,
    "tests/precis_web/test_routes.py": _SPREAD,
    "tests/test_authored_foot.py": _SPREAD,
    "tests/test_cad_handler.py": _SPREAD,
    "tests/test_cad_rounding.py": _SPREAD,
    "tests/test_deploy_tree_no_secrets.py": _SPREAD,
    "tests/test_draft_handler.py": _SPREAD,
    "tests/test_eye_everywhere.py": _SPREAD,
    "tests/test_finding.py": _SPREAD,
    "tests/test_fleet_verbs.py": _SPREAD,
    "tests/test_fleet_watch.py": _SPREAD,
    "tests/test_fleet_wave.py": _SPREAD,
    "tests/test_hexfold/test_catalogue.py": _SPREAD,
    "tests/test_hexfold/test_geom_clash.py": _SPREAD,
    "tests/test_hexfold/test_seam.py": _SPREAD,
    "tests/test_initial_migration.py": _SPREAD,
    "tests/test_measure_kind.py": _SPREAD,
    "tests/test_measures.py": _SPREAD,
    "tests/test_measures_si.py": _SPREAD,
    "tests/test_memory.py": _SPREAD,
    "tests/test_memory_index_retired.py": _SPREAD,
    "tests/test_memory_retiredoor.py": _SPREAD,
    "tests/test_migrate.py": _SPREAD,
    "tests/test_nanopub_gates_mint.py": _SPREAD,
    "tests/test_outbound_timeout_ratchet.py": _SPREAD,
    "tests/test_paper.py": _SPREAD,
    "tests/test_paper_body_ops.py": _SPREAD,
    "tests/test_pcb_ewod_generator.py": _SPREAD,
    "tests/test_pcb_generator_invariants.py": _SPREAD,
    "tests/test_pcb_generator_reput.py": _SPREAD,
    "tests/test_pcb_handler.py": _SPREAD,
    "tests/test_quest_compute.py": _SPREAD,
    "tests/test_quest_loop.py": _SPREAD,
    "tests/test_quest_tick.py": _SPREAD,
    "tests/test_release_ops.py": _SPREAD,
    "tests/test_release_poll.py": _SPREAD,
    "tests/test_release_script.py": _SPREAD,
    "tests/test_remote_ssh_credentials.py": _SPREAD,
    "tests/test_round_gated_descendant.py": _SPREAD,
    "tests/test_round_release.py": _SPREAD,
    "tests/test_se_flow.py": _SPREAD,
    "tests/test_se_prism120.py": _SPREAD,
    "tests/test_se_simp_bridge.py": _SPREAD,
    "tests/test_se_validate_bounded.py": _SPREAD,
    "tests/test_ship_narrow_lock.py": _SPREAD,
    "tests/test_ship_release.py": _SPREAD,
    "tests/test_skill_injection.py": _SPREAD,
    "tests/test_skill_prose.py": _SPREAD,
    "tests/test_ssh_node_executor.py": _SPREAD,
    "tests/test_taxon.py": _SPREAD,
    "tests/test_vocab_lint.py": _SPREAD,
    "tests/test_web.py": _SPREAD,
    "tests/workers/test_health_digest.py": _SPREAD,
    "tests/workers/test_materialize.py": _SPREAD,
    "tests/workers/test_pcb_place.py": _SPREAD,
    "tests/workspaces/test_pcb_board.py": _SPREAD,
    "tests/workspaces/test_pcb_local.py": _SPREAD,
    "tests/workspaces/test_rays_macro.py": _SPREAD,
    "tests/workspaces/test_scratch_import.py": _SPREAD,
    "tests/workspaces/test_trace_reanchor.py": _SPREAD,
}


def test_nightly_marks_follow_the_measured_cap() -> None:
    """The marks audit pin (review rounds 2–3): the cap is
    ``NIGHTLY_CAP_SECONDS`` on a file's measured ``seconds``.

    - every file tabled in ``nightly_tests`` is over the cap — a mark
      that costs nothing is drift;
    - every over-cap file is marked (file or function) or carries a
      listed reason in ``NIGHTLY_MARK_ALLOWLIST`` — the rule's other
      half: a slow file may not sit unmarked without a reason, whether
      or not the original audit listed it;
    - a stale allowlist entry — now marked, under the cap, or
      reasonless — is red. Dominance is not decidable from the
      committed tables for unmarked files (no per-test seconds are
      committed), so the allowlist is the audit's record; a refit
      re-derives it from fresh junit.
    """
    seconds = GF.load_seconds(REPO)
    gate = GF.load_gate_file(REPO)
    nightly_tests = gate.get("nightly_tests") or {}
    under_cap = {
        f for f in nightly_tests if (seconds.get(f) or 0.0) <= GF.NIGHTLY_CAP_SECONDS
    }
    assert not under_cap, f"marked files under the nightly cap: {under_cap}"
    assert not (set(nightly_tests) & set(NIGHTLY_MARK_ALLOWLIST))
    unmarked_over_cap = {
        f
        for f, s in seconds.items()
        if s > GF.NIGHTLY_CAP_SECONDS
        and f not in nightly_tests
        and (REPO / f).is_file()
        and not _marked_functions(REPO / f)
    }
    missing_reason = unmarked_over_cap - set(NIGHTLY_MARK_ALLOWLIST)
    assert not missing_reason, (
        "over-cap files with no mark and no allowlist reason — mark the "
        "dominant test(s) or list the file with a reason: "
        f"{sorted(missing_reason)}"
    )
    for f, reason in NIGHTLY_MARK_ALLOWLIST.items():
        assert reason.strip(), f"{f}: exempted without a reason"
        assert (seconds.get(f) or 0.0) > GF.NIGHTLY_CAP_SECONDS, (
            f"{f}: exempt entry under the cap — drop it"
        )
        marked = _marked_functions(REPO / f)
        assert not marked, f"{f}: fns {marked} marked — drop the exempt entry"


def test_real_selection_fits_budget_and_selects_live_files() -> None:
    """The committed inputs (shard_weights.json + gate_fast.json) select a
    leg that fits the fast budget — the property check.yml relies on. The
    budget half holds by construction (history files drop until it fits);
    the live content is the non-empty, on-disk, non-all-nightly check."""
    res = GF.select(
        REPO,
        "base",
        "head",
        impacted_result={"mode": "selected", "files": {}},
    )
    assert res["files"], "the real selection came out empty"
    assert res["seconds_pred"] <= GF.FAST_BUDGET_SECONDS
    for f in res["files"]:
        assert (REPO / f).is_file(), f"selected file does not exist: {f}"
        assert not GF.file_is_all_nightly(REPO / f), f"all-nightly file: {f}"


# ── the select script ────────────────────────────────────────────────


def _fake_select(result: dict[str, Any]) -> Any:
    return lambda root, base, head, **kw: result


def _res(files: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "files": files,
        "full": False,
        "why": "",
        "dropped": [],
        "over_cap_impacted": [],
        "explain": {f: ["floor"] for f in files},
        "seconds_pred": 0.0,
        "seconds": {},
        "nightly_seconds": {},
        "notes": [],
        **kw,
    }


def test_select_explain_prints_file_tab_tiers(
    staged: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    res = _res(
        ["tests/a.py", "tests/b.py"],
        explain={"tests/a.py": ["floor"], "tests/b.py": ["contract", "history"]},
    )
    monkeypatch.setitem(staged.sel._GF, "select", _fake_select(res))
    assert staged.sel.main(["b", "h", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "tests/a.py\tfloor" in out
    assert "tests/b.py\tcontract,history" in out


def test_select_json_emits_the_plan_map(
    staged: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    res = _res(["tests/a.py"], seconds_pred=12.0)
    monkeypatch.setitem(staged.sel._GF, "select", _fake_select(res))
    assert staged.sel.main(["b", "h", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["full"] is False
    assert payload["files"] == ["tests/a.py"]
    assert sorted(payload["shards"], key=int) == [str(k) for k in range(1, 11)]
    assert "predicted_max_s" in payload and "dropped" in payload


def test_select_full_exits_3_and_says_why(
    staged: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    res = _res(["tests/a.py"], full=True, why="harness diff: uv.lock")
    monkeypatch.setitem(staged.sel._GF, "select", _fake_select(res))
    assert staged.sel.main(["b", "h"]) == 3
    err = capsys.readouterr().err
    assert "gate-fast: FULL (harness diff: uv.lock)" in err


def test_select_dropped_and_over_cap_warn_on_stderr(
    staged: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    res = _res(
        ["tests/a.py"],
        dropped=["tests/old.py"],
        over_cap_impacted=["tests/fat.py"],
    )
    monkeypatch.setitem(staged.sel._GF, "select", _fake_select(res))
    assert staged.sel.main(["b", "h"]) == 0
    err = capsys.readouterr().err
    assert "dropped history files" in err and "tests/old.py" in err
    assert "over the shard cap" in err and "tests/fat.py" in err


# ── the refit script ─────────────────────────────────────────────────


def test_failed_re_matches_mid_line_failed_and_error(staged: Any) -> None:
    """gh --log-failed prefixes ``job<TAB>step<TAB>ts  `` — the ids sit
    mid-line; a postgres ``ERROR: relation`` must not count."""
    text = (
        "job1\tstep2\t2026-11-07T00:00:00Z FAILED tests/a.py::test_x - boom\n"
        "job1\tstep2\t2026-11-07T00:00:01Z ERROR tests/b.py::test_y at setup\n"
        "job1\tstep2\t2026-11-07T00:00:02Z ERROR: relation foo does not exist\n"
    )
    assert staged.refit.FAILED_RE.findall(text) == [
        "tests/a.py::test_x",
        "tests/b.py::test_y",
    ]


def test_refit_write_without_junit_refuses_and_writes_nothing(
    staged: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--write`` without ``--junit`` would wipe nightly_tests /
    nightly_seconds (no fresh per-test seconds to rebuild them) — it
    refuses with a one-liner before any gh call or file write."""
    target = staged.root / "tests" / "gate_fast.json"
    before = target.read_bytes()
    assert staged.refit.main(["--write"]) == 2
    assert "--junit" in capsys.readouterr().err
    assert target.read_bytes() == before


def test_marked_functions_reads_fn_class_and_module_marks(
    staged: Any, tmp_path: Path
) -> None:
    p = tmp_path / "test_m.py"
    p.write_text(
        "import pytest\n"
        "@pytest.mark.nightly(reason='s')\n"
        "def test_a(): pass\n"
        "@pytest.mark.nightly\n"
        "class TestK:\n"
        "    def test_b(self): pass\n"
        "    def test_c(self): pass\n"
        "def test_d(): pass\n",
        encoding="utf-8",
    )
    assert staged.refit.marked_functions(p) == {"test_a", "test_b", "test_c"}


def test_leave_one_out_reports_the_catch_rate(staged: Any) -> None:
    """The analysis the budget was sized on: per red incident, would the
    selection built without its own files have caught any of them."""
    per_incident = {
        1: {"tests/a.py", "tests/b.py"},
        2: {"tests/a.py"},
        3: {"tests/never.py"},
    }
    any_rate, per_file, n = staged.refit.leave_one_out(
        per_incident, {"tests/floor.py"}, {"tests/b.py"}, set()
    )
    # Run 1: a.py in run 2's history + b.py in contract → caught.
    # Run 2: a.py in run 1's history → caught. Run 3: never.py → missed.
    assert n == 3
    assert any_rate == pytest.approx(2 / 3)
    assert per_file == pytest.approx(3 / 4)
