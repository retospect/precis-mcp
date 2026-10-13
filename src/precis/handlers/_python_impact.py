"""Bounded change-impact view for the python kind — ``view='impact'``.

Two entry forms:

    get(kind='python', id='<alias>', view='impact',
        args={'base': '<rev>'[, 'head': '<rev>' | 'worktree']})
    get(kind='python', id='<alias>/<file.py>', view='impact',
        args={'base': '<rev>'[, 'head': ...]})
    get(kind='python', id='<alias>::<qualname>', view='impact')

Revision mode diffs two snapshots: ``base`` (a git revision) against
``head`` (another revision, or — default — the live working tree, read
fresh so uncommitted bytes get real fingerprints). Changed files are
indexed on both sides via ``index_module_bytes`` and their symbol tables
compared: added, removed, signature-changed, body-changed, moved
(in-file line move or cross-file relocate). Changed modules keep their
qualnames across a rename, so a pure rename reads as one ``moved``
module plus a diff of its symbols.

Symbol mode takes one symbol as the (hypothetical or already-applied)
change and skips the diff.

Both modes then fan out on the *indexed working tree*: bounded
transitive callers and importers (``depth``, ``max_results``), affected
tests via the ``tests`` view's evidence ladder, and owning docs (module
docstrings + ``*.md``/skill files naming the qualname). Unresolved
same-name call sites, unparsed changed files, and roots outside the
scan stay visible in an explicit ``Unknowns`` section. Nothing here
executes Python source; Git is read-only plumbing (``ls-tree`` /
``show`` / ``diff --name-status`` / ``status --porcelain``) under the
same env-scrubbed invocation as the provenance observer, with one total
time budget.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from precis.errors import BadInput
from precis.handlers import _python_tests as ptests
from precis.handlers._python_reverse import _import_target
from precis.python_index import ModuleIndex, RepoIndex, Symbol
from precis.python_index.indexer import _SKIP_DIRS, index_module_bytes
from precis.utils.next_block import render_next_section

#: Total wall-clock budget for every git invocation of one view call.
_GIT_BUDGET_S = 10.0

DEFAULT_DEPTH = 2
MAX_DEPTH = 6
DEFAULT_LIMIT = 30
MAX_LIMIT = 200

#: Changed-symbol seeds capped for per-symbol fan-out (tests/docs) —
#: large diffs still report every changed symbol but the expensive
#: per-symbol joins stay bounded.
_SEED_CAP = 12

#: Doc-scan bounds: at most this many text docs, each at most this big.
_DOC_FILES_CAP = 800
_DOC_BYTES_CAP = 512 * 1024

_DOC_SUFFIXES = (".md", ".rst", ".txt")


@dataclass(frozen=True, slots=True)
class ChangedSymbol:
    """One symbol-level change between two snapshots."""

    qualname: str  # head-side qualname (base-side for removals)
    file: str  # head-side file (base-side for removals)
    change: str  # added | removed | signature | body | moved
    detail: str = ""  # e.g. "def f(a) -> def f(a, b)" or "moved to pkg/b.py"
    live: bool = True  # False for removed symbols (no live callers)


@dataclass(slots=True)
class Impact:
    """Everything the renderer needs, already bounded."""

    base_desc: str
    head_desc: str
    changed_files: list[str]
    changes: list[ChangedSymbol]
    callers: list[tuple[str, str, int, str]]  # (caller qn, file, line, of)
    importers: list[tuple[str, str, str]]  # (module qn, file, imports-of)
    tests: list[ptests.TestHit]
    test_for: dict[str, str]  # nodeid -> changed qualname it evidences
    docs: list[tuple[str, str]]  # (file, what it names)
    unknowns: list[str]
    fingerprints: dict[str, str]  # file -> head-side bytes sha256 (worktree)
    n_caller_rows: int = 0  # pre-cap row count for "… N more" lines
    truncated_files: bool = False


# ---------------------------------------------------------------------------
# Git plumbing — env-scrubbed, budgeted, read-only
# ---------------------------------------------------------------------------


class _Git:
    """Bounded read-only git for one root (worktree-aware like provenance)."""

    def __init__(self, root: Path, git_dir: Path | None) -> None:
        self.root = root
        self.git_dir = git_dir
        self.deadline = time.monotonic() + _GIT_BUDGET_S

    def run(self, *args: str, check: bool = True) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BadInput(
                "impact: git observation budget exhausted",
                next="retry, or narrow the diff (a file id, or base near head)",
            )
        env = {
            **{
                k: v
                for k, v in os.environ.items()
                if k
                not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
            },
            "GIT_OPTIONAL_LOCKS": "0",
            **(
                {"GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.root)}
                if self.git_dir is not None
                else {}
            ),
        }
        try:
            proc = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    "-C",
                    str(self.root),
                    *args,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(remaining, 5.0),
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BadInput(
                f"impact: git unavailable for {self.root} ({type(exc).__name__})",
                next="compare registered roots whose checkouts are reachable",
            ) from exc
        if check and proc.returncode:
            raise BadInput(
                f"impact: `git {' '.join(args[:2])}` failed: "
                f"{proc.stderr.strip()[:200] or 'exit ' + str(proc.returncode)}",
                next="check the revision names (git rev-parse) and retry",
            )
        return proc.stdout

    def resolve_rev(self, rev: str) -> str:
        out = self.run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        sha = out.strip()
        if not sha:
            raise BadInput(
                f"impact: revision {rev!r} does not resolve in {self.root}",
                next="pass a commit sha/branch/tag, or omit head= for the worktree",
            )
        return sha

    def show(self, rev: str, path: str) -> bytes | None:
        """``git show <rev>:<path>`` bytes, or None when absent."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return None
        env = {
            **{
                k: v
                for k, v in os.environ.items()
                if k
                not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
            },
            "GIT_OPTIONAL_LOCKS": "0",
            **(
                {"GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.root)}
                if self.git_dir is not None
                else {}
            ),
        }
        try:
            proc = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(self.root),
                    "show",
                    f"{rev}:{path}",
                ],
                capture_output=True,
                timeout=min(remaining, 5.0),
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def tree_paths(self, rev: str) -> set[str]:
        """Every tracked ``*.py`` path at ``rev`` (pathspec ``*.py``
        doesn't match nested paths — filter in Python)."""
        out = self.run("ls-tree", "-r", "--name-only", "-z", rev)
        return {p for p in out.split("\0") if p.endswith(".py")}

    def changed_paths(self, base: str, head: str | None) -> dict[str, str]:
        """``{path: status-letter}`` for changed ``*.py`` files.

        ``head=None`` means the working tree: ``git diff <base>`` covers
        tracked modifications/deletions/additions; ``status --porcelain``
        adds untracked files. Status letters: A/M/D (renames arrive as
        separate D+A rows — callers don't need R handling).
        """
        if head is not None:
            out = self.run(
                "diff",
                "--name-status",
                "-z",
                "--no-renames",
                base,
                head,
                "--",
                "*.py",
            )
        else:
            out = self.run(
                "diff",
                "--name-status",
                "-z",
                "--no-renames",
                base,
                "--",
                "*.py",
            )
        changes: dict[str, str] = {}
        fields = [f for f in out.split("\0") if f]
        for i in range(0, len(fields) - 1, 2):
            status, path = fields[i], fields[i + 1]
            changes[path] = status[0]
        if head is None:
            status = self.run(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=all",
                "--",
                "*.py",
            )
            for line in status.split("\0"):
                if line.startswith("?? "):
                    changes.setdefault(line[3:], "A")
        return changes


