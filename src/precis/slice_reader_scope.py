"""The request-boundary allowlist for a scoped slice grant (gr574082,
td574161).

A minted ``scope=<name>``/``refs=<csv>`` pair on the grant file rides the
ASGI scope (:data:`precis.slice_tokens.SCOPE_GRANT_SCOPE` /
``SCOPE_GRANT_REFS``) into two checkpoints that both call
:func:`refusal`: the raw request boundary
(``server._slice_request_refusal``, before the SDK normalizes the
request) and the dispatch backstop
(``tools.core._require_slice_grant_scope``, after argument coercion —
slim verbs flatten ``args=`` onto declared parameters first, so both
layers see what the handler sees). ``None`` means the call is inside the
grant's surface; a :class:`~precis.errors.Denied` carries the refusal
with a ``next=`` the agent can act on.

Two scopes (:data:`precis.slice_tokens.SCOPES`):

- ``reader`` — structure-first reads only: ``get``/``search`` on the
  free kinds (python, md, skill) plus ``get`` on the refs the grant
  names — its own todo and the gripes it fixes.
- ``triage`` — the reader surface plus the gripe-triage write set:
  ``get``/``search`` on any gripe (selecting the one it claims), ``put``
  comments on its own todo and on gripes inside the *effective refs* —
  the grant's ``refs`` plus the gripe the own todo ``fixes``-links
  right now (:func:`fixes_targets`) — ``tag`` ``STATUS:`` /
  ``claimed-by:`` on those same gripes, and ``link(rel='fixes')`` from
  its own todo to one gripe, only while the todo fixes-links nothing
  (the one claim; afterwards that gripe is inside the effective refs).
  The own todo takes comments only — never a tag, and a comment whose
  first line opens with the ``review:``/``landed:``/``answer:`` shapes
  the loop machinery parses refuses.

Everything else — any other verb, kind, ref or parameter shape —
refuses, and a scope stamp outside the known set fails closed: the mint
could not have written it. Both checkpoints read only the server-issued
ASGI stamps, never ``params._meta`` or call arguments, so a parse
problem is a refusal, never full access.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from precis.errors import Denied
from precis.utils import handle_registry

#: Reads a scoped grant may reach regardless of refs: the python kind,
#: repo prose and the skill library. Deliberately not
#: ``_sandbox_read_mcp.READ_KIND_ALLOWLIST`` — that one admits memory
#: and file kinds a builder lane must not see.
_FREE_KINDS = frozenset({"python", "md", "skill"})

#: Kinds readable only through the refs the grant names (todo under
#: both scopes, gripe under reader). Under triage the gripe kind opens:
#: searching and reading any gripe IS the selection half of the job.
_BOUND_KINDS = frozenset({"todo", "gripe"})

_VERBS = {
    "reader": frozenset({"get", "search"}),
    "triage": frozenset({"get", "search", "put", "tag", "link"}),
}

#: The shape of a refs-bound ``get``: the comments views only — no
#: ``q=``, ``extent=`` or search-ish args on the bound kinds.
_GET_KEYS = frozenset({"kind", "id", "view", "args"})
_GET_VIEWS = frozenset({"comments"})
_GET_ARG_KEYS = frozenset({"comment", "comments", "last"})

#: A triage's write-set parameter shapes.
_PUT_KEYS = frozenset({"kind", "id", "text"})
_TAG_KEYS = frozenset({"kind", "id", "add", "remove"})
_LINK_KEYS = frozenset({"kind", "id", "target", "mode", "rel"})

#: The operational tag prefixes a triage may set — the claim and the
#: close, nothing else.
_TAG_PREFIXES = ("STATUS:", "claimed-by:")

#: First-line comment shapes a triage may never post on its own todo —
#: the verdict/landed/answer markers the loop machinery parses
#: (``plan_dispatch._VERDICT_LINE_RE``, ``run_slice._LANDED_RE``,
#: ``plans.release``'s ``answer:`` read), lstripped like those parsers
#: read it, with the optional ``handoff:`` lead
#: ``plan_dispatch._is_review_line`` accepts before a ``review:`` verdict.
#: A triage is not a reviewer; the grant is for picking and closing
#: gripes, not for closing loops.
_COMMENT_SHAPE_RE = re.compile(
    r"^\s*(?:handoff\s*:\s*)?(?:review|landed|answer)\s*:", re.IGNORECASE
)

#: Write verbs — the ones whose triage check reads the live ``fixes``
#: links off the store. Both callers gate the (blocking) lookup on the
#: verb being one of these.
_WRITE_VERBS = frozenset({"put", "tag", "link"})

_NEXT = {
    "reader": (
        "allowed: get/search on kind python, md or skill; "
        "get(kind='todo'|'gripe') on the refs your grant names "
        "(your todo, the gripes it fixes)"
    ),
    "triage": (
        "allowed: the reader surface plus search/get on any gripe, "
        "put(kind=..., id=..., text=...) comments on your todo and on "
        "gripes in your effective refs (the grant's refs plus the gripe "
        "your todo fixes-links), tag STATUS:/claimed-by: on those "
        "gripes, and one "
        "link(kind='todo', id=<your todo>, target='gr<id>', rel='fixes')"
    ),
}


def _resolve(kind: str, ident: Any) -> str | None:
    """The ``<code><pk>`` handle an ``id``/``target`` value names under
    ``kind``, or ``None``.

    Handles go through ``handle_registry.parse`` — the same resolver
    ``handlers/_numeric_ref.py::NumericRefHandler.get`` uses — so the
    decoded kind must match the call's (``gr800`` is not a todo id);
    then the ``<kind>:<int>`` link-target spelling and bare ints, each
    formatted back by ``try_format`` so the result compares against the
    grant's minted ``refs`` strings.
    """
    if isinstance(ident, bool) or ident is None:
        return None
    if isinstance(ident, int):
        return handle_registry.try_format(kind, ident if ident > 0 else None)
    raw = str(ident).strip()
    if not raw:
        return None
    parsed = handle_registry.parse(raw)
    if parsed is not None:
        real_kind, is_chunk, pk = parsed
        if is_chunk or real_kind != kind:
            return None
        return handle_registry.try_format(kind, pk)
    prefix, sep, rest = raw.partition(":")
    if sep:
        if prefix != kind:
            return None
        raw = rest.strip()
    if raw.isdigit():
        return handle_registry.try_format(kind, int(raw))
    return None


def _resolve_all(kind: str, ident: Any) -> list[str] | None:
    """Every member of a scalar|list ``id`` resolved under ``kind`` —
    ``None`` when the id is absent/empty or any member fails to resolve
    to that kind."""
    items = list(ident) if isinstance(ident, (list, tuple)) else [ident]
    if not items or items == [None]:
        return None
    out: list[str] = []
    for item in items:
        handle = _resolve(kind, item)
        if handle is None:
            return None
        out.append(handle)
    return out


def _ids_resolving(kind: str, ident: Any, allowed: Any) -> bool:
    """Every named id resolves under ``kind`` and lands in ``allowed``
    (a refs set), or — when ``allowed`` is None — resolves at all."""
    handles = _resolve_all(kind, ident)
    if handles is None:
        return False
    return allowed is None or all(h in allowed for h in handles)


def _deny(cause: str, scope: str) -> Denied:
    return Denied(cause, next=_NEXT[scope])


def _get_refusal(scope: str, payload: dict[str, Any]) -> Denied | None:
    """Refs-bound ``get``: the comments-view shape on todo (both scopes)
    and gripe (reader); gripe under triage is open — absent id, a
    ``/…`` list view or any gripe handle."""
    if not set(payload) <= _GET_KEYS:
        return _deny(
            f"slice scope {scope!r}: get on this kind takes only "
            "kind/id/view/args (comments views)",
            scope,
        )
    view = payload.get("view")
    if view is not None and view not in _GET_VIEWS:
        return _deny(
            f"slice scope {scope!r}: view={view!r} refused on this kind", scope
        )
    args = payload.get("args")
    if args is not None and (
        not isinstance(args, dict) or not set(args) <= _GET_ARG_KEYS
    ):
        return _deny(
            f"slice scope {scope!r}: get args are comment/comments/last only",
            scope,
        )
    return None


def _read_refusal(
    scope: str,
    verb: str,
    kinds: set[str],
    payload: dict[str, Any],
    refs: frozenset[str],
) -> Denied | None:
    if verb == "search":
        # Search stays free on the free kinds — and on gripe under
        # triage (selecting a gripe is the job). todo search and gripe
        # search under reader would exfiltrate refs the grant never named.
        for kind in kinds:
            if kind in _FREE_KINDS or (kind == "gripe" and scope == "triage"):
                continue
            return _deny(
                f"slice scope {scope!r}: search on kind={kind!r} refused",
                scope,
            )
        return None
    # get
    if kinds <= _FREE_KINDS:
        return None
    shape = _get_refusal(scope, payload)
    if shape is not None:
        return shape
    for kind in kinds & _BOUND_KINDS:
        ident = payload.get("id")
        if kind == "todo":
            if not _ids_resolving("todo", ident, refs):
                return _deny(
                    f"slice scope {scope!r}: todo get is bound to the "
                    "refs your grant names",
                    scope,
                )
        elif scope == "triage":
            # Any gripe — absent id, a '/…' list view or a resolvable id.
            if ident is None or (
                isinstance(ident, str) and ident.strip().startswith("/")
            ):
                continue
            if not _ids_resolving("gripe", ident, None):
                return _deny(
                    "slice scope 'triage': gripe get id must be absent, "
                    "a '/…' view or a gripe handle",
                    scope,
                )
        elif not _ids_resolving("gripe", ident, refs):
            return _deny(
                f"slice scope {scope!r}: gripe get is bound to the "
                "refs your grant names",
                scope,
            )
    return None


def _tag_values(payload: dict[str, Any]) -> Iterable[Any]:
    for key in ("add", "remove"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            yield from value
        else:
            yield value


def _write_refusal(
    verb: str,
    kinds: set[str],
    payload: dict[str, Any],
    refs: frozenset[str],
    fixes: frozenset[str] | None,
) -> Denied | None:
    """The triage write set (a reader's verbs were refused before
    here): comments on the own todo and on gripes inside the effective
    refs — ``refs`` plus ``fixes``, the gripes the own todo
    ``fixes``-links live — ``STATUS:``/``claimed-by:`` tags on those
    same gripes, and the one ``fixes``-link claim while ``fixes`` is
    empty. ``fixes=None`` means the live link read could not run: the
    effective refs narrow to the minted set and the claim refuses —
    never a possible second claim on a guess. Every write takes one
    scalar id; a list is refused outright.

    Known race (accepted, review round 3): the claim check reads the
    live links here while the insert happens later inside the link
    handler's own transaction, so two ``link(fixes)`` calls in flight
    for the same todo can both pass and both insert. Closing it needs
    the read and the insert in one critical section — the check lives
    at the grant boundary, the insert in the generic handler, and a
    unique index on ``fixes`` would forbid a todo fixing two gripes
    for every caller, not just this grant. The blast radius is small:
    a raced second claim is visible in the links, and the slice's own
    land clears only what it fixes."""
    scope = "triage"
    effective = refs | (fixes or frozenset())
    if not kinds <= _BOUND_KINDS or len(kinds) != 1:
        return _deny(
            f"slice scope {scope!r}: {verb} needs kind 'todo' or 'gripe'", scope
        )
    (kind,) = kinds
    ident = payload.get("id")
    if ident is None:
        return _deny(f"slice scope {scope!r}: {verb} needs an id — no create", scope)
    if isinstance(ident, (list, tuple)):
        return _deny(
            f"slice scope {scope!r}: {verb} takes a single id, never a list",
            scope,
        )
    handle = _resolve(kind, ident)
    if verb == "put":
        if not set(payload) <= _PUT_KEYS:
            return _deny(f"slice scope {scope!r}: put carries kind/id/text only", scope)
        text = payload.get("text")
        if not isinstance(text, str):
            # A non-string ``text`` (a ``$ref`` dict, an int, an absent
            # body) would slip the verdict-shape check and dereference
            # downstream — comments are strings, anything else refuses.
            return _deny(
                f"slice scope {scope!r}: put takes a string text — "
                "comment-shape refused",
                scope,
            )
        if kind == "todo":
            if handle not in refs:
                return _deny(
                    f"slice scope {scope!r}: todo comments go on your own todo",
                    scope,
                )
            if _COMMENT_SHAPE_RE.match(text):
                return _deny(
                    f"slice scope {scope!r}: an own-todo comment may not "
                    "open with the review:/landed:/answer: verdict "
                    "shapes — those close loops the grant does not own",
                    scope,
                )
            return None
        if handle not in effective:
            return _deny(
                f"slice scope {scope!r}: gripe comments stay inside the "
                "effective refs (your grant's refs plus the gripe your "
                "todo fixes-links)",
                scope,
            )
        return None
    if verb == "tag":
        if not set(payload) <= _TAG_KEYS:
            return _deny(
                f"slice scope {scope!r}: tag carries kind/id/add/remove only",
                scope,
            )
        if kind != "gripe":
            return _deny(
                f"slice scope {scope!r}: the own todo takes comments "
                "only — no tag on a todo",
                scope,
            )
        if handle not in effective:
            return _deny(
                f"slice scope {scope!r}: gripe tags stay inside the "
                "effective refs (your grant's refs plus the gripe your "
                "todo fixes-links)",
                scope,
            )
        if all(
            part.startswith(_TAG_PREFIXES)
            for value in _tag_values(payload)
            for part in str(value).split(",")
        ):
            return None
        return _deny(f"slice scope {scope!r}: only STATUS:/claimed-by: tags", scope)
    # link — the one claim: own todo → a single gripe, while the todo
    # fixes-links nothing.
    if kind != "todo" or not set(payload) <= _LINK_KEYS:
        return _deny(
            f"slice scope {scope!r}: link is kind/id/target/mode/rel on a todo",
            scope,
        )
    if payload.get("rel") != "fixes" or payload.get("mode") not in (None, "add"):
        return _deny(
            f"slice scope {scope!r}: only link(rel='fixes', mode absent|'add')",
            scope,
        )
    if handle not in refs:
        return _deny(f"slice scope {scope!r}: links start at your own todo", scope)
    target = payload.get("target")
    if isinstance(target, (list, tuple)) or _resolve("gripe", target) is None:
        return _deny(
            f"slice scope {scope!r}: link target must be a single gripe", scope
        )
    if fixes is None:
        return _deny(
            f"slice scope {scope!r}: the live fixes links could not be "
            "read — retry the claim",
            scope,
        )
    if fixes:
        return _deny(
            f"slice scope {scope!r}: your todo already fixes-links a "
            "gripe — the grant carries exactly one claim",
            scope,
        )
    return None


def fixes_targets(store: Any, refs: Any) -> frozenset[str] | None:
    """The gripe handles the grant's own todo ``fixes``-links right now —
    the live half of the effective refs and the one-claim check (review
    F1). Mirrors ``run_slice._fixes_refs``: the first todo handle in
    ``refs`` is the grant's own todo, and each out ``fixes`` link whose
    target is a live gripe counts. ``None`` when the read cannot run —
    no store, no todo handle in ``refs``, or a store error — so the
    caller narrows to the minted refs and the claim fails closed."""
    if store is None:
        return None
    own: int | None = None
    for ref in refs or ():
        parsed = handle_registry.parse(str(ref))
        if parsed is not None and parsed[0] == "todo" and not parsed[1]:
            own = parsed[2]
            break
    if own is None:
        return None
    try:
        out: set[str] = set()
        for link in store.links_for(own, direction="out", relation="fixes") or ():
            dst = getattr(link, "dst_ref_id", None)
            if isinstance(dst, int) and store.get_ref(kind="gripe", id=dst) is not None:
                handle = handle_registry.try_format("gripe", dst)
                if handle:
                    out.add(handle)
        return frozenset(out)
    except Exception:
        return None


def _more_target(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The (verb, kwargs) a ``more`` cursor continues, or ``None``.

    ``more`` re-issues the ready-made read a truncation footer names
    (gr592662), so the boundary judges the continuation exactly as the
    hand-issued call — the agent could issue that call directly, the
    cursor adds no authority. An unparseable cursor or one naming a
    write verb unwraps to ``None``: the refusal fails closed.
    """
    cursor = payload.get("cursor")
    if not isinstance(cursor, str):
        return None
    from precis.tools.command_parser import CommandParseError, parse_more_cursor

    try:
        return parse_more_cursor(cursor)
    except CommandParseError:
        return None


