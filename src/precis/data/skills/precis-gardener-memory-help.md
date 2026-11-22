---
id: precis-gardener-memory-help
family: memory
title: precis — review memory flags and proposals
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

The gardener's memory passes surface potential problems in live memories.
Verify the evidence before acting. MCP exposes the proposal queue and
rejection. Applying or undoing a proposal uses operator tools described in
[[precis-gardener-help]].

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

- `memory` — `dangling_mention` (a handle naming a retired ref; the
  consolidation log and its archive are skipped, their handles record
  past actions),
  `thread_shape` (a thread node whose `Left:` is missing or done),
  `anchor_rot` (a cited path/symbol/section/commit no longer resolves
  on main), `compact` (expired, cold-decaying or half-history, or
  holding a pending retire proposal — owes a keep/move/drop plan
  before retirement), `grab_bag` (one memory carrying many
  independent claims — a split candidate), `griped` (open
  `project:memory` complaints, newest first: one candidate per memory
  a `fix: me<N> (<what is wrong>)` line names; a handle mentioned in
  prose mints nothing).
- `memory-review` — proposal-only; nothing applies these:
  `entity_claim_stale` (a state sentence's entity handle or repo anchor
  changed on record since the memory's last touch — use the change
  evidence to check whether the claim still holds), `negative_undated`
  ("no X exists" with no date on that line), `notation` (the first
  authored-prose span breaking [[precis-notation-canon]] — quotes, code
  and math are skipped), `restated_claim` (a sentence repeating a claim
  another memory in the same hub already states — the rewrite points
  at the keeper), `review_due` (an expired shape, or a stale, missing or
  demoted review stamp), `source_drift` (a `Source:` line or cited docs
  section changed since the memory's last touch — a `cites`-edge
  finding pins the link and section address, not a body span),
  `title_shape` (a slug/path title, an over-long title, a dated
  snapshot on a thread node).
- `host-facts` — a one-off manual pass flags current-state claims about
  census machines. Put verified current state on the `machine` ref and
  keep the dated event and machine handle in the memory
  ([[precis-machine-help]]).
- `memory-grounding` — an audit, not a smell pass: pins each claim to
  its cited passages, writes one audit row per memory revision, and
  mints repair proposals for ungrounded claims.

## Answer a proposal

Verify the flagged claim against the memory, its evidence and the repo,
through the checklist of [[precis-fix-memory-gripe-help]] — the same
sections the evaluator read.

- Right → fix the memory yourself with an anchored
  `edit(kind='memory', …)`: state current truth with a live anchor and
  a date, preserving still-true claims. The edit makes the pinned proposal
  stale, and the row can stay pending. Automatic stale closure requires
  an eligible proposal and an enabled consumer. Ask before editing a
  `trust_origin='human'` node.
- Wrong → reject the row:
  `put(kind='job', mode='garden-reject', args={'review_id': <id>,
  'reason': '<why>'})` — decided while the target is unchanged;
  rejecting an `apply_error`/`unresolved` hold instead re-opens the
  key.
- A supersede/retire proposal asks whether still-true claims and needed
  figures already live on the survivor — "preserve them later" is not
  an answer.
- A proposal approved on a `trust_origin='human'` or currently
  `considered` node is held, not applied — the judge already reviewed
  it (a weak one closed there) and the survivor waits in the
  held-proposal digest ([[precis-gardener-help]]); `precis garden
  accept <review_id>` records Reto's ruling and applies it. Nothing is
  applied to such a node without that ruling (gr739890).
- Unsure → file a `memory-review` todo rather than acting on
  suspicion.

## Work a memory-review todo

The evaluator's `ask` on a memory-body candidate files one todo titled
`review memory me<N>: <the detector's finding>`, tagged
`OPEN:memory-review`, linked `raises-concern-about` → the memory — and
to the gripe too when a reader complaint caused it (the complaint's
story is the evidence; work it per [[precis-fix-memory-gripe-help]]).
Read the `Ask:` line and detector evidence in the todo body. Use its
`Detector:` and `Pinned content sha:` lines to identify the finding and
check whether it still describes the current revision. Older todos can
have only a title. The evaluator's reason is also recorded
on the closed ledger row. A grounding re-audit that changed a claim's
coverage queues review of the drafts and quests that cite the memory.

Work it per [[precis-memory-help]] "Working a memory-review todo":
verify the finding against the current revision — the repo and cited
evidence, repairing through the ledger or an anchored edit — then
close in this order: check `view='links'` for a prior closed review on
the same memory, record the `kg-review` stamp on the verified SHA
(the trust edit below), comment what changed, and mark the todo
`STATUS:done`. Closing without the stamp leaves the review
unrecorded. Unsettled goes to `waiting-for:reto`
([[precis-user-queue-help]]). An open review todo suppresses repeat
asks on the same memory. A done review suppresses a repeat for the
same memory, detector and content SHA. For a complaint, any review todo linked to both the memory and
the complaint already answers the ask.

## Close a review

The stamp step in the todo sequence is a manual trust edit on the
current revision:

```python
edit(kind='memory', id='me<N>', mode='trust', args={'trust': {
    'confidence': 'considered',
    'reason': 'kg-review:<model>:<YYYY-MM-DD>:ok',
    'expected_sha': '<sha from the trust line>'}})
```

`ok` = checked, nothing changed; `fixed` = you edited first. The
date records when you reviewed the revision in UTC. A content edit or
demotion invalidates a current stamp. The stamp records your judgement
and may leave the detector reporting `version_bumped` until the
evaluator stamps the revision. Mechanics and the plain
`considered` act: [[precis-edit-help]] "Record a trust act".

For `review_due` the todo is also the door for an accepted change: the
detector has no apply door, so no proposal row is written. To clear
stamp-related `review_due` debt, the evaluator must return `no-change`
on the current revision under its current review policy. That answer
records a versioned `kg-review:<model>:<date>:ok` stamp.

If the flag reports expiry, preserve still-needed claims before proposing
retirement, or renew the expiry after verifying that the memory is still
current. The expiry flag persists until you address the expiry.

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
