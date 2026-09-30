---
status: ready
title: relation constraints as data — domain/range kinds, functional, transitive, acyclic on the relations table, checked once at the link door
pillar: memory-graph
prio: normal
model: sonnet
---

# relation constraints as data — one validator at the link door

## Motivation / why

The relation registry is rich (81 slugs, FK-enforced, symmetric/inverse
declared) but carries no constraint: `relations` has `slug, is_symmetric,
inverse_slug, description, deprecated_at` and nothing else, and
`description` is never read at runtime. Every endpoint rule that exists is
a hand-written guard on one relation in one place:
`_link_tag_ops.py::guard_and_route_contradicts_disputes` (two slugs),
`taproot/hub.py::attach_evidence` / `link_claims` (evidence roles and
claim links), `handlers/cad.py::link` (an allowlist of four). The at-most-one
rule for `draft-of` / `plan-of` / `dossier-of` lives only in
`store/_draft_ops.py` and is bypassed by the generic `link()` verb. Quest
`serves` and concept `has-prerequisite` are called DAGs and have no cycle
guard at all; `contains` has its own BFS in `store/_component_ops.py`.
The comment at `_link_tag_ops.py` on the contradicts guard says a second
copy of that guard is how the gap it closes was introduced — the absence of
one door has already cost a defect.

The tag side already has the shape this item wants: `Tag.parse_strict` is
one choke point that refuses unknown axes, unknown values and axes a kind
may not carry, driven by `_CLOSED_VOCAB` and `_KIND_ALLOWED_AXES` as data.
Relations get the same treatment. The framing (constraints as data on the
relation: domain and range classes, functional properties, transitive
properties; validated at the ledger, not in prose) is the transferable
point of Coyle's talk on ontologies for agentic systems (youtube:458504).

## In scope

