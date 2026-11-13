"""The release merge-back (release-train T3): ``release/r<N>`` into main.

Spec: ``docs/backlog/release-train.md`` § "Merge back" and slice T3. A
release's fixes land on ``release/r<N>`` only (T1), so once it deploys
prod carries commits main lacks and ``release_poll``'s ``compare
prod...main`` reads ``diverged`` — no next pass would ever mint. This
step merges the release branch into main with a real merge commit
(``Merge release/r<N> fix forward``): the release head becomes an
ancestor of main, so the compare reads plain ancestry again and every
``sha-in-prod`` gate on a release-branch fix opens.

Not a job type of its own: it is ``release_confirm``'s ``merge_back``
mode (the ``merge_back`` node of the release-pass template, blocked-by
the deploy), like the restamp is a mode of ``ci_verdict_poll`` — no new
registry, claim-band or service wiring. It runs synchronously inside the
job's submit (a fetch, a merge, a push: seconds) on the slice host's
checkout, in a throwaway detached worktree off ``origin/main``, with the
factory git credential (:func:`precis.workers.factory_git.git_auth_env`,
the lands' path).

* No commit past the cut, or a release head main already contains: no
  merge commit — recorded as a no-op. A retry after a push that landed
  but was never recorded reads the same no-op.
* Hunks main already has (the same fix landed twice) resolve by content:
  git's three-way merge takes identical changes clean.
* A real conflict pushes nothing: the merge is aborted and two nodes are
  minted under the plan root (:func:`precis.plans.templates.mint_merge_back_resolver`):
  ONE resolver todo — a normal ``run_slice`` slice off main that carries
  the release fixes' intent onto main, resolving the conflicting hunks —
  and a second ``merge_back`` node blocked-by it with
  ``strategy="ours"``. ``scripts/ship`` squashes every land into one
  non-merge commit, so the resolver alone can never make the release
  head an ancestor of main; the ours-merge
  (``git merge --no-ff -s ours``, message ``Merge release/r<N> fix
  forward (resolved in <sha>)``) records that ancestry without touching
  main's tree. The pass root stays open until both close, so
  ``release_poll`` mints no next pass before the ancestry exists.
* A push lost to a moving main re-fetches and re-merges, a few times.

The outcome lands on ``meta.release.merge_back`` (``{state, sha?, at,
why?, resolver?, strategy?}``) and the job summary."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.workers import factory_git as _factory_git

log = logging.getLogger(__name__)

NAME = "release_merge_back"
#: ``meta.release`` key holding the outcome.
MERGE_BACK_KEY = "merge_back"
#: The resolver todo's meta key (the release's ``rN``) — what
#: :func:`_resolver` dedupes on before minting.
RESOLVER_KEY = "merge_back_resolver"
#: The ancestry-only merge after a resolver landed the content.
OURS = "ours"
#: Pushes lost to a moving main before the job gives up (the tick retries).
_PUSH_TRIES = 3
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
#: Serializes the resolver dedupe read and its mint ("mrg_back").
_RESOLVER_LOCK_KEY = 0x6D72675F6261636B


def merge_message(rn: str, resolved_in: str | None = None) -> str:
    """The merge commit's subject (release-train §7); the ours-merge
    names the resolver land it records."""
    base = f"Merge release/{rn} fix forward"
    return f"{base} (resolved in {resolved_in})" if resolved_in else base


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Seam: ``git <args>`` in ``cwd`` with the factory credential;
    never raises on a non-zero exit."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=_factory_git.git_auth_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "").strip()


def _err(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1][:200] if lines else f"exit {proc.returncode}"


def _record(store: Any, todo_id: int, outcome: dict[str, Any]) -> None:
    rel = _release.release_of(store, todo_id)
    if rel is None:
        return
    root = _release.plan_root(store, todo_id)
    when = datetime.now(UTC).strftime(_TS_FMT)
    store.stamp_ref_meta(
        root, {"release": {**rel, MERGE_BACK_KEY: {**outcome, "at": when}}}
    )


def _resolver_body(rn: str, cut: str, files: list[str]) -> str:
    shown = ", ".join(f"`{f}`" for f in files[:20]) or "(git named none)"
    since = f"{cut[:12]}..origin/release/{rn}" if cut else f"origin/release/{rn}"
    return (
        f"Release {rn} deployed, but merging `origin/release/{rn}` back into "
        f"main conflicts in {shown}. Make main carry the release fixes' "
        f"intent: on your slice branch (cut from origin/main), `git fetch "
        f"origin release/{rn}`, read the fixes with `git log -p {since}`, "
        "and apply each one's change to main, resolving the conflicting "
        "hunks so BOTH sides' intent survives (the release's fixes and "
        "main's later work). Commit plain commits, not a merge: the lander "
        "squashes the slice onto main like any other. Run the tests the "
        "conflicted files touch. A fix main already carries needs nothing. "
        f"Once this lands, the plan's next merge-back node records "
        f"release/{rn} as merged into main (an ours-merge, no content); the "
        "next release is not cut before then."
    )


def _resolver(
    store: Any, todo_id: int, rn: str, cut: str, files: list[str]
) -> tuple[int, bool]:
    """``(resolver id, minted)`` — the ONE resolver todo under the plan
    root for ``rn``. The dedupe reads :data:`RESOLVER_KEY`, the key the
    mint writes, under an advisory lock in the mint's own transaction
    (:func:`precis.plans.templates.mint_merge_back_resolver` joins it as
    a savepoint), so a crash between the mint and :func:`_record`, or
    two racing retries, can never mint a second."""
    from precis.plans import templates as _templates

    root = _release.plan_root(store, todo_id)
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RESOLVER_LOCK_KEY,))
        dup = conn.execute(
            """
            SELECT ref_id FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL AND parent_id = %s
               AND meta->>%s = %s
             ORDER BY ref_id
             LIMIT 1
            """,
            (root, RESOLVER_KEY, rn),
        ).fetchone()
        if dup is not None:
            return int(dup[0]), False
        rid, _ours = _templates.mint_merge_back_resolver(
            store, root, rn, _resolver_body(rn, cut, files)
        )
    return rid, True


def _resolved_in(store: Any, resolver: Any) -> tuple[str | None, str | None]:
    """``(label, refusal)`` for the ours-merge: the resolver's landed
    sha (``td<id>`` when its land recorded none — main already carried
    the content), or a refusal while it is not done."""
    from precis.workers.job_types import release_poll as _rp

    if not isinstance(resolver, int):
        return None, "strategy=ours names no resolver todo"
    status = _rp._status_of(store, resolver)
    if status != "done":
        return None, f"resolver td{resolver} is {status}, not done"
    sha = _rp._fix_landed_sha(store, resolver)
    return (sha[:12] if sha else f"td{resolver}"), None


def merge_back(
    store: Any,
    todo_id: int,
    root: Path,
    *,
    strategy: str | None = None,
    resolver: int | None = None,
) -> dict[str, Any]:
    """Merge ``release/r<N>`` into main from the checkout ``root``; the
    outcome (also recorded on ``meta.release.merge_back``). ``state`` is
    ``merged`` (``sha`` the merge commit), ``noop`` (``why``),
    ``conflict`` (``resolver`` the minted todo, ``files``) or ``failed``
    (``why`` — the caller fails the job, the tick retries).
    ``strategy="ours"`` is the post-resolver step: ``-s ours`` records
    the ancestry only, keeping main's tree, and needs ``resolver`` done."""
    rel = _release.release_of(store, todo_id)
    if rel is None:
        return {"state": "failed", "why": "no release recorded on the plan"}
    rn = str(rel["rN"])
    resolved: str | None = None
    if strategy == OURS:
        resolved, refusal = _resolved_in(store, resolver)
        if refusal is not None:
            return {"state": "failed", "why": refusal}
    elif strategy is not None:
        return {"state": "failed", "why": f"unknown merge strategy {strategy!r}"}
    branch = _release.release_branch(rn)
    tracking = f"refs/remotes/origin/{branch}"
    fetch = _git(
        ["fetch", "-q", "origin", "main", f"+refs/heads/{branch}:{tracking}"], root
    )
    if fetch.returncode != 0:
        return {"state": "failed", "why": f"git fetch: {_err(fetch)}"}
    head = _out(_git(["rev-parse", "--verify", "-q", f"{tracking}^{{commit}}"], root))
    if not head:
        return {"state": "failed", "why": f"origin/{branch} is unreadable"}
    cut = str(rel.get("cut_sha") or rel.get("sha") or "")
    cut_full = _out(_git(["rev-parse", "--verify", "-q", f"{cut}^{{commit}}"], root))
    out: dict[str, Any]
    if cut_full and cut_full == head:
        out = {"state": "noop", "why": f"{branch} has no commit past the cut"}
        _record(store, todo_id, out)
        return out
    if _git(["merge-base", "--is-ancestor", head, "origin/main"], root).returncode == 0:
        out = {"state": "noop", "why": f"origin/main already contains {head[:12]}"}
        _record(store, todo_id, out)
        return out
    message = merge_message(rn, resolved)
    how = ["-s", OURS] if strategy == OURS else []
    tree = root / ".claude" / "worktrees" / f"merge-back-{rn}"
    try:
        for attempt in range(1, _PUSH_TRIES + 1):
            _git(["worktree", "remove", "--force", str(tree)], root)
            shutil.rmtree(tree, ignore_errors=True)
            if attempt > 1:
                _git(["fetch", "-q", "origin", "main"], root)
            add = _git(["worktree", "add", "--detach", str(tree), "origin/main"], root)
            if add.returncode != 0:
                return {"state": "failed", "why": f"git worktree add: {_err(add)}"}
            merge = _git(
                ["merge", "--no-ff", *how, "-m", message, f"origin/{branch}"], tree
            )
            if merge.returncode != 0:
                files = [
                    f
                    for f in _out(
                        _git(["diff", "--name-only", "--diff-filter=U"], tree)
                    ).splitlines()
                    if f.strip()
                ]
                _git(["merge", "--abort"], tree)
                if not files or strategy == OURS:
                    # No unmerged path: git refused for another reason (no
                    # identity, a bad ref) — not a conflict to hand off.
                    return {"state": "failed", "why": f"git merge: {_err(merge)}"}
                rid, _minted = _resolver(store, todo_id, rn, cut_full, files)
                out = {"state": "conflict", "resolver": rid, "files": files[:50]}
                _record(store, todo_id, out)
                return out
            sha = _out(_git(["rev-parse", "HEAD"], tree))
            push = _git(["push", "-q", "origin", "HEAD:refs/heads/main"], tree)
            if push.returncode == 0:
                out = {"state": "merged", "sha": sha, "head": head}
                if strategy == OURS:
                    out.update(strategy=OURS, resolver=resolver)
                _record(store, todo_id, out)
                return out
            log.info("%s: push to main lost (try %d): %s", NAME, attempt, _err(push))
        return {
            "state": "failed",
            "why": f"push to main rejected {_PUSH_TRIES}x (main kept moving)",
        }
    finally:
        _git(["worktree", "remove", "--force", str(tree)], root)
        shutil.rmtree(tree, ignore_errors=True)


