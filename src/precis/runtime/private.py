"""Private-kind dispatch, outside shared tool ledgers and continuations.

The normal runtime records argument samples and error heads, search feedback
and exact-read handles. Those are useful corpus diagnostics but are not
owner-scoped private stores. The kind's declared capabilities are public, so
canonical requests for unsupported verbs or shared read views refuse without
looking up an owner or private record. Supported calls and malformed private
envelopes authenticate first. Private calls keep the common argument validator
and permission envelope while returning bounded text directly; no private
payload or response enters the shared result/continuation machinery.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.errors import BadInput, PrecisError, Unsupported, Upstream
from precis.private_kinds import PrivateKind

log = logging.getLogger(__name__)
_MAX_BODY_BYTES = 3500


def _bounded(
    runtime: Any, entry: PrivateKind, body: str, error: bool
) -> tuple[str, bool]:
    if len(body.encode("utf-8")) <= _MAX_BODY_BYTES:
        return body, error
    return runtime._render_failure(
        BadInput(f"private {entry.kind} result is too large", next=entry.next_hint)
    ), True


def dispatch_private(
    runtime: Any, verb: str, args: dict[str, Any], kind: str
) -> tuple[str, bool]:
    """Dispatch private calls without shared caches or content logs."""
    from precis import private_kinds

    with runtime.hub.request_scope(verb=verb):
        return _dispatch_private(runtime, verb, args, private_kinds.PRIVATE_KINDS[kind])


def _refuse_public_capability(
    verb: str, args: dict[str, Any], entry: PrivateKind
) -> None:
    """Refuse declared unsupported operations without inspecting private data."""
    from precis.runtime.dispatch import _VERBS

    kind, ident = args.get("kind"), args.get("id")
    if kind != entry.kind and not (
        kind is None and isinstance(ident, str) and ident.startswith(f"{entry.kind}:")
    ):
        return
    if ident is not None and not isinstance(ident, (str, int)):
        return
    spec = entry.handler_class().spec
    verbs = [name for name in _VERBS if spec.supports(name)]
    if verb not in verbs:
        raise Unsupported(
            f"{entry.kind} does not support {verb}",
            options=verbs,
            next=entry.next_hint,
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
            f"{entry.kind} has no public graph node or shared read view: "
            "use owned activity, settings or event addresses",
            options=verbs,
            next=entry.next_hint,
        )


def _dispatch_private(
    runtime: Any, verb: str, args: dict[str, Any], entry: PrivateKind
) -> tuple[str, bool]:
    from precis.runtime.dispatch import (
        _refuse_agent_secrets,
        _refuse_envelope_denied_verb,
    )
    from precis.user_context import require_user

    try:
        _refuse_public_capability(verb, args, entry)
        require_user(runtime.store)
        clean = dict(args)
        kind = clean.pop("kind", None)
        ident = clean.get("id")
        if ident is not None and not isinstance(ident, (str, int)):
            raise BadInput(
                f"private {entry.kind} id must be a string or integer",
                next=entry.next_hint,
            )
        if kind not in (None, entry.kind):
            raise BadInput(
                f"private {entry.kind} calls require kind='{entry.kind}' alone",
                next=entry.next_hint,
            )
        if isinstance(ident, str) and ident.startswith(f"{entry.kind}:"):
            clean["id"] = ident.removeprefix(f"{entry.kind}:")
        elif kind != entry.kind:
            raise BadInput(
                f"specify kind='{entry.kind}' explicitly", next=entry.next_hint
            )
        _refuse_envelope_denied_verb(verb, {"kind": entry.kind, **clean}, runtime.hub)
        _refuse_agent_secrets(verb, {"kind": entry.kind, **clean}, runtime.hub)
        handler = runtime._resolve_handler(entry.kind, verb, raw_id=clean.get("id"))
        runtime._validate_mode(handler, verb, entry.kind, clean)
        response = runtime._invoke_handler(
            handler,
            verb,
            kind=entry.kind,
            kind_was_defaulted=False,
            args=clean,
        )
        return _bounded(runtime, entry, runtime._render(response), False)
    except PrecisError as exc:
        return _bounded(runtime, entry, runtime._render_failure(exc), True)
    except Exception as exc:
        # Exception messages and SQL diagnostics may contain private values.
        log.warning("private %s request failed (%s)", entry.kind, type(exc).__name__)
        return runtime._render_failure(
            Upstream(
                f"private {entry.kind} storage is unavailable",
                next="retry once; if it persists, report the "
                f"{entry.kind} operation "
                "without including private answers or notes",
            )
        ), True
