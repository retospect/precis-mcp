"""Process launch sites must go through admission (gr647996).

Load-based scheduling (gr647996) routes every process that burns host memory
or CPU through one admission entry point, ``precis.workers.admission``.
This AST ratchet keeps the set of launch sites that do NOT go through it
from growing; the baseline only shrinks.

A *launch site* is, in any ``.py`` file (or python-shebang script) under
``src/`` and ``scripts/``:

* ``subprocess.{Popen,run,call,check_call,check_output,getoutput,
  getstatusoutput}`` (also through a ``sp = subprocess`` alias) and the
  repo's ``MeasuredPopen`` subclass;
* ``asyncio.create_subprocess_{exec,shell}``;
* ``os.{system,popen,fork,forkpty}``, ``os.spawn*``, ``os.posix_spawn*``,
  ``os.exec*``, ``pty.spawn``;
* ``multiprocessing.Process`` (also via ``get_context(...).Process``) and
  ``ProcessPoolExecutor``;
* a list/tuple argv literal headed ``docker``/``podman``/``ssh`` in a scope
  that makes none of the calls above (argv built here, run elsewhere).

Not a site:

* a call whose argv head is a literal binary in :data:`PROBES` — sub-minute
  bounded probes with no compute footprint (their wait bound is
  ``tests/test_outbound_timeout_ratchet.py``'s job) — or a
  ``docker``/``podman`` call whose verb is in :data:`CONTAINER_PROBE_VERBS`;
* a call in a function (or an enclosing one) that calls
  ``admission.admit``/``admission.spawn`` (or ``admit``/``spawn``
  from-imported from ``precis.workers.admission``), and anything inside
  that module itself.

The argv head resolves through a literal argv, a ``list + list`` whose left
side is literal, a shell string's first word, or a local ``cmd = [...]``
binding in the enclosing function. Anything else is unresolvable and counts.

Today's sites are frozen in ``tests/data/launch_admission_baseline.txt`` as
``path::qualname count`` — function-level keys, never line numbers, so
unrelated edits do not churn it. The test fails when

* a key appears or its count rises: route the launch through
  ``precis.workers.admission``, or justify a baseline entry in review;
* a key disappears or its count falls: lower the baseline (it only shrinks).

Regenerate on purpose: ``uv run python tests/test_launch_admission_ratchet.py
--regen`` (refuses to add or raise an entry unless ``--allow-growth``);
``--list`` prints every counted site with its line and argv head.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "data" / "launch_admission_baseline.txt"

_TREES = ("src", "scripts")
_SKIP = {".claude", ".venv", "node_modules", "build", ".git", "__pycache__"}
#: The admission entry point itself launches by design.
_ADMISSION_FILES = frozenset({"src/precis/workers/admission.py"})
_ADMISSION_MODULE = "precis.workers.admission"
_ADMISSION_CALLS = frozenset({"admit", "spawn"})

#: Sub-minute bounded probes: argv heads that never need admission.
PROBES: dict[str, str] = {
    "git": "version-control plumbing; seconds, no compute footprint",
    "gh": "GitHub API client; network-bound, no host load",
    "ps": "process-table read",
    "kill": "signal delivery",
    "sysctl": "kernel parameter read",
    "memory_pressure": "macOS memory-pressure read",
    "nvidia-smi": "GPU state query or reset",
    "systemctl": "unit state query/restart; the unit is a declared daemon",
    "security": "macOS keychain item read",
    "exiftool": "PDF metadata read",
    "chktex": "LaTeX lint of one file",
    "ruff": "lint/format of a few files",
    "hostname": "host identity read",
    "tcpdump": "bounded packet capture (count/time-capped)",
}
#: ``docker``/``podman`` verbs that inspect or tear down, never start load.
CONTAINER_PROBE_VERBS: frozenset[str] = frozenset(
    {
        "ps",
        "inspect",
        "logs",
        "stats",
        "version",
        "info",
        "images",
        "image",
        "rm",
        "rmi",
        "stop",
        "kill",
        "down",
        "port",
        "top",
        "network",
        "volume",
        "context",
        "events",
        "config",
    }
)
_CONTAINER_HEADS = frozenset({"docker", "podman"})
_ARGV_HEADS = _CONTAINER_HEADS | {"ssh"}

_SUBP_FUNCS = frozenset(
    {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    }
)
_ASYNC_FUNCS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
_OS_FUNCS = frozenset({"system", "popen", "fork", "forkpty"})
_OS_PREFIXES = ("spawn", "posix_spawn", "exec")
#: Call leaf names worth classifying (from-imports may alias, so the leaf is
#: the name as called; the import map then decides).
_LEAVES = (
    _SUBP_FUNCS
    | _ASYNC_FUNCS
    | _OS_FUNCS
    | {"MeasuredPopen", "Process", "ProcessPoolExecutor"}
)

_Fn = ast.FunctionDef | ast.AsyncFunctionDef
_Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
#: Nodes that cannot contain a site: skipping them halves the walk.
_LEAF_NODES = (
    ast.Name,
    ast.Constant,
    ast.expr_context,
    ast.operator,
    ast.cmpop,
    ast.unaryop,
    ast.boolop,
    ast.arg,
    ast.alias,
)


@dataclass(frozen=True)
class Site:
    key: str
    line: int
    callee: str
    head: str | None


def _str_head(s: str) -> str | None:
    parts = s.split()
    return parts[0].rsplit("/", 1)[-1] if parts else None


#: Global ``docker``/``docker compose`` flags that take a separate value.
_VALUE_FLAGS = frozenset(
    {"-c", "--context", "-H", "--host", "-l", "--log-level", "--config"}
    | {"-f", "--file", "-p", "--project-name", "--profile", "--env-file"}
    | {"--project-directory", "--ansi", "--progress", "--parallel"}
)


def _verb(tokens: list[str | None]) -> str:
    """The first non-flag token after a container head, past ``compose``
    and global flags' values; ``""`` when a non-literal (``None``) comes
    first."""
    skip = False
    for t in tokens[1:]:
        if skip:
            skip = False
        elif t is None:
            return ""
        elif t in _VALUE_FLAGS:
            skip = True
        elif not (t.startswith("-") or t == "compose"):
            return t
    return ""


class _Scan:
    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.sites: list[Site] = []
        tree = ast.parse(source)
        #: ``alias -> module`` and ``from-imported name -> 'module.name'``,
        #: file-wide (lazy in-function imports included).
        self.mods: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self._binds: dict[int, dict[str, ast.expr]] = {}
        self._evidence: dict[int, bool] = {}
        #: Scopes (innermost def, or the module) holding a launch call: an
        #: argv literal there is that call's argv, not a separate site.
        self._launching: set[int] = set()
        self._calls: list[tuple[ast.Call, list[_Scope]]] = []
        self._argv_lits: list[tuple[ast.expr, list[_Scope]]] = []
        self._tree = tree
        self._visit(tree, [])
        for call, stack in self._calls:
            self._check_call(call, stack)
        for node, stack in self._argv_lits:
            if id(self._inner(stack)) in self._launching:
                continue
            exempt, head = self._exempt(node)
            if not exempt:
                self._site(stack, node, "argv", head)

    # -- scope helpers ------------------------------------------------------

    def _fns(self, stack: list[_Scope]) -> list[_Fn]:
        return [s for s in stack if isinstance(s, _Fn)]

    def _inner(self, stack: list[_Scope]) -> ast.AST:
        fns = self._fns(stack)
        return fns[-1] if fns else self._tree

    def _binds_for(self, stack: list[_Scope]) -> dict[str, ast.expr]:
        fns = self._fns(stack)
        if not fns:
            return {}
        fn = fns[-1]
        if id(fn) not in self._binds:
            out: dict[str, ast.expr] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            out[t.id] = n.value
                elif (
                    isinstance(n, ast.AnnAssign)
                    and isinstance(n.target, ast.Name)
                    and n.value is not None
                ):
                    out[n.target.id] = n.value
            self._binds[id(fn)] = out
        return self._binds[id(fn)]

    def _admission_call(self, n: ast.Call) -> bool:
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr in _ADMISSION_CALLS:
            recv = ast.unparse(f.value)
            return recv.rsplit(".", 1)[-1] == "admission"
        if isinstance(f, ast.Name) and f.id in _ADMISSION_CALLS:
            return self.names.get(f.id, "").startswith(_ADMISSION_MODULE + ".")
        return False

    def _admitted(self, stack: list[_Scope]) -> bool:
        for fn in self._fns(stack):
            if id(fn) not in self._evidence:
                self._evidence[id(fn)] = any(
                    isinstance(n, ast.Call) and self._admission_call(n)
                    for n in ast.walk(fn)
                )
            if self._evidence[id(fn)]:
                return True
        return False

    # -- classification -----------------------------------------------------

    def _mod_is(self, recv: ast.expr, module: str) -> bool:
        return isinstance(recv, ast.Name) and self.mods.get(recv.id) == module

    def _launch_callee(self, n: ast.Call, stack: list[_Scope]) -> str | None:
        f = n.func
        if isinstance(f, ast.Name):
            full = self.names.get(f.id, "")
            mod, _, leaf = full.rpartition(".")
            if f.id == "MeasuredPopen" or full.endswith(".MeasuredPopen"):
                return "MeasuredPopen"
            if mod == "subprocess" and leaf in _SUBP_FUNCS:
                return f"subprocess.{leaf}"
            if mod == "asyncio" and leaf in _ASYNC_FUNCS:
                return f"asyncio.{leaf}"
            if mod == "os" and (leaf in _OS_FUNCS or leaf.startswith(_OS_PREFIXES)):
                return f"os.{leaf}"
            if mod == "pty" and leaf == "spawn":
                return "pty.spawn"
            if mod.startswith("multiprocessing") and leaf == "Process":
                return "multiprocessing.Process"
            if leaf == "ProcessPoolExecutor" or f.id == "ProcessPoolExecutor":
                return "ProcessPoolExecutor"
            return None
        if not isinstance(f, ast.Attribute):
            return None
        attr, recv = f.attr, f.value
        if attr == "MeasuredPopen":
            return "MeasuredPopen"
        if attr in _SUBP_FUNCS and self._mod_is(recv, "subprocess"):
            return f"subprocess.{attr}"
        if attr in _ASYNC_FUNCS and self._mod_is(recv, "asyncio"):
            return f"asyncio.{attr}"
        if self._mod_is(recv, "os") and (
            attr in _OS_FUNCS or attr.startswith(_OS_PREFIXES)
        ):
            return f"os.{attr}"
        if attr == "spawn" and self._mod_is(recv, "pty"):
            return "pty.spawn"
        if attr == "ProcessPoolExecutor":
            return "ProcessPoolExecutor"
        if attr == "Process":
            if self._mod_is(recv, "multiprocessing"):
                return "multiprocessing.Process"
            if isinstance(recv, ast.Call) and ast.unparse(recv.func).endswith(
                "get_context"
            ):
                return "multiprocessing.Process"
            bound = (
                self._binds_for(stack).get(recv.id)
                if isinstance(recv, ast.Name)
                else None
            )
            if isinstance(bound, ast.Call) and ast.unparse(bound.func).endswith(
                "get_context"
            ):
                return "multiprocessing.Process"
        return None

    def _argv_node(self, expr: ast.expr | None, stack: list[_Scope]) -> ast.expr | None:
        """The literal an argv expression resolves to, if any."""
        for _ in range(3):
            if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
                expr = expr.left
            elif isinstance(expr, ast.Name):
                expr = self._binds_for(stack).get(expr.id)
            else:
                break
        if isinstance(expr, ast.List | ast.Tuple | ast.Constant | ast.JoinedStr):
            return expr
        return None

    @staticmethod
    def _tokens(node: ast.expr | None) -> list[str | None]:
        """Literal argv tokens (``None`` for a non-literal element); a shell
        string or f-string contributes its leading literal words."""
        if isinstance(node, ast.List | ast.Tuple):
            return [
                e.value
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
                else None
                for e in node.elts
            ]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return list(node.value.split())
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return [*first.value.split(), None]
        return []

    def _exempt(self, node: ast.expr | None) -> tuple[bool, str | None]:
        """``(exempt, argv head)``: a :data:`PROBES` binary, or a container
        CLI whose verb is in :data:`CONTAINER_PROBE_VERBS`."""
        tokens = self._tokens(node)
        head = tokens[0].rsplit("/", 1)[-1] if tokens and tokens[0] else None
        if head in PROBES:
            return True, head
        verb = _verb(tokens) if head in _CONTAINER_HEADS else ""
        return verb in CONTAINER_PROBE_VERBS, head

    def _site(
        self, stack: list[_Scope], node: ast.AST, callee: str, head: str | None
    ) -> None:
        if self.rel in _ADMISSION_FILES or self._admitted(stack):
            return
        scope = ".".join(s.name for s in stack) or "<module>"
        self.sites.append(
            Site(f"{self.rel}::{scope}", getattr(node, "lineno", 0), callee, head)
        )

    def _check_call(self, n: ast.Call, stack: list[_Scope]) -> None:
        callee = self._launch_callee(n, stack)
        if callee is None:
            return
        self._launching.add(id(self._inner(stack)))
        argv = n.args[0] if n.args else None
        for kw in n.keywords:
            if kw.arg == "args":
                argv = kw.value
        takes_argv = callee.startswith(
            ("subprocess", "asyncio", "os.system", "os.popen", "MeasuredPopen")
        )
        exempt, head = self._exempt(
            self._argv_node(argv, stack) if takes_argv else None
        )
        if not exempt:
            self._site(stack, n, callee, head)

    def _visit(self, node: ast.AST, stack: list[_Scope]) -> None:
        """One walk: record imports, candidate calls and argv literals with
        their scope stacks; classification runs after, once imports are
        all known."""
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, _LEAF_NODES):
                continue
            inner = stack
            if isinstance(ch, _Scope):
                inner = stack + [ch]
            if isinstance(ch, ast.Call):
                f = ch.func
                leaf = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if (
                    leaf in _LEAVES
                    or leaf.startswith(_OS_PREFIXES)
                    or (isinstance(f, ast.Name) and f.id in self.names)
                ):
                    self._calls.append((ch, stack))
            elif isinstance(ch, ast.Import):
                for a in ch.names:
                    top = a.name.split(".")[0]
                    self.mods[a.asname or top] = a.name if a.asname else top
            elif isinstance(ch, ast.ImportFrom) and ch.module:
                for a in ch.names:
                    self.names[a.asname or a.name] = f"{ch.module}.{a.name}"
            elif (
                isinstance(ch, ast.Assign)
                and isinstance(ch.value, ast.Name)
                and ch.value.id in self.mods
            ):
                # ``sp = subprocess``: the alias names the module too.
                for t in ch.targets:
                    if isinstance(t, ast.Name):
                        self.mods[t.id] = self.mods[ch.value.id]
            elif isinstance(ch, ast.List | ast.Tuple) and ch.elts:
                first = ch.elts[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _str_head(first.value) in _ARGV_HEADS
                ):
                    self._argv_lits.append((ch, stack))
            self._visit(ch, inner)


def scan_source(rel: str, source: str) -> list[Site]:
    return _Scan(rel, source).sites


#: Cheap substring pre-filter: only files that could hold a site get parsed
#: (parsing all of ``src/`` costs ~10 s; the guard set wants < 5 s).
_MAYBE = (
    "import subprocess",
    "from subprocess",
    ", subprocess",
    "MeasuredPopen",
    "create_subprocess",
    "multiprocessing",
    "ProcessPoolExecutor",
    "import pty",
    "from pty",
    "from os import",
    "os.system",
    "os.popen",
    "os.fork",
    "os.spawn",
    "os.posix_spawn",
    "os.exec",
    '"docker',
    "'docker",
    '"podman',
    "'podman",
    '"ssh',
    "'ssh",
)


def _python_files(base: Path, shebang: bool) -> Iterable[Path]:
    """``*.py`` under ``base``, plus extensionless python-shebang scripts
    when ``shebang`` (only ``scripts/`` has them; ``src/`` is all modules)."""
    yield from base.rglob("*.py")
    if not shebang:
        return
    for p in base.rglob("*"):
        if p.suffix or not p.is_file():
            continue
        try:
            with p.open(encoding="utf-8") as fh:
                first = fh.readline()
        except (OSError, UnicodeDecodeError):
            continue
        if first.startswith("#!") and "python" in first:
            yield p


def _files(root: Path, trees: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for tree in trees:
        base = root / tree
        if base.is_dir():
            out += [
                p
                for p in _python_files(base, shebang=tree == "scripts")
                if not _SKIP & set(p.relative_to(root).parts)
            ]
    return sorted(set(out))


def scan_sites(root: Path = ROOT, trees: Iterable[str] = _TREES) -> list[Site]:
    sites: list[Site] = []
    for p in _files(root, trees):
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8")
        if not any(tok in text for tok in _MAYBE):
            continue
        try:
            sites.extend(scan_source(rel, text))
        except SyntaxError:
            continue
    return sites


def scan(root: Path = ROOT, trees: Iterable[str] = _TREES) -> dict[str, int]:
    return dict(sorted(Counter(s.key for s in scan_sites(root, trees)).items()))


@functools.cache
def _scan_repo() -> tuple[tuple[str, int], ...]:
    return tuple(scan().items())


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, n = line.rpartition(" ")
        out[key] = int(n)
    return out


def ratchet_problems(
    now: dict[str, int], base: dict[str, int]
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """``(grown, stale)`` as ``key -> (baseline, now)``."""
    grown = {k: (base.get(k, 0), n) for k, n in now.items() if n > base.get(k, 0)}
    stale = {k: (n, now.get(k, 0)) for k, n in base.items() if now.get(k, 0) < n}
    return grown, stale


def assert_ratchet(now: dict[str, int], base: dict[str, int]) -> None:
    grown, stale = ratchet_problems(now, base)
    assert not grown, (
        "new process launch site(s) outside admission (baseline -> now): "
        f"{grown}. Route the launch through precis.workers.admission "
        "(admit()/spawn()), or justify adding a baseline entry "
        "in review: uv run python tests/test_launch_admission_ratchet.py "
        "--regen --allow-growth. A sub-minute probe binary belongs in PROBES "
        "with a reason."
    )
    assert not stale, (
        "baseline is higher than the tree (baseline -> now): "
        f"{stale}. Lower it: uv run python "
        "tests/test_launch_admission_ratchet.py --regen"
    )


def render_baseline(now: dict[str, int]) -> str:
    head = (
        "# Process launch sites outside precis.workers.admission (gr647996).\n"
        "# Shrink-only ratchet: tests/test_launch_admission_ratchet.py.\n"
        "# Format: <path>::<qualname> <count>. Regenerate:\n"
        "#   uv run python tests/test_launch_admission_ratchet.py --regen\n"
    )
    return head + "".join(f"{k} {n}\n" for k, n in sorted(now.items()))


# -- the scanner bites ------------------------------------------------------


def _keys(src: str) -> list[str]:
    return [f"{s.key}::{s.callee}" for s in scan_source("x.py", src)]


def test_scanner_flags_each_launch_form() -> None:
    cases = {
        "import subprocess\ndef f():\n    subprocess.Popen(cmd)\n": "subprocess.Popen",
        "import subprocess as sp\ndef f():\n    sp.run([sys.executable, 'x'])\n": (
            "subprocess.run"
        ),
        "from subprocess import check_output\ndef f():\n    check_output(c)\n": (
            "subprocess.check_output"
        ),
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec(c)\n": (
            "asyncio.create_subprocess_exec"
        ),
        "import os\ndef f():\n    os.system('make all')\n": "os.system",
        "import os\ndef f():\n    os.spawnlp(os.P_NOWAIT, 'x')\n": "os.spawnlp",
        "import os\ndef f():\n    os.execvp('x', [])\n": "os.execvp",
        "import multiprocessing\ndef f():\n"
        "    ctx = multiprocessing.get_context('spawn')\n"
        "    ctx.Process(target=g)\n": "multiprocessing.Process",
        "from concurrent.futures import ProcessPoolExecutor\ndef f():\n"
        "    ProcessPoolExecutor(4)\n": "ProcessPoolExecutor",
        "from precis.utils.job_footprint import MeasuredPopen\ndef f():\n"
        "    MeasuredPopen(['claude', '-p'])\n": "MeasuredPopen",
        "def f():\n    return ['docker', 'run', '--rm', img]\n": "argv",
        "def f():\n    return ['ssh', host, 'uptime']\n": "argv",
        "import subprocess\ndef f():\n    subprocess.getoutput('make all')\n": (
            "subprocess.getoutput"
        ),
        "from subprocess import getstatusoutput\ndef f():\n"
        "    getstatusoutput(cmd)\n": "subprocess.getstatusoutput",
        "import subprocess\nsp = subprocess\ndef f():\n    sp.run(cmd)\n": (
            "subprocess.run"
        ),
    }
    for src, want in cases.items():
        assert any(k.endswith(f"::{want}") for k in _keys(src)), (
            f"{want} not flagged in:\n{src}"
        )


def test_docker_argv_run_inline_counts_once() -> None:
    src = (
        "import subprocess\ndef f():\n"
        "    cmd = ['docker', 'run', img]\n"
        "    subprocess.run(cmd, timeout=5)\n"
    )
    assert _keys(src) == ["x.py::f::subprocess.run"]


def test_probes_and_admitted_sites_pass() -> None:
    ok = (
        "import subprocess\n"
        "def a():\n    subprocess.run(['git', 'status'], timeout=5)\n"
        "def b():\n    cmd = ['nvidia-smi', '-q']\n    subprocess.run(cmd, timeout=5)\n"
        "def c():\n    subprocess.run('ps -Ao pcpu', shell=True, timeout=5)\n"
        "def c2():\n    subprocess.getoutput('git rev-parse HEAD')\n"
        "def d():\n    subprocess.run(['docker', 'compose', '-p', p, 'down'])\n"
        "def e():\n    return ['docker', 'inspect', name]\n"
        "class K:\n"
        "    def g(self):\n"
        "        from precis.workers import admission\n"
        "        with admission.admit(host, fp, holder):\n"
        "            subprocess.Popen(['sh', '-c', x])\n"
        "from precis.workers.admission import spawn\n"
        "def h():\n    spawn(fp)\n    subprocess.Popen(c)\n"
    )
    assert scan_source("ok.py", ok) == []
    assert (
        scan_source("src/precis/workers/admission.py", "import os\nos.fork()\n") == []
    )


def test_keys_are_function_level_with_classes() -> None:
    src = (
        "import subprocess\n"
        "class Runner:\n"
        "    def go(self):\n"
        "        def inner():\n"
        "            subprocess.Popen(c)\n"
        "        inner()\n"
        "subprocess.Popen(c)\n"
    )
    assert sorted(s.key for s in scan_source("m.py", src)) == [
        "m.py::<module>",
        "m.py::Runner.go.inner",
    ]


def test_ratchet_bites_on_a_new_launch_module(tmp_path: Path) -> None:
    """Counter-test: a new module with a fresh Popen fails against the real
    baseline, and dropping a baselined site fails as stale. The tree is
    the baseline plus the temp module (the ratchet test pins tree ==
    baseline), so this test skips the repo scan."""
    pkg = tmp_path / "src" / "precis" / "workers"
    pkg.mkdir(parents=True)
    (pkg / "brand_new_launcher.py").write_text(
        "import subprocess\n\n\ndef go(cmd):\n"
        "    return subprocess.Popen(cmd, start_new_session=True)\n",
        encoding="utf-8",
    )
    base = load_baseline()
    now = base | scan(tmp_path, ("src",))
    with pytest.raises(AssertionError) as exc:
        assert_ratchet(now, base)
    assert "src/precis/workers/brand_new_launcher.py::go" in str(exc.value)
    assert "precis.workers.admission" in str(exc.value)

    shrunk = dict(base)
    gone = next(iter(shrunk))
    del shrunk[gone]
    assert gone in ratchet_problems(shrunk, base)[1]
    with pytest.raises(AssertionError, match="Lower it"):
        assert_ratchet(shrunk, base)


def test_baseline_names_no_hosts() -> None:
    text = BASELINE.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


# -- the ratchet ------------------------------------------------------------


def test_no_new_launch_sites_outside_admission() -> None:
    assert_ratchet(dict(_scan_repo()), load_baseline())


def _main(argv: list[str]) -> None:
    if "--list" in argv:
        for s in sorted(scan_sites(), key=lambda s: (s.key, s.line)):
            print(f"{s.key}:{s.line} {s.callee} head={s.head}")
        return
    if "--regen" not in argv:
        now = scan()
        print(f"{sum(now.values())} sites in {len(now)} keys")
        return
    now = scan()
    if BASELINE.exists() and "--allow-growth" not in argv:
        grown, _ = ratchet_problems(now, load_baseline())
        if grown:
            raise SystemExit(
                f"refusing to grow the baseline: {grown}; pass --allow-growth "
                "only for a reviewed, justified entry"
            )
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(render_baseline(now), encoding="utf-8")
    print(f"pinned {sum(now.values())} sites in {len(now)} keys")


if __name__ == "__main__":
    _main(sys.argv[1:])
