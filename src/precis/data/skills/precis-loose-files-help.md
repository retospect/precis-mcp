---
id: precis-loose-files-help
family: work
title: precis — give loose working files a home (repo or precis), gripe the gaps
summary: map each loose working file (spec, ruling log, red list, metrics, drafts, scripts, research dumps) to its repo or precis home; gripe + meta-gripe what has none
answers:
  - where should this working file live instead of /tmp or my home dir?
  - I have a directory of drafts and notes, how do I empty it into precis?
  - where do rulings, metrics, red lists and specs go?
  - what do I do when a file has no precis home?
  - how do I clean up a session's loose files before handing off?
applies-to: put (kind='gripe'|'todo'|'checklist'|'memory'|'measure'), backlog files, repo branches
tags: [workflow]
kinds: [gripe, todo, checklist, memory, measure, perplexity-research, finding]
status: active
---

# precis-loose-files-help — give loose working files a home

Loose working files (`/tmp/<x>`, `~/<x>`, untracked notes beside the repo)
are lost on reboot, invisible to other sessions and drift from the DB. One
`/tmp` wipe on 2026-10-26 destroyed two pipelines' drafts, reviews and
handoffs (gr519361, gr520907). **Rule: before creating a working file, find
its home. When emptying a directory, every file leaves it or is gripe'd.**

## Map each file to its home
## Where does this working file go?

| file is… | home | how |
|---|---|---|
| a spec / design for repo work | a backlog item in the repo, committed in a task worktree and landed; find existing ones with `search(kind='md', q=…)` | open work only; shipped parts become a "shipped as" table pointing at code |
| a ruling log / changelog of decisions | comments on the gripe or todo that owns the work | one comment per ruling set, Reto's words kept; `put(kind='gripe', id=N, text=…)` |
| a red list / review queue | `checklist` on the target ref, or a comment on the finding concerned | a red list derivable from data is a query, not a file |
| run metrics, costs, measured numbers | a gripe/todo comment; `measure` rows when the number is a property of a subject | per-batch tables may ride along in the repo as provenance |
| pre-import drafts (YAML, skeletons) and the scripts that built them | a repo branch, landed through the builder | under `docs/runbooks/<topic>/`; no absolute paths (public repo); ruff-clean |
| a reusable procedure (protocol, agent brief) | `docs/runbooks/<topic>.md`, or a skill when agents run it at runtime | |
| a builder brief / handoff / review | a todo per task, the brief as body, handoffs and reviews as comments | gr519361 |
| research answers (Perplexity) | the `perplexity-research` / `perplexity-reasoning` cache, read through the kind | never dump answers to files |
| a decision with rationale | `memory` (tag `decision`) linked to what it decides | |
| a durable fact with one source chunk | `finding` with `cited_in` | |
| an ad-hoc analysis script for a design | the design's workspace (`precis-workspace-help`) | |
| scratch you will delete within the hour | the worktree's `scratch/` (gitignored, survives reboot) | never `/tmp` |

## No home? Gripe the gap, then the meta-gripe

When a file fits no row above:

1. **Gap gripe:** what the file holds, why each nearby kind doesn't fit
   (name the refusal or missing field), what home is wanted. Link the
   owning gripe. Example: gr520907 (no staging state for unverified draft
   knowledge).
2. **Meta-gripe, every time:** one more gripe linked to the gap gripe and
   to gr520908 ("agents fall back to loose files"), naming the directory
   and how long the files lived there. The count of these is the measure
   of whether homes are closing.
3. Keep the file in the worktree's `scratch/` (not `/tmp`) until the home
   exists, and say so in the gap gripe.

## Stale warning on gap gripes

Reto, 2026-10-26: "we should have a stale warning after a week or 3 if
nobody has picked it up and done it." Precis does not raise this yet
(gr520909). Until it does, whoever files a gap or meta-gripe checks back:
one still untouched (no fix job, todo, branch or status past `triaged`)
after **3 weeks** gets a comment `stale: unpicked since <date>` and a line
on the board or to the coordinator.

## Emptying a directory — the procedure

1. List every file (`find <dir> -type f`), including dotfiles.
2. Check what already shipped: `git log origin/main --grep=<gripe>`, the
   owning gripe's comments. Shipped content is not re-filed; its home is
   the code.
3. Move each file per the map. Prose that becomes comments is rewritten
   short, not pasted; tables may go to the repo as provenance.
4. Gripe + meta-gripe every gap.
5. Delete the directory. Report what is left and why (only files whose
   home is a pending gap gripe, kept in `scratch/`).
