"""``red_suspects`` — the red build's ranked changed-files table (gr595552).

A red check.yml run hands its fixer a ``scripts/ci-failed`` digest via
``ci_red_digest``'s ``brief:`` comment; nothing there says which files
changed since the last green build, which gripes touched them, or which
sit on the failure path. This assembles that one view — no new storage —
from git and the gripe/todo ledgers:

* **file** — every path the ``last-green..this-build`` diff touched
  (``git diff --numstat --no-renames``, a rename as delete + add);
* **lines** — added + deleted of the diff stat (binary files count 0);
* **commits** — the range commits touching the file (``git log
  --name-only``); several gripes-worth of commits on one file ranks
  higher;
* **gripes** — the commit → ledger mapping of the ``resolved: <sha>`` /
  ``landed: <sha>`` comments: a gripe's own ``resolved:`` names the sha
  that fixed it, a todo's ``landed:`` resolves through its plan root's
  ``related-to`` gripe (``td<N>`` when there is none);
* **on the failure path** — the file is a failing test's own file, an
  src module that test imports at the range's end
  (``red_waiver.imported_src_paths``), a repo path a ``File "…"``
  traceback line of the digest names, a path the digest text mentions
  at all, or the file of a changed symbol a traceback ``in <name>`` or
  a failing test id names. These rank first.

Sort: on the failure path, then commits touching it, then lines
changed — the gripe's own ordering. "Last green" is the newest
non-waived green row of ``release.ci_verdicts`` (a waived row was a red
CI run the gate deployed past, so its range stays suspect); the cut's
recorded ``prev_prod..head`` range is the fallback — prod only advances
on a verdict the gate calls green.

The python kind renders the changed symbols as a view: under the file
table and the fan-out pointer (``get(kind='python', view='impact')`` —
the caller/importer/test fan-out) the block lists the range's
symbol-level diff, the same two-side snapshot index and symbol
comparison ``view='impact'`` runs
(:mod:`precis.handlers._python_impact`). Every changed file gets its
full row and the pointer always ships: the brief is the fixer's only
copy of the complete table, so a caller's ``budget`` trims the symbol
tail, never a file row or the pointer (td655162).

The ledger-pointer column (the touching gripe's current-state summary)
waits on the code-anchor handle and the gripe summary block (gr595549);
the git half ships alone. The fixer's finding goes back as a ledger
entry on the culprit gripe — nothing here writes.

Everything degrades to ``""`` — an unreadable range, a missing checkout
or a ledger fault must never take the brief down with it.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.plans import gates as _gates
from precis.plans import red_waiver as _waiver

if TYPE_CHECKING:
    from precis.handlers import _python_impact as _impact

log = logging.getLogger(__name__)

#: Detailed rows of the changed-symbol view; the overflow's qualnames
#: are listed the same way the file tail lists paths.
_SYM_CAP = 40

#: Shortest sha prefix a ledger match may name — git's abbreviation floor
#: (``gates.MIN_SHA_LEN``, restated: a private helper's rule stays local).
_SHA_MIN = 7


@dataclass(frozen=True)
class FileStat:
    """One changed path: its diff stat and the range commits touching it."""

    path: str
    added: int
    deleted: int
    commits: tuple[str, ...]


@dataclass(frozen=True)
class SuspectRow:
    """A ranked row: the file plus what the ledgers and digest say."""

    path: str
    added: int
    deleted: int
    commits: tuple[str, ...]
    gripes: tuple[str, ...]
    on_path: bool
    #: The file's changed symbols (:func:`changed_symbols`), rendered
    #: under the table — ``()`` for non-Python files and unreadable diffs.
    symbols: tuple[_impact.ChangedSymbol, ...] = ()


# ── git reads ──────────────────────────────────────────────────────


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str | None:
    """One ``git -C root`` read; ``None`` on any failure (red_waiver's)."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def _prepare_range(root: Path, rng: str, env: dict[str, str] | None = None) -> bool:
    """Fetch origin (a cut pushed from another checkout may be absent
    here, ``red_waiver.range_paths``'s rule) and verify both ends of the
    two-sided ``rng`` resolve."""
    if ".." not in rng:
        return False
    _git(root, "fetch", "-q", "origin", env=env)
    for end in (e for e in rng.split("..", 1) if e):
        if _git(root, "rev-parse", "--verify", "--quiet", f"{end}^{{commit}}") is None:
            return False
    return True


def _int(text: str) -> int:
    return int(text) if text.isdigit() else 0


def _numstat(
    root: Path, rng: str, env: dict[str, str] | None = None
) -> dict[str, tuple[int, int]] | None:
    """``{path: (added, deleted)}`` from ``git diff --numstat -z`` —
    NUL-split records of ``added\\tdeleted\\tpath`` (binary: ``-\\t-``)."""
    out = _git(
        root,
        "-c",
        "core.quotePath=false",
        "diff",
        "--numstat",
        "--no-renames",
        "-z",
        rng,
        env=env,
    )
    if out is None:
        return None
    stats: dict[str, tuple[int, int]] = {}
    for rec in out.split("\0"):
        a, sep1, rest = rec.partition("\t")
        d, sep2, path = rest.partition("\t")
        if not sep1 or not sep2 or not path.strip():
            continue
        stats[path] = (_int(a), _int(d))
    return stats


#: A ``git log --format=%H`` commit header in the NUL-split stream: a
#: token that is exactly a full sha (40 hex). A path token carries a
#: leading ``\n`` (the record separator) and never matches.
_SHA_TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")


def _file_commits(
    root: Path, rng: str, env: dict[str, str] | None = None
) -> dict[str, list[str]] | None:
    """``{path: [full sha, …]}`` — newest first — from
    ``git log --name-only -z``: tokens alternate a 40-hex commit header
    and the NUL-separated paths it touched."""
    out = _git(
        root,
        "log",
        "--no-merges",
        "--format=%H",
        "--name-only",
        "-z",
        rng,
        env=env,
    )
    if out is None:
        return None
    files: dict[str, list[str]] = {}
    current: str | None = None
    for tok in out.split("\0"):
        if _SHA_TOKEN_RE.fullmatch(tok):
            current = tok
            continue
        path = tok.strip()
        if current and path:
            files.setdefault(path, []).append(current)
    return files


def changed_files(
    root: Path, rng: str, env: dict[str, str] | None = None
) -> list[FileStat] | None:
    """Every path ``rng`` touches, diff stat and range commits attached;
    ``None`` when the range cannot be read."""
    if not _prepare_range(root, rng, env):
        return None
    stats = _numstat(root, rng, env)
    if stats is None:
        return None
    commits = _file_commits(root, rng, env) or {}
    return [
        FileStat(path, a, d, tuple(commits.get(path, ())))
        for path, (a, d) in stats.items()
    ]


# ── the changed-symbol view ────────────────────────────────────────


def _git_bytes(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> bytes | None:
    """``_git`` for a blob read: ``git show`` as bytes for the snapshot
    indexer — undecodable text is the indexer's parse error, not this
    helper's decode failure."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=60,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def _py_tree(
    root: Path, rev: str, env: dict[str, str] | None = None
) -> set[str] | None:
    """The ``*.py`` path set of ``rev`` (``ls-tree -r``): the snapshot
    indexer's package walk needs the whole side's tree, not just the
    changed paths."""
    out = _git(
        root,
        "-c",
        "core.quotePath=false",
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        rev,
        env=env,
    )
    if out is None:
        return None
    return {p for p in out.split("\0") if p.endswith(".py")}


def changed_symbols(
    root: Path,
    rng: str,
    paths: Iterable[str],
    env: dict[str, str] | None = None,
) -> dict[str, tuple[_impact.ChangedSymbol, ...]] | None:
    """``{path: (ChangedSymbol, …)}`` — the changed symbols of ``rng``'s
    ``*.py`` paths, the python kind's own answer: both ends indexed as
    snapshots (``git show`` blobs through
    ``_python_impact._index_side``) and compared by
    ``_python_impact._diff_modules`` — the engine ``view='impact'``
    renders. Line-shifted symbols (an edit above moved their lines;
    nothing about the symbol changed) drop out like the impact view
    drops them from fan-out. ``None`` when a rev or the index cannot be
    read — the table ships without the column, never down."""
    if ".." not in rng:
        return None
    base, _, head = rng.partition("..")
    py = sorted({p for p in paths if p.endswith(".py")})
    if not py:
        return {}
    try:
        # Lazy like templates' handler imports: keeps a worker module
        # that loads red_suspects from paying the handlers' import cost.
        from precis.handlers import _python_impact as _impact

        base_tree = _py_tree(root, base, env)
        head_tree = _py_tree(root, head, env)
        if base_tree is None or head_tree is None:
            return None
        base_files = {
            rel: (
                _git_bytes(root, "show", f"{base}:{rel}", env=env)
                if rel in base_tree
                else None
            )
            for rel in py
        }
        head_files = {
            rel: (
                _git_bytes(root, "show", f"{head}:{rel}", env=env)
                if rel in head_tree
                else None
            )
            for rel in py
        }
        base_mods = _impact._index_side({}, base_files, base_tree)
        head_mods = _impact._index_side({}, head_files, head_tree)
        changes = _impact._diff_modules(base_mods, head_mods)
    except Exception:
        log.warning("red_suspects: symbol diff failed", exc_info=True)
        return None
    out: dict[str, list[_impact.ChangedSymbol]] = {}
    for c in changes:
        if c.change == "shifted":
            continue
        out.setdefault(c.file, []).append(c)
    return {p: tuple(cs) for p, cs in out.items()}


# ── the last green build ───────────────────────────────────────────


def last_green_sha(store: Any) -> str | None:
    """The newest non-waived green ``release.ci_verdicts`` sha — the last
    build CI actually passed. ``None`` when the ledger is empty or
    unreadable."""
    try:
        rows = _gates.read_ci_verdicts(store)
    except Exception:
        return None
    best: tuple[str, int] | None = None
    sha: str | None = None
    for i, row in enumerate(rows):
        if row.get("verdict") != "green" or row.get("waived"):
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, sha = key, str(row.get("sha") or "")
    return sha or None


def suspect_range(store: Any, sha: str, rng: str) -> str | None:
    """``last-green..this-build``; the cut's own ``prev_prod..head``
    range when the ledger names no green sha; ``None`` when neither is
    two-sided."""
    head = sha.strip() or (rng.rsplit("..", 1)[-1].strip() if ".." in rng else "")
    green = last_green_sha(store)
    if green and head:
        return f"{green}..{head}"
    return rng if ".." in rng else None


# ── the commit → ledger map ────────────────────────────────────────

#: ``resolved: <sha>`` (a gripe's fix landed) and ``landed: <sha>`` (a
#: slice todo's land) — the mapping the gripe asks for.
_LEDGER_RE = re.compile(
    r"^\s*(?:resolved|landed)\s*:\s*([0-9a-f]{7,40})\b", re.IGNORECASE
)
_LEDGER_SQL_RE = r"^[[:space:]]*(resolved|landed):[[:space:]]*[0-9a-f]"

_LEDGER_SQL = """
    SELECT r.kind, c.ref_id, c.text
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
     WHERE r.retired_at IS NULL
       AND r.kind IN ('gripe', 'todo')
       AND c.chunk_kind IN ('gripe_comment', 'todo_comment')
       AND c.text ~* %s
"""


def _ledger_entries(store: Any) -> list[tuple[str, int, str]]:
    """``(ref kind, ref id, comment sha)`` of every ``resolved:`` /
    ``landed:`` comment — one scan per digest, narrowed to comment
    chunks of the two kinds that can carry the markers."""
    with store.pool.connection() as conn:
        rows = conn.execute(_LEDGER_SQL, (_LEDGER_SQL_RE,)).fetchall()
    out: list[tuple[str, int, str]] = []
    for kind, ref_id, text in rows:
        m = _LEDGER_RE.match(str(text))
        if m:
            out.append((str(kind), int(ref_id), m.group(1).lower()))
    return out


def _todo_gripe(store: Any, todo_ids: set[int]) -> dict[int, int]:
    """``{todo id: gripe id}`` via each todo's ``plan:<root>`` tag and the
    root's ``related-to`` gripe (``land_slice._plan_gripe``'s rule);
    todos with no plan gripe are absent."""
    if not todo_ids:
        return {}
    with store.pool.connection() as conn:
        tag_rows = conn.execute(
            """
            SELECT rt.ref_id, t.value FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s)
               AND t.namespace = 'OPEN' AND t.value LIKE %s
             ORDER BY rt.ref_id, t.value
            """,
            (sorted(todo_ids), "plan:%"),
        ).fetchall()
        roots: dict[int, int] = {}
        for tid, value in tag_rows:
            v = str(value)
            if v[5:].isdigit():
                roots.setdefault(int(tid), int(v[5:]))
        if not roots:
            return {}
        link_rows = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id
              FROM links l JOIN refs g ON g.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = ANY(%s) AND l.relation = 'related-to'
               AND g.kind = 'gripe' AND g.retired_at IS NULL
             ORDER BY l.dst_ref_id
            """,
            (sorted(set(roots.values())),),
        ).fetchall()
    by_root: dict[int, int] = {}
    for root_id, gripe_id in link_rows:
        by_root.setdefault(int(root_id), int(gripe_id))
    return {t: by_root[r] for t, r in roots.items() if r in by_root}


