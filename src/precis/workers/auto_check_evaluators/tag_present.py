"""``type='tag_present'`` — generic gate on a tag being attached anywhere.

Resolves to ``True`` when at least one live ref carries the given
tag. Optional ``kind`` narrows the search to refs of one kind.

Spec
====

```json
{
  "type": "tag_present",
  "tag": "topic:co2-capture",
  "kind": "paper"
}
```

``tag`` accepts either the closed form (``STATUS:done``) or the
open form (``topic:foo``). The check uses the same
namespace-routing the store does — closed prefix → STATUS / PRIO /
…, everything else → OPEN.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    raw = spec.get("tag")
    if not isinstance(raw, str) or not raw.strip():
        raise BadInput(
            "tag_present needs a tag",
            next="meta.auto_check.tag='STATUS:done' (or 'topic:foo')",
        )
    # Use the same parser the agent boundary uses so closed-prefix
    # tags route to the right namespace and unregistered prefixes
    # raise the same descriptive error.
    parsed = Tag.parse_strict(raw)
    if parsed.namespace == "closed":
        assert parsed.prefix is not None
        namespace, value = parsed.prefix, parsed.value
    elif parsed.namespace == "flag":
        namespace, value = "FLAG", parsed.value
    else:
        namespace, value = "OPEN", parsed.value
    kind = spec.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise BadInput(
            f"tag_present.kind must be a string, got {type(kind).__name__}",
            next="meta.auto_check.kind='paper' (or omit for any kind)",
        )
    sql = (
        "SELECT 1 FROM ref_tags rt "
        " JOIN tags t ON t.tag_id = rt.tag_id "
        " JOIN refs r ON r.ref_id = rt.ref_id "
        "WHERE r.deleted_at IS NULL "
        "  AND t.namespace = %s "
        "  AND t.value = %s"
    )
    params: list[Any] = [namespace, value]
    if kind is not None:
        sql += " AND r.kind = %s"
        params.append(kind)
    sql += " LIMIT 1"
    with store.pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return row is not None
