"""Per-kind introspection views: ``view='meta'`` and ``view='tags'``.

mcp-introspection gap closures (2026-10-14, gr484241): session mining
showed agents answering "what meta keys does a kind carry" and "which
tag values are in use" by dumping raw ``refs``/``ref_tags`` through
``scripts/prod-psql`` (94 + 15 detours in a 24h window). These two
dispatch-level views serve the same questions over the MCP surface:

* ``search(kind=K, view='meta')`` — the meta keys in use on live refs
  of one kind, with their jsonb type, the count of refs carrying the
  key, and a bounded example value. ``args={'key': 'params'}`` drills
  one level into an object-valued key. The inventory changes on the
  timescale of a feature, not a call, so the render is cached
  in-process (``_META_VIEW_TTL_S``); the cached-at timestamp is shown
  so a stale read is honest.
* ``search(kind=K, view='tags')`` — per axis/value counts over
  ``ref_tags`` for live refs of one kind. ``status=`` narrows the
  counted set to one STATUS value; ``tags=`` filters it by the normal
  tag grammar. Expired ``ref_tags`` rows are excluded everywhere.

Both are single-kind, read-only and bounded — the same shape the
``view='count'`` interception established.

A third member joined later: :meth:`_dispatch_generic_count` serves
``search(kind=K, view='count')`` for kinds whose handler owns no native
count view (gr593053 — the slug-addressed block kinds' ``search``
signatures never declared ``view``, so the weekly report's
"pres: N / structure: N / …" line died on "does not accept ['view']").
It answers the same question at the refs level through
``store.count_refs_facets`` — live-ref totals, ``tags=``/``status=``
filters, ``since=``/``until=`` created_at bounds and ``group_by=``
facets — and refuses what a ref-level count can't honour (``q=`` block
matches) instead of silently counting the wrong set.

A fourth member is the *flow* view (gr633906):
``search(kind=K, view='flow', since=…, until=…, args={'bucket': 'day'})``
reports opened vs closed per bucket plus open-to-close time
percentiles — the "how fast are gripes burning down" question the
coordinator used to answer through ad-hoc ``ref_tags``/``tags`` SQL.
"Closed" is a per-kind set of terminal ``STATUS:`` values
(:data:`_FLOW_CLOSED`, overridable with ``args={'closed': …}``);
bucket membership reads the *set-time* of the ref's current terminal
tag row, so a closed-then-reopened ref counts only its latest close.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.errors import BadInput, Unsupported
from precis.response import Response
from precis.runtime._shared import RuntimeShape
from precis.store._tag_filter import build_tag_filter
from precis.store.types import _KIND_STATUS_VALUES, _STATUS_VALUES_ANY_KIND
from precis.utils.next_block import render_next_section
from precis.utils.secret_scan import mask_secrets

#: In-process TTL for the meta-key inventory — it describes the
#: corpus's *shape*, which changes on deploy/feature timescales.
_META_VIEW_TTL_S = 900.0
#: Rows rendered by view='meta' (key × type pairs can exceed key count).
_META_VIEW_LIMIT = 60
#: Rows rendered by view='tags' (axis × value pairs).
_TAGS_VIEW_LIMIT = 200
#: Example values are a sample, not a dump.
_META_EXAMPLE_CHARS = 60
#: Bucket rows rendered by view='flow' — past this the tail folds into
#: a summed ``… +K more buckets`` line (the totals line carries the rest).
_FLOW_MAX_BUCKETS = 90
#: ``args={'bucket': …}`` vocabulary for view='flow' — date_trunc units.
_FLOW_BUCKETS: frozenset[str] = frozenset({"hour", "day", "week", "month"})
#: Default flow window when neither ``since=`` nor ``until=`` is given.
_FLOW_DEFAULT_WINDOW = timedelta(days=7)
#: Terminal ``STATUS:`` values each kind counts as "closed" on the flow
#: view by default — gripe/todo/job match their lifecycle vocabularies
#: (``precis.todo.TERMINAL_TODO_STATUSES``, ``handlers.job``'s terminal
#: tuple); ``args={'closed': …}`` overrides per call.
_FLOW_CLOSED: dict[str, tuple[str, ...]] = {
    "gripe": ("done", "wontfix"),
    "todo": ("done", "won't-do", "auto-timeout"),
    "job": ("succeeded", "failed", "cancelled", "timed-out"),
    "quest": ("abandoned",),
}
#: Closed-set fallback for a kind with no entry above: the todo/gripe-
#: family closers, intersected with the kind's own STATUS vocabulary
#: (finding keeps ``won't-do``; a kind accepting the union takes all
#: three). An empty result refuses — the caller names ``closed=``.
_FLOW_CLOSED_FALLBACK: tuple[str, ...] = ("done", "wontfix", "won't-do")


def _flow_duration(seconds: Any) -> str:
    """``42s`` / ``19m`` / ``19h`` / ``4.3d`` — a coarse duration for the
    flow view's time-to-close percentiles."""
    if seconds is None:
        return "—"
    s = max(0.0, float(seconds))
    if s < 120:
        return f"{s:.0f}s"
    if s < 2 * 3600:
        return f"{s / 60:.0f}m"
    unit = "h" if s < 2 * 86400 else "d"
    v = s / (3600 if unit == "h" else 86400)
    return f"{v:.1f}".rstrip("0").rstrip(".") + unit


