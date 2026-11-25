"""Shared raw rendering for stored refs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._links_render import DEFAULT_LINK_ROW_CAP, render_links_section

if TYPE_CHECKING:
    from precis.store import Ref, Store, Tag


#: Cap on one top-level ``meta`` value's rendered JSON inside
#: ``view='raw'`` (gr635381): a job's ``meta.assembled_context`` runs
#: ~45 kB of cached prompt layers, burying the ``meta.error`` a failed
#: job's read exists to show — and the read's own byte cap cuts the
#: dump before the error ever renders. Past the cap the value collapses
#: to an elision line naming the ``args={'meta_key': '<key>'}`` door
#: that serves it whole; the dict's other keys render as usual.
_RAW_META_ELIDE_CHARS = 2000


def render_raw(
    store: Store, ref: Ref, tags: list[Tag], *, meta_key: str | None = None
) -> str:
    """Render stored columns, tags, bounded metadata and a capped link preview."""
    meta = ref.meta or {}
    if meta_key is not None:
        if meta_key not in meta:
            raise BadInput(
                f"{ref.kind} {ref.public_id} meta has no key {meta_key!r}",
                options=sorted(meta),
                next=(f"get(kind={ref.kind!r}, id={ref.public_id}, view='meta')"),
            )
        value = meta[meta_key]
        # A text value renders verbatim — the elided blob is usually
        # a prompt layer or a traceback, which quoting/escaping would
        # only make harder to read. Structured values keep JSON.
        rendered = (
            value
            if isinstance(value, str)
            else json.dumps(value, indent=2, sort_keys=True, default=str)
        )
        return f"# raw {ref.kind} {ref.public_id} — meta.{meta_key}\n{rendered}"

    out = [f"# raw {ref.kind} {ref.public_id}", ""]
    # Scalar columns — only the ones that are set, to keep it terse.
    scalars: list[tuple[str, Any]] = [
        ("id", ref.id),
        ("kind", ref.kind),
        ("slug", ref.slug),
        ("parent_id", ref.parent_id),
        ("prio", ref.prio),
        ("provider", ref.provider),
        ("set_by", ref.set_by),
        ("year", ref.year),
        ("created_at", ref.created_at),
        ("updated_at", ref.updated_at),
        ("retired_at", ref.retired_at),
        ("auto_refresh_days", ref.auto_refresh_days),
        ("refreshed_at", ref.refreshed_at),
    ]
    out += [f"{k}: {v}" for k, v in scalars if v is not None]
    out += ["", "title:", ref.title]
    if tags:
        out += ["", "tags: " + " ".join(str(t) for t in tags)]
    out += ["", "meta:"]
    shown: dict[str, Any] = {}
    for key, value in meta.items():
        blob = json.dumps(value, sort_keys=True, default=str)
        if len(blob) > _RAW_META_ELIDE_CHARS:
            shown[key] = (
                f"<{len(blob)} chars elided; get(kind={ref.kind}, "
                f"id={ref.public_id}, view='raw', "
                f"args={{'meta_key': '{key}'}}) for it whole>"
            )
        else:
            shown[key] = value
    out.append(json.dumps(shown, indent=2, sort_keys=True, default=str))
    body = "\n".join(out)
    # gr489582: ``raw`` exists so an agent can read meta — the link
    # list must not drown it. Cap like the bare-get append; the
    # overflow line already points at ``view='links'`` for the rest.
    return body + render_links_section(
        store, ref, limit=DEFAULT_LINK_ROW_CAP, priority=True
    )