# ---------------------------------------------------------------------------
# Snapshot indexing (git blobs / live bytes)
# ---------------------------------------------------------------------------


def _qualname_from_pathset(rel: str, pathset: set[str]) -> str:
    """``_qualname_for_file`` for a path not on disk (a git blob).

    Walks ``<dir>/__init__.py`` membership in the side's own path set —
    the same package-boundary rule the on-disk indexer applies.
    """
    parts = rel.split("/")
    stem = parts[-1][: -len(".py")]
    dirs = parts[:-1]
    if stem == "__init__":
        leaf: list[str] = [dirs[-1]] if dirs else []
    else:
        leaf = [stem]
    while dirs and ("/".join(dirs) + "/__init__.py") in pathset:
        leaf.insert(0, dirs.pop())
    return ".".join(leaf) or stem


def _index_side(
    mods: dict[str, ModuleIndex],
    files: dict[str, bytes | None],
    pathset: set[str],
) -> dict[str, ModuleIndex]:
    """Index every path of ``files`` (value = raw bytes or None=absent)."""
    for rel, raw in files.items():
        if raw is None:
            continue
        mods[rel] = index_module_bytes(
            raw,
            qualname=_qualname_from_pathset(rel, pathset),
            file_relative=rel,
            filename=f"<snapshot>/{rel}",
        )
    return mods


