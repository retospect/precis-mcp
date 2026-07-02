"""OracleHandler — saved long-form prompts / authoritative reference nodes.

Slug-addressed, durable. Each `oracle` is a curated collection of
wisdom entries (proverbs, Stoic principles, engineering rules of
thumb, …). Entries are blocks within the oracle ref; the slug picks
the tradition, a block selector picks the entry.

Addressing modes:

- ``get(kind='oracle')``                → list traditions
- ``get(kind='oracle', id='stoic')``    → **one random entry**
- ``get(kind='oracle', id='stoic~3')``  → entry at position 3
- ``get(kind='oracle', id='stoic/index')`` → numbered entry catalog
- ``get(kind='oracle', id='stoic', view='index')`` → same as above

The random default matches oracle semantics ("consult the oracle"
returns one piece of wisdom, not all of it) and keeps the per-call
token footprint bounded (~50–200 tokens) instead of dumping all
14–64 entries verbatim (MCP critic MAJOR-$: oracle:stoic was
~1750 tokens per call, oracle:engineering ~2355 tokens).
"""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    require_link_target,
    require_tag_ops,
    validate_link_mode,
)
from precis.handlers._slug_ref_shared import (
    render_slug_ref_list,
    resolve_live_slug_ref,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Block, Ref
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query, query_vec_for
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
)
from precis.utils.text import excerpt as _excerpt

# Block-selector views accepted on ``id=<slug>/<view>`` or
# ``view=<view>``. ``index`` is the escape hatch for callers who want
# the full catalog before picking an entry.
_ORACLE_VIEWS: tuple[str, ...] = ("index",)


class OracleHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="oracle",
        title="Oracle",
        description=(
            "Authoritative reference node - slug-addressed, curated "
            "prompt or rubric. Read-only body; use tag / link to "
            "cross-link to papers, memory, etc."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-9 / seven-verb cutover: oracle bodies are curated —
        # set externally via the corpus seeding pipeline, never
        # written from the agent surface. Cross-linking and tag
        # classification ride on the dedicated tag/link verbs;
        # ``put`` is therefore not exposed.
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        role="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("oracle: store required")
        self.store = hub.store
        # Optional — search degrades to lexical-only when the embedder
        # is absent or failing (see ``embed_query`` / ``query_vec_for``).
        self.embedder = hub.embedder

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        # No id → list oracles. Path-form lists (``id='/'``) land here
        # too for symmetry with other slug kinds.
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_list()

        slug, selector, path_view = _parse_oracle_id(str(id))

        # view= and path-form views are equivalent. Reject the
        # collision so the caller isn't confused if the two disagree.
        if view is not None and path_view is not None and view != path_view:
            raise BadInput(
                f"id= path view {path_view!r} conflicts with view={view!r}",
                next=f"pick one: get(kind='oracle', id={slug!r}, view={view!r})",
            )
        effective_view = view or path_view

        ref = resolve_live_slug_ref(self.store, kind="oracle", id=slug)
        handle = handle_registry.format_handle("oracle", ref.id)
        blocks = self.store.list_blocks_for_ref(ref.id)

        # Empty oracle — no blocks, body lives in the title only.
        if not blocks:
            return Response(
                body=f"# oracle {handle}\n_{ref.title}_\n\n(empty tradition)"
            )

        # Explicit view takes precedence over the selector.
        if effective_view is not None:
            if effective_view not in _ORACLE_VIEWS:
                raise BadInput(
                    f"unknown oracle view {effective_view!r}",
                    options=list(_ORACLE_VIEWS),
                    next=(f"get(id={handle!r}, view='index') to list entry handles"),
                )
            return self._render_index(ref, blocks)

        # Selector-based addressing: ``slug~N`` → entry at pos N.
        if selector is not None:
            try:
                pos = int(selector)
            except ValueError:
                raise BadInput(
                    f"oracle selector must be an integer entry position, "
                    f"got {selector!r}",
                    next=(
                        f"get(id={handle!r}, view='index') "
                        "to see available entry positions"
                    ),
                ) from None
            return self._render_entry(ref, blocks, pos)

        # Default (no selector, no view): single-block oracles render
        # verbatim; multi-block oracles return one random entry with
        # hints toward the deterministic paths.
        if len(blocks) == 1:
            body = blocks[0].text
            return Response(body=f"# oracle {handle}\n_{ref.title}_\n\n{body}")
        return self._render_random_entry(ref, blocks)

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        page_size: int = 10,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Content search over oracle **entry bodies**, not just titles.

        Oracle wisdom (hexagrams, Stoic quotes, proverbs) lives in the
        per-entry blocks, so this routes through the shared block search
        (``store.search_blocks``): hybrid lexical+semantic by default,
        degrading to lexical-only when the embedder is absent or failing
        (``query_vec_for`` → ``None``). Each hit is rendered as a
        deterministic ``oracle <slug>~<pos>`` entry the caller can fetch
        with ``get(kind='oracle', id='<slug>~<pos>')``.

        ``scope=<tradition-slug>`` narrows the search to a single
        tradition's entries.
        """
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='oracle', q='your query')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="oracle",
                id=scope,
                next_hint="get(kind='oracle') to list traditions",
            )
            scope_ref_id = scope_ref.id

        query_vec = query_vec_for(self.embedder, q, mode)
        hits = self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="oracle",
            scope_ref_id=scope_ref_id,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            body = f"no oracle entries match {q!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='oracle', q={q!r}, page_size=50)",
                        "widen the net",
                    ),
                    (
                        "get(kind='oracle')",
                        "list traditions to browse instead",
                    ),
                ]
            )
            return Response(body=body)

        total = self.store.count_blocks_lexical(
            q=q, kind="oracle", scope_ref_id=scope_ref_id
        )
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="oracle entry",
                query=q,
            )
        ]
        for block, ref, score in hits:
            slug = ref.slug or "???"
            handle = (
                handle_registry.try_format(ref.kind, block.id, chunk=True)
                or f"{slug}~{block.pos}"
            )
            title = _entry_title(block) or f"entry {block.pos}"
            lines.append(f"\n## oracle {handle}  (score={score:.4f})")
            lines.append(f"_{ref.title} - {title}_")
            lines.append(_excerpt(block.text))
        return Response(body="\n".join(lines))

    # ── search_hits: structured form for cross-kind merge ───────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level content search returned as ``SearchHit``s.

        Oracle bodies live in per-entry blocks; this searches that body
        text (hybrid lexical+semantic, degrading to lexical when the
        embedder is down) so cross-kind merge matches entry *content*,
        not just the tradition title. ``query_vec=`` may be pre-supplied
        by the runtime cross-kind dispatcher (embedded once for all
        kinds).
        """
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        triples = self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="oracle",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind="oracle")

    # ── seven-verb surface ─────────────────────────────────────────

    def _resolve_oracle_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair."""
        ref = resolve_live_slug_ref(
            self.store,
            kind="oracle",
            id=id,
            next_hint="search(kind='oracle', q='...') to find existing slugs",
        )
        return ref.slug or "", ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove oracle tags. Open-tag only (no closed prefixes)."""
        require_tag_ops("oracle", add, remove)
        slug, ref_id = self._resolve_oracle_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "oracle", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="oracle",
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
        """Add or remove a link from this oracle to another ref."""
        target = require_link_target("oracle", target)
        validate_link_mode(mode)
        slug, ref_id = self._resolve_oracle_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="oracle",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    def _render_list(self) -> Response:
        # Empty-list responses on read-only kinds still teach the
        # agent the next call shape — see-also the help skill
        # rather than just returning a bare sentence. (MCP critic
        # MINOR m2.)
        return render_slug_ref_list(
            self.store,
            kind="oracle",
            label_plural="oracle(s)",
            empty_body="no oracles defined yet",
            empty_next=[
                (
                    "get(kind='skill', id='precis-overview')",
                    "learn about the kind list",
                ),
            ],
            populated_next=[
                (
                    "get(kind='oracle', id='<slug>')",
                    "consult one tradition (random pick)",
                ),
                (
                    "get(kind='oracle', id='<slug>/index')",
                    "see all entries in a tradition",
                ),
                (
                    "search(kind='oracle', q='your query')",
                    "search across all traditions",
                ),
            ],
        )

    # ── per-entry rendering ─────────────────────────────────────────

    def _render_random_entry(self, ref: Ref, blocks: list[Block]) -> Response:
        """Pick one entry at random; render it with catalog hints.

        Uses ``secrets.randbelow`` (CSPRNG) for unbiased selection —
        ``random.randrange`` would be equally correct but ``secrets``
        reads slightly more intentionally for an "oracle consult"
        semantic. Callers that need determinism use ``~N``.
        """
        idx = secrets.randbelow(len(blocks))
        block = blocks[idx]
        slug = ref.slug or "???"
        handle = handle_registry.format_handle("oracle", ref.id)
        title = _entry_title(block) or f"entry {block.pos}"
        body = f"# oracle {slug}~{block.pos}\n_{ref.title} - {title}_\n\n{block.text}"
        body += render_next_section(
            [
                (
                    f"get(id={handle!r})",
                    "consult again (random pick)",
                ),
                (
                    f"get(id='{handle}/index')",
                    f"see all {len(blocks)} entries",
                ),
                (
                    f"get(kind='oracle', id='{slug}~{block.pos}')",
                    "fetch THIS entry deterministically",
                ),
            ]
        )
        return Response(body=body)

    def _render_entry(self, ref: Ref, blocks: list[Block], pos: int) -> Response:
        """Render the entry at ``pos`` (deterministic).

        Block positions are **1-indexed** for the ``oracle`` kind
        (see ``ingest_oracles.py``) so I-Ching ``iching~49`` maps
        to Hexagram 49 verbatim. The valid-range hint is derived
        from the actual min/max ``pos`` rather than hard-coded so
        any future tradition with a sparse or offset numbering
        scheme keeps an honest error message.
        """
        block = next((b for b in blocks if b.pos == pos), None)
        slug = ref.slug or "???"
        handle = handle_registry.format_handle("oracle", ref.id)
        if block is None:
            lo = min(b.pos for b in blocks)
            hi = max(b.pos for b in blocks)
            range_hint = f"{lo}..{hi}" if lo != hi else f"{lo}"
            raise NotFound(
                f"oracle {slug!r} has no entry at position {pos} "
                f"(valid range: {range_hint})",
                next=(f"get(id='{handle}/index') to list entry positions"),
            )
        title = _entry_title(block) or f"entry {pos}"
        body = f"# oracle {slug}~{pos}\n_{ref.title} - {title}_\n\n{block.text}"
        # Prev/next affordances are cheap and obvious.
        nav: list[tuple[str, str]] = []
        if pos > 0 and any(b.pos == pos - 1 for b in blocks):
            nav.append(
                (
                    f"get(kind='oracle', id='{slug}~{pos - 1}')",
                    "previous entry",
                )
            )
        if any(b.pos == pos + 1 for b in blocks):
            nav.append(
                (
                    f"get(kind='oracle', id='{slug}~{pos + 1}')",
                    "next entry",
                )
            )
        nav.append(
            (
                f"get(id={handle!r})",
                "another random entry",
            )
        )
        nav.append(
            (
                f"get(id='{handle}/index')",
                "full entry catalog",
            )
        )
        body += render_next_section(nav)
        return Response(body=body)

    def _render_index(self, ref: Ref, blocks: list[Block]) -> Response:
        """Numbered catalog of every entry — title + first-line preview.

        This is the critic's preferred "always-bounded" shape. Rough
        budget: ~8–15 tokens per entry × up to 64 entries (iching) ~=
        1000 tokens worst case, well below the 2355 the old dump-all
        default produced.
        """
        slug = ref.slug or "???"
        handle = handle_registry.format_handle("oracle", ref.id)
        lines = [
            f"# oracle {handle}/index",
            f"_{ref.title}_",
            f"\n{len(blocks)} entries:",
        ]
        for block in blocks:
            title = _entry_title(block) or f"(entry {block.pos})"
            preview = _first_line(block.text)
            handle = (
                handle_registry.try_format(ref.kind, block.id, chunk=True)
                or f"{slug}~{block.pos}"
            )
            if preview and preview != title:
                lines.append(f"- **{block.pos}. {title}** - {preview}  `{handle}`")
            else:
                lines.append(f"- **{block.pos}. {title}**  `{handle}`")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(id={handle!r})",
                    "random entry (default)",
                ),
                (
                    f"get(kind='oracle', id='{slug}~N')",
                    "fetch entry N (1-indexed; matches inherent numbering for I-Ching)",
                ),
            ]
        )
        return Response(body=body)


