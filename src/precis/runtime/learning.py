"""Private learning dispatch, outside shared tool ledgers and continuations.

The normal runtime records argument samples and error heads, search feedback
and exact-read handles. Those are useful corpus diagnostics but are not
owner-scoped learning stores. The kind's declared capabilities are public, so
canonical requests for unsupported verbs or shared read views refuse without
looking up an owner or private record. Supported calls and malformed learning
envelopes authenticate first. Private calls keep the common argument validator
and permission envelope while returning bounded text directly; no private
payload or response enters the shared result/continuation machinery.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.errors import BadInput, PrecisError, Unsupported, Upstream

log = logging.getLogger(__name__)
_MAX_BODY_BYTES = 3500
_NEXT = "get(kind='learning', id='/activity', args={'days': 14})"


def _bounded(runtime: Any, body: str, error: bool) -> tuple[str, bool]:
    if len(body.encode("utf-8")) <= _MAX_BODY_BYTES:
        return body, error
    return runtime._render_failure(
        BadInput("private learning result is too large", next=_NEXT)
    ), True


def dispatch_learning(
    runtime: Any, verb: str, args: dict[str, Any]
) -> tuple[str, bool]:
    """Dispatch private calls without shared caches or content logs."""
    with runtime.hub.request_scope(verb=verb):
        return _dispatch_learning(runtime, verb, args)


def _refuse_public_capability(verb: str, args: dict[str, Any]) -> None:
    """Refuse declared unsupported operations without inspecting private data."""
    from precis.handlers.learning import LearningHandler
    from precis.runtime.dispatch import _VERBS

    kind, ident = args.get("kind"), args.get("id")
    if kind != "learning" and not (
        kind is None and isinstance(ident, str) and ident.startswith("learning:")
    ):
        return
    if ident is not None and not isinstance(ident, (str, int)):
        return
    spec = LearningHandler.spec
    verbs = [name for name in _VERBS if spec.supports(name)]
    if verb not in verbs:
        raise Unsupported(
            f"learning does not support {verb}", options=verbs, next=_NEXT
        )
    extras = args.get("__extras__")
    read_keys = ("extent", "ring", "view", "q")
    if verb == "get" and (
        any(args.get(key) is not None for key in read_keys)
        or (
            isinstance(extras, dict)
            and any(extras.get(key) is not None for key in read_keys)
        )
    ):
        raise Unsupported(
            "learning has no public graph node or shared read view: "
            "use owned activity, settings or event addresses",
            options=verbs,
            next=_NEXT,
        )


def _dispatch_learning(
    runtime: Any, verb: str, args: dict[str, Any]
) -> tuple[str, bool]:
    from precis.runtime.dispatch import (
        _refuse_agent_secrets,
        _refuse_envelope_denied_verb,
    )
    from precis.user_context import require_user

    try:
        _refuse_public_capability(verb, args)
        require_user(runtime.store)
        clean = dict(args)
        kind = clean.pop("kind", None)
        ident = clean.get("id")
        if ident is not None and not isinstance(ident, (str, int)):
            raise BadInput(
                "private learning id must be a string or integer", next=_NEXT
            )
        if kind not in (None, "learning"):
            raise BadInput(
                "private learning calls require kind='learning' alone", next=_NEXT
            )
        if isinstance(ident, str) and ident.startswith("learning:"):
            clean["id"] = ident.removeprefix("learning:")
        elif kind != "learning":
            raise BadInput("specify kind='learning' explicitly", next=_NEXT)
        _refuse_envelope_denied_verb(verb, {"kind": "learning", **clean}, runtime.hub)
        _refuse_agent_secrets(verb, {"kind": "learning", **clean}, runtime.hub)
        handler = runtime._resolve_handler("learning", verb, raw_id=clean.get("id"))
        runtime._validate_mode(handler, verb, "learning", clean)
        response = runtime._invoke_handler(
            handler,
            verb,
            kind="learning",
            kind_was_defaulted=False,
            args=clean,
        )
        return _bounded(runtime, runtime._render(response), False)
    except PrecisError as exc:
        return _bounded(runtime, runtime._render_failure(exc), True)
    except Exception as exc:
        # Exception messages and SQL diagnostics may contain private values.
        log.warning("private learning request failed (%s)", type(exc).__name__)
        return runtime._render_failure(
            Upstream(
                "private learning storage is unavailable",
                next="retry once; if it persists, report the learning operation "
                "without including private answers or notes",
            )
        ), True
