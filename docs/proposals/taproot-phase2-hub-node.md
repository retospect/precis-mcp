---
status: draft
title: Taproot Phase 2 — finding-as-hub node + typed graded evidence relation
model: opus
---

# Taproot Phase 2 — the hub-node foundation

Build ticket for Phase 2 of `docs/proposals/taproot.md` (read that for the
*why*; this is the *how*). Phase 1 (the flat canonicalizer,
`src/precis/taproot/canon.py`) shipped and passed its gate (over-merge = 0 /
238, 2026-07-29). Phase 2 is **"the schema/vocab foundation the rest writes
to"**: promote `finding` to the claim **hub node**, hang a typed graded
evidence relation off it, render it, dedup its cards, and expand
`\cite{}`→originators at export.

Phase 2 is **not one ship** — it is five dependency-ordered slices. This
ticket locks the decomposition and the design decisions; each slice is its
own worktree cycle.

## Slices (dependency order)

| # | Slice | Gist | Depends on |
|---|---|---|---|
| **2a** | **FROLE classifier** | tag each `finding` `FROLE:claim` vs `FROLE:review` (open #11) | — |
| **2b** | **Evidence vocab + write-path** | `establishes`/`corroborates`/`contradicts` relations + the single taproot write door (`hub.py`) | 2a |
| 2c | Evidence view | `finding` `view='evidence'`: edges by derived role, support/integrity/caveats, originators marked (acceptance #1); seniority derivation from `links` (acceptance #4) | 2b |
| 2d | Citation-card dedup | stop double-counting citation cards vs the hub card in ANN (open #3 residual) | 2b |
| 2e | Cite→originators export | `precis resolve` expands a claim-hub `\cite` to its current `establishes` paper(s) (open #4 residual) | 2b |

**2a and 2b are this ticket's build target;** 2c/2d/2e are scoped here for
follow-on sessions.

## Why 2a must come first (open #11)

Many prod `finding` rows are editorial review notes ("acronym unexpanded"),
not grounded world-claims. The hub + evidence edges attach **only** to
grounded claims, so a discriminator must exist before `finding` becomes the
hub. Decision (repo-idiomatic, open-tag pattern): keep one `finding` kind,
add a **closed discriminator tag** `FROLE:claim` vs `FROLE:review`.
`canon.block()` already queries `FROLE:claim` hubs — so this slice
immediately makes live canonicalization real.

## Slice 2a — FROLE classifier

Reuses the generic `data/axes/<id>.yaml` runner (`workers/axis_pass.py`):
`cli/worker.py` auto-registers every axis with an `id:` as a default-OFF
`axis:<id>` service via `discover_axis_ids()`. So 2a is **declarative — no
worker code**.

- **`src/precis/data/axes/frole.yaml`** — `id: frole`, `version: 1`,
  `level: ref`, `applies_to_kinds: ["finding"]`, `values: [claim, review]`,
  `cost_tier: small`, a `prompt:` + worked `examples:`. **Omit
  `default_unknown`** so a parse/OOV result is `failed` (re-claimable) rather
  than a mis-tag — fail-open, matching taproot's over-merge-safe posture.
  Reads title + `finding_body` (ord=0) via `axis_pass._build_ref_prompt`
  (`_abstract` falls back to the first `ord>=0` chunk).
- **`src/precis/store/types.py`** — add
  `"FROLE": frozenset({"claim", "review"})` to `_CLOSED_VOCAB`. Do **not**
  add `finding` to `_KIND_ALLOWED_AXES` — it is deliberately unlisted
  (unrestricted); listing it would strip its other axes (AUDIT, …).
- **Tests** (`tests/test_taproot_frole_axis.py`, offline): `run_axis_pass`
  with a fake dispatch over synthetic `finding` refs in the test DB; assert
  the `FROLE:claim`/`FROLE:review` ref-tag + `FROLECASCADE:1` marker are
  written, and a parse failure writes neither. Plus a `_CLOSED_VOCAB` test:
  `FROLE:claim` parses, `FROLE:bogus` rejected.

Running the pass corpus-wide is a deliberate batch later (opt-in via
`PRECIS_AXES_ENABLED` / `/categorizers`), not part of the ship. LLM prompt
quality is validated by a host-native spot-check, not the offline gate
(live-model tests must run host-native).

## Slice 2b — evidence-relation vocab + single write-path

- **Forward migration** (`00NN_taproot_evidence_relations.sql`, mint the
  number via the `scaffold` agent) — seed 4 `relations` rows:
  `establishes`↔`established-by`, `corroborates`↔`corroborated-by`.
  `contradicts`↔`contradicted-by` already exists. Model on
  `0080_argument_graph_relations.sql`; `ON CONFLICT (slug) DO NOTHING`.
  **This corrects `taproot.md`** — its "no migration for the vocab" claim is
  wrong: `links.relation` has an FK to `relations(slug)`
  (`0001_initial.sql:1386`), so new slugs must be seeded.
- **`src/precis/store/types.py`** — add the 4 slugs to the `Relation` Literal
  (static typo-safety hint; kept in sync with the seed).
- **`src/precis/taproot/hub.py`** (new — the single write door):
  - `mint_hub(store, claim, *, set_by="agent") -> int` — create a `finding`
    (claim sentence → title, `claim.scope` → `meta.scope`) + tag `FROLE:claim`.
  - `attach_evidence(store, *, hub_ref_id, paper_ref_id, role, meta)` —
    validate `role` via `validate_relation`, guard `hub_ref_id` is a
    `FROLE:claim` finding, then `add_link(src=paper, dst=hub, relation=role)`.
  - `apply_placement(store, claim, placement, *, paper_ref_id, meta, todo_fn)`
    — bridge `canon.place()`: `attach`→`attach_evidence`;
    `new`/`new_contradicts`→`mint_hub` (+ hub↔hub `contradicts` for the
    latter); `needs_review`→`todo_fn` (never attach).
- **Governance ADR** (`docs/decisions/00NN-taproot-evidence-relations.md`, via
  `scaffold`) — the relation vocab + the single-write-path rule.
- **Tests** (`tests/test_taproot_hub.py`, offline, test DB): mint creates a
  `FROLE:claim` finding; attach writes each role, rejects an unknown role and
  a non-claim target; inverse auto-mirrors on `links_for`; `apply_placement`
  routes every `Placement.action` (fake `todo_fn` for `needs_review`).

## Locked design decisions

1. **Edge direction = paper → hub.** `paper --establishes/corroborates/
   contradicts--> finding`. Matches the existing `supports` = "Source
   provides evidence for target"; `establishes`/`corroborates` refine it into
   originator vs non-originator. The hub renders evidence via the
   auto-mirrored **inbound** inverses (`established-by`/`corroborated-by`/
   `contradicted-by`).
2. **`contradicts` has two endpoint uses** — hub↔hub (opposite *claims*, from
   `canon.place()`) and paper→hub (evidence contradicting the claim). Same
   slug.
3. **Hubs are findings, paper-sourced only** (open #15) — draft-novel claims
   stay draft-local, never minted.
4. **Single write path** (open #16) — all hub/edge writes go through
   `hub.py`; a raw `INSERT`/`add_link` for these relations is a defect.
5. **`needs_review` files a `kind='todo'`** (open #16), never auto-attaches.
6. **Edge `meta` shape** (defined now, *populated by chase in Phase 3*):
   `{support: "yes"|"partial"|"no", support_reason, caveats: [...],
   char_offset, source_handle}`. Integrity keys deferred to Phase 4.

## Explicitly NOT in Phase 2

- **Running `chase` live** to populate edges — that is Phase 3. 2b defines +
  unit-tests the write API; real edges come later.
- **Integrity axis** (retraction reason-relevance) — Phase 4.
- **Corpus backfill** — Phase 5.
- **Claim hierarchy** (broader/narrower) — v2.
- **2c/2d/2e** are scoped above but shipped separately.

## Acceptance (2a + 2b)

- `frole.yaml` classifies synthetic findings claim/review in tests; `FROLE`
  parses as a closed axis; corpus batch is opt-in and default-OFF.
- The migration seeds the 4 relation slugs (`store.valid_relations()`
  contains them); `hub.py` mints a `FROLE:claim` hub and attaches all three
  evidence roles (rejecting bad role / non-claim target), with the inverse
  visible on read and `apply_placement` routing every branch.
- Green container gate (ruff + mypy + `pytest -n6`) on each ship.
