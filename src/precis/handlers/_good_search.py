"""The ``good=True`` deep-search submit surface.

``search(kind='paper', q=…, good=True)`` does **not** search inline —
it mints a ``good_search`` coordinator campaign
(:mod:`precis.workers.job_types.good_search`) and returns an async
handle the caller polls. This module owns that submit path so
``handlers/paper.py`` stays a thin dispatch.

Per the design doc (§Parenting, ADR 0044): the campaign job rides the
intent lane — a bare interactive ``good=True`` parents it on an
auto-minted lightweight ephemeral todo carrying
``meta.auto_check={'type': 'child_job_succeeded'}``. Because the
campaign's triage children parent on the *coordinator* (not the todo),
the todo's only child job is the campaign, so the auto-check closes it
exactly when the campaign succeeds; a campaign failure bubbles
``child-failed:`` onto it, which is also correct.

Reuse + bounds (§Parenting / §Worker contention):

- ``idem_key`` is a stable hash of ``q``/``queries``/``answers``/
  ``want`` — an identical in-flight submit is reused, not duplicated.
- A global concurrent-campaign cap (``COUNT`` of non-terminal
  ``good_search`` jobs) is enforced *here*, not in ``validate_submit``
  (which doesn't fire for link-less submits). Default 3,
  ``PRECIS_GOOD_SEARCH_MAX_CONCURRENT``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from precis.errors import BadInput
from precis.response import Response

_TERMINAL = ("succeeded", "failed", "cancelled")

#: Parses ``id=N`` out of the todo / job put acks.
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")

#: Title cap for the ephemeral parent todo.
_TITLE_CAP = 96


def _max_concurrent() -> int:
    """Global cap on non-terminal good_search campaigns (default 3)."""
    try:
        n = int(os.environ.get("PRECIS_GOOD_SEARCH_MAX_CONCURRENT", "3"))
    except ValueError:
        return 3
    return max(1, min(64, n))


def _idem_key(q: str, queries: list[str], answers: list[str], want: str) -> str:
    """Stable campaign identity: same question → same in-flight job."""
    payload = json.dumps(
        {"q": q, "queries": queries, "answers": answers, "want": want},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()
    return f"good_search:{digest}"


def _count_nonterminal_campaigns(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*)
              FROM refs r
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = 'good_search'
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                   )
            """,
            (list(_TERMINAL),),
        ).fetchone()
    return int(row[0]) if row else 0


def _ack(job_id: int, *, todo_id: int | None, reused: bool) -> Response:
    """The async-handle envelope (design §MCP surface, wait=0)."""
    lines = []
    if reused:
        lines.append(
            f"deep search already in flight: job={job_id} status=running "
            "(identical q/queries/answers — reusing it instead of "
            "starting a duplicate)"
        )
    else:
        lines.append(f"deep search queued: job={job_id} status=queued")
    lines.append(f"poll: get(kind='job', id={job_id})")
    note = (
        "note: the campaign fuses q+queries+answers, fans out triage "
        "children, and lands a merged ranked verdict on the job "
        "(job summary + meta.result) — poll until STATUS:succeeded."
    )
    if todo_id is not None:
        note += f" parent todo #{todo_id} auto-closes on success."
    lines.append(note)
    return Response(body="\n".join(lines))


def submit_good_search(
    store: Any,
    *,
    q: str,
    queries: list[str] | None = None,
    answers: list[str] | None = None,
    want: str = "chunks",
) -> Response:
    """Mint (or reuse) a ``good_search`` campaign; return the handle.

    The thin-slice surface: async handle only (``wait=0`` semantics);
    ``wait=<seconds>`` block-poll is phase 2.
    """
    # Local imports — the handler layer already imports this module's
    # siblings; keep the job/todo handlers off the module import path.
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler
    from precis.handlers.todo import TodoHandler

    clean_queries = [str(s).strip() for s in (queries or []) if s and str(s).strip()]
    clean_answers = [str(s).strip() for s in (answers or []) if s and str(s).strip()]
    idem = _idem_key(q, clean_queries, clean_answers, want)

    jobs = JobHandler(hub=Hub(store=store))
    existing = jobs._lookup_idem(idem)
    if existing is not None:
        return _ack(existing, todo_id=None, reused=True)

    running = _count_nonterminal_campaigns(store)
    cap = _max_concurrent()
    if running >= cap:
        raise BadInput(
            f"{running} deep searches already in flight (cap {cap}) — "
            "retry later or poll a running one",
            next=(
                "search(kind='job', q='good_search', tags=['STATUS:running']) "
                "to find them, or re-issue this call in a few minutes"
            ),
        )

    # Ephemeral parent todo (intent lane). Its only child job will be
    # the campaign, so child_job_succeeded closes it exactly on success.
    title = f"deep search: {q}"
    if len(title) > _TITLE_CAP:
        title = title[: _TITLE_CAP - 1] + "…"
    todos = TodoHandler(hub=Hub(store=store))
    todo_ack = todos.put(
        text=title,
        tags=["ephemeral"],
        meta={"auto_check": {"type": "child_job_succeeded"}},
    )
    m = _ID_IN_ACK.search(todo_ack.body)
    if m is None:  # pragma: no cover — put()'s ack shape changed
        raise RuntimeError(
            f"good_search: could not parse todo id from ack: {todo_ack.body!r}"
        )
    todo_id = int(m.group(1))

    resp = jobs.put(
        job_type="good_search",
        executor="coordinator",
        parent_id=todo_id,
        idem_key=idem,
        params={
            "q": q,
            "queries": clean_queries,
            "answers": clean_answers,
            "want": want,
        },
    )
    m = _ID_IN_ACK.search(resp.body)
    if m is None:  # pragma: no cover — put()'s ack shape changed
        raise RuntimeError(
            f"good_search: could not parse job id from ack: {resp.body!r}"
        )
    job_id = int(m.group(1))

    reused = resp.body.startswith("existing job")
    if reused:
        # Lost the idem race between our pre-check and the put — the
        # fresh todo has no child and would linger as an orphan;
        # soft-delete it (recoverable, same shape as operator delete).
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE ref_id = %s AND kind = 'todo'",
                (todo_id,),
            )
            conn.commit()
        return _ack(job_id, todo_id=None, reused=True)

    return _ack(job_id, todo_id=todo_id, reused=False)


__all__ = ["submit_good_search"]
