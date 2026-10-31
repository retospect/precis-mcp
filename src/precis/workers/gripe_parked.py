"""``gripe_parked`` — the parked-gripe wake pass (gr543711).

A ``STATUS:parked`` gripe waits on the condition in ``meta.until``
(``{type, value, note}``, :mod:`precis.utils.parked`). Each fire
evaluates every live parked gripe's condition; a met one goes back to
the triage entry status (``STATUS:open``) with one comment
``unparked: <condition> met (<evidence>)``. Not met writes nothing —
the point of the shelf is that a waiting gripe costs no attention.

Evaluators:

- ``ref_done`` — the named ``gr<N>``/``td<N>`` reached a terminal
  STATUS (gripe done/wontfix; todo the TERMINAL_TODO_STATUSES set).
- ``sha_in_prod`` — the sha is in the recorded prod head
  (:func:`precis.plans.gates` — ``release.prod_head``; the server has
  no git, so "deployed" means *recorded* deployed).
- ``spec_landed`` — a ``docs/backlog`` spec: not evaluable from the DB
  (no repo view); stays parked — a ``text`` condition or a human
  unpark covers it.
- ``date`` — UTC now reached the ISO date.
- ``text`` — the cheapest LLM tier judges condition + the gripe's
  newest 3 comments; ``met``/``not_met``/``unsure``, unsure = not met.

The cadence interval resolves live through the settings registry
(``gripe.unpark_interval_hours``, default 48 — the "look at them from
time to time" knob).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from precis.utils.parked import describe_until, parse_until_date

log = logging.getLogger(__name__)

SERVICE_NAME = "gripe_parked"
#: The route-log source the ``text`` condition's calls bill under.
_LLM_SOURCE = "gripe_parked:until"
#: Newest comments fed to the ``text`` evaluator.
_TEXT_COMMENT_TAIL = 3

_UNPARK_STATUS = "open"


@dataclass
class UnparkResult:
    """What one pass did — carries the ``BatchResult`` fields the
    scheduler logs (``claimed``/``ok``/``failed``/``extra``) plus the
    per-condition detail a test or report reads."""

    handler: str = SERVICE_NAME
    claimed: int = 0
    ok: int = 0
    failed: int = 0
    extra: str = ""
    parked: int = 0
    woke: list[int] = field(default_factory=list)
    waiting: int = 0
    no_condition: int = 0
    errors: int = 0


def parked_list(store: Any) -> list[int]:
    """Every live ``STATUS:parked`` gripe id, oldest first."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "AND t.namespace = 'STATUS' AND t.value = 'parked' "
            "WHERE r.kind = 'gripe' AND r.retired_at IS NULL "
            "ORDER BY r.ref_id"
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ref_done(store: Any, value: str) -> tuple[bool, str]:
    kind = "gripe" if value.startswith("gr") else "todo"
    ref_id = int(value[2:])
    status = next(
        (
            str(t.value)
            for t in store.tags_for(ref_id)
            if t.namespace == "closed" and t.prefix == "STATUS"
        ),
        None,
    )
    if status is None:
        return False, f"{value} carries no STATUS"
    terminal: frozenset[str] | set[str]
    if kind == "gripe":
        terminal = {"done", "wontfix"}
    else:
        from precis.todo import TERMINAL_TODO_STATUSES

        terminal = TERMINAL_TODO_STATUSES
    if status in terminal:
        return True, f"{value} is {status}"
    return False, f"{value} is {status}"


def _sha_in_prod(store: Any, value: str) -> tuple[bool, str]:
    from precis.plans.gates import _sha_deployed

    verdict = _sha_deployed(store, value)
    return verdict.open, verdict.why


