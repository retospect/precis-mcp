---
status: draft
title: Taproot atom re-grounding — per-atom paper grounding before apply (no source, no atom)
model: opus
---

# Atom re-grounding pass — T1b prerequisite

Binding review feedback (Reto 2026-08-15, on the dry-run-49 dossier;
canonical list: `claim-publication-nanopub-ots.md` §"Review feedback
2026-08-15"). Atoms are currently extracted from the hub's claim sentence
alone; the dossier caught extractor content present in **no shown source**
(fi34985's ~0.1 eV benchmark, fi176551's "4 binding domains" — "Can't just
make stuff up"). Rule: **no source, no atom** — every atom must be
re-grounded against actual paper text, with its own quote + locator snip,
before placement. The nanopub mint gate (sibling session,
`claim-publication-nanopub-ots.md` Layer A) re-validates the same record at
mint; this pass *produces* it. Atoms whose only support is the summarizing
fi sentence are not placeable.

## Stage: between extraction verdict and apply

New pass over each `split`-verdict hub, before `apply_migrate` placement:

1. **Collect candidate source papers** — both provenance shapes (see
   `apply_migrate` module docstring): inbound evidence edges
   (`establishes`/`corroborates`/`contradicts`, src kind in
   EVIDENCE_SRC_KINDS) and outbound `derived-from` lineage.
2. **Candidate passages** per atom × paper: body chunks (`ord >= 0`,
   `retired_at IS NULL`), ranked by embedding similarity + content-word
   overlap with the atom. **Exclude hearsay sections** — `section_path`
   matching references / related-work / prior-art / background /
   bibliography (regex per `make_dossier.py`'s `_HEARSAY_SECTION`;
   mint-side SQL — there is no query-time section filter, 0118 dropped the
   index).
3. **Verify + quote** (LLM, same haiku lane as extraction): does the
   passage assert the atom's content? If yes, emit the minimal supporting
   quote — which must string-match the chunk text after the same
   normalization the gates use (notation folding), and be unique within
   the paper (the mint gate's uniqueness check; fail here, not at mint).
4. **Output** per atom: `{paper_ref_id, chunk_id, quote, snip}` (possibly
   several — an atom may be supported by more than one paper) or
   `UNGROUNDED`.

## Apply integration

- **Grounded atom** — placed; its evidence edge is written add-first with
  the chunk anchor, and the quote/snip stored where the nanopub spec
  expects grounding records (coordinate with the nanopub-infra session
  before picking the storage spot; do not invent a second home).
- **UNGROUNDED atom, hub has papers** — withheld from placement →
  `needs_review` (likely extractor invention). Never silently dropped:
  counters in ApplyReport + per-hub reasons in the run artifact.
- **UNGROUNDED atom, hub itself hanging (no papers at all)** — atoms may
  be placed *hanging* (lineage-only, no evidence edges); publish preflight
  blocks them downstream. Hanging is a legitimate parked state while the
  doer-paper hunt runs.
- **Re-point targeting** — when the hub's grounding block sits in a
  hearsay section (7/49 in the dry run), re-grounding will correctly find
  no non-hearsay passage in that paper; the atom goes needs_review with a
  `hearsay-only` reason so the doer-paper hunt can pick it up.

## Extraction-contract changes riding along (`taproot/canon.py`)

- **Bound semantics**: scope quantity values carry
  `bound: exact | upper | lower | approx` — the verify step reads the
  bound off the quote, not the fi sentence ("is 4 exact, an upper or a
  lower bound?").
- **Causal qualifiers stay in the claim**: fi34850's atom is "blocked for
  digital logic *because of zero bandgap*", never the blanket version —
  the cause may be engineered away. Gate: a mechanism clause in the
  original must survive in some atom (extends P1-6/P2-13's
  modality/mechanism rules).

## What this is not

- Not the nanopub mint gate — that session owns Layer A validation; this
  pass produces the records the gate consumes.
- Not `reground_claim` (`taproot-reground-add-first-invariant.md`) — that
  repairs evidence edges on *existing* hubs. Shared invariant, though:
  adds are read-back-confirmed before any prune, and the add-first logic
  should be one code path, not two.

## Cost shape

49 dry-run splits ≈ 150 atoms; full run ~1.3k hubs → order 4–5k
atom-verify calls on the haiku lane (~$0.05/call from the extraction
fixture) — bound the passage-candidate count per atom (top-k) and batch
per hub (one call verifying all atoms against one paper's top passages)
before scaling up.

test: an atom whose content is unsupported by any candidate paper passage
is withheld with a reason, not placed; a grounded atom's placement writes
the evidence edge with the chunk anchor and a quote that string-matches
the chunk text; a hub whose only grounding sits in a references/prior-art
section yields `hearsay-only` needs_review atoms, never an evidence edge
into that section.
