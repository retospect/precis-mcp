---
status: built
title: Acquisition-mode findings — claim-first mint with automated paper chase and verified grounding
model: opus
---

> **Built** (2026-08-04): shipped as specced — `STATUS:acquiring` vocab,
> migration `0105` (`awaits-evidence`), the acquisition put branch
> (`_put_acquiring`), chase claim-query widening + acquiring arm +
> give-up (`PRECIS_ACQUIRE_GRACE_DAYS`), planner lit-hunt template
> rewrite, skill docs, tests per the 9 ACs. One implementation note:
> grounding uses a deterministic lexical fallback when no embedder is
> configured (the chase pass keeps its conservative embedder gating —
> an unconditional bge-m3 load on ordinary passes was tried and
> reverted as a regression). Trust surfaces live in
> `finding-trust-surfaces.md` (not built).

# Acquisition-mode findings — claim-first mint with automated paper chase and verified grounding

## Motivation / why

A finding today must point at an in-corpus chunk at mint time
(`cited_in=` hard-required since the kind's inception, `44708813` —
the base "no thin-air claims" grounding requirement; ADR 0073's
taproot hub door adds its own, stricter version for hubs). That is
correct for the ordinary case (citing a paper already in the library)
but leaves **no representation for a claim whose supporting paper is
not yet ingested**. Three live workflows need exactly that state:

- **W1 — literature hunt (new drafts):** "I believe claim X; fetch the
  paper that supports it." The planner's lit-hunt template
  (`planner_prompt.py:406-418`) teaches a `cited_in`-less
  `put(kind='finding')` that has *never* been valid — every hunt tick
  dies on `BadInput` (gr183824, gr183865). The paper-first workaround
  (mint a bare stub, wait, then mint the finding) loses the claim while
  waiting and never satisfies the lit-hunt todo's
  `all_child_findings_resolved` auto-check.
- **W2 — legacy-draft repair:** an imported draft asserts something and
  cites a paper. Record the claim, fetch the cited paper if absent,
  verify it actually says that, remove/flag the claim if not.
- **W3 — external-paper review:** paper A cites paper B at some
  passage. Record what A claims, fetch B, verify, and aggregate the
  failures into a review draft.

All three are the same loop — *claim → get the paper → verify → keep
or reject* — differing only in where the claim came from and the
on-failure policy. The missing shared substrate is a first-class
"claim exists, corpus evidence pending" lifecycle state.

**Why naive relaxation is not an option** (root-cause dossier,
2026-08-04): a `cited_in`-less finding mints with `meta.chain=[]`;
`chase.advance_finding` (`chase.py:356-361`) treats an empty chain as
instantly terminal (`STATUS:dead_chain`), and `dead_chain` is in
`all_child_findings_resolved`'s resolved set — so the lit-hunt todo
would **auto-close as done having acquired zero papers**. Today's loud
failure would become a silent false completion. Any relaxed mode must
be designed around that hazard.

## In scope

### 1. New lifecycle state: `STATUS:acquiring`

Added to the closed STATUS vocabulary (`store/types.py`), non-terminal,
preceding `tracing`:

    acquiring → tracing → established | multi_candidate | dead_chain

### 2. Acquisition-mode mint (atomic)

    put(kind='finding', title=…, body=<claim>,
        wants=[{doi:…} | {arxiv:…} | {title:…, url:…}, …],   # ≥1
        provenance=<ref handle>)                              # required

- `wants=` — descriptors of the paper(s) the claim expects grounding
  from. The handler **atomically** upserts a `DREAM:acquire` paper stub
  per descriptor (via the existing `upsert_stub_paper` path) and links
  stub↔finding with a new `awaits-evidence` relation. One call — no
  multi-step contract for an agent to fumble (the 6×-spin history
  behind `4810c545`).
