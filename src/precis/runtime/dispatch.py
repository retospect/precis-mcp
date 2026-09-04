"""Core dispatch: verb routing, kind/handler resolution, handler
invocation — the in-process MCP verb call (`runtime.dispatch`; not the
`Hub` registration table in `precis/dispatch.py`, nor the dispatch
worker).

``DispatchMixin`` carries ``dispatch()``/``dispatch_with_status()``, the
single-kind ``kind=`` resolution chain (2-char handle-code expansion,
id-prefix/sigil/relative-handle inference), and handler invocation
(extras whitelist, required-kwarg check, default-tags policy). Cross-kind/
source-search fan-out lives in :mod:`precis.runtime.search`; angle
spray+dreamable region in :mod:`precis.runtime.angle`; hint emission in
:mod:`precis.runtime.hints`; error rendering in
:mod:`precis.runtime.error`. ``PrecisRuntime`` (``precis.runtime.core``)
composes all of them via multiple inheritance — every method runs
against the same ``self`` regardless of defining file.

``dispatch_with_status`` is the single chokepoint every verb call passes
through (MCP server, CLI, in-process agent tick), so it's where the
best-effort tool-call ledger write lives
(:meth:`DispatchMixin._record_tool_call`, migration 0133,
:mod:`precis.tool_ledger`).
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import time
from typing import Any

from precis.errors import BadInput, Internal, NotFound, PrecisError, Unsupported
from precis.protocol import _ALL_VERBS, Handler, Verb
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import (
    UNCITED_UNSUPPORTED_KINDS as _UNCITED_UNSUPPORTED_KINDS,
)
from precis.runtime._shared import RuntimeShape
from precis.utils import handle_registry

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = _ALL_VERBS

#: Sentinel key used by `precis.server` to forward the MCP tool's
#: ``args={...}`` payload through to the dispatcher without colliding
#: with the explicit positional kwargs. The dispatcher pops it before
#: calling the handler method and validates the keys against the
#: method's accepted-kwargs whitelist.
_EXTRAS_KEY = "__extras__"

#: Address sigils that self-identify a kind, so ``get(id='¶handle')``
#: works without ``kind=`` (the draft skill documents exactly that).
#: Value is ``(kind, keep_sigil)``: ``¶`` stays in ``id=`` because the
#: draft handler matches on a leading ``¶``; ``§`` is stripped to the
#: bare ``slug~n`` the paper handler resolves. Distinct from the
#: ``kind:slug`` colon prefix (also self-identifying) handled alongside.
_SIGIL_KIND: dict[str, tuple[str, bool]] = {
    "¶": ("draft", True),
    "§": ("paper", False),
}

#: A draft chunk handle ``dc<chunk_id>`` and any trailing relative operator;
#: group 1 is the bare ``<chunk_id>`` (existence probe), group 2 the operator.
_DRAFT_DC_RE = re.compile(r"^dc(\d+)(.*)$")

#: Matches the ``[error:ClassName]`` prefix :meth:`ErrorMixin.render_error`
#: always emits — the tool-call ledger (:mod:`precis.tool_ledger`) reads the
#: error class name back out of the already-rendered body instead of
#: threading the exception object through, so logging it costs one regex
#: match rather than a second code path.
_ERROR_TYPE_RE = re.compile(r"^\[error:(\w+)\]")

#: Per-(kind, verb) recovery hints for "kind does not support verb" — a
#: generic "try get(kind=…)" is a dead-end when the right move is a
#: *different shape entirely*. Drafts are the case that bites: an agent
#: reaches for the universal ``link``/``tag`` verbs, but a draft's
#: cross-references live in prose (the autolinker backlinks them), and it
#: has no whole-ref tag axis. Teach the real move inline.
_VERB_REDIRECTS: dict[tuple[str, str], str] = {
    # ("draft", "link") moved into DraftHandler.link itself — the verb
    # now exists for folder placement, so the prose-ref
    # teaching rides on its BadInput for every other relation.
    ("draft", "tag"): (
        "drafts have no whole-ref tag axis; tag the owning project todo "
        "instead, or use a glossary term / inline markup inside the prose."
    ),
}


def _tick_disabled_hint(kind: str) -> str | None:
    """The per-tick disable hint for ``kind``, or ``None``.

    Reads the thread-scoped in-process tick ContextVar
    (:func:`precis.utils.inproc_context.current`): if the active tick prohibits
    ``kind``, returns its contextual hint (what to do instead), else ``None``.
    Unset outside an in-process agent tick — the MCP server / CLI / tests never
    bind it — so this is a no-op there. The import is local so ``runtime`` keeps
    no import-time coupling to the loop context (``inproc_context`` is stdlib-
    only, but the read is per-call and cheap either way)."""
    from precis.utils.inproc_context import current

    ctx = current()
    if ctx is None or not ctx.disabled_kinds:
        return None
    return dict(ctx.disabled_kinds).get(kind)


class DispatchMixin(RuntimeShape):
    """Verb dispatch, kind/handler resolution, and handler invocation."""

    def dispatch(self, verb: str, args: dict[str, Any]) -> str:
        """Run one verb call. Returns the rendered string for the agent.

        Errors are caught and rendered as text — they never propagate
        out (MCP expects a string return). Tests rely on this shape;
        callers that need the protocol-level error flag (e.g. the MCP
        tool wrappers) should use :meth:`dispatch_with_status`."""
        body, _is_error = self.dispatch_with_status(verb, args)
        return body

    def dispatch_with_status(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one verb call and report whether it errored.

        Returns ``(body, is_error)``. ``body`` is the same rendered text
        that :meth:`dispatch` returns; ``is_error`` is True when the
        call raised a :class:`PrecisError` or unhandled exception.
        Lets the MCP tool wrapper raise so FastMCP sets the protocol-
        level ``isError`` flag while keeping a single source of truth
        for the rendering. (MCP critic MAJOR — errors silently masked
        as content because ``isError`` was never set.)
        """
        started = time.monotonic()
        with self.hub.request_scope():
            try:
                if verb not in _VERBS:
                    raise BadInput(
                        f"unknown verb: {verb}",
                        options=list(_VERBS),
                    )
                response = self._dispatch_inner(verb, dict(args))
                # Chunk over-large bodies so they don't blow the
                # MCP stdio frame. On a long-lived runtime (MCP
                # server / `precis repl`) the pagination cache stashes
                # the tail under a cursor; the agent calls
                # ``more(cursor=...)`` to retrieve it. A short-lived
                # runtime (e.g. `precis eval`, the default) has no
                # process around to redeem a cursor with, so
                # ``cursor_capable=False`` skips minting one and the
                # footer points at PRECIS_MAX_BODY_BYTES / a long-lived
                # session instead (gr267466). A handler may set
                # ``response.pagination_alt_hint`` to point at a
                # cheaper alternative to draining every page (e.g.
                # the skill handler's targeted-section access).
                body, _cursor = self.pagination.split(
                    self._render(response),
                    alt_hint=response.pagination_alt_hint,
                    cursor_capable=self.long_lived,
                )
                self._record_tool_call(verb, args, body, False, started)
                return body, False
            except PrecisError as e:
                self._maybe_add_skill_hint(e, verb, args)
                self._maybe_add_schema_drift_hint(e)
                body = self.render_error(e)
                self._record_tool_call(verb, args, body, True, started)
                return body, True
            except Exception as e:
                # F10: full traceback (with SQL fragments, Python
                # signatures, file paths) goes to the server log only.
                # The user-visible body keeps the exception *type* —
                # enough signal for the LLM to recover ("UndefinedTable
                # → run migrations") — but strips the message body that
                # leaks internals. Specific exception classes that have
                # a clean recovery story should be caught upstream and
                # converted to a typed PrecisError (Unavailable,
                # NotFound, etc.) before reaching this fallback.
                log.exception("internal error in %s", verb)
                body = self.render_error(
                    Internal(
                        f"internal error in {verb}: {type(e).__name__} "
                        "(see server log)",
                        next=self._schema_drift_note(e),
                    )
                )
                self._record_tool_call(verb, args, body, True, started)
                return body, True

    def _record_tool_call(
        self,
        verb: str,
        args: dict[str, Any],
        body: str,
        is_error: bool,
        started: float,
    ) -> None:
        """Best-effort tool-call ledger write (migration 0133).

        Fires after the verb has already fully run and its body is
        rendered — the one extra INSERT never gates the caller's
        result, and any failure here (missing table on a pre-0133 DB,
        a bad/absent store, a connection hiccup) is swallowed. A
        logging problem must never break the tool call it measures,
        mirroring :mod:`precis.route_log`'s dark-by-construction rule.

        ``args`` is read, never mutated — by the time this runs,
        ``_dispatch_inner`` has already consumed its own private copy
        (``dict(args)``), so the caller's original top-level kwarg
        names are still exactly what came in. Only names are
        recorded, never values (:mod:`precis.tool_ledger`'s no-
        payload-content contract).
        """
        store = self.store
        if store is None:
            return
        try:
            from precis.agentlog import current_from_env
            from precis.tool_ledger import ToolCallRecord, record_call
            from precis.utils.workspace import current_model_from_env

            error_type: str | None = None
            if is_error:
                m = _ERROR_TYPE_RE.match(body)
                error_type = m.group(1) if m else "Unknown"
            kind = args.get("kind")
            record_call(
                store,
                ToolCallRecord(
                    verb=verb,
                    kind=str(kind) if kind is not None else None,
                    # None-valued keys are wrapper defaults, not caller
                    # input: tools/core.py's put/edit/get pass every
                    # declared kwarg defaulted to None, so recording bare
                    # key presence would log the same static ~70-key list
                    # for every put — useless for arg-shape mining.
                    input_keys=sorted(str(k) for k, v in args.items() if v is not None),
                    outcome="error" if is_error else "ok",
                    error_type=error_type,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    agentlog_id=current_from_env(),
                    source=current_model_from_env(),
                    profile=os.environ.get("PRECIS_MCP_PROFILE", "typed"),
                ),
            )
        except Exception:
            log.debug("tool_ledger: _record_tool_call failed", exc_info=True)

    def _maybe_add_schema_drift_hint(self, err: PrecisError) -> None:
        """Append the schema-drift recovery hint to an already-typed error.

        Handlers (and :meth:`_invoke_handler`'s defaulted-kind wrap)
        sometimes re-raise a psycopg schema error inside a
        ``PrecisError`` — the drift note must ride on that envelope
        too, not only on the raw-exception fallback path.
        """
        note = self._schema_drift_note(err)
        if note is None:
            return
        if err.next is None:
            err.next = note
        elif isinstance(err.next, str):
            err.next = [err.next, note]
        else:
            err.next.append(note)

    def _schema_drift_note(self, exc: BaseException) -> str | None:
        """Actionable recovery hint when ``exc`` chains to a
        schema-shape DB error, else ``None``.

        The gr281493 outage family (dozens of duplicate gripes filed
        blind): a long-lived server keeps serving after a deploy
        migrates the DB under it, and every ref-backed verb then dies
        with an opaque ``[error:Internal] … UndefinedColumn (see server
        log)`` — agents can't tell a code bug from "the process is
        stale, restart it". When the exception chain carries
        ``psycopg.errors.UndefinedColumn``/``UndefinedTable``, compare
        the migration head captured at runtime construction
        (:data:`~precis.runtime.core.PrecisRuntime.boot_migration_head`)
        against the live ``_migrations`` ledger and name the real
        recovery. Best-effort: any probe failure (no store, ledger
        query fails, no boot head) returns ``None`` and the generic
        envelope stands.
        """
        try:
            from psycopg.errors import UndefinedColumn, UndefinedTable
        except Exception:  # pragma: no cover — psycopg always present
            return None
        seen: set[int] = set()
        cur: BaseException | None = exc
        found = False
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, (UndefinedColumn, UndefinedTable)):
                found = True
                break
            cur = cur.__cause__ or cur.__context__
        if not found:
            return None

        boot = getattr(self, "boot_migration_head", None)
        store = self.store
        if boot is None or store is None:
            return None
        try:
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT max(version) FROM public._migrations "
                    "WHERE plugin = 'precis'"
                ).fetchone()
            db_head = row[0] if row else None
        except Exception:
            log.debug("schema-drift probe failed", exc_info=True)
            return None
        if db_head is None or db_head == boot:
            return None
        if db_head > boot:
            return (
                f"the database is at migration {db_head} but this server "
                f"process booted with migrations up to {boot} — the process "
                "predates a migration and is serving stale code. Restart "
                "the precis MCP server / worker; no retry will succeed "
                "until then."
            )
        return (
            f"this build ships migration {boot} but the database head is "
            f"{db_head} — the database is behind the code. Run `precis "
            "migrate` (scripts/deploy does this), then retry."
        )

    def fetch_more(self, cursor: str) -> tuple[str, bool]:
        """Return the next page for a pagination cursor.

        Mirrors :meth:`dispatch_with_status`'s ``(body, is_error)``
        return shape so the ``more`` MCP tool's wrapper code is
        identical to the seven-verb wrappers. Returns
        ``(error_body, True)`` when ``cursor`` isn't in this
        process's :class:`~precis._pagination.PaginationCache` so the
        protocol-level ``isError`` flag flips.

        Recursive cursors: if the popped tail is itself oversized,
        :class:`PaginationCache` re-splits and embeds the new
        cursor in the returned body's footer.
        """
        tail = self.pagination.pop(cursor)
        if tail is None:
            # gr267466: ``PaginationCache._prune_expired`` drops an
            # actually-TTL-expired entry *before* ``pop`` can tell "it
            # expired" apart from "it never existed in this process" —
            # both land here as a plain miss. Rather than guess, lead
            # with the true, always-applicable explanation (process
            # lifetime) and fold the TTL/single-use case in as a
            # secondary possibility — never claim "expired", which
            # would misdirect a `precis eval` caller toward a timing
            # fix when the real problem is that the cursor's cache
            # died with the process that minted it.
            err = BadInput(
                f"no such cursor in this process: {cursor!r}",
                next=(
                    "pagination cursors live only in the process that "
                    "minted them — a `precis eval` invocation exits (and "
                    "takes its cursor cache with it) the moment it prints "
                    "its result, so a cursor from a prior `precis eval` "
                    "call can never be found here even though it was "
                    "genuinely valid a moment ago. Set PRECIS_MAX_BODY_BYTES "
                    "higher to avoid the truncation in the first place, or "
                    "use a long-lived session (the MCP server, or `precis "
                    "repl`) where cursors are retrievable for a few "
                    "minutes. If you're already in a long-lived session and "
                    "still see this, the cursor was single-use and already "
                    "consumed, or its few-minute window passed — re-issue "
                    "the original call to get a fresh page."
                ),
            )
            return self.render_error(err), True
        return tail, False

    def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
        """``search(uncited=...)`` resolution wrapper around
        :meth:`_dispatch_inner_core`.

        Resolves ``uncited=<draft>`` into a merged
        ``args['exclude_ref_ids']`` BEFORE any search-shape interception
        branches off, so every retrieval path sees the same filter — then
        prepends the "N already-cited sources excluded" note to whatever
        ``Response`` the core method returns, regardless of which
        internal early-return branch produced it. Kept as a thin wrapper
        (not folded into the core method) precisely because there are so
        many early returns there: one wrapping call guarantees the
        footer is never forgotten on a future branch.

        The note is **prepended**, not appended: pagination keeps the
        largest *leading* run that fits the byte cap, stashing the rest
        behind ``more()`` — a trailing note on a paginated result would
        strand the filter signal on a page the caller never reads.
        """
        uncited_note: str | None = None
        if verb == "search" and args.get("uncited") is not None:
            self._reject_uncited_unfiltered_shape(args)
            uncited_note = self._resolve_uncited_exclude(args)
        response = self._dispatch_inner_core(verb, args)
        if uncited_note is not None:
            from dataclasses import replace as _replace

            response = _replace(response, body=f"{uncited_note}\n\n{response.body}")
        return response

    def _reject_uncited_unfiltered_shape(self, args: dict[str, Any]) -> None:
        """Refuse ``uncited=`` on the search shapes that
        :meth:`_dispatch_inner_core` intercepts and returns from *before*
        any ``exclude_ref_ids`` is consulted.

        ``view='dreamable'``, ``view='stubs'``, ``view='chase-queue'`` and
        the ``angle=``/``like=`` spray each pick their own seed and target
        set and never read ``exclude_ref_ids`` — and their default target
        set includes ``paper``, the very kind the exclusion is built from.
        Left alone they would return fully unfiltered hits *underneath the
        "N already-cited sources excluded" note*, which is worse than no
        feature at all: the note actively attests that a filter ran. A
        caller cannot see the difference from the output, so this must
        fail loudly. Same stance as the ``UNCITED_UNSUPPORTED_KINDS``
        guard, for the same reason.
        """
        view = str(args.get("view") or "").strip()
        shape = (
            view
            if view in ("dreamable", "stubs", "chase-queue")
            else "angle="
            if "angle" in args
            else "like="
            if "like" in args
            else None
        )
        if shape is None:
            return
        raise Unsupported(
            f"uncited= is not supported with {shape} — that search shape "
            "picks its own seed and target set and has no "
            "exclude-by-ref_id wiring",
            next=(
                "drop uncited=, or use a plain search(q=..., uncited=...) "
                "which filters across every citeable kind"
            ),
        )

    def _resolve_uncited_exclude(self, args: dict[str, Any]) -> str:
        """Resolve ``search(uncited=<draft>)`` into
        ``args['exclude_ref_ids']``.

        Pops the raw ``uncited=`` token, replaces it with a merged, sorted
        ``list[int]`` under ``exclude_ref_ids`` — the exact channel every
        retrieval path (source-search, cross-kind, single-kind) reads, so
        the filter can't drift between paths. The exclusion set is
        :func:`precis.backfill.candidates.draft_cited_ref_ids`: every
        source the draft directly cites, plus a cited claim hub's
        evidence-**supporter** papers (originators+corroborators; a
        contradicting paper keeps surfacing). Raises when ``uncited=``
        doesn't resolve to a live draft — must never silently degrade to
        an empty exclusion set, which would make every hit look "new".

        Returns the agent-facing note reporting how many refs were
        excluded.
        """
        from precis.backfill.candidates import draft_cited_ref_ids, resolve_draft_ref_id
        from precis.utils import handle_registry

        token = args.pop("uncited")
        store = self.store
        if store is None:
            raise Unsupported("uncited= needs a store-backed deployment")
        draft_ref_id = resolve_draft_ref_id(store, str(token))
        cited = draft_cited_ref_ids(store, draft_ref_id)
        merged: set[int] = set(args.get("exclude_ref_ids") or ())
        merged |= cited
        args["exclude_ref_ids"] = sorted(merged)
        handle = (
            handle_registry.try_format("draft", draft_ref_id) or f"draft:{draft_ref_id}"
        )
        n = len(cited)
        return (
            f"_(uncited={handle}: {n} already-cited source{'s' if n != 1 else ''} "
            "excluded)_"
        )

    def _dispatch_inner_core(self, verb: str, args: dict[str, Any]) -> Response:
        """Orchestrate one verb call.

        Three responsibilities, each delegated to a helper:
          1. Cross-kind fan-out (``kind='*'`` / comma-list).
          2. Single-kind resolution including ``kind=`` defaulting
             for ``search`` calls that omit it.
          3. Handler invocation with extras whitelist + defaulted-kind
             error annotation.
        """
        # D1: accept URI-style ``id='kind:slug[~sel]'`` on input. Extract
        # the kind prefix into ``args['kind']`` (if not already set) and
        # leave the unprefixed identifier in ``args['id']``. Validation
        # that any explicit ``kind=`` matches the prefix lives in the
        # helper. Output stays kind-explicit — this is an *input*
        # convenience that mirrors the canonical ``kind:identifier``
        # grammar already used by ``link=`` / ``unlink=``.
        self._maybe_split_prefixed_id(args)
        kind = args.pop("kind", None)

        # Broad usability pass 2026-05-30 (#6): when an agent passes a
        # tag-shaped string as ``q=`` with no ``tags=`` filter, the
        # semantic search is statistically guaranteed to drown the
        # intended tagged refs in unrelated paper hits. Catch the
        # likely intent at the boundary and emit a deduplicated tip.
        if verb == "search":
            self._maybe_hint_tag_shaped_q(args)

        # Focus region: ``search(view='dreamable')`` is the salience
        # seed + its ANN ring (docs/backlog/dreaming.md, §view='dreamable'),
        # not the lexical+RRF path. Intercept before kind resolution —
        # it picks its own seed and cross-kind target set.
        if verb == "search" and str(args.get("view") or "").strip() == "dreamable":
            return self._dispatch_dreamable(kind, dict(args))

        # Backlog view: ``search(view='stubs')`` is the "papers we still
        # need to get" list — paper refs with an external id but no PDF
        # yet (the stub surfaces). Paper-only; ignores
        # ``q=``. Intercept before kind resolution.
        if verb == "search" and str(args.get("view") or "").strip() == "stubs":
            return self._dispatch_stubs(dict(args))

        # Chase-queue view: ``search(view='chase-queue')`` is the DOI-only,
        # never-tried-first slice of the same stub backlog — a tighter feed
        # for "what should I go find a DOI-resolvable PDF for right now"
        # (the stub surfaces). Paper-only; ignores ``q=``.
        # Intercept before kind resolution, same as ``view='stubs'``.
        if verb == "search" and str(args.get("view") or "").strip() == "chase-queue":
            return self._dispatch_chase_queue(dict(args))

        # Angle spray: ``search`` with ``angle=`` or ``like=`` is the
        # diverse-cone semantic sampler (docs/backlog/dreaming.md), not
        # the lexical+RRF path. Intercept before kind resolution — it
        # owns its own seed resolution and cross-kind target set.
        if verb == "search" and ("angle" in args or "like" in args):
            return self._dispatch_angle(kind, dict(args))

        # Compact keywords-only TOON: ``search(view='keywords', ...)``
        # — discovery shape that returns just the keyword arrays for
        # the top hits (no preview text). Cross-kind by default (so
        # ``view='keywords'`` alone works as "what topics span the
        # corpus"); a specific ``kind=`` narrows the fan-out the same
        # way the cross-kind path does.
        if verb == "search" and str(args.get("view") or "").strip() == "keywords":
            return self._dispatch_cross_kind(
                kind if kind is not None else _CROSS_KIND_WILDCARD,
                dict(args),
            )

        # ``folder=`` scope: any folder-scoped search runs
        # through the cross-kind fan-out — the structured SearchHit
        # stream is what makes subtree post-filtering possible — even
        # when a single ``kind=`` was named (it becomes a one-kind
        # "comma list").
        if verb == "search" and args.get("folder") is not None:
            return self._dispatch_cross_kind(
                (
                    self._expand_kind_code(str(kind))
                    if kind is not None
                    else _CROSS_KIND_WILDCARD
                ),
                dict(args),
            )

        # Source search (the cross-kind primitive): a ``sort=`` /
        # ``since=`` / ``until=`` search routes to the chunk-level
        # cross-kind primitive — one store query over ``refs.kind =
        # ANY(...)`` that RRF-fuses lexical+semantic, collapses to one
        # best chunk per ref, bounds by ``refs.created_at``, and orders
        # by relevance (default) or recency. Distinct from the per-handler
        # fan-out below. Intercept before kind resolution so it composes
        # with a single kind, a comma-list, a wildcard, or an omitted kind.
        if verb == "search" and self._is_source_search_request(args):
            return self._dispatch_source_search(kind, dict(args))

        # Cross-kind: ``kind='*'`` or comma-list. Other verbs keep the
        # single-kind contract — multi-kind get is meaningless and
        # multi-kind put would silently scatter writes.
        if verb == "search" and self._is_cross_kind_request(kind):
            return self._dispatch_cross_kind(kind, dict(args))

        # Resolve the kind. ``_resolve_kind`` may itself short-circuit
        # to a cross-kind merge when ``kind`` is None on a search call
        # and the corpus has >=2 search-supporting kinds.
        resolved_kind, kind_was_defaulted, cross_kind_resp = self._resolve_kind(
            verb, kind, args
        )
        if cross_kind_resp is not None:
            return cross_kind_resp

        # ``uncited=`` (resolved above into ``args['exclude_ref_ids']``)
        # combined with an EXPLICIT single-kind request for a citeable kind
        # that has no SQL-level exclusion wiring (patent's local+OPS-remote
        # search, edgar's filing search — see
        # :data:`~precis.runtime._shared.UNCITED_UNSUPPORTED_KINDS`) must
        # fail loudly rather than silently return hits that might already
        # be cited. The default wildcard cross-kind fan-out handles this
        # kind pair differently (drops + footer-notes them, in
        # ``_dispatch_cross_kind``) since raising there would break the
        # common unscoped ``search(q=..., uncited=...)`` call entirely.
        if (
            verb == "search"
            and args.get("exclude_ref_ids") is not None
            and resolved_kind in _UNCITED_UNSUPPORTED_KINDS
        ):
            raise Unsupported(
                f"uncited= is not supported for kind={resolved_kind!r} — its "
                "search has no exclude-by-ref_id wiring yet",
                next=(
                    "drop uncited= for this kind, or use "
                    "sort=/since=/until= (the cross-kind source-search "
                    "primitive) which excludes uniformly across every kind"
                ),
            )

        handler = self._resolve_handler(resolved_kind, verb)
        return self._invoke_handler(
            handler,
            verb,
            kind=resolved_kind,
            kind_was_defaulted=kind_was_defaulted,
            args=args,
        )

    def _resolve_kind(
        self,
        verb: str,
        kind: Any,
        args: dict[str, Any],
    ) -> tuple[str, bool, Response | None]:
        """Pin ``kind`` to a single string, or short-circuit to cross-kind.

        Returns ``(kind, defaulted, cross_kind_response)``. When the
        third element is non-None, the caller must return it directly
        — the resolver decided to fan out across kinds because no
        single defensible default exists.

        Raises ``BadInput`` when ``kind`` is missing for a non-search
        verb, or when ``search`` has no usable default and no eligible
        cross-kind targets.
        """
        if kind is not None:
            return self._expand_kind_code(str(kind)), False, None

        if verb != "search":
            raise BadInput(
                "missing kind=",
                options=sorted(self.hub.kinds),
                next=self._missing_kind_hints(verb),
            )

        # ``search()`` without ``kind=`` defaults to cross-kind
        # fan-out across every search-hits-capable kind. Earlier
        # versions defaulted to the most-recently-touched single
        # kind as a 7B affordance, but a "what do I know about X"
        # query is the natural shape of an unscoped search and
        # the user should see hits from every corner of the corpus
        # — biasing toward the last-touched kind hid useful answers
        # in the other kinds. The MCP critic flagged the gap as a
        # design hole (gripe:3681 #2, 2026-05-01); this commit
        # closes it by reversing the precedence: cross-kind first,
        # single-kind fallback only when the hub has <2 eligible
        # kinds. (MCP critic MAJOR-C 2026-05-02.)
        search_kinds: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            if handler.spec.supports_search:
                search_kinds.append(k)
        cross_kind = self._cross_kind_kinds()
        if len(cross_kind) >= 2:
            return (
                _CROSS_KIND_WILDCARD,
                False,
                self._dispatch_cross_kind(_CROSS_KIND_WILDCARD, dict(args)),
            )

        # ≤1 search-hits-capable kind in this build: fall back to
        # the most-recently-touched kind so a single-kind deployment
        # still works without forcing the agent to spell ``kind=``.
        defaulted = self._default_search_kind(search_kinds)
        if defaulted is not None:
            return defaulted, True, None

        raise BadInput(
            "missing kind= and no defensible default available",
            options=search_kinds,
            next=(
                "pass kind=<one of the listed kinds>, or use "
                "kind='*' / kind='all' / kind='paper,memory' for cross-kind merge"
            ),
        )

    def _expand_kind_code(self, kind: str) -> str:
        """Accept a 2-char handle code as ``kind=``.

        ``kind='dr'`` ≡ ``kind='draft'``, ``kind='pa'`` ≡ ``kind='paper'`` —
        the same registry that legends a handle now also resolves the code
        when it's passed as ``kind=``, so the ``kinds`` table is one legend
        for *reading* handles and *choosing* the kind.

        A literal that's already a registered kind always wins (never
        shadowed by a code), and only **record** codes expand: chunk codes
        (``dc``/``pc``) are address-only — you ``get``/``edit`` a chunk by
        its handle, never ``put(kind='dc', …)`` — so a code the registry
        flags ``is_chunk`` is left untouched (and falls through to the
        normal unknown-kind error). Anything that isn't a known code is
        returned verbatim."""
        if kind in self.hub.kinds:
            return kind
        try:
            resolved, is_chunk = handle_registry.kind_for_code(kind)
        except KeyError:
            return kind
        return resolved if not is_chunk else kind

    def _missing_kind_hints(self, verb: str) -> list[str]:
        """Recovery hints for a non-``search`` verb called without ``kind=``.

        Used to lead with :meth:`Store.most_recent_kind` — "you were
        last working on kind=X" — but that query is
        ``ORDER BY refs.updated_at DESC`` over the *whole corpus*, with
        no session/connection scoping column to filter on. On a
        single-tenant deployment that reads as "your session", but this
        MCP is multi-session (many concurrent agents against one prod
        DB — see any ``scripts/inflight`` table): the "most recently
        updated ref" is often another session's write, so the hint
        actively misdirects ("you were last working on kind='alert'" in
        a session that never touched ``alert``, gr311329). Without a
        real per-session touch log there is no session-accurate kind to
        offer, so this now falls straight back to the generic "pick
        one" — honest is better than a plausible-sounding guess. The
        per-verb help-skill pointer is still appended by
        :meth:`_maybe_add_skill_hint` after this.
        """
        return ["pass kind=<one of the listed options>"]

    def _resolve_handler(self, kind: str, verb: str) -> Handler:
        """Look up the handler for ``kind`` and verify it supports ``verb``.

        Raises:
            NotFound: ``kind`` is not registered at all (unknown name).
                Options carries only the verb-supporting kinds so an
                agent retrying against a suggested kind doesn't
                cascade into a second error (MCP critic MAJOR #12).
            Unsupported: handler is registered-but-disabled for this
                build (missing env var, missing optional dep), OR
                handler exists but does not implement ``verb``. The
                first variant names the missing precondition so the
                agent can route to the operator instead of guessing
                — see broad usability pass 2026-05-30 (#8). The
                second variant enumerates the verbs this kind *does*
                support so the recovery hint is sharp.
            Unsupported: the kind is prohibited *for the duration of the
                active in-process tick* (the ContextVar gate, below).
        """
        # Per-tick prohibition (in-process agent loop only): a background pass
        # may disable a kind for one tick via the tick ContextVar — plan_tick
        # gates the draft's colliding prose-file kind so the planner writes into
        # the draft, not a freestanding file. This is the in-process twin of the
        # claude path's PRECIS_KINDS_DISABLED env entry (the spawned MCP server
        # honors *that* at construction; the in-process Hub is built once at
        # boot, so the per-tick prohibition has to be a per-call check). No-op
        # outside a tick — the ContextVar is unset for the MCP server / CLI /
        # tests, so this is byte-identical there.
        tick_hint = _tick_disabled_hint(kind)
        if tick_hint is not None:
            raise Unsupported(
                f"kind {kind!r} is disabled for this tick ({tick_hint})",
                next=tick_hint,
            )
        handler = self.hub.handler_for(kind)
        if handler is None:
            # Distinguish "registered-but-disabled in this build" from
            # "unknown kind". The hub records every gated-out kind in
            # ``loadabilities``; if ``kind`` is in there, the right
            # error class is ``Unsupported`` (the agent can't fix it
            # by retrying — the operator has to enable the kind),
            # and the breadcrumb should name the missing precondition.
            verdict = getattr(self.hub, "loadabilities", {}).get(kind)
            if verdict is not None and not verdict.loaded:
                reason = verdict.reason or "disabled"
                raise Unsupported(
                    f"kind {kind!r} is registered but disabled in this build "
                    f"({reason})",
                    next=(
                        "see get(kind='skill', id='precis-kinds-disabled-help') "
                        "and precis-overview Needs column"
                    ),
                )
            # Broad usability pass 2026-05-30 (#10): the previous
            # ``options:`` trailer silently filtered to kinds that
            # support the calling verb — agents reading the list
            # could conclude the omitted kinds didn't exist at all
            # (precis-help shows 17 total; the options here show
            # 12 for search). Name the filter in ``next:`` so a
            # reader knows the list is verb-scoped, not the full
            # registry.
            verb_kinds = self._kinds_for_verb(verb)
            # Round-2 picky N-2, 2026-05-30: when no kinds support
            # this verb in the current build (e.g. ``edit`` with no
            # file kinds wired — markdown/plaintext/tex/python all
            # need PRECIS_ROOT/PRECIS_PYTHON_ROOTS), the previous
            # breadcrumb said *"options above are kinds that support
            # verb='edit'"* — but no options were printed above, so
            # the agent was told to consult a list that wasn't there.
            # Distinguish the empty case explicitly.
            if verb_kinds:
                next_hint = (
                    f"options above are kinds that support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') for the complete "
                    f"kind table"
                )
            else:
                next_hint = (
                    f"no kinds in this build support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') lists every "
                    f"kind and the verbs each one accepts. The most "
                    f"likely cause is a missing env var "
                    f"(see get(kind='skill', id='precis-kinds-disabled-help'))."
                )
            # Federation hint: if any other process in the cluster
            # currently advertises this kind via ``kind_provider``,
            # name the hosts so the caller knows where to route. Pure
            # informational — the local process still rejects the
            # call. Skipped on stateless boots (no store) and on any
            # query error (kind_provider may be absent on a fresh DB
            # that hasn't run migration 0022 yet).
            route_hint: str | None = None
            store = getattr(self.hub, "store", None)
            if store is not None:
                try:
                    hosts = store.find_kind_providers(kind)
                except Exception:  # pragma: no cover - missing table / DB error
                    hosts = []
                if hosts:
                    route_hint = (
                        f"kind {kind!r} routes through host(s): {', '.join(hosts)}"
                    )
            if route_hint is not None:
                next_hint = f"{route_hint}; {next_hint}"
            raise NotFound(
                f"unknown kind: {kind}",
                options=verb_kinds,
                next=next_hint,
            )

        if not handler.spec.supports(verb):
            verbs = [v for v in _VERBS if handler.spec.supports(v)]
            # ``options`` enumerates the supported verbs as the
            # recovery vocabulary; ``next`` gives a concrete
            # *callable* shape rather than re-listing the same
            # names so the LLM can copy-paste-execute. Pick ``get``
            # as the safest recovery suggestion when available —
            # every kind supports it and a minimum-arg ``get(kind=
            # X)`` either returns a list view (numeric/file kinds)
            # or fails with a kind-specific BadInput pointing at
            # the right next step (calc/math/web/etc. requiring
            # ``q=`` or ``id=``). Either way the LLM lands one
            # call closer to the answer.
            recovery = "get" if "get" in verbs else (verbs[0] if verbs else None)
            # A kind-specific redirect (e.g. how to "link" a draft) beats
            # the generic "try get(kind=…)" — it's the actual recovery the
            # agent needs, so lead with it.
            redirect = _VERB_REDIRECTS.get((kind, verb))
            if recovery is None and redirect is None:
                # Defensive: shouldn't happen — a kind with no
                # supported verbs would be useless. Drop the next:
                # trailer rather than render a meaningless one.
                raise Unsupported(
                    f"{kind} does not support {verb}",
                    options=verbs,
                )
            next_hints: list[str] = []
            if redirect is not None:
                next_hints.append(redirect)
            if recovery is not None:
                next_hints.append(f"try {recovery}(kind={kind!r})")
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=next_hints[0] if len(next_hints) == 1 else next_hints,
            )
        return handler

    def _invoke_handler(
        self,
        handler: Handler,
        verb: str,
        *,
        kind: str,
        kind_was_defaulted: bool,
        args: dict[str, Any],
    ) -> Response:
        """Call ``handler.<verb>`` with extras-whitelisted kwargs.

        ``args=`` extras forwarded by the MCP boundary are validated
        against the handler's signature *before* the call so
        ``**_kw`` doesn't swallow typos silently. Errors raised by
        the handler are annotated with ``(searched kind=…)`` when the
        caller omitted ``kind=`` and we defaulted, so failures stay
        traceable to the specific kind that was tried.
        """
        method = getattr(handler, verb)

        extras = args.pop(_EXTRAS_KEY, None)
        if extras:
            accepted = self._accepted_kwargs(method)
            # Handlers that opt into an explicit ``args: dict``
            # parameter (today: ``random.get`` — slug minting takes
            # ``len`` / ``alphabet`` inside ``args=``) want the whole
            # extras dict passed through, NOT flattened into top-level
            # kwargs. Without this branch, ``get(kind='random',
            # view='slug', args={'len': 4})`` errored with
            # ``args= keys ['len'] not accepted by random.get`` and the
            # error breadcrumb confusingly suggested using ``args`` or
            # ``view`` as args-dict keys (round-2 picky F-1). Detect
            # the opt-in by signature membership and forward extras
            # via the ``args`` kwarg unchanged.
            if "args" in accepted:
                args["args"] = dict(extras)
            else:
                unknown = self._unknown_extras(method, extras)
                if unknown:
                    accepted_kwargs = sorted(k for k in accepted if k not in ("args",))
                    raise BadInput(
                        f"args= keys {unknown!r} not accepted by {kind}.{verb}",
                        options=accepted_kwargs,
                        next=(
                            f"drop the unknown keys; {kind}.{verb} accepts "
                            f"top-level kwargs: {accepted_kwargs or '(none)'}"
                        ),
                    )
                args.update(extras)

        self._apply_default_tags_policy(handler, verb, args)

        # Strip None args so handlers see absence as missing.
        clean = {k: v for k, v in args.items() if v is not None}

        # F7: catch handler-signature-required kwargs that the caller
        # forgot, before ``method(**clean)`` raises a raw TypeError and
        # leaks Python signature internals through the [error:Internal]
        # envelope. Per-handler BadInput paths (e.g. NumericRefHandler.
        # link's "requires target=" check) still fire for *semantic*
        # requirements like "id must be paired with target"; this gate
        # only catches truly-missing keyword-only args with no default.
        missing = self._missing_required_kwargs(method, clean)
        if missing:
            accepted_kwargs = sorted(
                k for k in self._accepted_kwargs(method) if k != "args"
            )
            missing_str = ", ".join(f"{m}=" for m in missing)
            raise BadInput(
                f"{verb}(kind={kind!r}) requires {missing_str}",
                options=accepted_kwargs,
                next=f"get(kind='skill', id='precis-{verb}-help')",
            )

        try:
            response = method(**clean)
        except PrecisError as exc:
            if kind_was_defaulted:
                exc.cause = f"(searched kind={kind!r}) {exc.cause}"
            raise
        except Exception as exc:
            # Non-Precis exceptions get wrapped as ``Internal`` at
            # the dispatcher boundary; do the wrap here when the
            # kind was defaulted so the annotation lands on the
            # final rendered error rather than being lost in the
            # generic ``internal error: ...`` shape.
            if kind_was_defaulted:
                raise Internal(
                    f"(searched kind={kind!r}) internal error: {exc}"
                ) from exc
            raise

        if kind_was_defaulted:
            response = self._tag_defaulted_kind(response, kind)
        return response

    def _apply_default_tags_policy(
        self,
        handler: Handler,
        verb: str,
        args: dict[str, Any],
    ) -> None:
        """Apply ``PRECIS_DEFAULT_TAGS`` policy at the dispatch boundary.

        No-op if ``defaults`` is empty (env unset) or
        ``handler.spec.note_like`` is False (ingested kinds, fetched
        caches, generators don't accumulate session-context tags).
        Verb ``put`` on a note-like kind: merges defaults into
        ``args['tags']`` (caller's explicit-first ordering preserved, no
        dupes) and emits an info hint. Verb ``tag``: emits a suggestion
        hint for defaults missing from ``args.get('add')`` — **not**
        mutated, since ``tag`` is the agent's explicit op and silent
        mutation would surprise both agent and operator. Any other verb
        (get/search/edit/delete/link): no-op.

        Mutates ``args`` in place (``put`` only); returns ``None``.
        """
        defaults = self.default_tags_resolved
        if not defaults:
            return
        spec = handler.spec
        if not getattr(spec, "note_like", False):
            return

        from precis import default_tags as _dt
        from precis.hints import Hint

        if verb == "put":
            added = _dt.apply_to_put_args(args, defaults)
            if added:
                self.hub.emit_hint(
                    Hint(
                        text=("Added PRECIS_DEFAULT_TAGS to put: " + ", ".join(added)),
                        topic="default_tags.merged",
                    )
                )
        elif verb == "tag":
            missing = _dt.suggest_missing(args.get("add"), defaults)
            if missing:
                self.hub.emit_hint(
                    Hint(
                        text=(
                            "PRECIS_DEFAULT_TAGS suggested for tag add: "
                            + ", ".join(missing)
                        ),
                        topic="default_tags.suggested",
                    )
                )

    @staticmethod
    def _accepted_kwargs(method: Any) -> set[str]:
        """Return the set of explicit keyword names accepted by ``method``.

        ``self`` and any VAR_KEYWORD (``**kw``) catch-all are excluded
        — the catch-all is what we're working around. Used for the
        args= validation gate so ``**_kw`` no longer swallows typos.
        """
        sig = inspect.signature(method)
        return {
            name
            for name, p in sig.parameters.items()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name != "self"
        }

    @classmethod
    def _unknown_extras(cls, method: Any, extras: dict[str, Any]) -> list[str]:
        """Return the args= keys that aren't on the handler's signature."""
        accepted = cls._accepted_kwargs(method)
        return sorted(k for k in extras if k not in accepted)

    @staticmethod
    def _missing_required_kwargs(method: Any, clean: dict[str, Any]) -> list[str]:
        """Return required kwargs of ``method`` missing from ``clean``.

        A parameter is required when it has no default value AND is
        keyword-accessible (``POSITIONAL_OR_KEYWORD`` or ``KEYWORD_ONLY``).
        ``self`` and the magic ``args`` extras-passthrough parameter
        are excluded; ``**kw`` catch-alls don't count as required.

        Used by :meth:`_invoke_handler` to convert what would have been
        a raw ``TypeError: ... missing 1 required keyword-only
        argument: 'id'`` into a clean ``BadInput`` envelope (F7).
        """
        sig = inspect.signature(method)
        missing: list[str] = []
        for name, p in sig.parameters.items():
            if name in ("self", "args"):
                continue
            if p.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            if name not in clean:
                missing.append(name)
        return missing

    def _render(self, response: Response) -> str:
        out = [response.body]
        hints = self.hints.collect()
        for h in hints:
            out.append(f"\n[{h.level}] {h.text}")
        if response.cost:
            # Handlers return a fully-formatted cost string like
            # ``[cost: ~$0.0020 — cached]``. Don't prepend "— cost:" —
            # that produced the double-"cost:" trailer flagged by the
            # MCP critic ("— cost: [cost: ~$0.0020]").
            out.append(f"\n{response.cost}")
        return "".join(out)

    def _default_search_kind(self, search_kinds: list[str]) -> str | None:
        """Pick a sensible default kind for ``search()`` calls without one.

        Strategy:
          1. If the store has any live ref in a search-supporting
             kind, use the kind of the most recently updated one —
             this biases the default toward "what was the agent just
             working on?".
          2. Otherwise (no store, empty store), return None and let
             the caller raise the canonical missing-kind error.

        Returning None signals "I don't have a defensible default" so
        the dispatcher falls through to the explicit BadInput.
        """
        if self.store is None or not search_kinds:
            return None
        try:
            return self.store.most_recent_kind(kinds=search_kinds)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("most_recent_kind lookup failed")
            return None

    @staticmethod
    def _tag_defaulted_kind(response: Response, kind: str) -> Response:
        """Prepend a ``(searched kind=...)`` annotation to a response.

        Surfaced when the caller omitted ``kind=`` and the runtime
        defaulted to the most recently touched search-supporting
        kind. Naming it explicitly lets the caller see and steer
        the choice on retry.
        """
        annotated = f"(searched kind={kind!r})\n{response.body}"
        return Response(body=annotated, cost=response.cost)

    def _kinds_for_verb(self, verb: str) -> list[str]:
        """Return the active kinds whose KindSpec supports ``verb``.

        Used by error paths so an "unknown kind" reply on a search
        request lists only kinds that *do* support search — agents
        that retry against the suggested options shouldn't cascade
        into a second error. (MCP critic MAJOR #12.)
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            handler = self.hub.handler_for(k)
            assert handler is not None  # every kind in hub.kinds has a handler
            if handler.spec.supports(verb):
                out.append(k)
        return out

    def _infer_sigil_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a leading address sigil (``¶`` → draft,
        ``§`` → paper). ``¶`` is kept in ``id`` (the draft handler matches
        on it); ``§`` is stripped to the bare ``slug~n`` paper resolves.

        Leaves ``id`` alone when the implied kind isn't in this build
        (fail downstream with a real "no such kind", not a silent
        mis-route). Raises on an explicit ``kind=`` that contradicts the
        sigil, mirroring the colon-prefix conflict check.
        """
        kind, keep_sigil = _SIGIL_KIND[ident[:1]]
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind not in live_kinds:
            return
        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != kind:
            raise BadInput(
                f"id={ident!r} sigil implies kind={kind!r}, "
                f"conflicts with kind={existing_kind!r}",
                next=f"drop kind= — id={ident!r} already names the kind",
            )
        args["kind"] = kind
        if not keep_sigil:
            args["id"] = ident[1:]

    def _infer_slug_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a bare slug address (no ``kind:`` prefix, no
        sigil) when the slug uniquely identifies one live ref — e.g.
        ``wu22c~312`` → ``kind='paper'``. The ``~selector`` / ``/view``
        suffix stays on ``id`` for the handler to parse. No-op when the
        store is absent, ``kind=`` was already given, or the base slug is
        ambiguous / unknown (so a non-slug id falls through to the normal
        missing-kind error unchanged)."""
        if args.get("kind") is not None or self.store is None:
            return
        base = re.split(r"[~/?]", ident, maxsplit=1)[0].strip()
        if not base:
            return
        try:
            kind = self.store.kind_for_slug(base)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("kind_for_slug lookup failed")
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind is not None and kind in live_kinds:
            args["kind"] = kind

    def _maybe_route_draft_chunk(self, args: dict[str, Any], ident: str) -> bool:
        """Universal handles: route a draft chunk handle ``dc<id>`` (optionally with a
        relative operator ``^`` / ``+N`` / ``-lo..hi``) to the draft handler,
        which resolves it (drafts have no slug, so they can't go through the
        generic ``slug~ord`` chunk-handle rewrite). Confirms the base chunk
        exists so a bogus ``dc999`` falls through to a clean not-found.
        Returns ``True`` if it routed."""
        if self.store is None:
            return False
        m = _DRAFT_DC_RE.match(ident)
        if m is None:
            return False
        # A trailing operator must be a valid relative handle, else this is
        # not a draft address (``dc42garbage`` falls through).
        if m.group(2) and handle_registry.parse_relative(ident) is None:
            return False
        explicit = args.get("kind")
        if explicit is not None and explicit != "draft":
            return False
        try:
            if self.store.drafts.get_draft_chunk("dc" + m.group(1)) is None:
                return False
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("draft chunk routing lookup failed")
            return False
        args["kind"] = "draft"
        args["id"] = ident
        return True

    def _maybe_infer_kind_from_relative(self, args: dict[str, Any], ident: str) -> bool:
        """Relative-handle navigation: route ``pc10+1`` / ``pc10-2..3``.

        Resolves the relative chunk handle to its kind + the per-kind chunk
        selector (e.g. ``slug~ord`` for a paper) and rewrites ``args`` so the
        existing per-kind ``get`` renders the target with no change. Returns
        ``True`` if it routed a relative handle, ``False`` otherwise (not a
        relative handle, unresolvable, out of range, or an explicit ``kind=``
        that disagrees) so the caller falls through untouched.
        """
        if self.store is None:
            return False
        try:
            resolved = self.store.chunks.resolve_relative(ident)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("resolve_relative lookup failed")
            return False
        if resolved is None:
            return False
        kind, selector = resolved
        explicit = args.get("kind")
        if explicit is not None and explicit != kind:
            return False
        args["kind"] = kind
        args["id"] = selector
        return True

    def _kind_supports_chunk_selectors(
        self, kind: str, public_id: str, ref_id: int
    ) -> bool:
        """Whether ``kind``'s ``get`` understands the ``slug~ord`` chunk
        selector this dispatcher synthesizes for a resolved chunk handle.

        Reads the explicit :attr:`KindSpec.supports_chunk_selectors` opt-in
        when the handler has declared it; falls back to the historical
        public_id-vs-ref_id numeric proxy (True for slug-addressed kinds,
        False for numeric-id kinds) when it hasn't. The fallback preserves
        existing routing for every hand-rolled slug-document handler
        (paper/patent/edgar/markdown/plaintext/tex/…) without requiring each
        one to declare the flag — but it's no longer the *only* signal: a
        kind that IS slug-addressed but whose ``get`` has zero ``~ord``
        grammar (gr311336: news, via ``CacheBackedHandler``) can now declare
        ``supports_chunk_selectors=False`` explicitly to opt out, and a
        kind that implements the grammar (news, once fixed) can declare
        ``True`` regardless of its id shape.
        """
        handler = self.hub.handler_for(kind) if self.hub is not None else None
        declared = (
            getattr(handler.spec, "supports_chunk_selectors", None) if handler else None
        )
        if declared is not None:
            return declared
        return public_id != str(ref_id)

    def _maybe_infer_kind_from_handle(self, args: dict[str, Any], ident: str) -> bool:
        """Universal-handle surface dispatch: route a universal handle.

        If ``ident`` is a well-formed, resolvable record handle, sets
        ``args['kind']`` from its 2-char type code and rewrites
        ``args['id']`` to the per-kind public id (slug or ``str(ref_id)``)
        so the existing handler resolves it unchanged. A trailing
        chunk/view selector (``pa123~0..5``, ``pa123/toc``) is reattached
        to the public id, parsed as on a slug.

        A soft-deleted/never-existed handle has nothing for
        ``resolve_handle`` (a live-row lookup) to return, but still
        *parses* — the syntactic fallback below routes those to the
        per-kind handler instead of bare-slug inference (gr192827: a
        soft-deleted gripe's own handle produced a misleading "id must be
        an integer" instead of ``Gone``).

        Returns ``True`` if routed, ``False`` otherwise (non-handle,
        unknown/chunk handle, or a disagreeing explicit ``kind=``), so
        the caller falls through to bare-slug inference untouched.
        """
        if self.store is None:
            return False
        # Split off a trailing ``~selector`` / ``/view`` so the base handle
        # parses; the suffix is reattached to the resolved public id below.
        mm = re.match(r"^([a-zA-Z]{2}\d+)([~/].*)$", ident.strip())
        base, suffix = (mm.group(1), mm.group(2)) if mm else (ident, "")
        normalized = handle_registry.normalize(base)
        if not handle_registry.is_well_formed(normalized):
            return False
        resolved = self.store.resolve_handle(normalized)
        if resolved is None:
            return self._maybe_route_unresolved_record_handle(args, normalized, suffix)
        explicit = args.get("kind")
        if explicit is not None and explicit != resolved.kind:
            return False
        if resolved.chunk_id is not None:
            # Chunk handle → per-kind chunk selector. Slug-document kinds take
            # ``slug~ord`` (ChunkRow.ord == chunks.ord). Numeric-chunk kinds
            # (gripe/message/…) have no ``~ord`` selector yet, so fall through
            # (a chunk handle has no slug match → natural NotFound). A chunk
            # handle takes no further selector.
            if (
                suffix
                or resolved.chunk_ord is None
                or not self._kind_supports_chunk_selectors(
                    resolved.kind, resolved.public_id, resolved.ref_id
                )
            ):
                return False
            args["kind"] = resolved.kind
            args["id"] = f"{resolved.public_id}~{resolved.chunk_ord}"
            return True
        # resolve_handle already emitted the merge-redirect hint (via the
        # store's wired hint bus) if it followed a supersede; nothing to do here.
        args["kind"] = resolved.kind
        args["id"] = resolved.public_id + suffix
        return True

    def _maybe_route_unresolved_record_handle(
        self, args: dict[str, Any], normalized: str, suffix: str
    ) -> bool:
        """Syntactic fallback for a well-formed record handle whose row
        ``resolve_handle`` can't find (soft-deleted or never existed).

        ``resolve_handle`` is a DB lookup, so it returns ``None`` for both
        a soft-deleted ref and a never-existed ref_id. Without this
        fallback both fell through to bare-slug inference, landing on
        ``_coerce_id``'s "id must be an integer" instead of the accurate
        ``Gone``/``NotFound`` the per-kind handler would raise given the
        bare integer id (gr192827).

        :func:`handle_registry.parse` needs no DB row — decodes the
        2-char code + decimal body from the string alone — so it still
        yields ``(kind, is_chunk, pk)``. Routed only for non-chunk
        handles (a chunk has no per-kind selector to synthesize without
        the row). Kinds where ``KindSpec.is_numeric`` (memory, todo,
        gripe, …) have a public id that IS ``str(ref_id)``, so
        ``str(pk)`` works as ``id=`` the same way a live handle's
        ``public_id`` would — routed through to the per-kind handler so
        it can still tell a soft-deleted row (``Gone``) from a
        never-existed one (``NotFound``). Slug-addressed kinds (e.g.
        paper) have no live row to read a slug from, so there's nothing
        to hand the per-kind handler — raise the honest ``NotFound``
        directly instead of falling through to bare-slug inference
        (which also finds nothing) and on to a "missing kind=" ``
        BadInput`` a caller can't act on by adding ``kind=`` (gr311329:
        a nonexistent ``pa5`` misdirected the caller to spell out a
        kind= that was never the problem). Slug kinds have no
        Gone-detection today regardless of routing (``get_ref`` already
        filters out soft-deleted rows before any handler sees them), so
        raising here loses nothing.
        """
        parsed = handle_registry.parse(normalized)
        if parsed is None:
            return False
        kind, is_chunk, pk = parsed
        if is_chunk:
            return False
        explicit = args.get("kind")
        if explicit is not None and explicit != kind:
            return False
        handler = self.hub.handler_for(kind) if self.hub is not None else None
        if handler is None:
            return False
        if not handler.spec.is_numeric:
            raise NotFound(
                f"{kind} {normalized + suffix!r} not found",
                next=f"search(kind={kind!r}, q='...') to find existing",
            )
        args["kind"] = kind
        args["id"] = str(pk) + suffix
        return True

    def _maybe_split_prefixed_id(self, args: dict[str, Any]) -> None:
        """D1: extract a self-identifying kind from ``id=`` into
        ``args['kind']``.

        Two grammars: ``kind:identifier[~selector]`` colon prefix (same
        grammar ``link=``/``unlink=`` use — ``id='paper:chung19~4'`` →
        ``kind='paper', id='chung19~4'``; ``id='chung19~4'`` unchanged, no
        colon) or a leading **address sigil**, which *is* the kind tag
        (``id='¶YP377G'`` → ``kind='draft'``, sigil kept;
        ``id='§chung19~4'`` → ``kind='paper'``, sigil stripped).

        Fires only when ``id`` is a string with exactly one ``:`` before
        any ``/``/``~``/``?``, the prefix is a live kind, and any
        already-set ``kind=`` matches the prefix (else ``BadInput``, never
        silent override). Path views (``id='/recent'``) are skipped.
        Anything else passes through unchanged.
        """
        ident = args.get("id")
        if not isinstance(ident, str):
            return
        # Address sigil (``¶`` draft chunk, ``§`` paper citation) is
        # self-identifying — route it before the colon logic. The draft
        # skill documents ``get(id='¶handle')`` with no ``kind=``.
        if ident[:1] in _SIGIL_KIND:
            self._infer_sigil_kind(args, ident)
            return
        # Leading slash → path view (/recent etc.). Don't extract.
        if ident.startswith("/"):
            return
        if ":" not in ident:
            # a draft chunk handle ``dc<id>`` (optionally with a
            # ``-B+A`` reading window) routes to the draft handler, which
            # parses the window. Drafts have no slug, so the generic
            # chunk-handle path below can't rewrite them to ``slug~ord``.
            if self._maybe_route_draft_chunk(args, ident):
                return
            # Universal handles relative navigation: ``pc10+1`` / ``pc10-2..3`` /
            # ``pc10^`` resolves against current structure to a per-kind
            # chunk selector. Try this before the absolute-handle path
            # (a relative handle is not a well-formed absolute one).
            if self._maybe_infer_kind_from_relative(args, ident):
                return
            # a universal handle (``<2-char code><decimal id>``)
            # self-identifies — resolve it to (kind, public_id) before the
            # bare-slug fallback, so it isn't mis-read as a slug.
            if self._maybe_infer_kind_from_handle(args, ident):
                return
            # No ``kind:`` prefix and no sigil — try resolving a bare
            # slug (optionally with a ``~selector`` / ``/view``) to its
            # owning kind, so ``get(id='wu22c~312')`` self-identifies as
            # the paper that owns ``wu22c``.
            self._infer_slug_kind(args, ident)
            return
        # Only honour a colon that comes before any selector / view
        # path separator. ``markdown:notes/a.md`` is fine; ``foo/bar:x``
        # is not — the colon there isn't a kind prefix.
        for sep in ("/", "?"):
            if sep in ident and ident.find(sep) < ident.find(":"):
                return
        prefix, _, rest = ident.partition(":")
        prefix = prefix.strip()
        if not prefix or not rest:
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if prefix not in live_kinds:
            # Not a recognised kind prefix — leave the value alone.
            # Better to surface a "no such kind" error downstream than
            # eat a legitimate identifier that happens to contain ":".
            return

        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != prefix:
            raise BadInput(
                f"id={ident!r} prefix kind={prefix!r} conflicts with "
                f"kind={existing_kind!r}",
                next=(
                    f"drop one: either pass id={rest!r} with "
                    f"kind={prefix!r}, or pass id={ident!r} without kind="
                ),
            )
        args["kind"] = prefix
        args["id"] = rest
