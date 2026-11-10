---
id: precis-repo-dev-start-help
family: core
flavor: reference
title: precis — repo-dev start page (coding-agent orientation)
summary: fixed start page for sessions coding on the precis-mcp repo — recall memory first, then one-line pointers to the memory layer, repo docs and their canonical skill owners
answers:
  - where do I start when my task is work on the precis-mcp repository?
  - how do I recall repo-dev knowledge before touching code?
  - where do the ship, test, convention and memory procedures live?
  - who wins when a served page or a memory disagrees with the checkout?
  - what do I read when the precis MCP is unreachable?
applies-to: repo-dev sessions on the precis-mcp checkout
status: active
tags: [orientation, workflow]
---

# precis-repo-dev-start-help — repo-dev start page

An index, not a manual: one trigger plus its owning page per line.
Fetch a target when the task hits its trigger — nothing here is meant
to be read end to end.

Authority order: the checkout (`AGENTS.md`, `CLAUDE.md`, `docs/`,
`scripts/`) and current user rulings win; this served copy (its
`skill rev` trailer is the file's revision) and recalled memories are
pointers and dated evidence, never new permission. The same file in
the checkout is the MCP-outage fallback.

## Recall repo-dev knowledge before I touch code
## Orient in the memory graph at task start

```python
search(kind="memory", tags=["SPACE:repo-dev"], q="<the task>", view="index")
get(kind="skill", id="soul")                             # conduct index — open only the entries the task touches
get(kind="memory", id="me489895")                        # rulings & preferences hub
search(kind="taxon", q="<coined or overloaded term>", args={"under": "tn559460"})
```

Exploratory hub/index reads ride `extent='fisheye+2hop'`; an explicit
narrow read stays narrow, and a truncated render names its own next
read. Graph root me474312 → `section:index` hubs; browse mechanics:
[[precis-fisheye-help]].

## Write or fix memories
## Find the rules for remembering, griping and gardening

[[precis-memory-help]] owns the shape (one claim per node, hub
`part-of`, `section:<type>` tags); [[precis-make-memory]] reconciles
free text before writing; [[precis-fix-memory-gripe-help]] is the lane
when a recalled memory misled; [[precis-gardener-memory-help]] owns
gardener passes.

## Navigate the checkout structurally
## Read code and repo docs without grepping first

```python
search(kind="python", mode="pattern", q="<symbol>")        # structure first, grep second
get(kind="python", id="main::<qualname>")                  # signature, callers, callees
get(kind="md", id="main/docs/codebase.md")                 # repo shape → owning package __init__.py
get(kind="md", id="main/docs/backlog/INDEX.md")            # open work; threads/<slug>.md = campaign resume state
```

`wt-<tree>::<qualname>` reads your worktree; `scripts/coderef
callers|deps` walks it too. Grep is the truth for code you changed.
Mechanics: [[precis-python-help]], [[precis-md-help]].

## Find ship, test and convention procedures
## Run the repo's own procedures

- Ship/deploy authority and worktree rules: `docs/conventions/shipping.md` — run `scripts/ship`, read `scripts/inflight` first.
- Tests, lint, types: `scripts/test` — `docs/conventions/testing.md`. `uv` for everything.
- Irreversible invariants (migrations, chunks, safe_fetch, secrets): `docs/conventions/invariants.md`.
- Every convention leaf: `docs/conventions/`; schema: `docs/reference/schema.md`; MCP outage: `docs/runbooks/memory-restore.md` plus `outage-memory.md` beside the harness `MEMORY.md`.

## Coordinate work and the surface
## File bugs, claim todos, ask the user

- Bug intake → the checkout's `bug` skill; masked root cause → `root-cause`; a durable fact learned → `remember`.
- Todos, asks, work views: [[precis-work-help]]; gripe verbs: [[precis-gripe-help]]; the fleet loop: [[precis-gripe-loop-help]].
- What this deployment pins or disables: [[precis-session-context-help]]; verbs and kinds: [[precis-overview]].

## See also

- [[precis-search-help]] — search mechanics
- [[precis-startup-skills-help]] — pinning this page into the cold-start banner
- [[precis-session-context-help]] — the banner, disabled kinds, default tags
