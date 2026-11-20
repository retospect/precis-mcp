"""Paper measure inventory, grouped by stored table ordinal then prose.

Use the same subject-or-anchor scope and eligibility as default paper-scoped
search. Table extraction counters still count every unsuperseded output;
this view excludes ambiguous and anchor-lost measured outputs.
Escalated outputs carry their flags so readers can find existing runs before
extracting the same evidence again.
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
    money = store.money_tables()
    for number in sorted(groups):
        lines.extend(["", f"## T{number}"])
        lines.extend(render.result_line(row, money=money) for row in groups[number])
    if prose:
        lines.extend(["", "## prose"])
        lines.extend(render.result_line(row, money=money) for row in prose)
    return Response(body="\n".join(lines), result_count=len(rows))
