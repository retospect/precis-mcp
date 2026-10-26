"""Paper measure inventory, grouped by stored table ordinal then prose.

Use the same subject-or-anchor scope and live-row eligibility as measure
search. Table extraction counters still count every unsuperseded output;
this view excludes ambiguous, escalated and anchor-lost measured outputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.handlers import _measure_render as render
from precis.response import Response
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store


def render_measures_view(store: Store, ref: Ref) -> Response:
    rows = store.measures_for_paper(ref.id)
    paper = handle_registry.format_handle(ref.kind, ref.id)
    if not rows:
        return Response(body=f"no measures in {paper}", result_count=0)

    groups: dict[int, list[dict[str, Any]]] = {}
    prose: list[dict[str, Any]] = []
    for row in rows:
        table = (row.get("meta") or {}).get("table")
        number = table.get("table") if isinstance(table, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            groups.setdefault(number, []).append(row)
        else:
            prose.append(row)

    lines = [f"# {paper} — {len(rows)} measure(s)"]
    for number in sorted(groups):
        lines.extend(["", f"## T{number}"])
        lines.extend(render.result_line(row) for row in groups[number])
    if prose:
        lines.extend(["", "## prose"])
        lines.extend(render.result_line(row) for row in prose)
    return Response(body="\n".join(lines), result_count=len(rows))
