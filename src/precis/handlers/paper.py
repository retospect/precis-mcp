"""PaperHandler — read scientific papers from the v2 store.

Bodies are read-only via ``get`` / ``search``; ingest happens
out-of-band via :func:`precis.ingest.add.precis_add` (or the
top-level ``precis add`` / ``precis watch`` CLI), so ``put`` is not
exposed. ``edit`` is supported but scoped to *bibliographic metadata*
(authors / year / title / abstract / doi / arxiv) — it never touches
block text.

Slug parsing supports the canonical slug-with-chunk syntax used across
v2:

    wang2020state              — overview
    wang2020state~38           — block at pos=38
    wang2020state~38..42       — block range pos∈[38,42]
    wang2020state/cite/bib     — view shortcut path
    wang2020state/abstract     — view shortcut path
    wang2020state/toc          — view shortcut path
    wang2020state~38..42/toc   — TOC scoped to a range (drill-down, phase 3.5)
"""

from __future__ import annotations

import difflib
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import render_agent_table
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.handlers._paper_format import (
    _clean_inline_text,
    _format_authors,
    _format_citation,
    _strip_jats,
)
from precis.handlers._paper_text import (
    _chunk_keywords_or_caption,
    _is_image_only_block,
    _looks_like_caption,
    _render_block_body,
    _scrub_block_text,
)
from precis.handlers._slug_ref_shared import (
    reject_chunk_or_path_view,
    resolve_live_slug_ref,
)
from precis.ingest.cards import rewrite_cards
from precis.ingest.text_chunker import CHUNKER_VERSION as _PAPER_CHUNKER_VERSION
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Store, Tag
from precis.utils import handle_registry
from precis.utils.authors import to_name_dicts
from precis.utils.embed_query import embed_query, query_vec_for
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt
from precis.utils.toc import ChunksForToc
from precis.utils.toc_db import render_from_store


def _pa(ref: Ref) -> str:
    """ADR 0036 paper record handle (e.g. ``pa123``)."""
    return handle_registry.format_handle("paper", ref.id)


# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------

_SUPPORTED_VIEWS = (
    "bibtex",
    "ris",
    "endnote",
    "abstract",
    "toc",
    "health",
    "bibliography",
    "log",
    "abbrevs",
)


# Tunable knobs for the nearest-match suggester. The cutoff (0.6 of
# difflib's SequenceMatcher ratio) is deliberately conservative — it
# accepts ``wang2020stat`` → ``wang2020state`` (one missing char,
# ratio≈0.96) but rejects ``foo`` → ``wang2020state`` (ratio≈0.05),
# avoiding spurious "did you mean?" prompts when the agent is querying
# an obviously-different slug.
_SUGGEST_TOP_N = 3
_SUGGEST_CUTOFF = 0.6
# Hard cap on how many slugs we pull into memory for the close-match
# scan. At 30 chars per slug × 5K papers, that's ~150 KB of strings —
# well within the budget for an error path. If the corpus grows past
# this, the suggester silently truncates to the most-recent N papers
# (``list_refs`` orders by ``updated_at DESC``); that's a reasonable
# locality bias and keeps the worst-case cost bounded.
_SUGGEST_CORPUS_CAP = 5000


def _coerce_search_year(value: int | str | None, param: str) -> int | None:
    """Validate an ``after=`` / ``before=`` publish-year bound.

    Returns ``None`` when unset, else an int in 1500..2100. A
    non-integer or out-of-range value raises :class:`BadInput` at the
    agent boundary (the store-side guard in ``_blocks_ops`` re-checks).
    Year-grained because the corpus stores ``refs.year``, not full
    dates; a ``'2019-03'`` string keeps only the leading year via int().
    """
    if value is None:
        return None
    raw = str(value).strip().split("-", 1)[0]  # tolerate 'YYYY-MM' → 'YYYY'
    try:
        iv = int(raw)
    except ValueError:
        raise BadInput(
            f"{param}= must be a 4-digit year, got {value!r}",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        ) from None
    if not (1500 <= iv <= 2100):
        raise BadInput(
            f"{param}={iv} out of plausible range (1500..2100)",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        )
    return iv