def _prefix_match(a: str, b: str) -> bool:
    """``gates._prefix_match``'s rule: one sha may abbreviate the other;
    the ledger's ``resolved:``/``landed:`` comments carry 9-char
    prefixes of the full shas the range walk names."""
    n = min(len(a), len(b))
    return n >= _SHA_MIN and a[:n] == b[:n]


def commit_handles(store: Any, shas: Iterable[str]) -> dict[str, list[str]]:
    """``{full sha: [gr<N>/td<M>, …]}`` for the range commits, from the
    ``resolved:``/``landed:`` ledgers. A todo whose plan has no gripe
    keeps its ``td<N>`` handle — a ledger pointer either way. Only the
    todos whose ``landed:`` sha matches a range commit are resolved to
    gripes."""
    commits = sorted(shas)
    out: dict[str, set[str]] = {}
    pending: dict[int, set[str]] = {}  # todo id → the range shas it landed
    for kind, rid, csha in _ledger_entries(store):
        for sha in commits:
            if not _prefix_match(sha, csha):
                continue
            if kind == "gripe":
                out.setdefault(sha, set()).add(f"gr{rid}")
            else:
                pending.setdefault(rid, set()).add(sha)
    gripe_of = _todo_gripe(store, set(pending))
    for tid, landed in pending.items():
        handle = f"gr{gripe_of[tid]}" if tid in gripe_of else f"td{tid}"
        for sha in landed:
            out.setdefault(sha, set()).add(handle)
    return {s: sorted(h) for s, h in out.items()}


