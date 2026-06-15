"""PlaintextHandler — read/write ``.txt`` / ``.log`` / ``.bib`` files under a root.

Sibling of :class:`precis.handlers.markdown.MarkdownHandler` with a
simpler block grammar: paragraphs only, no headings or fenced code.
BibTeX ``.bib`` files fit that grammar naturally — each ``@entry{…}``
is its own paragraph — so the plaintext handler covers them without
a dedicated bib kind.
Writes go to disk verbatim after a UTF-8 encode check — there is no
AST or re-parse gate beyond what markdown already does.

Same address grammar:

    notes--log          — file overview
    notes--log~SLUG     — one paragraph by slug
    notes--log~N        — one paragraph by 0-indexed pos
    notes--log/raw      — full source text
    /                   — list every known plaintext file

Same put modes (``create`` / ``append`` / ``replace`` / ``delete`` /
``edit`` / ``insert``) with identical semantics to markdown.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
)
from precis.handlers._slug_ref_shared import reject_chunk_or_path_view
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref
from precis.store.types import Tag
from precis.utils.block_ingest import to_block_inserts
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    classify_diff_hunks,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.file_id import (
    canonicalize_path_id,
    format_write_result,
    nearest_slugs,
    parse_line_range,
)
from precis.utils.md_parse import (
    file_slug_from_path,
    is_valid_file_slug,
    path_from_file_slug,
)
from precis.utils.next_block import render_next_section
from precis.utils.plaintext_parse import (
    PlaintextBlock,
    parse_plaintext,
)
from precis.utils.search_header import detect_score_cliff, format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

log = logging.getLogger(__name__)


# After the seven-verb cutover, ``put`` on a file kind is creation-only.
# Region edits move to the ``edit`` verb (mode='find-replace'|'append'|
# 'insert'|'replace') and selector-deletes move to ``delete``.
_SUPPORTED_PUT_MODES = ("create",)


def _recipe(
    *,
    kind: str,
    slug: str,
    mode: str,
    find: str | None,
    text: str | None,
    before: str = "",
    after: str = "",
    where: str | None = None,
    match: str | None = None,
    nth: int | None = None,
    trailing_comment: str | None = None,
) -> str:
    """Render a copy-pasteable ``edit(...)`` call for use in error hints.

    Only emits the kwargs the caller actually supplied (non-empty anchors,
    non-default match policy, etc.) so the recipe reads like a call the
    agent could have written — not a ten-argument straw man that
    confuses small models more than it helps.

    ``find`` and ``text`` values that start with a quote are passed
    through verbatim (so the caller can pass sentinel strings like
    ``"'exact text'"`` or ``"''"``). Everything else is :func:`repr`-
    quoted so the recipe is valid Python.
    """

    def _q(v: str) -> str:
        # Pre-quoted sentinels (``"'exact text'"``, ``"''"``) pass through
        # unchanged so the hint reads naturally; everything else gets
        # normal repr quoting.
        if v.startswith(("'", '"')):
            return v
        return repr(v)

    parts: list[str] = [f"kind={kind!r}", f"id={slug!r}", f"mode={mode!r}"]
    if find is not None:
        parts.append(f"find={_q(find)}")
    if before:
        parts.append(f"before={before!r}")
    if after:
        parts.append(f"after={after!r}")
    if where:
        parts.append(f"where={where!r}")
    if match and match != "unique":
        parts.append(f"match={match!r}")
    if nth is not None:
        parts.append(f"nth={nth}")
    if text is not None:
        parts.append(f"text={_q(text)}")
    call = f"edit({', '.join(parts)})"
    if trailing_comment:
        call = f"{call}   {trailing_comment}"
    return call


class PlaintextHandler(Handler):
    """Slug-addressed read/write handler for ``.txt`` / ``.log`` / ``.bib`` files."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="plaintext",
        title="Plaintext",
        description=(
            "Read and edit local plaintext files (.txt, .log, .bib) "
            "under a configured root. Lazy re-ingest on stale mtime; "
            "paragraph slugs are content-stable."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        views=("raw",),
        modes=_SUPPORTED_PUT_MODES,
    )

    # ── subclass-extensible knobs ───────────────────────────────────
    # Sibling kinds (e.g. ``tex``) reuse the entire paragraph-block
    # pipeline by overriding these ClassVars + the ``spec`` above.
    # No instance methods need re-implementing.
    _KIND: ClassVar[str] = "plaintext"
    _EXTENSIONS: ClassVar[tuple[str, ...]] = (".txt", ".log", ".bib")
    _DEFAULT_EXT: ClassVar[str] = ".txt"

    #: Views accepted by :meth:`get` (in addition to the no-view
    #: overview / block selector). Subclasses extend this and override
    #: :meth:`_render_view` to add their own (e.g. tex's ``toc``).
    _SUPPORTED_VIEWS: ClassVar[tuple[str, ...]] = ("raw",)

    # ── parser hooks (overrideable by subclasses) ─────────────────
    def _parse_blocks(self, content: str) -> list[PlaintextBlock]:
        """Parse ``content`` into a list of paragraph-shaped blocks.

        Plaintext: blank-line splitting via
        :func:`precis.utils.plaintext_parse.parse_plaintext`. Subclasses
        (e.g. :class:`TexHandler`) override to inject their own block
        grammar; the return type may widen to a subclass of
        :class:`PlaintextBlock` as long as the new fields are
        additive.
        """
        return parse_plaintext(content)

    def _block_meta(self, block: PlaintextBlock) -> dict[str, Any]:
        """Per-block metadata stored on the row's ``meta`` JSON.

        Plaintext records only the line span; subclasses extend with
        kind-specific axes (e.g. tex stores ``section_level``,
        ``section_title``, ``section_path``, ``inputs``).
        """
        return {"line_start": block.line_start, "line_end": block.line_end}

    def _derive_title(self, blocks: Sequence[Any], *, fallback: str) -> str:
        """Pick a title for a freshly-ingested ref.

        Plaintext: first line of the first paragraph, truncated at 80
        characters. :class:`MarkdownHandler` overrides to prefer the
        first H1 heading. Subclasses override to inject kind-specific
        title grammar without re-implementing the full ingest
        pipeline.
        """
        return _derive_title(list(blocks), fallback=fallback)

    def _block_miss_options(self, blocks: Sequence[Any], sel: _BlockSel) -> list[str]:
        """Options list for a block NotFound error.

        Prefers ambiguous prefix-shorthand matches (the caller typed
        a shared prefix of several slugs; every candidate is a valid
        answer) and falls back to difflib nearest-match hinting for
        a one-character typo. Returns ``[]`` on pos-selector misses
        — integer pos errors don't benefit from slug suggestions.
        """
        if sel.is_pos:
            return []
        prefix_hits = _prefix_shorthand_matches(blocks, sel.value)
        if prefix_hits:
            return prefix_hits
        candidates = [b.slug for b in blocks if b.slug]
        return nearest_slugs(sel.value, candidates)

    def _block_noun(self) -> str:
        """Word used for a single block in error messages and responses.

        Plaintext: ``paragraph`` (only block kind the grammar
        produces). Markdown override: ``block`` (the file has
        headings / paragraphs / code / lists / tables so the
        generic noun is more accurate). Tex inherits
        ``paragraph`` — the noun is cosmetic and already distinct
        from ``block`` so cross-kind responses don't collide.
        """
        return "paragraph"

    def _overview_body_extras(self, ref: Ref, blocks: Sequence[Any]) -> list[str]:
        """Extra lines spliced into the overview after the header.

        Plaintext default: first-5-paragraph preview so the agent has
        something to drill into without fetching ``/raw``. Markdown
        overrides to show a heading-TOC preview.
        """
        lines: list[str] = []
        if blocks:
            lines.append("")
            lines.append("## Paragraphs (first few)")
            for b in blocks[:5]:
                preview = _excerpt(b.text, limit=60)
                lines.append(f"- ~{b.slug or b.pos}: {preview}")
            if len(blocks) > 5:
                lines.append(f"  … and {len(blocks) - 5} more (see /raw)")
        return lines

    @classmethod
    def _strip_ext(cls, rel_path: str) -> tuple[str, str]:
        """Return ``(base_without_extension, extension)`` for one of
        :attr:`_EXTENSIONS`. Unrecognised extensions yield an empty
        ``extension`` so the caller can reject them."""
        for candidate in cls._EXTENSIONS:
            if rel_path.lower().endswith(candidate):
                return rel_path[: -len(candidate)], rel_path[-len(candidate) :].lower()
        return rel_path, ""

    def _walk_files(self) -> list[Path]:
        """Yield every file under ``self.root`` whose extension is in
        :attr:`_EXTENSIONS` (case-insensitive).

        Symlinks whose target resolves **outside** :attr:`root` are
        dropped — listing them in the index would mislead the agent
        (the file would show up but every read would fail the
        ``relative_to`` gate). Listing == reachability.
        """
        out: list[Path] = []
        exts = tuple(e.lower() for e in self._EXTENSIONS)
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if not name.lower().endswith(exts):
                    continue
                candidate = Path(dirpath) / name
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(self.root)
                except (OSError, ValueError):
                    # Broken symlink, traversal escape, or any other
                    # unresolvable path — silently skip.
                    continue
                out.append(candidate)
        return out

    def __init__(self, *, hub: Hub, root: Path) -> None:
        if hub.store is None:
            raise InitError(f"{self._KIND}: store required")
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"{self._KIND} root {str(root)!r} does not exist or is not a directory"
            )
        self.store = hub.store
        self.embedder = hub.embedder
        self.root = root.resolve()

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Index/list sentinels — every form an agent might reach for
        # to mean "show me what's here":
        # - ``id=None`` (no id at all)
        # - ``id='/'`` or any path starting with `/` (path-form root)
        # - ``id='.'`` or ``id='./'`` (cwd shorthand)
        # - ``id=''`` (empty string)
        # All route to the same `ls` view rather than producing
        # ``invalid {kind} slug: '.'`` from the slug validator.
        if id is None or (
            isinstance(id, str)
            and (id.startswith("/") or id.strip() in ("", ".", "./"))
        ):
            return self._render_index()

        slug, sel, path_view = _parse_file_id(str(id), extensions=self._EXTENSIONS)
        effective_view = path_view or view

        ref = self._require_existing_file(slug)

        if sel is not None and effective_view is not None:
            raise BadInput(
                f"cannot combine block selector with view={effective_view!r}",
                next=f"get(kind='{self._KIND}', id='{slug}~SLUG') or '{slug}/raw'",
            )

        if sel is not None:
            return self._render_block(ref, sel)

        if effective_view is not None:
            return self._render_view(effective_view, ref, slug=slug)

        return self._render_overview(ref)

    def _render_view(self, view: str, ref: Ref, *, slug: str) -> Response:
        """Dispatch to a per-view renderer.

        Plaintext supports only ``raw``. Subclasses extend
        :attr:`_SUPPORTED_VIEWS` and override this method to add
        their own (calling ``super()._render_view(...)`` to fall
        through to ``raw`` / Unsupported).
        """
        if view == "raw":
            return self._render_raw(ref)
        raise Unsupported(
            f"unknown {self._KIND} view {view!r}",
            options=list(self._SUPPORTED_VIEWS),
            next=f"get(kind='{self._KIND}', id='{slug}/raw')",
        )

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next=f"search(kind='{self._KIND}', q='your query')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = self._ensure_ingested(scope)
            if scope_ref is None:
                raise NotFound(
                    f"{self._KIND} file {scope!r} not found",
                    next=f"search(kind='{self._KIND}', q='...') to find one",
                )
            scope_ref_id = scope_ref.id

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=self._KIND,
            scope_ref_id=scope_ref_id,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            # Canonical Next: block — c5 unified-trailer patch.
            body = f"no {self._KIND} blocks match {q!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='{self._KIND}', q={q!r}, page_size=50)",
                        "widen the lexical net",
                    ),
                    (
                        f"search(kind='{self._KIND}', q={q!r}, scope='<file-slug>')",
                        "search inside a specific file",
                    ),
                ]
            )
            return Response(body=body)

        total = self.store.count_blocks_lexical(
            q=q, kind=self._KIND, scope_ref_id=scope_ref_id
        )
        # Detect a score cliff — unique-literal queries
        # (PROBE_MARKER_ZX7Q-style) produce one dominant hit and a
        # long tail of low-confidence neighbours. Surfacing that in
        # the header saves the agent token cost on tail pagination.
        # MCP critic MINOR-$ 2026-05-02.
        hit_scores = [score for _block, _ref, score in hits]
        n_strong = detect_score_cliff(hit_scores)
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block hit",
                query=q,
                n_strong=n_strong,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "???"
            handle = f"{slug}~{block.slug or block.pos}"
            preview = _excerpt(block.text)
            lines.append(f"\n## {handle}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        if not (q and q.strip()):
            return []
        # query_vec= may be pre-supplied by the runtime cross-kind
        # dispatcher (computed once for all kinds).
        if query_vec is None and self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=self._KIND,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind=self._KIND)

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
        name: str | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a new paragraph-block file.

        Per the seven-verb surface (D6), ``put`` is creation-only on
        file kinds. Region edits live on the ``edit`` verb; region
        deletes live on ``delete``.

        ``tags=`` is the D3 shortcut for "create then tag": the new
        ref carries the listed tags in addition to the auto-stamped
        ``workspace`` flag. The runtime also layers
        ``PRECIS_DEFAULT_TAGS`` into this list via
        :meth:`PrecisRuntime._apply_default_tags_policy` before the
        handler runs (see ADR 0013 / OQ-17), so an operator-stated
        session-context tag set lands on every prose-file ref
        without per-call wiring on the agent side.
        """
        if mode in self._LEGACY_PUT_MODES_TO_EDIT:
            new_mode = "find-replace" if mode == "edit" else mode
            raise BadInput(
                f"mode={mode!r} is not accepted on put for kind={self._KIND!r}",
                next=(
                    f"use edit(kind='{self._KIND}', id=..., mode={new_mode!r}, ...) "
                    "for region edits"
                ),
            )
        if mode == "delete":
            raise BadInput(
                f"mode='delete' is not accepted on put for kind={self._KIND!r}",
                next=(
                    f"use delete(kind='{self._KIND}', id='slug~SLUG') for region "
                    "deletes"
                ),
            )
        # Default mode='create' when the caller used the slug-only
        # ``name=`` form. ``name=`` always means "create a fresh file";
        # there's no ambiguity to resolve. The explicit ``id=`` form
        # still requires ``mode='create'`` so existing path-form
        # callers stay strict. Removes a footgun the LLM hits often.
        if mode is None and name is not None and id is None:
            mode = "create"
        if mode != "create":
            raise BadInput(
                f"mode= is required and must be 'create' (got {mode!r})",
                options=["create"],
                next=f"put(kind='{self._KIND}', name='foo', text='...')  # mode defaults to create when name= is passed",
            )
        # Slug-only convention path: when the caller passes ``name=``
        # AND a workspace is ambient (``PRECIS_WORKSPACE`` env), the
        # layout convention computes the path. The LLM never sees
        # physical paths — it just says "write a section called
        # 'intro'" and the system routes it. The classic
        # ``id=<path>`` form still works as an escape hatch for
        # explicit paths or workspace-less callers.
        if name is not None and id is None:
            id = self._resolve_workspace_name_to_id(name)
        if id is None:
            raise BadInput(
                "put requires id= (the file path / slug) or name= (workspace-routed)",
                next=f"put(kind='{self._KIND}', name='<short>', text='...', mode='create')",
            )
        # Extension hint: the caller's raw id (e.g. ``./references.bib``)
        # tells us which extension they want. ``_parse_file_id`` strips
        # the extension during canonicalisation, so we sniff it here
        # before the strip and pass it down. Without this hint,
        # ``_resolve_path`` falls back to ``_DEFAULT_EXT`` on a fresh
        # file, silently producing e.g. ``references.txt`` when the
        # caller asked for ``references.bib``.
        raw_id = str(id)
        preferred_ext: str | None = None
        for ext in self._EXTENSIONS:
            if raw_id.lower().endswith(ext):
                preferred_ext = ext
                break
        slug, _sel, _path_view = _parse_file_id(raw_id, extensions=self._EXTENSIONS)
        return self._put_create(slug, text, preferred_ext=preferred_ext, tags=tags)

    # ── put helpers ────────────────────────────────────────────────

    def _put_create(
        self,
        slug: str,
        text: str | None,
        *,
        preferred_ext: str | None = None,
        tags: list[str] | None = None,
    ) -> Response:
        path = self._resolve_path(slug, must_exist=False, preferred_ext=preferred_ext)
        if path.exists():
            # Previously the hint pointed at
            # ``edit(..., mode='replace', text=...)`` with no
            # selector — which rejects at the edit layer with a hint
            # back at put(mode='replace'), which put also rejects.
            # Unrecoverable triangle (MCP critic CRITICAL-C
            # 2026-05-02). Break it by landing the hint on calls
            # that actually run end-to-end.
            raise BadInput(
                f"file already exists: {slug!r}",
                next=(
                    f"get(kind='{self._KIND}', id='{slug}/toc') to list "
                    "block slugs, then "
                    f"edit(kind='{self._KIND}', id='{slug}~<block>', "
                    "mode='replace', text='...') to rewrite one block, or "
                    f"edit(kind='{self._KIND}', id={slug!r}, "
                    "mode='find-replace', find='old', text='new') for a "
                    "surgical splice"
                ),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (text or "").rstrip() + "\n"
        # Layer-1 mechanical fix (tex kind only). Runs deterministic
        # tex-syntax fixes (unicode escapes, \\usepackage detection)
        # before the write. Result note attaches to the response.
        mechanical_note = ""
        if self._KIND == "tex":
            from precis.utils.tex_mechanical_fix import apply_mechanical_fixes

            mech = apply_mechanical_fixes(body)
            body = mech.text
            if mech.fixes:
                mechanical_note = (
                    " — mechanical fixes: " + "; ".join(mech.fixes)
                )
        # Workspace-scoped per-put advisory lock + git commit. The
        # lock is held through the disk write and the commit so two
        # concurrent puts in the same workspace serialize cleanly.
        # ``commit_sha`` is reported in the response.
        workspace_relpath, commit_sha = self._commit_in_workspace(
            path, body, slug=slug
        )
        if commit_sha is None:
            # Workspace-less path (no PRECIS_WORKSPACE): write directly.
            _atomic_write(path, body)
        ref = self._ensure_ingested(slug)
        assert ref is not None
        if tags:
            apply_tag_ops(self.store, self._KIND, ref.id, tags=tags, untags=None)
        n = self.store.count_blocks(ref.id)
        suffix = f" [commit={commit_sha[:8]}]" if commit_sha else ""
        return Response(
            body=f"created {self._KIND} {slug!r} ({n} paragraph(s)){suffix}"
            f"{mechanical_note}"
        )

    def _commit_in_workspace(
        self, path: Path, body: str, *, slug: str
    ) -> tuple[str | None, str | None]:
        """Acquire workspace lock, write the file, commit.

        Returns ``(workspace_relpath, commit_sha)``. When no
        workspace is active (no ``PRECIS_WORKSPACE``), returns
        ``(None, None)`` and the caller should write directly.
        Otherwise the lock + write + commit are atomic.
        """
        from precis.utils.workspace import (
            commit_put,
            current_from_env,
        )

        ws_path = current_from_env()
        if not ws_path:
            return (None, None)
        # Compute workspace-relative path for the commit message.
        try:
            workspace_relpath = str(
                path.relative_to((self.root / ws_path).resolve())
            )
        except ValueError:
            # Path landed outside the workspace; skip the commit path
            # and let the caller do a plain write.
            return (None, None)
        # Workspace-scoped PG advisory lock. Keyed on the workspace
        # path string hash. Auto-released when the connection closes
        # (we use a session-scoped lock and release explicitly).
        with self.store.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"workspace:{ws_path}",),
                )
                _atomic_write(path, body)
                ws_root = (self.root / ws_path).resolve()
                # Templated commit message; structured per-tick summary
                # lives in the job_result chunk (T1.6 wires the link).
                summary = (
                    f"{workspace_relpath}: write via slug={slug!r}"
                )
                commit_sha = commit_put(
                    ws_root,
                    summary=summary,
                    body="",
                )
        return (workspace_relpath, commit_sha)

    def _put_append(self, slug: str, text: str | None) -> Response:
        if text is None or not text.strip():
            raise BadInput(
                "append requires text=",
                next=(
                    f"edit(kind='{self._KIND}', id={slug!r}, text='...', mode='append')"
                ),
            )
        path = self._resolve_path(slug, must_exist=True)
        existing = path.read_text(encoding="utf-8")
        # Separate with a blank line so the new text reads as its own
        # paragraph on re-ingest.
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        new_content = existing.rstrip() + sep + text.rstrip() + "\n"
        _atomic_write(path, new_content)
        ref = self._ensure_ingested(slug, force=True)
        assert ref is not None
        # Unified response — name slug, block pos, block slug, and
        # line range so chained edits don't need a follow-up
        # /toc round-trip (MCP critic MAJOR-C 2026-05-02).
        last = _last_block(self.store.list_blocks_for_ref(ref.id))
        return Response(
            body=format_write_result(
                verb="appended",
                file_slug=slug,
                block_pos=last.pos if last else None,
                block_slug=last.slug if last else None,
                line_start=(last.meta or {}).get("line_start") if last else None,
                line_end=(last.meta or {}).get("line_end") if last else None,
            )
        )

    def _put_replace(
        self, slug: str, sel: _BlockSel | None, text: str | None
    ) -> Response:
        if sel is None:
            # Previously suggested put(mode='replace'), which put
            # rejects for file kinds — feeding the CRITICAL-C
            # hint triangle (MCP critic 2026-05-02).
            raise BadInput(
                "mode='replace' requires a block selector - "
                "id='slug~BLOCK' (or id='slug~L42-58' for a line range)",
                next=(
                    f"get(kind='{self._KIND}', id='{slug}/toc') to list "
                    "block slugs, then "
                    f"edit(kind='{self._KIND}', id='{slug}~<block>', "
                    "mode='replace', text='...')"
                ),
            )
        if text is None:
            raise BadInput(
                "replace requires text=",
                next=(
                    f"edit(kind='{self._KIND}', id='{slug}~{sel.value}', "
                    "mode='replace', text='...')"
                ),
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = self._parse_blocks(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            options = self._block_miss_options(blocks, sel)
            msg = f"{self._block_noun()} {sel.value!r} not found in {slug!r}"
            if (
                sel.is_pos is False
                and len(options) > 1
                and not any(b.slug == sel.value for b in blocks)
            ):
                # The options came from prefix shorthand and the
                # query is ambiguous — tell the caller so they can
                # pick one.
                prefix_hits = _prefix_shorthand_matches(blocks, sel.value)
                if len(prefix_hits) > 1:
                    msg += (
                        f" \u2014 prefix {sel.value!r} matches "
                        f"{len(prefix_hits)} blocks; disambiguate"
                    )
            raise NotFound(
                msg,
                options=options or None,
                next=f"get(kind='{self._KIND}', id='{slug}')",
            )
        new_lines = text.rstrip("\n").split("\n")
        _replace_lines(path, target.line_start, target.line_end, new_lines)
        ref = self._ensure_ingested(slug, force=True)
        assert ref is not None
        # Recover (slug, pos, lines) of the post-replace block —
        # line_start survives equal/shorter splices; pos is the
        # fallback.
        fresh = self.store.list_blocks_for_ref(ref.id)
        new_block = _block_at_line(fresh, target.line_start) or _block_at_pos(
            fresh, target.pos
        )
        return Response(
            body=format_write_result(
                verb="replaced",
                file_slug=slug,
                block_pos=new_block.pos if new_block else target.pos,
                block_slug=new_block.slug if new_block else target.slug,
                line_start=(new_block.meta or {}).get("line_start")
                if new_block
                else target.line_start,
                line_end=(new_block.meta or {}).get("line_end")
                if new_block
                else target.line_start + len(new_lines) - 1,
            )
        )

    def _put_delete(self, slug: str, sel: _BlockSel | None) -> Response:
        if sel is None:
            raise BadInput(
                "delete requires a block selector - id='slug~BLOCK'",
                next=f"delete(kind='{self._KIND}', id='{slug}~BLOCK')",
            )
        path = self._resolve_path(slug, must_exist=True)
        blocks = self._parse_blocks(path.read_text(encoding="utf-8"))
        target = _find_block(blocks, sel)
        if target is None:
            options = self._block_miss_options(blocks, sel)
            raise NotFound(
                f"{self._block_noun()} {sel.value!r} not found in {slug!r}",
                options=options or None,
                next=f"get(kind='{self._KIND}', id='{slug}')",
            )
        deleted_pos = target.pos
        deleted_slug = target.slug
        deleted_line_start = target.line_start
        deleted_line_end = target.line_end
        _replace_lines(path, target.line_start, target.line_end, [])
        self._ensure_ingested(slug, force=True)
        return Response(
            body=format_write_result(
                verb="deleted",
                file_slug=slug,
                block_pos=deleted_pos,
                block_slug=deleted_slug,
                line_start=deleted_line_start,
                line_end=deleted_line_end,
            )
        )

    def _delete_file(self, slug: str) -> Response:
        """Remove a whole file + its ref.

        Symmetry with ``put(mode='create')``: anything the handler
        can create via the API, it can also delete via the API
        (MCP critic MINOR-C 2026-05-02). Gated behind an explicit
        confirm string encoding the slug (see :meth:`delete`).
        """
        path = self._resolve_path(slug, must_exist=False)
        ref = self.store.get_ref(kind=self._KIND, id=slug)
        if not path.exists() and ref is None:
            self._raise_file_not_found(slug)
        if path.exists():
            os.remove(path)
        if ref is not None:
            self.store.soft_delete_ref(ref.id)
        return Response(body=format_write_result(verb="deleted file", file_slug=slug))

    def _put_anchored(
        self,
        *,
        slug: str,
        sel: _BlockSel | None,
        op_kind: str,
        find: str | None,
        text: str | None,
        before: str,
        after: str,
        where: str | None,
        match: str,
        nth: int | None,
        dry_run: bool | str = False,
    ) -> Response:
        # ``op_kind`` is the post-translation internal name (``edit`` for
        # the wire-level ``find-replace`` mode, ``insert`` for ``insert``).
        # The agent never sees ``edit`` on this verb — surfacing it in
        # errors used to make a 7B caller correct ``mode='find-replace'``
        # to ``mode='edit'`` and re-loop on a fresh BadInput. Echo the
        # name they actually wrote.
        user_mode = "find-replace" if op_kind == "edit" else op_kind
        dry_mode = normalize_dry_run(dry_run)
        if find is None or not find:
            raise BadInput(
                f"mode={user_mode!r} requires find= (the exact text to locate)",
                next=_recipe(
                    kind=self._KIND,
                    slug=slug,
                    mode=user_mode,
                    find="'exact text'",
                    text="'replacement'",
                    before=before,
                    after=after,
                    where=where,
                    match=match,
                    nth=nth,
                ),
            )
        if text is None:
            # MCP critic 2026-05-03: small models (qwen3:8b) hitting this
            # error were repeating the byte-identical call, not retrying
            # with text=. Root cause: the previous message buried the
            # delete idiom ("'' is allowed for delete-by-edit") in a
            # parenthetical, and the `next:` hint said "add text='...'"
            # without a copyable recipe. Invert: lead with the two
            # choices (delete vs replace), echo every supplied arg in
            # the recipe so the caller can copy-paste it.
            raise BadInput(
                f"mode={user_mode!r} requires text=. "
                f"Pass text='' to DELETE the matched span; "
                f"pass text='<replacement>' to REPLACE it.",
                next=_recipe(
                    kind=self._KIND,
                    slug=slug,
                    mode=user_mode,
                    find=find,
                    text="''",
                    before=before,
                    after=after,
                    where=where,
                    match=match,
                    nth=nth,
                    trailing_comment="# delete",
                ),
            )
        op = EditOp(
            op="edit" if op_kind == "edit" else "insert",
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,  # type: ignore[arg-type]
            match=match,  # type: ignore[arg-type]
            nth=nth,
            region_label=f"{slug}" if sel is None else f"{slug}~{sel.value}",
            base_line=1,
        )

        path = self._resolve_path(slug, must_exist=True)
        full = path.read_text(encoding="utf-8")

        if sel is None:
            result = apply_edit(full, op)
            new_full = result.new_buffer
        else:
            blocks = self._parse_blocks(full)
            target = _find_block(blocks, sel)
            if target is None:
                options = self._block_miss_options(blocks, sel)
                raise NotFound(
                    f"{self._block_noun()} {sel.value!r} not found in {slug!r}",
                    options=options or None,
                    next=f"get(kind='{self._KIND}', id='{slug}')",
                )
            op = EditOp(
                op=op.op,
                find=op.find,
                text=op.text,
                before=op.before,
                after=op.after,
                where=op.where,
                match=op.match,
                nth=op.nth,
                region_label=op.region_label,
                base_line=target.line_start,
            )
            result = apply_edit(target.text, op)
            new_block_lines = result.new_buffer.split("\n")
            new_full = _splice_lines(
                full, target.line_start, target.line_end, new_block_lines
            )

        if dry_mode is not None:
            return self._render_dry_run(
                slug=slug,
                sel=sel,
                pre=full,
                post=new_full,
                edited_spans=result.edited_spans,
                match_policy=op.match,
                mode=dry_mode,
            )

        # Validation gate: the post-edit buffer must round-trip through
        # UTF-8 (a Python str always does, but encoding-check the bytes
        # to catch surrogate escapes or lone surrogates that would
        # break other readers).
        try:
            new_full.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no cover — defensive
            raise BadInput(
                f"post-edit buffer failed UTF-8 encode: {exc}",
                next="check the replacement text for invalid Unicode",
            ) from exc

        _atomic_write(path, new_full)
        ref = self._ensure_ingested(slug, force=True)
        assert ref is not None

        # Unified response — first edited span's (slug, pos, lines)
        # so chained edits don't need a follow-up /toc round-trip.
        # ``[N spans]`` suffix signals when match='all' produced
        # multiple locations that may cross block boundaries.
        spans = result.edited_spans or ()
        verb = "edited" if op_kind == "edit" else "inserted"
        fresh = self.store.list_blocks_for_ref(ref.id)
        if spans:
            first_line = spans[0][0]
            last_line = spans[-1][1]
            anchor_block = _block_at_line(fresh, first_line)
        else:
            first_line = 0
            last_line = 0
            anchor_block = None
        return Response(
            body=format_write_result(
                verb=verb,
                file_slug=slug,
                block_pos=anchor_block.pos if anchor_block else None,
                block_slug=anchor_block.slug if anchor_block else None,
                line_start=first_line or None,
                line_end=last_line or None,
                span_count=len(spans),
            )
        )

    def _render_dry_run(
        self,
        *,
        slug: str,
        sel: _BlockSel | None,
        pre: str,
        post: str,
        edited_spans: tuple[tuple[int, int], ...],
        match_policy: str,
        mode: str,
    ) -> Response:
        region_label = f"{slug}~{sel.value}" if sel else slug
        try:
            n_blocks = len(self._parse_blocks(post))
            reparse_note = f"ok (would re-ingest {n_blocks} paragraph(s))"
        except Exception as exc:  # pragma: no cover — defensive
            reparse_note = f"FAILED ({exc})"
        within, outside = classify_diff_hunks(pre, post, edited_spans)
        extras = [
            ("re-parse:", reparse_note),
            ("hunks:", f"{within} within edited spans, {outside} outside"),
        ]
        header = render_dry_run_header(
            region_label=region_label,
            edited_spans=edited_spans,
            match_policy=match_policy,
            extras=extras,
        )
        if mode == "full":
            body = render_dry_run_full(
                post,
                edited_spans=edited_spans,
                region_label=region_label,
            )
        else:
            diff = format_unified_diff(pre, post, file_label=slug).rstrip("\n")
            body = diff or "(no diff - pre and post are identical)"
        return Response(body="\n".join([*header, "", body]))

    # ── seven-verb surface ─────────────────────────────────────────

    #: Modes accepted by :meth:`edit` — region-modifying ops only.
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
        dry_run: bool | str = False,
        **_kw: Any,
    ) -> Response:
        """Region-edit an existing paragraph-block file.

        Same shape as :meth:`MarkdownHandler.edit`. Routes to the
        existing private helpers; ``mode='find-replace'`` is the new
        default name for the legacy ``put(mode='edit')`` path.
        """
        if mode not in self._EDIT_MODES:
            raise BadInput(
                f"unknown edit mode {mode!r}",
                options=list(self._EDIT_MODES),
                next=(
                    f"edit(kind='{self._KIND}', id='slug', mode='find-replace', "
                    "find='old', text='new')"
                ),
            )
        slug, sel, _path_view = _parse_file_id(str(id), extensions=self._EXTENSIONS)
        # Gate on file existence *before* dispatching so downstream
        # hints (e.g. "_put_replace: next=get(id='slug/toc') …") never
        # echo a bogus slug produced by syntactic path-form canonicalisation
        # (``work/foo.tex`` → ``work--foo`` when PRECIS_ROOT is already
        # ``work/``). ``append`` legitimately creates-or-appends so we
        # skip the gate for it.
        if mode != "append":
            self._require_existing_file(slug)
        if mode == "append":
            return self._put_append(slug, text)
        if mode == "replace":
            return self._put_replace(slug, sel, text)
        op_kind = "edit" if mode == "find-replace" else mode
        return self._put_anchored(
            slug=slug,
            sel=sel,
            op_kind=op_kind,
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
            dry_run=dry_run,
        )

    def delete(  # type: ignore[override]
        self,
        *,
        id: str | int,
        confirm: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Delete a paragraph / region, or the whole file.

        - Block-level (default): requires a selector in ``id``
          (``slug~SLUG`` / ``slug~Lstart-Lend``).
        - File-level: pass ``confirm='delete-file-<slug>'`` with no
          selector. Unlinks the file under ``PRECIS_ROOT`` and
          soft-deletes the ref. The confirm string encodes the
          slug so a stray ``confirm=True`` can't delete the wrong
          file. Matches the file-level ``put(mode='create')`` so
          ``put`` and ``delete`` are symmetric (MCP critic MINOR-C
          2026-05-02).
        """
        slug, sel, _path_view = _parse_file_id(str(id), extensions=self._EXTENSIONS)
        if sel is None:
            expected_confirm = f"delete-file-{slug}"
            if confirm != expected_confirm:
                raise BadInput(
                    (
                        f"delete on {self._KIND} requires a block selector - "
                        f"id='{slug}~SLUG' - or confirm={expected_confirm!r} "
                        "to remove the whole file"
                    ),
                    next=(
                        f"delete(kind='{self._KIND}', id='{slug}~SLUG') "
                        "to remove a paragraph, or "
                        f"delete(kind='{self._KIND}', id={slug!r}, "
                        f"confirm={expected_confirm!r}) to remove the file"
                    ),
                )
            return self._delete_file(slug)
        return self._put_delete(slug, sel)

    def _resolve_pt_ref(self, id: str | int) -> tuple[str, int]:
        """Coerce an id to (slug, ref_id), ingesting the file if needed."""
        slug, sel, path_view = _parse_file_id(str(id), extensions=self._EXTENSIONS)
        reject_chunk_or_path_view(
            kind=self._KIND,
            slug=slug,
            sel=sel,
            path_view=path_view,
            selector_noun="block selector",
            level_noun="file",
        )
        ref = self._require_existing_file(slug)
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove tags on a paragraph-block file."""
        if not add and not remove:
            raise BadInput(
                f"tag(kind='{self._KIND}', id=...) requires add= or remove=",
                next=f"tag(kind='{self._KIND}', id='<slug>', add=['draft'])",
            )
        slug, ref_id = self._resolve_pt_ref(id)
        n_added, n_removed = apply_tag_ops(
            self.store, self._KIND, ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind=self._KIND,
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from a paragraph-block file to another ref."""
        if target is None:
            raise BadInput(
                f"link(kind='{self._KIND}', id=...) requires target=",
                next=(f"link(kind='{self._KIND}', id='<slug>', target='paper:slug')"),
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug, ref_id = self._resolve_pt_ref(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self._KIND,
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── ingest pipeline ────────────────────────────────────────────

    # Flag tag auto-applied to every ref under ``PRECIS_ROOT`` so the
    # LLM can scope ``search(tags=['workspace'])`` to its working
    # directory. Applied via :meth:`_apply_workspace_tag` at every
    # successful-ingest exit point.
    _WORKSPACE_FLAG: ClassVar[str] = "workspace"

    def _apply_workspace_tag(self, ref: Ref) -> None:
        """Idempotently stamp the ``workspace`` flag tag on *ref*.

        Called at the tail of :meth:`_ensure_ingested`. ``add_tag``
        uses ``ON CONFLICT DO NOTHING``, so repeated calls are cheap
        and safe (one INSERT that no-ops). The tag is set with
        ``set_by='system'`` to distinguish it from agent-authored
        tags — filter queries that want only the auto-stamp can
        still find it, but audit views can identify it as
        machine-applied.
        """
        self.store.add_tag(
            ref.id,
            Tag.flag(self._WORKSPACE_FLAG),
            set_by="system",
        )

    def _ensure_ingested(self, slug: str, *, force: bool = False) -> Ref | None:
        path = self._resolve_path(slug, must_exist=False)
        ref = self.store.get_ref(kind=self._KIND, id=slug)

        if not path.exists():
            if ref is not None:
                self.store.soft_delete_ref(ref.id)
            return None

        st = path.stat()
        mtime_ns = st.st_mtime_ns
        meta = (ref.meta if ref is not None else {}) or {}

        if not force and ref is not None and meta.get("mtime_ns") == mtime_ns:
            self._apply_workspace_tag(ref)
            return ref

        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if not force and ref is not None and meta.get("sha256") == sha:
            self.store.update_ref(ref.id, meta_patch={"mtime_ns": mtime_ns})
            self._apply_workspace_tag(ref)
            return ref

        pt_blocks = self._parse_blocks(content)
        title = self._derive_title(pt_blocks, fallback=slug)
        # Preserve the original extension in meta so the handler can
        # rebuild the real file path even when several extensions map
        # to the same slug shape (e.g. plaintext: .txt vs .log).
        rel = str(path.relative_to(self.root))
        _base, ext = self._strip_ext(rel)
        new_meta = {
            "path": rel,
            "ext": ext or self._DEFAULT_EXT,
            "mtime_ns": mtime_ns,
            "mtime_iso": datetime.datetime.fromtimestamp(
                st.st_mtime, tz=datetime.UTC
            ).isoformat(),
            "sha256": sha,
            "size": st.st_size,
        }

        inserts = to_block_inserts(
            pt_blocks, embedder=self.embedder, meta_for=self._block_meta
        )

        with self.store.tx() as conn:
            if ref is None:
                ref = self.store.insert_ref(
                    kind=self._KIND,
                    slug=slug,
                    title=title,
                    meta=new_meta,
                    conn=conn,
                )
            else:
                self.store.update_ref(ref.id, title=title, meta_patch=new_meta)

            self.store.insert_blocks(ref.id, inserts, replace=True, conn=conn)

        fresh = self.store.get_ref(kind=self._KIND, id=slug)
        if fresh is not None:
            self._apply_workspace_tag(fresh)
        return fresh

    # ── not-found helpers ──────────────────────────────────────────

    def _list_file_slugs_on_disk(self) -> dict[str, str]:
        """Enumerate valid ``{slug: relpath}`` pairs under ``self.root``.

        Shared by :meth:`_render_index` (which needs both slug and
        relpath) and :meth:`_raise_file_not_found` (which needs only
        the slug list for fuzzy-match suggestions). Keeps the
        canonical "what does this handler see on disk?" answer in a
        single place so index rendering and error hinting can't
        drift apart.
        """
        seen: dict[str, str] = {}
        for path in sorted(self._walk_files()):
            try:
                rel = str(path.relative_to(self.root))
                base, _ext = self._strip_ext(rel)
                slug = file_slug_from_path(base)
            except ValueError:
                continue
            if not is_valid_file_slug(slug):
                continue
            seen[slug] = rel
        return seen

    def _raise_file_not_found(self, slug: str) -> NotFound:
        """Raise ``NotFound`` for a missing file slug, with fuzzy-match
        suggestions drawn from actual on-disk files.

        The path-form → slug canonicalisation in
        :func:`canonicalize_path_id` is *syntactic* — it happily turns
        ``work/foo.tex`` into ``work--foo`` even when no such file
        exists under PRECIS_ROOT. Without fuzzy-match hints the agent
        sees ``work--foo not found`` and has no cue that the real
        slug is just ``foo`` (PRECIS_ROOT *is* ``work/``). This
        helper supplies that cue via :func:`nearest_slugs`.

        Never returns — always raises. Return type annotated as
        ``NotFound`` purely so ``raise self._raise_file_not_found(...)``
        passes the type checker.
        """
        candidates = list(self._list_file_slugs_on_disk())
        suggestions = nearest_slugs(slug, candidates)
        raise NotFound(
            f"{self._KIND} file {slug!r} not found in workspace",
            options=suggestions,
            next=(
                f"get(kind='{self._KIND}', id={suggestions[0]!r})"
                if suggestions
                else f"get(kind='{self._KIND}') to list every known file"
            ),
        )

    def _require_existing_file(self, slug: str) -> Ref:
        """Ingest + return a file ref, or raise a suggestions-rich NotFound.

        Wraps :meth:`_ensure_ingested` so every read/write entry point
        can guard against bogus slugs with one call, without each
        site re-inventing the error shape.
        """
        ref = self._ensure_ingested(slug)
        if ref is None:
            self._raise_file_not_found(slug)
        assert ref is not None  # _raise_file_not_found never returns
        return ref

    # ── render helpers ─────────────────────────────────────────────

    def _render_index(self) -> Response:
        seen = self._list_file_slugs_on_disk()
        if not seen:
            return Response(
                body=(
                    f"no {self._KIND} files in workspace\n"
                    f"create one with put(kind='{self._KIND}', id='SLUG', "
                    "text='...', mode='create')"
                )
            )

        lines = [f"# {len(seen)} {self._KIND} file(s) in workspace"]
        max_w = max(len(s) for s in seen)
        for slug in sorted(seen):
            lines.append(f"  {slug:<{max_w}}  {seen[slug]}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (f"get(kind='{self._KIND}', id='<slug>')", "open a file"),
                (f"get(kind='{self._KIND}', id='<slug>/raw')", "full source"),
                (
                    f"search(kind='{self._KIND}', q='...', scope='<slug>')",
                    "search inside one file",
                ),
            ]
        )
        return Response(body=body)

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        n_blocks = self.store.count_blocks(ref.id)
        rel = meta.get("path", "?")
        size = meta.get("size") or "?"
        # "paragraphs" is the plaintext-native noun. Subclasses that
        # use a different block grammar (markdown = blocks, tex =
        # section-scoped blocks) override :meth:`_overview_blocks_label`
        # for the right word.
        lines = [
            f"# {ref.slug}",
            f"_{ref.title}_",
            "",
            f"path:        {rel}",
            f"{self._overview_blocks_label():<12} {n_blocks}",
            f"bytes:       {size}",
        ]
        if meta.get("mtime_iso"):
            lines.append(f"mtime:       {meta['mtime_iso']}")

        # Block-preview pane — subclasses can inject headings / TOC.
        blocks = self.store.list_blocks_for_ref(ref.id)
        lines.extend(self._overview_body_extras(ref, blocks))

        body = "\n".join(lines)
        body += render_next_section(self._overview_next_hints(ref))
        return Response(body=body)

    def _overview_blocks_label(self) -> str:
        """Label for the block-count line in the overview.

        Plaintext: ``paragraphs:``. Markdown override: ``blocks:``.
        The trailing padding is handled by the f-string format spec
        in :meth:`_render_overview` so every entry aligns visually.
        """
        return "paragraphs:"

    def _overview_next_hints(self, ref: Ref) -> list[tuple[str, str]]:
        """Next: hint list for the overview response."""
        return [
            (f"get(kind='{self._KIND}', id='{ref.slug}/raw')", "full source"),
            (
                f"get(kind='{self._KIND}', id='{ref.slug}~SLUG')",
                "read one paragraph by slug",
            ),
            (
                f"search(kind='{self._KIND}', q='...', scope='{ref.slug}')",
                "search inside this file",
            ),
        ]

    def _render_block(self, ref: Ref, sel: _BlockSel) -> Response:
        if sel.is_line_range:
            return self._render_line_range(ref, sel.line_start, sel.line_end)
        if sel.is_pos:
            try:
                pos = int(sel.value)
            except ValueError as exc:
                raise BadInput(
                    f"unparseable pos selector: {sel.value!r}",
                    next=f"get(kind='{self._KIND}', id='{ref.slug}~SLUG')",
                ) from exc
            block = self.store.get_block(ref.id, pos=pos)
            if block is None:
                raise NotFound(
                    f"no {self._block_noun()} at ~{pos} in {ref.slug!r}",
                    next=f"get(kind='{self._KIND}', id='{ref.slug}')",
                )
        else:
            block = self.store.get_block(ref.id, slug=sel.value)
            if block is None:
                # Fallback 1: unique prefix shorthand — recover from
                # ``inserted-by-probe-marker`` → full hash-suffixed
                # slug (MCP critic MINOR-C 2026-05-02).
                all_blocks = self.store.list_blocks_for_ref(ref.id)
                prefix_hits = _prefix_shorthand_matches(all_blocks, sel.value)
                if len(prefix_hits) == 1:
                    block = next(b for b in all_blocks if b.slug == prefix_hits[0])
                else:
                    # Fallback 2: difflib nearest-match hinting so a
                    # one-character typo doesn't force a /toc round-
                    # trip (MCP critic MAJOR-C 2026-05-02). Prefer
                    # ambiguous prefix matches when there are
                    # several, since those are exactly what the
                    # caller asked for.
                    candidates = [b.slug for b in all_blocks if b.slug]
                    options = prefix_hits or nearest_slugs(sel.value, candidates)
                    msg = (
                        f"no {self._block_noun()} with slug "
                        f"{sel.value!r} in {ref.slug!r}"
                    )
                    if len(prefix_hits) > 1:
                        msg += (
                            f" - prefix {sel.value!r} matches "
                            f"{len(prefix_hits)} blocks; disambiguate"
                        )
                    raise NotFound(
                        msg,
                        options=options or None,
                        next=f"get(kind='{self._KIND}', id='{ref.slug}')",
                    )
        handle = f"{ref.slug}~{block.slug or block.pos}"
        body = f"# {handle}\n{block.text}"
        body += render_next_section(
            [
                (
                    f"get(kind='{self._KIND}', id='{ref.slug}')",
                    "back to overview",
                ),
                (
                    (
                        f"edit(kind='{self._KIND}', id='{handle}', "
                        "text='...', mode='replace')"
                    ),
                    "edit this paragraph",
                ),
                (
                    f"delete(kind='{self._KIND}', id='{handle}')",
                    "delete this paragraph",
                ),
            ]
        )
        return Response(body=body)

    def _render_raw(self, ref: Ref) -> Response:
        path = self._resolve_path(ref.slug or "", must_exist=False)
        if not path.exists():
            return Response(body=f"{ref.slug}: file no longer on disk")
        return Response(body=path.read_text(encoding="utf-8"))

    def _render_line_range(self, ref: Ref, line_start: int, line_end: int) -> Response:
        """Render every block that intersects 1-indexed
        ``[line_start, line_end]`` on disk.

        The skill advertises Track-A line-range addressing — a
        caller arriving from a stack trace, grep, or IDE should be
        able to address blocks by line directly (MCP critic MAJOR-C
        2026-05-02). Each matching block's header cites the
        canonical slug so follow-up calls can move to Track B.
        """
        all_blocks = self.store.list_blocks_for_ref(ref.id)
        matched: list[Any] = []
        for b in all_blocks:
            meta = b.meta or {}
            b_start = meta.get("line_start")
            b_end = meta.get("line_end")
            if b_start is None or b_end is None:
                continue
            if _intersects(int(b_start), int(b_end), line_start, line_end):
                matched.append(b)
        if not matched:
            raise NotFound(
                (f"no block intersects L{line_start}-{line_end} in {ref.slug!r}"),
                next=f"get(kind='{self._KIND}', id='{ref.slug}')",
            )
        pieces: list[str] = []
        for b in matched:
            meta = b.meta or {}
            b_start = int(meta.get("line_start") or 0)
            b_end = int(meta.get("line_end") or 0)
            name = b.slug or str(b.pos)
            line_str = f"L{b_start}-{b_end}" if b_end != b_start else f"L{b_start}"
            handle = f"{ref.slug}~{name}"
            pieces.append(f"# {handle}  (block {b.pos}, {line_str})\n{b.text}")
        body = "\n\n".join(pieces)
        first = matched[0]
        first_name = first.slug or str(first.pos)
        body += render_next_section(
            [
                (
                    f"get(kind='{self._KIND}', id='{ref.slug}~{first_name}')",
                    "re-fetch the first matched block by slug",
                ),
                (
                    f"get(kind='{self._KIND}', id='{ref.slug}/raw')",
                    "full source",
                ),
            ]
        )
        return Response(body=body)

    # ── path resolution ────────────────────────────────────────────

    def _resolve_workspace_name_to_id(self, name: str) -> str:
        """Route a workspace-scoped ``name`` to a concrete path-form id.

        Reads the ambient workspace from ``PRECIS_WORKSPACE``, looks
        up the layout convention for this handler's kind, and returns
        the relative path (workspace subdir + name + ext) as the
        path-form id the rest of put can consume.

        Side effect: ensures the workspace dir exists on disk (lazy
        init copies templates + runs git init if needed). The first
        put in a fresh workspace pays this one-time cost.

        Raises :class:`BadInput` when there's no workspace context
        (caller passed ``name=`` but no ``PRECIS_WORKSPACE`` env) so
        the LLM gets a clear error rather than a confusing path
        resolution failure later.
        """
        from precis.utils.workspace import (
            Workspace,
            current_from_env,
            ensure_initialized,
        )
        from precis.utils.workspace_layout import is_generated, resolve

        ws_path = current_from_env()
        if not ws_path:
            raise BadInput(
                "name= requires an active workspace (PRECIS_WORKSPACE unset)",
                next=(
                    "either set PRECIS_WORKSPACE in the MCP env, or pass "
                    f"id='<explicit/path.{self._DEFAULT_EXT.lstrip('.')}>'"
                ),
            )
        # Build a Workspace stub from the env-provided path + sensible
        # defaults. The actual workspace meta (format, entrypoint) is
        # on the parent todo but the file handlers don't have access
        # to that; derive format from the kind itself.
        format = "tex" if self._KIND == "tex" else "md"
        entrypoint = "main.tex" if format == "tex" else "main.md"
        workspace = Workspace(
            path=ws_path, format=format, entrypoint=entrypoint
        )
        ensure_initialized(workspace, self.root)
        try:
            workspace_relpath = resolve(
                format=format, kind=self._KIND, name=name
            )
        except ValueError as exc:
            raise BadInput(
                f"workspace layout rejected name={name!r}: {exc}",
                next=(
                    "names are lowercase a-z 0-9 hyphens with optional .ext; "
                    "the layout dict routes by (workspace.format, kind, name)"
                ),
            ) from exc
        if is_generated(workspace_relpath):
            raise BadInput(
                f"{workspace_relpath!r} is workspace-generated, not writable "
                "via put",
                next=(
                    "refs.bib regenerates from kind='citation' refs; mint "
                    "citations instead of writing the bib directly"
                ),
            )
        # Return as the path-form id (with the workspace prefix). The
        # rest of put consumes it as if the caller had passed
        # ``id='projects/x/tex/intro.tex'`` directly.
        return f"{ws_path}/{workspace_relpath}"

    def _resolve_path(
        self, slug: str, *, must_exist: bool, preferred_ext: str | None = None
    ) -> Path:
        if not is_valid_file_slug(slug):
            raise BadInput(
                f"invalid {self._KIND} slug: {slug!r}",
                next="slugs are lowercase a-z 0-9 hyphens, segments split by '--'",
            )
        # Look up the already-ingested ref to know which extension to
        # use. Fresh files default to ``self._DEFAULT_EXT``; re-reads
        # honour the stored extension so opening (e.g. a .log) doesn't
        # accidentally read a .txt.
        ext = self._DEFAULT_EXT
        existing = self.store.get_ref(kind=self._KIND, id=slug)
        if existing is not None:
            ext = (existing.meta or {}).get("ext") or self._DEFAULT_EXT
        elif preferred_ext and preferred_ext in self._EXTENSIONS:
            # Caller explicitly asked for a specific extension
            # (e.g. ``put(id='./references.bib', ...)``). Honour it
            # over both on-disk probe and ``_DEFAULT_EXT``.
            ext = preferred_ext
        else:
            # On first touch with no hint, probe disk for any registered
            # extension and prefer whichever exists. Ties fall back to
            # the default.
            rel_base = path_from_file_slug(slug, ext="").rstrip(".")
            for candidate_ext in self._EXTENSIONS:
                candidate = (self.root / (rel_base + candidate_ext)).resolve()
                try:
                    candidate.relative_to(self.root)
                except ValueError:
                    continue
                if candidate.exists():
                    ext = candidate_ext
                    break
        rel = path_from_file_slug(slug, ext=ext)
        path = (self.root / rel).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BadInput(
                f"path traversal not allowed: {slug!r}",
                next="use simple slugs",
            ) from exc
        if must_exist and not path.exists():
            raise NotFound(
                f"{self._KIND} file {slug!r} not found on disk",
                next=(
                    f"put(kind='{self._KIND}', id='<slug>', text='...', mode='create')"
                ),
            )
        return path


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BlockSel:
    """Parsed form of the ``~…`` portion of a file-kind id.

    Three variants, discriminated by the booleans:

    - ``is_pos=True``  → ``value`` is a digit string.
    - ``is_line_range=True`` → ``line_start`` / ``line_end`` are
      1-indexed inclusive; ``value`` is the raw ``L<a>-<b>`` form
      so error messages can quote the original input.
    - Both False → ``value`` is a content-derived block slug.
    """

    value: str
    is_pos: bool = False
    is_line_range: bool = False
    line_start: int = 0
    line_end: int = 0


_INT_RE = re.compile(r"^\d+$")


def _parse_file_id(
    raw: str, *, extensions: tuple[str, ...]
) -> tuple[str, _BlockSel | None, str | None]:
    """Parse a prose-file id into ``(file_slug, block_sel, view)``.

    Accepts both slug-form and path-form (the latter is advertised
    in ``precis-files-help`` and was previously silently mis-parsed —
    MCP critic CRITICAL-C, 2026-05-02)::

        slug                            — slug-form
        slug~BLOCK                      — paragraph / block by slug
        slug~N                          — block by 0-indexed pos
        slug~L42-58                     — line range (Track A)
        slug~L42                        — single line (Track A)
        slug/raw                        — view path
        notes/meeting.md                — path-form, canonicalised
        notes/meeting.md~conclusion     — path-form with selector
        notes/meeting.md/toc            — path-form with view

    ``extensions`` is the handler's ``_EXTENSIONS`` tuple; only those
    extensions trigger path-form canonicalisation so a plaintext
    handler can't mis-parse a ``.md`` id as a plaintext file path
    (and vice versa).
    """
    s = canonicalize_path_id(raw.strip(), extensions=extensions)
    sel: _BlockSel | None = None
    view: str | None = None

    if "/" in s:
        s, _, view = s.partition("/")
        view = view.strip() or None

    if "~" in s:
        slug, _, after = s.partition("~")
        after = after.strip()
        if not after:
            raise BadInput(
                f"empty block selector in {raw!r}",
                next="slug~SLUG  or  slug~N  or  slug~L42-58",
            )
        # Track A: line range / single line (~L42-58, ~L42).
        lr = parse_line_range(after, raw_id=raw)
        if lr is not None:
            sel = _BlockSel(
                value=lr.value,
                is_line_range=True,
                line_start=lr.line_start,
                line_end=lr.line_end,
            )
            return slug, sel, view
        is_pos = bool(_INT_RE.match(after))
        sel = _BlockSel(value=after, is_pos=is_pos)
        return slug, sel, view

    return s, sel, view


# Back-compat alias — historical callers use ``_parse_pt_id``. The
# new function takes an extensions tuple so subclasses (tex, markdown)
# can canonicalise against their own extensions.
def _parse_pt_id(raw: str) -> tuple[str, _BlockSel | None, str | None]:
    """Legacy signature: assumes ``.txt`` / ``.log``. Prefer
    :func:`_parse_file_id` which accepts ``extensions=``."""
    return _parse_file_id(raw, extensions=(".txt", ".log"))


def _find_block(blocks: Sequence[Any], sel: _BlockSel) -> Any | None:
    """Find a block matching ``sel`` in ``blocks``.

    Accepts any block dataclass with ``pos`` / ``slug`` / ``line_start`` /
    ``line_end`` fields (``PlaintextBlock``, ``TexBlock``,
    :class:`precis.utils.md_parse.MdBlock`) so the same helper covers
    every prose-file subclass.

    Slug resolution order (shortest-works-first for small models):

    1. Exact slug match (the durable Track-B form).
    2. Unique prefix shorthand — ``inserted-by-probe-marker`` resolves
       to ``inserted-by-probe-marker-c33c61`` when exactly one block's
       slug starts with the query followed by ``-``. Heading slugs
       (clean) and paragraph slugs (hash-suffixed) mix in the same
       file, so a 7B caller copying the clean form of a hash-suffixed
       slug used to get NotFound; prefix shorthand unblocks them
       (MCP critic MINOR-C 2026-05-02).
    3. Multiple prefix matches → ``None``, deferring to the caller's
       ``options=`` rendering (which lists every candidate).
    """
    if sel.is_line_range:
        for b in blocks:
            if _intersects(b.line_start, b.line_end, sel.line_start, sel.line_end):
                return b
        return None
    if sel.is_pos:
        try:
            target_pos = int(sel.value)
        except ValueError:
            return None
        for b in blocks:
            if b.pos == target_pos:
                return b
        return None
    # Exact slug first.
    for b in blocks:
        if b.slug == sel.value:
            return b
    # Unique prefix shorthand — anchor on ``query + '-'`` so
    # ``section-one`` doesn't accidentally match ``section-ones``
    # (the ambient ``-`` segment boundary disambiguates).
    anchor = sel.value + "-"
    prefix_matches = [b for b in blocks if b.slug and b.slug.startswith(anchor)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def _prefix_shorthand_matches(blocks: Sequence[Any], query: str) -> list[str]:
    """Every block slug for which ``query`` is a valid prefix shorthand.

    Returned in document order so the caller's ``options=`` list
    surfaces the candidates the way they appear in the file.
    """
    anchor = query + "-"
    return [b.slug for b in blocks if b.slug and b.slug.startswith(anchor)]


def _intersects(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """True iff 1-indexed inclusive spans share at least one line."""
    return a_start <= b_end and b_start <= a_end


# ── block-lookup helpers for unified write-result formatting ─────


def _last_block(blocks: Sequence[Any]) -> Any | None:
    """Highest-pos block, or ``None`` if the list is empty."""
    if not blocks:
        return None
    return max(blocks, key=lambda b: b.pos)


def _block_at_line(blocks: Sequence[Any], line: int) -> Any | None:
    """First block whose ``meta.line_start..line_end`` contains ``line``.

    Used after an atomic write to recover the (slug, pos, lines)
    tuple of the block that landed on the edited location, so the
    response can surface it without a ``/toc`` round-trip.
    """
    for b in blocks:
        meta = getattr(b, "meta", None) or {}
        start = meta.get("line_start")
        end = meta.get("line_end")
        if start is None or end is None:
            continue
        if int(start) <= line <= int(end):
            return b
    return None


def _block_at_pos(blocks: Sequence[Any], pos: int) -> Any | None:
    """Block with exact ``pos``, or ``None``."""
    for b in blocks:
        if b.pos == pos:
            return b
    return None


def _derive_title(blocks: list[PlaintextBlock], *, fallback: str) -> str:
    """Title is the first line of the first paragraph, truncated."""
    if not blocks:
        return fallback
    first_line = blocks[0].text.splitlines()[0] if blocks[0].text else ""
    first_line = first_line.strip()
    if not first_line:
        return fallback
    if len(first_line) > 80:
        return first_line[:77] + "…"
    return first_line


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".txt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _splice_lines(
    raw: str, line_start: int, line_end: int, new_lines: list[str]
) -> str:
    """Replace 1-indexed inclusive ``[line_start, line_end]`` lines."""
    lines = raw.splitlines()
    lo = line_start - 1
    hi = line_end
    if new_lines:
        lines[lo:hi] = new_lines
    else:
        del lines[lo:hi]
        while (
            lo < len(lines)
            and lo > 0
            and not lines[lo].strip()
            and not lines[lo - 1].strip()
        ):
            del lines[lo]
    new_content = "\n".join(lines)
    if not new_content.endswith("\n"):
        new_content += "\n"
    return new_content


def _replace_lines(
    path: Path, line_start: int, line_end: int, new_lines: list[str]
) -> None:
    raw = path.read_text(encoding="utf-8")
    new_content = _splice_lines(raw, line_start, line_end, new_lines)
    _atomic_write(path, new_content)
