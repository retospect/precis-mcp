"""``gripe_validate`` job_type — the deploy-time verifier (gr543584, gr519676).

The last rung of the gripe status ladder (gr541800, td541978): a gripe
parked at a verify state waits on the deploy its fix rode.
``scripts/gripe-validate`` does the git-side of this from a checkout
after a rollout; this job is the DB-only equivalent for a recurring run
on any worker host — it reads the ``release.prod_head`` record
``scripts/release`` writes after a deploy (``{head, deployed_at,
landed, release}``) and ``release.ci_verdicts`` for the gate verdict.

Per cohort (:func:`precis.cli.gripe.verify_list`):

- ``auto_verify`` (and legacy ``verify``): the check is the sha in its
  newest ``resolved:``/``fix-sha:`` comment landing in prod plus a
  green release gate (a recorded red ``ci_verdicts`` verdict on the
  deployed head fails it). Live and green → ``STATUS:done`` via
  :func:`~precis.cli.gripe.verify_done`. Sha in prod but the gate red
  → verification failed: comment + back to ``STATUS:triaged``. Not yet
  in prod → pending; no sha in a comment → one dedup'd note (it can't
  be verified).
- ``user_verify``: a human checks — :func:`~precis.cli.gripe.user_verify_sync`
  mints or refreshes ONE ask-user todo listing the cohort and closes
  the covered gripes once the ask is answered (gr519676).
- ``needs_spec`` (gr543698): listed for the report; the spec-naming
  check needs git, so ``scripts/gripe-validate`` owns the close.

``params.dry_run`` lists without writing. Idempotent and
non-interactive, so the minter's recurring todo is the whole schedule::

    put(kind='todo', text='gripe_validate tick (verify gripes to done)',
        meta={'schedule': {'every': '15m'}, 'executor': 'job_inproc',
              'job_type': 'gripe_validate'})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from precis.plans.gates import MIN_SHA_LEN, read_ci_verdicts, read_prod_head
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "gripe_validate"

#: One pass closes at most this many; the rest wait for the next tick
#: (``scripts/gripe-validate``'s ``MAX_IDS``).
MAX_IDS = 60

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # List what would flip, write nothing.
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Close auto_verify gripes whose resolved sha is in the recorded "
    "prod head (release.prod_head) under a green release gate; bounce a "
    "gate-red one to triaged; keep one ask-user todo for user_verify."
)


@dataclass
class ValidateResult:
    """What one pass did (or, ``dry_run``, would have done)."""

    dry_run: bool
    head: str | None = None
    verify: int = 0
    live: list[tuple[int, str]] = field(default_factory=list)
    pending: list[tuple[int, str]] = field(default_factory=list)
    no_sha: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    user: int = 0
    spec: int = 0
    closed: str = ""
    ask: str = ""

    def counts(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "verify": self.verify,
            "live": len(self.live),
            "pending": len(self.pending),
            "no_sha": len(self.no_sha),
            "failed": len(self.failed),
            "user": self.user,
            "spec": self.spec,
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        head = self.head[:9] if self.head else "none recorded"
        return (
            f"gripe_validate (prod head {head}): verify={self.verify} "
            f"live={len(self.live)} pending={len(self.pending)} "
            f"no_sha={len(self.no_sha)} failed={len(self.failed)} "
            f"user={self.user} spec={self.spec} dry_run={self.dry_run}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.live:
            verb = "would flip" if self.dry_run else "flipped"
            lines.append(
                f"{verb} verify → done: "
                + " ".join(f"gr{g}@{s[:9]}" for g, s in self.live)
            )
        if self.closed:
            lines.append(self.closed)
        if self.failed:
            lines.append(
                "gate red → triaged: " + " ".join(f"gr{g}" for g in self.failed)
            )
        if self.pending:
            lines.append(
                "not in prod yet: "
                + " ".join(f"gr{g}@{s[:9]}" for g, s in self.pending)
            )
        if self.no_sha:
            lines.append(
                "no sha in a resolved: comment: "
                + " ".join(f"gr{g}" for g in self.no_sha)
            )
        if self.ask:
            lines.append(self.ask)
        return "\n".join(lines)


def _prefix_match(a: str, b: str) -> bool:
    """Whether one sha is a prefix of the other (either may be abbreviated)."""
    n = min(len(a), len(b))
    return n >= MIN_SHA_LEN and a[:n] == b[:n]


def sha_live(rec: dict[str, Any], sha: str) -> bool:
    """``sha`` is the recorded prod head or one of the shas it landed."""
    sha = sha.lower()
    if _prefix_match(sha, str(rec["head"]).lower()):
        return True
    return any(
        _prefix_match(sha, str(s).lower())
        for s in rec.get("landed") or []
        if isinstance(s, str)
    )


def head_gate(
    rec: dict[str, Any], verdicts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The newest ``ci_verdicts`` row for the deployed head, or ``None``.

    A red row means the deployed head's release gate failed — the pass's
    "verify failure" signal; no row falls back to the prod-head
    derivation (a deploy only happens on a green gate).
    """
    head = str(rec.get("head") or "").lower()
    pick: dict[str, Any] | None = None
    best: tuple[str, int] | None = None
    for i, row in enumerate(verdicts):
        if not _prefix_match(head, str(row.get("sha") or "").lower()):
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, pick = key, row
    return pick


