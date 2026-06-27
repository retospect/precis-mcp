"""OrcidHandler — first-class author identity nodes (ADR 0039).

``kind='orcid'`` stores a researcher as a durable, refreshable ref keyed
on their ORCID iD (slug ``orcid:0000-0002-1825-0097``). Unlike the
cache-backed live kinds (``web`` / ``youtube`` / ``semanticscholar``)
the node is a **link hub** — its ``authored`` edges must survive cache
eviction — so it is a paper-like durable ref, not a
:class:`~precis.handlers._cache_base.CacheBackedHandler`.

Verbs:

* ``get(id='0000-…')`` — resolve via the ORCID Public API, store /
  refresh the record (names, bio, keywords, employments with ROR ids),
  embed a ``card_combined`` chunk, **link** any works already held in the
  corpus, and report the missing-DOI diff counts. Fetching the missing
  ones is **LLM-gated**, not automatic: pass ``args={'enqueue': N}`` (or
  ``'all'``) to mint that many fetch stubs (ADR 0039 §4).
* ``search(q=…)`` — semantic search over the embedded author cards.
* ``link`` / ``tag`` — attach ``authored`` / ``authored-by`` edges and
  classification tags.

Refresh is **on-demand**: a stale node (older than the soft TTL) renders
with a hint; the model re-pulls with ``args={'refresh': true}``. There is
no background refresh pass.

Auth degrades gracefully: missing ``ORCID_CLIENT_ID`` /
``ORCID_CLIENT_SECRET`` raises :class:`InitError` at boot, which
:func:`precis.dispatch._try` catches — the kind drops off the surface
rather than blocking the server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
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
from precis.ingest import orcid as orcid_api
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.store.types import BlockInsert
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query

# Soft staleness TTL. A node older than this still renders (cached), but
# carries a "may be stale — refresh" hint; the model decides whether to
# re-pull (``args={'refresh': true}``). Refresh is on-demand only — there
# is no background pass (ADR 0039, refresh decision).
_STALE_AFTER_DAYS = 30


def _oi(ref_id: int) -> str:
    """ADR 0036 ORCID-node handle (e.g. ``oi12``)."""
    return handle_registry.format_handle("orcid", ref_id)


def _enqueue_limit(enqueue: int | str, works: list[dict[str, Any]]) -> int:
    """Translate the ``enqueue=`` arg into a stub-mint ceiling.

    ``'all'`` → every missing work (bounded by the works count, the upper
    bound on stubs); an int → that many; anything else / negative → 0.
    """
    if isinstance(enqueue, str):
        return len(works) if enqueue.strip().lower() == "all" else 0
    try:
        return max(0, int(enqueue))
    except (TypeError, ValueError):
        return 0


def _link_authored(
    store: Store, orcid_ref_id: int, paper_ref_id: int, work: dict[str, Any]
) -> None:
    """Ref-level ``authored`` edge (orcid → paper). Author position is
    unknown from the works feed, so meta records only provenance + url;
    the senior-author heuristic is filled by the S2 authors: path."""
    if orcid_ref_id == paper_ref_id:
        return
    meta: dict[str, Any] = {"set_by": "orcid"}
    if work.get("url"):
        meta["url"] = work["url"]
    store.add_link(
        src_ref_id=orcid_ref_id,
        dst_ref_id=paper_ref_id,
        relation="authored",
        set_by="system",
        meta=meta,
    )


def enqueue_authored_works(
    store: Store,
    orcid_ref_id: int,
    works: list[dict[str, Any]],
    *,
    limit: int = 0,
) -> dict[str, int]:
    """The missing-DOI diff (ADR 0039 §4): link held papers; LLM-gated stubs.

    Always: for each work with a usable identifier (DOI preferred, arXiv
    fallback), resolve against the corpus and on a hit ensure an
    ``authored`` link. Linking held papers is free and unconditional.

    Stub minting is **gated** by ``limit`` — the number the *caller*
    (ultimately the LLM, via ``get(..., args={'enqueue': N})``) chose to
    fetch. ``limit=0`` (the resolve-time default) mints nothing and just
    reports the counts, so a resolve never silently floods the fetch
    queue. Each minted stub is idempotent (``set_by='orcid'``) and
    auto-claimed by ``fetch_oa``.

    Returns ``{linked, stubbed, missing_with_id, missing_no_id,
    remaining}`` where ``remaining = missing_with_id - stubbed`` (what an
    agent could still enqueue).
    """
    linked = stubbed = missing_with_id = missing_no_id = 0
    for work in works:
        ident = work.get("doi") or work.get("arxiv")
        if not ident:
            missing_no_id += 1
            continue
        existing = store.find_paper_ref_by_identifier(ident)
        if existing is not None:
            _link_authored(store, orcid_ref_id, existing, work)
            linked += 1
            continue
        missing_with_id += 1
        if stubbed >= limit:
            continue
        identifiers: list[tuple[str, str]] = []
        if work.get("doi"):
            identifiers.append(("doi", work["doi"]))
        if work.get("arxiv"):
            identifiers.append(("arxiv", work["arxiv"]))
        ref_id, _created = store.upsert_stub_paper(
            identifiers=identifiers,
            title=work.get("title"),
            year=work.get("year"),
            set_by="orcid",
        )
        _link_authored(store, orcid_ref_id, ref_id, work)
        stubbed += 1
    return {
        "linked": linked,
        "stubbed": stubbed,
        "missing_with_id": missing_with_id,
        "missing_no_id": missing_no_id,
        "remaining": missing_with_id - stubbed,
    }


def _card_text(record: dict[str, Any]) -> str:
    """Build the embedded ``card_combined`` text: name + bio + keywords +
    affiliations. This is what ``search(kind='orcid', q=…)`` matches."""
    parts: list[str] = []
    if record.get("name"):
        parts.append(record["name"])
    if record.get("biography"):
        parts.append(record["biography"])
    if record.get("keywords"):
        parts.append("; ".join(record["keywords"]))
    affils = [
        e["organization"]
        for e in record.get("employments", [])
        if e.get("organization")
    ]
    if affils:
        parts.append("; ".join(dict.fromkeys(affils)))  # dedup, keep order
    return "\n\n".join(parts).strip() or record.get("orcid_id", "[orcid]")


class OrcidHandler(Handler):
    """Slug-addressed ORCID author node (resolve + store + link hub)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="orcid",
        title="ORCID author",
        description=(
            "Researcher identity resolved from ORCID. get(kind='orcid', "
            "id='0000-0002-1825-0097') resolves + stores the record (names, "
            "bio, keywords, employments with ROR ids), links works already "
            "in the corpus, and reports how many are missing; fetching them "
            "is LLM-gated — get(..., args={'enqueue': N}) (or 'all') mints "
            "that many stubs. search(kind='orcid', q=...) runs over the "
            "embedded author card; link attaches authored / authored-by "
            "edges to papers. Durable link hub — never cache-evicted. See "
            "``precis-orcid-help``."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        requires_env=("ORCID_CLIENT_ID", "ORCID_CLIENT_SECRET"),
    )

    provider: ClassVar[str] = "orcid"

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("orcid: store required")
        # Client-credentials are mandatory — the Public API is not open.
        # Missing creds ⇒ disable the kind (InitError is caught by the
        # boot gate), never block the rest of the surface.
        if not orcid_api.has_credentials():
            raise InitError("orcid: ORCID_CLIENT_ID / ORCID_CLIENT_SECRET not set")
        self.store: Store = hub.store
        self.embedder = hub.embedder

    # -- get -----------------------------------------------------------------

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        refresh: bool = False,
        mode: str | None = None,
        enqueue: int | str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        # Bare get / q-only → search or listing, mirroring paper/oracle.
        if id is None and q is not None and q.strip():
            return self.search(q=q)
        if id is None:
            return render_slug_ref_list(
                self.store,
                kind="orcid",
                label_plural="ORCID author(s)",
                empty_next=[
                    (
                        "get(kind='orcid', id='0000-0002-1825-0097')",
                        "resolve an author by iD",
                    )
                ],
            )

        force_refresh = bool(refresh) or mode == "refresh"
        orcid_id = orcid_api.normalize_orcid_id(str(id))
        slug = orcid_api.slug_for(orcid_id)
        existing = self.store.get_ref(kind="orcid", id=slug)

        # Resolve upstream only on a miss or an explicit refresh. A stale
        # node is *not* auto-refreshed — it renders cached with a hint and
        # the model decides (refresh decision, ADR 0039).
        works: list[dict[str, Any]] | None = None
        if existing is None or force_refresh:
            record = orcid_api.fetch_record(orcid_id)
            ref_id = self._store_record(
                record, existing_ref_id=existing.id if existing else None
            )
            works = record["works"]
        else:
            ref_id = existing.id

        # The works diff: link held papers (always), mint stubs only up to
        # the LLM-chosen ``enqueue`` count.
        summary: dict[str, int] | None = None
        if enqueue is not None:
            if works is None:  # cached node — re-pull just /works (cheap leg)
                works = orcid_api.fetch_works_only(orcid_id)
            summary = enqueue_authored_works(
                self.store, ref_id, works, limit=_enqueue_limit(enqueue, works)
            )
            self._persist_diff(ref_id, summary)
        elif works is not None:  # fresh resolve — link held + count, no stubs
            summary = enqueue_authored_works(self.store, ref_id, works, limit=0)
            self._persist_diff(ref_id, summary)

        self._apply_tag_ops_if_any(ref_id, tags, untags)
        ref = self.store.get_ref(kind="orcid", id=slug)
        assert ref is not None
        stale = existing is not None and not force_refresh and not self._is_fresh(ref)
        return self._render(ref, summary=summary, stale=stale)

    # -- search --------------------------------------------------------------

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='orcid', q='spintronics PI')",
            )
        hits = self._card_hits(q, limit=page_size, mode=mode)
        if not hits:
            return Response(body=f"no ORCID authors match {q!r}")
        lines = [f"# {len(hits)} ORCID author(s) for {q!r}"]
        for _block, ref, score in hits:
            name = (ref.title or ref.slug or "?").split("\n", 1)[0]
            lines.append(f"\n## {_oi(ref.id)}  {ref.slug}  (score={score:.2f})\n{name}")
        return Response(body="\n".join(lines))

    def search_hits(self, *, q: str | None = None, page_size: int = 10, **_kw: Any):  # type: ignore[override]
        """Cross-kind merge entry point (``kind='*'`` fan-out)."""
        from precis.utils.search_merge import block_hits_to_search_hits

        if not (q and q.strip()):
            return []
        hits = self._card_hits(q, limit=page_size, mode=None)
        return block_hits_to_search_hits(hits, kind="orcid")

    def _card_hits(self, q: str, *, limit: int, mode: str | None):
        query_vec = embed_query(self.embedder, q)
        return self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="orcid",
            limit=limit,
            card_kinds=("card_combined",),
        )

    # -- tag / link ----------------------------------------------------------

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        require_tag_ops("orcid", add, remove)
        ref = self._resolve_ref(id)
        n_add, n_rem = apply_tag_ops(
            self.store, "orcid", ref.id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="orcid",
                ref_label=_oi(ref.id),
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_add,
                n_tags_removed=n_rem,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        target = require_link_target("orcid", target)
        validate_link_mode(mode)
        ref = self._resolve_ref(id)
        n_add, n_rem = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="orcid",
                ref_label=_oi(ref.id),
                n_links_added=n_add,
                n_links_removed=n_rem,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # -- internals -----------------------------------------------------------

    def _resolve_ref(self, id: str | int | None):
        if id is None:
            raise BadInput(
                "orcid id required",
                next="tag(kind='orcid', id='0000-0002-1825-0097', add=['topic-...'])",
            )
        raw = str(id).strip()
        reject_chunk_or_path_view(kind="orcid", slug=raw, sel=None, path_view=None)
        # Accept the universal handle (``oi12``) directly; else normalise an
        # iD to the slug form so ``orcid:`` / bare / URL all resolve.
        if handle_registry.parse(raw) is None:
            raw = orcid_api.slug_for(orcid_api.normalize_orcid_id(raw))
        return resolve_live_slug_ref(
            self.store,
            kind="orcid",
            id=raw,
            next_hint="get(kind='orcid', id='0000-...') to resolve the author first",
        )

    def _is_fresh(self, ref) -> bool:  # type: ignore[no-untyped-def]
        fetched = (ref.meta or {}).get("fetched_at")
        if not fetched:
            return False
        try:
            ts = datetime.fromisoformat(fetched)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - ts).total_seconds() / 86400
        return age_days < _STALE_AFTER_DAYS

    def _store_record(
        self, record: dict[str, Any], *, existing_ref_id: int | None
    ) -> int:
        """Insert or refresh the durable author ref + its embedded card.

        Per the append-only rule, a refresh DELETE+INSERTs the card chunk
        (``replace=True``); there are no body rows to disturb.
        """
        slug = orcid_api.slug_for(record["orcid_id"])
        title = record.get("name") or slug
        meta = {
            "orcid_id": record["orcid_id"],
            "given": record.get("given", ""),
            "family": record.get("family", ""),
            "credit_name": record.get("credit_name", ""),
            "biography": record.get("biography", ""),
            "keywords": record.get("keywords", []),
            "researcher_urls": record.get("researcher_urls", []),
            "country": record.get("country", ""),
            "employments": record.get("employments", []),
            "work_count": record.get("work_count", 0),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        card = _card_text(record)
        embedding = None
        if self.embedder is not None:
            try:
                embedding = self.embedder.embed_one(card)
            except Exception:
                embedding = None  # embed worker re-claims on a NULL vector
        card_block = BlockInsert(
            pos=-1,
            text=card,
            embedding=embedding,
            meta={"chunk_kind": "card_combined"},
        )

        with self.store.tx() as conn:
            if existing_ref_id is None:
                ref = self.store.insert_ref(
                    kind="orcid",
                    slug=slug,
                    title=title,
                    provider=self.provider,
                    meta=meta,
                    conn=conn,
                )
                ref_id = ref.id
            else:
                ref_id = existing_ref_id
                self.store.update_ref(ref_id, title=title, meta_patch=meta, conn=conn)
            self.store.insert_blocks(ref_id, [card_block], replace=True, conn=conn)
        return ref_id

    def _apply_tag_ops_if_any(
        self, ref_id: int, tags: list[str] | None, untags: list[str] | None
    ) -> None:
        if tags or untags:
            apply_tag_ops(self.store, "orcid", ref_id, tags=tags, untags=untags)

    def _persist_diff(self, ref_id: int, summary: dict[str, int]) -> None:
        """Cache the latest works-diff counts so a *cached* get can report
        them (and the enqueue affordance) without re-pulling /works."""
        self.store.update_ref(
            ref_id,
            meta_patch={
                "work_diff": {
                    "linked": summary["linked"],
                    "missing_with_id": summary["missing_with_id"],
                    "missing_no_id": summary["missing_no_id"],
                    "at": datetime.now(UTC).isoformat(),
                }
            },
        )

    def _render(  # type: ignore[no-untyped-def]
        self, ref, *, summary: dict[str, int] | None, stale: bool
    ) -> Response:
        meta = ref.meta or {}
        oid = meta.get("orcid_id", "?")
        lines = [f"# {ref.title}  ({_oi(ref.id)})", "", f"- ORCID: {oid}"]
        if meta.get("country"):
            lines.append(f"- Country: {meta['country']}")
        if meta.get("keywords"):
            lines.append(f"- Keywords: {', '.join(meta['keywords'][:12])}")
        emps = meta.get("employments") or []
        if emps:
            lines.append("- Affiliations:")
            for e in emps[:6]:
                org = e.get("organization") or "?"
                ror = f" [{e['ror']}]" if e.get("ror") else ""
                role = f" — {e['role']}" if e.get("role") else ""
                lines.append(f"    {org}{ror}{role}")
        if meta.get("biography"):
            bio = meta["biography"]
            lines.append("")
            lines.append(bio[:600] + ("…" if len(bio) > 600 else ""))
        lines.append("")
        lines.append(f"- Works on record: {meta.get('work_count', 0)}")

        # Works diff + the LLM-gated enqueue affordance. Prefer the fresh
        # summary; fall back to the cached counts from the last diff.
        diff = summary if summary is not None else meta.get("work_diff")
        if diff is not None:
            linked = diff.get("linked", 0)
            mwid = diff.get("missing_with_id", 0)
            mnoid = diff.get("missing_no_id", 0)
            lines.append(
                f"- In corpus: {linked} linked; missing: {mwid} with a DOI/"
                f"arXiv, {mnoid} with no identifier"
            )
            if summary is not None and summary.get("stubbed"):
                lines.append(
                    f"- Enqueued {summary['stubbed']} fetch stub(s); "
                    f"{summary.get('remaining', 0)} still missing"
                )
            if mwid - (summary.get("stubbed", 0) if summary else 0) > 0:
                lines.append(
                    f"- To fetch the missing ones: "
                    f"get(kind='orcid', id='{oid}', args={{'enqueue': N}}) "
                    f"(or 'all')"
                )
        if stale:
            lines.append(
                f"- ⚠ resolved {meta.get('fetched_at', '?')[:10]} — may be "
                f"stale; refresh with get(kind='orcid', id='{oid}', "
                f"args={{'refresh': true}})"
            )
        lines.append(f"- Source: https://orcid.org/{oid}")
        return Response(body="\n".join(lines))


__all__ = ["OrcidHandler"]
