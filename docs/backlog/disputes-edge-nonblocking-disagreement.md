---
status: ready
title: "split `contradicts` into non-blocking `disputes` + adjudicated `contradicts` — make disagreement free to file"
prio: high
model: opus
---

# The corpus cannot disagree with itself — Part 1: vocabulary + write path

This file is Part 1 only (ship alone). Part 2 (adjudication workflow —
the claim-about-two-claims tier, verdicts, reviewer persona) moved to
`docs/backlog/disputes-adjudication-workflow.md`, blocked by this.
Downstream consumer: `docs/backlog/claim-conflict-search.md` (blocked by
this file; its worker files the edges this vocabulary creates). Sibling:
`docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` — its
blocking defect is cured by this item (decisions D2/D3 below); its
residual scope stays open there.

Keep the vocabulary split regardless of volume: one slug (`contradicts`)
currently means at least four unrelated relationships —
evidence-contradicts-claim, claim-contradicts-paper,
critique-contradicts-claim, memory-contradicts-memory — and the case for
separating them rests on that naming collision, not on row count. It holds
at zero hub↔hub rows; don't reopen it on "but there's no data."

**Census (prod, 2026-08-20; all 6 `contradicts` rows, directional — no
reciprocal or self-loop rows):**

| direction | n | the relationship it encodes |
|---|---|---|
| `finding` → `paper` | 2 | a finding contradicts a paper |
| `paper` → `finding` | 1 | a paper's result contradicts a claim |
| `finding` → `finding` | 1 | a **review note** contradicts a hub |
| `memory` → `memory` | 2 | unrelated subsystem |
| hub ↔ hub | **0** | *what the relation was designed for* |

**Stale — re-measure at build (work item 0).** The sibling
`contradicts-conflates` audit (2026-08-29) implies at least 3
`finding`→`finding` review-critique rows (fi255164→fi191315,
fi192706→fi191316, fi255165→fi191329), not 1 — the drift is visible, not
hypothetical. (One count in this heading was wrong twice before the
census — verify against a fresh read-only query, never a remembered
number.)

The zero is not 1,524 acts of self-censorship — no agent chooses to file a
hub↔hub `contradicts`; it is written automatically by
`taproot/hub.py::_mint_for_placement`'s `new_contradicts` branch whenever
`taproot/canon.py::place()` gets a `"contradicts"` verdict from an
unreviewed MEDIUM-tier LLM call during ingest. `place()` can only judge two
claims as contradicting if `block()` retrieved one as a candidate for the
other, and `block()` retrieved over `card_combined` — a chunk kind only
187/1,524 hubs (12.3%) carried an embedding for, against 1,524/1,524 for
`finding_body`. The judge was blind to 88% of the corpus and was almost
never asked a question it could answer. (Fixed separately — this file's
numbers post-date the fix.)

**Schema note — RESOLVED.** `links` now carries a unique index on
`(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)`
(`links_endpoints_relation_idx`, NULLS NOT DISTINCT; shipped 278624b6).
Automated passes still need to upsert/skip on conflict rather than error,
but the double-every-edge hazard is closed.

**Confirmation asymmetry — partially fixed 2026-08-20.** `place()` now
filters `"contradicts"` on `confidence >= confidence_threshold`; a
sub-threshold verdict mints the hub **unlinked** (reason on
`Placement.reason` only). **Not fixed:** no `merge-confirm`-equivalent
second call exists for a high-confidence contradiction — the threshold
filters, it doesn't confirm. Under this spec that asymmetry stops
mattering at the gate (the verdict files a non-blocking `disputes`), and a
`contradiction_confirm` BIG-tier call becomes Part 2's adjudication
concern, not a write-path patch.

Every gate today is an admissibility gate — well-formed, sourced,
traceable. Admissible is not true. Claim-versus-claim disagreement is the
only truth-bearing mechanism in the system, and it has never actually run.
An external review (`get(kind='perplexity-research',
id='critique-the-design-of-a-scientific-claim-publication-pipeli')`)
points the same direction — the corpus is impeccably traced but
epistemically flat (paraphrase of its themes, not a verbatim quote).
Re-confirmed by the 2026-08-27 staged-queue review: finding-level
critiques must surface as a nonblocking review/dispute state — visible in
the evidence view, not silently absent, not a hard gate — which is exactly
the `disputes` rendering below.

## The change

Split one relation into two, along who has decided:

| relation | who files it | blocks publication? | means |
|---|---|---|---|
| `disputes` | anyone, freely — agent, human, or an LLM judge | **no** | "these two claims appear to conflict; someone should look" |
| `contradicts` | adjudication only (Part 2) | **yes** | "these do conflict, and it has been established" |

A `disputes` edge is a **question**, not a verdict, and must render as one
everywhere it appears — no demerit against either hub. Filing is free;
only *resolution* is expensive. `contradicts` becomes derived, not
authored: post-migration, a live claim-graph `contradicts` edge can only
come from a Part 2 adjudication, which is a far better warrant for
refusing publication than an anonymous unreviewed LLM row.

## Decisions (2026-09-02 — the former open questions, resolved)

- **D1 — gate reads `contradicts` only, unfiltered by source kind.**
  Post-split, adjudication is the warrant, so `check_contradicts` blocks
  on any live claim-graph `contradicts` edge regardless of `src` kind —
  the `EVIDENCE_SRC_KINDS` filter stops being a blocking-policy knob.
  `disputes` never blocks, from any source. The three read surfaces
  reconcile to two definitions used consistently: **blocking** = live
  `contradicts`; **open question** = live `disputes` (rendered by the
  claim page, `view='nanopub'`, and the overview's bucket — never the red
  UNMINTABLE banner).