- `provenance=` — where the claim came from: W1 the research/web ref or
  hunt todo; W2 the legacy-draft chunk; W3 the citing chunk. A link
  (`derived-from`), required in acquisition mode. This preserves the
  no-thin-air invariant in weakened form: every claim is traceable to
  *something* at mint, just not yet to corpus evidence.
- Ordinary mode (`cited_in=`) and the taproot hub door (`supporters=`)
  are unchanged; acquisition mode is a third, explicit branch.
- `awaits-evidence` is a **new row in the seeded `relations` table**
  (`links.relation` FKs to it) — a forward migration, precedent ADR
  0073 / migration `0094` (`establishes`).

### 3. The bridge: re-ground on ingest

Two coupled edits in `chase.py`:

- **Claim query**: `claim_tracing_findings` (`chase.py:195-274`), the
  sole feeder of `advance_finding`, currently hard-filters
  `t.value = 'tracing'`. It is widened to also claim
  `STATUS:acquiring` rows — without this the new arm is unreachable in
  the real worker loop.
- **Acquiring arm** in `advance_finding`: an `acquiring` finding with
  an empty chain is **not** `empty_chain→dead` — it polls its linked
  (`awaits-evidence`) stubs. When a stub has been promoted to a real
  ref with chunks, chase runs the existing grounding machinery
  (claim-text embedding search over the new paper's chunks → STANCE
  verdict), sets `cited_in`/`chain`, and flips `acquiring→tracing`;
  the normal lifecycle proceeds. No new verifier is invented.

### 4. Honest give-up

`acquiring` must not poll forever (the phantom-*open* twin of the
phantom-closed hazard). When every linked stub is exhausted (all fetch
legs returned `no_oa_version` / unfetchable) and a grace window has
passed (`PRECIS_ACQUIRE_GRACE_DAYS`, env-tunable, default 7 — decided),
the finding goes `dead_chain(reason=unacquirable)` and its stubs
surface in the hand-download queue (`f971f012` already ranks these).
Truthfully terminal: the todo's auto-check then reports reality. Owner
of the transition: the same chase pass (`run_finding_chase_pass`), in
the acquiring arm — no new worker.

### 5. Guard-rail edits

- `all_child_findings_resolved`: `acquiring` = "poll again", never
  resolved.
- Default `search(kind='finding')` cohort is an **allowlist**
  (`STATUS:established` + taproot hubs, `finding.py:~678-699`) — so
  `acquiring` is excluded by construction with **zero filter edits**;
  the acceptance criterion below verifies rather than changes this.
- Taproot hub door **untouched**: `seed_claim_hub` still requires live
  corpus supporters; an `acquiring` finding cannot join a hub until
  grounded.

### 6. Planner template upgrade

Rewrite `planner_prompt.py`'s lit-hunt section to teach acquisition-
mode mints (`wants=` + `provenance=`). Also fix the two collateral
defects the dossier found: the dead `verifier_confidence=` kwarg and
the wrong worker attribution (the OA cascade is `fetch_oa`, not
`finding_chase`). Skill docs (`precis-finding-help.md`) gain the
acquisition-mode contract.

## Explicitly NOT in scope

- **Trust surfaces (export marking + editor badges)** — split out to
  `docs/proposals/finding-trust-surfaces.md` (blocked by this
  proposal). Export/editor behavior for `acquiring`/`tracing`-backed
  claims ships there, including its integration with the already-built
  smartdraft review-status indicator.
- **fetch_oa URL leg.** `{title, url}` descriptors mint a stub, but
  nothing auto-fetches a bare URL in this proposal — URL-only stubs go
  straight to the hand-download queue. A direct-URL fetch leg (through
  `safe_fetch`, SSRF-guarded; landing-page-vs-PDF discrimination) is a
  follow-on ship.
- **W2/W3 workflow templates.** The verdict-policy layer (remove claim
  from draft / build a review-of-failings draft) lives in planner/todo
  templates *on top of* this substrate and ships separately once the
  substrate exists. Findings do not learn what a draft is.