def _body_slice(mod: ModuleIndex, sym: Symbol) -> str:
    """The symbol's source text within its module snapshot."""
    src = mod.source or ""
    return "\n".join(src.splitlines()[sym.start_line - 1 : sym.end_line])


def _diff_modules(
    base: dict[str, ModuleIndex], head: dict[str, ModuleIndex]
) -> list[ChangedSymbol]:
    """Symbol-level diff of two per-file snapshot maps."""
    changes: list[ChangedSymbol] = []

    removed_syms: dict[tuple[str, str], Symbol] = {}  # (name, kind) -> sym
    added_syms: dict[tuple[str, str], Symbol] = {}

    for rel in sorted(set(base) | set(head)):
        bmod = base.get(rel)
        hmod = head.get(rel)
        if bmod is None and hmod is not None:
            for s in hmod.symbols:
                if s.kind != "module":
                    added_syms[(s.name, s.kind)] = s
                changes.append(
                    ChangedSymbol(s.qualname, rel, "added", f"new file {rel}")
                )
            continue
        if hmod is None and bmod is not None:
            for s in bmod.symbols:
                if s.kind != "module":
                    removed_syms[(s.name, s.kind)] = s
                changes.append(
                    ChangedSymbol(
                        s.qualname, rel, "removed", "file deleted", live=False
                    )
                )
            continue
        assert bmod is not None and hmod is not None
        if bmod.parse_error or hmod.parse_error:
            changes.append(
                ChangedSymbol(
                    hmod.qualname,
                    rel,
                    "body",
                    f"parse error ({'base' if bmod.parse_error else 'head'} side)",
                )
            )
            continue
        bsyms = {s.qualname: s for s in bmod.symbols}
        hsyms = {s.qualname: s for s in hmod.symbols}
        for qn in sorted(set(bsyms) | set(hsyms)):
            b, h = bsyms.get(qn), hsyms.get(qn)
            if b is None and h is not None:
                added_syms[(h.name, h.kind)] = h
                changes.append(ChangedSymbol(qn, rel, "added"))
            elif h is None and b is not None:
                removed_syms[(b.name, b.kind)] = b
                changes.append(ChangedSymbol(qn, rel, "removed", live=False))
            else:
                assert b is not None and h is not None
                if b.signature != h.signature:
                    changes.append(
                        ChangedSymbol(
                            qn,
                            rel,
                            "signature",
                            f"{b.signature or b.kind} -> {h.signature or h.kind}",
                        )
                    )
                elif b.kind == "module":
                    # The module symbol's "body" is the whole file —
                    # only its docstring change is a contract signal.
                    if b.docstring != h.docstring:
                        changes.append(
                            ChangedSymbol(qn, rel, "body", "module docstring")
                        )
                elif _body_slice(bmod, b) != _body_slice(hmod, h):
                    changes.append(ChangedSymbol(qn, rel, "body"))
                elif b.start_line != h.start_line:
                    changes.append(
                        ChangedSymbol(
                            qn,
                            rel,
                            "moved",
                            f"L{b.start_line} -> L{h.start_line} (same file)",
                        )
                    )

    # Cross-file moves: a removed (name, kind) reappearing added with
    # identical signature+body. Rewrite both rows as one `moved`.
    moved_pairs: list[tuple[Symbol, Symbol]] = []
    for key, gone in removed_syms.items():
        new = added_syms.get(key)
        if new is None or gone.file == new.file or gone.signature != new.signature:
            continue
        bmod = base.get(gone.file)
        hmod = head.get(new.file)
        if (
            bmod is not None
            and hmod is not None
            and _body_slice(bmod, gone) == _body_slice(hmod, new)
        ):
            moved_pairs.append((gone, new))
    if moved_pairs:
        moved_from = {g.qualname for g, _ in moved_pairs}
        moved_to = {n.qualname for _, n in moved_pairs}
        changes = [c for c in changes if c.qualname not in moved_from | moved_to]
        for gone, new in moved_pairs:
            changes.append(
                ChangedSymbol(
                    new.qualname,
                    new.file,
                    "moved",
                    f"{gone.qualname} ({gone.file}) -> {new.qualname} ({new.file})",
                )
            )
    return changes


