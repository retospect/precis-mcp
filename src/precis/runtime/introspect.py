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
"""

from __future__ import annotations

import time
from typing import Any

from precis.errors import BadInput, Unsupported
from precis.response import Response
from precis.runtime._shared import RuntimeShape
from precis.store._tag_filter import build_tag_filter
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


class IntrospectionMixin(RuntimeShape):
    """``view='meta'`` / ``view='tags'`` render + cache."""

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
        if kind_s not in self.hub.kinds:
            raise BadInput(
                f"unknown kind {kind_s!r} for view='{view}'",
                options=sorted(self.hub.kinds),
            )
        if self.store is None:
            raise Unsupported(f"view='{view}' needs a store-backed deployment")
        raw_extras = args.get("__extras__")
        extras = raw_extras if isinstance(raw_extras, dict) else {}
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
                "SELECT count(*) FROM refs WHERE kind = %s AND retired_at IS NULL",
                (kind,),
            ).fetchone()
            if key is None:
                rows = conn.execute(
                    """
                    SELECT e.key, jsonb_typeof(e.value) AS typ, count(*) AS n,
                           min(left(e.value::text, %s)) AS example
                      FROM refs r, jsonb_each(coalesce(r.meta, '{}'::jsonb)) e
                     WHERE r.kind = %s AND r.retired_at IS NULL
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
        clauses = ["r.retired_at IS NULL", "r.kind = %s"]
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
