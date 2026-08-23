---
status: draft
title: Skill index caches a partial build forever, so a shed embed can drop slugs out of semantic search until restart
prio: normal
---

# Skill index caches a partial build forever

Found by the pre-ship reviewer on the gripe-244419 bulkhead ship
(2026-08-23). Filed rather than fixed: the fix is a change to the skill
index's rebuild policy, not to the bulkhead, and bundling it would have
widened a wedge fix into a caching redesign.

## Motivation / why

`skill_index/index.py::FileCorpusIndex._build()` only re-attempts the whole
build on a later `search()` when `is_ready()` reports `False`. Once a build
pass completes, `_entries` is set and never rebuilt for the process's
lifetime — even if individual per-slug embeds failed and were swallowed by
`_build_one`'s broad `except Exception`.

That was already latent. The bulkhead
(`embedder.py::BoundedConcurrencyEmbedder`, default 4 in-flight) makes it
more reachable: `handlers/skill.py` takes `hub.embedder`, which on the
request path is now the wrapped embedder, so a boot-window skill-index
build that contends with concurrent request-path embeds can be **shed**
where before it would merely have waited. Net effect: a busy boot window
can permanently drop some or all skill slugs out of semantic search until
the process restarts, with no error surfaced.

Note the shape — this is the one place where "shed instead of wait" is
genuinely worse than waiting, because the caller caches the failure
permanently instead of retrying. Everywhere else on the request path a shed
degrades one call to lexical-only and the next call retries.

## In scope

Make a partial build non-permanent. Cheapest plausible shapes, in
preference order:

1. Track whether any slug's embed failed during `_build()`; if so, mark the
   index incomplete so the next `search()` rebuilds (or backfills just the
   missing slugs) rather than serving the gap forever.
2. Have the skill-index build use an unbulkheaded embedder — it is a bulk
   pass, not a request, so it wants the same treatment
   `build_runtime(interactive=False)` gives `cli/taproot.py`.

(1) is the durable fix; (2) alone only narrows the window.

## Explicitly NOT in scope

- Changing the bulkhead's shed-don't-queue contract. Queueing is what the
  bulkhead exists to prevent — see gripe 244419.
- Making `_build_one` propagate instead of swallow. Its broad catch is
  deliberate: one bad skill file must not take out the whole index.

## Acceptance criteria

- A build in which one slug's embed raises leaves the index in a state
  where a later `search()` retries that slug, rather than serving a
  permanent gap.
- A build in which every embed succeeds still builds exactly once — no new
  per-search rebuild cost on the happy path.
- Concurrent `search()` callers during a rebuild don't each trigger their
  own build.

## Target + blast radius

`skill_index/index.py` (`FileCorpusIndex._build` / `_build_one` /
`is_ready`), `handlers/skill.py`'s embedder acquisition. Read path only —
semantic skill discovery. Watch: `search(kind='skill', q=...)` recall after
a busy boot.

## Open questions / decisions log

- Does `is_ready()` currently have any notion of "built but incomplete", or
  is it purely "built at all"? That determines whether (1) is a flag or a
  new state.
