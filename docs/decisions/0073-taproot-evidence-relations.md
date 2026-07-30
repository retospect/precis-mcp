# 0072 — Taproot evidence-edge relations + the single hub write-path

- **Status**: accepted (2026-07-29) · **built + verified** (this commit;
  migration `0094_taproot_evidence_relations.sql`, `src/precis/taproot/hub.py`).
  Phase 2 (slice 2b) of `docs/proposals/taproot.md`; build ticket
  `docs/proposals/taproot-phase2-hub-node.md`. The proposal stays `draft`
  (the shared model + decisions log across all five phases); this ADR is the
  durable record for the *relation vocabulary* specifically.
- **Deciders**: Reto + agent

## Context

Taproot promotes `finding` to the **claim hub** and hangs a typed, graded
evidence relation off it: many papers attach to one claim as
`establishes` / `corroborates` / `contradicts` edges (taproot.md §"The core
model"). Phase 2 must register that vocabulary and a write door for it.

Two facts about the substrate constrained the design:

1. **`links.relation` has an FK to `relations(slug)`** (`links_relation_fkey`,
   `0001_initial.sql`). So a new relation is **not** just a `Relation`-Literal
   edit — it needs a forward migration seeding the `relations` row, or the
   insert hits the FK. This **corrects `taproot.md`**, whose "Target + blast
   radius" section claimed the vocab needs "no migration."
2. **Two of the three roles already exist.** `contradicts`↔`contradicted-by`
   (0001) and `corroborates` (0085, integration-disposition, seeded with **no
   inverse**) are already registered. Only `establishes` is genuinely new.

## Decision

**One new relation slug.** Migration `0094` seeds **`establishes`**
(paper → claim hub; the originator that first showed the claim), with **no
inverse**. The other two roles **reuse** existing slugs:

- `corroborates` (0085) — the NOT-ORIGINATOR supporters.
- `contradicts` (0001) — evidence against a claim, *and* hub↔hub
  opposite-claim links.

Endpoint kinds disambiguate a shared slug (a `paper → finding` edge vs
0085's `chunk → point` disposition; a `paper → hub` vs `hub → hub`
contradiction) — the same overload this design already accepts for
`contradicts`.

**No inverse for `establishes` (and none added to `corroborates`).** The hub
reads its evidence via `links_for(hub, direction='in', relation=<role>)` —
direction filtering, not auto-mirroring — so an inverse slug is unnecessary.
This matches the asymmetric-no-inverse convention 0085 chose and, crucially,
**avoids perturbing the shared `corroborates`**: adding an inverse to it would
change read-time mirroring for the draft-integration subsystem that owns it.

**Single write path (taproot.md open #16).** Every hub-finding and every
evidence edge is written through **`src/precis/taproot/hub.py`**:

- `mint_hub` — create a `TAPROOT:claim` `finding` (reuse, not a new kind —
  ADR 0054 precedent). Only paper-grounded claims become hubs (open #15).
- `attach_evidence` — write one `paper --role--> hub` edge; `role` must be one
  of the three evidence roles **and** validate against the live `relations`
  table, and the target must be a `TAPROOT:claim` finding (never a `TAPROOT:review`
  note or a non-finding).
- `apply_placement` — route a canonicalizer `Placement`: `attach` / `new` /
  `new_contradicts` write edges; **`needs_review` files a `kind='todo'` and
  attaches nothing** (a risky merge is never auto-applied, open #16).

A raw `INSERT` / `store.add_link` for these relations elsewhere bypasses the
role + `TAPROOT:claim` guards and is a defect. The FK is the durable backstop;
`validate_relation` is the friendly pre-flight.

**Evidence role is derived, not set at write time.** `establishes` vs
`corroborates` is a derivation over the citation graph (taproot.md §"Seniority
is derived", Phase 2c/3). `apply_placement` attaches with a conservative
default (`corroborates` — never falsely claim originator); promotion is later.

## Consequences

- Forward-only, idempotent migration (`ON CONFLICT DO NOTHING`); the baseline
  snapshot is regenerated at release time, never hand-edited (ADR 0005).
- The edge `meta` shape (`support` / `support_reason` / `caveats` /
  `char_offset` / `source_handle`) is defined now but **populated at scale by
  the forward `chase` wiring in Phase 3** — Phase 2 unit-tests the write API.
- Integrity keys on the edge (retraction reason-relevance) are Phase 4.

## Alternatives rejected

- **Mint a new `claim` kind** — rejected; `finding` is the hub (ADR 0054
  precedent of not minting a kind, taproot.md §"The core model").
- **Give `corroborates`/`establishes` inverses** — rejected; reads use
  direction filtering, and adding an inverse to the shared `corroborates`
  would perturb the draft-integration subsystem.
- **A distinct `corroborated-by`/taproot-only corroborator slug** — rejected as
  needless vocabulary; endpoint kinds already disambiguate the shared slug.