# ── the failure path ───────────────────────────────────────────────

#: A traceback frame in the digest's log text: ``File "….py"``.
_TRACE_FILE_RE = re.compile(r'File "([^"]+?\.py)"')

#: The ``in <name>`` of a traceback frame — the function the frame ran.
#: A changed symbol of the same leaf name flags its file even when the
#: frame's path maps outside the repo (an installed site-packages copy
#: names no tree path). ``in <module>`` never matches — ``<`` is not
#: a word char.
_TRACE_FUNC_RE = re.compile(r'File "[^"]+?\.py", line \d+, in (\w+)')

#: Repo-rooted path prefixes a traceback may carry (absolute runner
#: paths name them mid-path).
_REPO_MARKERS = ("src/", "tests/", "scripts/")


def _repo_path(raw: str) -> str | None:
    """A ``File "…"`` traceback path as repo-relative, else ``None`` —
    site-packages and interpreter frames don't map onto the tree."""
    p = raw.strip()
    for marker in _REPO_MARKERS:
        if p.startswith(marker):
            return p
        i = p.find("/" + marker)
        if i != -1:
            return p[i + 1 :]
    return None


def failure_paths(
    root: Path, end: str, test_ids: Iterable[str], log_text: str
) -> set[str]:
    """The files the red run blames: each failing test's own file, the
    src modules it imports at ``end`` (``red_waiver.culprit_commits``'s
    chain — the same "import chain" the culprit pick reads), and every
    repo path a ``File "…"`` traceback line of the digest names."""
    out: set[str] = set()
    for tf in dict.fromkeys(_waiver.file_of_test(t) for t in test_ids):
        if not tf:
            continue
        out.add(tf)
        src = _git(root, "show", f"{end}:{tf}")
        if src:
            out.update(_waiver.imported_src_paths(src))
    for m in _TRACE_FILE_RE.finditer(log_text):
        p = _repo_path(m.group(1))
        if p:
            out.add(p)
    return out


