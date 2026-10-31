"""``gripe_validate`` job_type — flip ``STATUS:verify`` gripes to done once live.

The last rung of the gripe status ladder (gr541800, td541978): a gripe at
``verify`` waits on the sha in its newest ``resolved: <sha>`` comment; once
that sha is in prod it closes ``STATUS:done``. ``scripts/gripe-validate``
does this from a checkout after a rollout (``git merge-base`` against
``origin/prod``); this job is the DB-only equivalent for a recurring run on
any worker host — it reads the ``release.prod_head`` record
``scripts/release`` writes after a deploy (``{head, deployed_at, landed}``)
and treats a sha equal to the head, or in ``landed``, as live. A gripe whose
comment names no sha, or a sha not yet recorded, stays at ``verify``.

The listing and the close are :func:`precis.cli.gripe.verify_list` /
:func:`~precis.cli.gripe.verify_done` — the same functions the script's two
prod calls run — so the ``resolved:`` guard and the close cascade
(gr520237) apply unchanged. ``verify_done`` reaches the gripe handler at
call time (the cascade lives there); this module imports no handler.

``params.dry_run`` lists without writing. Idempotent and non-interactive,
so the minter's recurring todo is the whole schedule::

    put(kind='todo', text='gripe_validate tick (verify gripes to done)',
        meta={'schedule': {'every': '15m'}, 'executor': 'job_inproc',
              'job_type': 'gripe_validate'})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from precis.plans.gates import MIN_SHA_LEN, read_prod_head
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
    "Flip STATUS:verify gripes to done once the sha in their resolved: "
    "comment is in the recorded prod head (release.prod_head)."
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
    closed: str = ""

    def counts(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "verify": self.verify,
            "live": len(self.live),
            "pending": len(self.pending),
            "no_sha": len(self.no_sha),
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        head = self.head[:9] if self.head else "none recorded"
        return (
            f"gripe_validate (prod head {head}): verify={self.verify} "
            f"live={len(self.live)} pending={len(self.pending)} "
            f"no_sha={len(self.no_sha)} dry_run={self.dry_run}"
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


#: Close text; ``{sha}`` is each gripe's own verify sha (``verify_done``).
_CLOSE_TEXT = (
    "resolved: {sha} (on prod {head}) — gripe_validate: the sha is in the "
    "recorded prod head (release.prod_head)"
)


def run_pass(store: Any, *, dry_run: bool = False) -> ValidateResult:
    """One validation pass (module docstring)."""
    from precis.cli.gripe import verify_done, verify_list

    res = ValidateResult(dry_run=dry_run)
    rec = read_prod_head(store)
    rows = verify_list(store)
    res.verify = len(rows)
    if rec is None:
        # Nothing to compare against: every gripe stays at verify.
        res.no_sha = [g for g, sha in rows if not sha]
        res.pending = [(g, sha) for g, sha in rows if sha]
        return res
    res.head = str(rec["head"])
    for gid, sha in rows:
        if not sha:
            res.no_sha.append(gid)
        elif sha_live(rec, sha):
            res.live.append((gid, sha))
        else:
            res.pending.append((gid, sha))
    batch = res.live[:MAX_IDS]
    if batch and not dry_run:
        res.closed = verify_done(
            store,
            _CLOSE_TEXT.replace("{head}", res.head[:9]),
            [g for g, _ in batch],
        )
    if len(res.live) > MAX_IDS:
        res.closed = (res.closed + "\n" if res.closed else "") + (
            f"+{len(res.live) - MAX_IDS} live next run"
        )
    return res


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
    "load",
    "run_pass",
    "sha_live",
]
