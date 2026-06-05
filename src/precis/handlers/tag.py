"""TagHandler — discover the tag vocabulary in use across the corpus.

Read-only, slug-addressed. Lets the agent enumerate, browse, and
semantically search every tag attached to anything in the store.
Prevents fragmentation: before coining ``topic:carbon-capture``,
``search(kind='tag', q='carbon capture')`` will surface the
existing ``topic:co2-capture``.

Slug grammar mirrors the on-disk canonical form:

* ``id='STATUS:done'``      — closed UPPERCASE axis (any registered
                              prefix from ``_CLOSED_VOCAB``).
* ``id='topic:co2-capture'`` — open lowercase axis.
* ``id='pinned'``           — bare flag.

Verbs:

* ``get(kind='tag')``       — paginated list, most-used first.
* ``get(kind='tag', id=X)`` — metadata: usage count, first/last
                              seen, sample refs.
* ``search(kind='tag', q='...')`` — hybrid lexical + semantic over
                              the tag vocabulary.

Writes happen via the regular ``tag(...)`` verb on whatever kind
owns the ref — TagHandler doesn't expose put/edit/delete/tag/link.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store.types import _CLOSED_VOCAB
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

# Max sample refs shown on the per-tag metadata view. Matches the
# store-side cap in :meth:`Store.tag_metadata` — the handler trims
# again for safety in case the store ever returns more.
_MAX_SAMPLE_REFS = 5

#: Lexical/semantic results returned per page by default. The verb
#: surface across kinds settled on ``page_size`` as the canonical
#: name (renamed from ``top_k``).
_DEFAULT_PAGE_SIZE = 20


class TagHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="tag",
        title="Tag",
        description=(
            "Discover the tag vocabulary in use across the corpus. "
            "get(kind='tag') lists every tag with usage counts; "
            "get(kind='tag', id='topic:co2-capture') shows metadata "
            "and sample refs; search(kind='tag', q='...') finds tags "
            "by name or meaning (hybrid lexical + semantic). Read-only."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=False,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("tag: store required")
        self.store = hub.store
        # Embedder is optional — without it we fall back to
        # lexical-only search and a one-line hint nudging the
        # operator to run the worker.
        self.embedder = hub.embedder

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        page: int = 1,
        page_size: int = 50,
        scope: str | None = None,
        **_kw: Any,
    ) -> Response:
        """List tags (no id) or render metadata for one tag.

        ``scope=`` on the list path restricts to tags used on refs
        of that kind (``scope='paper'`` → just paper tags).
        """
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_list(scope=scope, page=page, page_size=page_size)
        return self._render_metadata(str(id))

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        scope: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Hybrid lexical + semantic search over the tag vocabulary.

        Empty ``q`` with ``scope=`` falls back to a list view scoped
        to one kind — the same surface as ``get(kind='tag', scope=X)``
        but reachable from the search verb so an agent that started
        from "find me tags on papers" doesn't have to switch verbs.

        Lexical matches surface first (exact substring on
        ``namespace:value``). Semantic matches are appended below
        with a divider — the cosine distance is a hint, not the
        relevance signal.
        """
        # Empty-q list-mode delegates to the get-list renderer so the
        # body shape stays uniform.
        if q is None or not q.strip():
            if scope is not None:
                return self._render_list(scope=scope, page=page, page_size=page_size)
            raise BadInput(
                "search(kind='tag') requires q= or scope=",
                next=(
                    "search(kind='tag', q='your query') or "
                    "search(kind='tag', scope='paper') to list tags on papers"
                ),
            )

        lex_hits = self.store.search_tags_lexical(q=q, page=page, page_size=page_size)
        sem_hits: list[tuple[str, str, float]] = []
        if self.embedder is not None:
            try:
                qvec = self.embedder.embed_one(q)
                sem_hits = self.store.search_tags_semantic(
                    query_vector=qvec, page=page, page_size=page_size
                )
            except Exception:
                # Semantic path is best-effort; lexical still serves.
                sem_hits = []

        # Dedupe: anything already in lex_hits drops out of sem_hits.
        lex_keys = {(ns, v) for (ns, v, _c) in lex_hits}
        sem_hits = [(ns, v, d) for (ns, v, d) in sem_hits if (ns, v) not in lex_keys]

        if not lex_hits and not sem_hits:
            empty_next: list[tuple[str, str]] = [
                ("get(kind='tag')", "browse the most-used tags"),
                (
                    "get(kind='skill', id='precis-tags')",
                    "learn the tag axes and rules",
                ),
            ]
            return Response(
                body=f"no tags match {q!r}" + render_next_section(empty_next)
            )

        return self._render_search_body(q=q, lex=lex_hits, sem=sem_hits, page=page)

    # ── rendering ──────────────────────────────────────────────────

    def _render_list(
        self,
        *,
        scope: str | None,
        page: int,
        page_size: int,
    ) -> Response:
        rows = self.store.list_all_tags(kind=scope, page=page, page_size=page_size)
        if not rows:
            scope_hint = f" on kind={scope!r}" if scope else ""
            return Response(
                body=f"no tags in use{scope_hint}"
                + render_next_section(
                    [
                        (
                            "get(kind='skill', id='precis-tags')",
                            "the tag system and axes",
                        ),
                    ]
                )
            )
        table_rows = [
            {
                "tag": _slug_from(namespace, value),
                "count": count,
                "axis": _axis_label(namespace),
            }
            for (namespace, value, count) in rows
        ]
        scope_hint = f" (scope={scope!r})" if scope else ""
        body = f"# {len(rows)} tag{'' if len(rows) == 1 else 's'}{scope_hint}\n"
        body += render_agent_table(table_rows, schema=["tag", "count", "axis"])
        nexts: list[tuple[str, str]] = []
        # Top-of-page drill-down hint.
        top = rows[0]
        top_slug = _slug_from(top[0], top[1])
        nexts.append(
            (
                f"get(kind='tag', id={top_slug!r})",
                "metadata + sample refs for the top tag",
            )
        )
        nexts.append(
            (
                "search(kind='tag', q='your query')",
                "find tags by name or meaning",
            )
        )
        # Pagination hint — there might be more.
        if len(rows) == page_size:
            nexts.append(
                (
                    (
                        f"get(kind='tag', page={page + 1}"
                        + (f", scope={scope!r}" if scope else "")
                        + ")"
                    ),
                    f"next {page_size} tags",
                )
            )
        body += render_next_section(nexts)
        return Response(body=body)

    def _render_metadata(self, slug: str) -> Response:
        namespace, value = _parse_slug(slug)
        # Try both shapes for bare slugs (could be OPEN or FLAG).
        candidates: list[tuple[str, str]]
        if namespace == "":
            candidates = [("OPEN", value), ("FLAG", value)]
        else:
            candidates = [(namespace, value)]

        meta: dict[str, Any] | None = None
        resolved: tuple[str, str] | None = None
        for ns, val in candidates:
            meta = self.store.tag_metadata(namespace=ns, value=val)
            if meta is not None:
                resolved = (ns, val)
                break

        if meta is None or resolved is None:
            raise NotFound(
                f"tag {slug!r} not found",
                next=(f"search(kind='tag', q={slug!r}) to find similar tags"),
            )
        ns_resolved, val_resolved = resolved
        canonical_slug = _slug_from(ns_resolved, val_resolved)
        lines: list[str] = [
            f"# tag {canonical_slug}",
            f"_axis: {_axis_label(ns_resolved)}_",
            "",
            f"- count:      {meta['count']}",
            f"- first seen: {meta['first_seen']}",
            f"- last seen:  {meta['last_seen']}",
        ]
        # Closed-axis tags: surface the allowed-values list from
        # ``_CLOSED_VOCAB`` so the agent sees the sibling values
        # without leaving the metadata view.
        if ns_resolved not in ("OPEN", "FLAG") and ns_resolved in _CLOSED_VOCAB:
            allowed = sorted(_CLOSED_VOCAB[ns_resolved])
            lines.append(f"- sibling values: {allowed}")

        sample_refs = meta.get("sample_refs") or []
        if sample_refs:
            lines.append("")
            lines.append(f"## sample refs ({len(sample_refs)})")
            sample_rows = []
            for kind, ref_slug, ref_id in sample_refs[:_MAX_SAMPLE_REFS]:
                handle = ref_slug if ref_slug else str(ref_id)
                sample_rows.append({"kind": kind, "id": handle})
            lines.append(render_agent_table(sample_rows, schema=["kind", "id"]))
        body = "\n".join(lines)

        nexts: list[tuple[str, str]] = []
        if sample_refs:
            kind0, slug0, id0 = sample_refs[0]
            handle0 = slug0 if slug0 else id0
            nexts.append(
                (
                    f"get(kind={kind0!r}, id={handle0!r})",
                    "open a ref that carries this tag",
                )
            )
        nexts.append(
            (
                f"search(q='', tags=[{canonical_slug!r}])",
                "find every ref carrying this tag",
            )
        )
        nexts.append(
            (
                "get(kind='tag')",
                "browse the tag vocabulary",
            )
        )
        body += render_next_section(nexts)
        return Response(body=body)

    def _render_search_body(
        self,
        *,
        q: str,
        lex: list[tuple[str, str, int]],
        sem: list[tuple[str, str, float]],
        page: int,
    ) -> Response:
        n_total = len(lex) + len(sem)
        head = format_search_headline(
            n_returned=n_total, total=None, noun="tag match", query=q
        )
        body_parts: list[str] = [head]
        if lex:
            lex_rows = [
                {
                    "tag": _slug_from(ns, v),
                    "count": c,
                    "axis": _axis_label(ns),
                }
                for (ns, v, c) in lex
            ]
            body_parts.append(
                render_agent_table(lex_rows, schema=["tag", "count", "axis"])
            )
        if sem:
            if lex:
                body_parts.append("")
                body_parts.append("## related (semantic)")
            sem_rows = [
                {
                    "tag": _slug_from(ns, v),
                    "axis": _axis_label(ns),
                    "distance": f"{d:.3f}",
                }
                for (ns, v, d) in sem
            ]
            body_parts.append(
                render_agent_table(sem_rows, schema=["tag", "axis", "distance"])
            )
        body = "\n".join(body_parts)
        # Drill-down: paste a top hit's slug into a metadata view.
        nexts: list[tuple[str, str]] = []
        if lex:
            top_slug = _slug_from(lex[0][0], lex[0][1])
            nexts.append(
                (
                    f"get(kind='tag', id={top_slug!r})",
                    "metadata for the top hit",
                )
            )
        elif sem:
            top_slug = _slug_from(sem[0][0], sem[0][1])
            nexts.append(
                (
                    f"get(kind='tag', id={top_slug!r})",
                    "metadata for the top hit",
                )
            )
        if self.embedder is None:
            nexts.append(
                (
                    "precis worker --once",
                    "drain the tag-embedding queue to enable semantic search",
                )
            )
        body += render_next_section(nexts)
        return Response(body=body)


