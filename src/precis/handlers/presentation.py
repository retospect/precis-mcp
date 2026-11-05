"""PresentationHandler — slide decks + unpublished writeups.

Slug-addressed kind. Each `pres` ref is one deck or writeup; the
body lives as ``blocks`` (one block per slide for slide decks, one
per paragraph for prose). Subtype (``slides`` / ``writeup`` /
``notes``) is carried as an open tag (``subtype:slides``) so the
closed-axis vocabulary stays empty (same shape as ``conv``).

Reads: get an overview, a block (``~N``), an inclusive range (``~N..M``),
or the body (``/full``); search across blocks. Full/range reads fit whole
slides under the byte cap and name the exact remaining range, so citation
harvesting reaches the tail without gaps or repeats. An indivisible
over-cap slide refuses with a budget remedy. LaTeXiT payloads are filtered
on reads as well as PDF extraction because existing body chunks are
append-only. Runtime can refit the loaded slide snapshot after reserving
hint bytes; only runtime applies the optional context cap, when folding
is enabled. The overview shows stored PDF metadata; ``pdf_link`` shares
the document-family signed download renderer in
:mod:`precis.handlers._doc_pdf`. Stored tags appear on the overview and
the dedicated ``tags`` view.

Writes: ``put(id='<slug>', text='<slide body>', pos=N, ...)`` is
the bridge for both human ingest (one call per slide while
running marker on a slide PDF) and inline agent capture of an
unpublished writeup. Subsequent calls with the same ``pos``
overwrite; absent ``pos`` appends. Per-block metadata can carry
``slide_index``, ``slide_title``, ``figure_refs``, etc.

Tag lists repeat the scalar operation inside one store transaction, like
numeric-ref kinds, so a missing deck cannot leave a partially tagged batch.
The runtime already repeats metadata edits with the same atomic contract.

PDF ingest uses fitz, one page per slide. Low-text pages are counted in
``meta.image_only_pages`` and shown on the overview. The explicit
``edit(args={'reextract': True})`` door queues ``pres_reextract``; it
does not run OCR. The worker preserves unchanged slide handles and only
swaps changed bodies after extraction succeeds.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis._result_budget import (
    DEFAULT_MAX_BODY_BYTES,
    _env_cap,
    body_bytes,
)
from precis.dispatch import Hub, InitError
from precis.errors import (
    BadInput,
    NotFound,
    PrecisError,
    ResultBudgetExceeded,
    Unsupported,
)
from precis.handlers._doc_pdf import pdf_overview_line, render_pdf_link
from precis.handlers._numeric_ref import BATCH_ID_CAP, _id_batch_next
from precis.handlers._paper_format import (
    _clean_inline_text,
    _latex_escape,
    bibtex_title,
)
from precis.handlers._slug_ref_shared import (
    reject_chunk_or_path_view,
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.block_selector import parse_block_selector
from precis.utils.link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.utils.match_all import fold_match_all_q
from precis.utils.next_block import render_next_section
from precis.utils.pres_text import strip_latexit
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.title_views import title_md, title_plain


class PresentationHandler(Handler):
    #: ``edit(id=[...])`` repeats the same edit per id in one transaction
    #: (gr514482); the method itself keeps its scalar-id signature.
    edit_id_list: ClassVar[str] = "repeat"
    ARGS: ClassVar[dict[str, ArgsSpec]] = {"edit": ArgsSpec({"reextract"})}

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
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        placement="corpus",
        can_own_jobs=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("pres: store required")
        self.store = hub.store
        self.hub = hub

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # First entry is the conventional default (overview). ``full``
        # renders every slide; ``bibtex`` / ``ris`` export the deck's
        # attribution for citing slides; ``links`` is the F8 graph view
        # (OPEN-ITEMS.md 🕸️ graph-completeness audit item 1).
        return ["full", "bibtex", "ris", "links", "pdf_link", "tags"]

    # ── get ─────────────────────────────────────────────────────────

    def get(
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
            if isinstance(chunk, int):
                return self._render_block(slug, ref.id, chunk)
            return self._render_full(slug, ref, pos_range=chunk)
        if effective_view == "full":
            return self._render_full(slug, ref)
        if effective_view in ("bibtex", "ris"):
            return Response(body=_format_pres_citation(ref, style=effective_view))
        if effective_view == "pdf_link":
            return render_pdf_link(self.store, ref)
        if effective_view == "tags":
            handle = handle_registry.format_handle("pres", ref.id)
            return Response(body=f"# {handle} — tags\n{self._tags_line(ref.id)}")
        if effective_view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="pres")
        if effective_view is not None:
            raise Unsupported(
                f"unknown pres view {effective_view!r}",
                next="try '/full', '/bibtex', or '~N'",
            )
        return self._render_overview(slug, ref)

    # ── search ──────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        q = fold_match_all_q(q)
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
        hits = self.store.chunks.search_chunks_fused(
            q=q,
            query_vec=None,
            kind="pres",
            scope_ref_id=scope_ref_id,
            limit=page_size,
        )
        if not hits:
            return Response(body=f"no pres blocks match {q!r}")
        total = self.store.chunks.count_chunks_lexical(
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
            handle = (
                handle_registry.try_format("pres", block.id, chunk=True)
                or f"{slug}~{block.ord}"
            )
            lines.append(f"\n## {handle}  (score={score:.4f})")
            lines.append(f"_{title_md(ref.title)}_")
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
        triples = self.store.chunks.search_chunks_fused(
            q=q,
            query_vec=None,
            kind="pres",
            limit=page_size,
        )
        return block_hits_to_search_hits(triples, kind="pres", excerpt=160)

    # ── put: per-slide/per-paragraph append ─────────────────────────

    def put(
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

        existing = self.store.chunks.list_chunks_for_ref(ref.id)
        if pos is None:
            target_pos = (existing[-1].ord + 1) if existing else 0
            replace = False
        else:
            target_pos = int(pos)
            replace = any(b.ord == target_pos for b in existing)

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
                inserted = self.store.chunks.insert_chunks(
                    ref.id,
                    [ChunkInsert(ord=target_pos, text=body, meta=block_meta)],
                    conn=conn,
                )
            verb = "overwrote"
        else:
            inserted = self.store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=target_pos, text=body, meta=block_meta)],
            )
            verb = "created + appended" if created else "appended"
        assert inserted, "insert_chunks returned no rows"
        handle = (
            handle_registry.try_format("pres", inserted[0].id, chunk=True)
            or f"{slug}~{inserted[0].ord}"
        )
        return Response(
            body=f"{verb} {handle}"
            + (f" (subtype={subtype!r})" if (created and subtype) else "")
        )

    # ── edit: attribution metadata (for citing slides) ──────────────

    def edit(
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        authors: Any = None,
        venue: str | None = None,
        date: str | None = None,
        url: str | None = None,
        note: str | None = None,
        bibtex_type: str | None = None,
        meta: dict[str, Any] | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Edit a deck's attribution metadata so slides can be cited.

        pres has no first-class ``authors`` / ``year`` columns (those
        are paper-only), so attribution lives in ``meta``: ``authors``
        (a list of name strings), ``venue`` (BibTeX ``howpublished``),
        ``date``, ``url``, ``note``, and ``bibtex_type``
        (``misc`` / ``unpublished`` / ``inproceedings``). Body blocks
        are only refreshed by the explicit reextract job door.

        Only the fields passed are changed; a ``None`` field is left
        as-is (the web form's "leave blank to keep" contract). Passing
        an explicit empty string clears a scalar field, and
        ``authors=[]`` clears the author list. Feeds
        ``get(view='bibtex')``.
        """
        if id is None:
            raise BadInput(
                "edit(kind='pres') needs a deck id",
                next="edit(kind='pres', id='pr<id>', args={'reextract': True})",
            )
        address = (
            handle_registry.format_handle("pres", id) if isinstance(id, int) else id
        )
        ref = resolve_live_slug_ref(self.store, kind="pres", id=address)

        if args and "reextract" in args:
            if args["reextract"] is not True:
                raise BadInput(
                    "reextract must be true",
                    next="edit(kind='pres', id='pr<id>', args={'reextract': True})",
                )
            if any(
                value is not None
                for value in (title, authors, venue, date, url, note, bibtex_type, meta)
            ):
                raise BadInput(
                    "reextract cannot be combined with metadata edits",
                    next="edit metadata in a separate call, then request reextract",
                )
            return self._reextract(ref)

        patch: dict[str, Any] = {}
        if authors is not None:
            patch["authors"] = _normalize_pres_authors(authors)
        for key, val in (
            ("venue", venue),
            ("date", date),
            ("url", url),
            ("note", note),
            ("bibtex_type", bibtex_type),
        ):
            if val is not None:
                patch[key] = str(val).strip()
        if meta:
            patch.update(meta)

        new_title = title.strip() if isinstance(title, str) and title.strip() else None

        if not patch and new_title is None:
            raise BadInput(
                "edit(kind='pres') needs at least one field to change",
                next=(
                    "edit(kind='pres', id='<slug>', venue='...', "
                    "date='2001', authors=['Payne, M. C.'])"
                ),
            )

        self.store.update_ref(ref.id, title=new_title, meta_patch=patch or None)
        handle = handle_registry.format_handle("pres", ref.id)
        changed = sorted([*(["title"] if new_title else []), *patch.keys()])
        body = f"updated {handle}: {', '.join(changed)}"
        body += render_next_section(
            [
                (f"get(id='{handle}', view='bibtex')", "get the BibTeX entry"),
                (f"get(id='{handle}')", "see the overview"),
            ]
        )
        return Response(body=body)

    def _reextract(self, ref: Any) -> Response:
        """Queue a fitz-only refresh; never drop the body at submit time."""
        from precis.workers.job_types import pres_reextract

        handle = handle_registry.format_handle("pres", ref.id)
        sha = ref.pdf_sha256
        if not sha or self.store.pdf_missing(sha):
            raise BadInput(
                f"{handle} has no stored source PDF available to re-extract",
                next="ingest the source PDF as a presentation, or restore its "
                "stored file into the corpus, then retry reextract",
            )
        response = self.hub.sibling("job").put(
            job_type=pres_reextract.JOB_TYPE,
            executor="job_inproc",
            parent_id=ref.id,
            params={"ref_id": ref.id, "pdf_sha256": sha},
            idem_key=pres_reextract.idem_key(ref.id, sha),
        )
        state = "already queued" if getattr(response, "reused", False) else "queued"
        return Response(
            body=f"{state}: pres_reextract for {handle}; "
            f"watch get(kind='job', id={response.ref_id}). "
            "Reads the stored PDF with fitz only; no OCR or model calls. "
            "A truly image-only deck stays textless: reextract refreshes its "
            "low-text page count only. Unchanged slides keep their handles; "
            "changed slides replace the body and remove old chunk links. "
            "The current body survives extraction failure."
        )

    # ── delete / tag / link ──────────────────────────────────────────

    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        """Retire the deck; the store's trigger journals the transition."""
        if id is None:
            raise BadInput(
                "delete(kind='pres') needs id=",
                next="delete(kind='pres', id='pr<id>')",
            )
        if isinstance(id, int):
            # Web routes pass ref IDs; a numeric slug may name a different deck.
            id = handle_registry.format_handle("pres", id)
        _slug, ref_id = self._resolve_pres_slug(id)
        self.store.retire_ref(ref_id)
        handle = handle_registry.format_handle("pres", ref_id)
        return Response(body=f"deleted {handle} (soft-deleted)")

    def _resolve_pres_slug(self, id: str | int) -> tuple[str, int]:
        if isinstance(id, int):
            id = handle_registry.format_handle("pres", id)
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

    def tag(
        self,
        *,
        id: Any = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        require_tag_ops("pres", add, remove)
        if isinstance(id, list):
            return self._tag_batch(id, add=add, remove=remove)
        if not isinstance(id, (str, int)) or isinstance(id, bool):
            raise BadInput(
                "tag(kind='pres') needs a slug, handle or numeric id",
                next="tag(kind='pres', id='pr<id>', add=['topic:slides'])",
            )
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

    def _tag_batch(
        self,
        ids: list[Any],
        *,
        add: list[str] | None,
        remove: list[str] | None,
    ) -> Response:
        """Apply the same tags to distinct decks, with atomic per-id receipts."""
        if not ids:
            raise BadInput(
                "id=[] is empty",
                next="tag(kind='pres', id='pr<id>', add=['topic:slides'])",
            )
        if len(ids) > BATCH_ID_CAP:
            raise BadInput(
                f"id=[...] takes at most {BATCH_ID_CAP} ids, got {len(ids)}",
                next=_id_batch_next("tag", "pres", ids),
            )
        lines: list[str] = []
        seen: set[int] = set()
        with self.store.atomic():
            for raw in ids:
                try:
                    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
                        raise BadInput(
                            "each pres id must be a slug, handle or integer",
                            next="tag(kind='pres', id=['pr<id>', '<slug>'], add=['topic:slides'])",
                        )
                    _, ref_id = self._resolve_pres_slug(raw)
                    if ref_id in seen:
                        continue
                    seen.add(ref_id)
                    response = self.tag(id=ref_id, add=add, remove=remove)
                except PrecisError as exc:
                    exc.cause = (
                        f"{raw}: {exc.cause} — batch of {len(ids)} "
                        "rolled back, nothing applied"
                    )
                    exc.args = (exc.cause,)
                    raise
                handle = handle_registry.format_handle("pres", ref_id)
                lines.append(f"{handle}: {response.body.splitlines()[0]}")
        return Response(
            body=f"tagged {len(seen)} pres in one transaction\n" + "\n".join(lines)
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
            hub=getattr(self, "hub", None),
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

    def _tags_line(self, ref_id: int) -> str:
        tags = ", ".join(str(tag) for tag in self.store.tags_for(ref_id))
        return f"tags: {tags or '(none)'}"

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
        n_blocks = self.store.chunks.count_chunks(ref.id)
        meta = ref.meta or {}
        handle = handle_registry.format_handle("pres", ref.id)
        lines = [f"# {handle}", f"_{title_md(ref.title)}_"]
        authors = meta.get("authors") or []
        if authors:
            lines.append("by " + ", ".join(str(a) for a in authors))
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
        lines.append(pdf_overview_line(self.store, ref))
        if "image_only_pages" in meta:
            from precis.ingest.pres import MIN_PRES_TEXT_CHARS

            count = meta["image_only_pages"]
            lines.append(
                f"text: {count} pages have no or little extractable text "
                f"(<{MIN_PRES_TEXT_CHARS} non-whitespace characters)."
                + render_next_section(
                    [
                        (
                            f"get(id='{handle}', view='pdf_link')"
                            if count
                            else f"get(id='{handle}/full')",
                            "inspect the stored PDF; OCR is needed to recover "
                            "image text."
                            if count
                            else "read the slides.",
                        ),
                        (
                            f"edit(kind='pres', id='{handle}', "
                            "args={'reextract': True})",
                            "Refresh; fitz only, no OCR.",
                        ),
                    ]
                )
            )
        lines.append(self._tags_line(ref.id))
        body = "\n".join(lines)
        next_steps = [
            (f"/pres/{ref.id}", "open the web reader"),
            (f"get(id='{handle}/full')", "read the whole body"),
            (f"get(id='{handle}~0')", "read the first block"),
            (
                f"search(kind='pres', q='...', scope='{handle}')",
                "search this presentation",
            ),
        ]
        if ref.pdf_sha256:
            next_steps.append(
                (f"get(id='{handle}', view='pdf_link')", "download the stored PDF")
            )
        # Offer the citation once any attribution is filled in.
        if authors or venue or date:
            next_steps.append(
                (f"get(id='{handle}', view='bibtex')", "get the BibTeX entry")
            )
        body += render_next_section(next_steps)
        return Response(body=body)

    def _render_full(
        self,
        slug: str,
        ref: Any,
        *,
        pos_range: tuple[int, int] | None = None,
    ) -> Response:
        blocks = self.store.chunks.list_chunks_for_ref(ref.id, pos_range=pos_range)
        handle = handle_registry.format_handle("pres", ref.id)
        if not blocks:
            if pos_range is not None:
                lo, hi = pos_range
                all_blocks = self.store.chunks.list_chunks_for_ref(ref.id)
                bounds = (
                    f"{ref.slug} has {len(all_blocks)} blocks, "
                    f"valid: ~{all_blocks[0].ord}..{all_blocks[-1].ord}"
                    if all_blocks
                    else f"{ref.slug} has no body blocks"
                )
                raise NotFound(
                    f"no blocks in {ref.slug} for range ~{lo}..{hi} — {bounds}",
                    next=f"get(kind='pres', id='{handle}/full')",
                )
            return Response(body=f"{slug}: no blocks")
        scope = "full" if pos_range is None else f"~{pos_range[0]}..{pos_range[1]}"
        header = f"# {handle} - {scope}\n_{title_md(ref.title)}_"
        sections = []
        for b in blocks:
            label = "slide" if b.chunk_kind == "pres_slide" else "block"
            sections.append(f"## {label} ~{b.ord}\n{strip_latexit(b.text)}")
        body = "\n\n".join([header, *sections]).rstrip()
        total = body_bytes(body)

        def fit(cap: int) -> str:
            if total <= cap:
                return body
            # Runtime may call again after reserving hints and applying
            # its optional context cap. Reuse the original slide snapshot.
            head = header
            fitted: str | None = None
            for i, section in enumerate(sections[:-1]):
                head += "\n\n" + section
                shown = body_bytes(head)
                next_call = f"get(kind='pres', id='{handle}~{blocks[i + 1].ord}..{blocks[-1].ord}')"
                candidate = (
                    head
                    + f"\n\n… truncated: showing {shown} of {total} bytes"
                    + f"\nnext: {next_call}"
                )
                if body_bytes(candidate) > cap:
                    # Keep the exact continuation when byte counts would
                    # crowd out a whole slide (e.g. a required id hint).
                    candidate = head + f"\n\n… truncated:\nnext: {next_call}"
                if body_bytes(candidate) <= cap:
                    fitted = candidate
                elif shown > cap:
                    break
            if fitted is not None:
                return fitted
            raise ResultBudgetExceeded(
                f"pres {handle}: block ~{blocks[0].ord} and continuation do not fit "
                f"under the {cap}-byte result budget",
                next=(
                    "increase PRECIS_MAX_BODY_BYTES (and "
                    "PRECIS_TOOL_RESULT_CONTEXT_BYTES if set), then retry "
                    f"get(kind='pres', id='{handle}~{blocks[0].ord}..{blocks[-1].ord}')"
                ),
            )

        return Response(
            body=fit(_env_cap("PRECIS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)),
            budget_fit=fit,
        )

    def _render_block(self, slug: str, ref_id: int, pos: int) -> Response:
        blocks = self.store.chunks.list_chunks_for_ref(ref_id, pos_range=(pos, pos))
        if not blocks:
            raise NotFound(
                f"no block at ~{pos} in pres {slug!r}",
                next=f"get(kind='pres', id='{slug}/full')",
            )
        b = blocks[0]
        label = "slide" if b.chunk_kind == "pres_slide" else "block"
        chunk_handle = handle_registry.chunk_head_label("pres", b.id, pos, slug)
        return Response(body=f"# {chunk_handle} ({label})\n{strip_latexit(b.text)}")


def _parse_pres_id(raw: str) -> tuple[str, int | tuple[int, int] | None, str | None]:
    """Parse pres ids, sharing paper's single/range selector grammar."""
    if "~" in raw:
        slug, _, sel = raw.partition("~")
        selector = sel.split("/", 1)[0]
        lo, hi = parse_block_selector(selector, raw=raw)
        # Keep ~N's existing single-block heading; ~N..N is a range read.
        chunk = lo if re.fullmatch(r"-?\d+", selector) else (lo, hi)
        return slug, chunk, None
    if "/" in raw:
        slug, _, view = raw.partition("/")
        return slug, None, view
    return raw, None, None


def _normalize_pres_authors(raw: Any) -> list[str]:
    """Coerce an authors input to a list of clean name strings.

    Accepts a list (of name strings or ``{name|family|given}`` dicts) or
    a single string with newline / semicolon separators. Commas are
    *not* split on, so a BibTeX-style ``Family, Given`` name stays whole
    — one author per line.
    """
    if raw is None:
        return []
    parts: list[str] = []
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[\n;]+", raw)]
    elif isinstance(raw, (list, tuple)):
        for a in raw:
            if isinstance(a, str):
                parts.append(a.strip())
            elif isinstance(a, dict):
                name = a.get("name") or " ".join(
                    str(x) for x in (a.get("given"), a.get("family")) if x
                )
                parts.append(str(name).strip())
    else:
        parts = [str(raw).strip()]
    return [p for p in parts if p]


def _format_pres_citation(ref: Any, *, style: str) -> str:
    """Render a deck's attribution as BibTeX (``@misc`` by default) or RIS.

    Reads the ``meta`` attribution keys written by
    :meth:`PresentationHandler.edit`. Reuses the paper formatter's
    ``_clean_inline_text`` / ``_latex_escape`` primitives so the escaping
    matches the paper BibTeX view.
    """
    meta = ref.meta or {}
    slug = ref.slug or "???"
    # gr515227: stored titles are raw — BibTeX takes the md view, RIS
    # the plain view (same split as _render_citation).
    # gr515709: ``bibtex_title`` (not title_md + escape) so md emphasis
    # becomes \emph and math islands get the safe-macro allowlist.
    title = title_plain(ref.title or "")
    authors = [_clean_inline_text(str(a)) for a in (meta.get("authors") or [])]
    authors = [a for a in authors if a]
    venue = _clean_inline_text(str(meta.get("venue") or ""))
    date = str(meta.get("date") or "")
    year = date[:4] if date[:4].isdigit() else ""
    url = str(meta.get("url") or "").strip()
    note = _clean_inline_text(str(meta.get("note") or ""))
    entry_type = str(meta.get("bibtex_type") or "").strip() or "misc"

    if style == "ris":
        out = ["TY  - SLIDE"]
        if title:
            out.append(f"TI  - {title}")
        for a in authors:
            out.append(f"AU  - {a}")
        if year:
            out.append(f"PY  - {year}")
        if venue:
            out.append(f"PB  - {venue}")
        if url:
            out.append(f"UR  - {url}")
        if note:
            out.append(f"N1  - {note}")
        out.append("ER  - ")
        return "\n".join(out)

    lines = [f"@{entry_type}{{{slug},"]
    title_bib = bibtex_title(ref.title or "")
    if title_bib:
        lines.append(f"  title = {{{title_bib}}},")
    if authors:
        lines.append(
            f"  author = {{{' and '.join(_latex_escape(a) for a in authors)}}},"
        )
    if year:
        lines.append(f"  year = {{{year}}},")
    if venue:
        lines.append(f"  howpublished = {{{_latex_escape(venue)}}},")
    if url:
        lines.append(f"  url = {{{url}}},")
    if note:
        lines.append(f"  note = {{{_latex_escape(note)}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"
