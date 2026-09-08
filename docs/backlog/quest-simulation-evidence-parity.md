---
status: draft
title: Quest evidence parity — simulation as a first-class evidence route
---

# Quest evidence parity — simulation as a first-class evidence route

Reto, 2026-09-08: *"it is ok to be not found in literature if we can show
something is working"* — and, choosing the scope: **"full evidence parity once
we learn; we have for example ml potential that provides feedback in the other
quest."**

## The defect

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

## What already exists

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

## Consequence of the gap

Because the tick could not ask, `qu330435` entry 10's Monte Carlo was written
out-of-band to `~/precis-experiments/photonic-arm/mc_protocol.py` on one
operator's laptop: not a ref, not citable by handle, not re-runnable, invisible
to the dialectic, and lost if that machine goes away.

## The epistemics to encode (the model already got this right unprompted)

Entry 10 framed itself as *"deliberately NOT a yield prediction — p_bit for a
photochemically clocked molecular register is unmeasured, so the sim sweeps it
and returns the REQUIREMENT instead, turning the missing number into a spec on
the chemistry rather than a blocker."*

That is the behaviour worth making explicit: **a simulation over an unmeasured
parameter returns a SPEC, not a result.** The prompt should ask for that
framing, not merely grant permission to simulate. It converts a blocker into a
falsifiable requirement on the physical system.

## Scope: full parity

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

## Blockers / notes

* `sandbox_run` slice 1 is live but **blocked on `gr329258`** (vault
  `CLAUDE_CODE_OAUTH_TOKEN` 401), so step 2 is buildable but not
  smoke-testable end-to-end until that is re-minted.
* Step 3 is the largest change to a load-bearing research prompt in the repo —
  `tick.py` is ~3k lines and drives every quest. Stage it behind 1 and 2.
