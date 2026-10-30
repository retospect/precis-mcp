"""Gate evaluators — the predicates a plan's gate todos wait on.

Spec: ``docs/backlog/dark-factory.md`` § "Plans" (node table row *gate*,
"The dispatcher tick" step 1). A gate is a todo with
``meta.gate = {kind, args}``; the dispatcher tick calls :func:`evaluate` for
each and nothing is dispatched for it. Kinds:

- ``not-before {ts}`` — open once now (UTC) has reached ``ts`` (ISO-8601 with
  ``Z`` or an offset; a naive timestamp is refused, not guessed at).
- ``answer`` — the gate todo is an ask-user todo; open once it is
  ``STATUS:done`` or its newest comment starts ``answer:`` (the convention in
  :mod:`precis.handlers.todo`).
- ``sha-in-prod {sha}`` — open once ``sha`` (hex prefix, at least 7 chars) is
  the deployed prod head or one of the commits that deploy brought in.
- ``tests-green {range}`` — ``range`` is ``A..B`` or one sha; open once its
  end (``B``) is in prod.

**Where prod's head comes from.** The server has no git. ``scripts/release``
records ``{head, deployed_at, landed}`` after each deploy into the
``release.prod_head`` setting (an ``app_settings`` row, see
:data:`precis.settings.REGISTRY`); ``landed`` accumulates the abbreviated
shas of every deploy's ``prev..head`` range, so a sha that rode an earlier
release stays visible. Reads go through :func:`precis.settings.get_str`, so
a freshly written record can take up to its 60 s cache TTL to show.

**tests-green is derived, not recorded.** Nothing in prod stores a CI
verdict, but ``scripts/release`` deploys a head only after check.yml went
green on exactly that head (``wait_ci``), and a green head covers its
ancestors. So a range whose end is in the release record has a green gate;
a range whose end is not yet deployed is closed even if CI is already green
on a release candidate (it has not reached prod, and the server cannot see
the candidate). Revisit if a verdict row ever lands.

Evaluators never raise: bad args, an unreadable record or a store fault
return ``GateResult(False, <reason>)`` — a closed gate with its reason
beats a tick that dies on one malformed row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: ``app_settings`` key ``scripts/release`` writes (it cannot import this
#: package, so the literal is repeated there; a test pins them equal).
PROD_HEAD_KEY = "release.prod_head"

#: Shortest sha prefix a gate may name — git's own default abbreviation.
MIN_SHA_LEN = 7

#: Chunk kind of a todo comment (mirrors ``precis.handlers.todo``).
_COMMENT_KIND = "todo_comment"

_HEX_RE = re.compile(rf"^[0-9a-f]{{{MIN_SHA_LEN},40}}$")


@dataclass(frozen=True, slots=True)
class GateResult:
    """A gate's verdict: ``open`` plus a one-line reason for the work view."""

    open: bool
    why: str


def _closed(why: str) -> GateResult:
    return GateResult(False, why)


def _args(gate: dict[str, Any]) -> dict[str, Any] | None:
    """``gate['args']`` as a dict; absent args fall back to the gate's own
    keys (``{kind, ts}``) so a flat spec still works. ``None`` = malformed."""
    raw = gate.get("args")
    if raw is None:
        return {k: v for k, v in gate.items() if k != "kind"}
    return raw if isinstance(raw, dict) else None


# ── not-before ────────────────────────────────────────────────────


def _not_before(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateResult:
    raw = args.get("ts")
    if not isinstance(raw, str) or not raw.strip():
        return _closed("not-before needs args.ts (ISO-8601 timestamp with Z)")
    text = raw.strip()
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return _closed(f"not-before: ts {raw!r} is not ISO-8601")
    if ts.tzinfo is None:
        return _closed(f"not-before: ts {raw!r} has no Z or offset; UTC is required")
    if now >= ts:
        return GateResult(True, f"now is past {text}")
    return _closed(f"waiting until {text}")


# ── answer ────────────────────────────────────────────────────────


def _answer(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateResult:
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None:
        return _closed(f"answer: td{todo_id} not found")
    for tag in store.tags_for(todo_id):
        if tag.prefix == "STATUS" and str(tag.value) == "done":
            return GateResult(True, f"td{todo_id} is STATUS:done")
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == _COMMENT_KIND
    ]
    if comments:
        newest = max(comments, key=lambda c: (c.ord, c.id))
        if newest.text.lstrip().lower().startswith("answer:"):
            return GateResult(True, f"td{todo_id} newest comment is an answer")
    return _closed(f"waiting for an answer on td{todo_id}")


# ── sha-in-prod / tests-green ─────────────────────────────────────


def read_prod_head(store: Store) -> dict[str, Any] | None:
    """The ``release.prod_head`` record, or ``None`` when absent or unreadable."""
    from precis import settings

    raw = settings.get_str(PROD_HEAD_KEY, store=store)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("head"), str):
        return None
    return rec


