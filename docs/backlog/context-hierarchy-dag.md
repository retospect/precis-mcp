---
status: draft
title: One addressed hierarchy over CLAUDE.md, conventions, skills and memory — resident core + discovered DAG with recency bands
---

# Context hierarchy — resident core, discovered DAG

Reto approved the two-level shape for conventions (2026-09-14) and then
widened it: n levels if practical, memory included, plus a recency structure,
combined with skills and the identity/"soul" layer. This item is the
resulting plan. Not started — the session that produced it shipped only these
two backlog files.

## Motivation / why

Three surfaces load into every session independently: `CLAUDE.md` (~2098 tok),
`MEMORY.md` (~3800 tok of 80 bullets), and the skill listing. They overlap —
several memory bullets restate rules already in `CLAUDE.md` or
`docs/conventions/` — and none of them has a placement rule, so each grows by
accretion.

Once a fact is *discovered* rather than preloaded, size stops being the
constraint and reachability becomes it. Both failures are silent: too big
means the signal drowns, never-found means the memory that would have
prevented the hour-long mistake was one hop away and nothing said so.

## The placement rule

> **Resident = what fires without being asked. Discovered = what answers a
> question you know you're asking.**
> Demote anything that *answers*. Keep anything that *prevents*.

`0%-CPU client is not a dead build` prevents — resident. `se kind slice
state` answers, and the prompt names `se` when it is needed — discovered.

## Layers

**L0 — soul (resident, exempt from demotion).** Identity + constraints
(`AGENTS.md` §Identity), response style (`CLAUDE.md` §Response style),
standing prohibitions (session MCP targets prod; forward-only migrations; no
cluster addresses; `safe_get` for agent-supplied URLs; no patent reading for
commercial work). These are not lookups — they shape every answer. The
exemption is stated so later diet passes do not nibble at them.

**L1 — traps + addresses (resident).** The biting one-liners of
`CLAUDE.md` §Hook/gate-enforced, stated as rules with rationale moved to the
leaf that owns it; plus ~6 address lines (conventions skill, memory DAG root,
`docs/codebase.md`, `docs/backlog/INDEX.md`, `docs/runbooks/INDEX.md`,
`AGENTS.md`). Symptom-recognition memory bullets stay here — they fire on
recognition, and a hop kills that.

**L2 — indexes (discovered).** `.claude/skills/conventions/SKILL.md`;
~5 memory cluster notes; the generated backlog and runbook indexes. Size
genuinely stops mattering at this level.

**L3 — leaves.** `docs/conventions/*.md`, the 83 memory topic files,
runbooks, package docstrings. Unchanged — leaves stay the single source of
truth; indexes never copy their content.

Edges already exist in all three dialects: `[[slug]]` in memory, markdown
links in docs, skill frontmatter in the session listing. It is a DAG, not a
tree — a leaf takes several inbound edges (photonic-arm hangs off both the
multiscale cluster and the quest cluster).

## Recency bands — computed, not curated

| band | test | placement |
|---|---|---|
| `ACTIVE` | touched < ~7d, or carries open-work words | head of its cluster, full hook |
| `SETTLED` | landed + verified, durable fact worth keeping | mid-cluster, name + one clause |
| `SPENT` | every cited sha in `main`, no open-work words | cluster tail, delete candidate next pass |

A memory moves band by oracle, never by hand. The SPENT oracle already exists
in `scripts/memory-lint` check 2 and is currently dead — see
`memory-lint-threads-scan-dead.md`. Reviving it and having it emit a band per
memory is the entire recency mechanism; no new machinery.

## Insertion rule — the anti-flattening part

The `##` grouping the lint documents *did* exist and drifted back to flat.
Structure with no enforced insertion rule rots to flat. So a new fact is
placed by:

1. Prevent or answer? → L1 trap line, or L3 leaf.
2. Which cluster already owns the subject? → link from there. If none owns
   it, it is a new cluster, or it is not a memory at all — it is a doc.
