"""MdHandler — slug-addressed navigator for the DB-free ``md`` kind.

Read-only mirror of :mod:`precis.handlers.python`'s pattern applied to
prose instead of code: an in-memory, mtime-gated index
(:class:`precis.md_index.MdRepoCache`) over one or more roots
registered via ``PRECIS_MD_ROOTS`` (parsed with the same
:func:`precis.handlers._roots.parse_alias_roots` the ``python`` kind
uses). No Postgres, no writes — see the :mod:`precis.md_index`
package docstring for the DB-free design.

Address grammar accepted by ``get``:

    None                            -> list registered roots
    '/'                             -> list registered roots
    <alias>                         -> root overview (file/block counts,
                                        top-level dirs)
    <alias>/<rel/path/to/file.md>   -> file outline (heading tree with
                                        block anchors); ``view='source'``
                                        for the full file text
    <alias>/<file>~<slug-or-title>  -> one heading's block(s) (the
                                        heading plus everything nested
                                        under it, up to the next
                                        heading of equal or higher rank)

``search`` is hybrid: a lexical leg (:func:`precis.md_index.search_blocks`)
always runs; a semantic leg (:func:`precis.md_index.cosine_search`) runs
only over blocks that already have a vector in the handler's
:class:`precis.md_index.MdVectorCache` — the cache is populated by a
background warm pass (later round), not by ``search`` itself, so a
cold or warming cache degrades to lexical-only rather than blocking
or erroring. The two legs are combined with
:func:`precis.md_index.fuse_blocks` (reciprocal rank fusion).

``put`` / ``edit`` / ``delete`` / ``tag`` / ``link`` are intentionally
not overridden: :class:`precis.protocol.Handler`'s defaults raise
``Unsupported`` for any verb a ``KindSpec`` doesn't declare, and this
kind's ``KindSpec`` declares only ``get``/``search`` — repo docs are
edited as files, not through this read-only index.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._roots import parse_alias_roots
from precis.md_index import (
    MdBlockEntry,
    MdFileEntry,
    MdRepoCache,
    MdRepoIndex,
    MdVectorCache,
    cosine_search,
    fuse_blocks,
    search_blocks,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils.embed_query import embed_query
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

log = logging.getLogger(__name__)

_SUPPORTED_VIEWS = ("outline", "source")


# ---------------------------------------------------------------------------
# Env var parsing
# ---------------------------------------------------------------------------


def parse_md_roots(raw: str | None) -> dict[str, Path]:
    """Parse a ``PRECIS_MD_ROOTS`` value into ``{alias: abs_path}``.

    Same format and semantics as ``PRECIS_PYTHON_ROOTS`` — see
    :func:`precis.handlers._roots.parse_alias_roots`, which this
    delegates to.
    """
    return parse_alias_roots(raw, env_var="PRECIS_MD_ROOTS")


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedMdId:
    """Components of an address string accepted by ``get``.

    ``selector``, when set, addresses one heading's block(s) within
    ``file`` (see module docstring). Deliberately simpler than
    python's ``_ParsedId``: no qualname track, no line-range track —
    prose has no symbol table.
    """

    alias: str
    file: str | None = None
    selector: str | None = None


def _parse_id(raw: str) -> _ParsedMdId:
    """Parse an address string. Raises ``BadInput`` on syntactic problems."""
    if not raw:
        raise BadInput("empty id", next="get(kind='md')")

    base, sep, raw_selector = raw.partition("~")
    selector: str | None = raw_selector if sep else None

    if "/" in base:
        alias, file = base.split("/", 1)
        if not alias:
            raise BadInput(
                f"malformed id {raw!r}: missing alias before '/'",
                next="get(kind='md') to list known aliases",
            )
        file_opt: str | None = file or None
    else:
        alias, file_opt = base, None

    if selector is not None:
        if not selector:
            raise BadInput(
                f"malformed id {raw!r}: empty selector after '~'",
                next=f"get(kind='md', id='{alias}/path.md~Heading')",
            )
        if file_opt is None:
            raise BadInput(
                f"selector ~{selector!r} requires a file id",
                next=f"get(kind='md', id='{alias}/path.md~{selector}')",
            )

    return _ParsedMdId(alias=alias, file=file_opt, selector=selector)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class MdHandler(Handler):
    """Read-only navigator + hybrid search over one or more md roots.

    Constructed with a dict of ``alias -> absolute root path``
    (typically parsed from ``PRECIS_MD_ROOTS`` by the registry).
    ``cache`` / ``vector_cache`` are optional injection seams for
    tests; production code lets the handler own fresh instances.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="md",
        title="Markdown workspace index (DB-free)",
        description=(
            "Browse and hybrid-search workspace markdown (docs, backlog, "
            "skills prose) via an in-memory, mtime-gated index. Read-only "
            "— edit files directly on disk."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
    )

    def __init__(
        self,
        *,
        hub: Hub,
        roots: dict[str, Path],
        cache: MdRepoCache | None = None,
        vector_cache: MdVectorCache | None = None,
    ) -> None:
        if not isinstance(roots, dict):
            raise TypeError("roots must be a dict[str, Path]")
        resolved: dict[str, Path] = {}
        for alias, path in roots.items():
            if not alias or "/" in alias or "::" in alias or "~" in alias:
                raise ValueError(
                    f"invalid md root alias {alias!r}: "
                    f"must be non-empty and must not contain '/', '::', or '~'"
                )
            p = Path(path).resolve()
            if not p.is_dir():
                raise ValueError(f"md root {alias!r} is not a directory: {p}")
            resolved[alias] = p
        self.roots = resolved
        self.cache = cache or MdRepoCache()

        # ``hub`` may be a store-less/stub hub (this kind opens zero DB
        # connections); only its ``embedder`` attribute is read, matching
        # how the DB-backed file handlers (plaintext/markdown) take
        # ``self.embedder = hub.embedder`` in __init__.
        self.embedder: Any = hub.embedder if hub is not None else None

        self.vector_cache: MdVectorCache | None
        if vector_cache is not None:
            self.vector_cache = vector_cache
        elif self.embedder is not None:
            self.vector_cache = MdVectorCache(
                model=self.embedder.model, dim=self.embedder.dim
            )
        else:
            self.vector_cache = None

    # ── get ────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or id == "/" or id == "":
            return Response(body=_render_index(self.roots))

        parsed = _parse_id(str(id))
        root = self._resolve_alias(parsed.alias)
        idx = self.cache.get(root)

        if view is not None and view not in _SUPPORTED_VIEWS:
            raise Unsupported(
                f"unknown md view {view!r}",
                options=list(_SUPPORTED_VIEWS),
                next=f"get(kind='md', id={id!r}, view='outline')",
            )

        if parsed.file is None:
            if view == "source":
                raise BadInput(
                    "view='source' requires a file id",
                    next=f"get(kind='md', id={parsed.alias!r})",
                )
            return Response(body=_render_overview(parsed.alias, idx))

        file_entry = idx.file(parsed.file)
        if file_entry is None:
            raise NotFound(
                f"file {parsed.file!r} not found in md root {parsed.alias!r}",
                next=f"get(kind='md', id={parsed.alias!r})",
            )

        if parsed.selector is not None:
            block = _resolve_selector(file_entry, parsed.selector)
            if block is None:
                raise NotFound(
                    f"no heading {parsed.selector!r} in {parsed.file}",
                    next=f"get(kind='md', id='{parsed.alias}/{parsed.file}')",
                )
            return Response(body=_render_section(parsed.alias, file_entry, block))

        if view == "source":
            return Response(body=_render_source(parsed.alias, file_entry, root))
        return Response(body=_render_file_outline(parsed.alias, file_entry))

    # ── search ─────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        """Hybrid lexical + semantic search over indexed md blocks.

        Lexical leg (:func:`precis.md_index.score_block`) always runs.
        Semantic leg (cosine similarity) runs only over blocks that
        already have a cached vector — a cold or still-warming vector
        cache degrades to lexical-only rather than failing. The two
        legs are RRF-fused per root, then merged by fused score across
        roots.

        ``scope=`` may be:
        - alias (``'docs'``) — restrict to one root
        - alias/path-prefix (``'docs/backlog'``) — restrict to a
          subtree or single file of one root
        """
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='md', q='your query')",
            )

        roots = self._roots_for_scope(scope)
        if not roots:
            raise NotFound(
                f"no md root matches scope={scope!r}",
                next="search(kind='md', q='...') to search all roots",
            )

        scope_prefix = _scope_path_prefix(scope)
        query_vec = embed_query(self.embedder, q) if self.embedder is not None else None

        # (score, alias, file, block)
        fused: list[tuple[float, str, str, MdBlockEntry]] = []
        total_blocks = 0
        indexed_blocks = 0

        for alias, root in roots.items():
            idx = self.cache.get(root)

            lex_hits = _filter_scope(search_blocks(idx, q), scope_prefix)
            sem_hits: list[tuple[float, str, MdBlockEntry]] = []
            if query_vec is not None and self.vector_cache is not None:
                sem_hits = _filter_scope(
                    cosine_search(idx, query_vec, self.vector_cache), scope_prefix
                )

            for score, f, b in fuse_blocks(lex_hits, sem_hits):
                fused.append((score, alias, f, b))

            blocks = _filter_scope_blocks(idx.all_blocks(), scope_prefix)
            total_blocks += len(blocks)
            if self.vector_cache is not None:
                indexed_blocks += sum(
                    1 for _, b in blocks if b.sha256 in self.vector_cache
                )

        if not fused:
            return Response(body=_render_no_hits(q, scope, roots))

        fused.sort(key=lambda h: (-h[0], h[1], h[2], h[3].pos))
        total = len(fused)
        hits = fused[:page_size]

        headline = format_search_headline(
            n_returned=len(hits), total=total, noun="md hit", query=q
        )
        headline += _coverage_suffix(
            embedder_wired=self.embedder is not None,
            total_blocks=total_blocks,
            indexed_blocks=indexed_blocks,
        )

        lines = [headline]
        for score, alias, f, b in hits:
            anchor = f"{alias}/{f}~{b.slug}"
            crumb = " > ".join(b.heading_path)
            lines.append(f"\n## {anchor}  (score={score:.4f}, {f}:{b.line_start})")
            if crumb:
                lines.append(f"  {crumb}")
            lines.append(f"  {_oneline(b.text)}")
        return Response(body="\n".join(lines))

    # ── helpers ────────────────────────────────────────────────────

    def _resolve_alias(self, alias: str) -> Path:
        if alias not in self.roots:
            raise NotFound(
                f"unknown md root alias {alias!r}",
                options=list(self.roots),
                next="get(kind='md') to list configured roots",
            )
        return self.roots[alias]

    def _roots_for_scope(self, scope: str | None) -> dict[str, Path]:
        """Return the ``{alias: root}`` subset matching a search scope."""
        if scope is None:
            return self.roots
        alias = scope.split("/", 1)[0] if "/" in scope else scope
        if alias not in self.roots:
            return {}
        return {alias: self.roots[alias]}


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _scope_path_prefix(scope: str | None) -> str | None:
    """Repo-relative path prefix from a ``alias/path`` scope, else None."""
    if scope is None or "/" not in scope:
        return None
    return scope.split("/", 1)[1]


def _file_in_scope(file: str, prefix: str | None) -> bool:
    if prefix is None:
        return True
    return file == prefix or file.startswith(prefix + "/")


def _filter_scope(
    hits: list[tuple[float, str, MdBlockEntry]], prefix: str | None
) -> list[tuple[float, str, MdBlockEntry]]:
    if prefix is None:
        return hits
    return [(s, f, b) for s, f, b in hits if _file_in_scope(f, prefix)]


def _filter_scope_blocks(
    blocks: list[tuple[str, MdBlockEntry]], prefix: str | None
) -> list[tuple[str, MdBlockEntry]]:
    if prefix is None:
        return blocks
    return [(f, b) for f, b in blocks if _file_in_scope(f, prefix)]


def _coverage_suffix(
    *, embedder_wired: bool, total_blocks: int, indexed_blocks: int
) -> str:
    if not embedder_wired:
        return " (lexical only: no embedder wired)"
    if total_blocks == 0 or indexed_blocks >= total_blocks:
        return ""
    pct = int(100 * indexed_blocks / total_blocks)
    return f" (semantic: {pct}% of blocks indexed)"


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------


def _resolve_selector(file_entry: MdFileEntry, selector: str) -> MdBlockEntry | None:
    """Resolve a ``~selector`` against one file's blocks.

    Tries an exact ``md_parse`` slug match first (stable, content-
    derived); falls back to a case-insensitive heading-title match so
    ``~'Ship workflow'`` works without knowing the minted slug.
    """
    block = file_entry.block(selector)
    if block is not None:
        return block
    needle = selector.strip().lower()
    for b in file_entry.blocks:
        if b.kind == "heading" and b.title and b.title.strip().lower() == needle:
            return b
    return None


def _section_blocks(
    file_entry: MdFileEntry, heading_block: MdBlockEntry
) -> list[MdBlockEntry]:
    """The addressed block plus everything nested under it.

    Non-heading blocks (matched by content slug rather than title)
    have no children — just the one block. Heading blocks pull every
    following block up to (not including) the next heading whose
    level is `<=` this one's.
    """
    if heading_block.kind != "heading":
        return [heading_block]
    level = heading_block.heading_level or 1
    out = [heading_block]
    started = False
    for b in file_entry.blocks:
        if b is heading_block:
            started = True
            continue
        if not started:
            continue
        if b.kind == "heading" and (b.heading_level or 1) <= level:
            break
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_index(roots: dict[str, Path]) -> str:
    if not roots:
        return (
            "# md - no roots configured\n\n"
            "Set `PRECIS_MD_ROOTS=alias:/abs/path,alias2:/abs/path` to "
            "register one or more markdown roots.\n"
        )

    lines = [f"# md - {len(roots)} root{'s' if len(roots) != 1 else ''}\n"]
    for alias, root in roots.items():
        lines.append(f"  {alias:<24} {root}")
    body = "\n".join(lines)
    first = next(iter(roots))
    body += render_next_section(
        [
            (f"get(kind='md', id={first!r})", "root overview"),
            ("search(kind='md', q='your query')", "search across all roots"),
        ]
    )
    return body


def _render_overview(alias: str, idx: MdRepoIndex) -> str:
    n_files = idx.n_files
    n_blocks = idx.n_blocks

    top_dirs: Counter[str] = Counter()
    for rel in idx.files:
        top_dirs["." if "/" not in rel else rel.split("/", 1)[0]] += 1

    lines = [f"# {alias} - md root overview\n"]
    lines.append(f"  Root:   {idx.root}")
    lines.append(f"  Files:  {n_files}")
    lines.append(f"  Blocks: {n_blocks}")
    if top_dirs:
        lines.append("")
        lines.append("Top-level dirs:")
        for d, count in top_dirs.most_common():
            lines.append(f"  {d:<28} {count} file{'s' if count != 1 else ''}")

    # ``sample`` below is the real file the drill-down hint names — the
    # overview otherwise only prints top-level dir *counts*, never a
    # filename, so without this line the hint's target is unverifiable
    # from the page it rides on. Minimal fix: print the sample right
    # next to it rather than re-deriving the hint from a dir listing.
    sample = sorted(idx.files)[0] if idx.files else None
    if sample:
        lines.append("")
        lines.append(f"  Sample file: {sample}")

    body = "\n".join(lines)
    hints: list[tuple[str, str]] = []
    if sample:
        hints.append(
            (f"get(kind='md', id='{alias}/{sample}')", "view a file's heading outline")
        )
    hints.append(
        (f"search(kind='md', q='your query', scope={alias!r})", "search this root")
    )
    body += render_next_section(hints)
    return body


def _render_file_outline(alias: str, file_entry: MdFileEntry) -> str:
    lines = [f"# {alias}/{file_entry.file}\n"]
    lines.append(
        f"  {file_entry.n_blocks} block{'s' if file_entry.n_blocks != 1 else ''}"
    )
    lines.append("")

    headings = [b for b in file_entry.blocks if b.kind == "heading"]
    if headings:
        for b in headings:
            indent = "  " * max((b.heading_level or 1) - 1, 0)
            lines.append(f"{indent}{b.title}  (~{b.slug}, L{b.line_start})")
    else:
        lines.append("  (no headings)")

    body = "\n".join(lines)
    hints = [
        (
            f"get(kind='md', id='{alias}/{file_entry.file}', view='source')",
            "full file text",
        )
    ]
    if headings:
        hints.append(
            (
                f"get(kind='md', id='{alias}/{file_entry.file}~{headings[0].slug}')",
                "view one section",
            )
        )
    body += render_next_section(hints)
    return body


def _render_source(alias: str, file_entry: MdFileEntry, root: Path) -> str:
    text = (root / file_entry.file).read_text(encoding="utf-8")
    body = f"# {alias}/{file_entry.file} (source)\n\n{text}"
    body += render_next_section(
        [(f"get(kind='md', id='{alias}/{file_entry.file}')", "heading outline")]
    )
    return body


def _render_section(alias: str, file_entry: MdFileEntry, block: MdBlockEntry) -> str:
    blocks = _section_blocks(file_entry, block)
    lines = [f"# {alias}/{file_entry.file}~{block.slug}\n"]
    if block.heading_path:
        lines.append("  " + " > ".join(block.heading_path))
        lines.append("")
    for b in blocks:
        lines.append(b.text)
        lines.append("")
    body = "\n".join(lines).rstrip("\n") + "\n"
    body += render_next_section(
        [(f"get(kind='md', id='{alias}/{file_entry.file}')", "back to file outline")]
    )
    return body


def _render_no_hits(q: str, scope: str | None, roots: dict[str, Path]) -> str:
    first_alias = sorted(roots)[0]
    hints: list[tuple[str, str]] = []
    if scope is not None:
        hints.append(
            (f"search(kind='md', q={q!r})", "widen to all roots (drop scope=)")
        )
    hints.append((f"get(kind='md', id={first_alias!r})", "browse this root"))
    body = f"no md hits for {q!r}\n"
    body += render_next_section(hints)
    return body


_WS_RE = re.compile(r"\s+")


def _oneline(text: str, *, max_len: int = 96) -> str:
    """First non-empty line of ``text``, whitespace-collapsed and
    truncated to ``max_len`` with an ellipsis."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = _WS_RE.sub(" ", first)
    if len(first) > max_len:
        first = first[: max_len - 1] + "…"
    return first
