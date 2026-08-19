---
status: draft
title: scope keys need a frequency-ordered registry, not a hardcoded 7-key list
---

# The vocabulary was invented, not measured

`refs.meta->'scope'` qualifies a claim hub's applicability and participates in
hub identity (`pub_id = hash(sentence, scope)`;
`nanopub-corpus-remediation.md` Phase 3/"Why dedup never fired"). The
controlled key vocabulary shipped as a fixed 7-key set —
`precis/taproot/sentence_lint.py::SCOPE_KEYS` (`material, method, regime,
system, quantity, substrate, temperature`) — chosen without consulting usage.

Measured 2026-08-19 over 1,524 live claim hubs (`lint_scope`):

| in vocabulary | n | | out of vocabulary | n |
|---|---|---|---|---|
| material | 265 | | catalyst | 52 |
| method | 205 | | device | 27 |
| quantity | 81 | | oxidant | 27 |
| substrate | 35 | | metal | 23 |
| regime | 31 | | property | 23 |
| temperature | 8 | | phase | 18 |
| system | 7 | | functional | 16 |
| | | | mode | 15 |
| | | | target | 14 |
| | | | solvent | 14 |
| | | | matrix | 13 |
| | | | quantity_bound | 12 |
| | | | draft_chunk | 10 |
| | | | metric | 8 |
| | | | solvation | 7 |
| | | | context | 6 |
| | | | domain | 6 |
| | | | +long tail | |

191 hubs lint `scope-unknown-key`. `catalyst` alone (52) outnumbers four of
the seven approved keys. Most of the 191 are sensible domain keys the fixed
list failed to anticipate, not sloppiness — forcing `catalyst`/`solvent`/
`oxidant` into `material` destroys information the hub actually carries.

## The design: a frequency-ordered registry, presented at write time

A table an agent can insert into freely just relocates the free-for-all —
that is exactly how 191 unknown keys arose against a *documented* 7-key list.
What fixes it is presenting the vocabulary **ordered by usage count** at the
point a scope key is chosen, so the canonical key is also the most visible
one.

Ordering by frequency buys three things beyond storage:

- **Cheapest correct choice.** The head of the list is what everyone has
  already converged on; picking it is less effort than inventing a new key.
- **Auto-promotion, no spec change.** When a corpus of bio claims arrives and
  `organism` crosses some threshold, it rises to the head of the list on its
  own. No migration, no re-editing `SCOPE_KEYS`.
- **The tail is the typo list, for free.** A key used once sits at the
  bottom forever, visible and greppable (`matrial`, one-off keyboard slips).
  No separate lint rule is needed to surface junk — low count *is* the
  signal.

## What frequency alone cannot catch: near-synonym forks

If `support` and `substrate` both reach comparable counts, frequency ranks
both healthy and the corpus has forked permanently — the same claim shape
now mints under two keys depending on which agent got there first. This is
the argument for a registry table over a longer hardcoded list: it needs an
`alias_of` column, curated occasionally (by a human or a reviewing agent),
with the write path rewriting the alias to its canonical key on the way in.
Frequency finds typos; `alias_of` fixes forks.

A one-line `gloss` per row is worth having for the same reason: `substrate`
vs `matrix` vs `support` gets adjudicated once, in the row, instead of
re-litigated per hub by whichever agent is minting.

Sketch of the columns (not DDL — a design sketch only):

- `key` — the canonical scope key text.
- `count` — usage count, the ordering key at write time.
- `alias_of` — nullable; when set, `key` is a retired synonym and writes
  should be rewritten to the target key.
- `gloss` — one line, when to use this key vs. its near-synonyms.

## Where it lives

`precis/draft/registry.py` already generalizes three registries — the
abbreviation glossary, the patent drawings/parts registry, and a
manufacturing components/BOM table — as one family: named
`chunk_kind='term'` leaves discriminated by `meta.registry`
(`REGISTRY_POLICY`), separated by exactly two axes, content richness and
callout numbering policy (`NumberingPolicy`, `registry.py::policy_for`).

A scope-key vocabulary is the **glossary** shape: name plus gloss, no
callout, `assign="none"`. It is not a `parts`- or `components`-shaped
registry — no numbering, no attribute bag beyond the gloss.

**The one honest mismatch, and the open design question.** The three
existing registries are per-draft leaves hanging off a draft ref — a BOM
belongs to *this* build's draft, a drawings registry to *this* patent's
draft. A scope-key vocabulary is corpus-global: every claim hub across every
project shares one vocabulary. It needs a singleton home, not a draft
parent. Whether that's a dedicated ref kind, a fixed sentinel draft ref that
every scope-key row hangs off of, or a table outside the `term`-leaf family
entirely is not decided here — flagging it as the question to settle before
implementation starts.