# ── module-level helpers ────────────────────────────────────────────


def _parse_oracle_id(id_str: str) -> tuple[str, str | None, str | None]:
    """Split ``slug``, ``slug~N``, ``slug/view`` into components.

    Returns ``(slug, selector, view)`` where:

    - ``selector`` is the text after ``~`` (stringly typed; the caller
      validates it as an integer position)
    - ``view`` is the text after ``/`` (must be in ``_ORACLE_VIEWS``)

    ``slug~N/view`` is rejected upstream — entry-level views aren't
    a thing yet; we keep the grammar linear.
    """
    id_str = id_str.strip()
    # Handle selector FIRST — ``slug~N`` can also have no ``/``.
    if "~" in id_str:
        slug, rest = id_str.split("~", 1)
        if "/" in rest:
            # Entry-level view (``slug~N/view``) not supported today.
            # Let the caller raise BadInput with an actionable hint.
            sel, view = rest.split("/", 1)
            return slug.strip(), sel.strip(), view.strip()
        return slug.strip(), rest.strip(), None
    if "/" in id_str:
        slug, view = id_str.split("/", 1)
        return slug.strip(), None, view.strip()
    return id_str, None, None


def _entry_title(block: Block) -> str | None:
    """Extract a human-readable title for an oracle entry.

    Oracle ingest (see ``jobs/ingest_oracles.py``) stores the entry
    title as the first element of ``meta['section_path']``. Falls
    back to ``None`` if the block was added through some other path.
    """
    meta = block.meta or {}
    path = meta.get("section_path") or []
    if isinstance(path, list) and path:
        first = path[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _first_line(text: str, *, max_chars: int = 80) -> str:
    """Return the first non-empty line, clipped to ``max_chars``."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            if len(s) > max_chars:
                return s[: max_chars - 1].rstrip() + "…"
            return s
    return ""