- **Any weakening of the taproot hub door** (`resolve_paper_ref_id` /
  `seed_claim_hub`) — separately load-bearing, deliberately strict.
- **Backfilling legacy findings** into the new state; existing findings
  are untouched.

## Acceptance criteria

1. `put(kind='finding', title=…, body=…, wants=[{doi:…}], provenance=…)`
   succeeds with no `cited_in`; the finding is `STATUS:acquiring`; a
   `DREAM:acquire` paper stub exists and is linked `awaits-evidence`;
   the stub is claimable by `fetch_oa` when it carries a doi/arxiv/s2
   id.
2. Acquisition mode without `provenance=`, or with empty `wants=`, is a
   `BadInput` naming the missing piece (no silent thin-air claims).
3. **Via the worker-loop entry point** (`run_finding_chase_pass`, not a
   hand-built call to `advance_finding`): an `acquiring` finding is
   claimed by the pass; while its stub is unfetched it stays
   `acquiring` (no `dead_chain(empty_chain)`); after the stub is
   ingested with chunks, a pass grounds it (chain populated,
   `cited_in` set, status `tracing`), and the pre-existing lifecycle
   proceeds unchanged from there.
4. A lit-hunt todo whose children are all `acquiring` is **not**
   resolved by `all_child_findings_resolved` (poll-again), and a todo
   whose children reached `established`/`dead_chain`/`multi_candidate`
   resolves exactly as today.
5. Give-up: an `acquiring` finding whose stubs are all exhausted past
   the grace window transitions to `dead_chain(reason=unacquirable)`
   exactly once (verified via the pass entry point), and its stubs
   appear in the hand-download queue.
6. Default `search(kind='finding')` returns no `acquiring` findings
   (allowlist verified by test, no filter edit expected); an explicit
   status filter does return them.
7. Migration: a fresh DB and an upgraded DB both carry the
   `awaits-evidence` row in `relations`; the sealed-migration check
   passes (forward-only, ADR 0005).
8. Regression: ordinary `cited_in=` mints and taproot `supporters=`
   hub mints behave byte-identically to before (existing test suites
   pass unmodified).
9. **A plain pytest unit** asserts on the rendered planner lit-hunt
   template string: it contains `wants=` and `provenance=`; it does
   NOT contain `verifier_confidence=`, a `cited_in`-less bare
   `put(kind='finding', text=` shape, or any claim that
   `finding_chase` performs OA resolution. (Mechanism decided: string
   assertions on the template constant — no new lint tooling.)

## Target + blast radius

- `src/precis/store/types.py` — STATUS closed vocab (+`acquiring`).
- **New forward migration** — seed `awaits-evidence` into `relations`
  (FK target of `links.relation`); baseline snapshot untouched
  (release-time only).
- `src/precis/handlers/finding.py` — third put branch (acquisition
  mode), `wants=`/`provenance=` parsing, atomic stub upsert + link.
- `src/precis/workers/chase.py` — `claim_tracing_findings` widened to
  claim `acquiring`; acquiring arm in `advance_finding` (poll stubs,
  ground on ingest, give-up transition, `PRECIS_ACQUIRE_GRACE_DAYS`).
- `src/precis/workers/auto_check_evaluators/all_child_findings_resolved.py`
  — `acquiring` = poll-again.
- `src/precis/workers/planner_prompt.py` — lit-hunt section rewrite.
- `src/precis/data/skills/precis-finding-help.md` — contract docs.
- Tests across all of the above.

## Open questions / decisions log

- **Decided (Reto, 2026-08-04):** atomic `wants=` mint over
  stub-first-then-handles; `unverified`/`unsupported` as the
  human-facing vocabulary ("speculative" rejected as default — it
  mis-attributes the doubt to the author; reserved for a possible
  future author-chosen conjecture marker). Export/editor decisions
  (always-mark, no refusal mode, recorded unacquirable override,
  badges) moved with the trust-surfaces split.
