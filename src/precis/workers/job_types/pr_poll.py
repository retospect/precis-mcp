"""``pr_poll`` — mint one ``pr-intake`` plan per open pull request (td543749).

Spec: ``docs/backlog/dark-factory.md`` § "9. Inbound pull requests". DB plus
GitHub reads under ``job_inproc`` (the ``gripe_validate`` shape). One pass:

1. The repo: ``params.repo``, else the ``release.repo`` setting, else the
   slice checkout's ``origin`` remote (:func:`precis.plans.pr_class.resolve_repo`).
2. ``GET /repos/<repo>/pulls?state=open`` through
   :func:`precis.utils.safe_fetch.safe_get` (public API; ``GH_TOKEN`` sent
   when set). GitHub unreadable (network, rate limit) ends the pass with a
   note and nothing minted; the next tick tries again.
3. For each open, non-draft PR that wants an intake
   (:func:`precis.plans.pr_class.needs_intake`: no plan root tagged
   ``pr:<n>``, or no OPEN root and the newest closed root's ``pr-head:``
   differs from the PR's current head — a closed root on the same head
   means done, a moved head after a closed plan is looked at again): read
   its files and commits and mint the plan (:func:`precis.plans.templates.mint_pr_intake` — the
   ``put(kind='todo', mode='plan', args={'template': 'pr-intake', 'pr': n})``
   path, called directly: workers never import handlers). At most
   ``params.max_new`` (default :data:`MAX_NEW`) per pass, so a burst of PRs
   spends a bounded share of the unauthenticated 60 requests/hour.

``params.dry_run`` reads and classifies, mints nothing. Minting is safe
while the factory is off: a plan only runs once ``plan_dispatch`` is armed.
Arming (an operator write)::

    put(kind='todo', text='pr_poll tick',
        meta={'schedule': {'every': '15m'}, 'executor': 'job_inproc',
              'job_type': 'pr_poll'})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from precis.plans import pr_class
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "pr_poll"

#: New plans one pass mints at most (each costs two GitHub reads).
MAX_NEW = 10

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Classify and report, mint nothing.
        "dry_run": {"type": "boolean"},
        # owner/repo; else release.repo, else the checkout's origin.
        "repo": {"type": "string"},
        # Cap on new plans this pass.
        "max_new": {"type": "integer"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Inbound pull requests: list the repo's open PRs on GitHub and mint one "
    "pr-intake plan (tagged pr:<n>) for each PR that has none, or whose head "
    "moved after its plan closed; the class "
    "(dependabot | harness | factory | external) is decided in code."
)


@dataclass
class PollResult:
    """What one pass did (or, ``dry_run``, would have done)."""

    dry_run: bool
    repo: str | None = None
    open: int = 0
    known: int = 0
    drafts: int = 0
    minted: list[tuple[int, int, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    deferred: int = 0
    note: str = ""

    def counts(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "open": self.open,
            "known": self.known,
            "drafts": self.drafts,
            "minted": len(self.minted),
            "errors": len(self.errors),
            "deferred": self.deferred,
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        return (
            f"pr_poll {self.repo or '(no repo)'}: open={self.open} "
            f"known={self.known} drafts={self.drafts} minted={len(self.minted)} "
            f"deferred={self.deferred} errors={len(self.errors)} "
            f"dry_run={self.dry_run}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.note:
            lines.append(self.note)
        verb = "would mint" if self.dry_run else "minted"
        lines += [
            f"{verb} PR #{n} ({cls})" + (f" -> td{root}" if root else "")
            for n, root, cls in self.minted
        ]
        lines += [f"error: {e}" for e in self.errors]
        return "\n".join(lines)


def run_pass(
    store: Any,
    *,
    repo: str | None = None,
    dry_run: bool = False,
    max_new: int = MAX_NEW,
) -> PollResult:
    """One poll pass (module docstring)."""
    from precis.plans import templates

    res = PollResult(dry_run=dry_run)
    res.repo = pr_class.resolve_repo(store, repo)
    if not res.repo:
        res.note = (
            "no GitHub owner/repo (params.repo, release.repo and the checkout's "
            "origin all empty); next: set release.repo"
        )
        return res
    try:
        pulls = pr_class.fetch_open_pulls(res.repo)
    except pr_class.GitHubUnreadable as exc:
        res.note = f"GitHub unreadable ({exc}); nothing minted, next tick retries"
        return res
    res.open = len(pulls)
    builders = pr_class.builders(store)
    for pull in sorted(pulls, key=lambda p: int(p.get("number") or 0)):
        number = int(pull.get("number") or 0)
        if number <= 0:
            continue
        if pull.get("draft"):
            res.drafts += 1
            continue
        head = str((pull.get("head") or {}).get("sha") or "")
        if not pr_class.needs_intake(store, number, head):
            res.known += 1
            continue
        if len(res.minted) >= max_new:
            res.deferred += 1
            continue
        try:
            facts = pr_class.fetch_facts(res.repo, number, pull=pull)
        except pr_class.GitHubUnreadable as exc:
            res.errors.append(f"PR #{number}: {exc}")
            continue
        cls, _why = pr_class.classify(facts, builders)
        if dry_run:
            res.minted.append((number, 0, cls))
            continue
        try:
            mint = templates.mint_pr_intake(store, pr=number, facts=facts)
        except Exception as exc:  # one bad PR must not stop the pass
            log.warning("pr_poll: mint for PR #%d failed", number, exc_info=True)
            res.errors.append(f"PR #{number}: mint failed: {type(exc).__name__}: {exc}")
            continue
        res.minted.append((number, mint.root_id, cls))
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    try:
        cap = int(params.get("max_new") or MAX_NEW)
    except (TypeError, ValueError):
        cap = MAX_NEW
    try:
        res = run_pass(
            ctx.store,
            repo=params.get("repo"),
            dry_run=bool(params.get("dry_run")),
            max_new=max(cap, 0),
        )
    except Exception as exc:  # a failed pass must not wedge the lane
        log.warning("pr_poll: pass raised (%s)", type(exc).__name__, exc_info=True)
        ctx.record_failure(f"pr_poll: pass raised: {type(exc).__name__}: {exc}")
        return
    ctx.set_meta(pr_poll=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A second poll while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live GitHub state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "MAX_NEW",
    "NAME",
    "PARAMS_SCHEMA",
    "SPEC",
    "PollResult",
    "load",
    "run_pass",
]
