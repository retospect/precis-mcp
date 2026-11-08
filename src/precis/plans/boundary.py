"""The security-boundary registry (td574934 A2).

Until this module nothing on main named the security boundary files, so a
factory branch touching the agent envelope, ``safe_fetch``, the dispatch
path or the secret code was indistinguishable from any other diff at land
time. The registry fixes that: :data:`BOUNDARIES` names the path sets
whose change always wants a human security review, :data:`CONTENT_RULES`
the added-line patterns with the same effect, and :func:`boundary_hits`
is the one call a land makes over the branch's changed paths and added
lines. A boundary hit is NOT a deny rule — the deny list is human-only
(Reto) — it marks the diff for the security-review lane; ``boundary.py``
and ``scripts/probes/**`` protect themselves by being ON the list, so the
fence cannot quietly move.

Matching is the same matcher as
:func:`precis.workers.job_types.run_slice.deny_violations`: repo-relative
posix paths through ``fnmatch.fnmatchcase`` — ``*`` already spans ``/``,
so ``dir/**`` is the subtree. The registry test pins every glob to at
least one tracked file, so a rename that would silently unwatch a
boundary file reds the build instead.

A boundary's ``probe`` names the mechanical check under
``scripts/probes/`` that may clear its hits (A3 wires the run into
``land_slice``); ``None`` — every boundary today — or a probe whose
script is absent from the tree means the hit stays :func:`unprobed` and
keeps its review. Nothing reads the registry yet: it is read-only while
A3 (the land wiring) and A5 (the lane pick) are unbuilt.

:data:`SECURITY_LANE_ADAPTERS` is the adapter set a ``security-review``
slice may launch on — the reviewer of a boundary diff is never the
builder's own vendor CLI, so only ``claude_p`` carries the class. It
lives here, not in ``lanes.py``: ``lanes.py`` stays on ``INTERIM_DENY``
and A5 imports the set from this module. A codex or devin account row may
still LIST the class — the ``put(kind='llm_account')`` vocab accepts it —
but :func:`pick_lane` narrows the table before the fit, so the pick
refuses them whatever the row says.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes

#: The task class a boundary-hit land is reviewed under.
SECURITY_REVIEW = "security-review"

#: The adapters a ``security-review`` slice may launch on (td574934 A2;
#: A5 imports it from here — ``lanes.py`` stays on INTERIM_DENY). Only
#: the Claude subscription lane reviews a boundary diff: a codex or devin
#: lane reviewing the boundary its own builder crossed is no review.
SECURITY_LANE_ADAPTERS: frozenset[str] = frozenset({"claude_p"})

#: The repo root this module sits in — the default tree :func:`unprobed`
#: checks probe scripts against (the land passes the slice worktree).
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Boundary:
    """One security boundary: the paths a factory diff may not touch
    without the change being marked for a security review.

    ``patterns`` are repo-relative posix globs matched like
    ``run_slice.deny_violations`` (``fnmatch.fnmatchcase``; ``**`` spans
    directories). ``probe`` is the repo-relative script under
    ``scripts/probes/`` whose green run may clear a hit — ``None`` while
    no probe exists, which is every boundary today. ``why`` is the one
    line the review prompt quotes: what a quiet edit here buys.
    """

    name: str
    patterns: tuple[str, ...]
    probe: str | None
    why: str


@dataclass(frozen=True)
class ContentRule:
    """A regex over a changed file's ADDED lines: content that marks the
    diff for the security review whatever boundary the path is on.

    ``paths`` are the same repo-relative posix globs as
    :attr:`Boundary.patterns`; ``pattern`` is the regex searched per
    added line, compiled case-insensitively (SQL keywords fold — a
    lowercase ``grant`` must not evade; a rule that needs case sensitivity
    inlines ``(?-i:…)``). ``probe`` and ``why`` read as on
    :class:`Boundary`.
    """

    name: str
    paths: tuple[str, ...]
    pattern: str
    probe: str | None
    why: str


@dataclass(frozen=True)
class Hit:
    """One boundary or content rule a diff tripped, with its evidence:
    the changed ``paths`` that matched and, for a content rule, the
    ``lines`` (added-line text, no diff prefix) that matched."""

    rule: Boundary | ContentRule
    paths: tuple[str, ...]
    lines: tuple[str, ...] = ()


#: The initial boundary list (td574934 §1), fixed against the tracked
#: tree: the agent envelope, the SSRF guard, the factory's dispatch and
#: land path, the credential modules, and the registry itself. A hit is
#: a review mark, never a refusal — the deny list is human-only.
BOUNDARIES: tuple[Boundary, ...] = (
    Boundary(
        "agent-envelope",
        (
            # the session's env allowlist, deny rules and adapter launch
            "src/precis/workers/job_types/run_slice.py",
            "src/precis/workers/job_types/_slice_adapters.py",
            # the per-slice grant: mint, validate, revoke
            "src/precis/slice_tokens.py",
            # the scoped-grant allowlist and its two checkpoints
            "src/precis/slice_reader_scope.py",
            "src/precis/server.py",
            "src/precis/tools/core.py",
            # the slice write-scope gate (mcp.slice_scope)
            "src/precis/principal.py",
        ),
        None,
        "the session's capability surface: env, grants and the scope "
        "checkpoints — a quieter envelope is invisible in a "
        "feature-sized diff",
    ),
    Boundary(
        "safe-fetch",
        ("src/precis/utils/safe_fetch.py",),
        None,
        "the SSRF guard every agent-supplied URL goes through — a "
        "weakened check turns any agent-reachable fetch into an internal "
        "network probe",
    ),
    Boundary(
        "factory-dispatch",
        (
            "src/precis/workers/job_types/plan_dispatch.py",
            "src/precis/workers/job_types/land_slice.py",
            "src/precis/workers/factory_rescue.py",
            # the task_class a node is minted with picks its lane — a
            # quiet edit classes a boundary build out of review
            "src/precis/plans/templates.py",
        ),
        None,
        "what the factory runs and lands next: dispatch picks, the "
        "reviewer mint and the rescue re-arm — a quiet edit routes "
        "around the review loop",
    ),
    Boundary(
        "secrets",
        (
            "src/precis/secrets.py",
            "src/precis/secret_status.py",
            "src/precis/cli/secret.py",
            "src/precis/utils/secret_scan.py",
            "src/precis/utils/claude_oauth.py",
            "src/precis/utils/llm/accounts.py",
            "src/precis/workers/secret_check.py",
            "src/precis/workers/job_types/secret_status.py",
            # resolves the prod coordinates ad-hoc SQL runs against — a
            # redirect here points prod-precis at another host
            "scripts/lib/pgb-host.sh",
        ),
        None,
        "credential storage, resolution and the write-path secret gate",
    ),
    Boundary(
        "boundary-registry",
        (
            "src/precis/plans/boundary.py",
            "src/precis/plans/lanes.py",
            "scripts/probes/**",
        ),
        None,
        "the registry and the deny-list module protect themselves: a "
        "boundary edit, a deny-list shrink or a probe edit is itself a "
        "boundary hit",
    ),
)

#: The added-line rules (td574934 §1). First rule: privilege SQL in a
#: migration — a GRANT/REVOKE/role statement or a SECURITY DEFINER body
#: reshapes who may read what, and the path glob keeps the same text in a
#: ``.py`` from firing.
CONTENT_RULES: tuple[ContentRule, ...] = (
    ContentRule(
        "migration-privilege-sql",
        ("src/precis/migrations/*.sql",),
        r"\b(GRANT|REVOKE|ALTER ROLE|CREATE ROLE|SECURITY DEFINER)\b",
        None,
        "privilege SQL in a migration reshapes the data boundary; it "
        "wants the security review a plain schema change does not",
    ),
)

#: ``(rule, compiled regex)`` pairs built once — patterns compile
#: case-insensitively because SQL keywords fold.
_COMPILED_RULES: tuple[tuple[ContentRule, re.Pattern[str]], ...] = tuple(
    (r, re.compile(r.pattern, re.IGNORECASE)) for r in CONTENT_RULES
)


def _matched(paths: Iterable[str], globs: tuple[str, ...]) -> tuple[str, ...]:
    """The ``paths`` matching any ``globs`` glob — the
    ``run_slice.deny_violations`` matcher (``fnmatchcase``; ``*`` spans
    ``/``, so ``dir/**`` is the subtree)."""
    return tuple(
        sorted({p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in globs)})
    )


def boundary_hits(
    changed_paths: Iterable[str],
    added_lines: Mapping[str, Iterable[str]] | None = None,
) -> list[Hit]:
    """The :class:`Hit`\\s a branch diff trips.

    ``changed_paths`` are the diff's repo-relative posix paths;
    ``added_lines`` maps a changed path to the text of its added lines
    (no ``+`` prefix — the content rules read payload only). Path hits
    come from :data:`BOUNDARIES`, line hits from :data:`CONTENT_RULES`
    over the added lines of the rule's own paths — the same text in a
    file outside the glob does not fire. ``added_lines`` may be omitted
    when only path hits matter.
    """
    paths = list(changed_paths)
    hits = [
        Hit(rule=b, paths=matched)
        for b in BOUNDARIES
        if (matched := _matched(paths, b.patterns))
    ]
    for rule, rx in _COMPILED_RULES:
        hit_paths: list[str] = []
        hit_lines: list[str] = []
        for path, lines in (added_lines or {}).items():
            if not any(fnmatch.fnmatchcase(path, g) for g in rule.paths):
                continue
            matched_lines = [ln for ln in lines if rx.search(ln)]
            if matched_lines:
                hit_paths.append(path)
                hit_lines.extend(matched_lines)
        if hit_paths:
            hits.append(
                Hit(rule=rule, paths=tuple(sorted(hit_paths)), lines=tuple(hit_lines))
            )
    return hits


def unprobed(hits: Iterable[Hit], *, root: Path | None = None) -> list[Hit]:
    """The ``hits`` no probe covers: ``rule.probe`` unset — every rule
    today — or the named script absent under ``root`` (default: the repo
    root this module sits in; the land passes the slice worktree). A
    probe that was never written, or was deleted, must not silently
    clear its boundary."""
    base = root if root is not None else _REPO_ROOT
    return [
        h for h in hits if h.rule.probe is None or not (base / h.rule.probe).is_file()
    ]


def fit_lanes(task_class: str, table: Iterable[_lanes.Lane]) -> tuple[_lanes.Lane, ...]:
    """The lane table a ``task_class`` pick fits over: a
    ``security-review`` slice launches only on
    :data:`SECURITY_LANE_ADAPTERS`, whatever class the account rows
    claim — a vendor lane reviewing the boundary its own builder crossed
    is no review. Every other class gets the table unchanged."""
    if task_class == SECURITY_REVIEW:
        return tuple(ln for ln in table if ln.adapter in SECURITY_LANE_ADAPTERS)
    return tuple(table)


def pick_lane(
    store: Any,
    task_class: str,
    *,
    lanes: Iterable[_lanes.Lane] | None = None,
    **kw: Any,
) -> _lanes.Lane | None:
    """``lanes.pick_lane`` over :func:`fit_lanes`' table — the pick a
    security-class dispatch makes (A5). ``lanes`` is the account-derived
    table (``lanes.load_lanes(store)`` when omitted); ``kw`` forwards to
    :func:`lanes.pick_lane` (``counts`` skips its DB reads, as there)."""
    table = fit_lanes(task_class, _lanes.load_lanes(store) if lanes is None else lanes)
    return _lanes.pick_lane(store, task_class, lanes=table, **kw)


__all__ = [
    "BOUNDARIES",
    "CONTENT_RULES",
    "SECURITY_LANE_ADAPTERS",
    "SECURITY_REVIEW",
    "Boundary",
    "ContentRule",
    "Hit",
    "boundary_hits",
    "fit_lanes",
    "pick_lane",
    "unprobed",
]