3. **New lint check:** a leaf whose only inbound edge is `MEMORY.md` is
   *unplaced* — flag it. This is the inverse of the existing unindexed check
   and is what makes step 2 stick.

## Skills

`conventions` is the first instance of **one index skill per domain**, never
one per doc. The cap is real: every skill description loads into the resident
listing, so index skills carry a resident cost even though their bodies do
not.

No memory skill. A skill is *invoked*; a memory is *recalled*. Wrapping
memories in a skill strictly reduces reachability. Cluster notes stay plain
memory files so they keep both paths — harness relevance-recall and
link-walk.

## Phases

**P0 — repo-only, one commit, gateable.**
- `.claude/skills/conventions/SKILL.md` — annotated toc over
  `docs/conventions/*.md`, one line per leaf (when to reach for it + path),
  no content copied. 14 entries, including a `rtk.md` §Long-running commands
  entry.
- `docs/conventions/rtk.md` — author §Long-running commands: background
  launch, redirect to a log, harness exit notification as the completion
  signal, optional Monitor with a filter covering every failure signature,
  and no waiter loops / blocking `TaskOutput`. Fold in the ~22K stdout-cap
  fact. This section does not exist yet; the nearest prose is
  `.claude/skills/quiet-output/SKILL.md` §"Long runs", which then collapses
  to a pointer.
- `CLAUDE.md` §Hook/gate-enforced — keep every rule, drop the rationale that
  already lives in a leaf, collapse the three `→ docs/conventions/x.md`
  arrows into one router line.
- `AGENTS.md` §On-demand pointers — same collapse for the **Conventions**
  bullet, which currently hand-picks `thresholds.md` and
  `llm-facing-prose.md`. The three inline refs at lines 92/125/130 stay:
  load-bearing inside a procedure, not a toc.
- `scripts/memory-lint` — fix check 2, add the band emitter, add the
  unplaced-leaf check.

**P1 — user-level memory files, outside the repo (cannot share the commit).**
Restore the five `##` groups; run the currency pass against the now-live band
report; retire SPENT; write the ~5 cluster notes
(multiscale, remarkable, claims, gripes, quests); rewire edges.

Known absorb candidates — memory bullets duplicating repo docs:
`background-task output cap kills gates` (becomes the rtk §Long-running
section), `no _Verified stamps` (already `llm-facing-prose.md` freshness
contract), `deploy tree: no real hostnames` (already a CLAUDE.md one-liner).
`soft-delete filters in ad-hoc SQL` and `green gate != green CI` look similar
but carry operational detail — check against the docs before killing.

**P2 — needs Reto's call.** Whether §Identity/prohibitions and §Response
style consolidate into one resident block, and whether that lives in
`CLAUDE.md` or its own `.claude/` file. Touches the most always-loaded text.

## Measurement

`scripts/memory-lint` prints the preamble token count (CLAUDE.md+MEMORY.md;
5904 tok against a 6000 budget at 2026-09-14). P0 alone is close to
token-neutral — three pointer fragments out (~35 tok), one router line in
(~38 tok), and the new skill's description enters the resident listing
(~40 tok). The shrink comes from the rationale trimming in P0 and, much more,
from P1's thread collapse: the mass is bullet *count*, not fat bullets
(75 of 80 bullets are under 300 B), so only collapsing many lines moves the
number.

## Caveats

- "Size no longer matters" holds for L2/L3 only. L0, L1 and the skill listing
  are resident by definition.
- The failure mode moves from *too big* to *never found* — equally silent.
  The band report and the unplaced-leaf check are the only automated defence;
  the rest is discipline.
- Load-bearing assumption: the harness's relevance-recall over topic files is
  good enough to make discovery work. Unverified from inside a session.

## Out of scope

`.claude/commands/*` (go/land/qland/next are task skills, not conventions);
per-convention skills — one index line in the session listing is the point.
