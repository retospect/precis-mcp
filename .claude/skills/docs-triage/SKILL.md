---
name: docs-triage
description: "Triage open work: when to retire a spec section (built, stale, parked), how far to compact, where surviving truth goes. Use when memory-lint or backlog-lint flags items."
---

# docs-triage — keep the open work live, retire the rest

**The disease.** A spec section is *open design*. When its work ships, the
truth moves into code + the owning package docstring, but nobody retires
the section — so it lingers, gets READ as open work, and rots. Cure:
**retire-default** ("rest in git and the revision trail for the
archaeologists"). `git log` and the node's revision history are the
archive; the live graph is the present. Specs live in graph memory under
the Specifications hub `me568215`, not in files (Reto 2026-11-05,
`me568168`); contract `get(kind='skill', id='precis-spec-help')`, ship
contract `docs/README.md` §Spec lifecycle.

## Start here

    scripts/memory-lint      # graph findings: stale current-state prose, orphans, expiry
    scripts/backlog-lint     # the residual docs/backlog/ files (threads + the one legacy spec)

Both advisory (never delete, never fail a build); they surface candidates
and you apply the judgment they can't. The spec inventory is generated:
`search(kind='memory', tags=['spec:*'], view='toc')` lists every spec root,
`get(kind='memory', id=<root>, view='toc')` its sections, and
`get(kind='todo', id=<plan root>, view='tree')` the execution state that
says what is actually being built.

## The per-section verdict

Classify against code **through the python kind** (`search(kind='python',
mode='pattern', …)`, `get(kind='python', id='<alias>::<qualname>')`) — the
prune pass, `precis-spec-help~9`. Evidence rule: **no verified handle, no
BUILT**.

- **Built** — the behaviour is in code on `main`. Retire the section,
  superseded by the symbol's handle:
  `delete(kind='memory', id=<id>, args={'superseded_by': 'python:main::<qualname>'})`.
  The group hub or spec root keeps a one-line pointer naming the behaviour
  and the handle. Never leave a "DONE ✅" body; the graph is the active
  design, not an archive. While `gr569350` blocks deletion, leave a
  retire-pending pointer node under the spec's holding hub instead.
- **Partial** — keep only the open design, opening with **one Built
  sentence** naming the built part and its handles.
- **Stale** — the section describes a problem the code no longer has, or a
  plan overtaken by a different shipped approach. Verify against the code
  (don't trust the prose), then retire. If retiring would orphan a claim a
  kept node or doc relies on, correct that one to code-truth in the same
  pass.
- **Parked** — deliberately shelved, not dead. Park on the **plan todo**
  (its `STATUS:`) with a `due:YYYY-MM-DD` tag for the unblock date; triage
  skips it until then, and on the date re-probes the condition and acts or
  bumps it. Memory carries no snooze field.
- **Reference** — surveys, evidence and gotchas stay. Rulings on shipped
  decisions stay too, as `absorbed`.
- **Open** — keep. Compact if bloated (bar below).

Before retiring, re-scan for stragglers:

    grep -rn "me<N>" . --exclude-dir=.git    # fix every surviving ref in the tree

and read the node's ring — `get(kind='memory', id=<id>,
extent='fisheye+1hop')` — so no live spec, plan or gripe points at a node
about to go.

## The compaction bar

- A **spec root** holds purpose, owner handles, its plan todo, scope
  exclusions and dependencies — not a copy of its sections. When it
  approaches the 4000-character fisheye limit, group sections into
  sub-hubs rather than trimming meaning.
- A **section** is one design claim, prose only, under about 3500
  characters, with observable acceptance and explicit unknowns. Acceptance
  criteria and blast radius are load-bearing for the builder; don't compact
  a live section down to a title.
- A section carrying several independent claims is a split, not a
  compaction — memory-lint flags it.
- A **small item** — an idea, a finding, a one-paragraph annoyance — is a
  gripe, never a spec node and never a file.

## Where truth folds

On ship (or when retiring a stale section that carried real rationale), any
surviving truth — the "why", the rejected alternative, the invariant —
folds into the **owning package's `__init__.py` docstring**, compactly, in
the same commit. Nothing else is an archive: no done-log, no CHANGELOG, no
completed/ hub. Reusable incident forensics go to `docs/runbooks/`.