# ── rank and render ────────────────────────────────────────────────


def rank_rows(
    files: Iterable[FileStat],
    on_path: set[str],
    handles: dict[str, list[str]],
    symbols: dict[str, tuple[_impact.ChangedSymbol, ...]] | None = None,
) -> list[SuspectRow]:
    """The gripe's ordering: on the failure path, then commits touching
    it, then lines changed; the path tie-breaks for a stable table.
    ``symbols`` — :func:`changed_symbols`'s map — rides each row for the
    changed-symbol view the block renders under the table."""
    syms = symbols or {}
    rows = [
        SuspectRow(
            f.path,
            f.added,
            f.deleted,
            f.commits,
            tuple(sorted({h for c in f.commits for h in handles.get(c, ())})),
            f.path in on_path,
            syms.get(f.path, ()),
        )
        for f in files
    ]
    return sorted(
        rows,
        key=lambda r: (not r.on_path, -len(r.commits), -(r.added + r.deleted), r.path),
    )


#: Render mark per change kind — the impact view's glyph set, restated:
#: this module renders rows, it does not import the renderer.
_CHANGE_MARK = {
    "added": "+",
    "removed": "-",
    "signature": "~",
    "body": "~",
    "moved": "~",
    "shifted": "~",
    "seed": "·",
}


