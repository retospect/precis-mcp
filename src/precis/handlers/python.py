"""PythonHandler — slug-addressed Python codebase navigator.

Read-only in slice 4 (slice 6 turns on `put`). Backed by an in-memory
`RepoCache` (no DB persistence — see `precis.python_index` for the
rationale).

Address grammar accepted by `get`:

    None                           → list registered roots
    '/'                            → list registered roots
    <alias>                        → repo overview
    <alias>/<rel/path/to/file.py>  → file outline (default)
    <alias>/<file>~La-Lb           → line range (Track A; L-prefixed)
    <alias>/<file>~Sym             → local symbol selector (Track B)
    <alias>/<file>~Class.method    → local symbol selector (Track B)
    <alias>::<dotted.qualname>     → symbol drill-down (cross-ref)

Views: ``toc`` (repo-level), ``outline`` (file/symbol; default),
``source`` (file or symbol body).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers import _python_callgraph as cgraph
from precis.handlers import _python_entries as entries_mod
from precis.handlers import _python_render as render
from precis.handlers import _python_runtrace as rtrace
from precis.handlers import _python_write as write
from precis.protocol import Handler, KindSpec
from precis.python_index import ModuleIndex, RepoCache, RepoIndex, Symbol
from precis.python_index.indexer import _qualname_for_file
from precis.response import Response
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    classify_diff_hunks,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

log = logging.getLogger(__name__)


_SUPPORTED_VIEWS = ("toc", "outline", "source", "callgraph", "entries", "runtrace")
_RUNTRACE_GATE_ENV = "PRECIS_PYTHON_ALLOW_EXEC"
# After the seven-verb cutover, ``put`` on a file kind is creation-only.
# Region edits live on ``edit`` (mode='find-replace'|'append'|'insert'|
# 'replace'); selector-deletes live on ``delete``. The three gates
# (parse / qualname-drop / ruff) ride on whichever verb is invoked.
_SUPPORTED_PUT_MODES = ("create",)


# ---------------------------------------------------------------------------
# Env var parsing
# ---------------------------------------------------------------------------


def parse_python_roots(raw: str | None) -> dict[str, Path]:
    """Parse a ``PRECIS_PYTHON_ROOTS`` value into ``{alias: abs_path}``.

    Format: ``alias1:/abs/path1,alias2:/abs/path2``. Whitespace around
    each component is stripped. Entries with the following problems are
    skipped with a warning, and the rest of the entries are kept:

    - missing ``:`` separator
    - empty alias or empty path
    - non-existent or non-directory path
    - duplicate alias (first wins)

    A None or empty string yields ``{}``. The returned paths are
    resolved absolute paths (``~`` expanded). The handler validates
    these again at construction time, so a transient race between
    parse and construct still produces a clean error.
    """
    if not raw:
        return {}

    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            log.warning(
                "PRECIS_PYTHON_ROOTS: skipping %r — missing ':' separator", entry
            )
            continue
        alias, _, path_str = entry.partition(":")
        alias = alias.strip()
        path_str = path_str.strip()
        if not alias or not path_str:
            log.warning("PRECIS_PYTHON_ROOTS: skipping %r — empty alias or path", entry)
            continue
        if alias in out:
            log.warning("PRECIS_PYTHON_ROOTS: duplicate alias %r — first wins", alias)
            continue
        path = Path(path_str).expanduser().resolve()
        if not path.is_dir():
            log.warning(
                "PRECIS_PYTHON_ROOTS: skipping %r — not a directory: %s",
                alias,
                path,
            )
            continue
        out[alias] = path

    return out


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedId:
    """Components of an address string accepted by `get`.

    Exactly one of `file`, `qualname` is non-None (or both are None for
    a bare alias). `start_line` / `end_line` hold the optional Track A
    line range; `block_selector` holds the Track B selector text (e.g.
    a method or `Class.method`). At most one of (line range, block
    selector) is set.
    """

    alias: str
    file: str | None = None
    qualname: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    block_selector: str | None = None


_LINE_RANGE_RE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")


def _parse_id(raw: str) -> _ParsedId:
    """Parse an address string. Raises BadInput on syntactic problems."""
    if not raw:
        raise BadInput("empty id", next="get(kind='python')")

    # Split off the trailing `~SELECTOR` if present. We keep splitting
    # before `::` and `/` because those separators only matter on the
    # alias-side base.
    base, sep, selector = raw.partition("~")
    if not sep:
        selector = None  # type: ignore[assignment]

    # Symbol address: `<alias>::<qualname>`. Selectors not allowed here
    # in v1 — symbols already have a fully-qualified handle.
    if "::" in base:
        alias, qn = base.split("::", 1)
        if not alias or not qn:
            raise BadInput(
                f"malformed id {raw!r}: expected '<alias>::<qualname>'",
                next="get(kind='python', id='myrepo::pkg.mod.Symbol')",
            )
        if selector is not None:
            raise BadInput(
                f"selector ~{selector!r} not supported on symbol id; "
                f"address sub-symbols by their full qualname instead",
                next=f"get(kind='python', id='{alias}::{qn}.{selector}')",
            )
        return _ParsedId(alias=alias, qualname=qn)

    # File or alias address.
    if "/" in base:
        alias, file = base.split("/", 1)
        if not alias:
            raise BadInput(
                f"malformed id {raw!r}: missing alias before '/'",
                next="get(kind='python') to list known aliases",
            )
        # `<alias>/` (trailing slash, no file) — treat as alias.
        if not file:
            return _ParsedId(alias=alias)
    else:
        alias, file = base, None

    parsed_start: int | None = None
    parsed_end: int | None = None
    block_sel: str | None = None
    if selector is not None:
        m = _LINE_RANGE_RE.match(selector)
        if m:
            parsed_start = int(m.group(1))
            parsed_end = int(m.group(2)) if m.group(2) else parsed_start
            if parsed_end < parsed_start:
                raise BadInput(
                    f"line range {selector!r} has end < start",
                    next=f"get(kind='python', id='{alias}/{file}~L{parsed_end}-L{parsed_start}')",
                )
        else:
            # Track B local symbol selector — interpreted by the handler
            # against the file's symbol table.
            block_sel = selector

    return _ParsedId(
        alias=alias,
        file=file,
        start_line=parsed_start,
        end_line=parsed_end,
        block_selector=block_sel,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class PythonHandler(Handler):
    """Read-only navigator for one or more Python repos.

    Constructed with a dict of `alias → absolute root path` (typically
    parsed from `PRECIS_PYTHON_ROOTS` by the registry). The cache is an
    optional injection seam for tests; production code lets the handler
    own a fresh `RepoCache`.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="python",
        title="Python code navigator",
        description=(
            "Navigate one or more Python repos: outlines, source slices, "
            "symbol drill-down, lexical search. AST-indexed in-memory "
            "with mtime-based cache invalidation."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
        modes=_SUPPORTED_PUT_MODES,
    )

    def __init__(
        self,
        *,
        hub: Hub,
        roots: dict[str, Path],
        cache: RepoCache | None = None,
    ) -> None:
        # ``hub`` is taken for signature uniformity; python is an
        # in-memory kind (no DB, no embedder), so we don't actually
        # read anything from it. ``Handler._register_with`` stashes
        # the hub on ``self.hub`` for any future expansion.
        _ = hub
        if not isinstance(roots, dict):
            raise TypeError("roots must be a dict[str, Path]")
        resolved: dict[str, Path] = {}
        for alias, path in roots.items():
            if not alias or "/" in alias or "::" in alias or "~" in alias:
                raise ValueError(
                    f"invalid python repo alias {alias!r}: "
                    f"must be non-empty and must not contain '/', '::', or '~'"
                )
            p = Path(path).resolve()
            if not p.is_dir():
                raise ValueError(f"python repo {alias!r} root is not a directory: {p}")
            resolved[alias] = p
        self.roots = resolved
        self.cache = cache or RepoCache()

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        entry: str | None = None,
        depth: int = 3,
        cross_repo: bool = False,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 10,
        max_events: int = 2_000,
        expand_stdlib: bool = False,
        **_kw: Any,
    ) -> Response:
        # Index — no id, or "/" sentinel.
        if id is None or id == "/" or id == "":
            return Response(body=render.render_index(self.roots))

        parsed = _parse_id(str(id))
        root = self._resolve_alias(parsed.alias)
        idx = self.cache.get(root)

        # Validate view eagerly — gives a sharp error with options.
        if view is not None and view not in _SUPPORTED_VIEWS:
            raise Unsupported(
                f"unknown python view {view!r}",
                options=list(_SUPPORTED_VIEWS),
                next=f"get(kind='python', id={id!r}, view='outline')",
            )

        # Callgraph view — alias-only id; entry= required.
        if view == "callgraph":
            return self._render_callgraph(
                parsed=parsed,
                idx=idx,
                entry=entry,
                depth=depth,
                cross_repo=cross_repo,
            )

        # Runtrace view — gated; spawns a subprocess.
        if view == "runtrace":
            return self._render_runtrace(
                parsed=parsed,
                idx=idx,
                root=root,
                entry=entry,
                argv=argv,
                env=env,
                timeout=timeout,
                cross_repo=cross_repo,
                max_events=max_events,
                expand_stdlib=expand_stdlib,
            )

        # Entries view — alias-only id; pyproject scripts + __main__ guards.
        if view == "entries":
            if (
                parsed.file is not None
                or parsed.qualname is not None
                or parsed.start_line is not None
                or parsed.block_selector is not None
            ):
                raise BadInput(
                    "view='entries' takes a bare alias id",
                    next=f"get(kind='python', id={parsed.alias!r}, view='entries')",
                )
            report = entries_mod.find_entries(idx)
            return Response(body=entries_mod.render_entries(parsed.alias, report))

        # Symbol address.
        if parsed.qualname is not None:
            return self._render_symbol(parsed.alias, parsed.qualname, idx, view)

        # File address.
        if parsed.file is not None:
            mod = idx.file(parsed.file)
            if mod is None:
                raise NotFound(
                    f"file {parsed.file!r} not found in repo {parsed.alias!r}",
                    next=f"get(kind='python', id={parsed.alias!r}, view='toc')",
                )
            return self._render_file(parsed, mod, idx, view)

        # Bare alias.
        if view == "toc":
            return Response(body=render.render_toc(parsed.alias, idx))
        if view == "source":
            raise BadInput(
                "view='source' requires a file or symbol id",
                next=f"get(kind='python', id={parsed.alias!r}, view='toc')",
            )
        return Response(body=render.render_repo_overview(parsed.alias, idx))

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        """Lexical search across symbols.

        Scores each symbol by where the query matched:
        qualname > signature > docstring. Returns up to `top_k` hits,
        deduped by qualname.

        `scope=` may be:
        - alias (`'myrepo'`) — restrict to one repo
        - alias::qualname-prefix (`'myrepo::pkg.mod'`) — restrict to a
          subtree of one repo
        - alias/path (`'myrepo/src/precis/registry.py'`) — restrict to
          one file
        """
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='python', q='your query')",
            )

        roots = self._roots_for_scope(scope)
        if not roots:
            raise NotFound(
                f"no python repo matches scope={scope!r}",
                next="search(kind='python', q='...') to search all repos",
            )

        needle = q.lower()
        scope_qn_prefix, scope_file = _split_scope(scope)

        hits: list[tuple[float, str, Symbol]] = []
        for alias, root in roots.items():
            idx = self.cache.get(root)
            for mod in idx.modules.values():
                if scope_file and mod.file != scope_file:
                    continue
                for sym in mod.symbols:
                    if scope_qn_prefix and not (
                        sym.qualname == scope_qn_prefix
                        or sym.qualname.startswith(scope_qn_prefix + ".")
                    ):
                        continue
                    score = _score_symbol(sym, needle)
                    if score > 0:
                        hits.append((score, alias, sym))

        if not hits:
            # Empty-search recovery (MCP critic round 2): a bare
            # "no matches" body leaves the caller without a next
            # step. Build a small `Next:` trailer that suggests
            # widening scope (when ``scope=`` narrowed it) and
            # browsing entry points (which frequently host the kind
            # of ``main`` the caller is actually hunting for).
            #
            # List the aliases we could suggest for entries browsing
            # — prefer the scoped alias if scope= pinned one, else
            # an arbitrary-but-deterministic pick. Only name one so
            # the trailer doesn't explode on multi-repo setups.
            first_alias = sorted(roots.keys())[0]
            hints: list[tuple[str, str]] = []
            if scope is not None:
                # Widen scope suggestion first — the most common fix
                # when a scoped search misses is that the scope was
                # too narrow (typo in alias or qualname prefix).
                hints.append(
                    (
                        f"search(kind='python', q={q!r})",
                        "widen to all repos (drop scope=)",
                    )
                )
            hints.append(
                (
                    f"get(kind='python', id={first_alias!r}, view='entries')",
                    "browse console scripts + __main__ guards",
                )
            )
            hints.append(
                (
                    f"get(kind='python', id={first_alias!r})",
                    "list files in this repo",
                )
            )
            body = f"no python symbols match {q!r}\n\n"
            body += render_next_section(hints)
            return Response(body=body)

        hits.sort(key=lambda h: -h[0])
        total = len(hits)
        hits = hits[:top_k]

        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="python hit",
                query=q,
            )
        ]
        for score, alias, sym in hits:
            handle = f"{alias}::{sym.qualname}"
            sig = sym.signature or sym.kind
            lines.append(
                f"\n## {handle}  (score={score:.2f}, {sym.file}:{sym.start_line})"
            )
            lines.append(f"  {sig}")
            if sym.docstring:
                lines.append(f"  {render._oneline(sym.docstring)}")
        return Response(body="\n".join(lines))

    # ── put: create a new file (creation-only) ─────────────────────

    _LEGACY_PUT_MODES_TO_EDIT: ClassVar[tuple[str, ...]] = (
        "append",
        "insert",
        "replace",
        "edit",
    )

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a new Python file in a configured repo.

        Per the seven-verb surface (D6), ``put`` on python is
        creation-only. Region edits (append / insert / replace /
        find-replace) live on the ``edit`` verb; symbol / file
        deletes live on ``delete``. Both go through the three
        gates (parse / qualname-drop / ruff).
        """
        if mode in self._LEGACY_PUT_MODES_TO_EDIT:
            new_mode = "find-replace" if mode == "edit" else mode
            raise BadInput(
                f"mode={mode!r} is not accepted on put for kind='python'",
                next=(
                    f"use edit(kind='python', id=..., mode={new_mode!r}, ...) "
                    "for region edits"
                ),
            )
        if mode == "delete":
            raise BadInput(
                "mode='delete' is not accepted on put for kind='python'",
                next="use delete(kind='python', id='r::pkg.mod.symbol') instead",
            )
        if mode != "create":
            raise BadInput(
                f"mode= is required and must be 'create' (got {mode!r})",
                options=["create"],
                next="put(kind='python', id='r/path/new.py', text='...', mode='create')",
            )
        if id is None:
            raise BadInput(
                "put requires id= (the file path)",
                next="put(kind='python', id='r/path/new.py', text='...', mode='create')",
            )
        parsed = _parse_id(str(id))
        root = self._resolve_alias(parsed.alias)
        return self._put_create(parsed, root, text)

    # ── seven-verb surface ─────────────────────────────────────────

    #: Modes accepted by :meth:`edit` — region-modifying ops only.
    #: Python supports the same set as markdown/plaintext minus
    #: ``create`` (lives on ``put``) and ``delete`` (its own verb).
    _EDIT_MODES: ClassVar[tuple[str, ...]] = (
        "find-replace",
        "append",
        "insert",
        "replace",
    )

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int,
        mode: str = "find-replace",
        text: str | None = None,
        find: str | None = None,
        before: str = "",
        after: str = "",
        where: str | None = None,
        match: str = "unique",
        nth: int | None = None,
        allow_rename: bool = False,
        dry_run: bool | str = False,
        **_kw: Any,
    ) -> Response:
        """Region-edit an existing python file or symbol.

        Routes through the same three gates (parse / qualname-drop /
        ruff) as the legacy ``put(mode=...)`` path. ``mode='find-
        replace'`` is the new default for the legacy ``put(mode='edit')``.
        """
        if mode not in self._EDIT_MODES:
            raise BadInput(
                f"unknown edit mode {mode!r}",
                options=list(self._EDIT_MODES),
                next=(
                    "edit(kind='python', id='r/file.py', mode='find-replace', "
                    "find='old', text='new')"
                ),
            )
        parsed = _parse_id(str(id))
        root = self._resolve_alias(parsed.alias)
        if mode == "append":
            return self._put_append(parsed, root, text)
        if mode == "replace":
            return self._put_replace(parsed, root, text, allow_rename=allow_rename)
        op_kind = "edit" if mode == "find-replace" else mode
        return self._put_anchored(
            parsed=parsed,
            root=root,
            op_kind=op_kind,
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
            allow_rename=allow_rename,
            dry_run=dry_run,
        )

    def delete(  # type: ignore[override]
        self,
        *,
        id: str | int,
        allow_rename: bool = False,
        **_kw: Any,
    ) -> Response:
        """Delete a file, symbol, or line range from a python repo.

        Same gating as ``put(mode='delete')`` — the qualname-drop
        check is skipped (delete intentionally drops symbols), and
        ruff still runs against the remaining file content.
        """
        parsed = _parse_id(str(id))
        root = self._resolve_alias(parsed.alias)
        return self._put_delete(parsed, root, allow_rename=allow_rename)

    # ── put dispatch ───────────────────────────────────────────────

    def _put_create(self, parsed: _ParsedId, root: Path, text: str | None) -> Response:
        if parsed.qualname is not None:
            raise BadInput(
                "create requires a file path id, not a qualname",
                next=f"put(kind='python', id='{parsed.alias}/path/to/new.py', "
                f"text='...', mode='create')",
            )
        if parsed.file is None:
            raise BadInput(
                "create requires a file path",
                next="put(kind='python', id='r/path/to/new.py', text='...', mode='create')",
            )
        if parsed.start_line is not None or parsed.block_selector is not None:
            raise BadInput(
                "create does not accept a selector",
                next=f"put(kind='python', id='{parsed.alias}/{parsed.file}', "
                f"text='...', mode='create')",
            )
        if text is None:
            raise BadInput("create requires text=", next="add text='...'")

        _reject_doubled_root_prefix(parsed, root)

        path = (root / parsed.file).resolve()
        # Refuse to escape the repo root via ../ tricks.
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BadInput(
                f"path {parsed.file!r} escapes repo root",
                next="use a relative path that stays inside the repo",
            ) from exc
        if path.exists():
            raise BadInput(
                f"file {parsed.file!r} already exists",
                next=f"put(kind='python', id='{parsed.alias}/{parsed.file}', "
                f"text='...', mode='replace')",
            )

        return self._finalize_write(
            parsed=parsed,
            path=path,
            new_content=_ensure_trailing_newline(text),
            pre_in_region=set(),  # new file → nothing to drop
            allow_rename=True,  # creating from scratch can't drop anything anyway
            change_summary=f"Created {parsed.alias}/{parsed.file}",
        )

    def _put_append(self, parsed: _ParsedId, root: Path, text: str | None) -> Response:
        if parsed.qualname is not None or parsed.file is None:
            raise BadInput(
                "append requires a file path id (no qualname, no selector)",
                next="put(kind='python', id='r/path/to/file.py', "
                "text='...', mode='append')",
            )
        if parsed.start_line is not None or parsed.block_selector is not None:
            raise BadInput(
                "append does not accept a selector",
                next=f"put(kind='python', id='{parsed.alias}/{parsed.file}', "
                f"text='...', mode='append')",
            )
        if text is None or not text.strip():
            raise BadInput("append requires text=", next="add text='...'")

        path, mod = self._require_existing_file(parsed, root)
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") else "\n"
        appended = existing + sep + text
        appended = _ensure_trailing_newline(appended)

        return self._finalize_write(
            parsed=parsed,
            path=path,
            new_content=appended,
            pre_in_region=set(),  # append never touches existing region
            allow_rename=True,
            change_summary=f"Appended {len(text.splitlines())} lines "
            f"to {parsed.alias}/{parsed.file}",
        )

    def _put_replace(
        self,
        parsed: _ParsedId,
        root: Path,
        text: str | None,
        *,
        allow_rename: bool,
    ) -> Response:
        if text is None:
            raise BadInput("replace requires text=", next="add text='...'")

        path, mod, region = self._resolve_replace_region(parsed, root)
        existing = path.read_text(encoding="utf-8")

        if region is None:
            # Whole-file replace.
            new_content = _ensure_trailing_newline(text)
            pre_in_region = {s.qualname for s in mod.symbols if s.kind != "module"}
        else:
            start, end = region
            new_content = write.splice_lines(
                existing, start_line=start, end_line=end, replacement=text
            )
            pre_in_region = {
                s.qualname
                for s in mod.symbols
                if s.kind != "module" and start <= s.start_line <= end
            }

        return self._finalize_write(
            parsed=parsed,
            path=path,
            new_content=new_content,
            pre_in_region=pre_in_region,
            allow_rename=allow_rename,
            change_summary=_replace_summary(parsed, region),
        )

    def _put_delete(
        self,
        parsed: _ParsedId,
        root: Path,
        *,
        allow_rename: bool,
    ) -> Response:
        path, mod, region = self._resolve_replace_region(parsed, root)
        if region is None:
            raise BadInput(
                "delete requires a selector or qualname (cannot delete a whole file)",
                next=f"put(kind='python', id='{parsed.alias}/{parsed.file}~Symbol', "
                f"mode='delete')",
            )
        existing = path.read_text(encoding="utf-8")
        start, end = region
        new_content = write.splice_lines(
            existing, start_line=start, end_line=end, replacement=""
        )
        # mode='delete' is an *intentional* drop; the user has already
        # said "remove this region". Skip gate 2 by treating the
        # region's pre-set as empty for the diff.
        return self._finalize_write(
            parsed=parsed,
            path=path,
            new_content=new_content,
            pre_in_region=set(),
            allow_rename=True,
            change_summary=f"Deleted lines {start}-{end} of "
            f"{parsed.alias}/{parsed.file or mod.file}",
        )

    def _put_anchored(
        self,
        *,
        parsed: _ParsedId,
        root: Path,
        op_kind: str,  # "edit" or "insert"
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        allow_rename: bool,
        dry_run: bool | str = False,
    ) -> Response:
        """Anchored find/replace (mode='edit') or insert (mode='insert').

        Region is the whole file when no selector is given, or the
        addressed symbol/line range when ``id`` carries one. The
        spliced buffer goes through the same three gates as
        ``mode='replace'``: ``ast.parse``, qualname-drop, ruff.
        """
        # See plaintext._put_anchored — ``op_kind='edit'`` is the
        # internal alias for the wire-level ``mode='find-replace'``;
        # error strings echo the user's name, not the alias.
        user_mode = "find-replace" if op_kind == "edit" else op_kind
        if find is None or not find:
            raise BadInput(
                f"mode={user_mode!r} requires find= (the exact text to locate)",
                next=(
                    f"edit(kind='python', id={parsed.alias}/{parsed.file or '...'}, "
                    f"mode={user_mode!r}, find='exact text', text='replacement')"
                ),
            )
        if text is None:
            raise BadInput(
                f"mode={user_mode!r} requires text= ('' is allowed for delete-by-edit)",
                next="add text='...' to the call",
            )

        # Resolve the search region. Same logic as replace/delete:
        # whole file when no selector, otherwise the symbol's line range.
        path, mod, region = self._resolve_replace_region(parsed, root)
        full = path.read_text(encoding="utf-8")

        if region is None:
            # Whole-file search.
            region_text = full
            base_line = 1
            region_start = 1
            region_end = len(full.splitlines()) or 1
            region_label = f"{parsed.alias}/{parsed.file}"
        else:
            region_start, region_end = region
            base_line = region_start
            # Extract the region's lines (1-indexed inclusive).
            file_lines = full.splitlines(keepends=True)
            region_text = "".join(file_lines[region_start - 1 : region_end])
            sel_token = (
                parsed.qualname
                or parsed.block_selector
                or (
                    f"L{parsed.start_line}-{parsed.end_line}"
                    if parsed.start_line is not None
                    else f"L{region_start}-{region_end}"
                )
            )
            region_label = f"{parsed.alias}/{parsed.file or mod.file}~{sel_token}"

        op = EditOp(
            op="edit" if op_kind == "edit" else "insert",
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,  # type: ignore[arg-type]
            match=match,  # type: ignore[arg-type]
            nth=nth,
            region_label=region_label,
            base_line=base_line,
        )
        result = apply_edit(region_text, op)

        # Splice the post-edit region back into the full file.
        if region is None:
            new_content = result.new_buffer
        else:
            new_content = write.splice_lines(
                full,
                start_line=region_start,
                end_line=region_end,
                replacement=result.new_buffer,
            )

        # Compute pre_in_region for the qualname-drop gate. Symbols
        # whose source lines overlap any edited span are at risk —
        # if they don't appear in the post-buffer, the gate fires.
        pre_in_region: set[str] = set()
        if op_kind == "edit":
            for sym in mod.symbols:
                if sym.kind == "module":
                    continue
                for span_start, span_end in result.edited_spans:
                    if not (sym.end_line < span_start or sym.start_line > span_end):
                        pre_in_region.add(sym.qualname)
                        break
        # mode='insert' adds bytes adjacent to existing symbols but
        # cannot drop them (the matched anchor is preserved). Empty
        # pre-set is correct.

        n_spans = len(result.edited_spans)
        verb = "edited" if op_kind == "edit" else "inserted"
        span_str = ", ".join(
            f"L{a}-{b}" if a != b else f"L{a}" for a, b in result.edited_spans
        )
        change_summary = (
            f"{verb} {n_spans} span{'s' if n_spans != 1 else ''} "
            f"in {region_label} ({span_str})"
        )

        return self._finalize_write(
            parsed=parsed,
            path=path,
            new_content=new_content,
            pre_in_region=pre_in_region,
            allow_rename=allow_rename,
            change_summary=change_summary,
            dry_run=dry_run,
            edited_spans=result.edited_spans,
            region_label=region_label,
            match_policy=op.match,
        )

    # ── put helpers ────────────────────────────────────────────────

    def _require_existing_file(
        self, parsed: _ParsedId, root: Path
    ) -> tuple[Path, ModuleIndex]:
        """Resolve `parsed` to (existing path, indexed module). Raises
        NotFound if either is missing."""
        idx = self.cache.get(root)
        if parsed.file is None:
            raise BadInput("missing file path in id")
        mod = idx.file(parsed.file)
        if mod is None:
            raise NotFound(
                f"file {parsed.file!r} not found in repo {parsed.alias!r}",
                next=f"get(kind='python', id={parsed.alias!r}, view='toc')",
            )
        return root / parsed.file, mod

    def _resolve_replace_region(
        self, parsed: _ParsedId, root: Path
    ) -> tuple[Path, ModuleIndex, tuple[int, int] | None]:
        """Map a replace/delete address to (path, module, region).

        `region` is None for whole-file replace; otherwise an
        inclusive 1-indexed (start, end) line range. Raises NotFound
        if the symbol or file is missing.
        """
        idx = self.cache.get(root)

        # Qualname address.
        if parsed.qualname is not None:
            sym = idx.symbol(parsed.qualname)
            if sym is None:
                raise NotFound(
                    f"symbol {parsed.qualname!r} not found in repo {parsed.alias!r}",
                    next=f"search(kind='python', q='{parsed.qualname.split('.')[-1]}')",
                )
            mod = idx.file(sym.file)
            assert mod is not None
            return root / sym.file, mod, (sym.start_line, sym.end_line)

        # File address (with optional selector).
        if parsed.file is None:
            raise BadInput("replace/delete requires a file path or qualname")
        mod = idx.file(parsed.file)
        if mod is None:
            raise NotFound(
                f"file {parsed.file!r} not found in repo {parsed.alias!r}",
                next=f"get(kind='python', id={parsed.alias!r}, view='toc')",
            )

        if parsed.start_line is not None:
            return (
                root / parsed.file,
                mod,
                (parsed.start_line, parsed.end_line or parsed.start_line),
            )
        if parsed.block_selector is not None:
            sym = _resolve_block_selector(mod, parsed.block_selector)
            if sym is None:
                raise NotFound(
                    f"no symbol {parsed.block_selector!r} in {parsed.file}",
                    next=f"get(kind='python', id='{parsed.alias}/{parsed.file}', "
                    f"view='outline')",
                )
            return root / parsed.file, mod, (sym.start_line, sym.end_line)

        # No selector → whole-file replace.
        return root / parsed.file, mod, None

    def _finalize_write(
        self,
        *,
        parsed: _ParsedId,
        path: Path,
        new_content: str,
        pre_in_region: set[str],
        allow_rename: bool,
        change_summary: str,
        dry_run: bool | str = False,
        edited_spans: tuple[tuple[int, int], ...] = (),
        region_label: str = "",
        match_policy: str = "unique",
    ) -> Response:
        """Run gates 1-3, write atomically, refresh cache, render the response.

        On any *blocking* gate failure (1 or 2), this raises ``BadInput``
        before any disk write. Gate 3 (ruff) never blocks; ruff failures
        proceed with the unfixed buffer and a warning surfaced in the
        response.

        When ``dry_run`` is set (``True`` / ``"diff"`` / ``"full"``), the
        same gates run on the post-edit buffer (so the agent sees would-it-
        validate?) but the atomic write is skipped and the response is the
        proposed diff or post-edit region instead. Disk and cache are
        untouched. Only ``mode='edit'`` / ``mode='insert'`` callers pass
        meaningful ``edited_spans`` / ``region_label``; older modes leave
        them as defaults and get the standard write response.
        """
        dry_mode = normalize_dry_run(dry_run)

        # Gate 1 — AST parse.
        ast_gate = write.gate_ast(new_content, filename=str(path))
        if not ast_gate.ok:
            raise BadInput(
                f"ast.parse failed on the post-edit buffer: {ast_gate.detail}",
                next="check the indentation / syntax of the replacement text",
            )

        # Compute the pre-edit module qualname for gate 2's qualname extraction.
        # If this is a brand-new file we don't have a ModuleIndex; derive the
        # qualname from the file path.
        module_qn = _module_qualname_for(path, repo_root=self.roots[parsed.alias])
        post_qns = write.qualnames_in_text(new_content, module_qualname=module_qn)
        qn_gate = write.gate_qualnames(
            pre_in_region=pre_in_region,
            post_in_file=post_qns,
            allow_rename=allow_rename,
        )
        if not qn_gate.ok:
            raise BadInput(
                f"qualname-drop gate failed: {qn_gate.detail}; "
                f"dropped: {sorted(qn_gate.dropped)!r}",
                next="add allow_rename=True if the rename or removal is intentional",
            )

        # Gate 3 — ruff (always runs, never blocks).
        canonical, ruff_changes = write.run_ruff(new_content, path)

        if dry_mode is not None:
            return self._render_dry_run(
                parsed=parsed,
                path=path,
                pre=path.read_text(encoding="utf-8") if path.exists() else "",
                post=canonical,
                edited_spans=edited_spans,
                region_label=region_label or change_summary,
                match_policy=match_policy,
                ast_gate=ast_gate,
                qn_gate=qn_gate,
                ruff_changes=ruff_changes,
                mode=dry_mode,
            )

        # Atomic write of the canonical buffer.
        write.atomic_write(path, canonical)

        # Force the cache to re-stat this file on the next get(); the
        # mtime change is what triggers reparse, so this is implicit.
        # No explicit drop needed — RepoCache.get() handles it.

        return Response(
            body=_render_put_response(
                parsed, ast_gate, qn_gate, ruff_changes, change_summary
            )
        )

    def _render_dry_run(
        self,
        *,
        parsed: _ParsedId,
        path: Path,
        pre: str,
        post: str,
        edited_spans: tuple[tuple[int, int], ...],
        region_label: str,
        match_policy: str,
        ast_gate: write.GateResult,
        qn_gate: write.GateResult,
        ruff_changes: write.RuffChanges,
        mode: str,  # 'diff' or 'full'
    ) -> Response:
        """Render a dry-run response for python: header + (diff | full).

        The header surfaces gate-by-gate results so the agent knows
        whether the proposed edit would pass validation (gates 1+2) and
        whether ruff made any incidental changes (a hunk count outside
        ``edited_spans``).
        """
        within, outside = classify_diff_hunks(pre, post, edited_spans)
        ruff_note = (
            f"would format ({within} hunk{'s' if within != 1 else ''} on edited spans, "
            f"{outside} unrelated)"
            if ruff_changes.ok and ruff_changes.changed
            else (
                "no changes"
                if ruff_changes.ok
                else f"unavailable ({ruff_changes.error or 'unknown'})"
            )
        )
        extras = [
            ("ast.parse:", "ok" if ast_gate.ok else f"FAILED — {ast_gate.detail}"),
            (
                "qualname-drop:",
                "ok" if qn_gate.ok else f"FAILED — dropped {qn_gate.dropped!r}",
            ),
            ("ruff:", ruff_note),
        ]
        label = region_label or f"{parsed.alias}/{parsed.file or '?'}"
        header = render_dry_run_header(
            region_label=label,
            edited_spans=edited_spans,
            match_policy=match_policy,
            extras=extras,
        )
        if mode == "full":
            body = render_dry_run_full(
                post,
                edited_spans=edited_spans,
                region_label=label,
            )
        else:
            file_label = f"{parsed.alias}/{parsed.file or path.name}"
            diff = format_unified_diff(pre, post, file_label=file_label).rstrip("\n")
            body = diff or "(no diff — pre and post are identical)"
        return Response(body="\n".join([*header, "", body]))

    # ── helpers ────────────────────────────────────────────────────

    def _resolve_alias(self, alias: str) -> Path:
        if alias not in self.roots:
            raise NotFound(
                f"unknown python repo alias {alias!r}",
                options=list(self.roots),
                next="get(kind='python') to list configured repos",
            )
        return self.roots[alias]

    def _roots_for_scope(self, scope: str | None) -> dict[str, Path]:
        """Return the `{alias: root}` subset matching a search scope."""
        if scope is None:
            return self.roots
        # First segment up to '::' or '/' is the alias.
        if "::" in scope:
            alias = scope.split("::", 1)[0]
        elif "/" in scope:
            alias = scope.split("/", 1)[0]
        else:
            alias = scope
        if alias not in self.roots:
            return {}
        return {alias: self.roots[alias]}

    def _render_file(
        self,
        parsed: _ParsedId,
        mod: ModuleIndex,
        idx,
        view: str | None,
    ) -> Response:
        # Line-range selector → source slice (overrides view).
        if parsed.start_line is not None:
            text = (idx.root / mod.file).read_text(encoding="utf-8")
            return Response(
                body=render.render_source(
                    text,
                    file_label=f"{parsed.alias}/{mod.file}",
                    start_line=parsed.start_line,
                    end_line=parsed.end_line or parsed.start_line,
                )
            )

        # Block selector → resolve a Track B symbol within this file.
        if parsed.block_selector is not None:
            sym = _resolve_block_selector(mod, parsed.block_selector)
            if sym is None:
                raise NotFound(
                    f"no symbol {parsed.block_selector!r} in {mod.file}",
                    next=f"get(kind='python', id='{parsed.alias}/{mod.file}', view='outline')",
                )
            return self._render_symbol(parsed.alias, sym.qualname, idx, view)

        # File-level views.
        if view == "source":
            text = (idx.root / mod.file).read_text(encoding="utf-8")
            return Response(
                body=render.render_source(
                    text,
                    file_label=f"{parsed.alias}/{mod.file}",
                    start_line=1,
                    end_line=mod.module_symbol.end_line,
                )
            )
        if view == "toc":
            raise BadInput(
                "view='toc' applies to a repo, not a file",
                next=f"get(kind='python', id={parsed.alias!r}, view='toc')",
            )
        # Default and view='outline'.
        return Response(body=render.render_file_outline(parsed.alias, mod))

    def _render_symbol(
        self, alias: str, qualname: str, idx, view: str | None
    ) -> Response:
        sym = idx.symbol(qualname)
        if sym is None:
            raise NotFound(
                f"symbol {qualname!r} not found in repo {alias!r}",
                next=f"search(kind='python', q='{qualname.split('.')[-1]}', "
                f"scope={alias!r})",
            )

        if view == "source":
            text = (idx.root / sym.file).read_text(encoding="utf-8")
            return Response(
                body=render.render_source(
                    text,
                    file_label=f"{alias}/{sym.file}",
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                )
            )
        if view == "toc":
            raise BadInput(
                "view='toc' applies to a repo, not a symbol",
                next=f"get(kind='python', id={alias!r}, view='toc')",
            )
        # Default and view='outline'.
        return Response(body=render.render_symbol(alias, sym, idx))

    def _render_callgraph(
        self,
        *,
        parsed: _ParsedId,
        idx: RepoIndex,
        entry: str | None,
        depth: int,
        cross_repo: bool,
    ) -> Response:
        """Build + render an entry-point-rooted static call graph.

        The id MUST be an alias-only address (no file, no qualname,
        no selector). The entry point is supplied as a separate
        kwarg in module-colon-function (`pkg.mod:func`) or dotted
        qualname (`pkg.mod.func`) form.
        """
        if (
            parsed.file is not None
            or parsed.qualname is not None
            or parsed.start_line is not None
            or parsed.block_selector is not None
        ):
            raise BadInput(
                "view='callgraph' takes a bare alias id (no file / qualname / selector)",
                next=f"get(kind='python', id={parsed.alias!r}, view='callgraph', "
                f"entry='pkg.mod:func')",
            )
        if entry is None or not entry.strip():
            raise BadInput(
                "view='callgraph' requires entry=",
                next=f"get(kind='python', id={parsed.alias!r}, view='callgraph', "
                f"entry='pkg.mod:func', depth=3)",
            )
        if not isinstance(depth, int) or depth < 1 or depth > 10:
            raise BadInput(
                f"depth must be an int in [1, 10]; got {depth!r}",
                next=f"get(kind='python', id={parsed.alias!r}, view='callgraph', "
                f"entry={entry!r}, depth=3)",
            )

        other_repos: dict[str, RepoIndex] = {}
        if cross_repo:
            other_repos = {
                a: self.cache.get(p) for a, p in self.roots.items() if a != parsed.alias
            }

        # Console-script resolution (MCP critic round 2): if ``entry``
        # is a bare word (no ``.`` and no ``:``), it's almost certainly
        # the *name* of a console script from pyproject.toml —
        # ``precis``, ``pytest``, ``my-cli`` — not a dotted qualname.
        # Look it up in the entries report and rewrite to the
        # underlying ``module:func`` so the caller doesn't have to
        # know the internal plumbing. Collisions across scripts with
        # the same name don't happen (pyproject names are unique per
        # project), so the first (and only) hit wins.
        resolved_entry = entry
        if "." not in entry and ":" not in entry:
            report = entries_mod.find_entries(idx)
            for script in report.console_scripts:
                if script.name == entry:
                    resolved_entry = script.entry
                    break

        try:
            tree = cgraph.build_callgraph(
                idx,
                entry=resolved_entry,
                max_depth=depth,
                other_repos=other_repos or None,
                cross_repo=cross_repo,
            )
        except ValueError as e:
            raise NotFound(
                f"callgraph entry {entry!r} not found in repo {parsed.alias!r}: {e}",
                next=f"search(kind='python', q={entry.rsplit('.', 1)[-1].rsplit(':', 1)[-1]!r}, "
                f"scope={parsed.alias!r})",
            ) from e

        body = cgraph.render_callgraph(
            tree,
            alias=parsed.alias,
            entry=resolved_entry,
            max_depth=depth,
            cross_repo=cross_repo,
        )
        return Response(body=body)

    def _render_runtrace(
        self,
        *,
        parsed: _ParsedId,
        idx: RepoIndex,
        root: Path,
        entry: str | None,
        argv: list[str] | None,
        env: dict[str, str] | None,
        timeout: int,
        cross_repo: bool,
        max_events: int,
        expand_stdlib: bool,
    ) -> Response:
        """Spawn a subprocess that runs `entry` under `sys.setprofile`,
        capture call events, and overlay them on the static call set.

        Gated by ``PRECIS_PYTHON_ALLOW_EXEC=1`` because it executes
        user code. Off by default. The error message points at the
        env var so the agent's recovery path is unambiguous.

        By default, stdlib subtrees (argparse, re, gettext, builtins,
        …) are folded for legibility. Pass ``expand_stdlib=True`` via
        ``args=`` to keep the full tree.
        """
        import os

        # ── address validation ─────────────────────────────────────
        if (
            parsed.file is not None
            or parsed.qualname is not None
            or parsed.start_line is not None
            or parsed.block_selector is not None
        ):
            raise BadInput(
                "view='runtrace' takes a bare alias id",
                next=f"get(kind='python', id={parsed.alias!r}, view='runtrace', "
                f"args={{'entry': 'pkg.mod:func'}})",
            )
        if entry is None or not entry.strip():
            raise BadInput(
                "view='runtrace' requires entry=",
                next=f"get(kind='python', id={parsed.alias!r}, view='runtrace', "
                f"args={{'entry': 'pkg.mod:func', 'argv': ['--help']}})",
            )
        if not isinstance(timeout, int) or timeout < 1 or timeout > 60:
            raise BadInput(
                f"timeout must be an int in [1, 60]; got {timeout!r}",
                next=f"get(kind='python', id={parsed.alias!r}, view='runtrace', "
                f"args={{'entry': {entry!r}, 'timeout': 10}})",
            )
        if not isinstance(max_events, int) or max_events < 1 or max_events > 1_000_000:
            raise BadInput(
                f"max_events must be an int in [1, 1_000_000]; got {max_events!r}",
                next=f"get(kind='python', id={parsed.alias!r}, view='runtrace', "
                f"args={{'entry': {entry!r}, 'max_events': 2000}})",
            )

        # ── env gate ───────────────────────────────────────────────
        if os.environ.get(_RUNTRACE_GATE_ENV) != "1":
            raise BadInput(
                f"runtrace is gated by {_RUNTRACE_GATE_ENV}=1 because it "
                "executes user code in a subprocess",
                next=(
                    f"export {_RUNTRACE_GATE_ENV}=1 to enable, or use "
                    f"view='callgraph' for static analysis "
                    f"(args={{'entry': {entry!r}}})"
                ),
            )

        # ── run trace ──────────────────────────────────────────────
        # Make the configured root importable from the subprocess.
        # When cross_repo=True, also expose every other configured root.
        syspath_entries: list[Path] = [root, root.parent]
        if cross_repo:
            for alias, other in self.roots.items():
                if alias == parsed.alias:
                    continue
                syspath_entries.append(other)
                syspath_entries.append(other.parent)

        result = rtrace.run_trace(
            entry=entry,
            argv=list(argv or []),
            cwd=root,
            timeout=timeout,
            env=env,
            syspath=syspath_entries,
            max_events=max_events,
        )

        # ── build tree + static-only diff ──────────────────────────
        tree = rtrace.build_tree(result.events)
        # Capture qualnames *before* the stdlib collapse so the diff
        # reflects what actually ran, not what the agent sees.
        runtime_qualnames = rtrace.collect_runtime_qualnames(tree)

        collapsed = False
        if not expand_stdlib:
            tree = rtrace.collapse_stdlib(tree)
            collapsed = True

        # The static-only diff only makes sense when entry resolves to
        # a known qualname in the indexed repo. If it doesn't (e.g.
        # the user pointed at an installed-but-not-indexed package),
        # skip the diff.
        entry_qn = entry.replace(":", ".")
        static_only: list[str] | None = None
        if idx.symbol(entry_qn) is not None:
            static_only = rtrace.static_only_qualnames(
                idx=idx,
                entry_qualname=entry_qn,
                runtime_qualnames=runtime_qualnames,
            )

        body = rtrace.render_runtrace(
            alias=parsed.alias,
            entry=entry,
            argv=list(argv or []),
            result=result,
            tree=tree,
            static_only=static_only,
            collapsed=collapsed,
        )
        return Response(body=body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_scope(scope: str | None) -> tuple[str | None, str | None]:
    """Decompose a search scope into `(qualname_prefix, file)`.

    Returns (None, None) for None or repo-only scopes; (qn, None) for
    `<alias>::<qn>`; (None, file) for `<alias>/<file>`.
    """
    if scope is None:
        return None, None
    if "::" in scope:
        return scope.split("::", 1)[1], None
    if "/" in scope:
        return None, scope.split("/", 1)[1]
    return None, None


def _score_symbol(sym: Symbol, needle: str) -> float:
    """Lexical match score for a symbol against a lowercased query.

    Higher is better. Zero means no hit. Heuristic but stable:
    - exact qualname match           → 10
    - qualname contains needle       → 5  (+ short-name bonus)
    - signature contains needle      → 2
    - docstring contains needle      → 1
    """
    score = 0.0
    qn = sym.qualname.lower()
    if qn == needle:
        score += 10
    elif needle in qn:
        # Bonus when the match is on the short name (more specific).
        score += 5
        if needle in sym.name.lower():
            score += 2
    if sym.signature and needle in sym.signature.lower():
        score += 2
    if sym.docstring and needle in sym.docstring.lower():
        score += 1
    return score


def _resolve_block_selector(mod: ModuleIndex, selector: str) -> Symbol | None:
    """Look up a Track B selector against a module's symbol table.

    Accepts `Symbol`, `Class.method`, `Outer.Inner.method`, etc. The
    selector is suffix-matched against each symbol's qualname (so the
    user doesn't have to spell out the module prefix).
    """
    suffix = "." + selector
    for sym in mod.symbols:
        if sym.qualname == selector or sym.qualname.endswith(suffix):
            return sym
        # Also match the bare `name` for top-level functions/classes.
        if sym.name == selector and sym.kind != "module":
            return sym
    return None


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _ensure_trailing_newline(text: str) -> str:
    """Files conventionally end with `\\n`. Idempotent."""
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


def _reject_doubled_root_prefix(parsed: _ParsedId, root: Path) -> None:
    """Refuse ids whose relative path re-enters the root's tail segments.

    Real-world trip-wire: precis itself is deployed with
    ``PRECIS_PYTHON_ROOTS=precis=<pkg>/src/precis`` — i.e. the alias
    already points *inside* the package. Agents copy-pasting paths
    from the repo root write ``id='precis/src/precis/foo.py'``; the
    handler would then happily create a phantom
    ``<pkg>/src/precis/src/precis/foo.py`` directory tree (which
    also confuses the indexer into short qualnames).

    Rule: if the caller's relative path begins with segments that
    equal the last N segments of ``root``, reject with a hint that
    strips the redundant prefix. Single-segment overlap is enough —
    the root's leaf is the package name, and having a sibling
    directory of that name inside the package is almost never what
    the agent meant.
    """
    if parsed.file is None:
        return
    file_parts = tuple(p for p in parsed.file.split("/") if p)
    if not file_parts:
        return
    root_parts = root.parts
    # Find the longest prefix of ``file_parts`` that matches a suffix
    # of ``root.parts``.
    max_overlap = min(len(file_parts), len(root_parts))
    for overlap in range(max_overlap, 0, -1):
        if root_parts[-overlap:] == file_parts[:overlap]:
            redundant = "/".join(file_parts[:overlap])
            stripped = "/".join(file_parts[overlap:])
            raise BadInput(
                f"path {parsed.file!r} repeats the root's path "
                f"components ({redundant!r}); addresses are relative "
                f"to the repo root",
                next=(
                    f"put(kind='python', id='{parsed.alias}/{stripped}', ...)"
                    if stripped
                    else f"put(kind='python', id={parsed.alias!r}, ...)"
                ),
            )


def _module_qualname_for(path: Path, *, repo_root: Path) -> str:
    """Compute the dotted import qualname a `.py` file *would* have if
    indexed under `repo_root`.

    Delegates to the indexer's authoritative helper so the handler's
    gate 2 (qualname-drop) sees the same qualname shape as
    ``RepoIndex.symbol`` does for the same file. An earlier in-file
    reimplementation stopped walking at ``repo_root``, which produced
    qualnames shorter than the indexer's whenever the repo root was
    itself a package (``__init__.py`` at root) — firing a phantom
    'would disappear' drop on otherwise no-op replaces.

    ``repo_root`` is retained only to keep call-site compatibility;
    the indexer helper uses the file's parent chain directly.
    """
    del repo_root  # kept for signature compatibility
    return _qualname_for_file(path)


def _replace_summary(parsed: _ParsedId, region: tuple[int, int] | None) -> str:
    """One-line description of what was replaced. Used in put responses."""
    target = (
        f"{parsed.alias}::{parsed.qualname}"
        if parsed.qualname is not None
        else f"{parsed.alias}/{parsed.file}"
    )
    if region is None:
        return f"Replaced whole file {target}"
    start, end = region
    span = end - start + 1
    return f"Replaced {span} line{'s' if span != 1 else ''} ({start}-{end}) in {target}"


def _render_put_response(
    parsed: _ParsedId,
    ast_gate,
    qn_gate,
    ruff_changes,
    change_summary: str,
) -> str:
    """Render the post-write response shape per spec § Response shape.

    Reports each gate's result on its own line, then the change summary,
    then a Next: hint pointing back at the affected address. Ruff's
    line distinguishes 'no changes', 'fix' edits, 'format' edits, or
    both — matches what an interactive `ruff check --fix && ruff format`
    run would produce.
    """
    target = (
        f"{parsed.alias}::{parsed.qualname}"
        if parsed.qualname is not None
        else f"{parsed.alias}/{parsed.file or ''}"
    )

    lines = [f"# {target}\n"]
    lines.append(f"  ast.parse:           {'ok' if ast_gate.ok else 'FAIL'}")
    lines.append(
        f"  qualname preserved:  "
        f"{'ok' if qn_gate.ok else 'FAIL'}"
        + (f"  ({qn_gate.detail})" if qn_gate.detail and qn_gate.ok else "")
    )

    if not ruff_changes.ok:
        lines.append(f"  ruff:                skipped — {ruff_changes.error}")
    elif not ruff_changes.changed:
        lines.append("  ruff:                no changes")
    else:
        kinds: list[str] = []
        if ruff_changes.fix_changed:
            kinds.append("fix")
        if ruff_changes.format_changed:
            kinds.append("format")
        lines.append(f"  ruff:                {' + '.join(kinds)}")
    if ruff_changes.unfixable_findings:
        for f in ruff_changes.unfixable_findings:
            lines.append(f"    note: {f}")

    lines.append("")
    lines.append(change_summary)

    lines.append("")
    lines.append("Next:")
    if parsed.qualname is not None:
        lines.append(f"  get(kind='python', id='{parsed.alias}::{parsed.qualname}')")
    elif parsed.file is not None:
        lines.append(f"  get(kind='python', id='{parsed.alias}/{parsed.file}')")

    return "\n".join(lines)