def refusal(
    scope: str,
    verb: str,
    payload: dict[str, Any],
    refs: Any,
    fixes: Any = None,
) -> Denied | None:
    """``None`` when the call is inside the grant's surface, else the
    :class:`Denied` both checkpoints raise or render.

    ``scope``/``refs`` are the trusted ASGI stamps the token-auth
    middleware set off the validated grant file — never caller input —
    so a scope outside :data:`precis.slice_tokens.SCOPES` fails closed:
    no mint could have written it. ``fixes`` is the caller's live read
    of the own todo's ``fixes`` links (:func:`fixes_targets`) — an
    iterable of gripe handles, or ``None`` when the read could not run.
    """
    if scope not in _VERBS:
        return Denied(
            f"unknown slice grant scope {scope!r}",
            next="re-mint the grant with scope=reader or scope=triage",
        )
    if not isinstance(payload, dict):
        payload = {}
    if verb == "more":
        inner = _more_target(payload)
        if inner is None:
            return _deny(
                f"slice scope {scope!r}: more(cursor=...) must name a "
                "get/search continuation call",
                scope,
            )
        verb, payload = inner
    if verb not in _VERBS[scope]:
        return _deny(
            f"slice scope {scope!r} refuses {verb or 'non-tool requests'!r}",
            scope,
        )
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return _deny(f"slice scope {scope!r}: {verb} needs kind=", scope)
    kinds = {k.strip() for k in kind.split(",") if k.strip()}
    if not kinds or not kinds <= _FREE_KINDS | _BOUND_KINDS:
        bad = sorted(kinds - _FREE_KINDS - _BOUND_KINDS) or sorted(kinds)
        return _deny(
            f"slice scope {scope!r} does not serve kind {','.join(bad)!r}", scope
        )
    refset = frozenset(refs or ())
    if verb in ("get", "search"):
        return _read_refusal(scope, verb, kinds, payload, refset)
    fixset = None if fixes is None else frozenset(fixes)
    return _write_refusal(verb, kinds, payload, refset, fixset)


__all__ = ["fixes_targets", "refusal"]
