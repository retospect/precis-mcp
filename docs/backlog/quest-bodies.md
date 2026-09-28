---
status: draft
prio: normal
---

# Quest bodies

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Restart the funding quest (qu401863) once the tick can serve a non-materials striving

_Grouped 2026-09-26; was `quest-401863-restart`, status draft, prio normal, blocked-by quest-bodies-inquiry._

`qu401863` ("A standing flow of money for open, independent research, so
the work never has to be sold") was set `STATUS:dormant` on 2026-09-25 by
Reto's decision. Parked, not renounced — the striving stands; the tick
body cannot serve it yet.

### Why it was parked

Minted 2026-09-21 active + high, the loop ran 4 ticks / 40 logbook
entries / $0.53 with **zero deeds**, cycling lit-search-finds-nothing →
agent-declines-to-propose → cost. Two causes, both now specced:

- the proposal leg only dispatches a proposal carrying an atomistic
  `structure`, so a funding proposal can never become an action and the
  deed count is structurally zero, not merely low
  (`quest-bodies-inquiry.md`);
- the force-acquire fallback appended catalysis facets to the quest's
  title, so it searched for "A standing flow of money for open,
  independent research … DFT barrier mechanism" and linked nine unrelated
  papers as servers (`quest-tick-incident-fix.md`).

Background: `gr447337`, and the decision entry on the quest's own logbook.

### Restart checklist

1. `quest-tick-incident-fix.md` has shipped and deployed — in particular
   the meta write path, without which step 3 needs hand-written prod SQL.
2. `quest-bodies-inquiry.md` has shipped and deployed.
3. Set `meta.quest_body = 'inquiry'` on `qu401863` via the new write path.
4. Confirm on a dry-run (`precis quest tick 401863 --dry-run`) that the
   assembled prompt carries none of "measured barriers", "awaiting a sim",
   "candidate materials to simulate", or the `structure` JSON example, and
   that the fallback query contains no catalysis facet.
5. `tag(kind='quest', id=401863, add=['STATUS:active'])`.
6. Watch the first three ticks. The failure to look for is the inquiry
   body's open question — a tick logging `milestone` entries for having
   thought about something. If deeds climb without an artifact existing,
   park it again and fix the deed definition first.

### Note on what is NOT blocked

The two serving todos are human work and stay open regardless:
`td401864` (independent-research funding pipeline — sweep calls, ingest
each as `kind='cfp'`, spin fits into proposal projects per
`precis-proposal-help`) and `td401865` (university-routed grant path —
which programs, which named academic partner could be PI).

Deleting this file is the ship: when the quest is active again and has
logged one honest deed, fold nothing anywhere — `git log` and the quest's
own logbook carry it.

## Quest evidence parity — simulation as a first-class evidence route

_Grouped 2026-09-26; was `quest-simulation-evidence-parity`, status draft._

Reto, 2026-09-08: *"it is ok to be not found in literature if we can show
something is working"* — and, choosing the scope: **"full evidence parity once
we learn; we have for example ml potential that provides feedback in the other
quest."**

### The defect

`src/precis/quest/tick.py` gives the tick model exactly **one** way to acquire
evidence — `searches`, i.e. the literature:

> *"Progress means new external evidence, not more restating. … When the answer
> lies in the literature you don't yet hold … emit `searches` to go get it
> instead of hypothesising in a vacuum."*

There is no other affordance in the prompt. So "not in the literature" reads as
a **dead end by construction**, and the only sanctioned move is to search again.

This is observable, not theoretical. Quest `qu330435` (photonic assembly arm)
entry 7 proposed a molecular charge-coupled-device analogue and logged it as
*"an analogy drawn here, not a literature finding. No molecular CCD has been
verified to exist"* — then queued another search. The idea was sound; the prompt
had no vocabulary for "so simulate it and see".

### What already exists

* **`sandbox_run`** (ADR-0048) is a registered job type with a params schema,
  executed by `claude_docker`. It is the substrate. Nothing in a quest tick can
  request it.
* **The catalysis/frontier lane already does this properly** — an ML potential
  computes structure relaxations that feed back into the quest's frontier, and
  `precis.quest.graduate` encodes the epistemic boundary in its own docstring:
  *"A simulation is not the world"*, graduating a candidate that crosses a bar
  to a `needs-experiment` gap for a human/lab.

**Model the design on that lane. Do not invent a parallel mechanism.** It is a
working instance of computed evidence feeding a quest, with the
simulation-vs-world boundary already drawn. Read it first.

### Consequence of the gap

Because the tick could not ask, `qu330435` entry 10's Monte Carlo was written
out-of-band to `~/precis-experiments/photonic-arm/mc_protocol.py` on one
operator's laptop: not a ref, not citable by handle, not re-runnable, invisible
to the dialectic, and lost if that machine goes away.

### The epistemics to encode (the model already got this right unprompted)

Entry 10 framed itself as *"deliberately NOT a yield prediction — p_bit for a
photochemically clocked molecular register is unmeasured, so the sim sweeps it
and returns the REQUIREMENT instead, turning the missing number into a spec on
the chemistry rather than a blocker."*

That is the behaviour worth making explicit: **a simulation over an unmeasured
parameter returns a SPEC, not a result.** The prompt should ask for that
framing, not merely grant permission to simulate. It converts a blocker into a
falsifiable requirement on the physical system.

### Scope: full parity

1. **Prompt** — rewrite the evidence paragraph so computed evidence counts as
   progress and absence from the literature is not a terminal state; require the
   spec-not-prediction framing.
2. **Affordance** — a `simulations` output field beside `searches`, dispatched
   as `sandbox_run`, whose result lands as a `result` logbook entry plus a
   stored artifact addressable by handle.
3. **Dialectic parity** — a simulation may `support`/`counter` a hypothesis with
   its own handle class and an explicitly **weaker epistemic tier** than a
   trusted measurement, extending `graduate.py`'s boundary into the dossier so a
   reader can never mistake a simulated support for a measured one.

### Blockers / notes

* `sandbox_run` slice 1 is live but **blocked on `gr329258`** (vault
  `CLAUDE_CODE_OAUTH_TOKEN` 401), so step 2 is buildable but not
  smoke-testable end-to-end until that is re-minted.
* Step 3 is the largest change to a load-bearing research prompt in the repo —
  `tick.py` is ~3k lines and drives every quest. Stage it behind 1 and 2.
