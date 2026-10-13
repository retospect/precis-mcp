"""Test discovery for the python kind — ``view='tests'``.

Ranks test nodes for one symbol by evidence strength, strongest first:

    measured   per-test coverage contexts (``.coverage`` written by
               ``scripts/test --coverage``) verified against the indexed
               bytes via the ``.coverage.precis.json`` manifest
    strong     a resolved ``CallEdge`` from a test function straight into
               the symbol (static direct call)
    medium     reached through non-test callers (helpers, fixtures)
               within ``depth`` reverse call hops, or injected through a
               pytest fixture parameter
    low        the test module imports the symbol (or its module)
               but no call edge resolves
    lowest     test file / test function name mentions the symbol's
               name — no static link at all

Navigation never executes tests, runtrace or source. Measured coverage
is *read* from the SQLite DB the opt-in producer left behind; when it
is absent, unverifiable or context-less the response says so and falls
back to static evidence only. Nothing here claims complete coverage —
dynamic dispatch, skipped tests and unmeasured files are invisible to
both evidence classes.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.handlers._python_reverse import _import_target
from precis.python_index import CallEdge, ModuleIndex, RepoIndex, Symbol
from precis.utils.next_block import render_next_section

log = logging.getLogger(__name__)

#: Per-test-context coverage DB the opt-in producer (``scripts/test
#: --coverage``, or ``scripts/ship --mutate`` in the gate) leaves at the
#: repo root. The manifest is written next to it by
#: ``scripts/lib/coverage_manifest.py`` and binds the DB to indexed bytes.
COVERAGE_DB = ".coverage"
COVERAGE_MANIFEST = ".coverage.precis.json"

TIER_MEASURED = 0
TIER_DIRECT = 1
TIER_INDIRECT = 2
TIER_IMPORTS = 3
TIER_NAMING = 4

_CONFIDENCE = {
    TIER_MEASURED: "measured",
    TIER_DIRECT: "strong",
    TIER_INDIRECT: "medium",
    TIER_IMPORTS: "low",
    TIER_NAMING: "lowest",
}

#: ``depth`` bounds reverse-call hops (test file -> helpers -> symbol)
#: and ``max_results`` bounds rendered rows. Both are validated in the
#: handler; these are the module-level defaults/caps.
DEFAULT_DEPTH = 3
MAX_DEPTH = 6
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_RUN_HINT_CAP = 5


def is_test_path(file: str) -> bool:
    """True when `file` (repo-relative, posix) looks like a test module.

    Covers both a `tests/` root directory and the `test_*.py` /
    `*_test.py` filename conventions, so a file like `src/foo/test_helper.py`
    (no `tests/` root) still counts on the basename rule.
    """
    parts = file.split("/")
    if parts and parts[0] == "tests":
        return True
    basename = parts[-1] if parts else file
    return basename.startswith("test_") or basename.endswith("_test.py")


def _is_fixture(sym: Symbol) -> bool:
    """Indexed symbol decorated as a pytest fixture (best-effort)."""
    return any("fixture" in dec for dec in sym.decorators)


def _is_test_node(sym: Symbol) -> bool:
    """A symbol pytest could collect: ``test_*`` def or a ``Test*`` method."""
    if sym.name.startswith("test"):
        return True
    if sym.kind == "method" and sym.parent:
        return sym.parent.rsplit(".", 1)[-1].startswith("Test")
    return False


@dataclass(frozen=True, slots=True)
class TestHit:
    """One ranked test candidate for a symbol."""

    nodeid: str  # pytest-style `tests/test_x.py::Class::test_y`
    file: str  # repo-relative test file
    tier: int  # TIER_* constant (lower = stronger)
    evidence: str  # short human label for the evidence kind
    distance: int = 0  # reverse-call hops for call-based evidence
    detail: str = ""  # e.g. the helper chain or bound import names

    @property
    def confidence(self) -> str:
        return _CONFIDENCE[self.tier]


@dataclass(slots=True)
class MeasuredCoverage:
    """A ``.coverage`` DB read against a manifest, or why it wasn't used.

    ``state``: ``absent`` (no DB), ``no-contexts`` (DB exists but no
    per-test contexts — a plain gate run), ``manifest-missing`` /
    ``manifest-bad`` / ``manifest-stale`` (bytes cannot be verified),
    ``undecodable`` (coverage package unavailable or DB unreadable),
    ``ok`` (contexts present, manifest verifies at least one file).
    """

    state: str
    note: str
    age: str = ""
    # coverage.CoverageData once state=='ok' (None otherwise); typed
    # Any because coverage is an optional dep imported lazily.
    _data: Any = field(default=None, repr=False)
    # repo-relative file -> measured-DB path key, manifest-verified.
    _verified: dict[str, str] = field(default_factory=dict, repr=False)

    def tests_covering(self, mod: ModuleIndex, start: int, end: int) -> set[str]:
        """Nodeids whose context covered any line of ``[start, end]``.

        Only consulted for files the manifest verified against indexed
        bytes (``_verified``); callers gate on ``self.state == 'ok'``.
        """
        key = self._verified.get(mod.file)
        if key is None or self._data is None:
            return set()
        contexts_by_lineno = self._data.contexts_by_lineno(key)
        out: set[str] = set()
        for lineno in range(start, end + 1):
            for ctx in contexts_by_lineno.get(lineno, ()):
                nodeid = ctx.split("|", 1)[0]
                if nodeid:
                    out.add(nodeid)
        return out


def _manifest_for(root: Path) -> tuple[dict[str, str] | None, str, str]:
    """Read ``.coverage.precis.json`` -> (files map, created_at, problem)."""
    path = root / COVERAGE_MANIFEST
    if not path.is_file():
        return None, "", "manifest-missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        files = raw.get("files")
        if not isinstance(files, dict):
            return None, "", "manifest-bad"
        return (
            {str(k): str(v) for k, v in files.items()},
            str(raw.get("created_at") or ""),
            "",
        )
    except (OSError, ValueError):
        return None, "", "manifest-bad"


def _resolve_measured(path: str, files: set[str]) -> str | None:
    """Match a DB-recorded path to a repo-relative file.

    ``relative_files = true`` stores paths relative to the directory the
    suite ran in (the repo root), so usually ``path == file``. Aliases
    that point deeper (e.g. at ``src/precis``) need the suffix fallback.
    """
    if path in files:
        return path
    for f in files:
        if f.endswith("/" + path) or path.endswith("/" + f):
            return f
    return None


def _age(created_at: str) -> str:
    """Human age from an ISO-8601 UTC stamp; empty when unparseable."""
    try:
        then = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        secs = max(0, int((datetime.now(UTC) - then).total_seconds()))
    except ValueError:
        return ""
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def load_measured(root: Path, idx: RepoIndex) -> MeasuredCoverage:
    """Load the root's ``.coverage`` DB, verifying bytes via the manifest.

    The DB alone never authorizes the ``measured`` tier: the manifest
    (written by ``scripts/lib/coverage_manifest.py`` at production time)
    carries a per-file sha256 of the bytes coverage measured, which we
    compare to the index's ``bytes_sha256``. A DB without a verifiable
    manifest — e.g. left by ``scripts/ship --mutate`` — is reported
    ``manifest-*`` and the caller falls back to static evidence.
    """
    db = root / COVERAGE_DB
    if not db.is_file():
        return MeasuredCoverage(
            state="absent",
            note=(
                "no .coverage DB at this root — measured evidence unavailable "
                "(produce one with `scripts/test --coverage`)"
            ),
        )

    try:
        import coverage  # optional test-time dep; lazy by design
    except ImportError:
        return MeasuredCoverage(
            state="undecodable",
            note=".coverage present but the coverage package is not installed here",
        )
    try:
        data = coverage.CoverageData(basename=str(db))
        data.read()
    except Exception as exc:  # corrupt/old-schema DB — report, don't die
        return MeasuredCoverage(
            state="undecodable",
            note=f".coverage present but unreadable ({type(exc).__name__}: {exc})",
        )

    if not any(ctx.split("|", 1)[0] for ctx in data.measured_contexts()):
        return MeasuredCoverage(
            state="no-contexts",
            note=(
                ".coverage present but has no per-test contexts (a plain gate "
                "run writes none) — regenerate with `scripts/test --coverage`"
            ),
        )

    manifest, created_at, problem = _manifest_for(root)
    if manifest is None:
        return MeasuredCoverage(
            state=problem,
            note=(
                f".coverage has per-test contexts but {COVERAGE_MANIFEST} is "
                f"{problem.split('-', 1)[1]} — bytes cannot be verified "
                "against the index; regenerate with `scripts/test --coverage`"
            ),
        )

    rel_files = set(idx._by_file)
    verified: dict[str, str] = {}  # repo-relative file -> measured DB key
    stale = 0
    for path in data.measured_files():
        if not path:
            continue
        rel = _resolve_measured(path, rel_files)
        if rel is None:
            continue
        mod = idx.file(rel)
        if (
            mod is None
            or mod.bytes_sha256 is None
            or manifest.get(path) != mod.bytes_sha256
        ):
            stale += 1
            continue
        verified[rel] = path

    if not verified:
        return MeasuredCoverage(
            state="manifest-stale",
            note=(
                f".coverage verifies against 0 indexed files "
                f"({stale} stale/missing) — the index moved since the run; "
                "regenerate with `scripts/test --coverage`"
            ),
        )
    note = f".coverage verified for {len(verified)} file{'s' if len(verified) != 1 else ''}"
    if stale:
        note += f", {stale} stale (bytes changed since the run)"
    if age := _age(created_at):
        note += f"; measured {age} ago"
    return MeasuredCoverage(
        state="ok", note=note, age=age, _data=data, _verified=verified
    )


def nodeid_for(sym: Symbol, mod: ModuleIndex) -> str:
    """pytest-style nodeid: ``file.py::Class::test_x`` (module→``::``).

    Module symbols collapse to their file. Nested defs produce
    ``outer::inner`` — pytest cannot select those, but the address is
    still faithful to the index.
    """
    tail = sym.qualname[len(mod.qualname) :].lstrip(".")
    return mod.file if not tail else f"{mod.file}::{tail.replace('.', '::')}"


def _edges_to(idx: RepoIndex) -> dict[str, list[CallEdge]]:
    """callee -> inbound call edges (reverse of callgraph's index)."""
    to: dict[str, list[CallEdge]] = defaultdict(list)
    for mod in idx.modules.values():
        for edge in mod.calls:
            to[edge.callee].append(edge)
    return dict(to)


def _target_qns(sym: Symbol, idx: RepoIndex) -> set[str]:
    """Qualnames a call to which counts as a call to `sym`.

    A class/module target includes every member: a call to ``Cls.m``
    counts toward ``Cls``. Function/method targets stand alone.
    """
    out = {sym.qualname}
    if sym.kind in ("class", "module"):
        out.update(s.qualname for s in idx.symbols_in(sym.qualname))
    return out


def discover_tests(
    sym: Symbol,
    idx: RepoIndex,
    *,
    depth: int = DEFAULT_DEPTH,
    measured: MeasuredCoverage | None = None,
) -> list[TestHit]:
    """Rank test nodes for `sym`, strongest evidence first.

    Static evidence only: the reverse walk uses indexed ``CallEdge``s,
    import bindings and names. ``measured`` (from ``load_measured``)
    overlays verified per-test coverage on top.
    """
    mod = idx.file(sym.file)
    assert mod is not None  # every indexed symbol's file is indexed

    best: dict[str, TestHit] = {}

    def offer(hit: TestHit) -> None:
        prev = best.get(hit.nodeid)
        if prev is None or (hit.tier, hit.distance) < (prev.tier, prev.distance):
            best[hit.nodeid] = hit

    # ── measured coverage (strongest) ────────────────────────────────
    if measured is not None and measured.state == "ok":
        for nodeid in measured.tests_covering(mod, sym.start_line, sym.end_line):
            offer(
                TestHit(
                    nodeid=nodeid,
                    file=nodeid.split("::", 1)[0],
                    tier=TIER_MEASURED,
                    evidence="per-test coverage",
                )
            )

    # ── reverse call walk (direct, then helpers/fixtures) ────────────
    targets = _target_qns(sym, idx)
    edges_to = _edges_to(idx)
    # Inbound edges for every target member (a call to Cls.m counts
    # toward a Cls target): one pass keeps this O(edges).
    inbound: dict[str, list[CallEdge]] = defaultdict(list)
    for callee, edges in edges_to.items():
        if callee in targets or any(callee.startswith(t + ".") for t in targets):
            for edge in edges:
                inbound[callee].append(edge)

    seen = set(targets)
    reached_fixtures: dict[str, tuple[int, str]] = {}  # name -> (dist, qn)
    frontier: list[str] = list(inbound)
    for dist in range(1, depth + 1):
        nxt: list[str] = []
        for callee in frontier:
            for edge in inbound.get(callee) or edges_to.get(callee, []):
                caller = edge.caller
                if caller in seen:
                    continue
                seen.add(caller)
                caller_sym = idx.symbol(caller)
                if is_test_path(edge.file):
                    if caller_sym is not None and _is_fixture(caller_sym):
                        reached_fixtures.setdefault(caller_sym.name, (dist, caller))
                    elif caller_sym is not None and _is_test_node(caller_sym):
                        cmod = idx.file(caller_sym.file)
                        offer(
                            TestHit(
                                nodeid=(
                                    nodeid_for(caller_sym, cmod)
                                    if cmod is not None
                                    else edge.file
                                ),
                                file=edge.file,
                                tier=TIER_DIRECT if dist == 1 else TIER_INDIRECT,
                                evidence=f"static call at {edge.file}:{edge.line}",
                                distance=dist,
                            )
                        )
                    elif caller_sym is None or caller_sym.kind == "module":
                        # Module-scope call inside a test file runs at
                        # collection time — the file is the node.
                        offer(
                            TestHit(
                                nodeid=edge.file,
                                file=edge.file,
                                tier=TIER_DIRECT if dist == 1 else TIER_INDIRECT,
                                evidence=(
                                    f"module-scope call at {edge.file}:{edge.line}"
                                ),
                                distance=dist,
                            )
                        )
                    # Helpers/fixtures in test files are not pytest
                    # nodes — don't report them; keep walking upward.
                nxt.append(caller)
        frontier = nxt

    # Fixture injection: a test function taking a reached fixture by
    # parameter name has no call edge — match it on the signature.
    if reached_fixtures:
        for tmod in idx.modules.values():
            if not is_test_path(tmod.file):
                continue
            for s in tmod.symbols:
                if s.kind not in ("function", "method") or not _is_test_node(s):
                    continue
                sig = s.signature or ""
                for fname, (fdist, fqn) in reached_fixtures.items():
                    if fname in sig:
                        offer(
                            TestHit(
                                nodeid=nodeid_for(s, tmod),
                                file=s.file,
                                tier=TIER_INDIRECT,
                                evidence=f"fixture parameter `{fname}`",
                                distance=fdist + 1,
                                detail=f"via {fqn}",
                            )
                        )
                        break

    # ── imports-only ─────────────────────────────────────────────────
    # File-level evidence (imports, then naming below) is suppressed for
    # a file that already earned a node-level hit — the stronger row says
    # everything the weaker one would.
    sym_mod = mod.qualname
    for tmod in idx.modules.values():
        if not is_test_path(tmod.file):
            continue
        if any(hit.file == tmod.file for hit in best.values()):
            continue
        bound_sym: list[str] = []
        bound_mod: list[str] = []
        for bound, value in tmod.imports.items():
            if value == sym.qualname or value.startswith(sym.qualname + "."):
                bound_sym.append(bound)
            elif _import_target(value, idx) == sym_mod:
                bound_mod.append(bound)
        for fn_qn, bound, value in tmod.local_imports:
            if value == sym.qualname or value.startswith(sym.qualname + "."):
                bound_sym.append(f"{bound} (in {fn_qn})")
            elif _import_target(value, idx) == sym_mod:
                bound_mod.append(f"{bound} (in {fn_qn})")
        if bound_sym or bound_mod:
            names = bound_sym or bound_mod
            offer(
                TestHit(
                    nodeid=tmod.file,
                    file=tmod.file,
                    tier=TIER_IMPORTS,
                    evidence=(
                        f"imports {'symbol' if bound_sym else 'module'} "
                        f"[{', '.join(sorted(names))}]"
                    ),
                )
            )

    # ── naming fallback (weakest) ────────────────────────────────────
    name = sym.name
    for tmod in idx.modules.values():
        if not is_test_path(tmod.file):
            continue
        if any(hit.file == tmod.file for hit in best.values()):
            continue
        if name in Path(tmod.file).stem:
            offer(
                TestHit(
                    nodeid=tmod.file,
                    file=tmod.file,
                    tier=TIER_NAMING,
                    evidence=f"file name mentions `{name}`",
                )
            )
        for s in tmod.symbols:
            if s.kind in ("function", "method") and name in s.name:
                offer(
                    TestHit(
                        nodeid=nodeid_for(s, tmod),
                        file=tmod.file,
                        tier=TIER_NAMING,
                        evidence=f"test name mentions `{name}`",
                    )
                )

    return sorted(best.values(), key=lambda h: (h.tier, h.distance, h.file, h.nodeid))


def render_tests(
    *,
    alias: str,
    sym: Symbol,
    hits: list[TestHit],
    measured: MeasuredCoverage,
    depth: int,
    limit: int,
) -> str:
    """Render the ranked test list with per-row evidence labels."""
    shown = hits[:limit]
    lines = [
        f"# tests for {alias}::{sym.qualname}  "
        f"({len(hits)} hit{'s' if len(hits) != 1 else ''})\n",
        f"coverage: {measured.state} — {measured.note}",
    ]
    if measured.state != "ok":
        lines.append("static evidence only; never a claim of complete coverage")
    lines.append("")
    if not shown:
        lines.append("  (no test evidence found)")
    for hit in shown:
        detail = f"  ({hit.detail})" if hit.detail else ""
        lines.append(f"  {hit.nodeid}  [{hit.confidence}]  {hit.evidence}{detail}")
    if len(hits) > limit:
        lines.append(f"  … ({len(hits) - limit} more; raise max_results to see)")
    lines.append("")
    if shown:
        lines.append("Run:")
        for hit in shown[:_RUN_HINT_CAP]:
            lines.append(f"  scripts/test {hit.nodeid}")
        lines.append("")
    lines.append(
        "Evidence tiers: measured > strong (direct call) > medium "
        f"(indirect, depth<={depth}) > low (imports) > lowest (name match)."
    )
    lines.append(
        "Not shown: dynamic dispatch (getattr/registries), tests in "
        "unregistered roots, and tests added after the index snapshot."
    )
    lines.append("")
    hints = [
        (
            f"get(kind='python', id='{alias}::{sym.qualname}', view='callers')",
            "list resolved call sites",
        ),
        (
            f"get(kind='python', id='{alias}::{sym.qualname}', view='source')",
            "read the symbol's body",
        ),
    ]
    body = "\n".join(lines)
    body += render_next_section(hints)
    return body + "\n"