def _normalise_exclude_slug(raw: str, *, store: Any) -> str | None:
    """Coerce one ``exclude=`` entry to a bare paper slug.

    The exclude list is coarse-only (ref-level): ``wang2020state`` and
    ``wang2020state~38`` both drop every block of the paper. We
    therefore strip any chunk selector or view path the caller may
    have copy-pasted from a hit handle, then DOI-resolve so
    ``exclude=['10.1111/jnc.13915']`` is equivalent to passing the
    matching slug.

    Returns ``None`` when the input is empty / whitespace / unresolvable
    DOI — the caller silently drops nones so a stale slug in the
    exclude list never poisons the whole search call.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # ADR 0036: an exclude entry copied from a hit is now a universal handle
    # (``pa5`` / ``pc40``). Resolve it to the owning paper slug (ref-level,
    # coarse) so ``exclude=['pc40']`` drops that paper's blocks.
    if handle_registry.parse(cleaned) is not None:
        resolved_handle = store.resolve_handle(cleaned)
        return resolved_handle.public_id if resolved_handle is not None else None
    # Coarse-only: trim the first ``~`` (chunk selector) or ``/``
    # (view path). DOI-form ids carry ``/`` in the suffix, but
    # _maybe_resolve_doi runs first and replaces the whole DOI with
    # a bare slug, so by the time we reach the split there's no DOI
    # ``/`` to confuse with a view path.
    try:
        resolved = _maybe_resolve_doi(store, cleaned)
    except Exception:
        # Unknown DOI raises NotFound from _maybe_resolve_doi; treat
        # the same as a stale slug in the exclude list — silent drop.
        return None
    for sep in ("~", "/"):
        if sep in resolved:
            resolved = resolved.split(sep, 1)[0]
    resolved = resolved.strip()
    return resolved or None


def _dedup_card_hits(
    hits: list[tuple[Any, Any, float]],
) -> list[tuple[Any, Any, float]]:
    """Drop a paper's synthetic ``card_*`` hit when a body block of the
    same paper is also on the page.

    Paper search opts ``card_combined`` into the legs so a title / meta
    query can surface a paper that has no matching body block. But when
    a real (quotable) body block of that paper *also* matched, the card
    is redundant noise — the body block is the better hit. So a card
    hit survives only for refs that have no body hit in this page; for
    refs with both, the card is dropped and ordering is otherwise
    preserved.
    """
    body_ref_ids = {
        ref.id
        for block, ref, _score in hits
        if not block.chunk_kind.startswith("card_")
    }
    return [
        (block, ref, score)
        for block, ref, score in hits
        if not (block.chunk_kind.startswith("card_") and ref.id in body_ref_ids)
    ]


def _suggest_paper_slugs(slug: str, *, store: Any) -> list[str]:
    """Return up to ``_SUGGEST_TOP_N`` paper slugs that look like ``slug``.

    Uses :func:`difflib.get_close_matches` with a ratio cutoff that
    rejects far-off matches — see ``_SUGGEST_CUTOFF`` for the rationale.
    Returns an empty list when:

    - the corpus is empty (no papers ingested yet),
    - no slug clears the cutoff (typical when the user types a topic
      string into the slug slot, e.g. ``id='nitrate reduction'``),
    - the typed slug exists exactly (caller should have resolved
      already; defensive no-op).

    The helper does **not** raise; callers always pass the result
    straight into the ``options=`` field of a ``NotFound``. Empty list
    → no ``options:`` line in the rendered envelope, which is exactly
    what we want when there's nothing useful to suggest.

    Why a free function (not a method): keeping this off the handler
    makes it independently testable without spinning up a Hub fixture
    and means the same logic could be reused by patent/oracle/conv
    handlers if they grow nearest-match support too.
    """
    if not slug:
        return []
    refs = store.list_refs(kind="paper", limit=_SUGGEST_CORPUS_CAP)
    candidates = [r.slug for r in refs if r.slug]
    if not candidates:
        return []
    return difflib.get_close_matches(
        slug,
        candidates,
        n=_SUGGEST_TOP_N,
        cutoff=_SUGGEST_CUTOFF,
    )


# Identifier prefixes the gated ``acquire`` tool accepts in its
# ``identifier=`` slot. Bare DOIs (``10....``) and arXiv ids
# (``NNNN.NNNNN``) are inferred without a prefix.
_ACQUIRE_ID_PREFIXES = ("doi", "arxiv", "s2", "pubmed")
_ARXIV_BARE_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def _parse_acquire_identifier(raw: str) -> tuple[str, str] | None:
    """Parse an ``acquire`` identifier into an ``(id_kind, id_value)`` pair.

    Accepts prefixed forms (``doi:10.1/x``, ``arxiv:2401.00001``,
    ``s2:<id>``, ``pubmed:<id>``) and infers bare DOIs (``10.``-prefixed)
    and bare arXiv ids. Returns ``None`` when nothing recognisable is
    found — the caller turns that into a ``BadInput`` with a usage hint.
    DOIs are lower-cased to match the chase worker's normalisation so
    identifier-collapse stays case-insensitive.
    """
    s = raw.strip()
    if not s:
        return None
    low = s.lower()
    for pfx in _ACQUIRE_ID_PREFIXES:
        if low.startswith(pfx + ":"):
            val = s[len(pfx) + 1 :].strip()
            if not val:
                return None
            return (pfx, val.lower() if pfx == "doi" else val)
    if low.startswith("10."):
        return ("doi", low)
    if _ARXIV_BARE_RE.match(s):
        return ("arxiv", s)
    return None


def _lookup_acquire_metadata(id_kind: str, id_value: str) -> dict[str, Any] | None:
    """Best-effort Semantic Scholar enrichment for a fresh stub.

    Returns the normalised S2 metadata dict (``title`` / ``year`` /
    ``doi`` / ``arxiv_id`` / ``s2_id`` / ...) or ``None`` on any failure
    (offline, rate-limited, not found, unsupported id kind). **Never
    raises** — enrichment is a nicety; the stub mints with or without
    it. Patched out in tests to keep them offline.
    """
    try:
        from precis.ingest.semantic_scholar import get_paper_by_id

        if id_kind == "doi":
            return get_paper_by_id(f"doi:{id_value}")
        if id_kind == "arxiv":
            return get_paper_by_id(f"arxiv:{id_value}")
        if id_kind == "s2":
            return get_paper_by_id(id_value)
    except Exception:
        return None
    return None


class PaperHandler(Handler):
    """Slug-addressed paper handler (read-only bodies; metadata-editable).

    Stored data: each paper is a ``refs`` row with kind='paper' and one
    block per chunk in ``blocks`` (text + optional embedding + density).
    Bibliographic metadata (doi, authors, year, journal, ...) lives in
    ``refs.meta``.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="paper",
        title="Paper",
        description=(
            "Scientific paper. Addressable by slug (e.g. 'wang2020dopamine') "
            "OR by bare DOI (e.g. '10.1038/nature10352') — `get` and "
            "`search` resolve DOIs transparently. One ref per paper, "
            "blocks per chunk. Ingested from .acatome bundles (paper "
            "bodies are import-only). Use tag / link to classify and "
            "cross-cite."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Paper *bodies* are import-only (arrive via .acatome bundle
        # ingest, never authored from the agent surface). ``put`` is
        # exposed for **stub minting only** — ``put(kind='paper',
        # doi=… / arxiv=… / title=…)`` requests a paper into the
        # "papers we need" backlog (the fetch_oa worker chases it); it
        # never writes a body. ``edit`` is scoped to *bibliographic
        # metadata* only (authors / year / title / abstract / doi /
        # arxiv) — the repair affordance the web metadata editor drives;
        # it never touches block bodies.
        supports_put=True,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("paper: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # -- acquire: the gated dream stub-mint tool -----------------------------

    def put(  # type: ignore[override]
        self,
        *,
        identifier: str | None = None,
        doi: str | None = None,
        arxiv: str | None = None,
        title: str | None = None,
        year: int | None = None,
        reason: str | None = None,
        context_ref_id: int | str | None = None,
        verify: bool = True,
        **_kw: Any,
    ) -> Response:
        """Mint a paper **stub** — the agent-facing "I want this paper".

        Paper *bodies* stay import-only (``.acatome`` ingest); ``put``
        only ever requests a paper into the "papers we need" backlog,
        where the ``fetch_oa`` worker chases an OA PDF. Shapes:

            put(kind='paper', doi='10.1038/nature10352')
            put(kind='paper', arxiv='2401.00001', title='…')
            put(kind='paper', title='…')   # title-only backlog stub

        This is a thin adapter over :meth:`acquire` — it folds the
        ``doi=`` / ``arxiv=`` conveniences into the canonical
        ``identifier=`` form and reuses the same S2-enrich →
        ``upsert_stub_paper`` → tag/link path (idempotent: a hit on an
        already-held or already-wanted paper is a no-op).
        """
        # ``put`` mints stubs; it never writes a body. A caller passing
        # ``text=`` is trying to rewrite a paper body — reject loudly
        # rather than silently drop the text into ``_kw``. Bodies stay
        # import-only (``.acatome`` ingest).
        if _kw.get("text") is not None:
            raise Unsupported(
                "paper does not support put with text= — bodies are "
                "import-only (.acatome ingest); put(kind='paper') only "
                "mints stubs",
                next="put(kind='paper', doi='10.1038/nature10352')",
            )
        ident = identifier.strip() if identifier and identifier.strip() else None
        if ident is None:
            if doi and doi.strip():
                ident = f"doi:{doi.strip()}"
            elif arxiv and arxiv.strip():
                ident = f"arxiv:{arxiv.strip()}"
        if ident is None and not (title and title.strip()):
            raise BadInput(
                "put(kind='paper') mints a stub - pass doi=, arxiv=, "
                "identifier= (doi:/arxiv:/s2:), or title=",
                next="put(kind='paper', doi='10.1038/nature10352')",
            )
        return self.acquire(
            identifier=ident,
            title=title,
            year=year,
            reason=reason,
            context_ref_id=context_ref_id,
            verify=verify,
        )

    def acquire(
        self,
        *,
        identifier: str | None = None,
        title: str | None = None,
        year: int | None = None,
        reason: str | None = None,
        context_ref_id: int | str | None = None,
        verify: bool = True,
        **_kw: Any,
    ) -> Response:
        """Queue a missing paper for fetch — the shared stub-mint impl.

        The agent-facing spelling is ``put(kind='paper', …)`` (see
        :meth:`put`); this method does the work. A dream (or anyone)
        notices the corpus keeps citing a paper it doesn't hold and mints
        a **stub** so the existing fetch pipeline takes over
        (docs/design/dreaming.md, §Acquire). It does the minimum and
        gets out of the way: it **never ingests inline** — no download,
        no Marker, in the dream turn.

        1. Resolve the ``identifier`` (``doi:`` / ``arxiv:`` / ``s2:`` or
           a bare DOI / arXiv id) and best-effort enrich via S2.
        2. Idempotently upsert a stub ``paper`` ref (identifier-collapse:
           a hit on an already-held or already-wanted paper short-circuits
           to a no-op), tagged ``DREAM:acquire`` with ``meta.set_by='dream'``.
        3. Link it from ``context_ref_id`` (provenance) when supplied.

        Downstream is automatic and needs no wiring here: the
        ``fetch_oa`` worker auto-claims the stub on a later pass and
        grabs an OA PDF if one exists; otherwise the stub waits on the
        ``precis stubs`` required-papers backlog. Minting is additive and
        reversible (soft-delete), so a runaway dream can at worst enqueue
        stubs — never blow a budget on downloads.

        Reachable from the MCP surface via ``put(kind='paper', …)`` (the
        agent-facing spelling — see :meth:`put`); this method is the
        shared implementation. The legacy in-process dream loop's gated
        ``acquire`` tool (``PRECIS_DREAM_ACQUIRE``) was retired when the
        dreamers were consolidated onto the ``claude -p`` + MCP
        ``dream_agent``.

        ``verify`` (default ``True``): when set, an unrecognised
        identifier (Semantic Scholar returns no metadata) is rejected
        with :class:`BadInput` so a hallucinated DOI / arXiv ID never
        lands on the "Papers we need" backlog. Pass ``verify=False``
        when minting a known-real preprint that S2 hasn't indexed yet,
        or when the resolver is unreachable. Resolver outages mint with
        ``meta.acquire_unverified=True`` so the operator can re-check
        on a later pass.
        """
        has_identifier = bool(identifier and identifier.strip())
        has_title = bool(title and title.strip())
        if not has_identifier and not has_title:
            raise BadInput(
                "acquire requires identifier= (doi/arxiv/s2) or title=",
                next="acquire(identifier='doi:10.1/x', reason='cited 5x in cluster')",
            )

        id_pair: tuple[str, str] | None = None
        if has_identifier:
            assert identifier is not None
            id_pair = _parse_acquire_identifier(identifier)
            if id_pair is None:
                raise BadInput(
                    f"acquire: unrecognised identifier {identifier!r}",
                    next=(
                        "use 'doi:10...', 'arxiv:2401.00001', or 's2:<id>' "
                        "(or pass title= for a backlog-only stub)"
                    ),
                )

        # Validate the provenance ref up-front so a bad id fails before
        # any write (kind-agnostic — context may be a paper or a memory).
        ctx_id: int | None = None
        if context_ref_id is not None:
            try:
                ctx_id = int(context_ref_id)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"acquire: context_ref_id must be an int, got {context_ref_id!r}",
                    next="pass the numeric ref id where the paper came up",
                ) from exc
            if ctx_id not in self.store.fetch_refs_by_ids(
                [ctx_id], include_deleted=False
            ):
                raise BadInput(
                    f"acquire: context_ref_id={ctx_id} is not a live ref",
                    next="omit context_ref_id or pass a live ref id",
                )

        # Best-effort S2 enrichment → a meaningful stub. Failure is fine.
        # ``year`` arrives as a caller hint (e.g. from put(kind='paper',
        # year=…)); S2 overrides it when it has one, else the hint stands.
        stub_title = title.strip() if has_title else None
        identifiers: list[tuple[str, str]] = [id_pair] if id_pair else []
        unverified = False
        if id_pair is not None:
            meta = _lookup_acquire_metadata(*id_pair)
            if meta:
                stub_title = stub_title or (meta.get("title") or None)
                raw_year = meta.get("year")
                year = int(raw_year) if isinstance(raw_year, int) else year
                for kind_key, meta_key in (
                    ("doi", "doi"),
                    ("arxiv", "arxiv_id"),
                    ("s2", "s2_id"),
                ):
                    val = meta.get(meta_key)
                    if val:
                        pair = (
                            kind_key,
                            str(val).lower() if kind_key == "doi" else str(val),
                        )
                        if pair not in identifiers:
                            identifiers.append(pair)
            elif verify and not has_title:
                # Strict path: caller gave us only an identifier and the
                # resolver returned nothing. We cannot distinguish
                # "DOI doesn't exist" from "S2 is down right now" — the
                # safe default is to reject hallucinated identifiers, and
                # let the caller pass ``verify=False`` for known-real
                # niche / brand-new papers, or add a ``title=`` hint
                # that converts this from "validate-or-reject" into
                # "validate-best-effort" (the title path means the
                # operator can still recognise the stub by hand).
                kind_key, val = id_pair
                raise BadInput(
                    f"acquire: identifier {identifier!r} did not resolve "
                    "via Semantic Scholar — likely a hallucinated or "
                    "mistyped ID",
                    next=(
                        f"verify {kind_key}:{val} on doi.org / arxiv.org, "
                        "OR add title='<best-known title>' to mint as a "
                        "title-only stub for manual ingest, OR pass "
                        "verify=False if you know the paper is real"
                    ),
                )
            elif verify and has_title:
                # We had a title hint, so the operator can still find
                # this in the "Papers we need" tab even though we
                # couldn't auto-confirm the identifier. Mark unverified.
                unverified = True

        with self.store.tx() as conn:
            ref_id, created = self.store.upsert_stub_paper(
                identifiers=identifiers,
                title=stub_title,
                year=year,
                set_by="dream",
                conn=conn,
            )
            # Tag the provenance only on a fresh stub — never slap
            # DREAM:acquire onto a paper the corpus already holds in full.
            if created:
                self.store.add_tag(
                    ref_id,
                    Tag.closed("DREAM", "acquire"),
                    set_by="agent",
                    conn=conn,
                )
                if unverified:
                    # Open tag so the operator can filter the
                    # "Papers we need" tab to un-validated stubs;
                    # the worker fetch cascade still tries them.
                    self.store.add_tag(
                        ref_id,
                        Tag.open("acquire:unverified"),
                        set_by="agent",
                        conn=conn,
                    )
            if ctx_id is not None:
                self.store.add_link(
                    src_ref_id=ctx_id,
                    dst_ref_id=ref_id,
                    relation="related-to",
                    set_by="agent",
                    meta={"acquire_reason": reason} if reason else None,
                    conn=conn,
                )

        state = "minted stub" if created else "already tracked"
        parts = [f"acquire: {state} paper id={ref_id}"]
        if identifiers:
            parts.append("(" + ", ".join(f"{k}:{v}" for k, v in identifiers) + ")")
        if ctx_id is not None:
            parts.append(f"← linked from ref {ctx_id}")
        body = " ".join(parts)

        # On a collapse hit (the identifier was already known), point the
        # caller straight at the existing paper — so re-requesting a paper
        # we already hold/want returns *the paper*, not just a bare
        # "already tracked id=N". Held papers (with a PDF) say so; stubs
        # report that the fetch is still pending.
        if not created:
            existing = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            if existing is not None:
                slug = existing.slug or str(ref_id)
                held = getattr(existing, "pdf_sha256", None) is not None
                title = (existing.title or "").split("\n", 1)[0].strip()
                where = "held" if held else "stub (awaiting fetch)"
                line = f"\n→ {where}: {_pa(existing)}"
                if title and title.lower() != "paper":
                    line += f" — {title[:120]}"
                line += f"\n  get(id='{_pa(existing)}') to read it"
                body += line

        return Response(body=body)

    # -- get -----------------------------------------------------------------

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Round-2 picky 2026-05-30: ``get(kind='paper', q='...')`` was
        # silently dropping ``q=`` and rendering the paper list,
        # leaving callers confused why their search returned no
        # chunks. Delegate to ``search`` when ``q=`` is present and
        # no concrete ``id=`` was given — same UX shift as the skill
        # handler's empty-q path, in the opposite direction.
        if id is None and q is not None and q.strip():
            return self.search(q=q)
        if id is None:
            return self._render_list_papers()
        raw_id = _maybe_resolve_doi(self.store, str(id))
        slug, chunk_spec, path_view = _parse_paper_id(raw_id)

        ref = resolve_live_slug_ref(
            self.store,
            kind="paper",
            id=slug,
            next_hint="search(kind='paper', q='your query') to find existing",
            options=_suggest_paper_slugs(slug, store=self.store),
        )

        # Path view (`slug/cite/bib`) takes precedence over kwarg `view`,
        # because the agent is being explicit in the id. Whatever wins,
        # normalise it through the same alias map so view='cite/bib' and
        # view='bibtex' resolve identically — the MCP critic flagged the
        # asymmetry where the path form accepted 'cite/bib' but the kwarg
        # form rejected it.
        effective_view = _normalise_view(path_view or view)
        # Phase F 2026-05-31: validate against per-kind enum so the
        # agent gets the accepted list back in one round-trip on a
        # bogus or missing-arg view. ``None`` (no view requested)
        # always passes through to the overview / chunk-resolver
        # paths below.
        if effective_view is not None:
            accepted = self.accepted_views(id=ref)
            if effective_view not in accepted:
                # Reserved views: ``fig/<N>`` is advertised in
                # ``precis-paper-help`` as a future-reserved
                # affordance. Surface a dedicated "reserved" error so
                # a caller who has read the help skill knows the
                # shape is right but the build is early — distinct
                # from a typo against the canonical enum.
                if effective_view.startswith("fig/"):
                    from precis.errors import Unsupported

                    raise Unsupported(
                        f"paper view {effective_view!r} is a reserved "
                        f"affordance — the help advertises fig/<N> but "
                        f"the build does not yet implement it",
                        next=(
                            "get(kind='skill', id='precis-paper-help') for "
                            "the current view enum"
                        ),
                    )
                from precis.errors import Unsupported

                raise Unsupported(
                    f"unknown view {effective_view!r} for kind='paper'",
                    options=accepted,
                    next=(
                        f"view= for kind='paper' accepts: {accepted}; "
                        f"omit view= for the paper overview"
                    ),
                )

        # Combined form: ``slug~A..B/toc`` → range-scoped TOC drill-down.
        # Only ``view='toc'`` is valid with a chunk_spec; other views
        # don't have a sensible "this range only" meaning yet.
        if chunk_spec is not None and effective_view is not None:
            if effective_view == "toc":
                return self._render_toc(ref, scope=chunk_spec)
            # Build the full id string first, then repr() it whole — the
            # MCP critic flagged ``id={slug!r}~{lo}..{hi}/toc`` as
            # producing ``id='slug'~38..38/toc`` (slug repr'd, suffix
            # outside the quotes) which is a SyntaxError when pasted.
            recovery_id = f"{_pa(ref)}~{chunk_spec[0]}..{chunk_spec[1]}/toc"
            raise BadInput(
                f"cannot combine chunk selector (~N..M) with view={effective_view!r}",
                next=f"get(id={recovery_id!r})",
            )

        if chunk_spec is not None:
            return self._render_chunks(ref, chunk_spec)

        if effective_view is None:
            return self._render_overview(ref)

        return self._render_view(ref, effective_view)

    # -- search --------------------------------------------------------------

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        page: int = 1,
        exclude: list[str] | None = None,
        mode: str | None = None,
        after: int | str | None = None,
        before: int | str | None = None,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='paper', q='your query')",
            )

        # Publish-date filter: ``after`` / ``before`` are inclusive
        # publication-year bounds (the corpus stores year, not full
        # dates). Validate at the agent boundary so a bad bound is a
        # clean error rather than zero silent hits.
        year_from = _coerce_search_year(after, "after")
        year_to = _coerce_search_year(before, "before")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise BadInput(
                f"after={year_from} is later than before={year_to}",
                next="search(kind='paper', q='…', after=2019, before=2023)",
            )

        # Validate the filter at the agent boundary. Same canonical-form
        # rejection as ``put(tags=...)`` so an agent that wrote
        # ``tags=['urgent']`` gets the same error shape it'd get on
        # write — not silently zero hits.
        # Per-kind axis enforcement: passing kind='paper' here means a
        # filter like ``tags=['STATUS:open']`` raises BadInput at the
        # boundary instead of silently returning zero hits — papers
        # have no STATUS axis. (Critic per-kind-axis follow-up.)
        normalized_tags = Tag.normalize_filter(tags, kind="paper")

        scope_ref_id: int | None = None
        if scope is not None:
            # ADR 0036: ``scope=`` accepts a universal handle (``pa<id>`` /
            # ``pc<id>``) — the form output now emits — resolving it to the
            # paper's ref_id; else the legacy slug / DOI path.
            scope_resolved = (
                self.store.resolve_handle(str(scope))
                if handle_registry.parse(str(scope)) is not None
                else None
            )
            if scope_resolved is not None:
                scope_ref_id = scope_resolved.ref_id
            else:
                scope_slug = _maybe_resolve_doi(self.store, str(scope))
                scope_ref = resolve_live_slug_ref(
                    self.store,
                    kind="paper",
                    id=scope_slug,
                    next_hint="search(kind='paper', q='...') to find one",
                    options=_suggest_paper_slugs(scope, store=self.store),
                )
                scope_ref_id = scope_ref.id

        # ``exclude=['slug1', 'slug2']`` drops every block of the
        # listed papers from the result set. Coarse / ref-level: a
        # ``slug~38`` entry is treated as ``slug``. Stale slugs are
        # silently dropped — the agent's exclude list may carry
        # ids that no longer resolve, and we'd rather quietly skip
        # than fail the whole search. The canonical use case is the
        # "show me hits 6..N" continuation rendered in the Next:
        # trailer below.
        excluded_slugs_in: list[str] = []
        exclude_ref_ids: list[int] = []
        if exclude:
            normalised: list[str] = []
            for raw in exclude:
                slug = _normalise_exclude_slug(str(raw), store=self.store)
                if slug is not None:
                    normalised.append(slug)
            # Dedup while preserving order so the trailer's "previous
            # exclude" cross-reference reads predictably.
            seen: set[str] = set()
            for s in normalised:
                if s not in seen:
                    seen.add(s)
                    excluded_slugs_in.append(s)
            if excluded_slugs_in:
                exclude_ref_ids = self.store.fetch_ref_ids_by_slugs(
                    excluded_slugs_in, kind="paper"
                )

        # Compute the query embedding for the semantic leg. A missing OR
        # failing embedder degrades to lexical-only (query_vec=None)
        # rather than 500 the whole search — the lexical leg still
        # answers (gripe #38684: search q='*' returned a 500). See
        # :func:`embed_query`.
        query_vec = query_vec_for(self.embedder, q, mode)

        # ``max_distance`` enforces a semantic relevance floor so a
        # nonsense query (``'xyzzy frobnicate quux'``) returns an
        # empty response instead of the top-K closest random blocks.
        # The lexical leg already has a natural zero (the tsquery
        # either matches or it doesn't); the floor is sem-only.
        # (Critic MAJOR #3.)
        # page=N → offset = (page-1) * page_size. Clamped to >= 0 so a 7B
        # caller passing ``page=0`` doesn't blow the query up.
        search_offset = max(0, (int(page) - 1) * int(page_size))

        # ``card_combined`` (the embedded title+authors+abstract+keywords
        # card, ``ord=-1``) is opted into the search so a *title* / meta
        # query surfaces the paper even when no body block matches — the
        # card is reachable in both the lexical and semantic legs. Body
        # chunks still win per paper (see the dedup below); the card is
        # only a fallback introducer, not a quotable hit.
        hits = self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="paper",
            scope_ref_id=scope_ref_id,
            tags=normalized_tags,
            limit=page_size,
            offset=search_offset,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=exclude_ref_ids or None,
            year_from=year_from,
            year_to=year_to,
            card_kinds=("card_combined",),
        )
        hits = _dedup_card_hits(hits)

        # When a publish-date filter is active, count papers that match
        # the query but have NO year — they're silently dropped by the
        # range predicate. Surfacing the count keeps an empty/short
        # year-range result from reading as "nothing exists" when the
        # real cause is missing metadata (the corpus has many such rows).
        year_notice = ""
        if year_from is not None or year_to is not None:
            omitted = self.store.count_paper_yearless_matches(
                q=q,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                exclude_ref_ids=exclude_ref_ids or None,
            )
            if omitted:
                lo = str(year_from) if year_from is not None else "…"
                hi = str(year_to) if year_to is not None else "…"
                year_notice = (
                    f"\n\n⚠ {omitted} matching paper(s) omitted — no publication "
                    f"year on record, so the {lo}..{hi} filter can't place them. "
                    "Fix metadata via /papers/triage, or drop after=/before= to "
                    "include them."
                )

        if not hits:
            # Use the canonical Next: block shape rather than an
            # inline ``next: ...`` prose line. The critic rules
            # (C17 / D12) say trailers share one delimiter style
            # across kinds, and the empty-search branch was the
            # last holdout of the lowercase-prose shape. (c5
            # unified-trailer patch.)
            #
            # DOI-shaped queries that miss are the dominant friction
            # case: agents fire 3-5 keyword variants trying to find a
            # paper that isn't in the corpus. Detect the DOI shape and
            # route to the structured stub-fetch pathway (finding +
            # ``precis worker --only fetch``) instead of suggesting a
            # wider search that will also miss.
            body = f"no paper blocks match {q!r}"
            doi_match = _DOI_RE.match(q.strip())
            if doi_match is not None:
                doi = doi_match.group(1)
                body += "\n\nThis DOI is not in the local corpus. "
                body += (
                    "Pull it into the corpus via the finding-chase + "
                    "Unpaywall/arXiv/S2 fetcher pipeline:"
                )
                body += render_next_section(
                    [
                        (
                            "put(kind='finding', title='<short claim>', "
                            f"body='<claim + setup>', cited_in='doi:{doi}', "
                            "scope={'...': '...'})",
                            "register the DOI as a chase target; the "
                            "fetcher will try Unpaywall/arXiv/S2 next pass",
                        ),
                        (
                            "precis stubs --awaiting",
                            "list stub backlog the fetcher will work on",
                        ),
                        (
                            "edit(kind='plaintext', id='./request_doi.md', "
                            f"mode='append', text='{doi} - <one-line reason>\\n')",
                            "(legacy) append to the plaintext queue — "
                            "deprecated; use put(kind='finding') above",
                        ),
                    ]
                )
            else:
                body += render_next_section(
                    [
                        (
                            f"search(kind='paper', q={q!r}, page_size=50)",
                            "widen the lexical net",
                        ),
                        (
                            "get(kind='skill', id='precis-help')",
                            "see search tips for other kinds",
                        ),
                    ]
                )
            return Response(body=body + year_notice)

        # Salience: heat the chunks this page surfaced (block-level).
        # One set-based bump; no-op for dream-actor reads.
        self.store.bump_salience([block.id for block, _ref, _score in hits])

        # Total-hits header: count blocks the lexical filter would
        # match without the LIMIT, so the agent sees "10 of K" when
        # paginated. RRF only re-ranks lexically-matching rows, so
        # the lexical count is the meaningful "K". (MCP critic
        # MAJOR #10b.)
        #
        # ``exclude_ref_ids`` is forwarded so the K reported in the
        # header is the *remaining* universe — i.e. when the caller
        # already excluded 5 papers, "10 of 42" tells them how many
        # hits are still left after their skip-list. Without this,
        # the header would lie ("10 of 47") and a 7B model would
        # think they had more to paginate through than actually
        # exist.
        total = self.store.count_blocks_lexical(
            q=q,
            kind="paper",
            scope_ref_id=scope_ref_id,
            tags=normalized_tags,
            exclude_ref_ids=exclude_ref_ids or None,
            card_kinds=("card_combined",),
        )

        # Hits arrive sorted by RRF fused rank — best first. The
        # raw fused number is, in absolute terms, a 1/(k+rank)
        # staircase that doesn't reflect query strength — but
        # every other block-level handler (web, think, markdown,
        # plaintext, conversation, python) renders the same value
        # as ``(score=X.XXXX)``, so paper opting out left agents
        # with a kind-specific shape inconsistency. The MCP critic
        # flagged this 2026-05-02; aligning here at cost of one
        # potentially-misleading float per hit, balanced by uniform
        # downstream parsing across kinds.
        # Round-2 picky 2026-05-31: stripped to two columns —
        # ``{handle, chunk_keywords}``. Score is internal-only
        # (it determines RRF order, which is the row order;
        # surfacing the float adds no signal once the rows are
        # already sorted). Title was dropped because the agent has
        # ``get(kind='paper', id='<handle-slug>')`` for the full
        # title + meta; spending 30-60 chars per row on the same
        # title for repeated hits on the same paper is wasteful.
        # The cluster-context hint (Phase E, once segmentation
        # lands) will be a single trailing line rather than a
        # per-row column.
        head = format_search_headline(
            n_returned=len(hits),
            total=total,
            noun="block hit",
            query=q,
        )
        # Each hit renders as a TOON row with its own per-chunk KeyBERT
        # keywords (from ``chunks.keywords``). Per-chunk keywords carry
        # the "what's in this specific chunk" signal directly — the
        # earlier segment-level excerpt sub-line had to go via a
        # central-sentence picker that produced too much noise.
        del query_vec  # no longer used for per-hit excerpt reranking
        table_rows: list[dict[str, str]] = []
        for block, ref, _score in hits:
            slug = ref.slug or "???"
            # ADR 0036: the computed chunk handle (``pc<chunk_id>``) is the
            # one address form; fall back to the legacy ``slug~pos`` only for
            # a kind with no chunk code.
            handle = (
                handle_registry.try_format(ref.kind, block.id, chunk=True)
                or f"{slug}~{block.pos}"
            )
            kw_list = block.keywords or []
            if kw_list:
                kw_display = ", ".join(kw_list[:5])
            else:
                chunk_text = _scrub_block_text(block.text)
                kw_display = _chunk_keywords_or_caption(chunk_text)
            table_rows.append(
                {
                    "handle": handle,
                    "chunk_keywords": kw_display,
                }
            )
        rendered_table = render_agent_table(
            table_rows,
            schema=["handle", "chunk_keywords"],
        )
        body = head + year_notice + "\n\n" + rendered_table

        # Pagination affordance — when the lexical total exceeds what
        # we returned, surface the narrow-with-scope path explicitly.
        # Without this trailer a 7B caller seeing ``# 100 of 101`` often
        # reads the header as "this is everything" and stops short of
        # hit #101. (MCP critic MAJOR — search has no pagination
        # affordance when capped.)
        #
        # Single ``Next:`` trailer for the whole search response.
        # Order (most actionable first):
        #   1. Drill-into-chunk: ``get(kind='paper', id=<handle>)`` —
        #      teaches that every handle in the table is a valid id.
        #   2. Pagination via ``exclude=`` (when more hits remain).
        #   3. Narrow-to-paper via ``scope=`` (multi-paper case).
        #   4. Salient-term refinement.
        # Round-2 picky 2026-05-30: previous code emitted two
        # separate ``Next:`` blocks back-to-back; merging keeps the
        # response shape consistent across kinds.
        nav: list[tuple[str, str]] = []
        if hits:
            # ADR 0036: point at the top hit by its computed chunk handle.
            first_handle = (
                handle_registry.try_format(hits[0][1].kind, hits[0][0].id, chunk=True)
                or f"{hits[0][1].slug or '???'}~{hits[0][0].pos}"
            )
            nav.append(
                (
                    f"get(id='{first_handle}')",
                    "read the full text of any hit (paste any handle above)",
                )
            )
            # F20: cluster-context navigation hint now uses the
            # dynamic TOC. Pointing at view='toc' on the paper gives
            # the agent the freshly-clustered structure for the
            # current corpus state — no segment-containing-chunk
            # lookup needed because clusters are computed on demand.

        # Singleton-hit special case (MCP critic MINOR-$): when
        # ``len(hits) == 1`` the original nav was 46 % of the response
        # and 100 % redundant — the scope suggestion narrowed to the
        # only hit's own paper (a no-op), and the salient-term
        # suggestion is moot when the caller already has a tight
        # match. Just bump ``page_size`` in that branch.
        if total > len(hits):
            if len(hits) == 1:
                nav.append(
                    (
                        f"search(kind='paper', q={q!r}, page_size=10)",
                        f"see more of the {total} matches",
                    )
                )
            else:
                # ADR 0036: scope by the top hit's paper record handle.
                top_handle = (
                    handle_registry.format_handle("paper", hits[0][1].id)
                    if hits
                    else None
                )

                # Pagination continuation: bump page=N. page= is the
                # canonical pagination knob; exclude= is a hand-skip
                # filter for known-irrelevant refs (kept available via
                # the arg but no longer the recommended next-step
                # because it bloats the call with a slug list).
                if total > page_size * page:
                    nav.append(
                        (
                            f"search(kind='paper', q={q!r}, page={page + 1})",
                            f"see the next {page_size} of {total} hits",
                        )
                    )

                if scope is None and top_handle is not None:
                    nav.append(
                        (
                            f"search(kind='paper', q={q!r}, scope='{top_handle}')",
                            f"narrow to blocks inside {top_handle}",
                        )
                    )
                # Round-2 picky F-9, 2026-05-30: previous wording was
                # ``q={q!r} + ' <salient term>'`` — Python-flavoured
                # pseudo-code that a literal-paste agent would send as
                # the verbatim string ``'cells' + ' <salient term>'``.
                # Spell the placeholder in pure-string form instead.
                nav.append(
                    (
                        f"search(kind='paper', q='{q} <salient term>')",
                        "tighten the query with a hit-specific token "
                        "(replace <salient term> with one)",
                    )
                )
                body += render_next_section(nav)

        return Response(body=body)

    # -- search_hits: structured form for cross-kind merge -------------------

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        exclude: list[str] | None = None,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level fused search returned as ``SearchHit``s.

        Same engine as :meth:`search`, but skips the per-handler
        rendering and surfaces the structured rows so the runtime
        cross-kind dispatcher can RRF-fuse them with hits from
        other kinds.  ``scope=`` is intentionally omitted — cross-
        kind merge has no per-paper scope.

        ``exclude=`` mirrors the ``search`` shape (coarse, ref-level
        slug list). Cross-kind callers can pass it through so
        pagination works across the merged stream.
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind="paper")
        exclude_ref_ids: list[int] = []
        if exclude:
            normalised: list[str] = []
            for raw in exclude:
                slug = _normalise_exclude_slug(str(raw), store=self.store)
                if slug is not None:
                    normalised.append(slug)
            if normalised:
                exclude_ref_ids = self.store.fetch_ref_ids_by_slugs(
                    normalised, kind="paper"
                )
        # query_vec= may be pre-supplied by the runtime cross-kind
        # dispatcher (computed once for all kinds), avoiding an
        # extra embed_one(q) per fanned-out kind.
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        # Opt the title/meta card in (same as :meth:`search`) so a paper
        # is reachable by title in the cross-kind merge too, then dedup
        # so a body hit wins over its own card.
        triples = self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="paper",
            tags=normalized_tags,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=exclude_ref_ids or None,
            card_kinds=("card_combined",),
        )
        triples = _dedup_card_hits(triples)
        # Salience bump (block-level); no-op for dream-actor reads.
        self.store.bump_salience([block.id for block, _ref, _score in triples])
        return block_hits_to_search_hits(triples, kind="paper")

    # -- seven-verb surface --------------------------------------------------

    def _resolve_paper_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair.

        Accepts a numeric ``ref_id`` (the web addresses papers by id,
        e.g. the triage queue's "Clear flag" → ``tag`` and the detail
        page's link ops), a slug, or a DOI. Slugs are never all-digits,
        so the numeric branch is unambiguous — and it must come first,
        because :func:`resolve_live_slug_ref` stringifies its id and
        would otherwise look ``"5822"`` up as a *cite_key* (a guaranteed
        miss that raised ``NotFound`` and silently no-op'd the web tag
        ops). Mirrors :meth:`_resolve_paper_ref_id`'s numeric path.

        Rejects chunk selectors and path views — link/tag ops live
        at the ref level only. Raises ``BadInput`` (selector
        present) or ``NotFound`` (slug unknown) so the caller can
        let those propagate.
        """
        if isinstance(id, int) or (isinstance(id, str) and id.strip().isdigit()):
            ref_id = int(id)
            ref = self.store.fetch_refs_by_ids([ref_id], include_deleted=False).get(
                ref_id
            )
            if ref is None or ref.kind != "paper":
                raise NotFound(
                    f"paper id={ref_id} not found",
                    next="search(kind='paper', q='...') to find existing",
                )
            return ref.slug or str(ref_id), ref_id
        raw_id = _maybe_resolve_doi(self.store, str(id))
        slug, chunk_spec, path_view = _parse_paper_id(raw_id)
        reject_chunk_or_path_view(
            kind="paper",
            slug=slug,
            sel=chunk_spec,
            path_view=path_view,
        )
        ref = resolve_live_slug_ref(
            self.store,
            kind="paper",
            id=slug,
            next_hint="search(kind='paper', q='...') to find existing slugs",
            options=_suggest_paper_slugs(slug, store=self.store),
        )
        return slug, ref.id

    def _resolve_paper_ref_id(self, id: str | int) -> int:
        """Resolve an id to a live paper ``ref_id``.

        Accepts a numeric ``ref_id`` (the web addresses papers by id),
        a slug, or a DOI — slugs are never all-digits, so the branch is
        unambiguous. Raises ``NotFound`` if missing / soft-deleted or
        the ref isn't a paper. Thin wrapper over
        :meth:`_resolve_paper_slug` (which owns the numeric/slug/DOI
        resolution) for callers that only need the id.
        """
        _slug, ref_id = self._resolve_paper_slug(id)
        return ref_id

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int,
        title: str | None = None,
        year: int | None = None,
        authors: Any = None,
        abstract: str | None = None,
        doi: str | None = None,
        arxiv: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Repair a paper's bibliographic metadata.

        The operator / agent affordance for fixing parse errors — wrong
        DOI, missing authors, off-by-one year. Paper *bodies* stay
        import-only; this never touches block text. Only the fields
        passed are changed; a ``None`` / blank field is left as-is
        (the web form's "leave blank to keep" contract).

        ``authors`` accepts any tolerated shape (name strings,
        ``{family, given}`` or ``{name}`` dicts) and is canonicalised
        to the stored ``[{"name": …}]`` shape via
        :func:`precis.utils.authors.to_name_dicts`. ``abstract`` merges
        into ``meta``; ``doi`` / ``arxiv`` replace this ref's alias via
        :meth:`Store.set_ref_identifier`.
        """
        ref_id = self._resolve_paper_ref_id(id)
        new_title = title.strip() if isinstance(title, str) and title.strip() else None
        new_authors = to_name_dicts(authors) if authors else None
        meta_patch: dict[str, Any] = {}
        if isinstance(abstract, str) and abstract.strip():
            meta_patch["abstract"] = abstract.strip()
        has_doi = bool(doi and str(doi).strip())
        has_arxiv = bool(arxiv and str(arxiv).strip())
        if (
            new_title is None
            and year is None
            and not new_authors
            and not meta_patch
            and not has_doi
            and not has_arxiv
        ):
            raise BadInput(
                "edit(kind='paper') needs at least one field to change",
                next="edit(kind='paper', id=<slug|id>, authors=[...], year=2024)",
            )
        changed: list[str] = []
        with self.store.tx() as conn:
            updated = self.store.update_paper_fields(
                ref_id,
                title=new_title,
                year=year,
                authors=new_authors,
                meta_patch=meta_patch or None,
                source="edit",
                conn=conn,
            )
            for scheme, value in (("doi", doi), ("arxiv", arxiv)):
                if (
                    value
                    and str(value).strip()
                    and self.store.set_ref_identifier(
                        ref_id, scheme, str(value), source="edit", conn=conn
                    )
                ):
                    changed.append(scheme)
            # Rewrite the derived search cards so an edit actually changes
            # what title/author/abstract search matches against — otherwise
            # the card_* chunks keep the stale (pre-edit) text. Uses the
            # *merged* post-update values (COALESCE means an unchanged field
            # still returns its current value), so e.g. a title-only edit
            # still rebuilds card_combined from the live authors + abstract.
            if new_title is not None or new_authors or meta_patch:
                meta = updated.meta or {}
                abstract_val = meta.get("abstract", "")
                kw = meta.get("keywords", [])
                rewrite_cards(
                    conn,
                    ref_id,
                    title=updated.title or "",
                    author_names=[
                        a.get("name", "")
                        for a in (updated.authors or [])
                        if a.get("name")
                    ],
                    abstract=abstract_val if isinstance(abstract_val, str) else "",
                    keywords=list(kw) if isinstance(kw, list) else [],
                )
        if new_title is not None:
            changed.append("title")
        if year is not None:
            changed.append("year")
        if new_authors:
            changed.append(f"authors({len(new_authors)})")
        if meta_patch:
            changed.append("abstract")
        return Response(
            body=(
                f"updated paper id={ref_id}: "
                f"{', '.join(changed) if changed else 'no change'}."
            )
        )

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove paper tags. Allowed axes: ``SRC``, ``CACHE`` + open."""
        require_tag_ops("paper", add, remove)
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "paper", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="paper",
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
        """Add or remove a link from this paper to another ref."""
        target = require_link_target("paper", target)
        validate_link_mode(mode)
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="paper",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # -- rendering helpers ---------------------------------------------------

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        doi = meta.get("doi")
        year = meta.get("year")
        authors_raw = meta.get("authors")
        authors = _format_authors(authors_raw)
        journal = _clean_inline_text(str(meta.get("journal") or ""))
        n_blocks = self.store.count_blocks(ref.id)

        lines: list[str] = []
        banner = _retraction_banner(ref)
        if banner:
            lines.append(banner)
            lines.append("")
        lines.extend([f"# {_pa(ref)}", f"_{_clean_inline_text(ref.title)}_"])
        if authors:
            lines.append(authors)
        venue: list[str] = []
        if journal:
            venue.append(journal)
        if year:
            venue.append(str(year))
        if venue:
            lines.append(", ".join(venue))
        if doi:
            lines.append(f"doi: {doi}")
        lines.append("")
        lines.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")
        abstract = meta.get("abstract")
        if abstract:
            lines.append("")
            # Strip JATS XML *before* excerpting — otherwise the
            # 500-char window can chop a tag mid-attribute and the
            # downstream "<jats:" sniff won't catch the dangling
            # garbage. The MCP critic flagged the default overview
            # leaking ``<jats:title>Abstract</jats:title><jats:p>…``
            # verbatim into the response body; ``_strip_jats`` is
            # the same helper view='abstract' uses, so the two
            # paths agree on cleanup.
            lines.append(_excerpt(_strip_jats(str(abstract)), limit=500))

        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "see the TOC",
                ),
                (
                    f"get(id='{_pa(ref)}~0..5')",
                    "read the first 6 chunks",
                ),
                (
                    f"get(id='{_pa(ref)}', view='bibtex')",
                    "get the BibTeX entry",
                ),
                (
                    f"search(kind='paper', q='...', scope='{_pa(ref)}')",
                    "search blocks within this paper",
                ),
            ]
        )
        return Response(body=body)

    def _render_view(self, ref: Ref, view: str) -> Response:
        if view == "log":
            # Per-ref chronological event log across every subsystem
            # that writes to ref_events (chase, fetcher, provenance,
            # ingest). No source filter — for papers the cross-cutting
            # view is the useful one ("what's happened to this paper?").
            from precis.handlers._event_log_render import render_event_log

            return render_event_log(self.store, ref.id)
        if view == "abstract":
            abstract = (ref.meta or {}).get("abstract")
            if not abstract:
                # Empty result still teaches the next call shape:
                # the paper's body is reachable via TOC + chunk
                # ranges even when the publisher's abstract metadata
                # is missing. Without this trailer the bare
                # "no abstract on file" was a dead end (MCP critic
                # MINOR-C 2026-05-02).
                slug = ref.slug or "???"
                body = f"no abstract on file for {slug}"
                body += render_next_section(
                    [
                        (
                            f"get(id='{_pa(ref)}', view='toc')",
                            "see the TOC and pick a section",
                        ),
                        (
                            f"get(id='{_pa(ref)}~0..5')",
                            "read the first 6 chunks (often include the abstract)",
                        ),
                        (
                            f"search(kind='paper', q='abstract', scope='{_pa(ref)}')",
                            "search blocks within this paper",
                        ),
                    ]
                )
                return Response(body=body)
            # Strip JATS XML namespace tags (<jats:title>, <jats:p>, …)
            # that some publishers leave in the metadata, then run the
            # entity unescape pipeline so ``&amp;`` lands as ``&``.
            # The MCP critic flagged the body-only response as missing
            # any affordance — add a slug header + Next: trailer so
            # the caller knows which paper they're reading and where
            # to go next. (MCP critic NIT — abstract has no header /
            # Next:.)
            cleaned = _clean_inline_text(_strip_jats(str(abstract)))
            slug = ref.slug or "???"
            title = _clean_inline_text(ref.title)
            body = f"# {slug} - abstract\n_{title}_\n\n{cleaned}"
            body += render_next_section(
                [
                    (
                        f"get(id='{_pa(ref)}', view='toc')",
                        "see the TOC",
                    ),
                    (
                        f"get(id='{_pa(ref)}', view='bibtex')",
                        "get the BibTeX entry",
                    ),
                ]
            )
            return Response(body=body)

        if view == "toc":
            return self._render_toc(ref, scope=None)

        if view in ("bibtex", "ris", "endnote"):
            # F15: pre-fetch the DOI from ref_identifiers. The v2
            # schema moved DOI off ref.meta into its own table, but
            # _format_citation was still reading meta.get('doi') and
            # always finding None — so every bibtex looked like a
            # stub even when the data was fully populated.
            doi: str | None = None
            try:
                for scheme, value, _src in self.store.list_ref_identifiers(ref.id):
                    if scheme == "doi" and value:
                        doi = value
                        break
            except Exception:
                doi = None
            return Response(body=_format_citation(ref, style=view, doi=doi))

        if view == "health":
            return self._render_health(ref)

        if view == "abbrevs":
            return self._render_abbrevs(ref)

        if view == "bibliography":
            return self._render_bibliography(ref)

        # The MCP critic flagged ``view='figures'`` as a silent
        # failure — the agent had no signal that figure retrieval
        # is unsupported. Surface a sharper hint pointing at the
        # caption-only figure workflow documented in
        # precis-paper-help so the agent doesn't keep retrying.
        # (Critic MAJOR #4.)
        if view in ("figures", "fig", "figure"):
            raise Unsupported(
                f"figure view {view!r} not implemented for kind='paper'",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "figure binaries aren't served - figures live as legend "
                    "blocks inside the body. Find the figure number via "
                    "view='toc', then read the legend block "
                    "(e.g. 'Figure 3. …' on a ~N block). See the "
                    "'Figures' section of precis-paper-help."
                ),
            )
        # ``view='fig/<N>'`` is documented in precis-paper-help as a
        # reserved-for-future affordance.  Without a dedicated branch
        # it falls into the generic "unknown view" error below,
        # which makes a caller who *has* read the help skill assume
        # the docs are wrong rather than the build being early.
        # Surface the reservation explicitly so the caller knows to
        # use the caption-only workaround until figure-binary
        # serving is wired.  (MCP critic MINOR — fig/<N> documented
        # but unrecognised view path returns the same enum as a
        # typo.)
        if view.startswith("fig/"):
            raise Unsupported(
                f"view={view!r} is reserved for a future build",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "view='fig/<N>' is documented in precis-paper-help as "
                    "reserved - figure-binary serving isn't wired yet.  "
                    "Until then, find the figure number via view='toc' "
                    "and read the legend block on the matching ~N "
                    "(e.g. 'Figure 3. …')."
                ),
            )
        raise Unsupported(
            f"unknown view {view!r} for kind='paper'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-paper-help - try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_bibliography(self, ref: Ref) -> Response:
        """``view='bibliography'`` — citations referencing this paper.

        Walks the ``links`` table for ``cites`` edges whose
        destination is this paper (``links_for(ref_id,
        relation='cited-by')`` — the inverse-rewrite returns the
        ``cites`` rows from citation→paper). For each, pulls the
        citation ref and renders its claim, source handle, verbatim
        quote, and verifier confidence.

        Returns a "no citations on file" placeholder with recovery
        hints when no citations exist for this paper.
        """
        cite_links = self.store.links_for(ref.id, direction="in", relation="cites")
        slug = ref.slug or "???"

        if not cite_links:
            body = f"# {slug} bibliography — 0 citations on file"
            body += render_next_section(
                [
                    (
                        f"get(id='{_pa(ref)}', view='toc')",
                        "browse segments to find drillable claims",
                    ),
                    (
                        "get(kind='skill', id='precis-citation-help')",
                        "how to file a verified citation",
                    ),
                ]
            )
            return Response(body=body)

        rows: list[dict[str, str]] = []
        for link in cite_links:
            citation = self.store.get_ref(kind="citation", id=link.src_ref_id)
            if citation is None:
                continue
            meta = citation.meta or {}
            handle = meta.get("source_handle") or ""
            quote = _clean_inline_text(meta.get("source_quote") or "")
            quote = _excerpt(quote, limit=120)
            claim = _clean_inline_text(meta.get("claim") or citation.title or "")
            claim = _excerpt(claim, limit=80)
            confidence = meta.get("verifier_confidence")
            conf_str = f"{float(confidence):.2f}" if confidence is not None else "?"
            rows.append(
                {
                    "id": f"citation:{citation.id}",
                    "claim": claim,
                    "source": handle,
                    "conf": conf_str,
                    "quote": f'"{quote}"',
                }
            )

        head = f"# {slug} bibliography — {len(rows)} citation{'s' if len(rows) != 1 else ''}"
        body = (
            head
            + "\n\n"
            + render_agent_table(
                rows, schema=["id", "claim", "source", "conf", "quote"]
            )
        )
        body += render_next_section(
            [
                (
                    "get(kind='citation', id=<N>)",
                    "read one citation's full record (the verifier's caveats too)",
                ),
                (
                    "get(kind='skill', id='precis-citation-help')",
                    "the verifier-workflow agent surface",
                ),
            ]
        )
        return Response(body=body)

    def _render_abbrevs(self, ref: Ref) -> Response:
        """F20: render the per-paper abbreviation legend.

        Reads ``ref.meta['abbrevs']`` (populated lazily by the
        chunk_keywords worker via Schwartz-Hearst over the body text).
        Renders alphabetical TOON ``{short	long}``. Empty-paper case
        gets a placeholder + a pointer at the worker.
        """
        slug = ref.slug or "???"
        meta = ref.meta or {}
        abbrevs = meta.get("abbrevs") or {}
        if not abbrevs:
            body = (
                f"# {slug} — no abbreviations detected yet\n\n"
                "Detection runs on the first chunk_keywords worker pass "
                "over this paper. Either the worker hasn't run yet, or "
                "the body text contains no ``long form (SHORT)`` "
                "patterns that Schwartz-Hearst recognises."
            )
            return Response(body=body)
        # Normalise: legacy entries are plain strings, newer ones
        # may be ``{long, first_at}`` envelopes. Render to plain dict
        # for sorting + table rendering.
        flat: dict[str, str] = {}
        for short, val in abbrevs.items():
            if isinstance(val, str):
                flat[short] = val
            elif isinstance(val, dict) and "long" in val:
                flat[short] = str(val["long"])
        rows = [{"short": short, "long": flat[short]} for short in sorted(flat)]
        head = f"# {slug} — {len(rows)} abbreviation(s)"
        table = render_agent_table(rows, schema=["short", "long"])
        return Response(body=f"{head}\n\n{table}")

    def _render_health(self, ref: Ref) -> Response:
        """Phase 5 shim: ``view='health'`` on a paper ref.

        Looks up the paper's DOI from ``ref_identifiers``, then
        delegates to the provenance kind's ``check_doi`` so agents
        that already have a slug don't have to do the slug→DOI
        lookup themselves. The full markdown report comes back via
        ``render_single`` — same shape as
        ``get(kind='provenance', id='<doi>')``.

        Edge cases:
        - **Paper has no DOI on file** (preprints from venues
          Crossref doesn't index, book chapters, hand-ingested
          records). We surface a clear error rather than silently
          succeeding — provenance is meaningless without a DOI.
        """
        from precis.handlers._provenance_report import render_single
        from precis.ingest.provenance import check_doi

        # Pull all known identifiers for this ref; pick the DOI.
        try:
            aliases = self.store.list_ref_identifiers(ref.id)
        except Exception as exc:
            raise BadInput(
                f"paper view='health': cannot read identifiers for {ref.slug}: {exc}",
                next=f"get(id='{_pa(ref)}', view='bibtex')",
            ) from exc

        doi: str | None = None
        for scheme, value, _source in aliases:
            if scheme == "doi" and value:
                doi = value
                break

        if doi is None:
            slug = ref.slug or "???"
            raise BadInput(
                f"paper view='health': no DOI on file for {slug} — "
                "provenance checks require a DOI",
                next=f"get(id='{_pa(ref)}', view='abstract')",
            )

        # Inherit the mailto convention from the env, same as the
        # provenance handler does at boot.
        import os

        mailto = os.environ.get("PRECIS_CROSSREF_MAILTO") or None
        result = check_doi(doi, store=self.store, mailto=mailto)
        return Response(body=render_single(result))

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        blocks = self.store.list_blocks_for_ref(ref.id, pos_range=(lo, hi))
        if not blocks:
            raise NotFound(
                f"no blocks in {ref.slug} for range ~{lo}..{hi}",
                next=f"get(id='{_pa(ref)}', view='toc')",
            )

        # Figure-and-caption coalescing: when a single-block request
        # lands on an image-only block, fetch the next block too so the
        # caller sees the caption in the same response. Without this an
        # agent gets just ``![](_page_19_Figure_1.jpeg)`` — no number,
        # no caption, and a relative URL that nothing serves.
        # (MCP critic MAJOR — figure block returns image marker with no
        # caption.)
        if len(blocks) == 1 and lo == hi and _is_image_only_block(blocks[0].text):
            tail = self.store.list_blocks_for_ref(ref.id, pos_range=(hi + 1, hi + 1))
            if tail and _looks_like_caption(tail[0].text):
                blocks = [*blocks, *tail]
                hi = tail[0].pos

        lines: list[str] = []
        banner = _retraction_banner(ref)
        if banner:
            lines.append(banner)
            lines.append("")
        for b in blocks:
            # ADR 0036: head each chunk with its computed handle (``pc<id>``);
            # the legacy ``slug~pos`` stays only for a kind with no chunk code.
            b_handle = (
                handle_registry.try_format(ref.kind, b.id, chunk=True)
                or f"{ref.slug}~{b.pos}"
            )
            lines.append(f"# {b_handle}")
            lines.append(_render_block_body(ref.slug or "???", b.pos, b.text))
            lines.append("")

        # Next: trailer — adjacent ranges + parent toc + citation.
        # Use the actual block count so the hint never points off the end.
        # Degenerate single-block ranges render as ``~N`` rather than
        # ``~N..N``: the MCP critic flagged ``~77..77`` as training the
        # wrong call shape — agents who saw a "range" hint then
        # extrapolated ``~5..5`` for unrelated singletons later. The
        # canonical single-block form is ``~N``. (Critic MINOR m6.)
        #
        # Single-block reads also widen the forward suggestion into a
        # range so a "next chunk" hint doesn't train a linear ~N → ~N+1
        # → ~N+2 sequential scan (observed pattern: agent reading
        # gerfen2011~13 through ~21 one-by-one across ~10 LLM turns
        # @ ~3min/turn when a single ~13..21 range read would have
        # finished in one).  The promoted "navigate via TOC" hint
        # comes first in single-block mode, since paging-by-block is
        # almost never the right strategy when scanning a paper.
        total = self.store.count_blocks(ref.id)
        nav: list[tuple[str, str]] = []
        single_block = lo == hi

        # In single-block mode, lead with the two structural reads
        # that are almost always more useful than paging linearly:
        #
        # 1. In-paper semantic search — when looking for a specific
        #    quote/section, scoped search beats reading sequentially.
        #    The same fused lexical+embedding index used by
        #    cross-paper search applies here, just narrowed to one
        #    paper.
        # 2. TOC — structural map of the whole paper, ~50 lines.
        #
        # In range mode these stay available but at lower priority
        # than the next/prev range hints.
        if single_block:
            nav.append(
                (
                    f"search(kind='paper', q='your query', scope='{_pa(ref)}')",
                    "search inside this paper "
                    "(fused lexical+embedding) - usually beats paging",
                )
            )
            nav.append(
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "TOC - structural map of the paper",
                )
            )

        # Forward read is always a range — never advertise a bare
        # ``~N+1`` single-block hint. Single-block reads widen to a
        # 5-block window; range reads widen to the same size as the
        # current read. Backward navigation ("previous chunk") and
        # range-scoped TOC are dropped: the former is rarely the
        # right next move when reading forward, and a TOC scoped to
        # a small range usually has at most one section header — both
        # waste tokens in every chunk response.
        if hi + 1 < total:
            span = 5 if single_block else (hi - lo + 1)
            n_next = min(span, total - 1 - hi)
            # ADR 0036: forward read via relative navigation off the last
            # chunk's handle (``pc<id>+1..N``) — self-identifying, no kind=.
            last = blocks[-1]
            last_h = handle_registry.try_format(ref.kind, last.id, chunk=True)
            if last_h is not None:
                rel = f"+1..{n_next}" if n_next > 1 else "+1"
                hint_id = f"{last_h}{rel}"
            else:
                nxt_lo, nxt_hi = hi + 1, hi + n_next
                hint_id = (
                    f"{ref.slug}~{nxt_lo}..{nxt_hi}"
                    if nxt_hi > nxt_lo
                    else f"{ref.slug}~{nxt_lo}"
                )
            nav.append(
                (
                    f"get(id='{hint_id}')",
                    f"next {n_next} chunks" if n_next > 1 else "next chunk",
                )
            )
        if not single_block:
            # Range mode: full TOC is the structural escape hatch.
            nav.append(
                (
                    f"get(id='{_pa(ref)}', view='toc')",
                    "see the full TOC",
                )
            )
        nav.append(
            (
                f"get(id='{_pa(ref)}', view='bibtex')",
                "get the BibTeX entry",
            )
        )
        body = "\n".join(lines).rstrip() + render_next_section(nav)
        return Response(body=body)

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # F3: single source of truth for paper views. Was previously
        # a hand-curated subset that contradicted ``_SUPPORTED_VIEWS``
        # — ``slug`` was advertised here but had no dispatch arm, and
        # ``ris``/``endnote``/``health`` were dispatched but not
        # advertised, so the agent couldn't discover them. Mirroring
        # ``_SUPPORTED_VIEWS`` directly removes the drift.
        return list(_SUPPORTED_VIEWS)

    def chunks_for_toc(self, ref: Any) -> ChunksForToc:
        """Adapter for the generic TOC renderer.

        Fetches every block of the paper (with embeddings) once,
        plus the H1/H2 structure detected by
        :func:`_paper_toc.detect_heading`. The TOC renderer caches
        on ref_id + chunker/embedder versions, so this method's
        cost amortises across repeated TOC views of the same paper.
        """
        from precis.handlers._paper_toc import detect_heading

        blocks = self.store.list_blocks_for_ref(ref.id, with_embedding=True)
        if not blocks:
            return ChunksForToc(
                chunks_text=(),
                embeddings=None,
                h2_boundaries=(),
            )
        # Sort by pos to guarantee reading order.
        blocks = sorted(blocks, key=lambda b: b.pos)
        chunks_text = tuple(b.text for b in blocks)
        # Canonical positions = block.pos so TOC handles (slug~N)
        # resolve via ``get(id='slug~<pos>')``. Skipping this would
        # leave handles using list indices and break search-hit
        # cluster lookups when block.pos has gaps.
        positions = tuple(b.pos for b in blocks)

        # Per-block embeddings — None when any block lacks one
        # (mixed corpus or partial reingest). The renderer falls
        # back to H2 / flat listing when embeddings is None.
        if all(b.embedding is not None for b in blocks):
            embeddings: tuple[tuple[float, ...], ...] | None = tuple(
                tuple(b.embedding) for b in blocks
            )
        else:
            embeddings = None

        # H2 boundaries: detect headings in each block, then walk
        # to assign each H1/H2 a (start, end) span. We treat both
        # H1 and H2 as section markers for TOC purposes — the
        # generic renderer just needs "where do natural sections
        # start and end" not the H1-vs-H2 hierarchy.
        #
        # Filter out journal-template "headings" (``PAPER``, ``View
        # Article Online``, ``Broader context``, ``Article info``,
        # …). These come from the markdown ingester picking up
        # journal page chrome as H1/H2 — they're not real sections
        # and they confuse the TOC's H2-mode policy (one of them
        # often expands to cover the entire body because the real
        # body sections aren't marked with markdown headings).
        # Round-2 picky #3 / cai23 verification 2026-05-31.
        headings: list[tuple[int, str]] = []
        for b in blocks:
            h = detect_heading(b)
            if h is None or h.level not in (1, 2):
                continue
            if _is_journal_template_heading(h.title):
                continue
            headings.append((b.pos, h.title))

        h2_boundaries: list[tuple[int, int, str]] = []
        for i, (start, title) in enumerate(headings):
            end = headings[i + 1][0] - 1 if i + 1 < len(headings) else blocks[-1].pos
            h2_boundaries.append((start, end, title))

        return ChunksForToc(
            chunks_text=chunks_text,
            embeddings=embeddings,
            h2_boundaries=tuple(h2_boundaries),
            positions=positions,
            chunker_version=_PAPER_CHUNKER_VERSION,
            embedder_name=getattr(self.embedder, "model", "unknown"),
            embedder=self.embedder,
        )

    def _render_toc(
        self,
        ref: Ref,
        *,
        scope: tuple[int, int] | None,
    ) -> Response:
        """Render the smart TOC, optionally scoped to a block range.

        Phase B integration 2026-05-31: replaced the H1/H2-only
        ``build_toc`` renderer with the unified
        :mod:`precis.utils.toc` renderer (TextTiling-style embedding
        segmentation, H2-first fallback, per-segment RAKE, shared-
        phrases footer, abbreviation legend). The old ``build_toc``
        / ``render_toc`` path is retained for callers that need
        explicit H1-hierarchy rendering — see git log.
        """
        # Dynamic clustering at request time over per-chunk keywords
        # (``chunks.keywords``, populated by the chunk_keywords worker).
        # See :mod:`precis.utils.toc_db` for the algorithm.
        body = render_from_store(
            store=self.store,
            ref_id=ref.id,
            # ADR 0036: emit the universal record handle (``pa<id>``) so
            # every row + drill-in hint is a copy-pasteable get id. The
            # legacy ``kind:slug~pos`` form was unparseable on input.
            handle=_pa(ref),
            kind="paper",
            scope=scope,
        )
        banner = _retraction_banner(ref)
        if banner:
            body = f"{banner}\n\n{body}"
        return Response(body=body)

    def _render_list_papers(self) -> Response:
        # Cap the page at 50 — production corpora can be 1000s of papers
        # and a flat dump blows the agent's context. We expose the total
        # count and a search affordance so the agent has somewhere to go.
        limit = 50
        refs = self.store.list_refs(kind="paper", limit=limit)
        total = self.store.count_refs(kind="paper")
        if not refs:
            return Response(
                body=(
                    "no papers ingested yet - "
                    "use `precis jobs ingest-bundles <dir>` to populate"
                )
            )
        suffix = "" if total <= limit else f" of {total}"
        # Surface total corpus depth so the agent doesn't have to
        # estimate chunk volume from per-paper counts (#38683).
        total_chunks = self.store.count_chunks_for_kind("paper")
        lines = [
            f"# {len(refs)} paper{'s' if len(refs) != 1 else ''}{suffix}"
            f"  ({total_chunks} chunks)"
        ]
        for r in refs:
            year = (r.meta or {}).get("year") or ""
            # Run titles through the JATS/entity cleanup before
            # excerpting — otherwise a title like
            # ``Cu/ZnO<sub>x</sub>`` lands in the list verbatim and
            # any LLM reading it copies the markup back into prose.
            # (MCP critic MINOR — list view leaks raw HTML/JATS.)
            preview = _excerpt(_clean_inline_text(r.title), limit=80)
            yr = f"  ({year})" if year else ""
            lines.append(f"  {_pa(r):<30}{yr}  {preview}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    "search(kind='paper', q='your topic')",
                    "find a specific paper by topic",
                ),
                (
                    "get(id='pa<id>')",
                    "open one paper from the list (paste any handle above)",
                ),
            ]
        )
        return Response(body=body)


# ---------------------------------------------------------------------------
# Slug + chunk parsing
# ---------------------------------------------------------------------------

# Slugs are lowercase alphanumeric + hyphens. The `~` introduces a chunk
# selector; the rest of the string is parsed as a path of `view/sub`
# segments.
_SLUG_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)(.*)$")
_RANGE_RE = re.compile(r"^(\d+)(?:\.\.|-)(\d+)$")
_CHUNK_RE = re.compile(r"^(\d+)$")

# A DOI-form paper id. DOIs start with ``10.<registrant>/<suffix>`` per
# the IDF spec; the suffix can legally contain slashes and dots (e.g.
# ``10.1038/s41598-023-44772-6``, ``10.1111/jnc.13915``), which is why
# they can't be routed through ``_SLUG_RE`` — the regex would try to
# split the DOI on ``/`` as a view path and fail. Chunk selectors
# (``~38``) still attach to DOI-form ids; view paths (``/abstract``)
# do not — use the ``view=`` kwarg instead, because we can't safely
# disambiguate ``/abstract`` as "view=abstract" vs "DOI suffix /abstract".
_DOI_RE = re.compile(r"^(10\.\d+/[^~]+?)(~.*)?$")

_VIEW_PATH_ALIASES: dict[tuple[str, ...], str] = {
    ("cite", "bib"): "bibtex",
    ("cite", "bibtex"): "bibtex",
    ("cite", "ris"): "ris",
    ("cite", "endnote"): "endnote",
    ("abstract",): "abstract",
    ("toc",): "toc",
    ("bibtex",): "bibtex",
    ("ris",): "ris",
    ("endnote",): "endnote",
}


# ── Journal-template heading filter ────────────────────────────────
#
# The markdown ingester (marker / PDFs → markdown) sometimes promotes
# page chrome to H1 / H2: an article-type label like ``PAPER`` at the
# top of a journal page, ``View Article Online`` from the side nav,
# ``Broader context`` from a publisher sidebar, ``Article info``
# blocks. None of these are real paper sections. They mislead the
# TOC's H2-mode policy — typically one of them then expands to cover
# the entire body because the actual Introduction / Methods / Results
# aren't marked with markdown headings, and the agent ends up with a
# TOC labelled "Broader context" for 85 chunks of unrelated body.
#
# Filter is a small allow-deny: known template strings + an "all-caps
# short word" rule for the ``PAPER`` / ``ARTICLE`` / ``BRIEF`` class.
# Real sections are almost never single uppercase words, so the rule
# is conservative.

#: Case-insensitive exact-match strings that are journal-template
#: chrome, not real section headings. Extend as new patterns appear.
_JOURNAL_TEMPLATE_HEADINGS: frozenset[str] = frozenset(
    {
        # Article-type labels
        "paper",
        "article",
        "review",
        "review article",
        "research article",
        "editorial",
        "communication",
        "letter",
        "news",
        "perspective",
        "news & views",
        # Journal page nav / chrome
        "view article online",
        "article info",
        "article information",
        "article history",
        "article menu",
        "cite this article",
        "how to cite",
        "download pdf",
        "download citation",
        "metrics",
        "altmetric",
        "open access",
        # Date stamps that sometimes get heading-promoted
        "received",
        "accepted",
        "revised",
        "published",
        "published online",
        # Publisher sidebars
        "broader context",
        "graphical abstract",
        "highlights",
        "key points",
        # Footer chrome
        "permissions",
        "reprints",
        "supporting information",
        "supplementary information",
        "supplementary material",
        "this journal is",
    }
)


_RETRACTION_LABELS: dict[str, str] = {
    "retracted": "RETRACTED",
    "expression_of_concern": "EXPRESSION OF CONCERN",
    "corrected": "CORRECTED",
}


def _retraction_banner(ref: Ref) -> str | None:
    """One-line warning banner for retracted / EoC / corrected papers.

    Returns ``None`` when ``ref.retraction_status`` is unset. Otherwise
    returns a single Markdown line including the status label, a date
    when ``retracted_at`` is populated, and a pointer to the provenance
    handler for full notice details. The banner is meant to be the
    first line of any paper view (overview / TOC / chunks).
    """
    status = (ref.retraction_status or "").strip().lower()
    if not status:
        return None
    label = _RETRACTION_LABELS.get(status, status.upper())
    parts = [f"> [!] **{label}**"]
    when = ref.retracted_at
    if when is not None:
        parts.append(f"({when.date().isoformat()})")
    reason = (ref.retraction_reason or "").strip()
    if reason:
        parts.append(f"— {_clean_inline_text(reason)}")
    doi = (ref.meta or {}).get("doi") if ref.meta else None
    if doi:
        parts.append(
            f"— see `get(kind='provenance', id={str(doi)!r})` for the full notice."
        )
    else:
        parts.append("— see the provenance handler for the full notice.")
    return " ".join(parts)


def _is_journal_template_heading(title: str) -> bool:
    """True when ``title`` is journal-template chrome, not a real section.

    Three rules, in order:

    1. Exact (case-insensitive) match against
       :data:`_JOURNAL_TEMPLATE_HEADINGS` — known offenders from the
       major chemistry / biology / physics publishers.
    2. Single uppercase word ≤ 8 chars (``PAPER``, ``ARTICLE``,
       ``NEWS``, ``BRIEF``). Real H1/H2 section titles are almost
       never a single short ALL-CAPS word; when they are, the cost
       of dropping one real section beats the cost of admitting
       the chrome.
    3. Empty / whitespace-only.

    Otherwise pass through.
    """
    if not title or not title.strip():
        return True
    raw = title.strip()
    if raw.lower() in _JOURNAL_TEMPLATE_HEADINGS:
        return True
    if raw.isupper() and raw.isalpha() and " " not in raw and 2 <= len(raw) <= 8:
        return True
    return False


def _maybe_resolve_doi(store: Store, raw: str) -> str:
    """Translate a DOI-form paper id to its slug form.

    When an agent hands us a DOI (e.g. ``10.1111/jnc.13915``) as the
    paper id, route it through ``meta->>'doi'`` and substitute the
    slug so the rest of the pipeline (``_parse_paper_id``,
    :func:`resolve_live_slug_ref`, rendering) stays slug-addressed
    and unchanged.

    Chunk selectors ride along: ``10.1111/jnc.13915~38`` →
    ``wang2020dopamine~38``. View paths are *not* supported on
    DOI-form ids — DOI suffixes can legally contain ``/``, so we
    can't disambiguate ``10.1000/foo/abstract`` between "DOI
    literal" and "DOI + view=abstract". The caller must use the
    ``view=`` kwarg alongside a DOI.

    Non-DOI inputs (starting with anything other than ``10.``) are
    returned unchanged — this function is a no-op on slug-form ids.

    Raises :class:`NotFound` when the DOI is well-formed but no live
    paper carries it; the error carries a ``search(kind='paper',
    q='...')`` hint rather than falling through to the generic
    ``"illegal character"`` message the slug regex would emit.
    """
    if not raw.startswith("10."):
        return raw
    m = _DOI_RE.match(raw)
    if m is None:
        # Looks like a DOI prefix but doesn't match the full shape —
        # let the slug parser emit its usual error.
        return raw
    doi, selector = m.group(1), (m.group(2) or "")
    slug = store.find_paper_slug_by_doi(doi)
    if slug is None:
        raise NotFound(
            f"paper with DOI {doi!r} not ingested",
            next=(
                f"put(kind='finding', title='<short claim>', body='<...>', "
                f"cited_in='doi:{doi}', "
                "scope={'electrode': 'Cu', 'ambient': 'N2'})  "
                "to register the DOI as a chase target; the fetcher "
                "(Unpaywall/arXiv/S2) will try to pull the PDF next "
                "pass. Alternatively: search(kind='paper', q='<title>') "
                "for an existing slug. Legacy: append to "
                f"./request_doi.md (deprecated)."
            ),
        )
    return slug + selector


def _parse_paper_id(
    raw: str,
) -> tuple[str, tuple[int, int] | None, str | None]:
    """Return (slug, chunk_range, view).

    ``slug`` is mandatory. Both ``chunk_range`` and ``view`` may be set
    when the id carries both — e.g. ``slug~46..105/toc`` is the
    drill-down form (TOC scoped to that block range). For plain chunk
    selectors (``slug~38``) view is ``None``; for plain view paths
    (``slug/cite/bib``) chunk_range is ``None``.
    """
    # Friendly redirect for the cross-kind list-view shape. Numeric
    # kinds (memory, todo, ...) accept ``id='/recent'`` for their
    # listing, so a 7B caller learning the convention from one kind
    # naturally retries it on paper. The MCP critic flagged the
    # generic "invalid paper id" reply as a footgun — it sent the
    # caller down a slug-fixup detour when the actual fix is "drop
    # the id=, papers list via the bare get". (Critic MINOR #7.)
    if isinstance(raw, str) and raw.startswith("/"):
        raise BadInput(
            f"paper has no list view {raw!r} - list-view paths are "
            "specific to numeric kinds (memory/todo/flashcard/...)",
            next=(
                "papers don't accept '/recent' - use the bare list shape: "
                "get(kind='paper')"
            ),
        )
    m = _SLUG_RE.match(raw)
    if not m:
        raise BadInput(
            f"invalid paper id: {raw!r}",
            next="paper ids look like 'wang2020state' or 'wang2020state~38'",
        )
    slug, rest = m.group(1), m.group(2)
    # The slug regex is permissive at the right edge — `[a-z0-9][a-z0-9-]*`
    # matches the *prefix*, then `(.*)` swallows whatever's left. So
    # `nonexistent_paper_xyz` parses as slug='nonexistent' + rest='_paper_xyz'.
    # The rest then doesn't start with `~` or `/` and falls through to
    # the generic "unparseable" error at the bottom — which doesn't
    # name the actual rule. The MCP critic flagged this: a 7B model
    # using snake_case sees BadInput instead of NotFound and goes down
    # the wrong recovery branch. Catch the underscore case explicitly
    # before the chunk/view logic gets a chance. (Critic MINOR m3.)
    if rest and not rest.startswith(("~", "/")):
        first_bad = rest[0]
        if first_bad == "_":
            raise BadInput(
                f"paper slug contains '_' (illegal): {raw!r}",
                next=(
                    "paper slugs are lowercase a-z + digits + '-' only - "
                    "no underscores. Most slugs look like 'wang2020state'"
                ),
            )
        raise BadInput(
            f"paper slug contains illegal {first_bad!r}: {raw!r}",
            next="paper slugs match [a-z0-9-]+ (e.g. 'wang2020state')",
        )

    if not rest:
        return slug, None, None

    chunk_range: tuple[int, int] | None = None
    if rest.startswith("~"):
        # Split selector from optional view path: ``~46..105/toc``.
        sel_and_path = rest[1:]
        if "/" in sel_and_path:
            sel, _, path_part = sel_and_path.partition("/")
            rest_after_sel = "/" + path_part
        else:
            sel = sel_and_path
            rest_after_sel = ""

        rng = _RANGE_RE.match(sel)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                raise BadInput(
                    f"empty chunk range: {raw!r}",
                    next="ranges run lo..hi inclusive (e.g. '~3..7')",
                )
            chunk_range = (lo, hi)
        else:
            single = _CHUNK_RE.match(sel)
            if single:
                n = int(single.group(1))
                chunk_range = (n, n)
            else:
                raise BadInput(
                    f"unparseable chunk selector after ~: {sel!r}",
                    next="use '~N' for a single block or '~N..M' for a range",
                )

        if not rest_after_sel:
            return slug, chunk_range, None
        rest = rest_after_sel

    if rest.startswith("/"):
        parts = tuple(rest[1:].split("/"))
        view = _VIEW_PATH_ALIASES.get(parts)
        if view is None:
            # Specific hint for the figure family — the MCP critic
            # flagged ``slug/fig/N`` as failing silently with a
            # generic "unknown view" error. Surface the caption-only
            # workflow so the agent stops retrying. (Critic MAJOR #4.)
            if parts and parts[0] in ("fig", "figure", "figures"):
                raise BadInput(
                    f"figure view path {raw!r} not implemented",
                    options=list(_SUPPORTED_VIEWS),
                    next=(
                        "figure binaries aren't served - figures live as "
                        "legend blocks inside the body. Use view='toc' to "
                        "locate the figure number, then read the legend "
                        "block. See 'Figures' in precis-paper-help."
                    ),
                )
            raise BadInput(
                f"unknown view path: {raw!r}",
                options=list(_SUPPORTED_VIEWS),
                next="see precis-paper-help for the supported view paths",
            )
        return slug, chunk_range, view

    raise BadInput(
        f"unparseable paper id: {raw!r}",
        next="format: <slug> | <slug>~N | <slug>~N..M | <slug>/<view> | <slug>~N..M/<view>",
    )


# Author + citation + inline-markup helpers moved to
# ``precis.handlers._paper_format`` (2026-06-05). The symbols
# ``_author_names``, ``_format_authors``, ``_format_citation``,
# ``_clean_inline_text``, ``_latex_escape`` are imported at the top of
# this file; tests previously reaching them via
# ``precis.handlers.paper`` should import from ``_paper_format`` now.


# ---------------------------------------------------------------------------
# View aliasing + abstract sanitisation
# ---------------------------------------------------------------------------


# Kwarg ``view=`` accepts the same vocabulary as the id-path form so that
# ``view='cite/bib'`` and ``id='slug/cite/bib'`` resolve identically.
# Without this, an agent that copied an id like ``slug/cite/bib`` from a
# docstring and then split it into id+view ended up with an Unsupported
# error — the asymmetry the MCP critic flagged.
_VIEW_KWARG_ALIASES: dict[str, str] = {
    "bibtex": "bibtex",
    "ris": "ris",
    "endnote": "endnote",
    "abstract": "abstract",
    "toc": "toc",
    "cite/bib": "bibtex",
    "cite/bibtex": "bibtex",
    "cite/ris": "ris",
    "cite/endnote": "endnote",
}


_VIEW_NOOP_ALIASES: frozenset[str] = frozenset({"text", "body", "full"})


def _normalise_view(view: str | None) -> str | None:
    """Canonicalise the ``view`` argument.

    Accepts both bare names (``'bibtex'``) and slash-paths
    (``'cite/bib'``). Returns the canonical bare name. Unknown views
    pass through verbatim so the renderer can produce its own
    ``Unsupported`` error with the supported-options list.

    ``view='text'``, ``'body'``, ``'full'`` are treated as no-ops —
    they map to ``None``. Workers reach for ``view='text'`` as the
    natural way to ask for chunk bytes (``id='slug~13', view='text'``);
    rather than fight that mental model and emit ``Unsupported``,
    accept it as a synonym for "render the addressed scope using the
    default renderer". With a chunk selector this gives the chunk
    text; without one it gives the paper overview.
    """
    if view is None:
        return None
    if view in _VIEW_NOOP_ALIASES:
        return None
    return _VIEW_KWARG_ALIASES.get(view, view)


# JATS-stripping (``_strip_jats``) and figure-and-caption coalescing
# (``_is_image_only_block`` / ``_looks_like_caption`` /
# ``_render_block_body`` / ``_chunk_keywords_or_caption`` /
# ``_scrub_block_text``) moved to ``precis.handlers._paper_text``
# (2026-06-05). Imported at the top of this file; tests previously
# reaching them via ``precis.handlers.paper`` should import from
# ``_paper_text`` now.


__all__ = ["PaperHandler"]
