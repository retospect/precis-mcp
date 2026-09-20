---
status: draft
title: autoreviewer — machine critique of papers we are about to lean on, written as paper notes
prio: normal
blocked-by: paper-review-notes
---

# autoreviewer — machine critique of papers we are about to lean on

## Motivation / why

A paper that grounds a `finding` gets leaned on hard: its passages become
evidence, its numbers get quoted into drafts, and its citations get
inherited by our prose. Today nothing systematically asks "is this paper
actually load-bearing?" before that happens. A human reviewer would ask
about stated limitations, sample size, whether the cited support says what
the citing sentence claims it says. We do that ad hoc, per paper, in
whatever session happens to notice.

The paper-review-notes build (see `paper-review-notes.md`) gives the
substrate: a note is a `memory` ref linked to `pa<id>` or a specific
`pc<id>`, surfaced to the agent inline when it reads that chunk. An
autoreviewer is a worker that writes those same notes from the machine
side — so a critique reaches the agent at exactly the moment it reads the
passage, not in a report nobody opens.

## In scope

- A worker pass that, for a selected paper, produces chunk-anchored notes
  in two families:
  - **limitations** — what the paper itself concedes (sample size, scope,
    assumptions, conditions not tested), anchored to the passage that
    concedes it.
  - **citation correctness** — where the paper's own claim outruns the
    support it cites. Precedent for the mechanics is
    `workers/inbound_chase.py` (it already resolves chunk-scoped `cites`
    edges with a verdict); this is the same shape pointed inward.
- Everything this worker writes is a **critique** (`rel='critiques'`), never
  a plain note — both families above bear on whether the paper is safe to
  lean on. The autoreviewer has no business filing "interesting figure".
- Critiques carry an author handle distinguishing them from human ones
  (`autoreviewer`, not a `web_users` abbrev), so the reader can weight them
  differently and a human can rebut one alongside.
- A trigger policy: which papers get reviewed. The obvious one is "papers
  cited by a live finding hub" — the set where being wrong is expensive.

## Explicitly NOT in scope

- Reviewing every paper in the corpus. This is a targeted pass over papers
  we depend on, not a corpus sweep — the cost is per-paper LLM work.
- Any auto-retraction, auto-dispute, or evidence demotion. The autoreviewer
  writes notes; it never mutates a finding's evidence or flips a verdict.
  A human reads the note and decides.
- Replacing the human review tab. Both write into the same note stream.
- Scoring papers with a number. A critique is prose anchored to a passage;
  a quality score invites ranking on something we cannot calibrate.

## Acceptance criteria

- Running the pass on a paper with known stated limitations produces notes
  anchored to the conceding passages, and reading those chunks over the MCP
  surfaces them inline.
- Critiques are attributable to `autoreviewer` and visually/structurally
  distinct from human-authored ones in both the web review tab and the MCP
  sidecar.
- A paper with no notes costs nothing on the read path (no sidecar, no
  extra query beyond the one the notes surfacing already does).
- The pass is budgeted and idempotent — re-running does not duplicate notes
  for the same passage.

## Target + blast radius

New worker under `src/precis/workers/`. Reads chunks + `cites` links;
writes `memory` refs + `critiques` links. Read-path rendering is already
owned by the paper-review-notes build (`handlers/paper.py::_render_chunks`
sidecar) — this item adds a producer, not a second renderer.

## Open questions / decisions log

- Trigger: on finding-hub mint, on demand, or a scheduled pass over the
  cited-by-a-hub set?
- Does citation-correctness checking require fetching the cited paper (an
  acquire), and what happens when it is not in the corpus?
- One note per issue, or one note per chunk bundling several?
- Should a limitation note that a human marks "wrong" feed back into the
  prompt for the next pass, or is that a later distillation item?
