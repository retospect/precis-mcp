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
- ``tests-green {range}`` — ``range`` is ``A..B`` or one sha; open once CI
  is recorded green on its end (``B``), else (no verdict row) once ``B`` is
  in prod.
- ``drc-clean {board}`` — ``board`` is a pcb design (``pb<id>`` handle, slug,
  ``pcb:<slug>``); open once the design's newest recorded DRC run (the
  ``pcb_drc_runs`` / ``pcb_drc_findings`` record ``pcb_review``'s check leg
  and ``op='check'`` write, read through
  :meth:`Store.pcb_drc_findings_latest`) is fresh and carries no live
  (unwaived) ``severity='error'`` finding — the rule
  :func:`precis.pcb.route_state.classify` applies. No run yet, or a run the
  design has changed since, is closed: "not checked" is not "clean".

**Where prod's head comes from.** The server has no git. ``scripts/release``
records ``{head, deployed_at, landed}`` after each deploy into the
``release.prod_head`` setting (an ``app_settings`` row, see
:data:`precis.settings.REGISTRY`); ``landed`` accumulates the abbreviated
shas of every deploy's ``prev..head`` range, so a sha that rode an earlier
release stays visible. Reads go through :func:`precis.settings.get_str`, so
a freshly written record can take up to its 60 s cache TTL to show.

**tests-green reads a recorded verdict first.** ``scripts/release`` writes
``[{sha, range, verdict, run_id, at}]`` into the ``release.ci_verdicts``
setting when its gate step learns check.yml's verdict on a candidate —
green or red, before any deploy. The newest row (by ``at``) whose ``sha``
is the gate range's end, or whose ``range`` equals the gate's range, decides:
green opens, red closes with the run id. So a green candidate that has not
deployed reads open, and a red one reads closed with its reason.

**Fallback: the prod-head derivation.** With no verdict row for the range
(nothing recorded, or the key unreadable) the gate keeps its earlier
behaviour: ``scripts/release`` deploys a head only after check.yml went
green on exactly that head, and a green head covers its ancestors, so a
range whose end is in the ``release.prod_head`` record is green and one that
is not yet deployed is closed.

Evaluators never raise: bad args, an unreadable record or a store fault
return ``GateVerdict(False, <reason>)`` — a closed gate with its reason
beats a tick that dies on one malformed row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.utils import handle_registry
from precis.utils.slug_ref import resolve_live_slug_ref

if TYPE_CHECKING:
    from precis.store import Store

#: ``app_settings`` key ``scripts/release`` writes (it cannot import this
#: package, so the literal is repeated there; a test pins them equal).
PROD_HEAD_KEY = "release.prod_head"

#: ``app_settings`` key holding the recorded CI verdicts (same repeated-
#: literal arrangement as :data:`PROD_HEAD_KEY`).
CI_VERDICTS_KEY = "release.ci_verdicts"

#: Shortest sha prefix a gate may name — git's own default abbreviation.
MIN_SHA_LEN = 7

#: Chunk kind of a todo comment (mirrors ``precis.handlers.todo``).
_COMMENT_KIND = "todo_comment"

_HEX_RE = re.compile(rf"^[0-9a-f]{{{MIN_SHA_LEN},40}}$")


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """A gate's verdict: ``open`` plus a one-line reason for the work view."""

    open: bool
    why: str


def _closed(why: str) -> GateVerdict:
    return GateVerdict(False, why)


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
) -> GateVerdict:
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
        return GateVerdict(True, f"now is past {text}")
    return _closed(f"waiting until {text}")


# ── answer ────────────────────────────────────────────────────────


def _answer(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateVerdict:
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None:
        return _closed(f"answer: td{todo_id} not found")
    for tag in store.tags_for(todo_id):
        if tag.prefix == "STATUS" and str(tag.value) == "done":
            return GateVerdict(True, f"td{todo_id} is STATUS:done")
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == _COMMENT_KIND
    ]
    if comments:
        newest = max(comments, key=lambda c: (c.ord, c.id))
        if newest.text.lstrip().lower().startswith("answer:"):
            return GateVerdict(True, f"td{todo_id} newest comment is an answer")
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


def read_ci_verdicts(store: Store) -> list[dict[str, Any]]:
    """The well-formed rows of ``release.ci_verdicts``; ``[]`` when the key
    is absent, unparsable or not a list. A malformed row is skipped."""
    from precis import settings

    raw = settings.get_str(CI_VERDICTS_KEY, store=store)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [
        r
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("sha"), str)
        and r.get("verdict") in ("green", "red")
    ]


def _newest_verdict(store: Store, sha: str, rng: str) -> dict[str, Any] | None:
    """The newest verdict row for the gate's range end ``sha`` (prefix match)
    or whose recorded range equals ``rng``; ties on ``at`` go to the row
    later in the list."""
    want = rng.strip().lower()
    best: tuple[str, int] | None = None
    pick: dict[str, Any] | None = None
    for i, row in enumerate(read_ci_verdicts(store)):
        hit = _prefix_match(sha, row["sha"].lower()) or (
            str(row.get("range") or "").strip().lower() == want
        )
        if not hit:
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, pick = key, row
    return pick


