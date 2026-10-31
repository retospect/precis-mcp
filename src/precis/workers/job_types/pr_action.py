"""``pr_action`` — act on the answered PR decision (td543749).

Spec: ``docs/backlog/dark-factory.md`` § "9. Inbound pull requests"; the
``act`` node of a ``pr-intake`` plan, blocked-by the ``decision`` ask.
Comments on a PR and closing it are outward-facing, so this op runs only
after the ask is answered: :func:`validate_submit` refuses (the tick notes
"not dispatched") while no ``answer:`` comment is on the ask that blocks
the node, and :func:`act` re-checks.

The answer (the newest ``answer:`` comment on the blocking ask, matched
case-insensitively against the options the template offered):

==========================  =================================================
``land`` / ``land as is``   nothing outward; the plan's land node runs next
``land after fixes: <l>``   a ``run_slice`` fix task (merges the PR head,
                            fixes ``<l>``, its land closes the PR) plus a
                            reviewer over it are minted; the template's land
                            and review are skipped
``ignore this major``       comment ``@dependabot ignore this major version``
``request changes …``       ``gh pr review --request-changes`` with the text
                            after the first colon (else a short default)
``close``                   ``gh pr close --comment`` (text after the colon)
``hold``                    nothing outward; the PR stays open
``land via /go``            nothing outward; Reto lands the harness PR by hand
==========================  =================================================

Every answer but a land skips the rest of the plan: each open node other
than the root and this one goes ``STATUS:won't-do`` with a ``skipped:``
comment, so the root closes. An answer that matches no option fails the
job (the retry-cap ask follows). GitHub writes go through the ``gh`` CLI of
the claiming host (:func:`_gh`; its identity is the host's ``gh auth``, the
attribution gap of gr543694). ``ssh_node``; the whole act is a few seconds,
so :func:`submit` does it and drives the job terminal (no handle).
Refused while ``PRECIS_FACTORY=off``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from precis.store.types import Tag
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "pr_action"

_GH_TIMEOUT_S = 120
_CLOSED = frozenset({"done", "won't-do", "cancelled"})
_LAND_RE = re.compile(r"^land(?:\s+as\s+is)?\b", re.I)
_FIX_RE = re.compile(r"^land\s+after\s+fixes\b\s*:?\s*(.*)$", re.I | re.S)
_IGNORE_RE = re.compile(r"^ignore\s+this\s+major\b", re.I)
_CHANGES_RE = re.compile(r"^request\s+changes\b[^:]*:?\s*(.*)$", re.I | re.S)
_CLOSE_RE = re.compile(r"^close\b[^:]*:?\s*(.*)$", re.I | re.S)
_HOLD_RE = re.compile(r"^hold\b", re.I)
_MANUAL_RE = re.compile(r"^land\s+via\s+/?go\b", re.I)

IGNORE_MAJOR_COMMENT = "@dependabot ignore this major version"
DEFAULT_CHANGES = (
    "Thanks for the pull request. Changes are requested before it can land; "
    "the maintainer will follow up with details."
)
DEFAULT_CLOSE = "Closing this pull request; thanks for the contribution."

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's act node (blocked-by the decision ask).
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (a host with gh auth).
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Act on an answered pull-request decision of a pr-intake plan: a land "
    "answer lets the land run (or mints a fix slice for 'land after fixes'); "
    "'request changes', 'close' and 'ignore this major' post to the PR with "
    "gh; every non-land answer skips the rest of the plan. Refused until the "
    "ask is answered, and while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return 600.0


# ── Seam ───────────────────────────────────────────────────────────


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>``; never raises on a non-zero exit."""
    try:
        return subprocess.run(
            [os.environ.get("PRECIS_GH_BIN") or "gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


# ── The answer ─────────────────────────────────────────────────────


def parse_choice(answer: str) -> tuple[str, str] | None:
    """``(choice, text)`` for an answer: choice one of ``land``, ``fix``,
    ``ignore-major``, ``request-changes``, ``close``, ``hold``, ``manual``;
    ``text`` is the fix list or the comment. ``None`` when it matches no
    option."""
    a = answer.strip().strip("'\"").strip()
    if m := _FIX_RE.match(a):
        return "fix", m.group(1).strip()
    if _MANUAL_RE.match(a):
        return "manual", ""
    if _LAND_RE.match(a):
        return "land", ""
    if _IGNORE_RE.match(a):
        return "ignore-major", ""
    if m := _CHANGES_RE.match(a):
        return "request-changes", m.group(1).strip()
    if m := _CLOSE_RE.match(a):
        return "close", m.group(1).strip()
    if _HOLD_RE.match(a):
        return "hold", ""
    return None


def _asks(store: Any, todo_id: int) -> list[int]:
    """The ``blocked-by`` targets of ``todo_id`` carrying ``meta.ask``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.ref_id FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = %s
               AND b.retired_at IS NULL AND b.meta ? 'ask'
             ORDER BY b.ref_id
            """,
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def answer_of(store: Any, todo_id: int) -> tuple[int | None, str | None]:
    """``(ask id, newest answer text)`` of the ask blocking ``todo_id``;
    ``(None, None)`` with no ask, ``(id, None)`` while it is unanswered."""
    from precis.plans import release as _release

    asks = _asks(store, todo_id)
    if not asks:
        return None, None
    ask_id = asks[0]
    return ask_id, _release._newest_answer(store, ask_id)


def _refusal(store: Any, params: dict[str, Any]) -> str | None:
    from precis.plans import pr_class
    from precis.workers.job_types import release_cut as _rc

    reason = _rc.factory_refusal(NAME) or _rc.todo_refusal(
        store, NAME, params.get("todo_id"), required=True
    )
    if reason is not None or store is None:
        return reason
    todo_id = int(params["todo_id"])
    if pr_class.pr_of(store, todo_id) is None:
        return f"{NAME}: td{todo_id} is in no pr-intake plan (no meta.pr on its root)"
    ask_id, answer = answer_of(store, todo_id)
    if ask_id is None:
        return f"{NAME}: no ask blocks td{todo_id}; a PR action needs an answered one"
    if answer is None:
        return f"{NAME}: decision td{ask_id} is not answered yet; nothing is posted"
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return _refusal(store, params)


# ── The act ────────────────────────────────────────────────────────


def _skip_rest(store: Any, todo_id: int, why: str) -> list[int]:
    """``STATUS:won't-do`` + a ``skipped:`` comment on every open node of
    the plan other than the root and ``todo_id``."""
    from precis.plans import release as _release

    root = _release.plan_root(store, todo_id)
    skipped: list[int] = []
    for node_id, _meta, status in _release._plan_nodes(store, root):
        if node_id in (root, todo_id) or status in _CLOSED:
            continue
        _rs._append_comment(store, node_id, f"skipped: {why}")
        store.add_tag(
            node_id,
            Tag.closed("STATUS", "won't-do"),
            set_by="system",
            replace_prefix=True,
        )
        skipped.append(node_id)
    return skipped


def _skip_land_path(store: Any, todo_id: int, why: str) -> list[int]:
    """The template's land + review nodes go ``won't-do`` (the fix path
    lands through its own slice)."""
    from precis.plans import release as _release

    root = _release.plan_root(store, todo_id)
    skipped: list[int] = []
    for node_id, meta, status in _release._plan_nodes(store, root):
        if node_id in (root, todo_id) or status in _CLOSED:
            continue
        op = meta.get("op")
        gen = meta.get("generate")
        land = isinstance(op, dict) and op.get("type") == "land_slice"
        review = isinstance(gen, dict) and gen.get("role") == "reviewer"
        if not (land or review):
            continue
        _rs._append_comment(store, node_id, f"skipped: {why}")
        store.add_tag(
            node_id,
            Tag.closed("STATUS", "won't-do"),
            set_by="system",
            replace_prefix=True,
        )
        skipped.append(node_id)
    return skipped


def act(store: Any, todo_id: int) -> tuple[bool, str]:
    """Carry out the answered decision for ``todo_id``'s PR. ``(ok,
    summary)``; ``ok`` False leaves the plan as it was."""
    from precis.plans import pr_class
    from precis.plans import templates as _templates

    reason = _refusal(store, {"todo_id": todo_id})
    if reason is not None:
        return False, reason
    pr = pr_class.pr_of(store, todo_id)
    assert pr is not None
    ask_id, answer = answer_of(store, todo_id)
    assert answer is not None
    number = int(pr["number"])
    repo = pr_class.resolve_repo(store, pr.get("repo"))
    parsed = parse_choice(answer)
    if parsed is None:
        return False, (
            f"{NAME}: answer {answer[:80]!r} on td{ask_id} matches no option; "
            "next: answer one of the ask's options"
        )
    choice, text = parsed
    why = f"answer {answer[:80]!r} on td{ask_id}"
    if choice == "land":
        return True, f"{NAME}: PR #{number}: {why} — the land runs next"
    if choice == "fix":
        if not text or text.startswith("<"):
            return False, (
                f"{NAME}: {why} names no fixes; next: answer "
                "'land after fixes: <the list>'"
            )
        from precis.plans import release as _release

        root = _release.plan_root(store, todo_id)
        skipped = _skip_land_path(store, todo_id, f"{why}; the fix slice lands it")
        ids = _templates.mint_pr_fix(store, root, pr, text)
        return True, (
            f"{NAME}: PR #{number}: {why} — fix slice td{ids['fix']} and review "
            f"td{ids['review']} minted; skipped "
            + (", ".join(f"td{i}" for i in skipped) or "nothing")
        )
    outward: list[str] | None = None
    if choice in ("ignore-major", "request-changes", "close") and not repo:
        return (
            False,
            f"{NAME}: no GitHub owner/repo for PR #{number}; next: set release.repo",
        )
    if choice == "ignore-major":
        outward = [
            "pr",
            "comment",
            str(number),
            "--repo",
            str(repo),
            "--body",
            IGNORE_MAJOR_COMMENT,
        ]
    elif choice == "request-changes":
        outward = [
            "pr",
            "review",
            str(number),
            "--repo",
            str(repo),
            "--request-changes",
            "--body",
            text or DEFAULT_CHANGES,
        ]
    elif choice == "close":
        outward = [
            "pr",
            "close",
            str(number),
            "--repo",
            str(repo),
            "--comment",
            text or DEFAULT_CLOSE,
        ]
    did = "nothing posted"
    if outward is not None:
        proc = _gh(outward)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:300]
            return False, f"{NAME}: gh {' '.join(outward[:3])} failed: {err}"
        did = f"gh {' '.join(outward[:2])} done"
    skipped = _skip_rest(store, todo_id, why)
    return True, (
        f"{NAME}: PR #{number}: {why} — {choice}, {did}; skipped "
        + (", ".join(f"td{i}" for i in skipped) or "nothing")
    )


# ── Job protocol ───────────────────────────────────────────────────


def submit(ctx: Any, spec: Any) -> Any:
    """Do the act and drive the job terminal (no detached compute)."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    try:
        ok, summary = act(ctx.store, int(params.get("todo_id") or 0))
    except Exception as exc:
        log.warning("%s: act raised", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: act raised {type(exc).__name__}: {exc}")
        return None
    if not ok:
        ctx.record_failure(summary)
        return None
    ctx.append_chunk("job_summary", summary)
    ctx.set_status("succeeded")
    return None


def poll(ctx: Any, handle: Any) -> bool:
    """Never reached with a handle (:func:`submit` finishes the job)."""
    del handle
    ctx.record_failure(f"{NAME}: unexpected poll (submit finishes the job)")
    return True


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "IGNORE_MAJOR_COMMENT",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "act",
    "answer_of",
    "parse_choice",
    "poll",
    "submit",
    "validate_submit",
    "wall_seconds",
]
