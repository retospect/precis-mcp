---
status: draft
title: taproot sole/derivative-supporter coverage signal
prio: normal
---

# taproot sole/derivative-supporter coverage signal

From gr307372 (narrowed out of gr178763). Spec drafted 2026-09-09; awaiting
Reto's review before build.

## Motivation / why

A hub whose ONLY attached source is a derivative paper (restating the claim
as cited background) looks exactly as healthy as one grounded in the
originating work. Seniority is computed only by walking `cites` edges
*among the hub's own attached supporters* (`derive_evidence` /
`derive_evidence_bulk`), so a set-of-one can never contain an intra-set
cite edge — the sole supporter always resolves to corroborator, and the
muted generic `coverage_note` ("seniority undetermined") fires identically
whether that supporter is the true originator or a downstream restater.
Live example on the gripe: tbx2hd's sole source was a 2025 circuit paper
restating E_g~1/W while the actual originator (Son/Cohen/Louie 2006) sat
un-attached in the same corpus.

## In scope

- A coverage-signal computation (in `workers/hub_refine.py` or a new
  `taproot/coverage.py`) that flags exactly-one-supporter hubs whose
  supporter is plausibly derivative: the supporter's own outbound `cites`
  edges (or corpus search by the claim's anchor terms) reach an in-corpus
  paper that predates it and is not attached to the hub.
- Surfaced as a distinct, higher-visibility nudge in `view='evidence'` and
  the fisheye rendering — not the existing muted `coverage_note`.
- Advisory only: names the candidate originating paper when one is found,
  so the operator (or hub_refine) can attach it.

## Explicitly NOT in scope

- Auto-attaching the candidate originator (hub_refine may act on the
  signal later; this item only computes and renders it).
- Any change to trust scoring (`taproot/trust.py`) or gate behavior.
- Multi-supporter seniority improvements — this is the N=1 blind spot only.

## Acceptance criteria

- A hub with exactly one supporter that cites an older in-corpus
  un-attached paper on the same claim surface shows the sole-supporter
  nudge in `view='evidence'`, naming the candidate originator.
- A hub whose sole supporter has no older in-corpus candidate shows a
  sole-supporter note without a candidate (still distinct from the generic
  coverage_note).
- Multi-supporter hubs are unaffected (no new signal, no perf regression
  in `derive_evidence_bulk`).
- tbx2hd-shaped fixture reproduces the gripe's scenario in a test.

## Target + blast radius

`taproot/hub.py` evidence derivation, `workers/hub_refine.py`,
`view='evidence'` rendering, fisheye renderer. Read-path only; no schema
change expected.

## Open questions / decisions log

- Candidate-originator search: cites-edge walk only, or also semantic
  search by claim anchor? (cites-only is cheap and precise; search widens
  recall but risks noise — proposal: cites-only first slice.)
- Where does the signal persist — computed on read, or stamped into hub
  meta by hub_refine's pass? (proposal: computed on read first; stamp only
  if it proves expensive.)
