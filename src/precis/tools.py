"""MCP tool implementations: read() and put().

These are the two entry points for all document operations. They parse the
URI, resolve the handler, and dispatch.
"""

from __future__ import annotations

from precis.protocol import PrecisError
from precis.registry import resolve
from precis.uri import parse


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
        uri: ``scheme:path[#selector][/view]``
            Schemes: ``file:`` (on disk), ``paper:`` (library).
            ``file:`` extension determines format (.docx, .tex, …).
        query: Filter or search within the addressed scope.
            Papers: semantic search. Files: grep (or semantic if configured).
        summarize: Show derived summaries (~) instead of full text (=).
            Only affects multi-node output. Single-node always returns full text.
        depth: Heading-level filter. 0 = everything (default), 1 = H1 only,
            2 = H1+H2, 3 = H1-H3, 4 = all headings no content.
        page: Result page (1-indexed) for paginated output.

    Output markers::

        =  verbatim (safe to quote)
        ~  derived (keywords/summary — not quotable)
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
    **kwargs,
) -> str:
    """Write to or annotate a document.

    Args:
        uri: ``scheme:path[#selector]`` — target document and node.
        text: Content to write. For ``mode='move'``, this is the target slug.
        mode: One of: ``replace``, ``after``, ``before``, ``delete``,
            ``append``, ``move``, ``note``.
        tracked: DOCX: write as track-changes (default true). Ignored by
            other handlers.
        **kwargs: Extra args passed to handler (e.g. ``title``, ``tags``
            for paper notes).

    ``note`` mode works on all document types:
        - DOCX → Word margin comment
        - paper → DB note in acatome-store
        - read-only types → the one writable operation
    """
    try:
        parsed = parse(uri)
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
