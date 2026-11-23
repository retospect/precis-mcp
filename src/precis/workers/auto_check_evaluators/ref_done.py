"""``type='ref_done'`` — wait for a named ref to reach ``STATUS:done``.

Spec
====

```json
{ "type": "ref_done", "ref": "gr123" }
```

The handle must name a live record when the todo is written. The check
returns ``False`` for other statuses or no status, and ``None`` if the
target no longer resolves to a record when polled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store


def _ref_handle(spec: dict[str, Any]) -> str:
    ref = spec.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise BadInput(
            "ref_done needs a ref handle", next="meta.auto_check.ref='gr123'"
        )
    return ref


def validate(spec: dict[str, Any]) -> None:
    _ref_handle(spec)


def validate_with_store(store: Store, spec: dict[str, Any]) -> None:
    ref = _ref_handle(spec)
    target = store.resolve_handle(ref)
    if target is None or target.chunk_id is not None:
        raise BadInput(
            f"ref_done target {ref!r} must name a live record",
            next="set meta.auto_check.ref to a live record handle such as 'gr123'",
        )


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    ref = _ref_handle(spec)
    with store.pool.connection() as conn:
        target = store.resolve_handle(ref, conn=conn)
        if target is None or target.chunk_id is not None:
            return None
        row = conn.execute(
            """
            SELECT 1 FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s
               AND t.namespace = 'STATUS'
               AND t.value = 'done'
             LIMIT 1
            """,
            (target.ref_id,),
        ).fetchone()
    return row is not None
