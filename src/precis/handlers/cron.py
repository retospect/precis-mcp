"""CronHandler — scheduled wakeups.

Numeric-id ref kind (migration 0009). Each cron ref is a scheduled
prompt: when its ``next_fire_at`` arrives, the cron-tick CLI fires a
``pg_notify('precis.cron', ...)`` event that asa_bot (or whichever
delivery layer is wired) picks up and translates into a synthetic
user prompt to the configured target.

Schedule lives in ``ref.meta``:

- ``next_fire_at``: ISO 8601 timestamp — when this fires next.
- ``last_fired_at``: ISO 8601 — null until first fire.
- ``recurring``: string or null. None = one-shot. Otherwise a small
  recurrence vocabulary the cron-tick CLI knows how to advance:
    * ``'every <N> <unit>'`` (unit ∈ minute/minutes/hour/hours/day/days)
    * ``'hourly'``, ``'daily'``, ``'weekly'``
    * ``'daily@HH:MM'`` (UTC)
    * ``'weekly@<dayname>@HH:MM'``
- ``catch_up``: bool. True = fire when overdue. False = skip past the
  missed slot, advance to next. Default policy:
    * one-shot: True (late is better than never).
    * recurring: False (avoid burst-fires after downtime).
- ``status``: ``'scheduled'`` | ``'fired'`` | ``'expired'`` | ``'cancelled'`` | ``'paused'``.
  String in meta — kept off the closed-tag axis so the cron kind
  stays flexible.
- ``target``: ``'conv:discord/<g>/<c>/<t>'`` or similar — where to
  deliver the fire payload. The handler validates the format but
  doesn't enforce existence; the delivery layer is responsible for
  routing.
- ``fire_count``: int — how many times this has fired (informational).

Body (the text payload) lives as a ``chunk_kind='cron_payload'``
chunk so the embed + chunk_keywords workers index it normally —
"what crons did I set up about my PR?" works via the standard
search surface.

See ``precis-cron-help``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
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

# Duration parsing: "10 minutes", "2 hours", "3 days", "1 day"
_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(minute|minutes|min|m|hour|hours|hr|h|day|days|d)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_DELTA = {
    "minute": timedelta(minutes=1),
    "minutes": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "m": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hours": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "h": timedelta(hours=1),
    "day": timedelta(days=1),
    "days": timedelta(days=1),
    "d": timedelta(days=1),
}

# Recurring vocabulary the cron-tick advancer understands. The handler
# only validates the *shape* on put; the actual advance logic lives in
# the CLI ``precis cron tick`` subcommand.
_RECURRING_HOURLY = "hourly"
_RECURRING_DAILY = "daily"
_RECURRING_WEEKLY = "weekly"
_RECURRING_EVERY_RE = _DURATION_RE  # `every 5 minutes` shares the duration grammar
_RECURRING_DAILY_AT_RE = re.compile(r"^daily@(\d{1,2}):(\d{2})$")
_RECURRING_WEEKLY_AT_RE = re.compile(
    r"^weekly@(mon|tue|wed|thu|fri|sat|sun)@(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)

_VALID_STATUSES = frozenset(
    {"scheduled", "fired", "expired", "cancelled", "paused"}
)


def parse_duration(s: str) -> timedelta:
    """Parse ``'10 minutes'`` / ``'2 hours'`` / ``'3 days'`` to a timedelta.

    Raises :class:`BadInput` on a malformed value with a recovery
    pointer at the documented vocabulary.
    """
    m = _DURATION_RE.match(s)
    if not m:
        raise BadInput(
            f"unparseable duration {s!r}",
            next=(
                "use '<N> <unit>' where unit is "
                "minute/hour/day (e.g. '10 minutes', '2 hours', '3 days')"
            ),
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * _UNIT_TO_DELTA[unit]


def validate_recurring(s: str) -> None:
    """Sanity-check a recurring expression. Raises BadInput on miss.

    Validates the shape only; the tick CLI computes the actual next-fire
    each cycle.
    """
    s_norm = s.strip().lower()
    if s_norm in (_RECURRING_HOURLY, _RECURRING_DAILY, _RECURRING_WEEKLY):
        return
    if s_norm.startswith("every "):
        rest = s_norm[len("every "):]
        # 'every N unit' — reuse the duration grammar (rejects ``every monday``
        # etc; intentional v1 scope).
        try:
            parse_duration(rest)
            return
        except BadInput as exc:
            raise BadInput(
                f"unparseable recurring expression {s!r}",
                next=(
                    "try 'every 10 minutes', 'every 2 hours', 'every 1 day'"
                ),
            ) from exc
    if _RECURRING_DAILY_AT_RE.match(s_norm):
        return
    if _RECURRING_WEEKLY_AT_RE.match(s_norm):
        return
    raise BadInput(
        f"unsupported recurring expression {s!r}",
        next=(
            "supported forms: 'hourly', 'daily', 'weekly', "
            "'every <N> <unit>', 'daily@HH:MM' (UTC), "
            "'weekly@<dayname>@HH:MM'"
        ),
    )


def _coerce_when(when: str | datetime) -> datetime:
    """Parse an ISO 8601 string into an aware UTC datetime."""
    if isinstance(when, datetime):
        if when.tzinfo is None:
            return when.replace(tzinfo=UTC)
        return when.astimezone(UTC)
    try:
        # Python's fromisoformat handles `2026-06-12T09:00:00Z` after
        # the Z → +00:00 normalization that landed in 3.11+.
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadInput(
            f"unparseable when= timestamp {when!r}",
            next=(
                "use ISO 8601: when='2026-06-12T09:00:00Z' or "
                "'2026-06-12T09:00:00+00:00'"
            ),
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class CronHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="cron",
        title="Cron",
        description=(
            "Scheduled wakeup. put(kind='cron', text='...', "
            "in_='10 minutes', target='conv:discord/...') schedules a "
            "synthetic prompt; cron-tick fires NOTIFY at next_fire_at "
            "and asa_bot delivers. Recurring + catch_up policy."
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

    kind: ClassVar[str] = "cron"
    sense: ClassVar[str] = "cron"

    # ── put: schedule a cron entry ──────────────────────────────────

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
        when: str | datetime | None = None,
        in_: str | None = None,
        recurring: str | None = None,
        catch_up: bool | None = None,
        target: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Schedule a cron entry.

        Required:
          - ``text``: the natural-language payload Asa receives when this
            fires (becomes the synthetic prompt).
          - ``target``: where to deliver the fire — e.g.
            ``'conv:discord/<g>/<c>/<t>'``.
          - One of ``when=`` (absolute) or ``in_=`` (relative) or
            ``recurring=`` (start at next occurrence).

        Optional:
          - ``recurring=``: recurrence vocabulary. None = one-shot.
          - ``catch_up=``: True/False. Default: True for one-shot, False
            for recurring.
        """
        # Reject the legacy/unsupported kwargs the parent NumericRefHandler
        # rejects. We can't call super().put() because we need to inject
        # cron-specific meta on creation, so duplicate the guards here.
        if id is not None:
            raise BadInput(
                f"put on existing cron id={id!r} is not supported",
                next=[
                    f"to mutate id={id}: tag(kind='cron', id=N, "
                    "add=['STATUS:paused']) for pause; "
                    "delete(kind='cron', id=N) to cancel",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )
        if mode is not None:
            raise BadInput(
                "mode= is not accepted on put for kind='cron'",
                next=[
                    "omit mode=",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )
        if untags is not None:
            raise BadInput(
                "untags= is not accepted on put",
                next="use tag(kind='cron', id=N, remove=[...])",
            )
        if unlink is not None:
            raise BadInput(
                "unlink= is not accepted on put",
                next="use link(kind='cron', id=N, mode='remove')",
            )

        # Cron-specific validation.
        if text is None or not str(text).strip():
            raise BadInput(
                "put(kind='cron') requires text= (the payload to deliver)",
                next=[
                    "put(kind='cron', text='ask about the PR', "
                    "in_='10 minutes', target='conv:discord/<g>/<c>/<t>')",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )
        if target is None or not str(target).strip():
            raise BadInput(
                "put(kind='cron') requires target= (where to deliver)",
                next=[
                    "put(kind='cron', text='...', target="
                    "'conv:discord/<g>/<c>/<t>')",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )
        # Exactly one schedule input. recurring alone is OK (starts at
        # next occurrence). recurring + when/in_ pins the first fire,
        # then continues recurring.
        if when is not None and in_ is not None:
            raise BadInput(
                "when= and in_= are mutually exclusive",
                next=[
                    "pass at most one of when=<iso> or in_='<N> <unit>'",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )
        if when is None and in_ is None and recurring is None:
            raise BadInput(
                "put(kind='cron') requires a schedule: when=, in_=, or recurring=",
                next=[
                    "put(kind='cron', text='...', in_='10 minutes', target='...') "
                    "OR put(kind='cron', text='...', recurring='daily@09:00', target='...')",
                    "get(kind='skill', id='precis-cron-help') for the full surface",
                ],
            )

        now = datetime.now(tz=UTC)
        if when is not None:
            next_fire_at = _coerce_when(when)
        elif in_ is not None:
            delta = parse_duration(str(in_))
            next_fire_at = now + delta
        else:
            # Recurring only — fire on the next occurrence. The tick CLI
            # advances on each fire so we just need *some* future
            # timestamp here; "1 minute from now" is the safe minimum
            # (lets the tick catch it on the next sweep).
            next_fire_at = now + timedelta(minutes=1)

        if recurring is not None:
            validate_recurring(str(recurring))

        if catch_up is None:
            catch_up_resolved = recurring is None  # one-shot defaults True
        else:
            catch_up_resolved = bool(catch_up)

        # Build cron-specific meta. Schedule lives here, body in a chunk.
        meta: dict[str, Any] = {
            "status": "scheduled",
            "target": str(target).strip(),
            "next_fire_at": next_fire_at.isoformat(),
            "recurring": str(recurring).strip() if recurring else None,
            "catch_up": catch_up_resolved,
            "fire_count": 0,
            "last_fired_at": None,
        }

        # Apply default + caller tags.
        all_tag_strs: list[str] = list(self.default_tags_on_create)
        if tags:
            all_tag_strs.extend(tags)

        # Pre-validate tags + link to avoid half-created refs.
        parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in all_tag_strs]
        link_target = parse_link_target(link, store=self.store) if link is not None else None
        if rel is not None and link is None:
            raise BadInput(
                "rel= requires link= on create",
                next=(
                    "put(kind='cron', text='...', in_='10m', target='...', "
                    "link='memory:42', rel='derived-from')"
                ),
            )
        relation = validate_relation(rel)

        text_str = str(text).strip()
        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text_str,
                meta=meta,
                conn=conn,
            )
            # Body lives as a chunk so it's indexable.
            self.store.insert_blocks(
                ref.id,
                [
                    BlockInsert(
                        pos=0,
                        text=text_str,
                        meta={"chunk_kind": "cron_payload"},
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

        return self._render_create_ack(ref.id, next_fire_at, recurring)

    # ── rendering ────────────────────────────────────────────────────

    def _render_create_ack(  # type: ignore[override]
        self,
        ref_id: int,
        next_fire_at: datetime | None = None,
        recurring: str | None = None,
    ) -> Response:
        if next_fire_at is None:
            # Defensive fallback if called by the parent path with no extra
            # context; should not happen in practice on cron.
            body = f"scheduled cron id={ref_id}"
        else:
            schedule_note = (
                f"recurring: {recurring}" if recurring else "one-shot"
            )
            body = (
                f"scheduled cron id={ref_id} "
                f"({schedule_note}; next fire at {next_fire_at.isoformat()})"
            )
        body += render_next_section(
            [
                (f"get(kind='cron', id={ref_id})", "read the schedule"),
                (
                    f"delete(kind='cron', id={ref_id})",
                    "cancel this cron",
                ),
                ("get(kind='cron', id='/recent')", "list scheduled crons"),
            ]
        )
        return Response(body=body)

    def _render_one(self, ref: Any, tags: list[Any]) -> str:  # type: ignore[override]
        meta = ref.meta or {}
        lines = [f"# cron {ref.id}", ref.title]
        lines.append("")
        status = meta.get("status", "unknown")
        lines.append(f"status: {status}")
        target = meta.get("target")
        if target:
            lines.append(f"target: {target}")
        recurring = meta.get("recurring")
        if recurring:
            lines.append(f"recurring: {recurring}")
        else:
            lines.append("recurring: no (one-shot)")
        catch_up = meta.get("catch_up", False)
        lines.append(f"catch_up: {catch_up}")
        next_fire_at = meta.get("next_fire_at")
        if next_fire_at:
            lines.append(f"next fire: {next_fire_at}")
        last_fired_at = meta.get("last_fired_at")
        if last_fired_at:
            lines.append(f"last fired: {last_fired_at}")
        fire_count = meta.get("fire_count", 0)
        lines.append(f"fired {fire_count} time{'s' if fire_count != 1 else ''}")
        if tags:
            lines.append("")
            lines.append("tags: " + ", ".join(str(t) for t in tags))
        return "\n".join(lines)