# ---------------------------------------------------------------------------
# Bounded fan-out on the live index
# ---------------------------------------------------------------------------


def _fanout_callers(
    idx: RepoIndex, seed_qns: list[str], *, depth: int, limit: int
) -> tuple[list[tuple[str, str, int, str]], int]:
    """Reverse-BFS callers of every seed, bounded by depth and limit.

    Level 1 matches callees by seed prefix (a call to ``Cls.m`` counts
    toward a ``Cls`` seed); deeper levels use exact callee lookups.
    """
    edges_to = ptests._edges_to(idx)
    seeds = set(seed_qns)
    inbound: dict[str, list] = {}
    for callee, edges in edges_to.items():
        if callee in seeds or any(callee.startswith(s + ".") for s in seeds):
            inbound[callee] = edges
    rows: list[tuple[str, str, int, str]] = []
    total = 0
    visited = set(seed_qns)
    frontier: list[tuple[str, str]] = [(q, q) for q in inbound]
    for _ in range(depth):
        nxt: list[tuple[str, str]] = []
        for qn, seed in frontier:
            for e in inbound.get(qn) or edges_to.get(qn, []):
                if e.caller in visited:
                    continue
                visited.add(e.caller)
                total += 1
                if len(rows) < limit:
                    rows.append((e.caller, e.file, e.line, seed))
                nxt.append((e.caller, seed))
        frontier = nxt
    rows.sort(key=lambda r: (r[1], r[2], r[0]))
    return rows, total


def _fanout_importers(
    idx: RepoIndex, seed_modules: list[str], *, depth: int, limit: int
) -> list[tuple[str, str, str]]:
    """Reverse-BFS module importers, bounded."""
    importers_of: dict[str, list[tuple[ModuleIndex, str]]] = {}
    for mod in idx.modules.values():
        for bound, value in mod.imports.items():
            importers_of.setdefault(_import_target(value, idx), []).append((mod, bound))
        for _fn, bound, value in mod.local_imports:
            importers_of.setdefault(_import_target(value, idx), []).append((mod, bound))
    rows: list[tuple[str, str, str]] = []
    seen = set(seed_modules)
    frontier = list(seed_modules)
    for _ in range(depth):
        nxt: list[str] = []
        for mod_qn in frontier:
            for mod, bound in importers_of.get(mod_qn, []):
                if mod.qualname in seen:
                    continue
                seen.add(mod.qualname)
                if len(rows) < limit:
                    rows.append((mod.qualname, mod.file, bound))
                nxt.append(mod.qualname)
        frontier = nxt
    rows.sort()
    return rows