- **Decided (2026-08-04, post-review):** grace window is env-tunable
  `PRECIS_ACQUIRE_GRACE_DAYS`, default 7.
- **Decided (2026-08-04, post-review):** `tracing` findings get the
  same "unverified" treatment as `acquiring`-born ones on the trust
  surfaces — the reader-facing question "is this verified?" doesn't
  care which path got here. (Implementation lives in the
  trust-surfaces proposal.)

### Readiness review (ADR 0048, 2026-08-04)

- **blocker** — "Migrations: none expected" is wrong for the new
  `awaits-evidence` link relation §2 introduces. `links.relation` has an
  FK to `relations(slug)` (`links_relation_fkey`, seeded in
  `migrations/baseline/schema.sql`) — a closed, migration-seeded
  vocabulary, not free text. `awaits-evidence` isn't in the seed data.
  This exact mistake already happened once in this repo:
  `docs/decisions/0073-taproot-evidence-relations.md` documents
  correcting `taproot.md`'s identical "no migration" claim for a new
  relation slug (`establishes`, migration `0094`). The STATUS half of
  the claim is verified correct (`_CLOSED_VOCAB["STATUS"]` in
  `store/types.py` is a plain Python frozenset, no schema/migration
  needed) — only the relations half is wrong.
  **→ RESOLVED:** forward migration added to scope (§2, Target, AC #7).
- **blocker** — Blast radius omits the chase worker's claim/dispatch
  query. `chase.claim_tracing_findings` (`chase.py:195-274`) is the
  *only* caller that feeds rows into `advance_finding`
  (`run_finding_chase_pass`, ~line 527), and it hard-filters
  `WHERE ... t.value = 'tracing'` — an `acquiring` finding is never
  selected. Without also widening/adding to this claim query, the new
  "acquiring arm" described in §3/§4 and the Target section is
  unreachable in the real worker loop, even though AC #3/#5 could pass
  against a hand-built unit-test `FindingRow` that calls
  `advance_finding` directly — a gate-green-but-inert build.
  **→ RESOLVED:** claim-query widening is now explicit in §3 and
  Target; AC #3/#5 rewritten to verify via `run_finding_chase_pass`.
- **blocker** — AC #7 (export record) depended on an unresolved Open
  Question. **→ RESOLVED by the split:** export marking + override +
  record moved wholesale to `finding-trust-surfaces.md`, where the
  record's home is decided (a `ref_events` row on the draft, written
  at export time via the existing `append_event`).
- **blocker** — AC #9's mechanism was undecided ("transcript-shaped
  test (or prompt-lint)" — neither exists in-repo).
  **→ RESOLVED:** plain pytest string assertions on the template
  constant; AC #9 rewritten accordingly.
- **blocker** — Unacknowledged overlap with
  `docs/proposals/smartdraft-review-status-ui.md` (status: `built`) —
  §6's editor-badge plan targeted the same per-paragraph indicator
  surface. **→ RESOLVED by the split:** the trust-surfaces proposal
  names that shipped system as its integration target and must specify
  fold-in vs sibling-badge before its own `ready`.
- **advisory** — default-search-cohort wording mischaracterized an
  allowlist as an exclude-list. **→ RESOLVED:** §5 reworded; AC #6 is
  verify-not-edit.
- **advisory** — "no thin-air claims" invariant misattributed to ADR
  0073. **→ RESOLVED:** Motivation reworded (base requirement =
  `44708813`; ADR 0073 = the hub door's stricter version).
- **advisory** — grace-window prose vs open-question tension.
  **→ RESOLVED:** decided above (env-tunable, default 7).
- **Split signal** — §6 independently testable, disjoint file set,
  one-way dependency. **→ ACCEPTED:** `finding-trust-surfaces.md`
  created with `blocked-by: finding-acquisition-mode`.