def run(ctx: Any, params: dict[str, Any]) -> None:
    """``release_confirm``'s ``merge_back`` mode: drive the job terminal
    from :func:`merge_back`'s outcome."""
    from precis.workers.job_types import run_slice as _rs

    todo_id = int(params["todo_id"])
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        ctx.record_failure(f"{NAME}: {exc}")
        return
    resolver = params.get("resolver")
    out = merge_back(
        ctx.store,
        todo_id,
        root,
        strategy=params.get("strategy"),
        resolver=resolver if isinstance(resolver, int) else None,
    )
    ctx.set_meta(merge_back=out)
    state = out["state"]
    if state == "failed":
        ctx.record_failure(f"{NAME}: {out['why']}; the tick retries")
        return
    line = {
        "merged": f"merged {out.get('head', '')[:12]} into main as {out.get('sha', '')[:12]}"
        + (" (ours: ancestry only)" if out.get("strategy") == OURS else ""),
        "noop": f"no merge commit — {out.get('why')}",
        "conflict": (
            f"conflict in {', '.join(out.get('files') or []) or '?'}; nothing pushed; "
            f"resolver td{out.get('resolver')} minted — an ours-merge records "
            "the ancestry once it lands; the next cut waits for both"
        ),
    }[state]
    ctx.append_chunk("job_summary", f"{NAME}: {line}")
    ctx.set_status("succeeded")


__all__ = [
    "MERGE_BACK_KEY",
    "NAME",
    "OURS",
    "RESOLVER_KEY",
    "merge_back",
    "merge_message",
    "run",
]
