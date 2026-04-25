"""MCP tool implementations: read(), put(), and helpers.

These are the entry points for all document operations. They parse the
URI, resolve the handler, enforce write_policy, and dispatch.
"""

from __future__ import annotations

import logging

from precis.protocol import ErrorCode, PrecisError
from precis.registry import get_corpus_plugin, resolve
from precis.uri import SEP, parse

log = logging.getLogger(__name__)

# Actions that modify document content (subject to write_policy)
_CONTENT_ACTIONS = {"replace", "after", "before", "delete", "append", "move"}

# Actions always allowed regardless of write_policy
_ANNOTATION_ACTIONS = {"note", "comment"}


def _check_write_policy(scheme: str, mode: str) -> None:
    """Enforce write_policy for corpus-based operations.

    File-based handlers (file: scheme) are not subject to write_policy.
    Corpus-based handlers check their plugin's declared write_policy.

    Raises:
        PrecisError: If the operation is not allowed by the write_policy.
    """
    if scheme == "file":
        return  # file handlers are always writable
    plugin = (
        get_corpus_plugin(scheme) if scheme != "paper" else get_corpus_plugin("papers")
    )
    if not plugin:
        return  # no plugin found — let the handler decide
    if mode in _ANNOTATION_ACTIONS:
        return  # annotations are always allowed
    if mode in _CONTENT_ACTIONS and plugin.write_policy == "ingestion":
        raise PrecisError(
            ErrorCode.READONLY,
            cause=(
                f"corpus {plugin.corpus_id!r} is ingestion-only "
                "(content changes happen at ingestion time)"
            ),
            next="put(mode='note') to annotate, or put(link=...) to link",
        )
    if plugin.write_policy == "system":
        raise PrecisError(
            ErrorCode.READONLY,
            cause=f"corpus {plugin.corpus_id!r} is system-managed",
            next="only system processes can write to this corpus",
        )


def read(
    uri: str,
    query: str = "",
    summarize: bool = False,
    depth: int = 0,
    page: int = 1,
    **kwargs,
) -> str:
    """Navigate, browse, search, or read any document.

    Args:
        uri: ``scheme:path[~selector][/view]``
            Schemes: ``file:`` (on disk), ``paper:`` (library).
            ``file:`` extension determines format (.docx, .tex, …).
        query: Filter or search within the addressed scope.
            Papers: semantic search. Files: grep (or semantic if configured).
        summarize: Show derived summaries (≈) instead of full text (=).
            Only affects multi-node output. Single-node always returns full text.
        depth: Heading-level filter. 0 = everything (default), 1 = H1 only,
            2 = H1+H2, 3 = H1-H3, 4 = all headings no content.
        page: Result page (1-indexed) for paginated output.

    Output markers::

        =  verbatim (safe to quote)
        ≈  derived (keywords/summary — not quotable)
        %  annotation (user note/comment)
    """
    parsed = parse(uri)
    handler = resolve(parsed.scheme, parsed.path)
    return handler.read(
        path=parsed.path,
        selector=parsed.selector,
        view=parsed.view,
        subview=parsed.subview,
        query=query,
        summarize=summarize,
        depth=depth,
        page=page,
        **kwargs,
    )


def put(
    uri: str,
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
    note: str = "",
    link: str = "",
    unlink: str = "",
    **kwargs,
) -> str:
    """Write to or annotate a document.

    Args:
        uri: ``scheme:path[~selector]`` — target document and node.
        text: Content to write. For ``mode='move'``, this is the target slug.
        mode: One of: ``replace``, ``after``, ``before``, ``delete``,
            ``append``, ``move``, ``note``.
        tracked: DOCX: write as track-changes (default true). Ignored by
            other handlers.
        note: Annotation text. Creates a note on the target ref/block.
            Separate from mode='note' — this is a dedicated parameter.
        link: Link spec as ``target_slug:relation`` (e.g. ``jones2023:cites``).
            Creates a link from the addressed ref/block to the target.
        unlink: Link spec as ``target_slug[:relation]``. Deletes every
            link from the addressed ref/block to the target that matches
            the relation (or all relations when unspecified).  Phase 7
            cross-kind primitive — works on every state-backed kind.
        **kwargs: Extra args passed to handler (e.g. ``title``, ``tags``
            for paper notes).

    ``note`` mode works on all document types:
        - DOCX → Word margin comment
        - paper → DB note in acatome-store
        - read-only types → the one writable operation
    """
    parsed = parse(uri)

    # Handle note= parameter (annotation, always allowed)
    if note:
        return _create_note(parsed.scheme, parsed.path, parsed.selector, note, **kwargs)

    # Handle link= parameter (link creation, always allowed)
    if link:
        return _create_link(parsed.scheme, parsed.path, parsed.selector, link)

    # Handle unlink= parameter (link deletion, always allowed — §9 / Phase 7)
    if unlink:
        return _delete_link(parsed.scheme, parsed.path, parsed.selector, unlink)

    # Normal write — enforce write_policy
    _check_write_policy(parsed.scheme, mode)

    handler = resolve(parsed.scheme, parsed.path)

    # ``tracked`` is a file-handler concept (DOCX track-changes, LaTeX
    # equivalent).  Non-file kinds reject unexpected kwargs via
    # ``extract_kwargs``, so only forward ``tracked`` when the target
    # is a file handler.  See the docstring above: "Ignored by other
    # handlers." — this enforces that contract.
    put_kwargs: dict[str, object] = dict(kwargs)
    if parsed.scheme == "file":
        put_kwargs["tracked"] = tracked

    return handler.put(
        path=parsed.path,
        selector=parsed.selector,
        text=text,
        mode=mode,
        **put_kwargs,
    )


