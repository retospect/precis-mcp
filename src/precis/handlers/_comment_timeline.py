"""Shared comment timeline for gripe and todo: append, display policy, hide.

Why a policy and not "render everything": gr470905 grew 12 comment chunks
(~30 KB), overflowed the response frame, and the *newest* comment — usually
the one carrying the current state — landed on page 2. A bare ``get`` now
renders the newest few visible comments in full (newest first), older
visible ones as one-liners (so their existence and gist are not lost),
and a footer naming the ways to see more. ``view='comments'`` is the
complete, oldest-first read.

How many comments render in full (and how far each runs before a head
cut) is a per-kind choice — ``_KIND_POLICY`` below. Gripe's bare get went
on the render diet (2026-10-14: measured p95 6.7 KB, dominated by the
body plus three full comments plus the one-liner list): newest comment
only, head-capped, eight one-liners. Todo keeps the original newest-3.

Comments are chunks (``gripe_comment`` / ``todo_comment``); chunks are
append-only, so "hiding a stale comment" is not a chunk write. The hidden
comment numbers live in ``refs.meta['hidden_comments']`` (a sorted int list,
written through :meth:`Store.stamp_ref_meta`). The chunk is untouched — still
searchable, still in ``view='comments'`` (marked ``[hidden]``) — and
``delete(..., args={'comment': K, 'restore': True})`` removes K from the list.
A comment's number K is its chunk ``ord``: stable, shown in every header.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any, NamedTuple

from psycopg.errors import ForeignKeyViolation, UniqueViolation

from precis.errors import BadInput, Upstream
from precis.response import Response
from precis.store.types import ChunkInsert, ChunkRow, Ref

#: Comments rendered in full on a bare ``get`` (the default policy).
FULL_COMMENTS = 3
#: One-liner cap for the older visible comments.
ONE_LINER_CAP = 20
#: Characters of comment text in a one-liner.
ONE_LINER_CHARS = 100

HIDDEN_META_KEY = "hidden_comments"


class _Policy(NamedTuple):
    """Default-read comment budgets for one kind.

    ``full_count``: how many of the newest visible comments render in
    full. ``full_chars``: per-comment head cap on those full renders
    (``None`` = uncapped). ``one_liner_cap``: how many older visible
    comments get a one-line gist before ``+N older``.
    """

    full_count: int
    full_chars: int | None
    one_liner_cap: int


_DEFAULT_POLICY = _Policy(FULL_COMMENTS, None, ONE_LINER_CAP)
#: Kinds whose bare get renders a leaner comment timeline than the
#: default. gripe measured p95 6.7 KB / max 9.5 KB over a 24h mining
#: window (2026-10-14): the newest comment carries the current state, the
#: rest is reachable at ``view='comments'``.
_KIND_POLICY: dict[str, _Policy] = {
    "gripe": _Policy(full_count=1, full_chars=800, one_liner_cap=8),
}


def _policy(kind: str) -> _Policy:
    return _KIND_POLICY.get(kind, _DEFAULT_POLICY)


#: A text up to ``budget * CUT_SLACK`` renders whole: cutting a body 22%
#: over budget at a line boundary hid 704 of 1833 chars (gr489192), more
#: than the overage it saved.
CUT_SLACK = 1.25
#: Share of the kept budget that goes to the head; the rest is the tail.
#: Comments and gripe bodies end on the current-state ask ("Needs …",
#: "Fix shape"), so a head-only cut dropped exactly that (gr489190).
HEAD_SHARE = 0.6


def clip_middle(text: str, budget: int | None, pointer: str) -> str:
    """Keep the head and the tail of an over-budget ``text``, cut the middle.

    ``budget=None`` or a text within ``budget * CUT_SLACK`` passes through
    whole. Otherwise the head gets ``HEAD_SHARE`` of the budget and the
    tail the rest, each cut at a line boundary when one falls in the
    outer half of its share (else at a space, else hard). The marker
    names the cut size and ``pointer`` — the read that has the whole
    text (gr489191).
    """
    if budget is None or len(text) <= budget * CUT_SLACK:
        return text
    head_n = int(budget * HEAD_SHARE)
    tail_n = budget - head_n
    head_end = text.rfind("\n", 0, head_n)
    if head_end < head_n // 2:
        head_end = text.rfind(" ", 0, head_n)
    if head_end < head_n // 2:
        head_end = head_n
    floor = len(text) - tail_n
    tail_start = text.find("\n", floor, floor + tail_n // 2)
    if tail_start < 0:
        tail_start = text.find(" ", floor, floor + tail_n // 2)
    tail_start = floor if tail_start < 0 else tail_start + 1
    return (
        text[:head_end].rstrip()
        + f"\n[… {tail_start - head_end} chars cut — {pointer} …]\n"
        + text[tail_start:].lstrip()
    )


#: ``args=`` keys ``delete`` reads on a comment-capable kind.
DELETE_ARGS_KEYS = frozenset({"comment", "restore"})


def hidden_comments(ref: Ref) -> set[int]:
    """The comment numbers hidden on ``ref`` (empty when none)."""
    raw = (ref.meta or {}).get(HIDDEN_META_KEY) or []
    return {int(n) for n in raw if isinstance(n, int) and not isinstance(n, bool)}


def _stamp(chunk: ChunkRow) -> str:
    return chunk.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _one_liner(chunk: ChunkRow) -> str:
    gist = " ".join(chunk.text.split())
    if len(gist) > ONE_LINER_CHARS:
        gist = gist[:ONE_LINER_CHARS].rstrip() + "…"
    return f"comment {chunk.ord} · {_stamp(chunk)} · {gist}"


def render_comments(
    ref: Ref, comments: list[ChunkRow], *, kind: str, full: bool = False
) -> str:
    """The comment part of a ``get`` ("" when the ref has no comments).

    ``full=False`` is the bare-``get`` policy (``_KIND_POLICY``);
    ``full=True`` is ``view='comments'`` (all, oldest first, hidden
    ones marked) — the complete read the footer points at.
    """
    if not comments:
        return ""
    ordered = sorted(comments, key=lambda c: c.ord)
    hidden = hidden_comments(ref)
    if full:
        parts = [
            f"## comment {c.ord} · {_stamp(c)}"
            f"{' [hidden]' if c.ord in hidden else ''}\n{c.text}"
            for c in ordered
        ]
        return "\n\n".join(parts)
    policy = _policy(kind)
    visible = [c for c in ordered if c.ord not in hidden]
    newest = list(reversed(visible[-policy.full_count :]))
    older = (
        list(reversed(visible[: -policy.full_count]))
        if len(visible) > policy.full_count
        else []
    )
    pointer = f"get(kind={kind!r}, id={ref.id}, view='comments')"
    parts = [
        f"## comment {c.ord} · {_stamp(c)}\n"
        + clip_middle(c.text, policy.full_chars, pointer)
        for c in newest
    ]
    if older:
        lines = [_one_liner(c) for c in older[: policy.one_liner_cap]]
        if len(older) > policy.one_liner_cap:
            lines.append(f"+{len(older) - policy.one_liner_cap} older")
        parts.append("\n".join(lines))
    n_hidden = len([c for c in ordered if c.ord in hidden])
    if n_hidden:
        parts.append(
            f"{n_hidden} hidden comment{'s' if n_hidden != 1 else ''} — "
            "view='comments' shows them"
        )
    if policy.full_chars is None:
        shown = (
            "newest in full"
            if policy.full_count == 1
            else f"newest {policy.full_count} in full"
        )
    else:
        shown = (
            "newest (long ones cut mid-text)"
            if policy.full_count == 1
            else f"newest {policy.full_count} (long ones cut mid-text)"
        )
    parts.append(
        f"comments: {shown}, older as one-liners · "
        f"all, oldest first: get(kind={kind!r}, id={ref.id}, view='comments') · "
        f"hide a stale one: delete(kind={kind!r}, id={ref.id}, args={{'comment': <K>}})"
    )
    return "\n\n".join(parts)


def _insert_next(store: Any, ref_id: int, comment_kind: str, text: str) -> int:
    existing = store.chunks.list_chunks_for_ref(ref_id)
    number = max((c.ord for c in existing), default=0) + 1
    with store.tx() as conn:
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=number, text=text, meta={"chunk_kind": comment_kind})],
            conn=conn,
        )
    return number


def append_comment(
    store: Any, ref: Ref, *, kind: str, comment_kind: str, text: str
) -> Response:
    """Append ``text`` as a ``comment_kind`` chunk on ``ref``.

    The new ord is ``max(existing ord, 0) + 1`` (not a chunk count): it can
    never collide with the body chunk or a tag-overflow chunk, even when ords
    have gaps. Comment numbers therefore start at 1.
    """
    try:
        number = _insert_next(store, ref.id, comment_kind, text)
    except UniqueViolation:
        # A concurrent append (or first body write) took the same ord;
        # recompute once. A second collision surfaces as an error.
        number = _insert_next(store, ref.id, comment_kind, text)
    except ForeignKeyViolation as e:
        # ``chunks.chunk_kind`` FK against ``chunk_kinds.slug``: the seed
        # migration for this comment kind is missing on this DB — server-side
        # schema drift, not user error.
        raise Upstream(
            f"could not append {comment_kind} chunk to {kind} id={ref.id} — "
            "likely missing chunk_kind seed",
            next=[
                f"verify: SELECT slug FROM chunk_kinds WHERE slug='{comment_kind}'",
                "re-apply the migration that seeds it (gripe_comment: 0005, "
                "todo_comment: 0195) against this DB",
            ],
        ) from e
    policy = _policy(kind)
    shown = (
        "the newest comment"
        if policy.full_count == 1
        else f"the newest {policy.full_count}"
    )
    return Response(
        body=(
            f"appended comment {number} to {kind} {ref.id}. A bare get shows "
            f"{shown} plus older one-liners; view='comments' shows all; hide "
            f"a stale one with delete(kind={kind!r}, id={ref.id}, "
            f"args={{'comment': {number}}})."
        )
    )


def _set_hidden(store: Any, ref_id: int, number: int, *, hide: bool) -> None:
    """Add/remove ``number`` in ``meta['hidden_comments']`` atomically.

    One UPDATE computes the new set from the row's current value, so two
    concurrent hides of different comments cannot drop one (a Python
    read-modify-write could). The ``comment_hidden`` / ``comment_restored``
    ref_events row is the audit trail (``view='log'``).
    """
    op = (
        "(SELECT COALESCE(jsonb_agg(DISTINCT v ORDER BY v), '[]'::jsonb) FROM ("
        "SELECT (x)::int AS v FROM jsonb_array_elements_text("
        "COALESCE(meta->'hidden_comments', '[]'::jsonb)) x "
        + ("UNION SELECT %s" if hide else "EXCEPT SELECT %s")
        + ") s)"
    )
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), "
            f"'{{hidden_comments}}', {op}), updated_at = now() WHERE ref_id = %s",
            (number, ref_id),
        )
        store.append_event(
            ref_id,
            source="agent",
            event="comment_hidden" if hide else "comment_restored",
            payload={"comment": number},
            conn=conn,
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def delete_comment(
    store: Any,
    ref: Ref,
    *,
    kind: str,
    comment_kind: str,
    args: dict[str, Any],
) -> Response:
    """``delete(kind, id=N, args={'comment': K[, 'restore': True]})``.

    Hides (or restores) comment K by editing ``refs.meta['hidden_comments']``.
    Never touches the chunk and never retires the ref.
    """
    raw = args.get("comment")
    if isinstance(raw, bool) or raw is None:
        raise BadInput(
            "args={'comment': K} needs the comment number K (the 'comment K' "
            "in its header)",
            next=f"delete(kind={kind!r}, id={ref.id}, args={{'comment': <K>}})",
        )
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise BadInput(
            f"comment must be an integer, got {raw!r}",
            next=f"delete(kind={kind!r}, id={ref.id}, args={{'comment': <K>}})",
        ) from None
    chunks = store.chunks.list_chunks_for_ref(ref.id)
    numbers = sorted(c.ord for c in chunks if c.chunk_kind == comment_kind)
    target = next((c for c in chunks if c.ord == number), None)
    if target is not None and target.chunk_kind != comment_kind:
        raise BadInput(
            f"{number} is the {kind}'s {target.chunk_kind.split('_')[-1]}, not a "
            "comment — only comments can be hidden",
            next=(
                f"comments on {kind} {ref.id}: {numbers}"
                if numbers
                else f"{kind} {ref.id} has no comments"
            ),
        )
    if target is None:
        raise BadInput(
            f"{kind} {ref.id} has no comment {number}",
            next=(
                f"comments on {kind} {ref.id}: {numbers}"
                if numbers
                else f"{kind} {ref.id} has no comments"
            ),
        )
    hidden = hidden_comments(ref)
    if _truthy(args.get("restore")):
        if number not in hidden:
            return Response(body=f"comment {number} of {kind} {ref.id} was not hidden.")
        _set_hidden(store, ref.id, number, hide=False)
        return Response(body=f"restored comment {number} of {kind} {ref.id}.")
    if number in hidden:
        return Response(body=f"comment {number} of {kind} {ref.id} is already hidden.")
    _set_hidden(store, ref.id, number, hide=True)
    return Response(
        body=(
            f"hid comment {number} of {kind} {ref.id} (still searchable; "
            "view='comments' shows it marked [hidden]). Undo: "
            f"delete(kind={kind!r}, id={ref.id}, "
            f"args={{'comment': {number}, 'restore': True}})."
        )
    )