def _owning_docs(
    idx: RepoIndex, root: Path, needles: set[str], *, limit: int
) -> tuple[list[tuple[str, str]], int]:
    """Modules whose docstring, and docs files whose text, name a needle."""
    hits: list[tuple[str, str]] = []
    scanned = 0
    # In-memory docstring owners first — no IO.
    for mod in idx.modules.values():
        for s in mod.symbols:
            if s.docstring and any(n in s.docstring for n in needles):
                named = next(n for n in needles if n in s.docstring)
                hits.append((mod.file, f"docstring of {s.qualname} names {named}"))
    # Then repo docs (bounded walk, same skip set as the .py indexer).
    stack = [root]
    while stack and scanned < _DOC_FILES_CAP and len(hits) < limit:
        d = stack.pop()
        try:
            ents = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for ent in ents:
            if ent.is_symlink():
                continue
            if ent.is_dir():
                if ent.name in _SKIP_DIRS or ent.name.startswith("."):
                    continue
                stack.append(ent)
            elif ent.suffix in _DOC_SUFFIXES:
                scanned += 1
                try:
                    if ent.stat().st_size > _DOC_BYTES_CAP:
                        continue
                    text = ent.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                doc_named = next((n for n in needles if n in text), None)
                if doc_named:
                    rel = ent.relative_to(root).as_posix()
                    if len(hits) < limit:
                        hits.append((rel, f"names {doc_named}"))
            if scanned >= _DOC_FILES_CAP or len(hits) >= limit:
                break
    return hits, scanned


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------


def impact_diff(
    *,
    alias: str,
    root: Path,
    idx: RepoIndex,
    git: _Git,
    base: str,
    head: str | None,
    only_file: str | None,
    depth: int,
    limit: int,
    measured: ptests.MeasuredCoverage,
    other_roots: list[str],
) -> Impact:
    """Diff ``base``..``head`` (or the worktree) and fan out."""
    base_sha = git.resolve_rev(base)
    head_sha = git.resolve_rev(head) if head is not None else None

    paths = git.changed_paths(base_sha, head_sha)
    if only_file is not None:
        paths = {p: s for p, s in paths.items() if p == only_file}
        if not paths:
            raise BadInput(
                f"impact: {only_file!r} unchanged between {base} and "
                f"{head or 'worktree'}",
                next=f"get(kind='python', id={alias!r}, view='impact', "
                f"args={{'base': {base!r}}}) for the whole repo",
            )

    base_tree = git.tree_paths(base_sha)
    head_tree = git.tree_paths(head_sha) if head_sha else set()
    # For the worktree side, live (possibly uncommitted/untracked) paths
    # come from the index's own file set plus anything status flagged.
    wt_paths = set(idx._by_file)
    head_files: dict[str, bytes | None] = {}
    fingerprints: dict[str, str] = {}
    for rel, _status in paths.items():
        if head_sha is not None:
            head_files[rel] = git.show(head_sha, rel) if rel in head_tree else None
        else:
            p = root / rel
            try:
                raw = p.read_bytes() if p.is_file() else None
            except OSError:
                raw = None
            head_files[rel] = raw
            if raw is not None:
                fingerprints[rel] = hashlib.sha256(raw).hexdigest()
    base_files: dict[str, bytes | None] = {
        rel: git.show(base_sha, rel) if rel in base_tree else None for rel in paths
    }

    base_mods = _index_side({}, base_files, base_tree)
    head_pathset = head_tree or (wt_paths | set(head_files))
    head_mods = _index_side({}, head_files, head_pathset)
    changes = _diff_modules(base_mods, head_mods)

    head_desc = (head_sha or "worktree")[:12]
    return _assemble(
        alias=alias,
        idx=idx,
        base_desc=base_sha[:12],
        head_desc=head_desc,
        changed_files=sorted(paths),
        changes=changes,
        head_is_rev=head_sha is not None,
        depth=depth,
        limit=limit,
        measured=measured,
        fingerprints=fingerprints,
        other_roots=other_roots,
    )


