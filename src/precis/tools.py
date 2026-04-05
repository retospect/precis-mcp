"""MCP tool implementations: read(), put(), and helpers.

These are the entry points for all document operations. They parse the
URI, resolve the handler, enforce write_policy, and dispatch.
"""

from __future__ import annotations

import logging

from precis.protocol import PrecisError
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
            f"Corpus '{plugin.corpus_id}' is read-only (write_policy='ingestion').\n"
            f"Allowed: put(mode='note') to annotate, put(link=...) to link.\n"
            f"Content changes happen at ingestion time only."
        )
    if plugin.write_policy == "system":
        raise PrecisError(
            f"Corpus '{plugin.corpus_id}' is system-managed (write_policy='system').\n"
            f"Only system processes can write to this corpus."
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
        uri: ``scheme:path[›selector][/view]``
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
    try:
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
    except PrecisError as e:
        return e.format()
    except ValueError as e:
        return f"!! ERROR {e}"


def put(
    uri: str,
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
    note: str = "",
    link: str = "",
    **kwargs,
) -> str:
    """Write to or annotate a document.

    Args:
        uri: ``scheme:path[›selector]`` — target document and node.
        text: Content to write. For ``mode='move'``, this is the target slug.
        mode: One of: ``replace``, ``after``, ``before``, ``delete``,
            ``append``, ``move``, ``note``.
        tracked: DOCX: write as track-changes (default true). Ignored by
            other handlers.
        note: Annotation text. Creates a note on the target ref/block.
            Separate from mode='note' — this is a dedicated parameter.
        link: Link spec as ``target_slug:relation`` (e.g. ``jones2023:cites``).
            Creates a link from the addressed ref/block to the target.
        **kwargs: Extra args passed to handler (e.g. ``title``, ``tags``
            for paper notes).

    ``note`` mode works on all document types:
        - DOCX → Word margin comment
        - paper → DB note in acatome-store
        - read-only types → the one writable operation
    """
    try:
        parsed = parse(uri)

        # Handle note= parameter (annotation, always allowed)
        if note:
            return _create_note(
                parsed.scheme, parsed.path, parsed.selector, note, **kwargs
            )

        # Handle link= parameter (link creation, always allowed)
        if link:
            return _create_link(parsed.scheme, parsed.path, parsed.selector, link)

        # Normal write — enforce write_policy
        _check_write_policy(parsed.scheme, mode)

        handler = resolve(parsed.scheme, parsed.path)
        return handler.put(
            path=parsed.path,
            selector=parsed.selector,
            text=text,
            mode=mode,
            tracked=tracked,
            **kwargs,
        )
    except PrecisError as e:
        return e.format()
    except ValueError as e:
        return f"!! ERROR {e}"


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
            f"Ref not found: '{slug}'\nHint: use search(query='...') to find documents."
        )
    ref_id = ref.get("ref_id") or ref.get("id")

    if selector:
        # Block-level note
        try:
            block_idx = int(selector)
        except ValueError:
            raise PrecisError(f"Invalid block index for note: {selector}")
        blocks = store.get_blocks(slug, block_type="text")
        target = [b for b in blocks if b.get("block_index") == block_idx]
        if not target:
            raise PrecisError(f"Block {SEP}{block_idx} not found in {slug}")
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


def _create_link(
    scheme: str,
    path: str,
    selector: str | None,
    link_spec: str,
) -> str:
    """Create a link from the addressed ref/block to a target.

    Link spec format: ``target_slug:relation`` or just ``target_slug``
    (defaults to 'references').

    Links are always allowed regardless of write_policy.
    """
    from precis._store import get_store

    store = get_store()
    src_slug = path

    # Parse link spec: "target_slug:relation" or "target_slug"
    if ":" in link_spec:
        parts = link_spec.rsplit(":", 1)
        dst_slug, relation = parts[0], parts[1]
    else:
        dst_slug = link_spec
        relation = "references"

    src_node_id = None
    if selector:
        try:
            block_idx = int(selector)
        except ValueError:
            raise PrecisError(f"Invalid block index for link: {selector}")
        blocks = store.get_blocks(src_slug, block_type="text")
        target = [b for b in blocks if b.get("block_index") == block_idx]
        if not target:
            raise PrecisError(f"Block {SEP}{block_idx} not found in {src_slug}")
        src_node_id = target[0].get("node_id")

    try:
        link = store.create_link(
            src_slug,
            dst_slug,
            relation,
            src_node_id=src_node_id,
        )
    except ValueError as e:
        raise PrecisError(str(e))

    anchor = f"{SEP}{selector}" if selector else ""
    return (
        f"🔗 Link created: {src_slug}{anchor} —[{relation}]→ {dst_slug}\n"
        f"Next:\n"
        f"  get(id='{src_slug}/links')  — view all links\n"
        f"  get(id='{dst_slug}')        — read target"
    )
