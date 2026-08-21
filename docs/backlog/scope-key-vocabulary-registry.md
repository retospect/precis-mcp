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

> **The 2026-08-19 table below used the contaminated predicate**
> (`TAPROOT:claim` alone, which sweeps in 280 chase-tree findings that never
> mint). Re-measured 2026-08-21 against the strict population
> (`+ STATUS:canonical`, **n=1249**), keys with ≥6 uses:
>
> | key | n | | key | n | | key | n |
> |---|---|---|---|---|---|---|---|
> | material | 232 | | oxidant | 27 | | solvent | 14 |
> | method | 206 | | device | 25 | | quantity_bound | 12 |
> | quantity | 79 | | property | 23 | | system | 8 |
> | catalyst | 52 | | mode | 14 | | temperature | 8 |
> | substrate | 35 | | regime | 30 | | process | 6 |
>
> `catalyst` (52) still outranks four of the seven approved keys, so the
> core argument holds. But **seed and curate from the strict numbers, not
> the ones below** — and note `draft_chunk` drops to 0.

Measured 2026-08-19 over 1,524 live claim hubs (`lint_scope`), permissive
predicate — superseded by the strict table above:

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
parent.

### Settled 2026-08-21 (Reto): claim hubs are not draft-scoped

> "The nanopubs … can be used in any draft, and will be published … the
> nanopubs are ultimately published separately and can be shared between
> any drafts."

A draft is a *view* onto hubs, never their owner. Two consequences:

1. **The sentinel-draft option is dead.** Parenting the registry to a fixed
   draft ref would make a draft load-bearing for a vocabulary that outlives
   every draft — the same shape defect as the acquisition marker (a durable
   state parked where a legitimate writer destroys it). Remaining choice is
   a dedicated corpus-global ref kind (preferred — `refs.meta` is jsonb, so
   no migration) or a table outside the `term`-leaf family.
2. **The per-draft view is still required** (Reto, same exchange: *"I still
   want to have the option to see per draft"*). Draft-independence is about
   *ownership and identity*, not visibility — "which hubs does this draft
   use?" must stay answerable. Serve it with a **draft→hub edge**, not a
   scope key: a hub is used by *many* drafts, so a scalar inside `scope`
   could only ever name one of them, and would change `pub_id` per draft.
   Drafts already cite hubs this way (`cites` edges from the drafting
   document — see `claim-hub-dedup-sweep.md`), so the substrate exists;
   confirm coverage before building anything new.

**Drop `count` from the stored columns.** The frequency table is derivable
(`GROUP BY` over `refs.meta->'scope'` across live hubs); storing it
materializes an aggregate with an update path someone will forget, and it
goes stale silently — precisely how the invented 7-key list drifted from
reality. Store only what is *curated*: `key`, `alias_of`, `gloss`. A key
that needs neither an alias nor a gloss needs no row. The registry is a
small curated overlay on measured usage, not a mirror of the vocabulary:
no seeding pass, no registry/corpus drift, ordering true by construction.

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

  **`draft_chunk`: retracted as a claim-hub defect, 2026-08-21.** It was
  briefly raised here as a `pub_id` blocker on the strength of the "10"
  above. Re-measured against the **strict** population
  (`TAPROOT:claim` + `STATUS:canonical`, `n=1249`): **`draft_chunk` appears
  on 0 hubs.** All 10 live on the 280 non-hub rows the contaminated
  predicate swept in (chase-tree findings — see
  `claim-hub-definition-divergence.md`), which never mint and have no
  `pub_id`. Nothing to lift on the minting path. `quantity_bound` (12) *is*
  real on strict hubs and still needs its own `meta` home.

  The general principle survives the retraction and is worth keeping: a key
  inside `scope` is inside `pub_id`, so anything naming a *local, mutable
  drafting artifact* must never appear there. Guard against it re-entering
  rather than repairing it now.
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

1. ~~Settle the open design question above.~~ **Done 2026-08-21** —
   corpus-global, not draft-parented; sentinel-draft ruled out; `count`
   derived rather than stored. Remaining sub-choice: dedicated ref kind
   (preferred) vs. a table outside the `term`-leaf family.
2. Design the curated overlay (`key`, `alias_of`, `gloss` — **no `count`**)
   and the write path that surfaces it ordered by *derived* frequency —
   likely a `put(kind='finding', scope=…)`-adjacent lookup, not a new verb.
3. Lift `quantity_bound` (12) out of `scope` into its own `meta` field and
   re-derive `pub_id` for those hubs. **`draft_chunk` needs no repair** — 0
   on strict hubs (retraction recorded above); just keep it out.
3b. Verify the draft→hub `cites` edge covers the per-draft view before
   assuming it does, then expose "hubs used by draft X" off that edge.
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