## The constraint that bites: scope is inside the identity hash

`pub_id` is derived from the claim sentence *and* scope together
(`nanopub-corpus-remediation.md`). Rewriting a hub's `catalyst_material` key
to the canonical `catalyst` therefore changes that hub's `pub_id`.

That rewrite is free right now: per the same doc's reframing, every live hub
is `candidate` and zero are `published`, so there is no frozen `claim_sha`
and no signed artifact to invalidate. After the first publication, the same
rewrite becomes a supersede event — a new `pub_id`, a new artifact, and a
provenance link back to the retired one.

Two consequences:

1. **The alias/normalization pass must run before re-approval**, in the same
   repair window as notation normalization — cross-reference the
   drift-ordering constraint in `nanopub-corpus-remediation.md` (Phase 3.1:
   repair while `candidate`, re-approve only after). Normalizing a scope key
   under an already-frozen `claim_sha` re-triggers gate #14
   (`nanopub/gates.py::check_drift`) the same way title repairs did.
2. **After publication, the registry is append-and-alias-forward only.** A
   key that appears in a published, signed artifact is never remapped in
   place — only aliased forward for *future* writes. The published artifact
   keeps its original key; only new hubs converge on the canonical spelling.

## Why this doesn't balloon: growth is in values, not keys

Scope keys answer a small, bounded set of questions per domain — what stuff,
how measured, under what conditions, what was measured, where it applies.
Chemistry, the worked example here, turned 7 invented keys into roughly 18
real ones once measured. A new domain adds a bounded handful once and then
goes flat: bio would plausibly add something like `organism, cell_line,
tissue, assay, dose, strain, buffer, ph` — on the order of 10-15 keys, not
an open-ended stream.

What is genuinely unbounded is a scope *value* — `organism = E. coli K-12`
— and that was always unbounded, always free text, and stays that way. The
registry governs keys, never values.

## Two defects to lift out of scope-key space, not fold into the vocabulary

- **`quantity_bound` (12) and `draft_chunk` (10) are not scope descriptors —
  they are machine fields that leaked into `scope` because it was the only
  writable dict in reach.** `quantity_bound` is already validated
  structurally against `QUANTITY_BOUNDS`
  (`precis/nanopub/vocab.py::QUANTITY_BOUNDS`, checked in
  `precis/nanopub/gates.py`). Counting either as vocabulary usage pollutes
  the frequency ordering the whole registry design depends on being
  trustworthy. They need a separate home (a distinct `meta` key, not
  `scope`), not a slot in this registry.
- **`scope-free-text` (156 hubs) is the defect that actually matters, and
  the registry must not soften it.** Because scope is inside the identity
  hash, a sentence fragment stuffed into a scope *value* manufactures
  spurious non-convergence: two of the three byte-identical title pairs in
  the corpus differ only by prose drift inside scope
  (`nanopub-corpus-remediation.md`, "Why dedup never fired" #2). This
  registry loosens **keys** (generous, frequency-driven, growable); it must
  not loosen **values** (short, enumerable, still flagged by
  `sentence_lint.py::lint_scope`'s free-text detector). Widening the key
  vocabulary is not license to widen what counts as a valid value.

## Work

1. Settle the open design question above — singleton home for a
   corpus-global registry vs. the existing per-draft `term`-leaf shape.
2. Design the registry table (key, count, alias_of, gloss) and the write
   path that surfaces it frequency-ordered — likely a `put(kind='finding',
   scope=…)`-adjacent lookup, not a new verb.
3. Lift `quantity_bound` and `draft_chunk` out of `scope` into their own
   `meta` field; re-derive the out-of-vocabulary counts above once that's
   done, since they're currently inflating the "needs a registry key" tally.
4. Seed the registry from the measured 2026-08-19 counts (in-vocabulary +
   out-of-vocabulary keys above), curate `alias_of` for the near-synonym
   forks (`support`/`substrate`, `catalyst`/`catalyst_material`, etc. —
   sample the full out-of-vocabulary list, not just the head, before
   deciding aliases).
5. Extend `sentence_lint.py::lint_scope` to check keys against the registry
   instead of the fixed `SCOPE_KEYS` set; keep the free-text value detector
   unchanged.
6. Run the key-normalization pass over the whole `candidate` cohort — same
   repair window as notation normalization, strictly before any
   re-approval, per the drift-ordering constraint above.
7. Retire `SCOPE_KEYS` as a hardcoded constant once the registry is live.