# ── module-level helpers ────────────────────────────────────────────


def _parse_slug(slug: str) -> tuple[str, str]:
    """Split ``namespace:value`` (or bare value) into the two parts.

    Returns ``("", slug)`` when there's no colon — the caller has
    to probe both ``OPEN`` and ``FLAG`` namespaces in that case.
    For prefixed slugs, the first colon is the separator (open
    tags carry the lowercase prefix inside ``value``, but our
    handler exposes them as ``topic:co2-capture`` which round-trips
    to ``namespace='OPEN', value='topic:co2-capture'``).
    """
    s = slug.strip()
    if not s:
        raise BadInput(
            "tag id is empty", next="get(kind='tag', id='topic:co2-capture')"
        )
    if ":" not in s:
        return ("", s)
    prefix, _, value = s.partition(":")
    if prefix and prefix.isupper():
        # Closed-vocab axis: namespace = the uppercase prefix.
        return (prefix, value)
    # Lowercase prefix → stored as OPEN with the full string as value.
    return ("OPEN", s)


def _slug_from(namespace: str, value: str) -> str:
    """Inverse of :func:`_parse_slug` — render storage form back to a slug."""
    if namespace in ("OPEN", "FLAG"):
        return value
    return f"{namespace}:{value}"


def _axis_label(namespace: str) -> str:
    """Short human-friendly description of the tag axis."""
    if namespace == "FLAG":
        return "flag"
    if namespace == "OPEN":
        return "open"
    return f"closed:{namespace}"


__all__ = ["TagHandler"]