#: Close text; ``{sha}`` is each gripe's own verify sha (``verify_done``).
_CLOSE_TEXT = (
    "resolved: {sha} ({where}) — auto-verified after deploy: the sha is "
    "in the recorded prod head (release.prod_head)"
)
#: The "can't verify" note appended once to a sha-less auto gripe
#: (``note_gripes`` dedupes on the exact text).
_NO_SHA_NOTE = (
    "validate: no sha in a resolved:/fix-sha: comment — cannot "
    "auto-verify; a human check can move it to STATUS:user_verify"
)
#: The verify-failure note; ``{run}``/``{head}`` fill at send.
_FAILED_TEXT = (
    "validate: the resolved sha is in prod but the release gate on "
    "{head} is RED{run} — verification failed, back to triaged"
)


def _verify_failed(store: Any, ids: list[int], row: dict[str, Any], head: str) -> str:
    """Send verify-failed gripes back to triaged with the gate-red note."""
    from precis.cli.gripe import verify_back

    run = f" (run {row['run_id']})" if row.get("run_id") else ""
    return verify_back(store, _FAILED_TEXT.format(head=head[:9], run=run), ids)


def run_pass(store: Any, *, dry_run: bool = False) -> ValidateResult:
    """One validation pass (module docstring)."""
    from precis.cli.gripe import (
        AUTO_VERIFY_STATUSES,
        note_gripes,
        spec_list,
        user_verify_sync,
        verify_done,
        verify_list,
    )

    res = ValidateResult(dry_run=dry_run)
    rec = read_prod_head(store)
    rows = verify_list(store)
    auto = [(g, s) for g, st, s in rows if st in AUTO_VERIFY_STATUSES]
    res.verify = len(auto)
    res.user = sum(1 for _g, st, _s in rows if st not in AUTO_VERIFY_STATUSES)
    res.spec = len(spec_list(store))
    if rec is None:
        # Nothing to compare against: every gripe stays at its state.
        res.no_sha = [g for g, sha in auto if not sha]
        res.pending = [(g, sha) for g, sha in auto if sha]
        return res
    res.head = str(rec["head"])
    gate = head_gate(rec, read_ci_verdicts(store))
    red = gate is not None and gate.get("verdict") == "red"
    for gid, sha in auto:
        if not sha:
            res.no_sha.append(gid)
        elif not sha_live(rec, sha):
            res.pending.append((gid, sha))
        elif red:
            res.failed.append(gid)
        else:
            res.live.append((gid, sha))
    if dry_run:
        return res
    if res.failed:
        out = _verify_failed(store, res.failed, gate or {}, res.head)
        if out:
            res.closed = out
    if res.no_sha:
        note_gripes(store, _NO_SHA_NOTE, res.no_sha)
    batch = res.live[:MAX_IDS]
    if batch:
        where = str(rec.get("release") or f"on prod {res.head[:9]}")
        line = verify_done(
            store,
            _CLOSE_TEXT.replace("{where}", where),
            [g for g, _ in batch],
        )
        res.closed = (res.closed + "\n" if res.closed else "") + line
    if len(res.live) > MAX_IDS:
        res.closed += f"\n+{len(res.live) - MAX_IDS} live next run"
    if res.user or _has_live_ask(store):
        res.ask = user_verify_sync(store, head=res.head)
    return res


def _has_live_ask(store: Any) -> bool:
    """Whether a live user-verify digest todo exists (retire check)."""
    from precis.cli.gripe import _uv_digest_rows
    from precis.todo import TERMINAL_TODO_STATUSES

    with store.pool.connection() as conn:
        rows = _uv_digest_rows(conn)
    return any(status not in TERMINAL_TODO_STATUSES for _id, _m, status in rows)


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    try:
        res = run_pass(ctx.store, dry_run=bool(params.get("dry_run")))
    except Exception as exc:  # a failed pass must not wedge the lane
        log.warning("gripe_validate: pass raised (%s)", type(exc).__name__)
        ctx.record_failure(f"gripe_validate: pass raised: {type(exc).__name__}")
        return
    ctx.set_meta(gripe_validate=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A second run while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live gripe state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "MAX_IDS",
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "ValidateResult",
    "head_gate",
    "load",
    "run_pass",
    "sha_live",
]
