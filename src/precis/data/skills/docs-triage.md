---
id: docs-triage
family: work
title: precis — keep the open work live, retire the rest
summary: triage open work in the spec graph — verify then retire built or stale sections against code handles, park on the plan todo, compact bloated nodes, and fold surviving truth into the owning package docstring; git log and the revision trail are the archive
answers:
  - how do I act on a spec section the linter flagged as done?
  - the work shipped — what do I do with its spec section?
  - how do I park specced work without losing it?
  - where does a retired section's surviving rationale go?
applies-to: get/delete/edit (kind='memory'), get/tag (kind='todo'), get (kind='python')
status: active
tags: [workflow]
governs: factory
---

# docs-triage — keep the open work live, retire the rest

**The disease.** A spec section is *open design*. When its work ships, the
truth moves into code + the owning package docstring, but nobody retires
the section — so it lingers, gets READ as open work, and rots. Cure:
**retire-default** ("rest in git and the revision trail for the
archaeologists"). The live graph is the present. Specifications are hubs of
linked memory nodes under the Specifications hub, not files: the format and
the prune and retirement passes are [[precis-spec-help]].

## Start here

The repo's own linters are advisory (they never delete and never fail a
build); they surface candidates and you apply the judgment they can't. The
spec inventory is generated, so nothing is hand-maintained:

```python
search(kind="memory", tags=["spec:*"], view="toc")   # every spec root
get(kind="memory", id="<spec root>", view="toc")     # its sections, in reading order
get(kind="todo", id="<plan root>", view="tree")      # what is actually being built
```

## The per-section verdict

Classify against code **through the python kind** — the prune pass,
`precis-spec-help~9`. Evidence rule: **no verified handle, no BUILT**.

```python
search(kind="python", mode="pattern", q="<symbol>")
get(kind="python", id="<alias>::<qualname>")
```

- **Built** — the behaviour is in the shipped code. Retire the section,
  superseded by the symbol's handle; the group hub or spec root keeps a
  one-line pointer naming the behaviour and the handle. Never leave a
  "DONE ✅" body; the graph is the active design, not an archive.

```python
delete(kind="memory", id="<id>", args={"superseded_by": "python:main::<qualname>"})
```

- **Partial** — keep only the open design, opening with **one Built
  sentence** naming the built part and its handles.
- **Stale** — the section describes a problem the code no longer has, or a
  plan overtaken by a different shipped approach. Verify against the code
  (don't trust the prose), then retire. If retiring would orphan a claim a
  kept node or doc relies on, correct that one in the same pass.
- **Parked** — deliberately shelved, not dead. Park on the **plan todo**
  (its `STATUS:`) with a `due:YYYY-MM-DD` tag for the unblock date; triage
  skips it until then, and on the date re-probes the condition and acts or
  bumps it. Memory carries no snooze field.
- **Reference** — surveys, evidence and gotchas stay. Rulings on shipped
  decisions stay too, as `absorbed`.
- **Open** — keep. Compact if bloated (bar below).

Before retiring, read the node's ring with `extent='fisheye+1hop'`, so no
live spec, plan or gripe points at a node about to go.

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
  compaction — the spec lint flags it.
- A **small item** — an idea, a finding, a one-paragraph annoyance — is a
  gripe, never a spec node.

## Where truth folds

On ship (or when retiring a stale section that carried real rationale), any
surviving truth — the "why", the rejected alternative, the invariant —
folds into the **owning package's `__init__.py` docstring**, compactly, in
the same commit. Nothing else is an archive: no done-log, no CHANGELOG, no
completed/ hub. Reusable incident forensics go to the repo's runbooks.

## See also

- [[precis-spec-help]] — the spec-in-memory format, the prune pass, retirement.
- [[flow-stages]] — the spec stage writes these nodes; the ship stage retires them.
- [[next]] — the spec graph and the gripe tracker are where the next piece of work comes from.
- [[whatneedsdoing]] — scanning open work before picking an item.
