---
status: draft
title: hypothesis cites — derived sigil + falsification prose, nothing stored in text
prio: normal
---

# Hypothesis cites render as hypotheses, everywhere, without storing a marker

## Motivation / why

A hypothesis hub is cited in prose as `[fi<id>]` — byte-identical to an
evidence-backed claim. A reader (human or agent) cannot tell them apart, so a
conjecture reads as established support. This has already happened twice in
live drafts: `fi262681` is cited from dr43051 (Nox Ammonia Paper) and
`fi262870` from dr43037, both with relation `cites`.

Hypothesis-ness is not a defect to be fixed by grounding — it is the artifact
type's defining property. `nanopub/gates.py::run_mint_gates` *rejects* a
hypothesis arriving with grounding passages ("a hypothesis has no supporting
passage by definition — motivation, not evidence") and demands `testable_by`
plus ≥2 motivators across ≥2 papers. So the fix is presentational: make the
epistemic status visible at every render site, and show the falsification
terms the type already carries.

**Rejected alternative: a `hy<id>` handle prefix.** Writing `hy` into prose
stores `artifact_type` in the text — it goes stale the moment a hypothesis is
refuted (`STATUS:refuted`, shipped) or promoted, and repairing it means a
prose rewrite across every citing draft. It also fights
`docs/backlog/retire-fi-go-nanopub.md` (decided 2026-08-16) by adding a third
citation surface mid-migration, requires threading `artifact_type` through
~20 emit sites (`code_for_kind` is single-valued and `format_handle(kind, id)`
never sees the row), and diverges from `nanopub/assemble.py`, which bakes
`ref/fi<id>` into signed TriG bytes.

**The stored handle stays a dumb pointer. Status is read from the DB at
render time** — the same living-cite discipline `STATUS:refuted` already uses.

## In scope

### 1. Derived hypothesis sigil at cite sites

`precis_web/linkify.py::_render_claim_hub` already takes three DB-derived
maps — `claims`, `pending_claims`, `refuted_claims` — and picks the anchor at
render time. Add a fourth, `hypothesis_claims`, populated from
`refs.meta.artifact_type == 'hypothesis'` in the same window query that builds
the others.

Precedence (refuted must keep winning): **refuted → hypothesis → canonical →
pending**. A refuted hypothesis is red, not hypothesis-marked; the
do-not-repropose signal outranks the epistemic one.

Give it its own sigil + anchor class beside `_CLAIM_SIGIL` /
`_CLAIM_PENDING_SIGIL` / `_CLAIM_REFUTED_SIGIL`, `title="hypothesis — motivation, not evidence"`,
href to the claim page. Smartdraft's `data-claim-head` sync should behave as
for canonical hubs (a hypothesis hub *is* a hub).

### 2. Falsification prose wherever a hypothesis renders

`meta.proposed_payload.{motivation, testable_by}` is currently inert: no
template reads it, no agent renderer reads it. It surfaces only as a raw
`json.dumps` in the `/claim` review textarea
(`precis_web/nanopub_render.py::_suggested_payload`) and at sign time in
`nanopub/assemble.py`. Surface it as structured prose at three sites:

- **Agent-facing finding output** — extend the existing seam in
  `handlers/_finding_evidence.py` (already prints "no supporters — this is a
  hypothesis, which carries motivation instead of evidence by definition" plus
  `_motivation_section`). Add the falsification terms from `testable_by`.
- **Draft fisheye Claims block** — `utils/refeye.py`. `_mine_claim_hub_ids`
  already mines `[fi<id>]`/`[pub_id]` cites and gates on
  `is_claim_hub(store, ref_id)`; mark hypothesis hubs in `_claim_block` and
  render their falsification line in place of the evidence lines they cannot
  have.
- **Web `/claim` page** — real fields, not the JSON dump.

Wording to standardise (agent-facing):

```
fi262718 [finding·HYPOTHESIS] A cubic molecular cage that houses...
  motivation:    two established results are bound here without demonstration...
  falsified by:  switching suppressed in the lattice, or loss of long-range
                 order upon switching.
```

### 3. Draft-lint counter — NOT BUILT, and the premise was wrong

**Correction (2026-08-27):** this section claimed `handlers/_draft_lint.py`
"already counts `[fi<id>]` handles in live drafts for the fi→np migration
criterion." It does not. That file knows about `[fi<id>]` only in order to
*never flag it* ("a finding has no…", `_draft_lint.py` docstring ~:71). The
prose-coverage counter is listed as planned-but-unbuilt work in
`docs/backlog/retire-fi-go-nanopub.md`, and I mistook the plan for the code.

So the intent stands but the sizing was wrong: a hypothesis-cite count is
still the one thing render-layer marking cannot do (raw markdown read outside
precis shows a plain `[fi<id>]`), but building it means inventing the whole
prose-coverage counter — where the count lives (return value / meta stamp /
CLI report), what counts as "live" — which is an architecture decision owned
by `retire-fi-go-nanopub.md`, not a small extension of existing code.

**Deferred to that item.** Items 1 and 2 shipped without it.

## Explicitly NOT in scope

- Any new handle prefix or citation grammar (see rejected alternative).
- Changing `nanopub/assemble.py`'s `ref/fi<id>` URI — signed bytes stay put.
- Rewriting existing draft prose. Marking is derived; no migration needed.
- Removing the two existing hypothesis cites from dr43051 / dr43037 — those
  are the owning sessions' authorial calls, surfaced by the new lint counter.

## Verification

- Unit: `_render_claim_hub` precedence — a hub that is both refuted and a
  hypothesis renders red; a live hypothesis renders the new sigil; a
  non-hypothesis hub is unchanged. Extend `tests/precis_web/test_linkify.py`.
- Unit: fisheye Claims block renders falsification for a hypothesis hub and
  is byte-identical to today for a normal hub (`tests/test_refeye.py`).
- Unit: `_finding_evidence` output includes falsification terms
  (`tests/test_finding_hypothesis_put.py` neighbours).
- End-to-end on the dev DB (`scripts/dev` — never the session MCP): mint a
  hypothesis, cite it from a draft chunk, confirm the fisheye and `/claim`
  render, then stamp `STATUS:refuted` and confirm red wins.
- Real data available for eyeballing: fi262718 (uncited), fi262681 (cited from
  dr43051), fi262870 (cited from dr43037).
