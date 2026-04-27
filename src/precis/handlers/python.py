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

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers import _python_render as render
from precis.protocol import Handler, KindSpec
from precis.python_index import ModuleIndex, RepoCache, Symbol
from precis.response import Response

log = logging.getLogger(__name__)


_SUPPORTED_VIEWS = ("toc", "outline", "source")


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
        supports_put=False,  # slice 6
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
        modes=(),
    )

    def __init__(
        self,
        *,
        roots: dict[str, Path],
        cache: RepoCache | None = None,
    ) -> None:
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
            return Response(body=f"no python symbols match {q!r}")

        hits.sort(key=lambda h: -h[0])
        hits = hits[:top_k]

        lines = [f"# {len(hits)} python hit{'s' if len(hits) != 1 else ''} for {q!r}"]
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
