"""Outbound waits must carry a bound — the half ruff cannot enforce.

The outbound wait-bound gate (docs/conventions/invariants.md §Outbound,
folded from gr503084 parts B+C): a call that can block forever is a
fleet-wide hang
class. Ruff covers ``requests.*`` without ``timeout=`` (S113) but nothing
else, so this AST ratchet scans ``src/`` and ``scripts/`` for:

* ``subprocess.run``/``check_output``/``check_call``/``call`` and
  ``Popen.wait``/``communicate`` without ``timeout=``;
* ``psycopg.connect`` without ``connect_timeout=``;
* ``Thread.join()``, ``Queue.get()``, ``Lock.acquire()``,
  ``Future.result()``, ``as_completed`` without a timeout;
* blocking ``fcntl.flock`` (``LOCK_EX``/``LOCK_SH`` without ``LOCK_NB``);
* ``await``s on ``asyncio`` primitives (``Queue``/``Event``/``Lock``/…)
  and on ``*ClientSession`` receivers not wrapped in
  ``wait_for``/``timeout``;
* bare ``httpx.<method>``/``urlopen`` without ``timeout=``.

Receivers resolve through local bindings (``proc = Popen(...)``,
``q = asyncio.Queue()``) inside the enclosing function — name tracking,
not types. ``while True`` poll loops and the ``tests/`` tree are scanned
into the baseline's ``report`` section only: visible, never enforced.

Existing offenders are pinned in ``tests/data/outbound_timeout_baseline.json``
as a ONE-WAY floor keyed ``file::enclosing function::callee`` -> count (not
line numbers, so unrelated edits do not churn it). The test fails when

* a key's count rises or a new key appears: put a bound on the call;
* a count falls: lower the baseline (it only ever goes down).

Regenerate only downwards: ``uv run python tests/test_outbound_timeout_ratchet.py
--write`` refuses to raise or add any entry.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "data" / "outbound_timeout_baseline.json"

#: Enforced trees (paths relative to ROOT). ``tests/`` is report-only.
_ENFORCED = ("src", "scripts")
_REPORT_ONLY = ("tests",)
_SKIP = {".claude", ".venv", "node_modules", "build", ".git"}

_SUBP_FUNCS = {"run", "check_output", "check_call", "call"}
_POPEN_WAITS = {"wait", "communicate"}
_HTTP_METHODS = {"get", "post", "put", "delete", "head", "patch", "request", "stream"}
#: asyncio (and anyio, by leaf name) primitives whose awaits block unbounded.
_AIO_CTORS = {
    "Queue",
    "LifoQueue",
    "PriorityQueue",
    "SimpleQueue",
    "Event",
    "Lock",
    "Condition",
    "Semaphore",
    "BoundedSemaphore",
}
_AIO_WAITS = {"get", "wait", "acquire", "put", "join"}
#: Calls/with-items that bound an await: asyncio.wait_for / timeout / timeout_at.
_WRAP_LEAVES = {"wait_for", "timeout", "timeout_at"}


_Fn = ast.FunctionDef | ast.AsyncFunctionDef


def _call_name(call: ast.Call) -> str:
    return ast.unparse(call.func)


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _mod_names(tree: ast.AST, module: str) -> tuple[set[str], set[str]]:
    """``(module aliases, from-imported names)`` bound to ``module``."""
    mods, funcs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.asname or a.name for a in n.names if a.name == module}
        elif (
            isinstance(n, ast.ImportFrom)
            and n.module
            and (n.module == module or n.module.endswith("." + module))
        ):
            funcs |= {a.asname or a.name for a in n.names}
    return mods, funcs


def _ctor_binds(fn: ast.AST) -> dict[str, str]:
    """``var -> callee`` for ``v = Ctor(...)`` / ``with Ctor(...) as v``."""
    out: dict[str, str] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = _call_name(n.value)
        elif isinstance(n, ast.With | ast.AsyncWith):
            for item in n.items:
                if isinstance(item.optional_vars, ast.Name) and isinstance(
                    item.context_expr, ast.Call
                ):
                    out[item.optional_vars.id] = _call_name(item.context_expr)
    return out


class _Scan:
    """One file's offenders and report-only findings."""

    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.bad: Counter[str] = Counter()
        self.poll_lines: list[int] = []
        tree = ast.parse(source)
        self.subs, self.subfuncs = _mod_names(tree, "subprocess")
        self.psyc, self.psycfuncs = _mod_names(tree, "psycopg")
        self.qmods, self.qfuncs = _mod_names(tree, "queue")
        self.thrmods, self.thrfuncs = _mod_names(tree, "threading")
        self.aio, self.aiofuncs = _mod_names(tree, "asyncio")
        self.cf, self.cfuncs = _mod_names(tree, "concurrent.futures")
        self.hx = _mod_names(tree, "httpx")[0]
        self.url_funcs = _mod_names(tree, "urllib.request")[1]
        self.fcntl = _mod_names(tree, "fcntl")[0]
        self._binds: dict[int, dict[str, str]] = {}
        self._visit(tree, [], False)

    def _sync_ctor(self, ctor: str, leaf: str) -> bool:
        """``ctor`` binds a *synchronous* primitive — ``queue.Queue`` /
        ``threading.Lock`` / their from-imported leaves — never
        ``asyncio.*``/``anyio.*`` (those leaf names collide: ``Queue``,
        ``Event``, ``Lock``, ``Condition`` are the same words)."""
        head = ctor.split(".")[0]
        if head in self.aio or ctor in self.aiofuncs:
            return False
        if head in self.qmods or ctor in self.qfuncs:
            return leaf in {"Queue", "LifoQueue", "PriorityQueue", "SimpleQueue"}
        if head in self.thrmods or ctor in self.thrfuncs:
            return leaf in {
                "Thread",
                "Lock",
                "RLock",
                "Event",
                "Condition",
                "Semaphore",
                "BoundedSemaphore",
            }
        return False

    def _binds_for(self, fn: _Fn | None) -> dict[str, str]:
        if fn is None:
            return {}
        key = id(fn)
        if key not in self._binds:
            self._binds[key] = _ctor_binds(fn)
        return self._binds[key]

    def _scope(self, stack: list[_Fn]) -> str:
        return ".".join(f.name for f in stack) or "<module>"

    def _flag(self, stack: list[_Fn], callee: str) -> None:
        self.bad[f"{self.rel}::{self._scope(stack)}::{callee}"] += 1

    def _visit(self, node: ast.AST, stack: list[_Fn], wrapped: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            inner_stack, inner_wrap = stack, wrapped
            if isinstance(ch, ast.FunctionDef | ast.AsyncFunctionDef):
                inner_stack = stack + [ch]
            if (
                isinstance(ch, ast.Call)
                and _call_name(ch).rsplit(".", 1)[-1] in _WRAP_LEAVES
            ):
                inner_wrap = True
            if isinstance(ch, ast.With | ast.AsyncWith):
                if any(
                    isinstance(i.context_expr, ast.Call)
                    and _call_name(i.context_expr).rsplit(".", 1)[-1] in _WRAP_LEAVES
                    for i in ch.items
                ):
                    inner_wrap = True
            if isinstance(ch, ast.Call):
                self._check_call(ch, stack)
            elif isinstance(ch, ast.Await):
                self._check_await(ch, stack, inner_wrap)
            elif isinstance(ch, ast.While):
                self._check_while(ch)
            self._visit(ch, inner_stack, inner_wrap)

    def _check_call(self, n: ast.Call, stack: list[_Fn]) -> None:
        f = n.func
        binds = self._binds_for(stack[-1] if stack else None)
        if isinstance(f, ast.Name):
            if (
                f.id in self.subfuncs
                and f.id in _SUBP_FUNCS
                and not _has_kw(n, "timeout")
            ):
                self._flag(stack, f"subprocess.{f.id}")
            elif (
                f.id == "connect"
                and "connect" in self.psycfuncs
                and not _has_kw(n, "connect_timeout")
            ):
                self._flag(stack, "psycopg.connect")
            elif (
                f.id == "as_completed"
                and "as_completed" in self.cfuncs
                and not _has_kw(n, "timeout")
            ):
                self._flag(stack, "as_completed")
            elif (
                f.id == "urlopen"
                and "urlopen" in self.url_funcs
                and not _has_kw(n, "timeout")
            ):
                self._flag(stack, "urllib.request.urlopen")
            elif f.id == "flock" and "flock" in self.fcntl and self._blocking_flock(n):
                self._flag(stack, "fcntl.flock")
            return
        if not isinstance(f, ast.Attribute):
            return
        recv = ast.unparse(f.value)
        attr = f.attr
        if (
            isinstance(f.value, ast.Name)
            and f.value.id in self.subs
            and attr in _SUBP_FUNCS
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, f"subprocess.{attr}")
        elif (
            isinstance(f.value, ast.Name)
            and f.value.id in self.fcntl
            and attr == "flock"
            and self._blocking_flock(n)
        ):
            self._flag(stack, "fcntl.flock")
        elif (
            attr in _POPEN_WAITS
            and binds.get(recv, "").endswith("Popen")
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, f"Popen.{attr}")
        elif (
            attr == "connect"
            and (recv in self.psyc or binds.get(recv, "").endswith("Connection"))
            and not _has_kw(n, "connect_timeout")
        ):
            self._flag(stack, "psycopg.connect")
        elif (
            attr == "join"
            and self._sync_ctor(binds.get(recv, ""), "Thread")
            and not n.args
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, "Thread.join")
        elif (
            attr == "get"
            and self._sync_ctor(binds.get(recv, ""), "Queue")
            and not n.args
            and not _has_kw(n, "timeout")
            and not _has_kw(n, "block")
        ):
            self._flag(stack, "Queue.get")
        elif (
            attr == "acquire"
            and any(
                self._sync_ctor(binds.get(recv, ""), leaf)
                for leaf in ("Lock", "RLock", "Semaphore", "BoundedSemaphore")
            )
            and not n.args
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, "Lock.acquire")
        elif (
            attr == "wait"
            and any(
                self._sync_ctor(binds.get(recv, ""), leaf)
                for leaf in ("Event", "Condition")
            )
            and not n.args
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, f"{binds[recv].rsplit('.', 1)[-1]}.wait")
        elif (
            attr == "result"
            and (
                binds.get(recv, "").endswith("submit")
                or binds.get(recv, "").endswith("Future")
            )
            and not n.args
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, "Future.result")
        elif (
            attr == "as_completed"
            and (recv in self.cf or recv.endswith("futures"))
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, "as_completed")
        elif attr in _HTTP_METHODS and recv in self.hx and not _has_kw(n, "timeout"):
            self._flag(stack, f"httpx.{attr}")
        elif (
            attr == "urlopen" and recv == "urllib.request" and not _has_kw(n, "timeout")
        ):
            self._flag(stack, "urllib.request.urlopen")
        elif (
            isinstance(f.value, ast.Call)
            and _call_name(f.value).endswith("Popen")
            and attr in _POPEN_WAITS
            and not _has_kw(n, "timeout")
        ):
            self._flag(stack, f"Popen.{attr}")

    def _blocking_flock(self, n: ast.Call) -> bool:
        """``flock(fd, LOCK_EX|LOCK_SH)`` without ``LOCK_NB`` blocks."""
        if len(n.args) < 2:
            return False
        op = ast.unparse(n.args[1])
        return ("LOCK_EX" in op or "LOCK_SH" in op) and "LOCK_NB" not in op

    def _check_await(self, n: ast.Await, stack: list[_Fn], wrapped: bool) -> None:
        if wrapped or not isinstance(n.value, ast.Call):
            return
        call = n.value
        binds = self._binds_for(stack[-1] if stack else None)
        f = call.func
        if not isinstance(f, ast.Attribute):
            return
        attr = f.attr
        recv = ast.unparse(f.value) if not isinstance(f.value, ast.Call) else ""
        ctor = binds.get(recv, "") if isinstance(f.value, ast.Name) else ""
        leaf = ctor.rsplit(".", 1)[-1]
        if leaf in _AIO_CTORS and attr in _AIO_WAITS and not _has_kw(call, "timeout"):
            self._flag(stack, f"await {leaf}.{attr}")
        elif leaf == "ClientSession":
            # aiohttp/httpx/mcp session awaits — the unbounded-network class.
            self._flag(stack, f"await {recv}.{attr}")
        elif (
            isinstance(f.value, ast.Name)
            and f.value.id in self.aio
            and attr == "open_connection"
        ):
            self._flag(stack, "await asyncio.open_connection")

    def _check_while(self, n: ast.While) -> None:
        """``while True`` poll loops — report only, never a flag."""
        if not (isinstance(n.test, ast.Constant) and n.test.value is True):
            return
        text = ast.unparse(n)
        if "sleep(" not in text:
            return
        if any(
            w in text
            for w in (
                "deadline",
                "timeout",
                "monotonic",
                "perf_counter",
                "attempt",
                "max_",
                "time()",
                ".time",
                "budget",
            )
        ):
            return
        self.poll_lines.append(n.lineno)


def scan_source(rel: str, source: str) -> _Scan:
    return _Scan(rel, source)


def _files(tree: str) -> list[Path]:
    return [
        p
        for p in (ROOT / tree).rglob("*.py")
        if not _SKIP & set(p.relative_to(ROOT).parts)
    ]


def scan_tree(tree: str) -> tuple[Counter[str], dict[str, list[int]]]:
    bad: Counter[str] = Counter()
    polls: dict[str, list[int]] = {}
    for p in _files(tree):
        rel = str(p.relative_to(ROOT))
        scan = scan_source(rel, p.read_text(encoding="utf-8"))
        bad.update(scan.bad)
        if scan.poll_lines:
            polls[rel] = scan.poll_lines
    return bad, polls


def scan() -> dict[str, int]:
    out: Counter[str] = Counter()
    for tree in _ENFORCED:
        bad, _ = scan_tree(tree)
        out.update(bad)
    return dict(sorted(out.items()))


def report() -> dict[str, Any]:
    """Report-only findings: ``while True`` polls + the ``tests/`` tree."""
    polls: dict[str, list[int]] = {}
    for tree in _ENFORCED + _REPORT_ONLY:
        _, p = scan_tree(tree)
        polls.update(p)
    tests_bad, _ = scan_tree("tests")
    return {
        "while_true_polls": polls,
        "tests_tree": dict(sorted(tests_bad.items())),
    }


def _baseline() -> dict[str, int]:
    return dict(json.loads(BASELINE.read_text(encoding="utf-8"))["baseline"])


def test_checker_flags_synthetic_violations() -> None:
    cases = {
        "import subprocess\ndef f():\n    subprocess.run(cmd)\n": "subprocess.run",
        "import subprocess\ndef f():\n    p = subprocess.Popen(cmd)\n    p.wait()\n": "Popen.wait",
        "import psycopg\ndef f():\n    psycopg.connect(dsn)\n": "psycopg.connect",
        "import threading\ndef f():\n    t = threading.Thread(target=g)\n    t.join()\n": "Thread.join",
        "import queue\ndef f():\n    q = queue.Queue()\n    q.get()\n": "Queue.get",
        "import threading\ndef f():\n    l = threading.Lock()\n    l.acquire()\n": "Lock.acquire",
        "import concurrent.futures\ndef f():\n    fut = pool.submit(g)\n    fut.result()\n": "Future.result",
        "from concurrent.futures import as_completed\ndef f():\n    as_completed(fs)\n": "as_completed",
        "import fcntl\ndef f():\n    fcntl.flock(fd, fcntl.LOCK_EX)\n": "fcntl.flock",
        "import asyncio\nasync def f():\n    q = asyncio.Queue()\n    await q.get()\n": "await Queue.get",
        "import asyncio\nasync def f():\n    await asyncio.open_connection(h)\n": "await asyncio.open_connection",
        "async def f():\n    s = aiohttp.ClientSession()\n    await s.get(u)\n": "await s.get",
        "import httpx\ndef f():\n    httpx.get(u)\n": "httpx.get",
        "import urllib.request\ndef f():\n    urllib.request.urlopen(u)\n": "urllib.request.urlopen",
    }
    for src, want in cases.items():
        scan = scan_source("x.py", src)
        assert any(k.endswith(f"::{want}") for k in scan.bad), (
            f"{want} not flagged: {dict(scan.bad)}"
        )
    # Bounded forms pass.
    ok = (
        "import subprocess\ndef f():\n    subprocess.run(cmd, timeout=5)\n"
        "    p = subprocess.Popen(cmd)\n    p.wait(timeout=5)\n"
        "import threading\ndef g():\n    t = threading.Thread(target=h)\n    t.join(5)\n"
        "import asyncio\nasync def w():\n    q = asyncio.Queue()\n"
        "    await asyncio.wait_for(q.get(), 5)\n"
        "import fcntl\ndef l():\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    )
    assert not scan_source("ok.py", ok).bad


def test_no_new_unbounded_waits() -> None:
    base = _baseline()
    now = scan()
    worse = {k: (base.get(k, 0), n) for k, n in now.items() if n > base.get(k, 0)}
    assert not worse, (
        "unbounded wait(s) beyond the frozen baseline (pinned -> now): "
        f"{worse}. Bound the call: timeout=, connect_timeout=, "
        "LOCK_NB, or a wait_for/timeout wrapper around the await."
    )


def test_baseline_only_goes_down() -> None:
    base = _baseline()
    now = scan()
    stale = {k: (n, now.get(k, 0)) for k, n in base.items() if now.get(k, 0) < n}
    assert not stale, (
        "baseline is higher than the tree (pinned -> now): "
        f"{stale}. Lower it: uv run python "
        "tests/test_outbound_timeout_ratchet.py --write"
    )


def test_report_only_scan_runs() -> None:
    """The report-only half must keep working: while-True polls and the
    tests/ tree scan to dicts without raising (their content never gates)."""
    rep = report()
    assert isinstance(rep["while_true_polls"], dict)
    assert isinstance(rep["tests_tree"], dict)


def _write() -> None:
    now = scan()
    if BASELINE.exists():
        base = _baseline()
        up = {k: n for k, n in now.items() if n > base.get(k, 0)}
        if up:
            raise SystemExit(f"refusing to raise the floor: {up}")
    BASELINE.parent.mkdir(exist_ok=True)
    rep = report()
    polls: dict[str, list[int]] = rep["while_true_polls"]
    tests_tree: dict[str, int] = rep["tests_tree"]
    BASELINE.write_text(
        json.dumps({"baseline": now, "report": rep}, indent=1) + "\n",
        encoding="utf-8",
    )
    print(
        f"pinned {sum(now.values())} sites in {len(now)} keys; "
        f"report: {sum(len(v) for v in polls.values())} polls, "
        f"{sum(tests_tree.values())} tests-tree sites"
    )


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write()
    else:
        print(sum(scan().values()))