def impact_symbol(
    *,
    alias: str,
    sym: Symbol,
    idx: RepoIndex,
    depth: int,
    limit: int,
    measured: ptests.MeasuredCoverage,
    other_roots: list[str],
) -> Impact:
    """Single-symbol blast radius: no diff, same fan-out sections."""
    return _assemble(
        alias=alias,
        idx=idx,
        base_desc=f"symbol {sym.qualname}",
        head_desc="indexed worktree",
        changed_files=[sym.file],
        changes=[ChangedSymbol(sym.qualname, sym.file, "seed", "queried symbol")],
        head_is_rev=False,
        depth=depth,
        limit=limit,
        measured=measured,
        fingerprints={},
        other_roots=other_roots,
    )


def _assemble(
    *,
    alias: str,
    idx: RepoIndex,
    base_desc: str,
    head_desc: str,
    changed_files: list[str],
    changes: list[ChangedSymbol],
    head_is_rev: bool,
    depth: int,
    limit: int,
    measured: ptests.MeasuredCoverage,
    fingerprints: dict[str, str],
    other_roots: list[str],
) -> Impact:
    """Shared fan-out + unknowns for both entry forms."""
    unknowns: list[str] = []

    live_qns = [c.qualname for c in changes if c.live]
    # Seed symbols for per-symbol fan-out (bounded).
    seeds: list[Symbol] = []
    for qn in live_qns:
        s = idx.symbol(qn)
        if s is not None:
            seeds.append(s)
        if len(seeds) >= _SEED_CAP:
            break
    if len(live_qns) > _SEED_CAP:
        unknowns.append(
            f"per-symbol test/doc fan-out sampled the first {_SEED_CAP} of "
            f"{len(live_qns)} changed symbols"
        )

    seed_qns = [s.qualname for s in seeds]
    callers, n_callers = _fanout_callers(idx, seed_qns, depth=depth, limit=limit)
    seed_mods = sorted(
        {m.qualname for s in seeds if (m := idx.file(s.file)) is not None}
    )
    importers = _fanout_importers(idx, seed_mods, depth=depth, limit=limit)

    by_nodeid: dict[str, ptests.TestHit] = {}
    test_for: dict[str, str] = {}
    per_seed = max(3, limit // max(1, len(seeds)))
    for s in seeds:
        for hit in ptests.discover_tests(s, idx, depth=depth, measured=measured)[
            :per_seed
        ]:
            prev = by_nodeid.get(hit.nodeid)
            if prev is None or hit.tier < prev.tier:
                by_nodeid[hit.nodeid] = hit
                test_for[hit.nodeid] = s.qualname
    tests = sorted(by_nodeid.values(), key=lambda h: (h.tier, h.file, h.nodeid))[:limit]

    needles = set(live_qns) | {s.qualname for s in seeds} | set(seed_mods)
    docs, _scanned = _owning_docs(idx, idx.root, needles, limit=limit)

    # Explicit unknowns: unresolved same-name call sites + exclusions.
    names = {s.name for s in seeds}
    unresolved = sum(
        1
        for mod in idx.modules.values()
        for e in mod.calls
        if e.callee.startswith("ext:")
        and e.callee.rsplit(".", 1)[-1].removeprefix("ext:") in names
    )
    if unresolved:
        unknowns.append(
            f"{unresolved} unresolved same-name call site{'s' if unresolved != 1 else ''} "
            "(ext:*) — dynamic dispatch is invisible to this view"
        )
    unparsed = [c.file for c in changes if "parse error" in c.detail]
    if unparsed:
        unknowns.append(
            f"unparsed changed file{'s' if len(unparsed) != 1 else ''}: "
            f"{', '.join(sorted(set(unparsed)))}"
        )
    if other_roots:
        unknowns.append(
            f"caller/importer/test fan-out covers {alias} only; "
            f"registered roots not scanned: {', '.join(sorted(other_roots))}"
        )
    if head_is_rev:
        unknowns.append(
            "fan-out (callers/importers/tests/docs) computed on the indexed "
            "working tree, not on the head revision"
        )
    return Impact(
        base_desc=base_desc,
        head_desc=head_desc,
        changed_files=changed_files,
        changes=changes,
        callers=callers,
        importers=importers,
        tests=tests,
        test_for=test_for,
        docs=docs,
        unknowns=unknowns,
        fingerprints=fingerprints,
        n_caller_rows=n_callers,
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_GLYPH = {
    "added": "+",
    "removed": "-",
    "signature": "~",
    "body": "~",
    "moved": "~",
    "seed": "·",
}


def render_impact(alias: str, imp: Impact, *, depth: int, limit: int) -> str:
    lines = [
        f"# impact on {alias}: {imp.base_desc}..{imp.head_desc}  "
        f"({len(imp.changed_files)} file{'s' if len(imp.changed_files) != 1 else ''}, "
        f"{len(imp.changes)} symbol{'s' if len(imp.changes) != 1 else ''})\n"
    ]

    lines.append("Changed symbols:")
    if not imp.changes:
        lines.append("  (no symbol-level changes)")
    for c in imp.changes[:limit]:
        detail = f"  {c.detail}" if c.detail else ""
        lines.append(
            f"  {_GLYPH.get(c.change, '?')} {c.change:<9} {alias}::{c.qualname}  "
            f"{c.file}{detail}"
        )
    if len(imp.changes) > limit:
        lines.append(f"  … ({len(imp.changes) - limit} more)")

    lines.append("")
    lines.append(f"Callers of changed symbols (depth<={depth}):")
    if not imp.callers:
        lines.append("  (none resolved)")
    for caller, file, line, seed in imp.callers:
        lines.append(f"  {alias}::{caller}  {file}:{line}  calls {seed}")
    if imp.n_caller_rows > len(imp.callers):
        lines.append(
            f"  … ({imp.n_caller_rows - len(imp.callers)} more; raise max_results)"
        )

    lines.append("")
    lines.append(f"Importers of changed modules (depth<={depth}):")
    if not imp.importers:
        lines.append("  (none)")
    for qn, file, bound in imp.importers:
        lines.append(f"  {alias}::{qn}  {file}  [{bound}]")

    if imp.fingerprints:
        lines.append("")
        lines.append("Worktree fingerprints (uncommitted bytes, exact sha256):")
        for rel, sha in sorted(imp.fingerprints.items())[:10]:
            lines.append(f"  {rel}  {sha[:12]}")

    lines.append("")
    lines.append("Affected tests:")
    if not imp.tests:
        lines.append("  (no test evidence found)")
    for hit in imp.tests:
        lines.append(
            f"  {hit.nodeid}  [{hit.confidence}]  "
            f"for {imp.test_for.get(hit.nodeid, '')} — {hit.evidence}"
        )

    lines.append("")
    lines.append("Docs naming changed symbols:")
    if not imp.docs:
        lines.append("  (none)")
    for file, what in imp.docs:
        lines.append(f"  {file}  ({what})")

    lines.append("")
    lines.append("Unknowns:")
    if not imp.unknowns:
        lines.append("  (none beyond the usual: dynamic dispatch invisible)")
    for u in imp.unknowns:
        lines.append(f"  - {u}")

    lines.append("")
    hints: list[tuple[str, str]] = []
    if imp.changes:
        first = imp.changes[0]
        if first.live:
            hints.append(
                (
                    f"get(kind='python', id='{alias}::{first.qualname}', view='tests')",
                    "full evidence ladder for one changed symbol",
                )
            )
            hints.append(
                (
                    f"get(kind='python', id='{alias}::{first.qualname}', "
                    "view='callers')",
                    "its resolved call sites",
                )
            )
    body = "\n".join(lines)
    body += render_next_section(hints)
    return body + "\n"
