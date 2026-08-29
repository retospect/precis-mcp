---
status: in-progress
title: bring the 139 claim hubs behind the nanobud draft above board
prio: high
---

# Nanobud claim remediation (dr173020)

In-flight. Full phase plan + rationale:
`~/.claude/plans/greedy-gliding-anchor.md`. This file is the durable
resume pointer — the measured state and the decisions already made.

**Target is `dr173020`** ("WC:…", 180 body chunks, 139 claim hubs).
**Not `dr43020`** — that one has *zero* claim hubs (its 100 cites are
paper-level) despite a later `updated_at`; the taproot work is all on
173020.

## Measured state (2026-08-29, prod)

139 claim hubs cited; **108 (78%) clean on both axes**, 31 are not:

| problem | n |
|---|---|
| fail the approve-time blocking lint | 18 |
| live `contradicts` edge | 4 |
| zero verdicts (15 withheld-only, **2 with no evidence edges at all**) | 17 |
| overlap lint ∩ evidence | 8 |

Zero refuted. All 3 `signed` hubs in the whole corpus are nanobud hubs
and all 3 are clean. The draft also touches 42 non-hub chase findings —
no posture, no gate, invisible to every check above.

The 18 lint-failers:

```
189535 189536 189542 189543 190987 191014 191169 191260 191307
191318 192836 192855 211522 269443 269509 269510 269543 269548
```

(fi189542 is also disputed, so `reword.py::_COHORT_SQL` excludes it —
expect 17 to actually process.)

The 2 with no evidence edges at all: **fi211522** ("Graphene is the
strongest material ever measured…" — a superlative, likely cut) and
**fi191014**.

## The ordering constraint

**Reword before verify.** `taproot/hub.py::refine_claim_sentence` never
touches `links.meta`; `nanopub/preflight.py::withheld_edges` compares
`meta.verified_claim_sha` against `claim_sha(live refs.title)` at *read*
time and withholds on mismatch. So rewording a hub stales every verdict
it already had. Verify-first pays for the same LLM work twice.

## The 4 disputed — three are prose, not evidence

Only **fi189542** is a real evidence conflict (contradicted by paper
pa5828, *Broad Family of Carbon Nanoallotropes*). It needs a human.

The other three are contradicted by **review findings against the
draft's own prose**, not by opposing evidence:

- fi191315 ← fi255164 *"citation overstates beam-damage artifact as
  'engineering'"* (chunk dc2445944)
- fi191316 ← fi192706 *"claim-strength inflation — 'will ultimately
  require' vs source's 'could be used'"* (dc2445944)
- fi191329 ← fi255165 *"doesn't cover 'two distinct methods / CO
  disproportionation' clause"* (dc2445957)

Those are text fixes. See
`docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` for why
fixing the prose will *not* clear the hub's `disputed` posture on its own.

## Decisions already taken

- Uncited-assertion hunt: **full adversarial pass** over the draft body
  (`quest/review_fanout.py::_FANOUT_ONLY_BRIEFS['adversarial']`, opus).
  No mechanical detector exists — every hygiene check is token-anchored
  and only inspects citations already present.
- A claim with no hub *and* no supporting passage in the held corpus:
  **soften or cut the prose**, don't go acquire. Report every cut.
- Reach `refine_claim_sentence` only through `reword-sweep` — the freeze
  guard lives in the cohort SQL, and the manual retitle door
  (`edit(kind='finding', title=…)`) has **no** freeze check at all.
- Attach evidence via MCP `put(kind='finding', supporters=[…])`, not
  `direct-mint --apply` — see
  `docs/backlog/direct-mint-apply-rerolls-the-reviewed-sentence.md`.

## Blocker

The auto-mode classifier blocks agent-run prod CLI, and cannot
distinguish `--dry-run` from `--apply`. Every `reword-sweep` /
`verify-edges` invocation must be handed to the user as a prepared
command + success criterion (`docs/runbooks/prod-one-off-cli.md`). Draft
body edits go through the session MCP, which *is* write-capable.