class IntrospectionMixin(RuntimeShape):
    """``view='meta'`` / ``view='tags'`` / ``view='flow'`` render + cache."""

    # ── dispatch entry point ────────────────────────────────────

    def _dispatch_introspect_view(
        self, view: str, kind: str | None, args: dict[str, Any]
    ) -> Response:
        """Route ``search(kind=K, view='meta'|'tags')``.

        Single-kind only, like ``view='count'`` — a cross-kind key/tag
        inventory is a different (and much noisier) question.
        """
        kind_s = (kind or "").strip()
        if not kind_s or self._is_cross_kind_request(kind_s):
            raise BadInput(
                f"view='{view}' reports one kind at a time — pass kind=",
                options=sorted(self.hub.kinds),
                next=f"search(kind='job', view='{view}')",
            )
        kind_s = self._expand_kind_code(kind_s)
        # The inventory reads store rows only, so a kind whose handler is
        # dark here (plugin extra not installed: ``pathway`` without
        # autocatpath) is still inspectable; ``loadabilities`` records it.
        if kind_s not in self.hub.kinds and kind_s not in self.hub.loadabilities:
            raise BadInput(
                f"unknown kind {kind_s!r} for view='{view}'",
                options=sorted(self.hub.kinds),
            )
        if self.store is None:
            raise Unsupported(f"view='{view}' needs a store-backed deployment")
        if kind_s == "profile":
            # Owner-only read (gr490266): the inventory aggregates every
            # owner's rows and samples meta values (palette, logos), so an
            # identified user may not run it across other owners' profiles.
            from precis import profiles

            if profiles.acting_login(self.store) is not None:
                raise BadInput(
                    f"view='{view}' is not available on kind='profile' for a "
                    "signed-in user — it spans every owner's profiles",
                    next="get(kind='profile') lists your own profiles",
                )
        raw_extras = args.get("__extras__")
        extras = raw_extras if isinstance(raw_extras, dict) else {}
        if view == "flow":
            return self._flow_view(kind_s, args, extras)
        bad = next(
            (
                a
                for a in ("angle", "like", "sort", "since", "until", "q", "link")
                if args.get(a) is not None or extras.get(a) is not None
            ),
            None,
        )
        if bad is not None:
            raise BadInput(
                f"{bad}= is not supported with view='{view}'",
                next=f"search(kind={kind_s!r}, view='{view}')",
            )
        if view == "meta":
            return self._meta_view(kind_s, extras)
        return self._tags_view(kind_s, args, extras)

    # ── view='meta' ─────────────────────────────────────────────

    def _meta_view(self, kind: str, extras: dict[str, Any]) -> Response:
        key = extras.get("key")
        limit = extras.get("limit", _META_VIEW_LIMIT)
        try:
            limit_n = min(max(1, int(limit)), 200)
        except (TypeError, ValueError):
            raise BadInput(f"args.limit must be an int, got {limit!r}") from None
        unknown = sorted(set(extras) - {"key", "limit"})
        if unknown:
            raise BadInput(
                f"args= keys {unknown!r} not supported with view='meta'",
                options=["key", "limit"],
            )
        cache_key = (kind, str(key) if key is not None else None)
        cached = self._meta_cache_get(cache_key)
        if cached is not None:
            return Response(body=cached)

        assert self.store is not None  # gate above
        with self.store.pool.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = %s AND retired_at IS NULL "
                "AND (kind <> 'agentlog' OR meta->>'trace' IS DISTINCT FROM 'search')",
                (kind,),
            ).fetchone()
            if key is None:
                rows = conn.execute(
                    """
                    SELECT e.key, jsonb_typeof(e.value) AS typ, count(*) AS n,
                           min(left(e.value::text, %s)) AS example
                      FROM refs r, jsonb_each(coalesce(r.meta, '{}'::jsonb)) e
                     WHERE r.kind = %s AND r.retired_at IS NULL
                       AND (r.kind <> 'agentlog' OR r.meta->>'trace' IS DISTINCT FROM 'search')
                     GROUP BY e.key, jsonb_typeof(e.value)
                     ORDER BY n DESC, e.key
                     LIMIT %s
                    """,
                    (_META_EXAMPLE_CHARS, kind, limit_n),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT e.key, jsonb_typeof(e.value) AS typ, count(*) AS n,
                           min(left(e.value::text, %s)) AS example
                      FROM refs r,
                           jsonb_each(r.meta -> %s) e
                     WHERE r.kind = %s AND r.retired_at IS NULL
                       AND (r.kind <> 'agentlog' OR r.meta->>'trace' IS DISTINCT FROM 'search')
                       AND jsonb_typeof(r.meta -> %s) = 'object'
                     GROUP BY e.key, jsonb_typeof(e.value)
                     ORDER BY n DESC, e.key
                     LIMIT %s
                    """,
                    (_META_EXAMPLE_CHARS, key, kind, key, limit_n),
                ).fetchall()

        n_live = int(total[0]) if total else 0
        scope = f"meta['{key}']" if key is not None else "meta"
        live = f"{n_live} live ref{'s' if n_live != 1 else ''}"
        lines = [f"# {scope} keys in use — {kind} ({live})"]
        if not rows:
            lines.append(
                f"(no {scope} keys on live {kind} refs)"
                if key is None
                else f"(meta['{key}'] is not an object on live {kind} refs)"
            )
        for k, typ, n, example in rows:
            ex = mask_secrets(str(example) if example is not None else "", warn=False)
            lines.append(f"  {k}  {typ}  {int(n)}  {ex}")
        lines.append("")
        lines.append(
            render_next_section(
                [
                    (
                        "args={'key': '<object-key>'}",
                        "drill one level into an object-valued key",
                    ),
                    (
                        f"search(kind='{kind}', view='tags')",
                        "tag axis/value counts on the same kind",
                    ),
                    (
                        f"search(kind='{kind}', view='count')",
                        "filtered totals + group_by facets",
                    ),
                ]
            ).lstrip("\n")
        )
        body = "\n".join(lines)
        self._meta_cache_put(cache_key, body)
        return Response(body=body)

    def _meta_cache_get(self, key: tuple[str, str | None]) -> str | None:
        entry = getattr(self, "_meta_view_cache", {}).get(key)
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]

    def _meta_cache_put(self, key: tuple[str, str | None], body: str) -> None:
        cache = getattr(self, "_meta_view_cache", None)
        if cache is None:
            cache = {}
            self._meta_view_cache = cache
        if len(cache) > 256:
            cache.clear()
        cache[key] = (time.monotonic() + _META_VIEW_TTL_S, body)

    # ── view='tags' ─────────────────────────────────────────────

    def _tags_view(
        self, kind: str, args: dict[str, Any], extras: dict[str, Any]
    ) -> Response:
        status = args.get("status") or extras.get("status")
        tags_arg = args.get("tags") or extras.get("tags")
        filter_tags: list[str] = []
        if isinstance(tags_arg, str):
            filter_tags = [tags_arg]
        elif isinstance(tags_arg, (list, tuple)):
            filter_tags = [str(t) for t in tags_arg]
        if status is not None and str(status).strip() and status != "*":
            filter_tags.append(f"STATUS:{str(status).strip()}")

        assert self.store is not None  # gate above
        clauses = [
            "r.retired_at IS NULL",
            "r.kind = %s",
            "(r.kind <> 'agentlog' OR r.meta->>'trace' IS DISTINCT FROM 'search')",
        ]
        params: list[Any] = [kind]
        if filter_tags:
            frag, frag_params = build_tag_filter(filter_tags, ref_alias="r")
            if frag:
                clauses.append(frag)
                params.extend(frag_params)
        with self.store.pool.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM refs r WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT t.namespace,
                       CASE WHEN t.namespace IN ('OPEN', 'FLAG') THEN t.value
                            ELSE t.namespace || ':' || t.value END AS label,
                       count(DISTINCT rt.ref_id) AS n
                  FROM ref_tags rt
                  JOIN tags t ON t.tag_id = rt.tag_id
                  JOIN refs r ON r.ref_id = rt.ref_id
                 WHERE (rt.expires_at IS NULL OR rt.expires_at > now())
                   AND {" AND ".join(clauses)}
                 GROUP BY t.namespace, label
                 ORDER BY t.namespace, n DESC, label
                 LIMIT %s
                """,
                [*params, _TAGS_VIEW_LIMIT],
            ).fetchall()

        n_live = int(total[0]) if total else 0
        filt = f" (filtered: {filter_tags!r})" if filter_tags else ""
        live = f"{n_live} live ref{'s' if n_live != 1 else ''}"
        lines = [f"# tag counts — {kind} ({live}){filt}"]
        if not rows:
            lines.append(f"(no live {kind} refs carry tags{filt})")
        axis = None
        for ns, label, n in rows:
            if ns != axis:
                axis = ns
                lines.append(f"## {ns}")
            lines.append(f"  {label}  {int(n)}")
        if len(rows) >= _TAGS_VIEW_LIMIT:
            lines.append(f"… (top {_TAGS_VIEW_LIMIT} tag values by count)")
        lines.append("")
        lines.append(
            render_next_section(
                [
                    (
                        f"search(kind='{kind}', tags=['<value>'])",
                        "list the refs carrying a tag value",
                    ),
                    (
                        f"search(kind='{kind}', view='meta')",
                        "meta keys in use on the same kind",
                    ),
                ]
            ).lstrip("\n")
        )
        return Response(body="\n".join(lines))

    # ── view='flow' (gr633906) ───────────────────────────────────

    #: Top-level args a flow read can't honour — named, not swallowed.
    #: ``under=``/``folder=`` arrive already folded into
    #: ``include_ref_ids`` at dispatch, so they aren't listed here.
    _FLOW_UNSUPPORTED_TOP: tuple[str, ...] = (
        "q",
        "link",
        "mode",
        "scope",
        "page",
        "page_size",
        "n",
        "k",
        "limit",
        "offset",
        "depth",
        "axis",
        "angle",
        "like",
        "sort",
        "id_min",
        "id_max",
        "uncited",
        "cited",
        "hubbed",
    )
    #: args= keys the flow view reads — ``since``/``until``/``tags``/
    #: ``status`` ride extras on the job listing spelling too.
    _FLOW_ALLOWED_EXTRAS: frozenset[str] = frozenset(
        {
            "bucket",
            "closed",
            "since",
            "until",
            "tags",
            "status",
            "job_type",
            "include_ref_ids",
            "exclude_ref_ids",
        }
    )

    def _flow_view(
        self, kind: str, args: dict[str, Any], extras: dict[str, Any]
    ) -> Response:
        """``search(kind=K, view='flow')`` — opened vs closed per bucket.

        The "is the backlog burning down" read (gr633906): per-bucket
        opens (``refs.created_at``), closes (the set-time of a live
        ref's *current* terminal ``STATUS:`` tag) and net, plus
        open-to-close percentiles over the window's closes and a live
        "open now" snapshot. Works on any STATUS-carrying kind —
        ``_FLOW_CLOSED`` names each kind's terminal values;
        ``args={'closed': 'done,wontfix'}`` (or a list) overrides.

        ``since=``/``until=`` bound the *event* time on both sides —
        a ref minted before the window but closed inside it still
        counts in its close bucket. ``_job_window_bound`` parses, so
        the age spellings (``'7d'``/``'24'``/ISO) the job listing
        takes work here too; no bound at all defaults to the last
        :data:`_FLOW_DEFAULT_WINDOW`. ``args={'bucket': 'hour'|'day'|
        'week'|'month'}`` picks the granularity (default ``day``);
        ``tags=``/``status=``, ``args={'job_type': …}`` and the resolved
        ``*_ref_ids`` scope channels (``under=``/``folder=``) filter the
        cohort on both sides.

        Honest limit, printed on the response: ``ref_tags`` keeps one
        row per (ref, tag) — a transition deletes the old row — so a
        close measures the *latest* terminal set-time on a ref that is
        still closed. Reopened-and-reclosed refs count their latest
        close only; a close followed by a reopen counts nothing.
        """
        from precis.handlers.job import _job_window_bound

        assert self.store is not None  # gate in _dispatch_introspect_view
        bad = next(
            (
                a
                for a in self._FLOW_UNSUPPORTED_TOP
                if args.get(a) is not None or extras.get(a) is not None
            ),
            None,
        )
        if bad is not None:
            raise BadInput(
                f"{bad}= is not supported with view='flow'",
                next=(
                    "view='flow' filters with tags=/status=/since=/until= and "
                    "args={'bucket': 'day', 'closed': ['done', …]}"
                ),
            )
        unknown = sorted(set(extras) - self._FLOW_ALLOWED_EXTRAS)
        if unknown:
            raise BadInput(
                f"args= keys {unknown!r} not supported with view='flow' "
                f"on kind={kind!r}",
                options=sorted(self._FLOW_ALLOWED_EXTRAS),
            )
        bucket = str(extras.get("bucket") or "day").strip().lower()
        if bucket not in _FLOW_BUCKETS:
            raise BadInput(
                f"args.bucket must be one of {sorted(_FLOW_BUCKETS)}, "
                f"got {extras.get('bucket')!r}",
                options=sorted(_FLOW_BUCKETS),
            )
        closed = self._flow_closed_values(kind, extras.get("closed"))
        since_dt = _job_window_bound(
            args.get("since") if args.get("since") is not None else extras.get("since"),
            "since",
        )
        until_dt = _job_window_bound(
            args.get("until") if args.get("until") is not None else extras.get("until"),
            "until",
        )
        defaulted_window = since_dt is None and until_dt is None
        if defaulted_window:
            since_dt = datetime.now(UTC) - _FLOW_DEFAULT_WINDOW
        if since_dt is not None and until_dt is not None and since_dt >= until_dt:
            raise BadInput(
                f"since={since_dt.isoformat()} is not before "
                f"until={until_dt.isoformat()}",
                next="widen the window, or drop a bound",
            )

        filter_tags: list[str] = []
        tags_arg = (
            args.get("tags") if args.get("tags") is not None else extras.get("tags")
        )
        if isinstance(tags_arg, str):
            filter_tags = [tags_arg]
        elif isinstance(tags_arg, (list, tuple)):
            filter_tags = [str(t) for t in tags_arg]
        status = (
            args.get("status")
            if args.get("status") is not None
            else extras.get("status")
        )
        if status is not None and str(status).strip() and status != "*":
            filter_tags.append(f"STATUS:{str(status).strip()}")
        include = self._ref_id_channel(args.get("include_ref_ids"), "include_ref_ids")
        caller_include = self._ref_id_channel(
            extras.get("include_ref_ids"), "args.include_ref_ids"
        )
        if include is not None and caller_include is not None:
            include &= caller_include
        elif include is None:
            include = caller_include
        exclude = (
            self._ref_id_channel(args.get("exclude_ref_ids"), "exclude_ref_ids")
            or set()
        ) | (
            self._ref_id_channel(extras.get("exclude_ref_ids"), "args.exclude_ref_ids")
            or set()
        )

        clauses = [
            "r.retired_at IS NULL",
            "r.kind = %s",
            "(r.kind <> 'agentlog' OR r.meta->>'trace' IS DISTINCT FROM 'search')",
        ]
        params: list[Any] = [kind]
        if filter_tags:
            frag, frag_params = build_tag_filter(filter_tags, ref_alias="r")
            if frag:
                clauses.append(frag)
                params.extend(frag_params)
        if include is not None:
            clauses.append("r.ref_id = ANY(%s)")
            params.append(sorted(include))
        if exclude:
            clauses.append("NOT (r.ref_id = ANY(%s))")
            params.append(sorted(exclude))
        job_type = extras.get("job_type")
        if job_type is not None:
            if kind != "job":
                raise BadInput(
                    f"args.job_type filters kind='job' only, got kind={kind!r}",
                    next="drop it, or filter the cohort with tags=",
                )
            clauses.append("r.meta->>'job_type' = %s")
            params.append(str(job_type))
        where = " AND ".join(clauses)

        # Bucket bounds differ per clock: opens count refs minted in
        # the window (r.created_at), closes count terminal tags *set*
        # in it (rt.created_at) — a ref opened before the window and
        # closed inside it lands only on the closed side, which is
        # exactly what a burn-down read wants.
        opens_window = ""
        closes_window = ""
        if since_dt is not None:
            opens_window += " AND r.created_at >= %s"
            closes_window += " AND rt.created_at >= %s"
        if until_dt is not None:
            opens_window += " AND r.created_at < %s"
            closes_window += " AND rt.created_at < %s"
        window_params = [bound for bound in (since_dt, until_dt) if bound is not None]
        terminal_sql = (
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE t.namespace = 'STATUS' AND t.value = ANY(%s) "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
            f"AND {where}"
        )
        with self.store.pool.connection() as conn:
            open_rows = conn.execute(
                "SELECT date_trunc(%s, r.created_at AT TIME ZONE 'UTC'), "
                "count(*) FROM refs r "
                f"WHERE {where}{opens_window} GROUP BY 1 ORDER BY 1",
                [bucket, *params, *window_params],
            ).fetchall()
            close_rows = conn.execute(
                "SELECT date_trunc(%s, rt.created_at AT TIME ZONE 'UTC'), "
                "count(DISTINCT r.ref_id) "
                f"FROM refs r {terminal_sql}{closes_window} "
                "GROUP BY 1 ORDER BY 1",
                [bucket, sorted(closed), *params, *window_params],
            ).fetchall()
            ttc = conn.execute(
                "SELECT count(DISTINCT r.ref_id), "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "extract(epoch from rt.created_at - r.created_at)), "
                "percentile_cont(0.9) WITHIN GROUP (ORDER BY "
                "extract(epoch from rt.created_at - r.created_at)), "
                "max(extract(epoch from rt.created_at - r.created_at)) "
                f"FROM refs r {terminal_sql}{closes_window}",
                [sorted(closed), *params, *window_params],
            ).fetchone()
            open_now = conn.execute(
                f"SELECT count(*) FROM refs r WHERE {where} "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "  WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
                "    AND t.value = ANY(%s) "
                "    AND (rt.expires_at IS NULL OR rt.expires_at > now())"
                ")",
                [*params, sorted(closed)],
            ).fetchone()

        buckets: dict[Any, list[int]] = {}
        for b, n in open_rows:
            buckets.setdefault(b, [0, 0])[0] = int(n)
        for b, n in close_rows:
            buckets.setdefault(b, [0, 0])[1] = int(n)
        ordered = sorted(buckets.items())
        n_open = sum(o for _b, (o, _c) in ordered)
        n_close = sum(c for _b, (_o, c) in ordered)
        n_open_now = int(open_now[0]) if open_now else 0

        lo = since_dt.strftime("%Y-%m-%d") if since_dt else "…"
        hi = until_dt.strftime("%Y-%m-%d") if until_dt else "now"
        window = f"{lo} → {hi}" + (" (default)" if defaulted_window else "")
        head = (
            f"# {kind} flow — by {bucket}, {window} UTC (closed = {', '.join(closed)})"
        )
        filter_note = ", ".join(
            ([f"tags={filter_tags!r}"] if filter_tags else [])
            + ([f"job_type={job_type!r}"] if job_type is not None else [])
        )
        if filter_note:
            head += f" — filtered: {filter_note}"
        lines = [
            head,
            f"opened {n_open} · closed {n_close} · net {n_open - n_close:+d} "
            f"· open now {n_open_now}",
            "",
        ]
        if not ordered:
            lines.append("(no opens or closes in the window)")
        else:
            label_w = max(len(self._flow_bucket_label(b, bucket)) for b, _v in ordered)
            lines.append(f"  {'bucket'.ljust(label_w)}  opened  closed   net")
            shown = ordered[:_FLOW_MAX_BUCKETS]
            for b, (o, c) in shown:
                lines.append(
                    f"  {self._flow_bucket_label(b, bucket).ljust(label_w)}"
                    f"  {o:>6}  {c:>6}  {o - c:>+5}"
                )
            if len(ordered) > len(shown):
                tail = ordered[len(shown) :]
                to = sum(o for _b, (o, _c) in tail)
                tc = sum(c for _b, (_o, c) in tail)
                lines.append(
                    f"  … +{len(tail)} more buckets (opened {to}, closed {tc})"
                )
        if ttc is not None and int(ttc[0]):
            n_ttc = int(ttc[0])
            lines.append("")
            lines.append(
                f"time to close over {n_ttc} windowed "
                f"close{'s' if n_ttc != 1 else ''}: "
                f"median {_flow_duration(ttc[1])} · "
                f"p90 {_flow_duration(ttc[2])} · "
                f"max {_flow_duration(ttc[3])}"
            )
        lines.append("")
        lines.append(
            "a close is the set-time of a live ref's latest terminal "
            "STATUS — a reopened-then-reclosed ref counts its newest "
            "close only."
        )
        lines.append(
            render_next_section(
                [
                    (
                        f"search(kind={kind!r}, view='count', "
                        "args={'group_by': 'state'})",
                        "current STATUS snapshot, not the flow",
                    ),
                    (
                        f"search(kind={kind!r}, view='tags')",
                        "tag axis/value counts on the same kind",
                    ),
                    (
                        "args={'closed': ['<STATUS>', …]}",
                        "which statuses count as closed",
                    ),
                ]
            ).lstrip("\n")
        )
        return Response(body="\n".join(lines), result_count=n_open + n_close)

    @staticmethod
    def _flow_bucket_label(bucket_start: Any, bucket: str) -> str:
        """``2026-11-05`` / ``2026-11-05 13Z`` / ``2026-11`` — the UTC
        bucket label for one ``date_trunc`` row."""
        at = bucket_start if isinstance(bucket_start, datetime) else None
        if at is None:
            return str(bucket_start)
        if bucket == "hour":
            return at.strftime("%Y-%m-%d %HZ")
        if bucket == "month":
            return at.strftime("%Y-%m")
        return at.strftime("%Y-%m-%d")

    @staticmethod
    def _flow_closed_values(kind: str, raw: Any) -> tuple[str, ...]:
        """``args={'closed': …}`` or the per-kind default → STATUS values.

        ``'STATUS:done'`` spellings strip to ``done`` (the shorthand
        already implies the axis). A kind's own STATUS vocabulary
        (:data:`_KIND_STATUS_VALUES`) validates each value when the
        kind declares one — a typo'd closer would otherwise under-
        count silently.
        """
        vocab = _KIND_STATUS_VALUES.get(kind)
        if raw is None:
            if kind in _FLOW_CLOSED:
                return _FLOW_CLOSED[kind]
            pool = vocab if vocab is not None else _STATUS_VALUES_ANY_KIND
            closed = tuple(v for v in _FLOW_CLOSED_FALLBACK if v in pool)
            if closed:
                return closed
            raise BadInput(
                f"kind={kind!r} has no recognised terminal STATUS — "
                "name which values count as closed",
                next=(
                    "args={'closed': ['<status>', …]} — e.g. "
                    f"{sorted(vocab)[:3] if vocab else ['done']}"
                ),
            )
        items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        values: list[str] = []
        for item in items:
            v = str(item).strip()
            if v[:7].lower() == "status:":
                v = v[7:]
            if v and v not in values:
                values.append(v)
        if not values:
            raise BadInput(
                "args.closed needs at least one STATUS value",
                next="args={'closed': ['done']} or drop it for the kind default",
            )
        if vocab is not None:
            bad = [v for v in values if v not in vocab]
            if bad:
                raise BadInput(
                    f"args.closed {bad!r} not in kind={kind!r}'s STATUS vocabulary",
                    options=sorted(vocab),
                )
        return tuple(values)

    # ── generic view='count' (gr593053) ───────────────────────────

    #: Top-level args a ref-level count can't honour — named, not
    #: swallowed, so a typo'd filter fails loudly.
    _GENERIC_COUNT_UNSUPPORTED_TOP: tuple[str, ...] = (
        "q",
        "link",
        "mode",
        "under",
        "cited",
        "uncited",
        "hubbed",
        "scope",
        "page",
        "page_size",
        "id_min",
        "id_max",
    )
    #: args= keys the generic count reads.
    _GENERIC_COUNT_ALLOWED_EXTRAS: frozenset[str] = frozenset(
        {"group_by", "include_ref_ids", "exclude_ref_ids"}
    )

    def _dispatch_generic_count(self, kind: str, args: dict[str, Any]) -> Response:
        """``search(kind=K, view='count')`` on a kind with no native count view.

        gr593053: the weekly report's "quick facts" wanted
        ``search(kind='pres'|'structure'|'pathway'|'draft'|'conv'|'web',
        view='count')`` and each died on ``does not accept ['view']`` —
        those handlers search *blocks*, not refs, so their ``search``
        never declared ``view``. This fallback answers the ref-level
        question they were actually asking ("how many live <kind> refs")
        through the same :func:`~precis.store.Store.count_refs_facets`
        primitive the numeric-ref kinds use, so the shape — ``# N
        <kind> entries`` head, ``label: n`` facet rows — matches what
        count consumers already parse.

        Honest limits, said out loud: the counted set is refs
        (``retired_at IS NULL``, the agentlog search-trace rows
        excluded), filtered by ``tags=``/``status=`` and bounded by
        ``since=``/``until=`` on ``refs.created_at``; ``group_by=``
        facets work as on the native view. ``q=`` is refused — a
        block-match count is a different question these kinds' search
        would answer, and a title-lexical count wearing the same
        spelling would lie. The header notes when this is the generic
        path so a total read beside a numeric kind's filtered count
        isn't mistaken for one.
        """
        from precis.handlers._numeric_ref import (
            _COUNT_GROUP_LIMIT,
            _count_facet_label,
            _count_group_label,
            _parse_count_facets,
            _parse_window_bound,
        )

        if self.store is None:
            raise Unsupported("view='count' needs a store-backed deployment")
        bad = next(
            (a for a in self._GENERIC_COUNT_UNSUPPORTED_TOP if args.get(a) is not None),
            None,
        )
        if bad is not None:
            raise BadInput(
                f"{bad}= is not supported with view='count' on kind={kind!r} — "
                "this kind counts live refs, not block matches",
                next=(
                    f"search(kind={kind!r}, view='count') for the ref total, "
                    f"or search(kind={kind!r}, q='...') for the ranked blocks"
                ),
            )
        raw_extras = args.get("__extras__")
        extras = raw_extras if isinstance(raw_extras, dict) else {}
        unknown = sorted(set(extras) - self._GENERIC_COUNT_ALLOWED_EXTRAS)
        if unknown:
            raise BadInput(
                f"args= keys {unknown!r} not supported with view='count' "
                f"on kind={kind!r}",
                options=sorted(self._GENERIC_COUNT_ALLOWED_EXTRAS),
            )
        status = args.get("status")
        tags_arg = args.get("tags")
        filter_tags: list[str] = []
        if isinstance(tags_arg, str):
            filter_tags = [tags_arg]
        elif isinstance(tags_arg, (list, tuple)):
            filter_tags = [str(t) for t in tags_arg]
        if status is not None and str(status).strip() and status != "*":
            filter_tags.append(f"STATUS:{str(status).strip()}")
        # The ref-id scope channels arrive at two levels: dispatch
        # resolves ``folder=`` (in the view='count' block above),
        # ``under=``, ``cited=``, ``uncited=`` and ``hubbed=`` into
        # TOP-LEVEL ``include_ref_ids``/``exclude_ref_ids`` before this
        # runs, and a caller's ``args=`` may carry a second set. Both
        # bind — inclusions intersect, exclusions union. An empty
        # inclusion list is a real scope: ``ANY('{}')`` counts zero rows,
        # which is the right answer; collapsing it to None would count
        # the whole kind under a folder or facet that matched nothing.
        include = self._ref_id_channel(args.get("include_ref_ids"), "include_ref_ids")
        caller_include = self._ref_id_channel(
            extras.get("include_ref_ids"), "args.include_ref_ids"
        )
        if include is not None and caller_include is not None:
            include &= caller_include
        elif include is None:
            include = caller_include
        exclude = (
            self._ref_id_channel(args.get("exclude_ref_ids"), "exclude_ref_ids")
            or set()
        ) | (
            self._ref_id_channel(extras.get("exclude_ref_ids"), "args.exclude_ref_ids")
            or set()
        )
        since_dt = _parse_window_bound(args.get("since"), "since")
        until_dt = _parse_window_bound(args.get("until"), "until")
        facets = _parse_count_facets(extras.get("group_by"))

        total, groups = self.store.count_refs_facets(
            kind=kind,
            tags=filter_tags or None,
            include_ref_ids=sorted(include) if include is not None else None,
            exclude_ref_ids=sorted(exclude) if exclude else None,
            facets=facets,
            group_limit=_COUNT_GROUP_LIMIT,
            since=since_dt,
            until=until_dt,
        )

        filters: list[str] = []
        if filter_tags:
            filters.append(f"tags={filter_tags!r}")
        if since_dt is not None or until_dt is not None:
            lo = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else "…"
            hi = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if until_dt else "…"
            filters.append(f"created [{lo}, {hi})")
        head = f"# {total} {kind} entr{'y' if total == 1 else 'ies'}"
        if filters:
            head += f" ({', '.join(filters)})"
        head += " — live refs"
        parts = [head]
        if facets:
            parts.append("")
            parts.append(" × ".join(_count_facet_label(f) for f in facets))
            for vals, n, _span in groups:
                parts.append(f"  {_count_group_label(vals, facets)}: {int(n)}")
            if len(groups) == _COUNT_GROUP_LIMIT:
                parts.append(
                    f"  … (top {_COUNT_GROUP_LIMIT} groups by count — "
                    "narrow the filter set for the rest)"
                )
        parts.append("")
        parts.append(
            "ref-level count on a kind whose search reads blocks — "
            "q= block-match counts aren't available here."
        )
        parts.append(
            render_next_section(
                [
                    (
                        f"search(kind={kind!r}, q='...')",
                        "the ranked block search this kind serves",
                    ),
                    (
                        "get(kind='job', id='/facts')",
                        "storage facts: refs per kind, chunk coverage, PDF bytes",
                    ),
                ]
            ).lstrip("\n")
        )
        return Response(body="\n".join(parts), result_count=total)

    @staticmethod
    def _ref_id_channel(value: Any, name: str) -> set[int] | None:
        """One ``*_ref_ids`` channel → ``set[int]``; ``None`` when absent.

        ``[]`` stays ``[]`` — an empty inclusion is a scope that matches
        nothing (``ANY('{}')``), not an absent filter. ``BadInput`` on a
        non-list or a non-int member: the channel carries ref ids, never
        handles or slugs.
        """
        if value is None:
            return None
        if isinstance(value, (str, bytes)) or not isinstance(
            value, (list, tuple, set, frozenset)
        ):
            raise BadInput(
                f"{name}= must be a list of ref ids, got {value!r}",
                next=f"pass {name}= as a JSON list of integers",
            )
        try:
            return {int(i) for i in value}
        except (TypeError, ValueError):
            raise BadInput(
                f"{name}= members must be ref ids, got {value!r}",
                next=f"pass {name}= as a JSON list of integers",
            ) from None