def _prefix_match(a: str, b: str) -> bool:
    """Whether one sha is a prefix of the other (either may be abbreviated)."""
    n = min(len(a), len(b))
    return n >= MIN_SHA_LEN and a[:n] == b[:n]


def _sha_deployed(store: Store, sha: str) -> GateResult:
    """Whether ``sha`` is in the recorded prod head; the shared core of
    ``sha-in-prod`` and ``tests-green``."""
    rec = read_prod_head(store)
    if rec is None:
        return _closed(f"no prod head recorded ({PROD_HEAD_KEY}); {sha} not seen")
    head = rec["head"].lower()
    landed = [str(s).lower() for s in rec.get("landed") or [] if isinstance(s, str)]
    at = rec.get("deployed_at") or "unknown time"
    if _prefix_match(sha, head):
        return GateResult(True, f"{sha} is the prod head (deployed {at})")
    if any(_prefix_match(sha, s) for s in landed):
        return GateResult(True, f"{sha} landed in prod by head {head[:9]} ({at})")
    return _closed(f"{sha} not in prod yet (head {head[:9]}, deployed {at})")


def _clean_sha(raw: object, what: str) -> tuple[str, GateResult | None]:
    if not isinstance(raw, str):
        return "", _closed(f"{what} needs a hex sha of at least {MIN_SHA_LEN} chars")
    sha = raw.strip().lower()
    if not _HEX_RE.match(sha):
        return "", _closed(
            f"{what}: {raw!r} is not a hex sha of {MIN_SHA_LEN}-40 chars"
        )
    return sha, None


def _sha_in_prod(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateResult:
    sha, bad = _clean_sha(args.get("sha"), "sha-in-prod args.sha")
    return bad or _sha_deployed(store, sha)


def _tests_green(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateResult:
    raw = args.get("range")
    if not isinstance(raw, str) or not raw.strip():
        return _closed("tests-green needs args.range (A..B or one sha)")
    end = raw.strip().rsplit("..", 1)[-1].lstrip(".")
    sha, bad = _clean_sha(end, "tests-green args.range end")
    if bad:
        return bad
    res = _sha_deployed(store, sha)
    why = res.why.replace("not in prod yet", "no green deploy covers it yet")
    return GateResult(res.open, why)


_Evaluator = Callable[..., GateResult]

#: Gate kind → evaluator. A new kind is one entry here and one in the spec.
EVALUATORS: dict[str, _Evaluator] = {
    "not-before": _not_before,
    "answer": _answer,
    "sha-in-prod": _sha_in_prod,
    "tests-green": _tests_green,
}


def evaluate(
    store: Store,
    gate: dict[str, Any],
    *,
    todo_id: int,
    now: datetime | None = None,
) -> GateResult:
    """Evaluate one gate spec ``{kind, args}`` on todo ``todo_id``.

    ``todo_id`` is the todo carrying the gate (the ask itself for
    ``answer``). ``now`` overrides the clock for ``not-before``. Never
    raises — see the module docstring.
    """
    try:
        if not isinstance(gate, dict):
            return _closed("gate must be a dict {kind, args}")
        kind = gate.get("kind")
        fn = EVALUATORS.get(kind) if isinstance(kind, str) else None
        if fn is None:
            return _closed(
                f"unknown gate kind {kind!r}; known: {', '.join(sorted(EVALUATORS))}"
            )
        args = _args(gate)
        if args is None:
            return _closed(f"{kind}: args must be a dict")
        return fn(store, args, todo_id=todo_id, now=now or datetime.now(UTC))
    except Exception as e:  # a gate fault is a closed gate, not a dead tick
        return _closed(f"{type(e).__name__} evaluating gate: {str(e)[:160]}")


__all__ = [
    "EVALUATORS",
    "MIN_SHA_LEN",
    "PROD_HEAD_KEY",
    "GateResult",
    "evaluate",
    "read_prod_head",
]