def _create_note(
    scheme: str,
    path: str,
    selector: str | None,
    note_text: str,
    **kwargs,
) -> str:
    """Create a note annotation on a ref or block.

    Notes are always allowed regardless of write_policy.
    """
    from precis._store import get_store

    store = get_store()
    slug = path

    # Resolve slug → ref
    ref = store.get(slug)
    if not ref:
        raise PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause=f"ref {slug!r} not in corpus",
            next="search(query='...') to find refs",
        )
    ref_id = ref.get("ref_id") or ref.get("id")

    if selector:
        # Block-level note
        try:
            block_idx = int(selector)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid block index for note: {selector!r}",
            ) from exc
        blocks = store.get_blocks(slug, block_type="text")
        target = [b for b in blocks if b.get("block_index") == block_idx]
        if not target:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"block {SEP}{block_idx} not found in {slug}",
            )
        block_node_id = target[0].get("node_id")
        note_id = store.add_note(
            note_text,
            ref_id=ref_id,
            block_node_id=block_node_id,
            tags=kwargs.get("tags") or None,
            origin=kwargs.get("origin", "bot"),
        )
        return f"📝 Note #{note_id} on {slug}{SEP}{block_idx}\n{note_text}"
    else:
        # Ref-level note
        note_id = store.add_note(
            note_text,
            ref_id=ref_id,
            title=kwargs.get("title") or None,
            tags=kwargs.get("tags") or None,
            origin=kwargs.get("origin", "bot"),
        )
        return f"📝 Note #{note_id} on {slug}\n{note_text}"


#: Schemes where the stored slug does NOT include the scheme prefix —
#: papers and paper-adjacent id schemes use bare slugs like
#: ``wang2020state``.  Everything else (todo, fc, memory, conv, wiki, …)
#: stores the scheme-prefixed form and expects it back.
_BARE_SLUG_SCHEMES = frozenset(
    {"paper", "doi", "arxiv", "pmid", "pmcid", "isbn", "issn"}
)


def _store_slug_for(scheme: str, path: str) -> str:
    """Reconstruct the full slug used by acatome-store for this URI.

    Paper-family schemes store bare slugs (``wang2020state``).
    Everything else stores ``<scheme>:<slug>`` (e.g. ``todo:fix-bug``,
    ``memory:cluster-db-user``).  :func:`precis.uri.parse` strips the
    scheme prefix into ``parsed.scheme`` + ``parsed.path`` — this
    helper undoes that stripping for the non-paper kinds so the link
    primitive matches the stored slug.
    """
    if scheme in _BARE_SLUG_SCHEMES:
        return path
    # Already includes the prefix? (e.g. caller passed raw URI path).
    if path.startswith(f"{scheme}:"):
        return path
    return f"{scheme}:{path}"


def _parse_link_spec(spec: str, *, default_relation: str = "") -> tuple[str, str]:
    """Parse a link spec into ``(dst_slug, relation)``.

    Convention:

    - ``"dst"``                 → ``(dst, default_relation)``
    - ``"dst:relation"``        → ``(dst, relation)``
    - ``"memory:foo"``          → ``(memory:foo, default_relation)``
      (left side is a registered scheme → the colon belongs to the
       slug, not the relation separator)
    - ``"memory:foo:cites"``    → ``(memory:foo, cites)``

    Heuristic: ``rsplit(":", 1)`` first.  If the left side is exactly a
    registered scheme name (``memory`` / ``conv`` / ``fc`` / ``paper``
    / etc.) with no further colons, the split was wrong — the whole
    spec is a scheme-prefixed slug with no relation.  Otherwise trust
    the split.
    """
    from precis.registry import SCHEMES, _discover

    _discover()  # ensure SCHEMES is populated
    if ":" not in spec:
        return spec, default_relation
    left, right = spec.rsplit(":", 1)
    # If the left side is itself a scheme name (e.g. "memory" for
    # "memory:a"), the colon is the scheme separator, not the relation
    # separator — put it back.
    if left in SCHEMES and ":" not in left:
        return spec, default_relation
    return left, right