1. **Migration (forward-only).** `ALTER TABLE relations ADD COLUMN
   domain_kinds text[] NULL, range_kinds text[] NULL, functional boolean
   NOT NULL DEFAULT false, transitive boolean NOT NULL DEFAULT false,
   acyclic boolean NOT NULL DEFAULT false`. NULL kinds = unconstrained.
   Seed only rows with a rule already documented or enforced somewhere:
   - `contradicts`: domain and range `{memory}` (today's hand guard).
   - `draft-of`, `plan-of`, `dossier-of`: `functional` (the 1:1 family,
     `reading/cast_common.py`).
   - `establishes`, `corroborates`: domain = the evidence kinds
     (`taproot/hub.py::EVIDENCE_SRC_KINDS` ∪ `PATHWAY_EVIDENCE_KINDS`),
     range `{finding}`. The taproot door keeps its stricter live-claim-hub
     checks; the row is the superset.
   - `specialises`, `contains`, `has-prerequisite`, `serves`: `transitive`
     + `acyclic`. `instance-of` (term-taxonomy): range `{taxon}` — lands
     with that item's migration as its first consumer, not here.
2. **One validator.** `handlers/_link_tag_ops.py::check_relation_constraints
   (store, rel, src, dst)` runs in both link doors (`apply_link_ops` and
   `NumericRefHandler.link`) after `parse_link_target`, using the existing
   `_endpoint_kinds` helper. Domain/range violation → `BadInput` naming the
   relation, the offending end and the allowed kinds. `functional`: a
   second live target from the same source → `BadInput` naming the existing
   target. `acyclic`: a generic ancestor walk (recursive CTE over the
   relation and its inverse, depth-capped) refuses a cycle. `transitive` is
   a declaration this item only stores and documents (used by the closure
   helper in 3).
3. **One closure helper.** `store/_links_ops.py::ancestors(relation,
   ref_id, max_depth)` — the recursive CTE the acyclic check uses, exposed
   so `component_would_cycle` delegates to it and later readers (taxon
   `view='path'`, quest reweight) do not each roll their own.
4. **Registry read.** `Store.valid_relations()` grows a sibling
   `relation_constraints()` (one cached read per store, invalidated like
   `valid_relations`). The hand guard for `contradicts` is deleted;
   `guard_and_route_contradicts_disputes` keeps only the `disputes`
   routing.
5. **Runtime doc.** `precis-relations` gains four columns (domain, range,
   functional, acyclic) rendered from the seeded rows, and one paragraph:
   the error an agent sees and what to do (pick the right endpoint, or
   `mode='remove'` the old functional target first).

## Explicitly NOT in scope

- Materialised transitive closure (term-taxonomy's standing rejection;
  revisit trigger recorded there).
- Inferring a ref's kind from a relation (RDFS-style domain inference):
  kinds are explicit columns, never derived.
- Disjointness between kinds: already true by the `kind` column.
- A hierarchy over relations (sub-relations); `meta.axis` → taxon in
  term-taxonomy v1.5 is the answer to hierarchical link tags.
- Retrofitting all 81 relations. Only rows with a documented rule are
  seeded; adding a constraint later is one `UPDATE relations` migration.
- Repairing existing violating rows. The validator gates new writes; a
  one-shot probe (`precis doctor`-style read) lists existing violators for
  a human to judge.
- The taproot doors' live-hub and support-verdict checks stay where they
  are; this item never weakens them.

## Acceptance criteria

1. After the migration, `SELECT slug FROM relations WHERE domain_kinds IS
   NOT NULL OR functional OR acyclic` returns exactly the seeded rows above,
   and `tests/test_kind_totality.py` pins every kind named in any
   `domain_kinds`/`range_kinds` to a registered kind.
2. `link(kind='memory', id=N, rel='contradicts', target='paper:P')` raises
   `BadInput` naming `range_kinds` = memory, through both doors
   (`put(link=)` and `link()`); the deleted hand guard's tests pass
   unchanged against the new path.
3. A second `draft-of` from the same project ref through the generic
   `link()` verb raises `BadInput` naming the existing draft; the
   `_draft_ops` `ValueError` path is either deleted or delegates.
4. `link(kind='quest', id=A, rel='serves', target='quest:B')` when B
   already (transitively) serves A raises `BadInput` "cycle"; same for
   `has-prerequisite` between two concepts and `contains` between two
   components (`component_would_cycle` delegating to the shared helper,
   its existing tests green).
5. `get(kind='skill', id='precis-relations')` shows the constraint
   columns for the seeded rows and no prose-only endpoint hints remain
   for those rows.
6. A link on a relation with no constraint row behaves exactly as before
   (a snapshot test over one unconstrained relation per door).

## Target + blast radius

- `src/precis/migrations/<next>_relation_constraints.sql`
- `src/precis/store/_links_ops.py` (constraint read, ancestors helper),
  `src/precis/store/_component_ops.py` (delegation)
- `src/precis/handlers/_link_tag_ops.py` (validator, guard trimmed),
  `src/precis/handlers/_numeric_ref.py::link` (call site)
- `src/precis/store/_draft_ops.py` (functional delegation)
- `src/precis/data/skills/precis-relations.md`
- tests: `tests/test_link_tag_ops.py` (or the existing home of the
  contradicts guard tests), `tests/test_kind_totality.py`, quest/concept/
  component cycle tests.

## Open questions / decisions log

- **[decided 2026-09-30]** Constraints live on the `relations` table, not
  in `KindSpec`: a relation's rule is the same whichever handler writes it,
  and the plugin relations (`precis_chem`, `precis_bio`) get the same door
  by seeding their own rows.
- **[decided 2026-09-30]** `transitive` is stored and documented but drives
  no inference in v1; the only consumer is the shared ancestors helper via
  `acyclic`. A reasoner over declared transitivity is a later item.
- **[open, non-blocking]** Whether `functional` should accept
  `mode='replace'` (repoint) instead of forcing remove-then-add. Default:
  refuse; revisit on the first agent gripe.
