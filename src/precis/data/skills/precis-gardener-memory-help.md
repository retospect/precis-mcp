---
id: precis-gardener-memory-help
family: memory
title: precis — the graph gardener's memory detectors
summary: what a memory flag, pending proposal or memory-review todo means, which detectors file them, what to do when you meet one, and how to close a review
answers:
  - which memory detectors does the gardener run?
  - what does a garden proposal on a memory mean?
  - what do I do with a memory-review todo?
  - how do I close a memory review or clear review debt?
  - what does the AUDIT:notation tag on a memory mean?
  - what does the grounding line on a memory read mean?
  - how do I fix a memory flagged AUDIT:notation?
applies-to: get/search (kind='memory'/'job'), memory-review todos
tags: [workflow]
kinds: [memory, job]
status: active
---

# precis-gardener-memory-help — memory flags and reviews

The gardener's memory passes file proposals and review todos over live
memories. A proposal is pending evidence, not truth; none of it applies
itself and there is no MCP apply or undo — each flag waits on a reader's
edit, a review stamp or a human apply. Proposals, the pending queue and
the reject door: [[precis-gardener-help]]. Every memory detector reads
DREAM memories as duplicate/merge-only and never proposes a rewrite for
them.

## Where you meet a flag

- A pending proposal — `get(kind='job', id='/garden/proposals')` lists
  the queue; `get(kind='memory', id='me<N>', view='smells')` groups one
  memory's open proposals plus the last 30 days of decisions by
  detector, with evidence ([[precis-memory-help]] "Read garden
  proposals"). `not_checked` entries in that evidence record what the
  detector could not verify — a limit, never proof of a fault.
- An `OPEN:memory-review` todo linked `raises-concern-about` → a memory
  — the evaluator's `ask`, a reader complaint needing judgement, or a
  dependency re-audit.
- An `AUDIT:notation` tag on the memory itself — write-path lint caught
  a canon break; `search(kind='memory', tags=['AUDIT:notation'])`
  lists them.
- The `grounding:` line in the `get(kind='memory')` footer and the
  fisheye header — the audit's per-claim coverage, or
  `grounding: not audited`.

## Which detectors run

The memory passes are opt-in: a `graph_garden` job must name the pass —
`all` skips them and nothing schedules one by itself. Names here are
the detector each proposal's evidence and the `view='smells'` grouping
carry.

- `memory` — `dangling_mention` (a handle naming a retired ref),
  `thread_shape` (a thread node whose `Left:` is missing or done),
  `anchor_rot` (a cited path/symbol/section/commit no longer resolves
  on main), `compact` (an expired or stale node owes a keep/move/drop
  plan before retirement), `grab_bag` (one memory carrying many
  independent claims — a split candidate), `griped` (open
  `project:memory` complaints, newest first).
- `memory-review` — proposal-only; nothing applies these:
  `entity_claim_stale` (a state sentence about an entity or machine no
  longer matches its recorded state), `negative_undated` ("no X exists"
  with no date on that line), `notation` (the first authored-prose span
  breaking [[precis-notation-canon]] — quotes, code and math are
  skipped), `restated_claim` (a sentence repeating a claim another
  memory in the same hub already states — the rewrite points at the
  keeper), `review_due` (an expired shape, or a stale, missing or
  demoted review stamp), `source_drift` (a `Source:` line or cited docs
  section changed since the memory's last touch), `title_shape` (a
  slug/path title, an over-long title, a dated snapshot on a thread
  node).
- `host-facts` — a one-off hand-run: a sentence asserting a census
  machine's current state belongs on its `machine` ref as a dated
  event ([[precis-machine-help]]).
- `memory-grounding` — an audit, not a smell pass: pins each claim to
  its cited passages, writes one audit row per memory revision, and
  mints repair proposals for ungrounded claims.

## Answer a proposal

Verify the flagged claim against the memory, its evidence and the repo,
through the checklist of [[precis-fix-memory-gripe-help]] — the same
sections the evaluator read.

- Right → fix the memory yourself with an anchored
  `edit(kind='memory', …)`: state current truth with a live anchor and
  a date, preserving still-true claims. The edit moves the target's
  SHA, so the pinned proposal goes stale and closes by itself. A
  `trust_origin='human'` node is a hold: ask, don't edit.
- Wrong → reject the row:
  `put(kind='job', mode='garden-reject', args={'review_id': <id>,
  'reason': '<why>'})` — decided while the target is unchanged;
  rejecting an `apply_error`/`unresolved` hold instead re-opens the
  key.
- A supersede/retire proposal asks whether still-true claims and needed
  figures already live on the survivor — "preserve them later" is not
  an answer.
- Unsure → file a `memory-review` todo rather than acting on
  suspicion.

## Work a memory-review todo

The evaluator's `ask` on a memory-body candidate files one todo titled
`review memory me<N>: <the detector's finding>`, tagged
`OPEN:memory-review`, linked `raises-concern-about` → the memory — and
to the gripe too when a reader complaint caused it (the complaint's
story is the evidence; work it per [[precis-fix-memory-gripe-help]]).
The evaluator's stated reason is recorded on the ask's closed ledger
row, not on the todo. A grounding re-audit that changed a claim's
coverage mints a `memory-review` todo on each dependent draft/quest.

Work it per [[precis-memory-help]] "Working a memory-review todo":
verify against the repo and cited evidence, repair through the ledger
or an anchored edit, comment what changed and close `STATUS:done`;
unsettled goes to `waiting-for:reto` ([[precis-user-queue-help]]). An
open review todo dedupes a repeat ask on the same memory; for a
complaint, any review todo — closed included — already answers it.

## Close a review

A `review_due` flag — and review debt generally — clears on a current
review stamp, not on the edit alone:

```python
edit(kind='memory', id='me<N>', mode='trust', args={'trust': {
    'confidence': 'considered',
    'reason': 'kg-review:<model>:<YYYY-MM-DD>:ok|fixed',
    'expected_sha': '<sha from the trust line>'}})
```

`ok` = checked, nothing changed; `fixed` = you edited first. The
stamp's UTC date decides freshness (a backdated review stays old); a
content edit or a demote re-opens the debt. Mechanics and the plain
`considered` act: [[precis-edit-help]] "Record a trust act".

An `AUDIT:notation` tag clears differently: fix the flagged span with
an anchored `edit` — the write path re-lints the body and a clean
result drops the tag. Keep quoted spans, code spans and `$$…$$` math
byte-identical; when the canon does not fit the text, file a gripe
instead of forcing it.

## The grounding line

The footer reads `grounding: <n> claims - <s> supported - <u>
unresolved - <c> unchecked - audited <date> - current|stale`, or
`grounding: not audited`; a bounded scan renders `<n>+` plus
`truncated scan`. `stale` means the memory, a cited source or the audit
itself moved since the pinned row — nothing is applied and no trust
state changes. `view='grounding'` is the drill-down: per-claim status,
evidence and premise handles, stale pins, unchecked spans and the
audit history.

Unsupported or contradicted claims mint repair proposals into the same
pending queue; unresolved ones mint bounded `grounding-handoff`
research todos — return the named check as a todo comment starting
`candidate-evidence:` with one `Checks performed:` line per completed
check, then close the todo; the return prompts re-audit of the same
revision. Term, identity and authority questions mint `ask-user` todos
on the owner's queue. Suggested findings stay provisional — an audit
may remain unresolved.
