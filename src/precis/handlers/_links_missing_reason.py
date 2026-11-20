"""Read-only missing-rationale queue shared by corpus and focused reads."""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.response import Response
from precis.store import Store
from precis.utils import handle_registry


def render_missing_reasons(
    store: Store,
    *,
    kind: str,
    id_arg: str | int,
    focus_ref_id: int | None = None,
    **args: Any,
) -> Response:
    """Render exact stored scopes, bounded titles and a repeatable next call."""
    args = {k: v for k, v in args.items() if v is not None}
    args.pop("reason", None)
    first_args = {k: v for k, v in args.items() if k != "cursor"}
    if focus_ref_id is not None:
        first_args["reason"] = "missing"

    def call(a: dict[str, Any]) -> str:
        view = ", view='links'" if focus_ref_id is not None else ""
        return f"get(kind={kind!r}, id={id_arg!r}{view}, args={a!r})"

    restart = call(first_args)
    try:
        limit = args.get("limit", 25)
        if isinstance(limit, bool) or not str(limit).isdigit():
            raise BadInput("limit must be an integer", next=restart)
        for name in ("count", "include_structural"):
            if name in args and not isinstance(args[name], bool):
                raise BadInput(f"{name} must be boolean", next=restart)
        under = args.get("under")
        if under is not None:
            if isinstance(under, str) and under.startswith("me"):
                under = under[2:]
            if not str(under).isdigit():
                raise BadInput("under must name a memory handle", next=restart)
            under = int(under)
        relation = args.get("relation", args.get("rel"))
        if relation is not None and not isinstance(relation, str):
            raise BadInput("relation must be a relation slug", next=restart)
        page = store.links_missing_reason(
            relations=[relation] if isinstance(relation, str) else relation,
            include_structural=args.get("include_structural", False),
            src_kind=args.get("src_kind"),
            dst_kind=args.get("dst_kind"),
            focus_ref_id=focus_ref_id,
            direction=args.get("direction", "both"),
            space=args.get("space"),
            under=under,
            endpoint=args.get("endpoint", "src"),
            cursor=args.get("cursor"),
            limit=int(limit),
            count_only=args.get("count", False),
        )
    except BadInput as exc:
        raise BadInput(exc.cause, next=restart) from None
    rows = []
    for edge in page["rows"]:
        row = dict(edge)
        for side in ("src", "dst"):
            row[side] = handle_registry.try_format(
                row.pop(f"{side}_kind"), row[side]
            ) or str(row[side])
        row["scope"] = f"chunks:{row.pop('src_chunk_id')}→{row.pop('dst_chunk_id')}"
        rows.append(row)
    body = "# Links missing a reason\n\n"
    if rows:
        body += render_agent_table(rows, schema=list(rows[0])) + "\n\n"
    body += f"shown {len(rows)} of {page['total']} missing"
    if args.get("count", False):
        body += " · count only (dry run)"
    elif page["done"]:
        body += " · complete"
    else:
        body += " · next: " + call({**first_args, "cursor": page["next_cursor"]})
    return Response(body=body, result_count=len(rows))
