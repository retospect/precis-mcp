"""The ``tidy`` hook of a job type (gr741695, round 6): the executor calls it
once per watch pass, at the end, under the host's watch lock — the one
sequential place a job type does host-wide work that belongs to no job in
hand (``run_slice`` saves and drops the attempt folders of ended jobs).

DB-free: the pass is driven with the store, the lock and the job lists
faked."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.executors import ssh_node
from precis.workers.job_types import get_job_type, run_slice


class _Conn:
    def commit(self) -> None:
        pass


class _Pool:
    @contextmanager
    def connection(self) -> Iterator[_Conn]:
        yield _Conn()


class _Store:
    dsn = None
    pool = _Pool()


def _store() -> Any:
    """The fake store; ``Any`` because the pass is typed for the real one."""
    return _Store()


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A pass with nothing to poll and nothing to claim; ``events`` records
    the order of the watch steps and the hooks."""
    state: dict[str, Any] = {"events": [], "held": True, "hooks": {}, "loaded": []}

    @contextmanager
    def watch_lock(store: Any, node: Any) -> Iterator[bool]:
        yield state["held"]

    def reap(store: Any, node: Any, **kw: Any) -> int:
        state["events"].append("reap")
        return 0

    monkeypatch.setattr(ssh_node, "_watch_lock", watch_lock)
    monkeypatch.setattr(ssh_node, "_polling_jobs", lambda store, node: [])
    monkeypatch.setattr(ssh_node, "_cancelling_jobs", lambda store, node: [])
    monkeypatch.setattr(ssh_node, "_reap_detached_orphans", reap)
    monkeypatch.setattr(ssh_node, "claim_executor_jobs", lambda conn, **kw: [])

    def load(name: str) -> SimpleNamespace:
        state["loaded"].append(name)
        return SimpleNamespace(name=name, tidy=state["hooks"][name])

    # ``hooks`` is filled by the test after this fixture; the executor walks
    # the names when the pass runs, so it sees them (dict keys, in order).
    monkeypatch.setattr(ssh_node, "TIDY_JOB_TYPES", state["hooks"])
    monkeypatch.setattr(ssh_node, "get_job_type", load)
    return state


def _hook(state: dict[str, Any], name: str, *, raises: bool = False) -> None:
    def tidy(store: Any, node: Any) -> None:
        state["events"].append((name, node))
        if raises:
            raise RuntimeError("disk gone")

    state["hooks"][name] = tidy


def test_tidy_is_called_once_per_pass_after_the_watch_steps_under_the_lock(
    rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    _hook(rig, "one")
    _hook(rig, "two")
    store = _store()
    assert ssh_node.run_ssh_node_pass(store) == {"claimed": 0, "ok": 0, "failed": 0}
    assert rig["events"] == ["reap", ("one", "node-a"), ("two", "node-a")]
    ssh_node.run_ssh_node_pass(store)
    assert rig["events"].count(("one", "node-a")) == 2  # once per pass


def test_tidy_is_not_called_when_another_worker_holds_the_watch_lock(
    rig: dict[str, Any],
) -> None:
    _hook(rig, "one")
    rig["held"] = False
    ssh_node.run_ssh_node_pass(_store())
    assert rig["events"] == []


def test_a_raising_tidy_hook_does_not_break_the_pass_or_the_next_hook(
    rig: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    _hook(rig, "bad", raises=True)
    _hook(rig, "good")
    result = ssh_node.run_ssh_node_pass(_store())
    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert [e for e in rig["events"] if e != "reap"] == [
        ("bad", None),
        ("good", None),
    ]
    assert any("tidy hook of bad raised" in r.getMessage() for r in caplog.records)


def test_a_job_type_that_declares_no_tidy_is_skipped(rig: dict[str, Any]) -> None:
    rig["hooks"]["none"] = None
    _hook(rig, "one")
    ssh_node.run_ssh_node_pass(_store())
    assert [e for e in rig["events"] if e != "reap"] == [("one", None)]


def test_run_slice_declares_the_sweep_as_its_tidy_hook() -> None:
    spec = get_job_type("run_slice")
    assert spec is not None and spec.tidy is run_slice.tidy


def test_the_pass_loads_only_the_job_types_that_declare_a_hook(
    rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every worker runs this pass every few seconds. If it walked every
    known job type it would import each one's module into every worker
    (``fix_gripe`` pulls the LLM router in) and log each one that cannot
    load on every pass."""
    _hook(rig, "one")
    monkeypatch.setattr(ssh_node, "known_job_types", lambda: ["one", "heavy", "broken"])
    ssh_node.run_ssh_node_pass(_store())
    assert rig["loaded"] == ["one"]


def test_the_named_tidy_types_are_exactly_the_specs_that_set_a_hook() -> None:
    """``TIDY_JOB_TYPES`` is a hand-kept list (so nothing has to be loaded to
    know it). A spec that gains ``tidy=`` without being named there would
    never have its hook called. Read from the source, nothing imported."""
    import ast
    from pathlib import Path

    from precis.workers import job_types

    def sets_tidy(scope: ast.AST) -> bool:
        return any(
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "JobTypeSpec"
            and any(k.arg == "tidy" for k in node.keywords)
            for node in ast.walk(scope)
        )

    package = Path(job_types.__file__).resolve().parent
    declared: set[str] = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.name != "__init__.py":
            # a job type that builds its own spec: named after its module
            if sets_tidy(tree):
                declared.add(path.stem)
            continue
        # the registry's loaders: ``_load_<job type>``
        declared |= {
            func.name.removeprefix("_load_")
            for func in ast.walk(tree)
            if isinstance(func, ast.FunctionDef) and sets_tidy(func)
        }
    assert declared == set(job_types.TIDY_JOB_TYPES)
