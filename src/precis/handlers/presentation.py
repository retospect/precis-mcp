"""PresentationHandler — slide decks + unpublished writeups.

Slug-addressed kind. Each `pres` ref is one deck or writeup; the
body lives as ``blocks`` (one block per slide for slide decks, one
per paragraph for prose). Subtype (``slides`` / ``writeup`` /
``notes``) is carried as an open tag (``subtype:slides``) so the
closed-axis vocabulary stays empty (same shape as ``conv``).

Reads: get an overview, get a specific block (``~N``), get the
whole rendered body (``/full``), search across blocks.

Writes: ``put(id='<slug>', text='<slide body>', pos=N, ...)`` is
the bridge for both human ingest (one call per slide while
running marker on a slide PDF) and inline agent capture of an
unpublished writeup. Subsequent calls with the same ``pos``
overwrite; absent ``pos`` appends. Per-block metadata can carry
``slide_index``, ``slide_title``, ``figure_refs``, etc.

PDF ingest (auto-chunk a slide PDF into per-slide blocks) is a
follow-up — see ``cluster/playbooks/27-extract-watch.yml`` for
the watch-and-extract pattern the new_pres drop folder will reuse.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.handlers._slug_ref_shared import (
    reject_chunk_or_path_view,
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits


class PresentationHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pres",
        title="Presentation",
        description=(
            "Slide deck or unpublished writeup. Slug-addressed; "
            "one block per slide (or paragraph). Subtype via "
            "``subtype:slides|writeup|notes`` open tag. Body is "
            "put-on-write — call per slide / paragraph during "
            "ingest."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("pres: store required")
        self.store = hub.store

    # ── get ─────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_list()

        slug, chunk, path_view = _parse_pres_id(str(id))
        ref = resolve_live_slug_ref(self.store, kind="pres", id=slug)

        effective_view = path_view or view
        if chunk is not None:
            return self._render_block(slug, ref.id, chunk)
        if effective_view == "full":
            return self._render_full(slug, ref)
        if effective_view is not None:
            raise Unsupported(
                f"unknown pres view {effective_view!r}",
                next="try '/full' or '~N'",
            )
        return self._render_overview(slug, ref)

    # ── search ──────────────────────────────────────────────────────

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
                next="search(kind='pres', q='your query')",
            )
        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="pres",
                id=scope,
                next_hint="search(kind='pres', q='...')",
            )
            scope_ref_id = scope_ref.id
        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=None,
            kind="pres",
            scope_ref_id=scope_ref_id,
            limit=page_size,
        )
        if not hits:
            return Response(body=f"no pres blocks match {q!r}")
        total = self.store.count_blocks_lexical(
            q=q, kind="pres", scope_ref_id=scope_ref_id
        )
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block match",
                query=q,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "?"
            preview = (block.text[:160] + "…") if len(block.text) > 160 else block.text
            lines.append(f"\n## {slug}~{block.pos}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        if not (q and q.strip()):
            return []
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=None,
            kind="pres",
            limit=page_size,
        )
        return block_hits_to_search_hits(triples, kind="pres", excerpt=160)

    # ── put: per-slide/per-paragraph append ─────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        pos: int | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        ref_meta: dict[str, Any] | None = None,
        subtype: str | None = None,
        chunk_kind: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create-or-append a block on a pres ref.

        First call mints the ref using ``title`` (or a slug
        fallback) and ``ref_meta`` (authors, venue, date,
        source_pdf, slide_count, …). ``subtype`` ('slides' /
        'writeup' / 'notes') is recorded as the open tag
        ``subtype:<value>`` on creation.

        ``pos`` controls position: omit to append (next contiguous
        slot), pass an int for an explicit slot — useful when
        ingesting a deck out-of-order (slide N before slide N-1).
        Re-putting at an existing ``pos`` overwrites that block.

        ``chunk_kind`` defaults to ``pres_slide`` (the
        migration-seeded vocabulary) for slide decks and should be
        passed as ``paragraph`` for writeup-style ingestion so the
        cross-kind renderer doesn't label paragraphs as slides.
        """
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='pres') requires id= (the pres slug)",
                next=(
                    "put(kind='pres', id='2026-06-talk-foo', "
                    "text='...slide body...', pos=0, "
                    "subtype='slides', title='Talk: Foo')"
                ),
            )
        if text is None or not str(text).strip():
            raise BadInput(
                "put(kind='pres') requires text= (the block body)",
                next=("put(kind='pres', id='<slug>', text='...', pos=N)"),
            )
        slug = str(id).strip()
        body = str(text)

        ref = self.store.get_ref(kind="pres", id=slug)
        created = False
        if ref is None:
            ref_title = (title or slug).strip() or slug
            ref = self.store.insert_ref(
                kind="pres",
                slug=slug,
                title=ref_title,
                meta=dict(ref_meta or {}),
            )
            if subtype:
                apply_tag_ops(
                    self.store,
                    "pres",
                    ref.id,
                    tags=[f"subtype:{subtype}"],
                    untags=None,
                )
            created = True
        elif subtype is not None:
            # Subtype is a creation-time setting; ignore on update so
            # an ingester replaying slides doesn't accidentally retag.
            pass

        existing = self.store.list_blocks_for_ref(ref.id)
        if pos is None:
            target_pos = (existing[-1].pos + 1) if existing else 0
            replace = False
        else:
            target_pos = int(pos)
            replace = any(b.pos == target_pos for b in existing)

        block_meta: dict[str, Any] = dict(meta or {})
        # chunk_kind: pres_slide for decks, paragraph for prose.
        # Default to pres_slide because we expect the slide-deck case
        # most often — writeup ingest can override.
        block_meta.setdefault("chunk_kind", chunk_kind or "pres_slide")

        if replace:
            # Delete-then-insert via the replace=False path on a single
            # ref/pos pair. The cleanest available primitive is to delete
            # the matching chunk row, then insert. The store doesn't
            # expose a single-block delete, so we use a small SQL update
            # at the application layer.
            with self.store.tx() as conn:
                conn.execute(
                    "DELETE FROM chunks WHERE ref_id = %s AND ord = %s",
                    (ref.id, target_pos),
                )
                inserted = self.store.insert_blocks(
                    ref.id,
                    [BlockInsert(pos=target_pos, text=body, meta=block_meta)],
                    conn=conn,
                )
            verb = "overwrote"
        else:
            inserted = self.store.insert_blocks(
                ref.id,
                [BlockInsert(pos=target_pos, text=body, meta=block_meta)],
            )
            verb = "created + appended" if created else "appended"
        assert inserted, "insert_blocks returned no rows"
        return Response(
            body=f"{verb} {slug}~{inserted[0].pos}"
            + (f" (subtype={subtype!r})" if (created and subtype) else "")
        )

    # ── tag / link ──────────────────────────────────────────────────

    def _resolve_pres_slug(self, id: str | int) -> tuple[str, int]:
        slug, chunk, path_view = _parse_pres_id(str(id))
        reject_chunk_or_path_view(
            kind="pres",
            slug=slug,
            sel=chunk,
            path_view=path_view,
            selector_noun="block selector",
        )
        ref = resolve_live_slug_ref(
            self.store,
            kind="pres",
            id=slug,
            next_hint="search(kind='pres', q='...') to find existing slugs",
        )
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        require_tag_ops("pres", add, remove)
        slug, ref_id = self._resolve_pres_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "pres", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="pres",
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
        target = require_link_target("pres", target)
        validate_link_mode(mode)
        slug, ref_id = self._resolve_pres_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="pres",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── render helpers ──────────────────────────────────────────────

    def _render_list(self) -> Response:
        return render_slug_ref_list(
            self.store,
            kind="pres",
            label_plural="presentation(s)",
            limit=20,
            empty_body="no presentations recorded yet",
            empty_next=[
                (
                    "put(kind='pres', id='<slug>', text='...', "
                    "subtype='slides', title='...')",
                    "ingest a new deck or writeup",
                ),
            ],
        )

    def _render_overview(self, slug: str, ref: Any) -> Response:
        n_blocks = self.store.count_blocks(ref.id)
        meta = ref.meta or {}
        lines = [f"# {slug}", f"_{ref.title}_"]
        venue = meta.get("venue")
        date = meta.get("date")
        if venue or date:
            lines.append(
                "venue: "
                + (str(venue) if venue else "?")
                + (f" — {date}" if date else "")
            )
        lines.append("")
        lines.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='pres', id='{slug}/full')",
                    "read the whole body",
                ),
                (f"get(kind='pres', id='{slug}~0')", "read the first block"),
                (
                    f"search(kind='pres', q='...', scope='{slug}')",
                    "search this presentation",
                ),
            ]
        )
        return Response(body=body)

    def _render_full(self, slug: str, ref: Any) -> Response:
        blocks = self.store.list_blocks_for_ref(ref.id)
        if not blocks:
            return Response(body=f"{slug}: no blocks")
        lines = [f"# {slug} - full", f"_{ref.title}_", ""]
        for b in blocks:
            label = "slide" if b.chunk_kind == "pres_slide" else "block"
            lines.append(f"## {label} ~{b.pos}")
            lines.append(b.text)
            lines.append("")
        return Response(body="\n".join(lines).rstrip())

    def _render_block(self, slug: str, ref_id: int, pos: int) -> Response:
        blocks = self.store.list_blocks_for_ref(ref_id, pos_range=(pos, pos))
        if not blocks:
            raise NotFound(
                f"no block at ~{pos} in pres {slug!r}",
                next=f"get(kind='pres', id='{slug}/full')",
            )
        b = blocks[0]
        label = "slide" if b.chunk_kind == "pres_slide" else "block"
        return Response(body=f"# {slug}~{pos} ({label})\n{b.text}")


def _parse_pres_id(raw: str) -> tuple[str, int | None, str | None]:
    """Parse pres ids: ``slug``, ``slug~N``, ``slug/full``."""
    if "~" in raw:
        slug, _, sel = raw.partition("~")
        try:
            pos = int(sel.split("/", 1)[0])
        except ValueError as exc:
            raise BadInput(
                f"unparseable block selector after ~: {sel!r}",
                next="use '~N' for a single block",
            ) from exc
        return slug, pos, None
    if "/" in raw:
        slug, _, view = raw.partition("/")
        return slug, None, view
    return raw, None, None
