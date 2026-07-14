"""Handler ABC and KindSpec.

Every kind subclasses `Handler` and exposes a `KindSpec` ClassVar
declaring which verbs it supports, what views/modes it understands, and
any runtime-required env vars. The dispatcher uses KindSpec to validate
calls and to hide kinds whose env requirements aren't met.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from precis.errors import Unsupported
from precis.response import Response

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.utils.search_merge import SearchHit
    from precis.utils.toc import ChunksForToc

Verb = Literal["get", "search", "put", "edit", "delete", "tag", "link"]


@dataclass(frozen=True, slots=True)
class KindSpec:
    """Declarative metadata for a kind."""

    kind: str
    title: str
    description: str

    supports_get: bool = False
    supports_search: bool = False
    supports_put: bool = False
    #: Region-edit verb (``edit(mode='find-replace'|'append'|'insert'|...)``).
    #: Distinct from ``supports_put`` because file kinds want a clean
    #: split between "create new ref" (put) and "rewrite an existing
    #: one's content" (edit). Numeric-ref kinds (memory, todo, …) keep
    #: ``supports_edit=False`` — their text mutation is just put-with-id.
    supports_edit: bool = False
    #: Soft-delete or selector-delete (``delete(kind, id)``). True for
    #: numeric-ref kinds (soft-delete the ref) and for file kinds where
    #: a selector targets a block / symbol.
    supports_delete: bool = False
    #: Tag-ops verb (``tag(kind, id, add=[...], remove=[...])``).
    supports_tag: bool = False
    #: Link-ops verb (``link(kind, id, target='...', mode='add'|'remove',
    #: rel='...')``).
    supports_link: bool = False

    # Cross-kind search opt-in. When True, the handler's
    # ``search_hits`` method returns ``list[SearchHit]`` and
    # participates in fan-out merges (``kind='paper,memory'``,
    # ``kind='*'``). Independent of ``supports_search``: a handler
    # may serve a custom-shaped single-kind ``search()`` (skill,
    # python) without being eligible for the universal merge.
    supports_search_hits: bool = False

    is_numeric: bool = False  # public id is int (else str slug)
    id_required: bool = True  # False if get may omit id

    #: Corpus role for the document family (paper / patent / cfp / …).
    #: ``"evidence"`` — a citable source that participates in literature
    #: search (paper, patent). ``"spec"`` — a read-only requirements
    #: document (a call-for-proposal, a standard) that must **never** be
    #: cited as evidence and lives in its own reader namespace. The
    #: citation handler already resolves sources only against
    #: ``kind='paper'``, so a ``"spec"`` doc is non-citable by
    #: construction; this flag formalises the distinction for the planner
    #: prompt, the reader chrome, and any future cross-kind search.
    #: ``"none"`` for kinds outside the document family (the default for
    #: notes, jobs, todos — they are not ingested source documents).
    corpus_role: Literal["evidence", "spec", "none"] = "none"

    #: Organizational role (ADR 0045). ``"artifact"`` — an authored
    #: thing (draft, structure, cad, todo, folder): placeable in a
    #: ``kind='folder'`` container via the reserved virtual ``parent``
    #: relation, first-class in the Drive / one-list web surface.
    #: ``"corpus"`` — collected / ingested sources (paper, patent,
    #: cfp, pres): never placed — they have their own discovery layer
    #: (search / clusters / TOC / tags). ``"stream"`` — machine-emitted
    #: records arriving at machine rate (memory, alert, agentlog, job,
    #: news): never placed; stream content reaches a folder only by
    #: explicit promotion into an authored note. ``"system"`` —
    #: infrastructure kinds (skill, tag, cron, oracle). The default is
    #: ``"stream"``: the safe failure mode — a new kind stays out of
    #: folders until deliberately promoted. NB ``role`` does *not*
    #: alter cross-kind search fan-out, which stays keyed on
    #: ``supports_search_hits``.
    role: Literal["artifact", "corpus", "stream", "system"] = "stream"

    #: User-authored note-like kinds opt-in to ``PRECIS_DEFAULT_TAGS``
    #: merging on ``put`` (and tag-suggestion hints on ``tag``).
    #: Default ``False`` so ingested kinds (paper, patent), fetched
    #: caches (web, wolfram, youtube), and generators (oracle, random,
    #: skill) don't accidentally accumulate session-context tags they
    #: shouldn't carry. The flip-list is curated in
    #: ``docs/design/mcp-cold-start-token-budget.md`` Phase 5 step 2:
    #: memory, gripe, conversation, anki, todo (numeric
    #: refs) and markdown, plaintext, tex (file-rooted authored
    #: content).
    note_like: bool = False

    #: Compute-lane opt-in (ADR 0044). When True, a ``kind='job'`` may
    #: parent on a ref of this kind — the artifact owns its derived,
    #: cache-fillable build job (relax / route / compile / a catpath
    #: pathway run). Lets a *plugin* kind join the compute lane without a
    #: core edit to ``JOB_PARENT_KINDS`` (the built-in owners —
    #: structure/cad/draft — predate this flag and stay in that set).
    can_own_jobs: bool = False

    views: tuple[str, ...] = ()  # supported view= values
    modes: tuple[str, ...] = ()  # supported mode= values for put

    requires_env: tuple[str, ...] = ()  # all must be set or kind is hidden
    #: Secrets that must resolve (env → DB vault → file, ADR 0055) or the kind
    #: is hidden. Use this instead of ``requires_env`` for credentials that live
    #: in the secrets vault, so the kind stays available after the env var is
    #: pulled and the value lives only in the DB.
    requires_secret: tuple[str, ...] = ()

    def is_available(self) -> bool:
        """True iff every required env var is set and every required secret
        resolves (env / vault / file)."""
        if not all(os.environ.get(v) for v in self.requires_env):
            return False
        if self.requires_secret:
            from precis import secrets as _secrets

            return all(_secrets.is_available(n) for n in self.requires_secret)
        return True

    def supports(self, verb: Verb) -> bool:
        return getattr(self, f"supports_{verb}")


class Handler:
    """Base for all handlers.

    Subclasses override the verbs they support and declare a `KindSpec`
    ClassVar. The default implementations raise `Unsupported` so a
    handler that lies about its KindSpec is detectable.

    Construction: :func:`precis.dispatch._try` builds the instance,
    then calls :meth:`_register_with` to publish it to the
    :class:`~precis.dispatch.Hub`. See
    ``docs/user-facing/seven-verb-surface-migration.md`` D7 for the contract.
    """

    spec: ClassVar[KindSpec]

    #: Populated by :meth:`_register_with` so handlers that need
    #: hub introspection (e.g. SkillHandler rendering
    #: ``precis-help``, or any handler that wants the embedder /
    #: hint bus) can read it without a separate late-bind hook.
    #: Typed ``Any`` to avoid a hard import of
    #: ``precis.dispatch.Hub`` in every handler module.
    hub: Any = None

    def _register_with(self, hub: Hub) -> None:
        """Register every verb declared supported in ``self.spec``.

        Invoked by :func:`precis.dispatch._try` immediately after
        successful construction. Reads ``self.spec`` and populates
        the flat dispatch table with bound methods, and stashes
        ``hub`` on ``self.hub`` so the handler can reach shared
        infrastructure (``embed_one``, ``emit_hint``, the live
        registry of sibling kinds, …) at request time.

        ``mode`` on every ability is ``None`` under the v1 shape —
        ``put`` was polymorphic over a mode-string. The seven-verb
        cutover splits ``put`` into ``put / edit / delete / tag /
        link``; mode strings are still ``None`` at this layer because
        each new verb has its own dedicated method on the handler.
        Per-verb mode discrimination (e.g. ``edit(mode='replace')``)
        happens inside the handler, not at the dispatch table layer.
        """
        self.hub = hub
        spec = self.spec
        hub.register_handler(spec.kind, self)
        for verb in _ALL_VERBS:
            if spec.supports(verb):  # type: ignore[arg-type]
                hub.register_ability(spec.kind, verb, None, getattr(self, verb))
        hub.register_overview(spec.kind, spec.description)

    def get(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support get")

    def search(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support search")

    def search_hits(self, **kw: Any) -> list[SearchHit]:
        """Structured search for cross-kind merge.

        Returns a list of ``SearchHit`` already sorted best-first.
        Used by the runtime when ``kind`` is a comma-list / ``'*'``
        / ``None``-with-cross-kind-default to fan out across every
        kind whose ``KindSpec.supports_search_hits`` is True.

        Default raises ``Unsupported``; concrete handlers override.
        Single-kind ``search()`` text rendering stays the canonical
        agent surface — this method is the structured input to the
        merge primitive, not a replacement.
        """
        raise Unsupported(f"{self.spec.kind} does not support cross-kind search")

    def accepted_views(self, *, id: Any = None) -> list[str]:
        """Per-kind list of accepted ``view=`` values.

        Returned in display order — first entry is the conventional
        default. Empty list signals "this kind has no view enum"
        (the dispatcher then treats any non-default view as a
        regular ``BadInput`` without listing alternatives).

        ``id=`` is optional context — most kinds return the same
        view list regardless of which ref is being viewed, but kinds
        that vary their accepted views by ref shape (e.g. paper has
        ``cite`` only when DOI is present) can branch on it.

        Used by the runtime to wrap CLI / handler ``view=``
        validation in a per-kind ``BadInput`` envelope: when the
        caller passes an unknown view, the error lists what's
        actually accepted for *this* kind, not a generic placeholder.
        Phase F 2026-05-31.
        """
        return []

    def chunks_for_toc(self, ref: Any) -> ChunksForToc | None:
        """Return ``(chunks, embeddings, h2_boundaries)`` for ``ref``.

        Opt-in contract for the generic TOC renderer
        (:mod:`precis.utils.toc`). Kinds that implement this method
        get a smart-TOC view for free; kinds that don't are
        ignored. Returning ``None`` is also an opt-out signal
        (e.g. cache-only kinds with no chunk structure).

        Implementations should:

        * Return chunk bodies in reading order (full text per chunk
          — the renderer feeds them to RAKE for per-segment keyword
          extraction).
        * Provide per-chunk embeddings *only* if the kind has them.
          ``None`` is acceptable; the renderer falls back to H2
          structure or a flat listing.
        * Provide ``h2_boundaries`` as ``[(start, end, heading), ...]``
          when the source has explicit section structure (markdown
          H2s, paper section headings). Empty / None when no
          structure is present.

        Stable / cacheable: the TOC renderer caches outputs keyed on
        ``(ref.id, chunker_version, embedder_name, SEGMENTATION_VERSION)``,
        so implementations should return the same shape across calls
        for a given ref unless the underlying chunks change.
        """
        raise Unsupported(f"{self.spec.kind} does not support TOC")

    def put(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support put")

    def edit(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support edit")

    def delete(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support delete")

    def tag(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support tag")

    def link(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support link")


# Verb iteration order. ``_register_with`` walks this list to populate
# the dispatch table; the runtime walks it for "what does this kind
# support?" answers in error messages. Reads top-down match the agent-
# facing mental model: read verbs first, then write verbs.
_ALL_VERBS: tuple[Verb, ...] = (
    "get",
    "search",
    "put",
    "edit",
    "delete",
    "tag",
    "link",
)