def _prefix_match(a: str, b: str) -> bool:
    """Whether one sha is a prefix of the other (either may be abbreviated)."""
    n = min(len(a), len(b))
    return n >= MIN_SHA_LEN and a[:n] == b[:n]


def _sha_deployed(store: Store, sha: str) -> GateVerdict:
    """Whether ``sha`` is in the recorded prod head; the shared core of
    ``sha-in-prod`` and ``tests-green``."""
    rec = read_prod_head(store)
    if rec is None:
        return _closed(f"no prod head recorded ({PROD_HEAD_KEY}); {sha} not seen")
    head = rec["head"].lower()
    landed = [str(s).lower() for s in rec.get("landed") or [] if isinstance(s, str)]
    at = rec.get("deployed_at") or "unknown time"
    if _prefix_match(sha, head):
        return GateVerdict(True, f"{sha} is the prod head (deployed {at})")
    if any(_prefix_match(sha, s) for s in landed):
        return GateVerdict(True, f"{sha} landed in prod by head {head[:9]} ({at})")
    return _closed(f"{sha} not in prod yet (head {head[:9]}, deployed {at})")


def _clean_sha(raw: object, what: str) -> tuple[str, GateVerdict | None]:
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
) -> GateVerdict:
    sha, bad = _clean_sha(args.get("sha"), "sha-in-prod args.sha")
    return bad or _sha_deployed(store, sha)


def _tests_green(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateVerdict:
    raw = args.get("range")
    if not isinstance(raw, str) or not raw.strip():
        return _closed("tests-green needs args.range (A..B or one sha)")
    end = raw.strip().rsplit("..", 1)[-1].lstrip(".")
    sha, bad = _clean_sha(end, "tests-green args.range end")
    if bad:
        return bad
    row = _newest_verdict(store, sha, raw)
    if row is not None:
        at = row.get("at") or "unknown time"
        run = f"run {row['run_id']}" if row.get("run_id") else "no run id"
        short = row["sha"][:9]
        if row["verdict"] == "green":
            return GateVerdict(True, f"CI green on {short} ({run}, {at})")
        return _closed(f"CI red on {short} ({run}, {at})")
    res = _sha_deployed(store, sha)
    why = res.why.replace("not in prod yet", "no green deploy covers it yet")
    return GateVerdict(res.open, why)


# ── drc-clean ─────────────────────────────────────────────────────


def resolve_board(store: Store, raw: object) -> int:
    """The pcb ref id a board spelling names: a ``pb<id>`` handle, a slug,
    ``pcb:<slug>`` / ``pcb:<id>``, or a bare ref id. Raises ``BadInput``
    (empty or not a string/int) or ``NotFound`` (no live pcb design)."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise BadInput(
            f"board={raw!r} is not a pcb design",
            next="name the design as its handle 'pb<id>' or its slug",
        )
    text = str(raw).strip()
    if text.lower().startswith("pcb:"):
        text = text[4:].strip()
    if not text:
        raise BadInput(
            "board is empty",
            next="name the design as its handle 'pb<id>' or its slug",
        )
    return int(resolve_live_slug_ref(store, kind="pcb", id=text).id)


def _drc_clean(
    store: Store, args: dict[str, Any], *, todo_id: int, now: datetime
) -> GateVerdict:
    raw = args.get("board")
    if not isinstance(raw, str) or not raw.strip():
        return _closed("drc-clean needs args.board (a pcb handle 'pb<id>' or slug)")
    try:
        ref_id = resolve_board(store, raw)
    except (BadInput, NotFound) as e:
        return _closed(f"drc-clean: {e}")
    board = handle_registry.format_handle("pcb", ref_id)
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    if run_id is None:
        return _closed(f"no DRC run yet on {board}")
    run = str(run_id)[:12]
    if store.pcb_drc_run_stale(ref_id, run_id):
        return _closed(f"{board} changed since DRC run {run}; the check must re-run")
    errors = [
        f for f in findings if f.get("severity") == "error" and not f.get("waived_by")
    ]
    if errors:
        return _closed(f"{len(errors)} DRC error(s) on {board} (run {run})")
    warns = sum(1 for f in findings if f.get("severity") != "error")
    return GateVerdict(True, f"DRC clean on {board} (run {run}, {warns} warning(s))")


_Evaluator = Callable[..., GateVerdict]

#: Gate kind → evaluator. A new kind is one entry here and one in the spec.
EVALUATORS: dict[str, _Evaluator] = {
    "not-before": _not_before,
    "answer": _answer,
    "sha-in-prod": _sha_in_prod,
    "tests-green": _tests_green,
    "drc-clean": _drc_clean,
}


def evaluate(
    store: Store,
    gate: dict[str, Any],
    *,
    todo_id: int,
    now: datetime | None = None,
) -> GateVerdict:
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
    "CI_VERDICTS_KEY",
    "EVALUATORS",
    "MIN_SHA_LEN",
    "PROD_HEAD_KEY",
    "GateVerdict",
    "evaluate",
    "read_ci_verdicts",
    "read_prod_head",
    "resolve_board",
]
