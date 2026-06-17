"""MessageHandler — proactive outbound messages.

Numeric-id ref kind (migration 0010). Each message ref is one
outbound proactive send. asa_bot calls
``put(kind='message', target='discord/G/C/T', text='...')`` to ping
the user unprompted; the handler stores the ref AND fires
``pg_notify('precis.messages', {ref_id: N})``. asa_bot LISTENs on
that channel, fetches the ref, and posts to the target transport.

Every send becomes a stored, searchable ref — "what have I been
nagging the user about this week?" via
``search(kind='message', q='...')``.

State in ``ref.meta``:

- ``target``: ``'discord/<g>/<c>/<t>'`` (transport-prefixed). The
  handler validates the format prefix; the delivery layer is
  responsible for routing.
- ``status``: ``'queued'`` | ``'sent'`` | ``'failed'``. New rows
  start ``'queued'``; asa_bot updates after delivery.
- ``reason``: free-form trace (``'cron:42 fired'``, ``'asa response
  to user'``) for audit.
- ``attachments``: optional list of attachment dicts (Q1 — Discord
  files, see precis-message-help).

Body lives as a ``chunk_kind='message_body'`` chunk so the embed +
chunk_keywords workers index it for later retrieval.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.handlers._link_target import parse_link_target
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag
from precis.store.types import BlockInsert
from precis.utils.next_block import render_next_section

# Transports we accept on a target=. v1: discord only. The validator
# is permissive on the right-hand side so a Discord guild/channel/thread
# id triplet just rides through; the delivery layer enforces semantics.
_VALID_TARGET_PREFIXES = frozenset({"discord/", "conv:discord/"})


def _validate_target(target: str) -> None:
    """Sanity-check the target= shape. Raises BadInput on miss."""
    t = target.strip()
    if not t:
        raise BadInput(
            "target= is required for kind='message'",
            next=[
                "target='discord/<guild>/<channel>/<thread>'",
                "get(kind='skill', id='precis-message-help') for the full surface",
            ],
        )
    if not any(t.startswith(p) for p in _VALID_TARGET_PREFIXES):
        raise BadInput(
            f"unsupported target transport in {target!r}",
            next=[
                "target='discord/<guild>/<channel>/<thread>'",
                "get(kind='skill', id='precis-message-help') for the full surface",
            ],
        )


class MessageHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="message",
        title="Message",
        description=(
            "Proactive outbound message. put(kind='message', "
            "target='discord/<g>/<c>/<t>', text='...') stores the ref "
            "and fires pg_notify('precis.messages'); asa_bot LISTENs "
            "and delivers. Every send is searchable history."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "message"
    sense: ClassVar[str] = "message"

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        target: str | None = None,
        reason: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        **_kw: Any,
    ) -> Response:
        """Queue a proactive outbound message.

        Required: ``text`` (body), ``target`` (where to deliver).

        Optional:
          - ``reason``: short trace string ("cron:42 fired",
            "asa noticed PR was stale") for audit / debugging.
          - ``attachments``: list of attachment specs the delivery
            layer fetches and posts inline. Shape:
            ``[{filename, content_type, archive_path}]`` —
            asa_bot reads each path from NFS and uploads.

        The handler stores the ref then fires
        ``pg_notify('precis.messages', '{"ref_id": N}')`` so the
        delivery layer wakes immediately. asa_bot reads the
        notification, fetches the ref + chunks + meta, and posts.

        State transitions: every new send starts
        ``meta.status='queued'``. asa_bot stamps ``'sent'`` (or
        ``'failed'`` with a ``meta.error`` note) after delivery
        attempt. Re-delivery on failure is not automatic in v1
        (acceptable for human-scale traffic); operator can flip
        status back to ``'queued'`` to re-attempt.
        """
        if id is not None:
            raise BadInput(
                f"put on existing message id={id!r} is not supported",
                next=[
                    "messages are immutable once queued; create a new one",
                    "get(kind='skill', id='precis-message-help') for the full surface",
                ],
            )
        if mode is not None:
            raise BadInput(
                "mode= is not accepted on put for kind='message'",
                next="omit mode=",
            )
        if untags is not None:
            raise BadInput(
                "untags= is not accepted on put",
                next="use tag(kind='message', id=N, remove=[...])",
            )
        if unlink is not None:
            raise BadInput(
                "unlink= is not accepted on put",
                next="use link(kind='message', id=N, mode='remove')",
            )
        if text is None or not str(text).strip():
            raise BadInput(
                "put(kind='message') requires text= (the message body)",
                next=[
                    "put(kind='message', text='hi', target='discord/<g>/<c>/<t>')",
                    "get(kind='skill', id='precis-message-help') for the full surface",
                ],
            )
        if target is None:
            raise BadInput(
                "put(kind='message') requires target=",
                next=[
                    "put(kind='message', text='...', target='discord/<g>/<c>/<t>')",
                    "get(kind='skill', id='precis-message-help') for the full surface",
                ],
            )
        _validate_target(str(target))

        if attachments is not None and not isinstance(attachments, list):
            raise BadInput(
                f"attachments= must be a list of dicts, got {type(attachments).__name__}",
                next=(
                    "attachments=[{'filename':'a.png', 'content_type':'image/png', "
                    "'archive_path':'/opt/asa/.../a.png'}, ...]"
                ),
            )

        meta: dict[str, Any] = {
            "status": "queued",
            "target": str(target).strip(),
        }
        if reason is not None:
            meta["reason"] = str(reason).strip()
        if attachments is not None:
            meta["attachments"] = attachments

        # Tag + link prep (parent shape).
        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]
        link_target = (
            parse_link_target(link, store=self.store) if link is not None else None
        )
        if rel is not None and link is None:
            raise BadInput(
                "rel= requires link= on create",
                next=(
                    "put(kind='message', text='...', target='...', "
                    "link='conv:discord/...', rel='derived-from')"
                ),
            )
        relation = validate_relation(rel)

        text_str = str(text)
        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text_str[:200],
                meta=meta,
                conn=conn,
            )
            self.store.insert_blocks(
                ref.id,
                [
                    BlockInsert(
                        pos=0,
                        text=text_str,
                        meta={"chunk_kind": "message_body"},
                    )
                ],
                conn=conn,
            )
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if link_target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=link_target.ref_id,
                    dst_pos=link_target.pos,
                    relation=relation,
                    conn=conn,
                )
            # Fire the delivery notification *inside* the same tx so it
            # commits with the ref. psycopg's auto-commit-NOTIFY model
            # sends on transaction commit, which is what we want — a
            # rolled-back insert won't ship a phantom notification.
            import json

            conn.execute(
                "SELECT pg_notify('precis.messages', %s)",
                (json.dumps({"ref_id": ref.id, "target": meta["target"]}),),
            )

        return self._render_create_ack(ref.id, target=meta["target"])

    def _render_create_ack(  # type: ignore[override]
        self,
        ref_id: int,
        target: str | None = None,
    ) -> Response:
        body = f"queued message id={ref_id}"
        if target:
            body += f" → {target}"
        body += render_next_section(
            [
                (f"get(kind='message', id={ref_id})", "read the queued message"),
                (
                    f"delete(kind='message', id={ref_id})",
                    "cancel before delivery",
                ),
                ("get(kind='message', id='/recent')", "list recent sends"),
            ]
        )
        return Response(body=body)

    def _render_one(self, ref: Any, tags: list[Any]) -> str:  # type: ignore[override]
        meta = ref.meta or {}
        lines = [f"# message {ref.id}", ref.title]
        lines.append("")
        lines.append(f"status: {meta.get('status', 'unknown')}")
        target = meta.get("target")
        if target:
            lines.append(f"target: {target}")
        reason = meta.get("reason")
        if reason:
            lines.append(f"reason: {reason}")
        attachments = meta.get("attachments") or []
        if attachments:
            lines.append(f"attachments: {len(attachments)}")
            for a in attachments:
                if isinstance(a, dict):
                    fn = a.get("filename", "?")
                    ct = a.get("content_type", "?")
                    lines.append(f"  - {fn} ({ct})")
        if tags:
            lines.append("")
            lines.append("tags: " + ", ".join(str(t) for t in tags))
        return "\n".join(lines)
