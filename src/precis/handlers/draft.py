"""DraftHandler — the editable document kind.

A `draft` is a slug-addressed ref whose body chunks are mutable in
structure (reorder/reparent) and text — the one deliberate exception
to the append-only body-chunk invariant. The document lives IN the
chunk store so all chunk infra (embed, keywords, search, TOC, windows)
works on it for free; safety comes from four orthogonal columns:
addresses are the immutable ``chunk_id`` (hierarchy in
``parent_chunk_id``, order in fractional ``pos``), so a move never
touches text, and a text edit bumps ``content_sha`` so derived rows
re-derive instead of going stale. The handler wraps the
:class:`~precis.store._draft_ops.DraftStore` store ops (``store.drafts``)
behind the existing seven verbs — **no new verbs**:

- ``put``   — create a draft (`project=`, born with a title heading) or
  add a chunk (`chunk_kind=`, `text=`, placed by `at=`).
- ``get``   — list drafts (no id), a draft's outline (`id='<slug>'`), or
  a chunk verbatim with a relative window (`id='dc<id>'`, `dc<id>-2..3`).
- ``edit``  — change a chunk's text (`text=`) or move it (`move=`).
- ``delete``— soft-retire a chunk (`mode='cascade'|'promote'` for a
  heading with children).

Chunks are addressed by the computed ``dc<chunk_id>`` handle (the legacy ``¶<base58>`` still resolves during the transition); the draft
itself by its slug (the universal ``id=``). See ``precis-draft-help``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.draft.scaffolds import SCAFFOLDS as _SCAFFOLDS
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import toon
from precis.handlers import _draft_lint
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    format_link_tag_ack,
    require_link_target,
    validate_link_mode,
)
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._draft_ops import content_sha
from precis.utils import draft_regex, handle_registry
from precis.utils.authors import to_author_dicts
from precis.utils.edit_resolve import format_unified_diff, normalize_dry_run
from precis.utils.embed_query import query_vec_for
from precis.utils.table_data import (
    find_replace_cells,
    normalize_table,
    set_cell,
    table_to_markdown,
)
from precis.workers.working_set import Extent

log = logging.getLogger(__name__)

# A bare draft chunk address: the universal handle ``dc<chunk_id>``
# or the legacy the draft editable-document model ``¶<base58>``. Relative navigation (``^`` / ``+N`` /
# ``-lo..hi``) is parsed separately via ``handle_registry.parse_relative``.
_CHUNK_ADDR = re.compile(r"^(?:dc(?P<cid>\d+)|¶(?P<h>[A-Za-z0-9]+))$")

#: Recognises a draft chunk address — bare or with a relative-nav
#: operator (``^``/``+``/``-``/``..``) — used to tell a chunk address from a
#: draft slug in ``get`` / ``search``.
_DRAFT_CHUNK_ADDR_RE = re.compile(r"^(?:dc\d+|¶[A-Za-z0-9]+)(?:[+\-^].*|\.\..*)?$")

#: Relations an owning *process* (a quest today, per its module
#: docstring any ref) uses to mark a ``draft`` as its machine-managed
#: body — mirrors ``precis.quest.dossier``'s private ``_RELATION`` /
#: ``_PAPER_RELATION`` (duplicated, not imported: the guard below is
#: enforced independently of quest internals, at the agent-facing
#: handler boundary, so it can't be defeated by anything that changes
#: on the quest side). A draft that is the SOURCE of either link
#: (``draft --dossier-of/paper-of--> owner``) is off-limits to
#: ``put``/``edit``/``delete`` through this handler — see
#: :meth:`DraftHandler._refuse_if_machine_owned` for why: a generic
#: draft-hygiene todo once executed against a quest dossier through
#: this exact surface, retiring its narrative AND its pinned ledger
#: chunk and silently losing the ledger's whole attempt tree
#: (quest 202469 / dossier 202546, Aug 2026).
_DOSSIER_RELATION = "dossier-of"
_PAPER_RELATION = "paper-of"
_MACHINE_OWNED_RELATIONS: tuple[str, ...] = (_DOSSIER_RELATION, _PAPER_RELATION)


def _is_draft_chunk_addr(s: str) -> bool:
    """True iff ``s`` addresses a draft chunk (``dc<id>`` / ``¶<base58>``,
    optionally with a relative operator)."""
    return bool(_DRAFT_CHUNK_ADDR_RE.match(s.strip()))


#: Job status → short display label for :func:`_summarize_job_counts`
#: (mirrors the STATUS closed-vocab job lifecycle: queued → submitted →
#: running → succeeded|failed|cancelled|cancel_requested). Insertion
#: order is the *source* status order, not the display order — see
#: ``_JOB_STATUS_LABEL_ORDER`` below for the latter (worth-a-look
#: statuses first: ok, failed, running, …).
_JOB_STATUS_LABELS: dict[str, str] = {
    "succeeded": "ok",
    "failed": "failed",
    "running": "running",
    "queued": "queued",
    "submitted": "submitted",
    "cancel_requested": "cancel_requested",
    "cancelled": "cancelled",
}

#: Display-label render order, deduped by first occurrence (several
#: statuses could in principle collapse to the same label). Must be
#: built from ``_JOB_STATUS_LABELS.values()``, not its keys — the keys
#: are raw statuses ("succeeded"), the labels are what actually lands
#: in the ``counts`` dict ("ok").
_JOB_STATUS_LABEL_ORDER: tuple[str, ...] = tuple(
    dict.fromkeys(_JOB_STATUS_LABELS.values())
)


def _summarize_job_counts(jobs: tuple[tuple[int, str], ...]) -> str:
    """Collapse a todo's child-job list to per-status counts.

    The draft outline's "Work in progress" block used to spell out
    every job (``job:187049 succeeded, job:187242 succeeded, …``) —
    for a todo with 20 retries that's 20 comma-joined entries per line,
    which alone pushed the outline into pagination (gr192827 item 3).
    Callers care about the *shape* of the retry history (how many
    landed, how many are still stuck), not each job id, so this
    renders ``"5 ok / 1 failed / 1 running"`` instead.
    """
    if not jobs:
        return ""
    counts: dict[str, int] = {}
    for _job_id, status in jobs:
        label = _JOB_STATUS_LABELS.get(status, status)
        counts[label] = counts.get(label, 0) + 1
    ordered = [
        f"{counts[label]} {label}"
        for label in _JOB_STATUS_LABEL_ORDER
        if label in counts
    ]
    # Any status outside the known lifecycle (future vocab addition)
    # still renders, just after the recognised ones, in first-seen order.
    extra = [
        f"{n} {label}"
        for label, n in counts.items()
        if label not in _JOB_STATUS_LABEL_ORDER
    ]
    return " / ".join(ordered + extra)


#: A figure's origin class — drives the clearance gate. ``original``
#: is ours; ``own_graph`` is generated from data (ships a data supplement);
#: ``third_party`` is reused under a publisher permission (carries the paper-trail).
_FIGURE_ORIGINS = ("original", "own_graph", "third_party")

#: magic-byte → mime sniff for a pasted image when ``mime=`` is omitted.
_MAGIC_MIME: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


#: A 1×1 transparent PNG, the deferred-image placeholder for a computed graph
#: figure — there's always a `chunk_blobs` row so the reader/export
#: never hit a missing blob; the render pass overwrites it with the real chart.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _looks_like_svg(raw: bytes) -> bool:
    """True for SVG bytes (the ``blob``-SVG medium). SVG is
    text/XML, not magic-byte sniffable, so peek at the leading window: a
    ``<svg`` root, or an XML prolog followed by an ``<svg`` element."""
    head = raw.lstrip()
    if head[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM
        head = head[3:].lstrip()
    low = head[:256].lower()
    return low.startswith(b"<svg") or (
        low.startswith(b"<?xml") and b"<svg" in raw[:4096].lower()
    )


def _sniff_mime(raw: bytes) -> str:
    """Best-effort image mime from magic bytes; WEBP needs the RIFF check;
    SVG is sniffed from its XML markup."""
    for sig, mime in _MAGIC_MIME:
        if raw.startswith(sig):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if _looks_like_svg(raw):
        return "image/svg+xml"
    return "application/octet-stream"


def _sanitize_svg_bytes(raw: bytes) -> bytes:
    """Compile-check + sanitize SVG figure bytes before they land in
    ``chunk_blobs`` (the figure medium axis — SVG is a trust boundary: it can carry
    ``<script>`` / ``on*`` handlers / ``javascript:`` hrefs). Reuses the figure
    kind's sanitizer so the rule is single-sourced. Raises ``BadInput`` on
    non-UTF-8 or unparseable markup."""
    from precis.figure.svg import SvgError, parse_error, sanitize_svg

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadInput("an SVG figure must be UTF-8 text") from exc
    err = parse_error(text)
    if err is not None:
        raise BadInput(f"invalid SVG figure: {err}")
    try:
        return sanitize_svg(text).encode("utf-8")
    except SvgError as exc:  # pragma: no cover — parse_error already guards
        raise BadInput(f"invalid SVG figure: {exc}") from exc


_AUTHORING_ON = frozenset({"on", "true", "1", "yes", "enable", "enabled"})
_AUTHORING_OFF = frozenset({"off", "false", "0", "no", "disable", "disabled"})


def _coerce_authoring(v: bool | str) -> bool:
    """Coerce ``edit(kind='draft', authoring=…)`` to a bool. A real bool
    passes through; a string is matched case/whitespace-insensitively
    against on/off synonyms; anything else is rejected."""
    if isinstance(v, bool):
        return v
    key = str(v).strip().lower()
    if key in _AUTHORING_ON:
        return True
    if key in _AUTHORING_OFF:
        return False
    raise BadInput(
        f"authoring={v!r} not understood",
        next="edit(kind='draft', id=<slug>, authoring='on')  # or 'off' / True / False",
    )


def _coerce_word_target(raw: dict[str, Any]) -> tuple[int, int] | None:
    """Validate an ``edit(word_target=…)`` payload → ``(min, max)`` or
    ``None`` (clear). ``{}`` / both-bounds-absent clears; a present bound
    must be a non-negative int and ``min <= max``."""
    if not raw:
        return None
    lo_raw = raw.get("min")
    hi_raw = raw.get("max")
    if lo_raw is None and hi_raw is None:
        return None
    try:
        lo = int(lo_raw) if lo_raw is not None else 0
        hi = int(hi_raw) if hi_raw is not None else 10**9
    except (TypeError, ValueError):
        raise BadInput(
            "word_target min/max must be integers",
            next="edit(id='dc<heading>', word_target={'min':200,'max':400})",
        ) from None
    if lo < 0 or hi < 0:
        raise BadInput("word_target min/max must be non-negative")
    if lo > hi:
        raise BadInput(
            f"word_target min={lo} exceeds max={hi}",
            next="edit(id='dc<heading>', word_target={'min':200,'max':400})",
        )
    return (lo, hi)


class DraftHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="draft",
        title="Draft",
        description=(
            "Editable, chunk-native document. put creates a "
            "draft (project=, born with a title heading), forks one "
            "(copy_of=<src-slug>, project=<todo|new-title>: deep-copies "
            "chunks + hierarchy + links into a new draft, source untouched; "
            "project= an int/todo:N binds an existing project, any other "
            "string mints a fresh one titled that), or adds a chunk "
            "(chunk_kind=, text=, at={first|last|into|before|after}); get "
            "lists / outlines / reads a chunk window dc<id>-B+A; search "
            "(q=, mode=lexical|semantic|hybrid|regex, scope=slug|dc<id>, "
            "headings_only=) over prose — mode=regex is a literal grep; edit "
            "changes text, moves (move=), renames the document "
            "(title=, syncs refs.title + the title heading), sets a heading's "
            "section style (style=<skill>), or regex-substitutes across a "
            "draft/section (sub={find,replace}, dry-run unless apply=True); "
            "delete soft-retires (mode=cascade|promote). Chunks "
            "addressed by dc<chunk_id> (legacy ¶handle still resolves). "
            "See precis-draft-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        role="artifact",
        views=("toc", "links"),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("draft: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    #: Relations a draft accepts through the generic (non-placement)
    #: ``apply_link_ops`` path. Deliberately narrow — cross-references
    #: between drafts still live in prose, not this verb (the autolinker
    #: on ``edit`` handles those) — but ``serves`` is the one relation
    #: an outside kind (``quest``) needs materialised as a real edge,
    #: not a mention: a draft (e.g. a living dossier) put in a quest's
    #: service (gripe 161912; see ``precis-quest-help``: "Any node ...
    #: can serve a quest").
    _GENERIC_LINK_RELS: ClassVar[frozenset[str]] = frozenset({"serves"})

    # ── link: placement + the narrow generic-link allowlist ─────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Folder placement (``rel='parent'``) or a ``serves`` edge.

        ``rel='parent'`` is the reserved virtual relation — a
        ``refs.parent_id`` write into a ``kind='folder'`` container, never a stored ``links`` row. ``rel='serves'`` goes
        through the shared ``apply_link_ops`` path (the same one
        paper/structure use), most notably targeting a ``quest:`` ref.
        Every other relation is rejected — cross-references between
        drafts still live in prose, not this verb.
        """
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=str(id).strip())
            return place_ref(
                self.store, kind="draft", ref=ref, target=target, mode=mode
            )
        if rel in self._GENERIC_LINK_RELS:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=str(id).strip())
            target = require_link_target("draft", target)
            validate_link_mode(mode)
            n_added, n_removed = apply_link_ops(
                self.store,
                ref.id,
                link=target if mode == "add" else None,
                unlink=target if mode == "remove" else None,
                rel=rel,
            )
            return Response(
                body=format_link_tag_ack(
                    kind=self.spec.kind,
                    ref_label=str(ref.slug),
                    n_links_added=n_added,
                    n_links_removed=n_removed,
                    n_tags_added=0,
                    n_tags_removed=0,
                )
            )
        raise BadInput(
            "draft link supports only rel='parent' (folder placement) or "
            "rel='serves' (mark this draft as serving a quest)",
            next=[
                "link(kind='draft', id='<slug>', target='folder:N', "
                "rel='parent') places; mode='remove' unfiles",
                "link(kind='draft', id='<slug>', target='quest:N', "
                "rel='serves') marks this draft as serving that quest",
                "cross-references live in prose, not the link verb: "
                "edit(kind='draft', id='dc<src>', text='…existing… "
                "[dc<target>]') and the autolinker materialises the "
                "related-to backlink",
            ],
        )

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        targets: list[str] | None = None,
        project: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        if project is not None:
            # Reverse lookup (paper-writing pipeline rung 4,
            # docs/backlog/paper-writing-pipeline.md §Gap-analysis —
            # backlog_draft_by_project): the draft(s) bound to a project
            # todo via the ``draft-of`` link create_draft() mints.
            if id is not None:
                raise BadInput(
                    "get(kind='draft') accepts id= or project=, not both",
                    next="get(kind='draft', project=<todo-id>)  # OR "
                    "get(kind='draft', id='<slug>')",
                )
            return self._render_by_project(project)
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()
        if _is_draft_chunk_addr(s):
            # Turn-taking persona threads eye — render this node at a focus extent via ``view=``:
            # the ladder kwd|summary|verbatim|fisheye|fisheye+1hop (labels
            # derived from ``Extent`` so they can't drift from the enum).
            # Exposes the composer one node at a time; ``view='backfill'``
            # composes many.
            extent_ladder = [e.label for e in Extent if e is not Extent.NONE]
            if view in extent_ladder:
                from precis.utils.eye_render import render_eye

                try:
                    return Response(body=render_eye(self.store, s, view))
                except ValueError as e:
                    raise BadInput(
                        str(e),
                        next=f"view ∈ {'|'.join(extent_ladder)}",
                    ) from e
            if view == "backfill":  # source-backfill workspace for this section
                from precis.backfill import render_backfill

                return Response(
                    body=render_backfill(
                        self.store, self.embedder, [s, *(targets or [])]
                    )
                )
            if view == "toc":  # TOC of the subtree under this heading
                return self._render_toc(root_handle=s)
            if view == "wordcount":  # word counts for this heading's subtree
                return self._render_wordcount(root_handle=s)
            if view == "review-diff":
                # Paper-writing pipeline rung 3 — the human checker's
                # approved→current diff for this one chunk.
                from precis.handlers._review_view import render_review_diff_view

                return render_review_diff_view(self.store, s)
            if view is not None:
                # No silent degrade to the lone-chunk render (was the bug —
                # an unrecognized view fell through here unnoticed).
                raise BadInput(
                    f"unknown draft chunk view {view!r}",
                    next="view ∈ backfill|toc|wordcount|review-diff, or a "
                    f"focus-ladder label {'|'.join(extent_ladder)}",
                )
            return self._render_chunk(s)
        ref = resolve_live_slug_ref(self.store, kind="draft", id=s)
        if view == "backfill":
            # Whole-draft roll-up (Build 2 §G2): every top-level section's
            # sweep, merged by source ref — narrower per-section detail is
            # still available at get(id='dc<id>', view='backfill').
            from precis.backfill import render_backfill_draft

            return Response(body=render_backfill_draft(self.store, self.embedder, ref))
        if view == "toc":
            return self._render_toc(ref=ref)
        if view == "wordcount":
            return self._render_wordcount(ref=ref)
        if view == "links":
            # Graph-completeness audit item 1 — draft was link-blind like
            # paper (OPEN-ITEMS.md 🕸️). Ref-level only; draft's own
            # chunk-scoped edges (if any land later) aren't rendered here.
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="draft")
        if view == "integration":
            # Paper-writing pipeline rung 2 (docs/backlog/paper-writing-pipeline.md
            # §"The integration ledger") — a topic
            # dossier's woven-in papers (INTEGRATED) vs its topic:-tagged
            # papers with no disposition edge yet (PENDING).
            from precis.handlers._integration_view import render_integration_view

            return render_integration_view(self.store, ref)
        if view == "review":
            # Paper-writing pipeline rung 3 (docs/backlog/paper-writing-pipeline.md
            # §"Review — the memoized approval
            # ledger") — per-chunk checker status, dirty-for-human flagged.
            from precis.handlers._review_view import render_review_view

            return render_review_view(self.store, ref)
        if view == "citations":
            # The draft-citation lifecycle view — the draft's
            # paper/claim citations partitioned into to-fetch /
            # to-re-ground / to-promote / done. Purely derived (token kind
            # + paper block-count), read-only.
            from precis.handlers._citations_view import render_citations_view

            return render_citations_view(self.store, ref)
        if view == "hygiene":
            # gr192827 item 9 — the complete undefined-abbreviation +
            # whole-paper-cite lists, un-elided, with nothing else. The
            # outline footer (below) shows the same data truncated to 8
            # entries per list.
            return self._render_hygiene(s, ref)
        if view == "review-diff":
            raise BadInput(
                "review-diff targets a chunk (dc<id>), not a whole draft",
                next="point it at a chunk handle: "
                "get(kind='draft', id='dc123', view='review-diff')",
            )
        if view not in (None, "outline"):
            # 'outline' is the default render (view omitted); accept it as an
            # explicit value too — the model's intuitive guess (naming the
            # concept the error itself calls "the outline") is otherwise a
            # dead-end BadInput hit by many independent planner jobs.
            raise BadInput(
                f"unknown draft view {view!r}",
                next=(
                    "view='toc' for the heading skeleton, view='wordcount' for "
                    "per-section word counts vs targets, view='links' for the "
                    "link graph, view='integration' for the integration ledger "
                    "(a topic dossier only), view='review' for the approval "
                    "ledger, view='citations' for the citation lifecycle "
                    "(to-fetch/to-re-ground/to-promote/done), view='hygiene' "
                    "for the full undefined-abbreviation/whole-paper-cite "
                    "lists (un-elided), or omit (or view='outline') for the "
                    "outline"
                ),
            )
        return self._render_outline(s, ref)

    # ── search: lexical / semantic over draft chunks ─────────────────

    def search(
        self,
        *,
        q: str | None = None,
        scope: str | int | None = None,
        id: str | int | None = None,
        mode: str | None = None,
        flags: str | None = None,
        headings_only: bool = False,
        page_size: int = 10,
        page: int = 1,
        **_kw: Any,
    ) -> Response:
        """Search draft prose. ``mode='lexical'`` is verbatim/keyword,
        ``mode='semantic'`` is by meaning, default ``hybrid`` fuses both;
        ``mode='regex'`` is a literal **grep** — ``q`` is a Python regex run
        verbatim over chunk text (find ``\\*\\*\\w+\\*\\*`` bold, ``—`` em-dashes,
        a malformed cite), with ``flags='i'``/``'s'`` opt-in. Scope: a
        ``dc<id>`` chunk handle searches the subtree under that chunk (a
        section, or just the chunk if a leaf), a draft slug searches that
        whole draft, nothing searches every draft. ``headings_only=True``
        restricts hits to section headings (a semantic TOC jump)."""
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='draft') requires q=",
                next="search(kind='draft', q='topic', mode='semantic')",
            )
        q = str(q)
        # ``id='¶…'`` is accepted as a scope alias — the sigil already
        # pinned kind='draft', and an agent naturally points search at the
        # chunk it is reading.
        raw_scope = next(
            (str(c).strip() for c in (scope, id) if c is not None and str(c).strip()),
            None,
        )
        if (mode or "").strip() == "regex":
            return self._regex_find(
                q, raw_scope, flags=flags or "", page_size=page_size, page=page
            )
        scope_ref_id: int | None = None
        chunk_ids: list[int] | None = None
        where = "all drafts"
        if raw_scope:
            if _is_draft_chunk_addr(raw_scope):
                chunk_ids = self.store.drafts.draft_subtree_chunk_ids(raw_scope)
                if not chunk_ids:
                    raise NotFound(f"draft chunk {raw_scope} not found")
                root = self.store.drafts.get_draft_chunk(raw_scope)
                scope_ref_id = int(root.ref_id) if root else None
                where = f"subtree {raw_scope}"
            else:
                ref = resolve_live_slug_ref(self.store, kind="draft", id=raw_scope)
                scope_ref_id = ref.id
                where = f"draft {raw_scope!r}"
        chunk_kinds = ["heading"] if headings_only else None
        query_vec = query_vec_for(self.embedder, q, mode)
        offset = max(0, (int(page) - 1) * int(page_size))
        hits = self.store.blocks.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="draft",
            scope_ref_id=scope_ref_id,
            chunk_ids=chunk_ids,
            chunk_kinds=chunk_kinds,
            limit=page_size,
            offset=offset,
        )
        return self._render_search(hits, q=q, where=where, headings_only=headings_only)

    def _render_search(
        self, hits: list[Any], *, q: str, where: str, headings_only: bool
    ) -> Response:
        noun = "heading" if headings_only else "chunk"
        if not hits:
            return Response(
                body=(
                    f"no draft {noun}s match {q!r} in {where}\n\n"
                    "Next: widen with mode='semantic', drop scope=, or "
                    "drop headings_only to search body text too."
                )
            )
        lines = [f"# {len(hits)} draft {noun} hit(s) for {q!r} — {where}\n"]
        for block, ref, _score in hits:
            handle = handle_registry.format_handle("draft", block.id, chunk=True)
            draft = ref.slug or ref.id
            first = (block.text or "").strip().splitlines()[0] if block.text else ""
            if len(first) > 90:
                first = first[:89] + "…"
            lines.append(f"draft:{draft}  {handle}  [{block.chunk_kind}] {first}")
        lines.append("\nNext: get(id='dc<chunk_id>') to read any hit in full.")
        return Response(body="\n".join(lines))

    # ── regex find (grep) + substitute (s///) ────────────────────────

    #: caps so a broad pattern can't return a wall of text
    _RX_MATCHES_PER_CHUNK: ClassVar[int] = 10
    _RX_LINE_CONTEXT: ClassVar[int] = 40  # chars of context each side of a hit
    _RX_PREVIEW_CHUNKS: ClassVar[int] = 40  # substitute dry-run sample size

    def _scope_chunks(
        self, raw_scope: str | int | None, *, allow_all: bool
    ) -> tuple[list[tuple[str, Any]], str]:
        """Resolve a find/substitute scope to ``(slug, DraftChunk)`` pairs in
        reading order, plus a human ``where`` label. Scope is the same axis as
        ``search``: a draft slug (whole draft), a ``dc<id>`` handle (the
        subtree under it — a section, or just the chunk if a leaf), or
        ``None`` (every draft, only when ``allow_all``)."""
        if not raw_scope:
            if not allow_all:
                raise BadInput(
                    "substitute needs a scope — a draft slug or a dc<id> "
                    "(no corpus-wide rewrite)",
                    next="edit(kind='draft', id='<slug>', sub={'find':…,'replace':…})",
                )
            pairs: list[tuple[str, Any]] = []
            for ref in self.store.list_refs(kind="draft", limit=10_000):
                slug = ref.slug or str(ref.id)
                pairs.extend((slug, c) for c in self.store.drafts.reading_order(ref.id))
            return pairs, "all drafts"
        raw = str(raw_scope).strip()
        if _is_draft_chunk_addr(raw):
            ids = self.store.drafts.draft_subtree_chunk_ids(raw)
            if not ids:
                raise NotFound(f"draft chunk {raw} not found")
            root = self.store.drafts.get_draft_chunk(raw)
            if root is None:
                raise NotFound(f"draft chunk {raw} not found")
            ref_id = int(root.ref_id)
            owner = self.store.get_ref(kind="draft", id=ref_id)
            slug = (owner.slug if owner and owner.slug else None) or str(ref_id)
            keep = set(ids)
            chunks = [
                c for c in self.store.drafts.reading_order(ref_id) if c.chunk_id in keep
            ]
            return [(slug, c) for c in chunks], f"subtree {raw}"
        ref = resolve_live_slug_ref(self.store, kind="draft", id=raw)
        slug = ref.slug or str(ref.id)
        return [
            (slug, c) for c in self.store.drafts.reading_order(ref.id)
        ], f"draft {raw!r}"

    def _regex_find(
        self,
        pattern: str,
        raw_scope: str | None,
        *,
        flags: str,
        page_size: int,
        page: int,
    ) -> Response:
        """``mode='regex'`` grep: run ``pattern`` over chunk text in scope and
        list every hit with its handle, line, and the matched span. Read-only
        — table/figure chunks are matched on their stored ``text`` (derived
        markdown, or raw LaTeX for tex-imported tables). Scope is the live
        draft only: a retired chunk never matches even though its ``dc<id>``
        still reads (gr192827 finding 8 — the read path now discloses
        retired state so a miss here is explicable)."""
        rx = draft_regex.compile_pattern(pattern, flags)
        pairs, where = self._scope_chunks(raw_scope, allow_all=True)
        hits: list[tuple[str, Any, list[draft_regex.Match]]] = []
        total = 0
        for slug, c in pairs:
            ms = draft_regex.find_in_text(rx, c.text or "")
            if ms:
                total += len(ms)
                hits.append((slug, c, ms))
        if not hits:
            return Response(
                body=(
                    f"no draft chunk matches /{pattern}/ in {where}\n\n"
                    "Next: loosen the pattern, add flags='i' (case-fold), or "
                    "widen scope (drop scope= to grep every draft)."
                )
            )
        npages = (len(hits) + page_size - 1) // page_size
        pg = max(1, min(int(page), npages))
        start = (pg - 1) * page_size
        shown = hits[start : start + page_size]
        head = f"# {total} match(es) in {len(hits)} chunk(s) for /{pattern}/ — {where}"
        if npages > 1:
            head += f"  (page {pg}/{npages})"
        lines = [head, ""]
        for slug, c, ms in shown:
            lines.append(f"draft:{slug}  {c.dc}  [{c.chunk_kind}]")
            for m in ms[: self._RX_MATCHES_PER_CHUNK]:
                lines.append(f"  L{m.line_no}:{m.col}  {self._mark_line(m)}")
            if len(ms) > self._RX_MATCHES_PER_CHUNK:
                lines.append(
                    f"  … +{len(ms) - self._RX_MATCHES_PER_CHUNK} more in this chunk"
                )
        lines.append(
            "\nNext: get(id='dc<id>') to read a hit; substitute with "
            "edit(kind='draft', id='<slug|dc<id>>', sub={'find':"
            f"{pattern!r}, 'replace':'…'}}) (dry-run unless apply=True)."
        )
        return Response(body="\n".join(lines))

    def _mark_line(self, m: draft_regex.Match) -> str:
        """Render one match's physical line, trimmed to a window around the
        hit and wrapping the matched span in »…« so it stands out."""
        col, n, line = m.col, len(m.matched), m.line
        ctx = self._RX_LINE_CONTEXT
        lo = max(0, col - ctx)
        hi = min(len(line), col + n + ctx)
        pre = ("…" if lo > 0 else "") + line[lo:col]
        mid = line[col : col + n]
        post = line[col + n : hi] + ("…" if hi < len(line) else "")
        return f"{pre}»{mid}«{post}"

    def _parse_sub_expr(self, sub: dict[str, Any] | str) -> tuple[str, str, str]:
        """Parse a ``sub=`` param — ``{'find':…, 'replace':…, 'flags':…}`` or
        a ``s/find/replace/flags`` string — into ``(find, replace, flags)``.
        Pure parsing, no scope/chunk involved; shared by the whole-draft/
        subtree substitute (:meth:`_substitute`) and the table cell-level
        find-replace (:meth:`_edit_table`, the shipped draft-table-editing
        proposal item 1, git history)."""
        if isinstance(sub, str):
            return draft_regex.parse_sed(sub)
        if isinstance(sub, dict):
            if "find" not in sub or "replace" not in sub:
                raise BadInput(
                    "sub= needs both 'find' and 'replace' keys",
                    next="sub={'find': '\\*\\*(\\w+)\\*\\*', 'replace': '\\\\1'}  # strip bold",
                )
            find = str(sub["find"])
            replace = str(sub["replace"])
            flags = str(sub.get("flags") or "")
            return find, replace, flags
        raise BadInput(
            "sub= must be {'find':…,'replace':…} or a 's/find/replace/' string",
            next="sub={'find':'—', 'replace':', '}  or  sub='s/—/, /'",
        )

    def _substitute(
        self, scope: str | int | None, sub: dict[str, Any] | str, *, apply: bool
    ) -> Response:
        """Regex substitute (vi ``:%s/find/replace/``) across a scope's prose.
        ``sub`` is ``{'find':…, 'replace':…, 'flags':…}`` or a ``s/find/replace/``
        string. Dry-run by default (reports counts + a per-chunk before→after
        sample); ``apply=True`` rewrites each chunk via the normal edit path
        (re-embed / keywords / links cascade). Replacement is a Python regex
        template, so ``\\1`` backreferences resolve. Table/figure chunks are
        skipped (derived / blob text)."""
        find, replace, flags = self._parse_sub_expr(sub)
        rx = draft_regex.compile_pattern(find, flags)
        pairs, where = self._scope_chunks(scope, allow_all=False)
        if pairs:
            # ``allow_all=False`` guarantees a single-draft scope (a slug or
            # a dc<id> subtree, never "all drafts") — every pair shares one
            # ref_id, so the first is enough to guard the whole write.
            self._refuse_if_machine_owned(int(pairs[0][1].ref_id))

        changes: list[tuple[str, Any, str, int]] = []
        total_subs = 0
        skipped: list[str] = []  # derived chunks a substitution would have hit
        for slug, c in pairs:
            old = c.text or ""
            if c.chunk_kind in draft_regex.DERIVED_KINDS:
                if rx.search(old):
                    skipped.append(c.dc)
                continue
            new_text, n = draft_regex.sub_in_text(rx, replace, old)
            if n and new_text != old:
                changes.append((slug, c, new_text, n))
                total_subs += n

        if not changes:
            note = ""
            if skipped:
                note = (
                    f"\n\n(skipped {len(skipped)} derived table/figure chunk(s) "
                    f"that match: {', '.join(skipped[:8])} — edit their data, not text)"
                )
            return Response(
                body=f"no substitutable matches for /{find}/ in {where}{note}"
            )

        if not apply:
            return self._sub_dryrun(
                find, replace, flags, where, changes, skipped, total_subs
            )

        written = 0
        for _slug, c, new_text, _n in changes:
            # base_sha = the sha of the text this chunk was scoped against
            # (``old``, folded into ``changes`` above) — a concurrent edit
            # between scoping and this write raises BadInput instead of
            # silently clobbering it (gr176088).
            res = self.store.drafts.edit_text(
                c.handle, new_text, base_sha=content_sha(c.text or "")
            )
            if res is not None:
                self.sync_draft_links(res.ref_id)
                self._attribute_touch([res.chunk_id])
                written += 1
        body = (
            f"substituted /{find}/ → /{replace}/ in {where}: "
            f"{total_subs} replacement(s) across {written} chunk(s)"
        )
        if skipped:
            body += (
                f"; skipped {len(skipped)} derived chunk(s) ({', '.join(skipped[:8])})"
            )
        body += "\n\nEach edited chunk re-embeds; the original text is kept in chunk history."
        return Response(body=body)

    def _sub_dryrun(
        self,
        find: str,
        replace: str,
        flags: str,
        where: str,
        changes: list[tuple[str, Any, str, int]],
        skipped: list[str],
        total_subs: int,
    ) -> Response:
        """Render the substitution preview: totals + a per-chunk before→after
        on the first changed line of each chunk (capped), then the copy-ready
        apply call."""
        rx = draft_regex.compile_pattern(find, flags)
        lines = [
            f"# DRY RUN — /{find}/ → /{replace}/ in {where}",
            f"{total_subs} replacement(s) across {len(changes)} chunk(s). "
            "Nothing written yet.",
            "",
        ]
        for _slug, c, _new, n in changes[: self._RX_PREVIEW_CHUNKS]:
            ms = draft_regex.find_in_text(rx, c.text or "")
            sample = ms[0].line if ms else ""
            after = rx.sub(replace, sample)
            lines.append(f"{c.dc}  [{c.chunk_kind}]  ({n}×)")
            lines.append(f"  - {sample.strip()}")
            lines.append(f"  + {after.strip()}")
        if len(changes) > self._RX_PREVIEW_CHUNKS:
            lines.append(f"… +{len(changes) - self._RX_PREVIEW_CHUNKS} more chunk(s)")
        if skipped:
            lines.append(
                f"\nskipped {len(skipped)} derived table/figure chunk(s) that match: "
                f"{', '.join(skipped[:8])} (edit their data, not text)"
            )
        # The scope label echoes back as a copy-ready apply call.
        scope_hint = where.replace("draft ", "").replace("subtree ", "").strip("'")
        if scope_hint == "all drafts":
            scope_hint = "<slug>"
        lines.append(
            f"\nApply: edit(kind='draft', id={scope_hint!r}, "
            f"sub={{'find': {find!r}, 'replace': {replace!r}"
            + (f", 'flags': {flags!r}" if flags else "")
            + "}, apply=True)"
        )
        return Response(body="\n".join(lines))

    # ── put: create a draft, or add a chunk ──────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        project: str | int | None = None,
        chunk_kind: str | None = None,
        at: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        image: str | None = None,
        mime: str | None = None,
        origin: str | None = None,
        permission: dict[str, Any] | None = None,
        table: str | dict[str, Any] | None = None,
        caption: str | None = None,
        regen: dict[str, Any] | None = None,
        render: str | None = None,
        plots: list[str] | None = None,
        voice: str | None = None,
        lang: str | None = None,
        copy_of: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        if copy_of is not None:
            # Draft fork/deep-copy primitive: put(kind='draft',
            # copy_of='<src-slug>', project=<todo>) deep-copies the WHOLE
            # source draft (chunks + hierarchy + links) into a NEW draft
            # bound to `project`, leaving the source untouched. `id=`, if
            # given, seeds the new slug (deduped); otherwise `<src>-copy`.
            return self._fork(copy_of, project=project, new_id=id, title=title)

        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='draft') requires id= (the draft slug)",
                next="put(kind='draft', id='nanotrans', title='…', project=<todo-id>)",
            )
        slug = str(id).strip()
        # A draft slug becomes a filesystem path segment at export time
        # (draft_export writes `<export-root>/<slug>/main.tex`), and the DB
        # enforces no slug format. Reject path separators / traversal segments
        # here so a slug like `../../etc/x` can never be created — defence in
        # depth behind draft_export's own containment check.
        if "/" in slug or "\\" in slug or "\x00" in slug or slug in {".", ".."}:
            raise BadInput(
                f"put(kind='draft') slug {slug!r} may not contain a path "
                "separator or be a '.'/'..' segment",
                next="use a plain slug, e.g. put(kind='draft', id='nanotrans', …)",
            )

        if chunk_kind == "figure" and image is not None:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            self._refuse_if_machine_owned(ref.id)
            return self._add_figure(
                slug=slug,
                ref_id=ref.id,
                caption=text,
                image=image,
                mime=mime,
                origin=origin,
                permission=permission,
                at=at,
            )

        # A computed figure (graph): render code + plots links, image deferred
        # to the render pass. text= is the caption.
        if chunk_kind == "figure" and (render is not None or plots is not None):
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            self._refuse_if_machine_owned(ref.id)
            return self._add_graph_figure(
                slug=slug,
                ref_id=ref.id,
                caption=text or caption,
                render=render,
                plots=plots,
                at=at,
            )

        if chunk_kind is not None or at is not None:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=slug)
            self._refuse_if_machine_owned(ref.id)
            if (chunk_kind or "paragraph") == "table" or table is not None:
                return self._put_table(
                    slug,
                    ref,
                    table=table,
                    caption=caption,
                    regen=regen,
                    at=at,
                    meta=meta,
                )
            if text is None or not str(text).strip():
                raise BadInput(
                    "adding a draft chunk requires text=",
                    next="put(kind='draft', id='nanotrans', chunk_kind='paragraph', text='…', at={'after': 'dc<chunk_id>'})",
                )
            kind = chunk_kind or "paragraph"
            # Per-chunk narration routing (audio export): which Kokoro voice +
            # language phonemizer speaks this chunk. Validated against the voice
            # catalogue so a typo fails loudly; merged into the chunk meta the
            # narration layer reads. Enables mixed-voice / multilingual drafts
            # (a French epigraph, a Mandarin drill) — set them per chunk.
            if voice is not None or lang is not None:
                from precis.tts import voices as _voices

                try:
                    _v, _lg = _voices.resolve(voice, lang)
                except ValueError as exc:
                    raise BadInput(
                        str(exc),
                        next="voices: get(kind='skill', id='precis-audio-help')",
                    ) from exc
                meta = {**(meta or {}), "voice": _v, "lang": _lg}
            # A registry ``term`` leaf (glossary / parts / components)
            # is stamped with its ``meta.registry`` family, gets its
            # insert-callout frozen if the policy is ``assign="insert"``, and
            # files under that registry's one home heading unless the caller
            # placed it explicitly.
            if kind == "term":
                meta, term_role = self._prepare_term_meta(ref.id, meta)
                if at is None:
                    at = {
                        "into": self.store.drafts.ensure_registry_heading(
                            ref.id, term_role
                        )
                    }
            chunks = self.store.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind=kind,
                text=str(text),
                at=at,
                meta=meta,
            )
            self.sync_draft_links(ref.id)
            self._attribute_touch([c.chunk_id for c in chunks])
            handles = " ".join(f"{c.dc}" for c in chunks)
            n = len(chunks)
            body = f"added {n} chunk{'' if n == 1 else 's'} to {slug}: {handles}"
            # Hint the LLM about abbreviations it just wrote (skip when the
            # write *is* a term definition). All of a new chunk's text is
            # "newly introduced", so there's no prior text to diff against.
            if kind != "term":
                body += _draft_lint.write_abbrev_hints(
                    self.store, slug, ref.id, str(text), ""
                )
                body += _draft_lint.citation_form_hint(str(text))
                body += _draft_lint.whole_paper_cite_hint(str(text), "")
                body += _draft_lint.pc_cite_claim_hub_hint(self.store, str(text))
                body += _draft_lint.literal_cite_hint(str(text))
                body += _draft_lint.temperature_form_hint(str(text))
            return Response(body=body)

        # else: create the draft
        if project is None:
            raise BadInput(
                "creating a draft requires project= (the owning project todo id)",
                next="put(kind='draft', id='nanotrans', title='…', project=<todo-id>)",
            )
        project_ref_id = self._resolve_project(project)
        ref, title_chunk = self.store.drafts.create_draft(
            name=slug,
            title=(title or slug).strip() or slug,
            project_ref_id=project_ref_id,
            meta=meta,
        )
        return Response(
            body=(
                f"created draft '{slug}' (title heading {title_chunk.dc}); "
                f"linked draft-of project {project_ref_id}"
            )
        )

    def _dedup_slug(self, candidate: str) -> str:
        """``candidate``, or ``candidate-2``/``-3``/… if a live draft
        already holds it — same "never clobber a slug" instinct as the
        rest of the kind, just applied to a machine-derived fork slug
        instead of an agent-typed one."""
        slug = candidate
        n = 2
        while self.store.get_ref(kind="draft", id=slug) is not None:
            slug = f"{candidate}-{n}"
            n += 1
        return slug

    def _fork(
        self,
        copy_of: str | int,
        *,
        project: str | int | None,
        new_id: str | int | None,
        title: str | None,
    ) -> Response:
        """``put(kind='draft', copy_of='<slug>', project=<todo>)`` — deep-copy
        the WHOLE source draft (every chunk + its hierarchy + every link
        touching it) into a NEW draft bound to ``project``, via
        :meth:`~precis.store._draft_ops.DraftStore.fork_draft`. The source is
        never touched. Refuses (does not clobber) if ``project`` already owns
        a draft — unlike ``draftimport``'s re-import path, a fork never
        retires an existing draft out from under a project. ``project=`` may
        also be a fresh project TITLE (see :meth:`_resolve_or_create_project`)."""
        src_slug = str(copy_of).strip()
        if not src_slug:
            raise BadInput(
                "copy_of= must name the source draft's slug",
                next="put(kind='draft', copy_of='<src-slug>', project=<todo-id>)",
            )
        src = resolve_live_slug_ref(self.store, kind="draft", id=src_slug)
        if project is None:
            raise BadInput(
                "put(kind='draft', copy_of=…) requires project= "
                "(an existing project todo id/handle, or a NEW project's title)",
                next=f"put(kind='draft', copy_of='{src_slug}', project=<todo-id>)",
            )
        project_ref_id = self._resolve_or_create_project(project)
        if self.store.links_for(project_ref_id, direction="in", relation="draft-of"):
            raise BadInput(
                f"project {project_ref_id} already has a draft — "
                "a project owns at most one",
                next=(
                    f"get(kind='draft', project={project_ref_id}) to see it, "
                    "or pick a fresh project todo for the fork"
                ),
            )
        candidate = str(new_id).strip() if new_id else f"{src_slug}-copy"
        new_slug = self._dedup_slug(candidate)
        new_title = (title or f"{src.title} (review copy)").strip()
        new_ref = self.store.drafts.fork_draft(
            src.id, project_ref_id, new_slug=new_slug, title=new_title
        )
        return Response(
            body=(
                f"forked draft '{src_slug}' → '{new_ref.slug}' (ref {new_ref.id}); "
                f"linked draft-of project {project_ref_id}; copy-of {src_slug}"
            )
        )

    def _resolve_or_create_project(self, project: str | int) -> int:
        """``project=`` for a fork: an int or a ``'todo:N'`` string resolves
        to an EXISTING project todo, exactly like :meth:`_resolve_project`
        (and every other draft/plan ``project=`` call site) — never
        fuzzy-matched. A plain non-numeric string is a NEW project's
        **title**: mints a fresh ``meta.rotation_root=true`` project todo
        with that title.

        Reuses ``TodoHandler.put`` — the same "mint a project when the
        caller doesn't hand us one" path
        :func:`precis.draftimport.build.run_import` takes — rather than an
        ``insert_ref`` direct to the store, so the level-gradient authority
        guard (``_todo_guards.check_facets_on_create``: workers can't mint
        ``rotation_root=true``) still applies instead of being silently
        bypassed for the fork's own project-minting shortcut. The new ref's
        id is read off ``Response.ref_id`` (structured, not regex-parsed
        off the ack's ``td<id>`` handle) — a title has no uniqueness
        guarantee, but the id on this call's own ``put`` response is."""
        raw = str(project).strip()
        bare = raw[len("todo:") :] if raw.startswith("todo:") else raw
        if bare.isdigit():
            return self._resolve_project(project)
        # self.hub is set at registration; a hand-constructed handler
        # (tests) leaves it None, so fall back to a minimal hub over the
        # same store — TodoHandler only needs hub.store.
        hub = self.hub if self.hub is not None else Hub(store=self.store)
        resp: Response = hub.sibling("todo").put(text=raw, meta={"rotation_root": True})
        if resp.ref_id is None:  # pragma: no cover - defensive; contract broken
            raise BadInput(
                f"minted project {raw!r} but its put() returned no ref_id: "
                f"{resp.body!r}"
            )
        return resp.ref_id

    def _prepare_term_meta(
        self, ref_id: int, meta: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str]:
        """Stamp a registry ``term`` leaf's ``meta`` and return ``(meta, role)``. Records the ``meta.registry`` family (defaulting to
        the glossary) so the projection + reconcile can find it, and freezes a
        consecutive ``meta.callout`` when the registry's policy is
        ``assign="insert"`` (a BOM item number, stable under later reorder)."""
        from precis.draft import registry as _reg

        m = dict(meta or {})
        role = str(m.get("registry") or _reg.DEFAULT_REGISTRY)
        m["registry"] = role
        policy = _reg.policy_for(role)
        if policy.assign == "insert" and m.get("callout") is None:
            existing = self.store.drafts.registry_callouts(ref_id, role)
            m["callout"] = _reg.next_insert_callout(existing, policy)
        return m, role

    def _add_figure(
        self,
        *,
        slug: str,
        ref_id: int,
        caption: str | None,
        image: str,
        mime: str | None,
        origin: str | None,
        permission: dict[str, Any] | None,
        at: dict[str, Any] | None,
    ) -> Response:
        """Add a figure chunk with binary payload. ``text`` is
        the caption; ``image`` is base64 bytes; ``origin`` classes the
        figure for the clearance gate; a ``third_party`` figure must carry
        a ``permission`` paper-trail."""
        if caption is None or not str(caption).strip():
            raise BadInput(
                "a figure requires text= (the caption)",
                next="put(kind='draft', id='…', chunk_kind='figure', text='Fig 1. …', image=<b64>, origin='original')",
            )
        org = (origin or "").strip()
        if org not in _FIGURE_ORIGINS:
            raise BadInput(
                f"figure origin= must be one of {list(_FIGURE_ORIGINS)}",
                next="origin='original' (ours) | 'own_graph' (from data) | 'third_party' (publisher permission)",
            )
        try:
            raw = base64.b64decode(str(image), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadInput(
                "image= must be base64-encoded image bytes",
                next="pass the raw image base64-encoded (no data: URI prefix)",
            ) from exc
        if not raw:
            raise BadInput("image= decoded to empty bytes")
        # The figure medium axis — an SVG blob (sniffed, or an explicit image/svg+xml from
        # a web upload) is sanitized at rest: strip script / on* / javascript:
        # so a pasted or reused SVG can't smuggle active content.
        resolved_mime = (mime or _sniff_mime(raw)).split(";", 1)[0].strip()
        if resolved_mime == "image/svg+xml":
            raw = _sanitize_svg_bytes(raw)
        fig_meta: dict[str, Any] = {}
        if org == "third_party":
            if not permission:
                raise BadInput(
                    "a third_party figure requires permission= (the publisher paper-trail)",
                    next=(
                        "permission={'publisher':'…','permission_id':'…',"
                        "'status':'granted','source_paper':'<cite-key>', …}"
                    ),
                )
            fig_meta["permission"] = permission
        chunk = self.store.drafts.add_figure(
            ref_id=ref_id,
            caption=str(caption),
            origin=org,
            image=raw,
            mime=resolved_mime,
            at=at,
            figure_meta=fig_meta,
        )
        self.sync_draft_links(ref_id)
        return Response(
            body=f"added figure {chunk.dc} [{org}] to {slug} ({len(raw)} bytes)"
        )

    def _add_graph_figure(
        self,
        *,
        slug: str,
        ref_id: int,
        caption: str | None,
        render: str | None,
        plots: list[str] | None,
        at: dict[str, Any] | None,
    ) -> Response:
        """Add a *computed* figure — a graph: the render code goes to
        ``meta.render``, ``plots`` links the data chunks it renders, and the image
        is **deferred** (a placeholder blob until the render pass fills it).
        ``origin='own_graph'``; the caption is the face (``text``)."""
        if not render or not str(render).strip():
            raise BadInput(
                "a graph figure requires render= (the Python that draws it)",
                next=(
                    "put(kind='draft', id='…', chunk_kind='figure', "
                    "render='import matplotlib.pyplot as plt; …', "
                    "plots=['dc<data-id>'], text='Fig 1. …')"
                ),
            )
        if not plots:
            raise BadInput(
                "a graph figure requires plots=[dc<id>] — the data chunk(s) it renders",
                next="plots=['dc<table-chunk-id>']  (the table/data chunk handles)",
            )
        if caption is None or not str(caption).strip():
            raise BadInput(
                "a figure requires text= (the caption)",
                next="put(kind='draft', …, chunk_kind='figure', render=…, plots=[…], text='Fig 1. …')",
            )
        # Resolve each plots target to a live chunk in *this* draft.
        targets = []
        for p in plots:
            c = self.store.drafts.get_draft_chunk(str(p))
            if c is None:
                raise NotFound(f"plots target {p!r} not found")
            if int(c.ref_id) != ref_id:
                raise BadInput(
                    f"plots target {p!r} is not a chunk in draft {slug!r}",
                    next="plots= must reference data chunks in the same draft",
                )
            targets.append(c)

        chunk = self.store.drafts.add_figure(
            ref_id=ref_id,
            caption=str(caption),
            origin="own_graph",
            image=_PLACEHOLDER_PNG,  # deferred — the render pass overwrites it
            mime="image/png",
            at=at,
            figure_meta={"render_pending": True},
        )
        self.store.drafts.set_render_recipe(
            chunk.chunk_id,
            {"kind": "code", "lang": "python", "src": str(render)},
        )
        n = self.store.drafts.link_figure_plots(
            chunk.chunk_id, [t.chunk_id for t in targets]
        )
        self.sync_draft_links(ref_id)
        self._attribute_touch([chunk.chunk_id])
        return Response(
            body=(
                f"added graph figure {chunk.dc} to {slug} "
                f"(plots {n} data source{'' if n == 1 else 's'}); "
                "image renders out-of-band — render pending"
            )
        )

    # ── edit: text or move ───────────────────────────────────────────

    def _render_draft_dry_run(
        self,
        dc: str,
        old_text: str,
        new_text: str,
        *,
        mode: str,
        note: str = "",
    ) -> Response:
        """Preview a would-be text edit without writing (gr48518).

        ``mode='diff'`` (dry_run=True) → a unified diff of the chunk's
        current vs proposed text; ``mode='full'`` → the whole post-edit
        chunk text. Nothing is persisted.
        """
        if old_text == new_text:
            return Response(
                body=f"[dry-run] {dc}: no change — pre and post are identical{note}"
            )
        if mode == "full":
            head = f"[dry-run] {dc} — full post-edit text (nothing written){note}:"
            return Response(body=f"{head}\n\n{new_text}")
        diff = format_unified_diff(old_text, new_text, file_label=str(dc)).rstrip("\n")
        head = f"[dry-run] {dc} — nothing written{note}. Proposed diff:"
        return Response(body=f"{head}\n\n{diff or '(no diff)'}")

    def edit(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        find: str | None = None,
        move: dict[str, Any] | None = None,
        style: str | None = None,
        list_kind: str | None = None,
        word_target: dict[str, Any] | None = None,
        base_sha: str | None = None,
        authors: list[dict[str, Any]] | str | None = None,
        not_abbrev: list[str] | str | None = None,
        sub: dict[str, Any] | str | None = None,
        apply: bool = False,
        permission: dict[str, Any] | None = None,
        origin: str | None = None,
        table: str | dict[str, Any] | None = None,
        caption: str | None = None,
        regen: dict[str, Any] | None = None,
        cell: str | dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        voice: str | None = None,
        lang: str | None = None,
        review: str | None = None,
        verdict: str = "approved",
        authoring: bool | str | None = None,
        scaffold: str | None = None,
        dry_run: bool | str = False,
        source: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        # ``source`` is a code/provenance-caller convenience (not documented
        # on the general edit surface): forwarded verbatim to
        # ``store.drafts.edit_text``'s ``chunk_events.source`` payload for the two
        # plain-text-mutation paths (find-replace + whole-chunk rewrite)
        # only. Lets a caller that knows *why* it's editing — e.g. the
        # grounded-authoring reviewer persona stamping
        # ``source={'authored_by': 'review:<lens>'}`` — leave that on the
        # append-only edit log, queryable per-chunk, without a new store
        # primitive or a ``chunks.meta`` write (which would collide with the
        # ``meta=`` term-attrs patch branch below).
        #
        # ``dry_run`` is advertised on the shared edit surface as "preview
        # without writing". It used to be swallowed in ``**_kw`` and the edit
        # *applied anyway* — a data-loss footgun (gr48518). It is now honored
        # for the text-mutation paths (whole-chunk rewrite + find-replace):
        # ``dry_run=True`` renders a unified diff and writes nothing, so a
        # scary "massive rewrite" can be eyeballed first; ``dry_run='full'``
        # shows the whole post-edit chunk. The structural / metadata ops
        # (move, style, table, authors, …) have no diff semantics and reject
        # the flag rather than silently writing; the regex ``sub`` op has its
        # own apply=-gated preview.
        dry_mode = normalize_dry_run(dry_run)

        def _reject_dry_run(op: str) -> None:
            if dry_mode is not None:
                raise BadInput(
                    f"dry_run has no preview for the '{op}' draft op — only "
                    "text edits (whole-chunk rewrite + find-replace) render a "
                    "diff. This op writes directly; omit dry_run to apply it.",
                    next="for a text preview: edit(kind='draft', id='dc<id>', "
                    "text='…', dry_run=True)",
                )

        # ``title`` is a draft-level op (rename the document) — id is the slug
        # (or any handle in the draft), not a single chunk. Writes BOTH
        # ``refs.title`` and the title heading chunk (``store.drafts.set_draft_title``):
        # the heading was always editable while the ref title had no write path
        # at all, so the two could diverge — the reader showing one name and
        # every search hit / list row / link chip another.
        if title is not None:
            _reject_dry_run("title")
            ref = self._resolve_draft_any(id)
            old, synced = self.store.drafts.set_draft_title(
                ref.id, title, source={"reason": "draft-title", "actor": "draft-edit"}
            )
            note = "" if synced else " (no title heading — ref renamed only)"
            return Response(
                body=f"renamed {ref.slug or ref.id}: {old!r} → {title.strip()!r}{note}"
            )
        # ``authors`` is a draft-level op (set the byline + affiliations) —
        # id is the slug (or any handle in the draft), not a single chunk.
        # Stored on the draft ref's first-class ``authors`` column, so the
        # exporters + web reader render a byline; ROR ids on each entry
        # join to the canonical institution (https://ror.org).
        if authors is not None:
            _reject_dry_run("authors")
            ref = self._resolve_draft_any(id)
            entries = to_author_dicts(authors)
            self.store.update_paper_fields(ref.id, authors=entries, source="draft-edit")
            n = len(entries)
            affil = sum(1 for e in entries if e.get("affiliation"))
            note = f" ({affil} with affiliation)" if affil else ""
            return Response(
                body=f"set {n} author{'s' if n != 1 else ''} on {ref.slug or ref.id}{note}"
            )
        # ``not_abbrev`` is a draft-level op (silence the undefined-abbrev
        # hint) — id may be the slug or any ¶handle in the draft.
        if not_abbrev:
            _reject_dry_run("not_abbrev")
            tokens = [not_abbrev] if isinstance(not_abbrev, str) else list(not_abbrev)
            ref = self._resolve_draft_any(id)
            self.store.drafts.add_abbrev_ignore(ref.id, tokens)
            return Response(body=f"marked not-an-abbrev: {', '.join(tokens)}")
        # ``authoring`` is a draft-level op (paper-writing pipeline rung 3e,
        # the per-document auto-author toggle) — id is the slug (or any
        # handle in the draft), not a single chunk. When on, the grounded
        # review lenses (``cites``/``structure``) EDIT the draft instead of
        # only filing findings (``quest/review_fanout.py``'s
        # ``mint_review_fanout`` ORs this into its ``author`` decision).
        if authoring is not None:
            _reject_dry_run("authoring")
            ref = self._resolve_draft_any(id)
            on = _coerce_authoring(authoring)
            self.store.stamp_ref_meta(ref.id, {"authoring_enabled": on})
            return Response(
                body=f"auto-author {'ON' if on else 'OFF'} for {ref.slug or ref.id}"
            )
        # ``scaffold`` is a draft-level op (paper-writing pipeline rung 4,
        # docs/backlog/paper-writing-pipeline.md §"Document classes"): lays
        # down a genre's standard section skeleton, the
        # same table the web ``/drafts/new`` picker uses — id is the slug
        # (or any handle in the draft), not a single chunk.
        if scaffold is not None:
            _reject_dry_run("scaffold")
            sections = _SCAFFOLDS.get(scaffold)
            if sections is None:
                raise BadInput(
                    f"unknown scaffold class {scaffold!r}",
                    next=f"scaffold= one of {sorted(_SCAFFOLDS)}",
                )
            ref = self._resolve_draft_any(id)
            handles = self.store.drafts.scaffold_sections(ref.id, sections)
            new_chunks = [self.store.drafts.get_draft_chunk(h) for h in handles]
            self._attribute_touch([c.chunk_id for c in new_chunks if c is not None])
            if not handles:
                return Response(body=f"{scaffold} scaffold is empty — nothing added")
            n = len(handles)
            return Response(
                body=(
                    f"scaffolded {n} section{'' if n == 1 else 's'} on "
                    f"{ref.slug or ref.id} ({scaffold}): {' '.join(handles)}"
                )
            )
        # ``sub`` is a draft-level regex substitution (vi ``:%s/a/b/``) — id is
        # the *scope* (a slug for the whole draft, or a dc<id> for one
        # section/chunk), not a single chunk. Dry-run by default; apply=True
        # commits.
        if sub is not None:
            # sub has its own preview: apply=False (the default) is a dry run,
            # apply=True commits. dry_run= is redundant/ambiguous here.
            if dry_mode is not None:
                raise BadInput(
                    "dry_run is not used with sub= — the regex substitution op "
                    "previews by default (apply=False) and commits on apply=True.",
                    next="edit(kind='draft', id=<scope>, sub={...})  # preview; "
                    "add apply=True to commit",
                )
            # A ``sub=`` addressed straight at a chunk_kind='table' chunk (not
            # a slug/subtree scope) is the regex cell-level find-replace from
            # the shipped draft-table-editing proposal (item 1, git
            # history) — fall through to
            # the normal handle/_base resolution below so the ``is_table``
            # branch routes it to ``_edit_table``. Otherwise (a slug, a
            # subtree, or a non-table chunk) keep the original multi-chunk
            # substitute, which treats 'table' as a derived kind and skips it
            # (draft_regex.DERIVED_KINDS).
            _sub_target = None
            if id is not None and _is_draft_chunk_addr(str(id).strip()):
                _sub_target = self.store.drafts.get_draft_chunk(str(id).strip())
            if _sub_target is None or _sub_target.chunk_kind != "table":
                return self._substitute(id, sub, apply=bool(apply))
        handle = self._require_chunk_id(id, verb="edit")
        # Normalize a ``dc<id>`` address to the legacy base-58 anchor the
        # store mutators still key on; the agent-facing emit uses ``.dc``.
        _base = self.store.drafts.get_draft_chunk(handle)
        if _base is None:
            raise NotFound(f"draft chunk {handle!r} not found")
        self._refuse_if_machine_owned(int(_base.ref_id))
        handle = _base.handle
        # dry_run previews only the text-mutation paths (find-replace / rewrite,
        # handled below). The structural / metadata ops write in place and have
        # no diff semantics — reject rather than silently write (gr48518).
        if dry_mode is not None and (
            permission is not None
            or origin is not None
            or style is not None
            or list_kind is not None
            or word_target is not None
            or move is not None
            or table is not None
            or regen is not None
            or voice is not None
            or lang is not None
            or review is not None
            or _base.chunk_kind == "table"
        ):
            _reject_dry_run("structural")
        if review is not None:
            # Review ledger (rung 3, docs/backlog/paper-writing-pipeline.md
            # §"Review — the memoized approval
            # ledger"): records that `review=` (the checker, e.g. 'human',
            # 'cites', 'flow') evaluated this chunk at its *current*
            # content_sha, with `verdict=` (free text, default 'approved').
            # Metadata-only — no re-embed, no text touched.
            #
            # `verdict='retract'` is the un-review op instead (smartdraft-
            # review-status-ui item 7): deletes the ledger row for
            # `(chunk, review)` via `Store.retract_review` rather than
            # upserting a fresh approval — the edit-door twin of the web
            # reader's `POST /drafts/{ident}/review/retract`.
            if verdict == "retract":
                existed = self.store.drafts.retract_review(_base.chunk_id, review)
                if not existed:
                    return Response(body=f"no {review} review to retract on {_base.dc}")
                return Response(body=f"retracted {review} review on {_base.dc}")
            sha = self.store.drafts.record_review(
                _base.chunk_id, review, verdict=verdict
            )
            return Response(
                body=f"recorded {review} review on {_base.dc} @ {sha[:12]}… → {verdict}"
            )
        if permission is not None or origin is not None:
            # Edit a figure's provenance — caption/bytes untouched.
            if origin is not None and origin not in _FIGURE_ORIGINS:
                raise BadInput(
                    f"figure origin= must be one of {list(_FIGURE_ORIGINS)}",
                    next="origin='original' | 'own_graph' | 'third_party'",
                )
            c = self.store.drafts.set_figure_provenance(
                handle, permission=permission, origin=origin
            )
            return Response(body=f"updated figure provenance {(c or _base).dc}")
        if style is not None:
            # Set/clear the heading's section style. Metadata-only
            # (meta.style = a skill slug) — no re-embed.
            c = self.store.drafts.set_chunk_style(handle, style or None)
            dc = (c or _base).dc
            if style:
                return Response(body=f"styled {dc} → {style}")
            return Response(body=f"cleared style on {dc}")
        if list_kind is not None:
            # Switch a ulist/olist container's kind, or dissolve it to normal
            # text (migration 0037). Structural — no text touched.
            dc = _base.dc
            self.store.drafts.set_list_kind(handle, list_kind)
            if list_kind == "normal":
                return Response(body=f"dissolved list {dc} → normal text")
            return Response(body=f"set list {dc} → {list_kind}")
        if voice is not None or lang is not None:
            # Set this chunk's narration routing (audio export): which Kokoro
            # voice + language phonemizer speaks it. Validated against the voice
            # catalogue; metadata-only (no re-embed), so it patches in place.
            from precis.tts import voices as _voices

            try:
                _v, _lg = _voices.resolve(voice, lang)
            except ValueError as exc:
                raise BadInput(
                    str(exc),
                    next="voices: get(kind='skill', id='precis-audio-help')",
                ) from exc
            self.store.drafts.patch_chunk_meta(handle, {"voice": _v, "lang": _lg})
            return Response(body=f"set narration on {_base.dc}: voice={_v} lang={_lg}")
        if meta is not None:
            # Patch a registry ``term`` leaf's attribute bag / hover surfaces
            # in place: manufacturer / mpn / url / ordering, and the
            # short / surface_forms surfaces. Metadata-only — no re-embed.
            _reject_dry_run("meta")
            c = self.store.drafts.set_term_attrs(handle, meta)
            return Response(body=f"updated term attributes {c.dc}" if c else "updated")
        if word_target is not None:
            # Set/clear a heading section's word limit (proposal writing).
            # ``word_target={'min':200,'max':400}`` sets it; ``{}`` clears.
            target = _coerce_word_target(word_target)
            c = self.store.drafts.set_word_target(handle, target)
            dc = (c or _base).dc
            if target:
                lo, hi = target
                return Response(
                    body=(
                        f"set word target on {dc} → {lo}–{hi} words "
                        f"(check with get(kind='draft', view='wordcount'))"
                    )
                )
            return Response(body=f"cleared word target on {dc}")
        if move is not None:
            c = self.store.drafts.move_chunk(handle, move)
            if c is not None:
                self._attribute_touch([c.chunk_id])
            return Response(body=f"moved {(c or _base).dc}")
        is_table = _base.chunk_kind == "table"
        if is_table or table is not None or regen is not None:
            return self._edit_table(
                handle,
                _base,
                table=table,
                caption=caption,
                regen=regen,
                cell=cell,
                find=find,
                text=text,
                sub=sub,
                base_sha=base_sha,
            )
        # ``find=`` substitutes *within* the chunk — never a wholesale
        # overwrite. Presence of ``find`` is the sole signal: the wire
        # defaults ``mode='find-replace'`` on *every* edit (tools.core),
        # so a plain ``edit(id, text=)`` rewrite also arrives with that
        # mode — gating on it would wrongly demand ``find=`` on every
        # whole-chunk rewrite. Historically ``find`` fell into ``**_kw``
        # and was dropped, so ``edit(mode='find-replace', find=, text=)``
        # clobbered the whole chunk with just ``text`` (gr48203 — a
        # data-loss footgun). Now ``find`` drives a literal substitution,
        # and a ``find`` that is absent from the chunk *refuses* the edit
        # (leaving the text untouched) instead of erasing it.
        if find is not None:
            if not find:
                raise BadInput(
                    "find= must be a non-empty string (the exact text to "
                    "locate within the chunk)",
                    next=(
                        f"edit(kind='draft', id={_base.dc!r}, "
                        "find='old text', text='new text')"
                    ),
                )
            if text is None:
                raise BadInput(
                    "find-replace requires text=. Pass text='' to DELETE the "
                    "matched span, or text='<replacement>' to substitute it.",
                    next=(
                        f"edit(kind='draft', id={_base.dc!r}, find={find!r}, "
                        "text='')  # delete the span"
                    ),
                )
            prior = self.store.drafts.get_draft_chunk(str(handle).lstrip("¶"))
            old_text = prior.text if prior else ""
            if find not in old_text:
                raise NotFound(
                    f"find= text not present in {_base.dc} — nothing replaced, "
                    "the chunk was left unchanged. Re-read the exact current text.",
                    next=f"get(kind='draft', id={_base.dc!r})",
                )
            occurrences = old_text.count(find)
            new_text = old_text.replace(find, str(text))
            if dry_mode is not None:
                note = (
                    f" ({occurrences} occurrences of find= would be replaced)"
                    if occurrences > 1
                    else ""
                )
                return self._render_draft_dry_run(
                    _base.dc, old_text, new_text, mode=dry_mode, note=note
                )
            c = self.store.drafts.edit_text(
                handle, new_text, base_sha=base_sha, source=source
            )
            body = f"edited {c.dc}" if c else "edited"
            if c is not None:
                if occurrences > 1:
                    body += f" ({occurrences} occurrences of find= replaced)"
                self.sync_draft_links(c.ref_id)
                self._attribute_touch([c.chunk_id])
                ref = self.store.get_ref(kind="draft", id=int(c.ref_id))
                slug = ref.slug if ref and ref.slug else str(c.ref_id)
                body += _draft_lint.write_abbrev_hints(
                    self.store, slug, c.ref_id, new_text, old_text
                )
                body += _draft_lint.citation_form_hint(new_text)
                body += _draft_lint.whole_paper_cite_hint(new_text, old_text)
                body += _draft_lint.pc_cite_claim_hub_hint(self.store, new_text)
                body += _draft_lint.literal_cite_hint(new_text)
                body += _draft_lint.temperature_form_hint(new_text)
                body += _draft_lint.dangling_edit_hint(self.store, new_text, old_text)
            return Response(body=body)
        if text is not None:
            # Capture the prior text *before* the rewrite so the abbrev
            # hints fire only on what this edit introduced (not on
            # acronyms already living in the chunk — the MOF re-nag).
            prior = self.store.drafts.get_draft_chunk(str(handle).lstrip("¶"))
            old_text = prior.text if prior else ""
            if dry_mode is not None:
                return self._render_draft_dry_run(
                    _base.dc, old_text, str(text), mode=dry_mode
                )
            c = self.store.drafts.edit_text(
                handle, str(text), base_sha=base_sha, source=source
            )
            body = f"edited {c.dc}" if c else "edited"
            if c is not None:
                self.sync_draft_links(c.ref_id)
                self._attribute_touch([c.chunk_id])
                ref = self.store.get_ref(kind="draft", id=int(c.ref_id))
                slug = ref.slug if ref and ref.slug else str(c.ref_id)
                body += _draft_lint.write_abbrev_hints(
                    self.store, slug, c.ref_id, str(text), old_text
                )
                body += _draft_lint.citation_form_hint(str(text))
                body += _draft_lint.whole_paper_cite_hint(str(text), old_text)
                body += _draft_lint.pc_cite_claim_hub_hint(self.store, str(text))
                body += _draft_lint.literal_cite_hint(str(text))
                body += _draft_lint.temperature_form_hint(str(text))
                body += _draft_lint.dangling_edit_hint(self.store, str(text), old_text)
            return Response(body=body)
        raise BadInput(
            "edit(kind='draft') requires text= (rewrite), move= (reorder/reparent), "
            "style= (set a heading's section style), word_target= (set a heading's "
            "word limit), authors= (set the byline + affiliations), sub= (regex "
            "substitute across a draft/section), or not_abbrev= (silence the "
            "abbrev hint)",
            next="edit(kind='draft', id='dc<chunk_id>', text='…')",
        )

    # ── delete: soft-retire ──────────────────────────────────────────

    def delete(
        self,
        *,
        id: str | int | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        handle = self._require_chunk_id(id, verb="delete")
        chunk = self.store.drafts.get_draft_chunk(handle)
        if chunk is None:
            raise NotFound(f"draft chunk {handle!r} not found")
        self._refuse_if_machine_owned(int(chunk.ref_id))
        self.store.drafts.retire_chunk(chunk.handle, mode=mode)
        self.sync_draft_links(chunk.ref_id)
        return Response(body=f"retired {chunk.dc}")

    # ── helpers ──────────────────────────────────────────────────────

    def _resolve_draft_any(self, id: str | int | None) -> Any:
        """Resolve a draft ref from either its slug or a ¶handle (a chunk
        in it), refusing a machine-owned target (:meth:`_refuse_if_machine_owned`).
        Used by every draft-level edit op (``title=``/``authors=``/
        ``not_abbrev=``/``authoring=``/``scaffold=``) — all mutating, so
        every caller wants the guard."""
        s = str(id or "").strip()
        if _is_draft_chunk_addr(s):
            chunk = self.store.drafts.get_draft_chunk(s)
            if chunk is None:
                raise NotFound(f"draft chunk {s} not found")
            ref = self.store.get_ref(kind="draft", id=int(chunk.ref_id))
            if ref is None:
                raise NotFound(f"draft for chunk {s} not found")
        else:
            ref = resolve_live_slug_ref(self.store, kind="draft", id=s)
        self._refuse_if_machine_owned(ref.id)
        return ref

    def _machine_owner(self, ref_id: int) -> tuple[str, int, str, str | None] | None:
        """``(relation, owner_ref_id, owner_kind, owner_title)`` iff ``ref_id``
        is the SOURCE of an outbound ``dossier-of``/``paper-of`` link (the
        owning process is the link's ``dst``) — else ``None``. One query
        (link + owner ref joined), only ever run from a
        put/edit/delete entry point on a draft that's about to be mutated
        — never from a read path (``get``/``search``)."""
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT l.relation, l.dst_ref_id, r.kind, r.title "
                "FROM links l JOIN refs r ON r.ref_id = l.dst_ref_id "
                "WHERE l.src_ref_id = %s AND l.relation = ANY(%s) LIMIT 1",
                (ref_id, list(_MACHINE_OWNED_RELATIONS)),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), int(row[1]), str(row[2]), (str(row[3]) if row[3] else None)

    def _refuse_if_machine_owned(self, ref_id: int) -> None:
        """Raise :class:`Unsupported` when the draft ``ref_id`` is a
        process's machine-managed body (a quest dossier, or its paper
        projection — see the ``_DOSSIER_RELATION``/``_PAPER_RELATION``
        module note).

        Its structure — one code/model-rewritten narrative chunk plus
        ``meta.pinned`` ledger/frontier chunks — is a machine invariant
        enforced by the owning process's own write path
        (:mod:`precis.quest.dossier`, :mod:`precis.quest.tick`), never by
        this handler; markdown-looking prose inside it is intentional
        storage for that process, not authoring debt. This is the exact
        incident this guard exists for: a generic draft-hygiene todo once
        "cleaned up" a dossier through this surface and silently lost its
        entire attempt-tree ledger. Points the caller at the owning ref
        instead of retrying a different write shape.

        The only DB work on the common (unowned) path is
        :meth:`_machine_owner`'s single query — the slug lookup below only
        runs once a violation is already confirmed, on the way to raising.
        """
        owner = self._machine_owner(ref_id)
        if owner is None:
            return
        relation, owner_id, owner_kind, owner_title = owner
        noun = "dossier" if relation == _DOSSIER_RELATION else "reader-facing paper"
        owner_handle = handle_registry.try_format(owner_kind, owner_id) or (
            f"{owner_kind}:{owner_id}"
        )
        label = f" ({owner_title})" if owner_title else ""
        self_ref = self.store.get_ref(kind="draft", id=ref_id, include_deleted=True)
        slug_label = self_ref.slug if self_ref and self_ref.slug else ref_id
        raise Unsupported(
            f"draft {slug_label!r} is the {noun} of {owner_handle}{label} "
            f"(a {relation} machine invariant) — it is written and "
            "structured by that process's own code, not by hand: exactly "
            "one narrative chunk (whole-rewritten every cycle) plus "
            "code/model-managed pinned chunks (a ledger, a frontier tree). "
            "That structure is not a formatting defect to refactor, and "
            "markdown-looking content inside it is intentional storage, "
            "not authoring debt — a generic 'clean this up' pass must "
            "stop here rather than retry a different edit shape. "
            f"put/edit/delete are refused on this draft through this handler.",
            next=(f"inspect the owning process instead: get(id={owner_handle!r})"),
        )

    def _require_chunk_id(self, id: str | int | None, *, verb: str) -> str:
        if id is None or not _is_draft_chunk_addr(str(id)):
            raise BadInput(
                f"{verb}(kind='draft') targets a chunk — id='dc<chunk_id>'",
                next=f"{verb}(kind='draft', id='dc42', …)",
            )
        return str(id)

    #: Kinds whose chunks are citable literature: a reference to one is a
    #: ``cites`` edge (the bibliography / "who cites this?" graph), not a
    #: ``related-to`` provenance edge. Citations are to the literature;
    #: links (a memory, another draft) are to our own notes.
    _CITABLE_KINDS: ClassVar[frozenset[str]] = frozenset({"paper", "patent", "finding"})

    def sync_draft_links(self, ref_id: int) -> None:
        """Materialise graph edges from this draft to every ref its chunks
        reference — the superset grammar (``kind:ref`` mentions, ``¶``
        cross-refs, ``§``/``[pc<id>]`` citations). A reference to a
        **citable source** (paper/patent/finding) becomes a ``cites``
        edge; every other reference (a memory, another draft) is a
        ``related-to`` provenance edge.

        Edges are **chunk-grounded on the source side**: each reference is
        resolved against the individual chunk it sits in, so the edge
        carries that draft chunk as ``src_pos`` (``dc<id>``-granular) — a
        reader can then see *which passage* cites a finding/paper, not just
        that the draft as a whole does, which is what lets the citation
        tree resolve to the originating paragraph. (Resolving over the
        whole concatenated draft, as this once did, threw the source chunk
        away and every edge landed ref-level ``dr<id>``.)

        Recomputed over the whole draft on each write, replacing the prior
        ``auto='mention'`` set in BOTH relations so a removed reference
        loses its edge. Best-effort: a resolution failure never fails the
        write — mirrors the note autolinker
        (`_numeric_ref._sync_mention_links`).
        """
        from precis.utils import draft_markup

        try:
            chunks = self.store.drafts.reading_order(ref_id)
            ord_by_chunk = self.store.drafts.chunk_ord_map(ref_id)
            # Resolve per chunk so the source draft chunk (its ord) is
            # preserved. (src_ord, dst_ref_id, dst_pos) → desired relation.
            resolved: list[tuple[int | None, Any]] = []
            dst_ids: set[int] = set()
            for c in chunks:
                targets = draft_markup.resolve_draft_link_targets(
                    self.store, c.text, exclude_ref_id=ref_id
                )
                if not targets:
                    continue
                src_ord = ord_by_chunk.get(c.chunk_id)
                for t in targets:
                    resolved.append((src_ord, t))
                    dst_ids.add(t.dst_ref_id)
            refs_by_id = self.store.fetch_refs_by_ids(list(dst_ids))
            wanted: dict[tuple[int | None, int, int | None], str] = {}
            for src_ord, t in resolved:
                tref = refs_by_id.get(t.dst_ref_id)
                rel = (
                    "cites"
                    if tref is not None and tref.kind in self._CITABLE_KINDS
                    else "related-to"
                )
                wanted[(src_ord, t.dst_ref_id, t.dst_pos)] = rel
            # Drop stale auto-mention edges in BOTH relations (a removed
            # reference, one whose routed relation changed, or one that
            # moved to a different source chunk).
            for relation in ("cites", "related-to"):
                for link in self.store.links_for(
                    ref_id, direction="out", relation=relation
                ):
                    if (link.meta or {}).get("auto") != "mention":
                        continue
                    key = (link.src_pos, link.dst_ref_id, link.dst_pos)
                    if wanted.get(key) != relation:
                        self.store.remove_link(
                            src_ref_id=ref_id,
                            src_pos=link.src_pos,
                            dst_ref_id=link.dst_ref_id,
                            dst_pos=link.dst_pos,
                            relation=relation,
                        )
            for (src_ord, dst, pos), relation in wanted.items():
                self.store.add_link(
                    src_ref_id=ref_id,
                    src_pos=src_ord,
                    dst_ref_id=dst,
                    dst_pos=pos,
                    relation=relation,
                    set_by="agent",
                    meta={"auto": "mention"},
                )
        except Exception:
            log.warning(
                "draft: autolink mentions failed for ref %s", ref_id, exc_info=True
            )

    def _attribute_touch(self, chunk_ids: list[int]) -> None:
        """Attribute the just-written chunks to the current agent run.

        A no-op unless ``PRECIS_CURRENT_AGENTLOG`` is set (the runner
        threads it onto the ``claude -p`` subprocess); an operator console
        edit or a test that didn't open a log just skips attribution.
        Best-effort — never fails the write."""
        from precis import agentlog

        agentlog.touch_from_env(self.store, chunk_ids=chunk_ids)

    # ── data/table chunks ──────────────────────────────

    def _put_table(
        self,
        slug: str,
        ref: Any,
        *,
        table: str | dict[str, Any] | None,
        caption: str | None,
        regen: dict[str, Any] | None,
        at: dict[str, Any] | None,
        meta: dict[str, Any] | None,
    ) -> Response:
        """Add a ``chunk_kind='table'`` data chunk: canonical ``meta.table``
        + derived markdown ``text``. ``meta.regen`` (provenance/how-to-rebuild)
        and ``meta.caption`` (legend) are stamped verbatim — both inert, no
        execution."""
        if table is None:
            raise BadInput(
                "a table chunk requires table={header, rows}",
                next=(
                    f"put(kind='draft', id={slug!r}, chunk_kind='table', "
                    "table={'header': ['x','y'], 'rows': [[1,2],[3,4]]}, "
                    "caption='…', at={'last': True})"
                ),
            )
        norm = normalize_table(table)
        cap = caption.strip() if caption and caption.strip() else None
        md = table_to_markdown(norm, caption=cap)
        chunk_meta = dict(meta or {})
        chunk_meta["table"] = norm
        if cap is not None:
            chunk_meta["caption"] = cap
        if regen is not None:
            chunk_meta["regen"] = regen
        chunks = self.store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="table",
            text=md,
            at=at,
            meta=chunk_meta,
            split=False,
        )
        self.sync_draft_links(ref.id)
        self._attribute_touch([c.chunk_id for c in chunks])
        c = chunks[0]
        rows, cols = len(norm["rows"]), len(norm["header"])
        return Response(
            body=(
                f"added table {c.dc} to {slug} ({rows} row"
                f"{'' if rows == 1 else 's'} × {cols} col"
                f"{'' if cols == 1 else 's'}); text is the derived markdown — "
                f"edit table=/caption=/regen=, not text="
            )
        )

    def _edit_table(
        self,
        handle: str,
        chunk: Any,
        *,
        table: str | dict[str, Any] | None,
        caption: str | None,
        regen: dict[str, Any] | None,
        cell: str | dict[str, Any] | None = None,
        find: str | None = None,
        text: str | None = None,
        sub: dict[str, Any] | str | None = None,
        base_sha: str | None,
    ) -> Response:
        """Edit a chunk_kind='table' chunk — precedence (the shipped draft-table-editing proposal item 1, git history): (1) ``table=`` replaces the whole
        canonical structure; (2) ``cell=`` (A1 string or ``{row,col}``, 1-based,
        row 1 = header) + ``text=`` sets ONE field via :func:`set_cell`; (3)
        ``find=`` (literal, paired with ``text=`` as the replacement) or
        ``sub=`` (regex, ``{find,replace,flags}``/``s/…/…/``) find-replaces
        across every string cell via :func:`find_replace_cells`, refusing (chunk
        untouched) on zero matches — mirrors the prose find-replace guard;
        (4) ``caption=``/``regen=`` alone patch metadata only; (5) otherwise a
        table's ``text=`` is derived, never hand-edited — reject.
        Whichever path fires, the markdown is re-derived from the SAME
        resolved data + caption and persisted through one ``edit_text`` call."""
        if chunk is None or chunk.chunk_kind != "table":
            raise BadInput(
                "table=/cell=/find=/sub=/caption=/regen= apply only to a "
                "chunk_kind='table' chunk",
                next="edit(kind='draft', id='dc<chunk_id>', table={…})",
            )
        # Exactly one *mutation selector* per edit — table=/cell=/find=/sub=
        # each pick a different data-mutation path (full replace / one field
        # / cell find-replace), and silently favoring one over the others
        # (the old table > cell > find/sub fallthrough) is a footgun: a
        # caller who passes two believes both applied. caption=/regen= are
        # NOT selectors — they're metadata that may legitimately ride along
        # with table= (set data + legend together); text= is the operand for
        # cell=/find=, not a selector itself.
        _selectors = [
            name
            for name, val in (
                ("table", table),
                ("cell", cell),
                ("find", find),
                ("sub", sub),
            )
            if val is not None
        ]
        if len(_selectors) > 1:
            raise BadInput(
                f"conflicting table edit selectors: {', '.join(s + '=' for s in _selectors)} "
                "— pass only one of table=/cell=/find=/sub= per edit",
                next=f"edit(kind='draft', id={chunk.dc!r}, {_selectors[0]}=…)  "
                "# one selector at a time",
            )
        cur = self.store.drafts.draft_chunk_meta(handle)
        cur_table = cur.get("table")
        no_data_err = BadInput(
            "this table chunk has no stored data — pass table={header, rows}",
            next="edit(kind='draft', id='dc<chunk_id>', table={'header': […], 'rows': […]})",
        )
        replace_count: int | None = None
        if table is not None:
            norm = normalize_table(table)
        elif cell is not None:
            if text is None:
                raise BadInput(
                    "cell= addresses one field and needs text= for its new value",
                    next=f"edit(kind='draft', id={chunk.dc!r}, cell={cell!r}, "
                    "text='…')",
                )
            if not cur_table:
                raise no_data_err
            norm = set_cell(cur_table, cell, str(text))
        elif find is not None or sub is not None:
            if not cur_table:
                raise no_data_err
            if find is not None:
                if not find:
                    raise BadInput(
                        "find= must be a non-empty string (the exact cell "
                        "text to locate)",
                        next=f"edit(kind='draft', id={chunk.dc!r}, "
                        "find='old', text='new')",
                    )
                if text is None:
                    raise BadInput(
                        "find-replace requires text= (the replacement "
                        "value; pass '' to blank the matched cell content)",
                        next=f"edit(kind='draft', id={chunk.dc!r}, "
                        f"find={find!r}, text='')",
                    )
                pattern, replacement, is_regex = find, str(text), False
            else:
                assert sub is not None
                f, r, flags = self._parse_sub_expr(sub)
                # find_replace_cells takes a bare pattern (no separate flags
                # arg) — fold the vi-style case-fold/dot-all letters in as an
                # inline group; 'm' (multiline) is a no-op on a single cell.
                prefix = "".join(f"(?{c})" for c in flags if c in "is")
                pattern, replacement, is_regex = prefix + f, r, True
            norm, replace_count = find_replace_cells(
                cur_table, pattern, replacement, regex=is_regex
            )
            if replace_count == 0:
                raise BadInput(
                    f"no cell matches /{pattern}/ in {chunk.dc} — nothing "
                    "replaced, the table was left unchanged.",
                    next=f"get(kind='draft', id={chunk.dc!r})",
                )
        elif caption is not None or regen is not None:
            norm = cur_table or {}
        else:
            raise BadInput(
                "a table chunk's text is derived from its data — pass "
                "find=, cell=, table=, or caption= (not text=)",
                next=f"edit(kind='draft', id={chunk.dc!r}, find='old', text='new')",
            )
        if not norm:
            raise no_data_err
        # Caption: an explicit string (even "") replaces the legend; None
        # keeps the stored one. Derive the markdown from the SAME resolved
        # caption we persist, so clearing a caption drops the ``**…**`` lead
        # line instead of leaving it stranded in the derived text (one-source,
        # no drift — the empty-string clear used to zero meta.caption while the
        # markdown kept the old legend).
        if caption is not None:
            cap = caption.strip() or None
        else:
            cap = cur.get("caption") or None
        md = table_to_markdown(norm, caption=cap)
        patch: dict[str, Any] = {"table": norm}
        if caption is not None:
            patch["caption"] = cap or ""
        if regen is not None:
            patch["regen"] = regen
        c = self.store.drafts.edit_text(handle, md, base_sha=base_sha, meta_patch=patch)
        if c is not None:
            self._attribute_touch([c.chunk_id])
            self.sync_draft_links(c.ref_id)
        rows, cols = len(norm["rows"]), len(norm["header"])
        extra = f" ({replace_count} replacement(s))" if replace_count else ""
        return Response(
            body=f"edited table {(c or chunk).dc} ({rows}×{cols}){extra}; "
            "markdown re-derived"
        )

    def _resolve_project(self, project: str | int) -> int:
        raw = str(project).strip()
        raw = raw.split(":", 1)[1] if raw.startswith("todo:") else raw
        try:
            pid = int(raw)
        except ValueError as exc:
            raise BadInput(
                f"project must be a todo id, got {project!r}",
                next="project=<int todo id>",
            ) from exc
        ref = self.store.get_ref(kind="todo", id=pid)
        if ref is None:
            raise NotFound(f"project todo {pid} not found")
        return ref.id

    def _render_by_project(self, project: str | int) -> Response:
        """``get(kind='draft', project=…)`` — the draft(s) bound to a
        project todo via the ``draft-of`` link (``create_draft()`` mints
        it 1:1 at creation). One bound draft → its outline (same shape as
        ``get(kind='draft', id='<slug>')``); several → a listing; none →
        ``NotFound`` (mirrors ``_resolve_project``'s "project not found")."""
        pid = self._resolve_project(project)
        links = self.store.links_for(pid, direction="in", relation="draft-of")
        refs = []
        for link in links:
            ref = self.store.get_ref(kind="draft", id=link.src_ref_id)
            if ref is not None:
                refs.append(ref)
        if not refs:
            raise NotFound(
                f"no draft bound to project {pid} (draft-of link)",
                next=f"put(kind='draft', id='<slug>', title='…', project={pid})",
            )
        if len(refs) == 1:
            ref = refs[0]
            return self._render_outline(str(ref.slug or ref.id), ref)
        lines = [f"{len(refs)} draft(s) bound to project {pid}:\n"]
        for ref in refs:
            lines.append(f"- {ref.slug or ref.id}: {ref.title}")
        return Response(body="\n".join(lines))

    def _render_list(self) -> Response:
        return render_slug_ref_list(
            self.store,
            kind="draft",
            label_plural="draft(s)",
            empty_body="no drafts yet — put(kind='draft', id='…', project=<todo>)",
        )

    def _render_outline(self, slug: str, ref: Any) -> Response:
        chunks = self.store.drafts.reading_order(ref.id)
        # Per-block gloss preference: the llm-v1 summary, else the keyword
        # set, else the truncated first line. Lets the outline read as
        # *meaning* once the summarize/keyword workers have run, degrading
        # to the raw-text peek for blocks they haven't reached yet.
        views = self.store.drafts.block_views(ref.id)
        n = len(chunks)
        lines = [f"# {ref.title}  ({slug}) — {n} chunk{'' if n == 1 else 's'}\n"]
        for c in chunks:
            v = views.get(c.handle, {})
            gloss = v.get("summary") or v.get("keywords") or ""
            if not gloss:
                gloss = c.text.splitlines()[0] if c.text else ""
            # Flatten to one line: split() drops every whitespace run —
            # spaces, tabs, \n, \r — so a multi-line gloss stays on a single
            # outline row. No length cap: show the full gloss.
            gloss = " ".join(gloss.split())
            lines.append(f"{'  ' * c.depth}{c.dc}  [{c.chunk_kind}] {gloss}")
        lines.extend(self._work_lines(ref.id))
        lines.extend(self._hygiene_lines(ref.id, chunks))
        return Response(body="\n".join(lines))

    def _hygiene_lines(
        self, ref_id: int, chunks: list[Any], *, elide: bool = True
    ) -> list[str]:
        """Whole-draft specificity debt, surfaced on every ``get`` — not
        just a fresh write — so a legacy/bulk-authored draft that never
        passed through an incremental ``put``/``edit`` still gets flagged.
        Two checks, both advisory (never blocking):

        * **undefined abbreviations** — acronym-shaped tokens anywhere in
          the draft with no glossary ``term``/inline definition/silence.
          ``undefined_abbrevs`` normally scopes to one write's new text;
          passing it the whole draft surfaces everything a legacy draft
          never got hinted about.
        * **whole-paper citations** — ``[pa<id>]``/``[pk<id>]`` (no chunk)
          anywhere in the draft, with the ``dc<id>`` they live in so
          they're locatable.

        A third, informational-only line (never a ``⚠``) scoreboards how
        many of the draft's cited passages have a Taproot claim hub
        available to cite instead — see :meth:`_taproot_hub_scoreboard`.

        ``elide=True`` (the outline footer's default) truncates each list to
        8 entries with a "``+N more``" tail and points at
        ``view='hygiene'`` for the rest. ``elide=False``
        (``get(view='hygiene')``) prints every entry — gr192827 item 9: an
        agent clearing 65 undefined abbreviations shouldn't need ~4
        paginated outline round-trips to see the next alphabetical batch.
        """
        out: list[str] = []
        text = "\n\n".join(c.text for c in chunks if c.text)
        limit = 8 if elide else None

        undefined = self.store.drafts.undefined_abbrevs(ref_id, text)
        if undefined:
            shown = ", ".join(undefined if limit is None else undefined[:limit])
            tail = (
                f" (+{len(undefined) - limit} more — see "
                "get(kind='draft', id=<slug>, view='hygiene') for the full list)"
                if limit is not None and len(undefined) > limit
                else ""
            )
            out.append(
                f"⚠ {len(undefined)} undefined abbreviation(s): {shown}{tail}. "
                "put(kind='draft', chunk_kind='term', text='<expansion>', "
                "meta={'short': '<ABBR>'}) to define, or "
                "edit(not_abbrev=['<ABBR>']) to silence."
            )

        whole_refs: list[str] = []
        for c in chunks:
            if not c.text:
                continue
            for bare in _draft_lint.find_whole_ref_citations(c.text):
                whole_refs.append(f"{c.dc}:[{bare}]")
        if whole_refs:
            shown = ", ".join(whole_refs if limit is None else whole_refs[:limit])
            tail = (
                f" (+{len(whole_refs) - limit} more — see "
                "get(kind='draft', id=<slug>, view='hygiene') for the full list)"
                if limit is not None and len(whole_refs) > limit
                else ""
            )
            out.append(
                f"⚠ {len(whole_refs)} whole-paper (non-chunk) citation(s): "
                f"{shown}{tail}. Drill to the supporting chunk when "
                "precision matters — [pc<id>] via search(kind='paper', …) "
                "or get(kind='paper', id='<slug>~lo..hi', view='toc')."
            )

        grounded, total = self._taproot_hub_scoreboard(chunks)
        if grounded:
            out.append(
                f"ℹ taproot: {grounded} of {total} cited passages have a "
                "claim hub available; cite [pub_id] to use it."
            )

        if not out:
            return []
        return ["", "## Hygiene", *out]

    def _render_hygiene(self, slug: str, ref: Any) -> Response:
        """``get(kind='draft', view='hygiene')`` — gr192827 item 9: the
        complete, un-elided hygiene report (undefined abbreviations +
        whole-paper citations), and nothing else — no outline body, no
        WIP block, no pagination of unrelated content."""
        chunks = self.store.drafts.reading_order(ref.id)
        lines = self._hygiene_lines(ref.id, chunks, elide=False)
        header = f"# {ref.title}  ({slug}) — hygiene report"
        if not lines:
            return Response(
                body=f"{header}\n\nclean — no undefined abbreviations or "
                "whole-paper citations found."
            )
        # Drop the leading blank + "## Hygiene" heading _hygiene_lines
        # prepends for the outline footer; this view is already scoped.
        return Response(body="\n".join([header, "", *lines[2:]]))

    def _taproot_hub_scoreboard(self, chunks: list[Any]) -> tuple[int, int]:
        """``(grounded, total)`` over every paper/patent cite token in the
        draft (:func:`~precis.handlers._draft_lint.find_paper_cite_tokens`,
        one pass per chunk — a
        token repeated in two chunks counts as two cited passages).
        ``grounded`` is the subset whose paper resolves and already has
        ≥1 Taproot claim hub (:func:`~precis.taproot.lookup.
        hubs_grounded_by_paper`) available to cite instead. Caches the
        per-paper hub check so a paper cited from many passages costs one
        lookup, not N."""
        from precis.taproot.lookup import hubs_grounded_by_paper
        from precis.utils.mentions import resolve_handle_target

        hub_cache: dict[int, bool] = {}
        grounded = 0
        total = 0
        for c in chunks:
            if not c.text:
                continue
            for tok in _draft_lint.find_paper_cite_tokens(c.text):
                target = resolve_handle_target(self.store, tok)
                if target is None:
                    continue
                total += 1
                has_hub = hub_cache.get(target.dst_ref_id)
                if has_hub is None:
                    has_hub = bool(
                        hubs_grounded_by_paper(self.store, target.dst_ref_id)
                    )
                    hub_cache[target.dst_ref_id] = has_hub
                if has_hub:
                    grounded += 1
        return grounded, total

    def _work_lines(self, ref_id: int) -> list[str]:
        """Surface stuck / in-flight work on this draft (Fix A): the open
        todos in the draft's project subtree that are blocked by a
        failure-bubble or have a live/failed child job. Without this a
        failed enrichment job parks the parent silently and never
        registers when you look at the draft itself."""
        try:
            items = self.store.drafts.draft_attached_work(ref_id)
        except Exception:
            log.warning(
                "draft: attached-work walk failed for %s", ref_id, exc_info=True
            )
            return []
        if not items:
            return []
        out = ["", "## Work in progress"]
        for it in items:
            mark = "⚠ blocked" if it.blocked else "⚙ in flight"
            jobs = _summarize_job_counts(it.jobs)
            suffix = f" — {jobs}" if jobs else ""
            out.append(f"{mark}  todo:{it.todo_id}  {it.title}{suffix}")
        out.append(
            "\nNext: get(kind='todo', id=<id>) to inspect; a blocked todo "
            "carries a child-failed:<job> bubble — retry, split, or drop it "
            "(tag remove the bubble + STATUS:done) to unblock the parent."
        )
        return out

    def _render_chunk(self, addr: str) -> Response:
        # Universal handles relative navigation: ``dc<id>^N`` (ancestor), ``+N``/``-N``
        # (sibling step), ``-lo..hi`` (signed sibling span — the reading
        # window). Resolved against the draft tree; supersedes the legacy
        # ``-B+A`` reading-order window.
        rel = self.store.drafts.draft_relative_chunk_ids(addr)
        if rel is not None:
            if not rel:
                raise NotFound(
                    f"draft chunk {addr!r} resolves to nothing "
                    "(out of range, or no enclosing heading)"
                )
            window = [
                c
                for cid in rel
                if (c := self.store.drafts.get_draft_chunk(f"dc{cid}")) is not None
            ]
        else:
            m = _CHUNK_ADDR.match(addr)
            if m is None:
                raise BadInput(
                    f"unparseable chunk address {addr!r}",
                    next="id='dc<chunk_id>' (or dc<id>^ / +1 / -2..3 to navigate)",
                )
            # ``get_draft_chunk`` accepts ``dc<id>`` and legacy ``¶<base58>``.
            core = ("dc" + m.group("cid")) if m.group("cid") else m.group("h")
            chunk = self.store.drafts.get_draft_chunk(core)
            if chunk is None:
                raise NotFound(f"draft chunk {addr!r} not found")
            window = [chunk]
        # ``sha:`` is a short prefix of the chunk's content_sha — pass it
        # back as ``edit(base_sha=…)`` for an optimistic edit that won't
        # clobber a change that landed since this read. 12 hex chars (48
        # bits) is ample to detect a change to one chunk; the full digest
        # is needlessly long on every line. ``edit`` matches by prefix, so
        # a full 64-char sha still works.
        blocks = [
            f"{c.dc}  [{c.chunk_kind}]"
            f"{'  ⚠ RETIRED' if c.retired else ''}"
            f"  sha:{content_sha(c.text)[:12]}\n{c.text}"
            for c in window
        ]
        body = "\n\n".join(blocks)
        if any(c.retired for c in window):
            # A retired chunk stays readable by direct handle (gripe 49153)
            # but is no longer part of the draft — without this notice a
            # reader can't tell why search/reading-order/export skip text
            # they can plainly see (gr192827 finding 8).
            body += (
                "\n\n⚠ RETIRED chunk(s) above: no longer part of the draft — "
                "excluded from reading order, search (all modes), and export. "
                "A live replacement may exist; read the enclosing section."
            )
        window_text = "\n\n".join(c.text for c in window)
        body += _draft_lint.dangling_finding_hint(self.store, window_text)
        body += _draft_lint.dangling_chunk_hint(self.store, window_text)
        if len(window) == 1:
            body += self._fisheye_affordance()
        return Response(body=body)

    def _fisheye_affordance(self) -> str:
        """Advertise the neighborhood render on the plain chunk read — the
        *unprompted-discovery* channel for fisheye. A process
        that just reads a bare chunk learns, at the point of relevance, that
        it can get the node in context without having to already know the
        feature exists or go searching the skill index. Gated to
        single-chunk reads (``_render_chunk`` only calls this when
        ``len(window) == 1``): the footer text says "this node" (singular),
        wrong for a multi-chunk reading window, and a window read already
        asked for surrounding context, so advertising fisheye there is
        redundant."""
        return (
            "\n\n→ view='fisheye' renders this node with its neighbourhood "
            "(nearby chunks + section path); view='fisheye+1hop' also shows what "
            "it references. skill: precis-fisheye-help"
        )

    # ``_dangling_*_tokens``/``_newly_dangling`` proxy ``_draft_lint`` (the
    # ``*_hint`` formatters that used to sit alongside them moved there
    # outright) — kept here as the stable attribute `precis_web/routes/
    # drafts.py`'s inline-editor save-gate and `tests/test_draft_handler.py`
    # reach into directly.
    def _dangling_finding_tokens(self, text: str) -> list[str]:
        """The ``finding #slug`` markers in ``text`` that resolve to no live
        finding ref. See :func:`~precis.handlers._draft_lint.
        dangling_finding_tokens`."""
        return _draft_lint.dangling_finding_tokens(self.store, text)

    def _dangling_chunk_tokens(self, text: str) -> list[str]:
        """The ``[<handle>]`` references in ``text`` that resolve to
        nothing. See :func:`~precis.handlers._draft_lint.
        dangling_chunk_tokens`."""
        return _draft_lint.dangling_chunk_tokens(self.store, text)

    def _newly_dangling(
        self, new_text: str, old_text: str
    ) -> tuple[list[str], list[str]]:
        """``(newly-broken chunk-ref tokens, newly-broken finding slugs)``.
        See :func:`~precis.handlers._draft_lint.newly_dangling` — the
        shared core of the inline-editor validation gate
        (``docs/backlog/draft-inline-editor.md``)."""
        return _draft_lint.newly_dangling(self.store, new_text, old_text)

    def _render_toc(
        self, *, ref: Any = None, root_handle: str | None = None
    ) -> Response:
        """The heading skeleton — whole draft, or the subtree under a
        heading (`view='toc'` at any hierarchy level). Computed §-numbers,
        with each heading's gist/keywords when a worker has produced them."""
        if root_handle is not None:
            chunk = self.store.drafts.get_draft_chunk(root_handle)
            if chunk is None:
                raise NotFound(f"draft heading {root_handle} not found")
            entries = self.store.drafts.draft_toc(chunk.ref_id, root_handle=root_handle)
            header = f"# TOC under {chunk.dc}: {chunk.text}"
        else:
            entries = self.store.drafts.draft_toc(ref.id)
            header = f"# {ref.title} — table of contents"
        if not entries:
            return Response(body=f"{header}\n\n(no sub-headings yet)")
        # TOON table (TOON output — the house format for tabular tool output).
        # `level` (tree depth) conveys hierarchy since TOON is flat; the
        # stable `¶handle` is the address the agent navigates/edits by.
        # Display §-numbers are positional (computed at render/export, not
        # here — they'd rot on reorder and aren't a valid handle).
        rows = [
            {
                "handle": e.dc,
                "level": e.depth,
                "title": e.title,
                "gist": e.gist or (", ".join(e.keywords[:6]) if e.keywords else ""),
            }
            for e in entries
        ]
        table = toon.dump(rows, schema=["handle", "level", "title", "gist"])
        return Response(body=f"{header}\n\n{table}")

    def _render_wordcount(
        self, *, ref: Any = None, root_handle: str | None = None
    ) -> Response:
        """Per-section word counts vs targets (proposal writing).

        Counts visible prose words (paragraphs / asides — not headings,
        equations, figures, tables, code, or glossary terms; inline
        reference markers stripped) per heading subtree, and renders an
        over/under/ok verdict against each heading's
        ``meta.word_target``. The planner self-checks length here mid-
        write; the human sees the same numbers in the reader.

        Whole-draft (``view='wordcount'`` on the slug) or a single
        section subtree (on a ``dc<heading>`` handle)."""
        from precis.utils.wordcount import aggregate_word_counts

        root = None
        if root_handle is not None:
            root = self.store.drafts.get_draft_chunk(root_handle)
            if root is None:
                raise NotFound(f"draft heading {root_handle} not found")
            if root.chunk_kind != "heading":
                raise BadInput(
                    f"wordcount scope must be a heading; {root.dc} is a "
                    f"{root.chunk_kind}",
                    next="get(kind='draft', id='<slug>', view='wordcount')",
                )
            ref_id = root.ref_id
            header = f"# words under {root.dc}: {root.text}"
        else:
            ref_id = ref.id
            header = f"# {ref.title} — word counts"

        chunks = self.store.drafts.reading_order(ref_id)
        # Scope to the heading's DFS subtree when a root is given: the
        # contiguous run after the root whose depth exceeds the root's,
        # plus the root itself (standard DFS subtree property).
        if root is not None:
            scoped: list[Any] = []
            collecting = False
            root_depth = 0
            for c in chunks:
                if c.chunk_id == root.chunk_id:
                    collecting = True
                    root_depth = c.depth
                    scoped.append(c)
                    continue
                if collecting:
                    if c.depth <= root_depth:
                        break
                    scoped.append(c)
            chunks = scoped

        report = aggregate_word_counts(chunks)
        if not report.sections:
            body = f"{header}\n\ntotal: {report.total} words\n\n(no sections yet)"
            return Response(body=body)

        rows = []
        for sc in report.sections:
            if sc.target is None:
                target_s = "—"
            else:
                target_s = f"{sc.target[0]}–{sc.target[1]}"
            rows.append(
                {
                    "handle": handle_registry.format_handle(
                        "draft", sc.chunk_id, chunk=True
                    ),
                    "title": sc.title,
                    "words": sc.words,
                    "target": target_s,
                    "verdict": sc.verdict,
                }
            )
        table = toon.dump(
            rows, schema=["handle", "title", "words", "target", "verdict"]
        )
        flagged = [s for s in report.sections if s.verdict in ("under", "over")]
        trailer = f"\n\ntotal: {report.total} words"
        if flagged:
            names = ", ".join(
                f"{s.title or '(untitled)'} ({s.verdict})" for s in flagged[:6]
            )
            trailer += f"\n⚠ {len(flagged)} section(s) off target: {names}"
        return Response(body=f"{header}\n\n{table}{trailer}")