- **D2 — the migration repoints existing claim-graph `contradicts` rows
  to `disputes`.** None was ever adjudicated (no adjudication mechanism
  has existed), so none carries the warrant the new semantics assign to
  the slug. `memory`↔`memory` rows are left untouched (different
  subsystem; never read by these surfaces). Claim-graph `contradicts`
  therefore starts at zero — correct, not a regression — and the three
  review-critique blocks (fi191315/fi191316/fi191329) become visible
  open questions, curing the sibling item's live defect.
- **D3 — repoint every writer, not just `place()`.** Item 1a covers the
  ingest dedup judge; the review-lens writer that produced the 3 live
  problem rows (likely `quest/review_fanout.py` — "find the writer first"
  per the sibling item; confirm at build) must also emit `disputes`: a
  review critique is a question about a claim, not adjudicated evidence
  conflict. After Part 1, **no code path writes `contradicts`.**
- **D4 — `link_claims` is the door.** Add `disputes` to
  `taproot/hub.py::link_claims`'s `CLAIM_LINK_RELATIONS`; the generic MCP
  `link()` door delegates a `disputes` op between two claim-hub findings
  to it. The genuinely missing guard at the generic door is only the
  live-claim-hub-kind check — `store/_links_ops.py::add_link` already
  rejects self-loops and is idempotent, and `parse_link_target` already
  requires a live ref — so this is a small delegation, not a guard
  rebuild. Manual `contradicts` stays unfileable through every door.
- **D5 — boundary with `contradicts-conflates-evidence-and-prose-misuse`.**
  Its gate-level question and this file's are one decision, made here
  (D1/D2). Its deeper ask — a dedicated cite-misuse relation scoped to
  the (draft-chunk, hub) pair, fixing the scope inversion — stays open in
  that item and is *not* foreclosed: a review-sourced `disputes` edge is
  an interim superset that a later `misused-by` relation can re-target.

## Scope of work (Part 1 — ship this alone)

0. **Measure first.** Fresh read-only census of `contradicts` rows (count,
   directions, writers). If sharply different from the table above, revise
   before building.
1. **Relation vocabulary** — add `disputes`: `relations` table row +
   `links_relation_fkey` + the `Relation` Literal in
   `src/precis/store/types.py`, in lockstep, one migration (pattern:
   `migrations/0100_taproot_refines_relation.sql`; check current max
   number first). The same migration performs D2's repoint of existing
   claim-graph `contradicts` rows.
1a. **Repoint the ingest judge** — `taproot/canon.py::place()`'s
   `"contradicts"` branch and `taproot/hub.py`'s `new_contradicts`
   placement action emit `disputes`.
1b. **Repoint the review-lens writer** (D3) — find it (start at
   `quest/review_fanout.py`), make it emit `disputes`.
2. **Reconcile the three read surfaces to D1** —
   `nanopub/gates.py::check_contradicts` (blocks on live `contradicts`,
   any source kind), `precis_web/nanopub_render.py` and
   `nanopub/overview.py` (both show `disputes` as a non-blocking open
   question, any source kind, counterpart linked).
3. **The write door** (D4) — `link_claims` gains `disputes`; generic
   `link()` delegates claim-pair `disputes` there; add the claim-hub-kind
   check.
4. **Render** — `disputes` gets its own visibly non-blocking treatment
   ("open question", counterpart hub linked), never the red banner.

## First run

Success looks like the `disputes` count growing into the hundreds — a
large `disputes` graph is the deliverable, not a regression: it's the map
of where inquiry should go. Systematic filing at scale is
`claim-conflict-search`'s worker (dense neighbourhoods first; its specced
retrieval is ANN-based and its recall is a floor, never an estimate — see
that file). Two seed cases already in hand that no automated gate caught:

- fi191120 vs fi218681 — possible genuine contradiction
- pa1992 — GPa/TPa unit error, off by ~10³

## Related

- `docs/backlog/disputes-adjudication-workflow.md` — Part 2, blocked by
  this
- `docs/backlog/claim-conflict-search.md` — the systematic filer, blocked
  by this
- `docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` —
  residual cite-misuse scope (D5)
- `docs/backlog/claim-review-mechanism.md` — the procedure this plugs into
- `docs/backlog/nanopub-corpus-remediation.md` Phase 4 — the original
  observation

## Open questions / decisions log

- 2026-09-02: ready-gate drift check re-verified every Part-1 code
  citation against the current tree (all held; only the `links`
  uniqueness note had rotted, fixed). Its three blockers — gate
  visibility, write door, sibling-item reconciliation — resolved as
  D1–D5 above; its advisories (guard-gap overstatement, quote-as-
  paraphrase, stale census) folded into the text. Flipped to `ready`.
- Near-neighbour dedup lead (the 2026-08-20 measurement: ≤0.10-cosine
  band dominated by unconverged duplicates, e.g. `fi191179`/`fi191260`,
  `fi191192`/`fi191262`) is real but is **not** Part 1 work — it needs no
  new vocabulary; it belongs to the dedup/merge-door thread
  (`docs/backlog/claim-hub-dedup-sweep.md`).