def _create_link(
    scheme: str,
    path: str,
    selector: str | None,
    link_spec: str,
) -> str:
    """Create a link from the addressed ref/block to a target.

    Link spec format: ``target_slug:relation`` or just ``target_slug``
    (defaults to 'references').  Cross-kind link specs like
    ``memory:foo:cites`` are handled by :func:`_parse_link_spec`.

    Links are always allowed regardless of write_policy.
    """
    from precis._store import get_store

    store = get_store()
    src_slug = _store_slug_for(scheme, path)

    dst_slug, relation = _parse_link_spec(link_spec, default_relation="references")

    src_node_id = None
    if selector:
        try:
            block_idx = int(selector)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid block index for link: {selector!r}",
            ) from exc
        blocks = store.get_blocks(src_slug, block_type="text")
        target = [b for b in blocks if b.get("block_index") == block_idx]
        if not target:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"block {SEP}{block_idx} not found in {src_slug}",
            )
        src_node_id = target[0].get("node_id")

    try:
        link = store.create_link(
            src_slug,
            dst_slug,
            relation,
            src_node_id=src_node_id,
        )
    except ValueError as e:
        raise PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause=str(e),
        ) from e

    anchor = f"{SEP}{selector}" if selector else ""
    return (
        f"🔗 Link created: {src_slug}{anchor} —[{relation}]→ {dst_slug}\n"
        f"Next:\n"
        f"  get(id='{src_slug}/links')  — view all links\n"
        f"  get(id='{dst_slug}')        — read target"
    )


def _delete_link(
    scheme: str,
    path: str,
    selector: str | None,
    unlink_spec: str,
) -> str:
    """Delete outbound links from the addressed ref/block to a target.

    Phase 7 — §9.  Mirrors ``_create_link``:

    - Spec ``"dst_slug"`` → delete every link ``path → dst_slug`` (any
      relation).
    - Spec ``"dst_slug:relation"`` → delete only links with that
      relation.
    - ``selector`` (when non-empty) narrows the deletion to links
      originating at that block, not the whole ref.

    Always allowed regardless of write_policy — links are cross-cutting
    metadata, not content, and unlink is the natural dual of the
    already-unrestricted link operation.
    """
    from precis._store import get_store

    store = get_store()
    src_slug = _store_slug_for(scheme, path)

    # Parse spec: "dst[:relation]" with scheme-prefixed-slug awareness.
    # Empty default relation means "match any relation" on unlink.
    dst_slug, relation = _parse_link_spec(unlink_spec, default_relation="")

    # Narrow to a specific block when a selector is given.
    src_node_id: str | None = None
    if selector:
        try:
            block_idx = int(selector)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid block index for unlink: {selector!r}",
            ) from exc
        blocks = store.get_blocks(src_slug, block_type="text")
        target = [b for b in blocks if b.get("block_index") == block_idx]
        if not target:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"block {SEP}{block_idx} not found in {src_slug}",
            )
        src_node_id = target[0].get("node_id")

    # Find matching outbound links.  get_links returns a list of dicts;
    # we filter by direction+dst+relation, then delete by id.
    all_out = store.get_links(src_slug, direction="outbound")
    matches = []
    for link in all_out:
        if link.get("dst_slug") != dst_slug:
            continue
        if relation and link.get("relation") != relation:
            continue
        if src_node_id is not None and link.get("src_node_id") != src_node_id:
            continue
        matches.append(link)

    if not matches:
        rel_desc = f" with relation {relation!r}" if relation else ""
        anchor = f"{SEP}{selector}" if selector else ""
        raise PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause=(f"no links found from {src_slug}{anchor} to {dst_slug}{rel_desc}"),
            next=f"get(id='{src_slug}/links') to see existing links",
        )

    removed = 0
    for link in matches:
        link_id = link.get("id")
        if link_id is None:
            continue
        if store.delete_link(link_id):
            removed += 1

    anchor = f"{SEP}{selector}" if selector else ""
    rel_desc = f" [{relation}]" if relation else ""
    plural = "s" if removed != 1 else ""
    return (
        f"🔗 {removed} link{plural} removed: "
        f"{src_slug}{anchor} ✂{rel_desc} {dst_slug}\n"
        f"Next:\n"
        f"  get(id='{src_slug}/links')  — remaining links"
    )
