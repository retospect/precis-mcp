"""``type='discord_reply_received'`` — wait for the owner's Discord reply.

Resolves to ``True`` when a memory exists tagged
``replied-to:<ask_message_id>``. The asa-bot chatter writes this
tag from its in-thread reply detection (Slice 2); from this
worker's perspective, the tag is the signal.

Spec
====

```json
{
  "type": "discord_reply_received",
  "ask_message_id": "1234567890123456789",
  "thread": "discord/<guild>/<channel>/<thread>"
}
```

``thread`` is informational only (the asker stamped it so
inspection is easier). The match is on the tag alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    ask_id = spec.get("ask_message_id")
    if not isinstance(ask_id, (str, int)) or str(ask_id).strip() == "":
        raise BadInput(
            "discord_reply_received needs ask_message_id",
            next="meta.auto_check.ask_message_id='<discord message id>'",
        )
    tag_value = f"replied-to:{ask_id}"
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
              JOIN refs r ON r.ref_id = rt.ref_id
             WHERE r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
             LIMIT 1
            """,
            (tag_value,),
        ).fetchone()
    return row is not None