def _text_met(
    until: dict[str, Any],
    comments: list[str],
    *,
    llm: Any,
) -> tuple[bool, str]:
    """The ``text`` condition, judged by the cheapest tier; ``unsure``
    (and any transport failure) is not met."""
    prompt = (
        "A parked bug report carries a wake condition. Decide whether the "
        "condition is met from the report's newest comments.\n\n"
        f"condition: {until.get('value', '')}\n"
        + (f"note: {until['note']}\n" if until.get("note") else "")
        + "\nnewest comments:\n"
        + "\n".join(f"- {c}" for c in comments)
        + "\n\nAnswer exactly one word: met, not_met, or unsure."
    )
    result = llm(prompt)
    answer = (getattr(result, "text", "") or "").strip().lower().split()
    word = answer[0].rstrip(".,;:") if answer else ""
    if word == "met":
        return True, "text judge: met"
    if word == "unsure":
        return False, "text judge: unsure"
    return False, f"text judge: {word or 'no answer'}"


def _default_llm(store: Any) -> Any:
    """The cheapest-tier call through the router; a paused/failed call
    surfaces as not-met by its empty text."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    def call(prompt: str) -> Any:
        return route(
            LlmRequest(
                tier=Tier.SMALL,
                prompt=prompt,
                source=_LLM_SOURCE,
                work_class="background",
                max_tokens=8,
            )
        )

    return call


def _eval(
    store: Any,
    ref: Any,
    until: dict[str, Any],
    *,
    llm: Any,
    now: datetime,
) -> tuple[bool, str]:
    """``(met, evidence)`` for one parked gripe's condition."""
    typ = until.get("type")
    value = str(until.get("value") or "")
    if typ == "ref_done":
        return _ref_done(store, value)
    if typ == "sha_in_prod":
        return _sha_in_prod(store, value)
    if typ == "date":
        try:
            met = now >= parse_until_date(value)
        except Exception:
            return False, f"until date {value!r} unparsable"
        return met, f"now is {'past' if met else 'before'} {value}"
    if typ == "text":
        comments = [
            c.text
            for c in sorted(
                (
                    c
                    for c in store.chunks.list_chunks_for_ref(ref.id)
                    if c.chunk_kind == "gripe_comment"
                ),
                key=lambda c: c.ord,
                reverse=True,
            )[:_TEXT_COMMENT_TAIL]
        ]
        return _text_met(until, comments, llm=llm)
    # spec_landed (and anything newer this build doesn't know): the
    # DB-only pass can't see the repo — never met here.
    return False, f"{typ} not evaluable here"


def run_gripe_parked_pass(store: Any, *, llm: Any = None) -> UnparkResult:
    """One wake pass: flip every parked gripe whose condition is met."""
    from precis.cli.gripe import unpark_gripe

    res = UnparkResult()
    ids = parked_list(store)
    res.parked = res.claimed = len(ids)
    if not ids:
        return res
    if llm is None:
        llm = _default_llm(store)
    now = datetime.now(UTC)
    for gid in ids:
        ref = store.get_ref(kind="gripe", id=gid)
        if ref is None:
            continue
        until = (ref.meta or {}).get("until")
        if not isinstance(until, dict) or not until.get("type"):
            res.no_condition += 1
            log.warning("gripe_parked: gr%d parked with no meta.until", gid)
            continue
        try:
            met, evidence = _eval(store, ref, until, llm=llm, now=now)
        except Exception:
            res.errors += 1
            log.exception("gripe_parked: eval failed for gr%d", gid)
            continue
        if not met:
            res.waiting += 1
            continue
        if not unpark_gripe(
            store, gid, f"unparked: {describe_until(until)} met ({evidence})"
        ):
            res.errors += 1
            log.warning("gripe_parked: unpark of gr%d refused", gid)
            continue
        res.woke.append(gid)
    res.ok = len(res.woke)
    res.failed = res.errors
    if res.waiting or res.no_condition:
        res.extra = f"waiting={res.waiting} no_condition={res.no_condition}"
    return res


def resolve_interval_s(store: Any) -> int:
    """The live interval: ``gripe.unpark_interval_hours`` (settings
    registry), default 48 h, floored at one minute."""
    from precis import settings

    hours = settings.get_float("gripe.unpark_interval_hours", store=store)
    return max(60, int((hours or 48.0) * 3600))


__all__ = [
    "SERVICE_NAME",
    "UnparkResult",
    "parked_list",
    "resolve_interval_s",
    "run_gripe_parked_pass",
]
