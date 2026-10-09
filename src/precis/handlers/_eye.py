"""The eye ladder's one door for every kind — ``get(kind=K, id=…, extent=…)``.

``docs/backlog/fisheye-everywhere.md`` in-scope 2: one argument selects
the rung on every kind's ``get``, and every kind either renders its
neighbourhood or raises ``Unsupported`` with the reason in one sentence —
never a silent fall-through to a bare chunk. The renderer
(:func:`precis.utils.eye_render.render_eye`) already generalises by kind
family (tree / document / link kinds, skills); what was missing was the
plumbing from ``(kind, id)`` to the handle it takes, repeated per handler.

This module is that plumbing, once:

- :func:`is_eye_view` — is a ``view=`` value a ladder label (or ``+recall``
  suffixed)? The ladder predates ``extent=`` on the ``view=`` door, so the
  labels keep working there; ``extent=`` is the canonical spelling.
- :func:`eye_handle` — ``(kind, id)`` → the ``render_eye`` handle, resolving
  a numeric id, a slug, a universal handle or a ``slug~ord`` chunk selector
  (the shape the dispatcher rewrites a ``pc<id>`` handle into). Refuses,
  with one sentence each, the kinds that have no graph node to focus: the
  codeless providers (``web``, ``calc``, …), the file-backed kinds
  (``python``, ``md``) and ``tag``; and a whole draft/plan, whose eye is a
  section handle.
- :func:`eye_response` — the render, with the renderer's ``ValueError``
  turned into a ``BadInput`` that names the ladder.

The dispatcher (:meth:`precis.runtime.dispatch.DispatchMixin._invoke_handler`)
routes a ``get`` carrying ``extent=`` — or a ladder label in ``view=`` — to
:meth:`precis.protocol.Handler.eye`, whose default calls
:func:`eye_response`. A kind that needs more overrides ``eye``: ``finding``
prefixes its trust posture, ``measure`` refuses (a row of a run is not a
node), ``draft`` accepts its legacy ``¶`` anchors. The per-handler ``get``
branches that used to call the renderer directly now delegate to ``eye`` as
well, so a direct handler call and the MCP door render the same thing.

``skill`` is the one kind whose eye is not refs-backed: ``sk:<slug>`` renders
straight from the skill corpus (atomic — a file has no corpus position to be
a neighbour of), which the renderer already handles by prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound, Unsupported
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.eye_render import _TREE_KINDS, RECALL_SUFFIX, render_eye
from precis.workers.working_set import Extent

if TYPE_CHECKING:
    from precis.store.store import Store

#: The rung labels, in ladder order (``kwd`` … ``fisheye+2hop``).
EYE_LADDER: tuple[str, ...] = tuple(e.label for e in Extent if e is not Extent.NONE)

#: The ``next=`` line every eye error carries.
EYE_NEXT = f"extent ∈ {'|'.join(EYE_LADDER)}, optionally +recall"

#: Kinds whose rows are not refs: the eye has nothing to focus. One sentence
#: each — the reason, not a shrug.
_NO_NODE_REASON: dict[str, str] = {
    "python": "a python symbol is file-backed (no refs row), so it has no "
    "link neighbourhood to render — get(kind='python', id=…) reads it with "
    "callers and callees",
    "md": "the md index is file-backed (no refs row), so it has no link "
    "neighbourhood — get(kind='markdown', id=…) is the refs-backed file kind",
    "tag": "a tag is a vocabulary row, not a graph node — search(tags=[…]) "
    "lists what carries it",
}


def is_eye_view(view: str | None) -> bool:
    """True when ``view`` names a ladder rung (``fisheye``, ``kwd``, …) or
    carries the ``+recall`` suffix — the values the ``view=`` door forwards
    to the eye."""
    return view is not None and (view in EYE_LADDER or view.endswith(RECALL_SUFFIX))


def _no_node(kind: str) -> Unsupported:
    reason = _NO_NODE_REASON.get(kind)
    if reason is None:
        reason = (
            f"kind={kind!r} is addressed by query or URL, not a stored ref, so "
            "it has no graph node to focus"
        )
    return Unsupported(
        f"no eye on kind={kind!r}: {reason}",
        next="get(kind='skill', id='precis-fisheye-help') lists the kinds with a ladder",
    )


def _chunk_handle_for_ord(store: Store, kind: str, ref_id: int, ord_: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
            (ref_id, ord_),
        ).fetchone()
    if row is None:
        raise NotFound(
            f"{kind} chunk ~{ord_} not found on {handle_registry.format_handle(kind, ref_id)}"
        )
    return handle_registry.format_handle(kind, int(row[0]), chunk=True)


def _resolve_ref_id(store: Store, kind: str, ident: str) -> int:
    """``ident`` (a record handle, digits or a slug) → the live ref id."""
    parsed = handle_registry.parse(ident)
    if parsed is not None and parsed[0] == kind and not parsed[1]:
        ref = store.get_ref(kind=kind, id=parsed[2])
    elif ident.isdigit():
        ref = store.get_ref(kind=kind, id=int(ident))
    else:
        ref = store.get_ref(kind=kind, id=ident)
    if ref is None:
        raise NotFound(
            f"no live {kind} {ident!r}",
            next=f"search(kind={kind!r}, q='...') to find one",
        )
    return int(ref.id)


def eye_handle(store: Store, *, kind: str, id: str | int | None) -> str:
    """``(kind, id)`` → the handle :func:`render_eye` takes, or raise.

    Accepts the kind's public id (an int for numeric kinds, a slug for the
    slug kinds), a universal record or chunk handle, a ``kind:id`` link
    target, and the ``<id>~<ord>`` chunk selector the dispatcher rewrites a
    chunk handle into. ``Unsupported`` for a kind with no graph node;
    ``NotFound`` for a dead or unknown id; ``BadInput`` for a window
    selector (``~0..5``) — an eye focuses one node.
    """
    if kind == "skill":
        if id is None or not str(id).strip():
            raise BadInput("the skill eye needs id=<slug>", next=EYE_NEXT)
        return f"sk:{str(id).strip()}"
    if kind in _NO_NODE_REASON or handle_registry.try_format(kind, 0) is None:
        raise _no_node(kind)
    if id is None or not str(id).strip():
        raise BadInput(f"get(kind={kind!r}, extent=…) needs id=", next=EYE_NEXT)
    s = str(id).strip()
    prefix = f"{kind}:"
    if s.startswith(prefix):
        s = s[len(prefix) :].strip()
    if ".." in s:
        raise BadInput(
            f"an eye focuses one node, not a window ({s!r})",
            next="drop the ..range suffix, or omit extent= to read the window",
        )
    base, sep, ord_s = s.partition("~")
    parsed = handle_registry.parse(base)
    if parsed is not None and parsed[0] == kind and parsed[1] and not sep:
        if store.resolve_handle(base) is None:
            raise NotFound(f"no live {kind} chunk {base!r}")
        return handle_registry.normalize(base)
    ref_id = _resolve_ref_id(store, kind, base)
    if sep:
        if not ord_s.strip().lstrip("-").isdigit():
            raise BadInput(
                f"bad chunk selector {s!r}: expected <id>~<ord>", next=EYE_NEXT
            )
        return _chunk_handle_for_ord(store, kind, ref_id, int(ord_s))
    if kind in _TREE_KINDS:
        code = handle_registry.code_for_kind(kind, chunk=True)
        raise Unsupported(
            f"a {kind} is a tree of sections and the eye focuses one section: "
            f"pass a {code}<id> handle (get(kind={kind!r}, id=…, view='toc') lists them)",
            next=f"get(kind={kind!r}, id='{code}<id>', extent='fisheye+1hop')",
        )
    return handle_registry.format_handle(kind, ref_id)


def eye_response(
    store: Store,
    *,
    kind: str,
    id: str | int | None,
    extent: str | int | Extent,
    q: str | None = None,
) -> Response:
    """Render the eye on ``(kind, id)`` at ``extent`` — the default body of
    :meth:`precis.protocol.Handler.eye`."""
    handle = eye_handle(store, kind=kind, id=id)
    try:
        return Response(body=render_eye(store, handle, extent, q=q))
    except ValueError as e:
        raise BadInput(str(e), next=EYE_NEXT) from e


def eye_body(store: Store, handle: str, extent: Any, *, q: str | None = None) -> str:
    """:func:`render_eye` with its ``ValueError`` as a ``BadInput`` — for a
    handler that already holds the handle (draft's ``¶`` anchors)."""
    try:
        return render_eye(store, handle, extent, q=q)
    except ValueError as e:
        raise BadInput(str(e), next=EYE_NEXT) from e