def _row_line(r: SuspectRow) -> str:
    flag = "*" if r.on_path else " "
    n = len(r.commits)
    gripes = f" — {', '.join(r.gripes)}" if r.gripes else ""
    return (
        f" {flag} {r.path} — {n} commit{'s' if n != 1 else ''} "
        f"(+{r.added}/-{r.deleted}){gripes}"
    )


def _sym_line(r: SuspectRow, c: _impact.ChangedSymbol) -> str:
    detail = f" — {c.detail}" if c.detail else ""
    return (
        f"  {_CHANGE_MARK.get(c.change, '?')} {c.qualname}  "
        f"{c.change}  {r.path}{detail}"
    )


def _fit(lines: list[str], budget: int | None, kept: int) -> str:
    """Join ``lines``; over ``budget`` chars, cut the tail past ``kept``
    to a count line. The first ``kept`` lines — the header, every file
    row and the fan-out pointer — always ship: the table is the fixer's
    only copy of it, so no budget may drop a file row (td655162); only
    the changed-symbol tail gives room."""
    text = "\n".join(lines)
    if budget is None or len(text) <= budget:
        return text
    out = lines[:kept]
    used = sum(len(line) + 1 for line in out)
    for i, line in enumerate(lines[kept:], start=kept):
        if used + len(line) + 1 > max(0, budget - 60):
            out.append(f"… {len(lines) - i} more lines cut (brief room)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def render_block(rows: list[SuspectRow], rng: str, budget: int | None = None) -> str:
    """The brief section: a header naming the range and ordering, then
    one line per changed file in rank order — ``* path — N commits
    (+a/-d) — grA, tdB`` — every file with its full detail, then the
    fan-out pointer to the python kind's ``impact`` view and the
    range's changed symbols (:attr:`SuspectRow.symbols`, detailed for
    the first :data:`_SYM_CAP`, the overflow's qualnames still listed).
    ``budget`` (chars) bounds the block but can only trim the symbol
    tail: the header, every file row and the pointer always render —
    the brief is the fixer's only copy of the complete table."""
    lines = [
        f"changed files in {rng} (ranked by suspicion — * is on the failure path):"
    ]
    lines.extend(_row_line(r) for r in rows)
    base, _, head = rng.partition("..")
    if base and head:
        lines.append("")
        lines.append(
            "symbol fan-out (callers/importers/tests of the range's "
            "changed symbols): "
            f"get(kind='python', id='main', view='impact', "
            f"args={{'base': '{base}', 'head': '{head}'}})"
        )
    kept = len(lines)
    sym_rows = [(r, c) for r in rows for c in r.symbols]
    if sym_rows:
        lines.append("")
        lines.append(
            "changed symbols — the python kind's diff of the same range "
            "(+ added, - removed, ~ signature/body/moved):"
        )
        lines.extend(_sym_line(r, c) for r, c in sym_rows[:_SYM_CAP])
        if len(sym_rows) > _SYM_CAP:
            lines.append(f"    … +{len(sym_rows) - _SYM_CAP} more symbols:")
            lines.extend(f"    {c.qualname} ({r.path})" for r, c in sym_rows[_SYM_CAP:])
    return _fit(lines, budget, kept)


def suspects_block(
    store: Any,
    root: Path,
    *,
    sha: str,
    rng: str,
    log_text: str,
    rescued: dict[str, list[str]] | None = None,
    env: dict[str, str] | None = None,
    budget: int | None = None,
) -> str:
    """The "changed files …" section of the fixer's brief; ``""`` when
    the range or the repo cannot be read — a tableless brief is the old
    behaviour, never a failure. ``budget`` bounds only the
    changed-symbol tail (:func:`render_block`): every file row always
    renders — the brief is the fixer's only copy of the table."""
    rng2 = suspect_range(store, sha, rng)
    if rng2 is None:
        return ""
    files = changed_files(root, rng2, env)
    if files is None:
        return ""
    if not files:
        return f"(no changed files in {rng2})"
    end = rng2.rsplit("..", 1)[-1]
    failures, _problems = _waiver.parse_failures(log_text, rescued or {})
    on_path = failure_paths(root, end, failures, log_text)
    # A changed path the digest names at all — a FAILED id, an
    # annotation — is on the path even outside a File "…" frame.
    on_path.update(f.path for f in files if f.path in log_text)
    # … or symbol appears: a traceback's ``in <name>`` frame or a
    # failing id's ``::name`` parts matching a changed symbol flags its
    # file — the frame's path may sit outside the repo (an installed
    # site-packages copy names no tree path).
    symbols = changed_symbols(root, rng2, (f.path for f in files), env) or {}
    traced = set(_TRACE_FUNC_RE.findall(log_text))
    for tid in failures:
        for part in tid.split("::")[1:]:
            traced.update(p for p in part.split(".") if p)
    if traced:
        on_path.update(
            p
            for p, cs in symbols.items()
            if {c.qualname.rsplit(".", 1)[-1] for c in cs} & traced
        )
    try:
        handles = commit_handles(store, {c for f in files for c in f.commits})
    except Exception:
        log.warning("red_suspects: ledger read failed — no gripe column", exc_info=True)
        handles = {}
    return render_block(rank_rows(files, on_path, handles, symbols), rng2, budget)


__all__ = [
    "FileStat",
    "SuspectRow",
    "changed_files",
    "changed_symbols",
    "commit_handles",
    "failure_paths",
    "last_green_sha",
    "rank_rows",
    "render_block",
    "suspect_range",
    "suspects_block",
]
