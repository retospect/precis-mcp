---
status: ready
title: checklist kind — argued, invalidating check ledgers; first instance pcb pre-tapeout
prio: high
model: opus
---

# The `checklist` kind — Checklist-Manifesto gates for LLM agents

Design session 2026-09-09 (Reto + agent), zippy-booping-sky worktree.
Prompted by "pre-tapeout pcb checks — a big checklist, maybe a skill, with
notes on components so repeated checks accumulate instead of restarting."
Generalized per Reto in-session: a first-class checklist kind, useful
beyond pcb (3D printing, se, deploy preflight), where an agent can **check
and argue each point**, with **invalidation on change**.

**DRC and checklist are distinct layers (Reto, in-session).** DRC/ERC
are *rules we must follow*: owned by the domain kind, auto-checked,
exhaustive within their encoded scope. The checklist never decomposes
them — it consumes each checker as ONE coarse item ("DRC clean", "all
nets routed") whose evidence is the latest run id. Everything else on
the checklist is an **LLM eval task**: board shape reasonable, board
mountable, connectors mate with their actual counterparts, parts in
stock, fastener metals galvanically compatible. This matches the
Perplexity survey's central finding: the expensive respins are designs
that pass all encoded rules — representation mismatches (symbol vs
footprint vs cable pinout, layout vs enclosure, BOM vs purchasable
parts) no rule captures. The checklist layer exists for what DRC
cannot encode. Item content is data — addressable, editable, arguable —
not prose frozen in a skill; skills shrink to orchestration.

## Why a kind and not a pcb-local table

- The ledger mechanics are domain-independent: versioned items, per-target
  runs, verdict + evidence + checked-at, staleness, three-valued honesty.
  Next consumers already exist: `cad-printability-probe` (3D printing),
  se validate-before-build, deploy preflight (`deploy-verification-guards`).
- pcb supports neither `tag` nor `link` (`protocol.py` raises
  `Unsupported`) — and that mostly does not matter here. Links INTO a
  pcb ref already work (other kinds write the edge; pcb `view='links'`
  renders it), so the checklist side writes the run↔target binding.
  Link/tag are ref-granular anyway; per-component verdicts need
  sub-object anchors (refdes, net name) the link graph cannot express —
  hence name-anchored verdict rows. Asked in-session ("add tag and
  link to pcb?"): **add `tag` only** (STATUS-style workflow tags are
  how state works everywhere else; a small independent change);
  pcb-initiated `link` buys nothing this design needs.
- Fits the product thesis: durable, structured, agent-consumable state.
  An agent `get`s the checklist, works it, and the run is queryable state
  rather than transcript prose.

Lives in **core** (`src/precis/handlers/checklist.py`), not a plugin —
pcb is core and core must not import plugins.

## Data model

**Storage (Reto, in-session: "files that get ingested, or database
things?"): DB entities, with standard checklists shipped as files.**
Runs/verdicts/notes are DB-only (operational state). Definitions are DB
entities too, but standard checklists (pcb-tapeout, later
cad-printability) ship as data files in `src/precis/data/checklists/`
(git-reviewed content, arrives on deploy — skills are the precedent
for packaged data files, but NOT for the sync: skills are read into an
in-process cache, never DB-synced) and sync into the DB on boot,
upsert-by-item-name. **The sync mechanism to imitate is
`jobs/oracle_sync.py`** (sha256 content-state, advisory lock,
idempotent reingest), wired at boot in `dispatch.py` behind the
`mcp_read_only` guard (`_boot_is_read_only`) — the sync MUST be
skipped on a read-only DSN, the exact failure that guard was added
for. Items carry an origin: `shipped` items are owned
by the file (edits go through git; sync never clobbers its own),
`local` items are DB-native runtime additions sync never touches.
Runtime disagreement with a shipped item is a run-level fact (waiver
with rationale in the argument thread), not a definition mutation — so
shipped content stays stable and reviewable while every target can
argue its own case. Deliberately NOT the oracle/ingest model: ingest
yields chunks; definitions need per-item identity (verdicts and
threads anchor to item names), versions, and sync semantics.
`pcb-tapeout-checklist-seed-items.md` is the first shipped checklist,
one format change away.

**Definition** — a checklist is a named, versioned set of items. Per item:

- `name` — stable slug (the anchor for verdicts and argument threads).
- `phase` — free-text ordered phase label owned by the checklist
  (pcb: `schematic` → `netlist` → `layout` → `fab`).
- `severity` — `blocking | advisory`. Reuse the DRC two-tier posture
  (fab floor = error, house margin = warn); do not invent a third scale.
- `decidability` — `tool` (names the checker whose latest run decides
  it, e.g. `pcb view='drc'` or an `auto_check` evaluator; always ONE
  item per checker, never a decomposition of its rule set) or
  `judgment` (an LLM eval task: instructions the agent follows and
  argues). Most items are judgment; tool items are the few bridges to
  the rules layer.
- `prevents` — the failure this item catches. **Mandatory** — the
  cargo-cult filter is structural: no failure statement, no item.
- `applies` — optional predicate hint (e.g. "boards with connectors",
  "designs with an MCU") so N/A verdicts are honest, not skipped.

**Revving (Reto, in-session): never overwrite — append revs; current
is derived.** `checklist_items` is append-only with
`UNIQUE (checklist_id, name, rev)`; current = highest live rev. An
edit inserts rev+1; deploy sync does the same mechanically (file
content differs from current rev → new rev; item gone from file →
`retired_at`). History is kept because **a verdict must name what it
judged**: verdicts pin `item_rev`, and `item_rev < current` is the
staleness signal — invalidation on item change falls out of the same
comparison as invalidation on target change. No hand-maintained
checklist-level version number: git holds file history, the DB holds
item revs.

**Git/DB consistency (Reto, in-session): one source of truth PER
FACT, disjoint write ownership.** Git owns shipped item content; the
DB rows for shipped revs are a derived materialization (as the oracle
corpus is for oracle files) and the `edit` verb REJECTS writes to
shipped items (handler-enforced, with a hint: add a local item or
file a gripe). The DB owns local items, verdicts, notes, assignments;
sync never touches them and git never holds them. Sync invariants:
(1) atomic + idempotent — one transaction per file, content-hash
compared, no-op on unchanged; (2) append-only — inserts revs or sets
`retired_at`, never rewrites, so a rollback deploy just appends a rev
matching older content and pinned verdicts stay valid; (3) staleness
bounded at one deploy — same lag the code itself has. Promotion race
resolved by making **origin a property of the rev, not the name**:
local rev N → shipped rev N+1, linear sequence, ordinary staleness,
no retire-and-recreate. Drift (e.g. hand-edited prod rows) must be
detectable: a doctor check compares current DB revs against the
deployed files.

**New-item intake (Reto: "add with gripe or just insert?"): insert
first, gripe to promote.** Board-specific idea → insert a `local`
item (optionally target-scoped via `target_ref_id`), live
immediately. Generally-useful idea → insert local too (the current
board benefits now) AND file a gripe asking the repo to add it to the
shipped file; review lands it, deploy sync matches by name and
supersedes the local item. The gripe is the promotion path, not the
storage — nothing waits on a deploy to be usable.

**Verdict ledger — no "run" entity.** A run-as-row fights
carry-forward; the ledger is the primitive and "a run" is the current
view over it. Definitions exist once; what is per-board is verdicts:

```
checklists           (id, name UNIQUE, origin, created_at, retired_at)
checklist_items      (id, checklist_id, name, rev, phase, severity,
                      decidability, prevents, applies, body,
                      origin shipped|local, target_ref_id NULL,  -- NULL = all
                      created_at, retired_at)
                      UNIQUE (checklist_id, name, rev)
checklist_assignments(target_ref_id, checklist_id, ...)
checklist_verdicts   (id, target_ref_id, checklist_id, item_name, item_rev,
                      verdict pass|fail|n/a|waived, evidence jsonb,
                      fingerprint, checked_at, checked_by, retired_at)
checklist_notes      (target_ref_id, checklist_id, item_name, name, kind,
                      body, re, about, origin, created_at, retired_at)
                      -- se_notes shape; checklist_id present for the same
                      -- reason verdicts carry it: two assigned checklists
                      -- may define the same item name
```

Status per item = latest live verdict judged against current item rev
+ current target fingerprint. The **assignment row is what makes
silence honest**: an assigned target with no verdicts renders every
item "not checked"; without assignments, "no rows" would be
indistinguishable from "nothing to check". Kind-level defaults (every
pcb gets pcb-tapeout) create assignments implicitly; extras explicit.
Evidence anchors: DRC `run_id`, `datasheet` ref + chunk, or stated
reasoning for judgment items. Waivers carry mandatory rationale —
the "argue" outlet for items deliberately not met.

**Argumentation** — per-item note threads, copying the `se_notes` shape
verbatim (migration `0005_se_notes_freedom.sql` is the model): name-keyed
(never row-id FKs), append-only (corrections are new notes), kinds
`question | answer | decision`, open/settled **derived** never stored,
dangling anchors reported at read time not rejected at write time.
`about` here anchors a note to subobjects of the *target* (pcb:
refdes/net names) or to another item name — resolved at read time,
dangling reported not rejected, same as se. `precis_se/notes.py` is
~pure already; lift it to **`src/precis/utils/notes.py`** and have
`precis_se` import it back (fallback if plugin packaging fights back:
copy the 96-line shape, file the dedup as follow-up).

**Three-valued honesty (non-negotiable).** A target+checklist with no
run renders "not checked", never "clean". This repo has already paid for
that lesson twice (`Store.pcb_drc_findings_latest` docstring;
`netlist_drc_clean.evaluate` returning `None` for "not yet"). Every view
distinguishes not-run / stale / current, and a programmatic item whose
check surface cannot fire (missing data) must say so in its verdict.

## Invalidation on change

The accumulate-don't-restart semantics ("we already checked this"):

- Each verdict stores a content fingerprint of the target scope it
  covered — for pcb, a hash of the anchored subobject (component by
  refdes, net by name; **name-anchored, never row-id** — `pcb_apply`
  rebuilds rows on every put, same constraint that shaped se_notes).
- On read, current-fingerprint ≠ stored-fingerprint ⇒ verdict renders
  **stale**, verdict retained (history, and the diff tells the agent
  what changed since the check).
- A re-run diffs against the ledger: current verdicts carry forward,
  only stale/not-run items are revisited.
- Precedent for the read-time-recheck shape: `refs.retraction_checked_at`
  (checked-at + TTL ⇒ re-check is a no-op unless stale or forced).
  Optional per-item TTL for checks against moving external state (BOM
  availability, part lifecycle) where the target didn't change but the
  world did.

## First instance: pcb pre-tapeout

Phases and seed items. A best-practice survey is cached+pinned under
kind `perplexity-research` (query "Comprehensive PCB design review
checklist of industry best practices, organized by phase…" —
retrievable by that query or `search(kind='perplexity-research', …)`).
Curation rule (Reto): keep what names a real failure; discard cargo
cult. Tool items are the two bridges — "DRC clean", "all nets routed"
(plus netlist-exceptions clean once `pcb-agent-interface-gaps` ships
its delta/exceptions surface). The rest below are judgment items:

- **schematic** (pre-netlist-complete): connector gender/polarity chosen
  to mate with the *actual counterpart* (judgment — the model can't know
  the other side); spare MCU ADC inputs wired to supply rails for
  readback, dividers as needed (judgment, "considered" not pass/fail);
  strap/boot pins vs datasheet table; decoupling per power pin; unused
  pin handling; test points; current budget per rail.
- **netlist** (before place/route): ERC-style programmatic checks —
  unconnected pins, single-node nets, nets missing current, parts
  without footprints, unresolved measure operands. Much of this is
  already specified as the "delta + exceptions" push surface in
  `pcb-agent-interface-gaps.md` — the checklist item names it; build
  once, consume twice.
- **layout** (post-place/route): the existing geometric DRC (15 named
  rules, persisted findings) — the item's evidence is the DRC `run_id`;
  pin-1/polarity silkscreen; courtyards; mechanical fit.
- **fab** (pre-order): gerber-level review, stackup, drill table, BOM
  availability/lifecycle (TTL'd), assembly warnings
  (`_export_warnings`). **Honesty gate:** `pcb-fab-output-unwired.md` —
  until the gerber tail is wired, the fab phase must show that item
  red, not absent. A checklist that can't be completed says so.

Netlist question settled in-session: the board model *is* the netlist
(`view='netlist'` exports it); validation runs directly over it, no
interchange format. **Non-goal: backannotation** — single source of
truth, nothing to annotate back to. Becomes relevant only if an external
schematic source or layout-time refdes renumbering ever appears; noted
here so nobody builds it speculatively.

## Wiring

- **Evaluator:** one new `auto_check` evaluator (`checklist_clean` —
  registry says adding one is "two lines") so a todo can gate on
  "run current, no blocking failures" with `None` = not yet run.
- **Skill:** `precis-tapeout-help`, modeled structurally on
  `precis-preflight.md` (severity legend, per-item how-to-decide,
  numbered pre-release sequence ending "re-run until clean"). It
  orchestrates: which checklist, which phase when, which programmatic
  views to invoke, how to argue/waive. Under the 16 KB soft cap —
  item *content* lives in the checklist, not the skill.
- **Generic skill:** `precis-checklist-help` for the kind itself
  (author a checklist, run one, read staleness, argue an item).

## Later slice: annotated schematic view

`view='schematic'` already renders. Enhancement, not new renderer:
place tables pulled from linked `datasheet` refs (address-strap bits,
boot-pin configs) next to the relevant instance/pins, with links back
to the datasheet chunks — the chain exists
(`pcb_components.part_lcsc` → `parts` → `datasheet-of`). Doubles as
the review aid for the schematic-phase judgment items: strap-pin
verification becomes one page instead of schematic-vs-PDF flipping.

## Open issues → v1 defaults (none blocking; decided 2026-09-09)

- **Fingerprint scope**: whole-target fingerprint by default; agent-
  supplied anchors (refdes/net names) at verdict time narrow it to
  those subobjects. Broad verdicts stale on any target edit — honest;
  the skill tells agents to anchor.
- **Fingerprint computation**: v1 calls pcb directly (both core); a
  per-kind protocol hook waits for the second consumer (cad, slice 4).
- **Assignments**: v1 explicit only; kind-defaults ("every pcb gets
  pcb-tapeout") arrive with slice 2.
- **Notes extraction**: lift `precis_se/notes.py` to
  `src/precis/utils/notes.py`, se imports back; if plugin packaging
  fights back mid-build, copy the 96-line shape and file the dedup
  as follow-up.
- **Seed format**: the seed-items markdown table converts to the
  shipped per-item data file (YAML) in `src/precis/data/checklists/`.
- **Migration number**: picked at build time (sibling-collision risk;
  see impacted-ship migration-guard history).
- **Trailing independents**: `tag` on pcb; `checklist_clean`
  evaluator.

## Slices

1. **checklist kind core** — tables (checklists, items, assignments,
   verdicts, notes), put/edit/get/search, deploy-sync of shipped
   files, staleness rendering, the shared notes module extraction.
2. **pcb tapeout instance** — seed content from
   `pcb-tapeout-checklist-seed-items.md` (curated from the Perplexity
   survey + this session's items), the tool-item bridges (DRC clean /
   all-nets-routed / netlist exceptions, the last via
   `pcb-agent-interface-gaps`' delta/exceptions surface),
   `checklist_clean` evaluator.
3. **skills** — `precis-checklist-help`, `precis-tapeout-help`.
4. **cad assembly/printability instance** — seed content in
   `cad-assembly-checklist-seed-items.md` (tool-clearance,
   one-direction fastening, sequence marking, turns budget,
   hands count, disassembly path; prerequisite: owned-tool library +
   reachability probe), print items joint with
   `cad-printability-probe`; proves the kind is actually generic
   before declaring it so.
5. **annotated schematic view** (independent of 1–4).

## Target + blast radius (slice 1)

New files: `src/precis/handlers/checklist.py` (template:
`handlers/rxn.py` + `0157_rxn_kind.sql`, the most recent core kind),
`src/precis/migrations/0158_checklist_kind.sql` (number re-checked at
build — sibling collision risk), `src/precis/store/_checklist_ops.py`
(Store mixin), `src/precis/jobs/checklist_sync.py` (modeled on
`jobs/oracle_sync.py`), `src/precis/utils/notes.py` (lifted),
`src/precis/data/checklists/` (slice 1: a minimal test fixture only;
pcb-tapeout arrives in slice 2). Touched: `dispatch.py` (boot wiring
next to oracle sync, behind the read-only guard), `store/store.py`
(mixin registration), `precis_se/notes.py` (becomes a re-export of
the lifted module). Everything else is additive — new tables, new
kind; the se re-export is the only shared-code change.

## Acceptance criteria (slice 1)

- Migration creates the five tables; full gate green.
- `put` creates a local checklist + items; `edit` ops add an item rev,
  retire a local item, `add_note`/`remove_note`; `edit` on a shipped
  rev is REJECTED with the local-item/gripe hint.
- Verdict writes pin `item_rev` + caller-supplied fingerprint and are
  append-only (new verdict supersedes; old row retained, queryable).
- The per-target status view renders all three states, test-verified:
  assigned-but-no-verdict = "not checked"; `item_rev < current` =
  stale; otherwise current. An UNassigned target renders "no checklist
  assigned", never empty-clean.
- Sync: from a fixture file — first run inserts revs; second run is a
  no-op (content-hash); an item removed from the file is retired;
  local items/verdicts/notes are untouched; sync is skipped on a
  read-only DSN (same test pattern as the oracle-sync guard).
- se's notes tests stay green with `precis_se` importing the lifted
  module.
- A drift-check function compares DB current revs against the shipped
  files (doctor wiring may follow later; the function + test land now).

## Explicitly NOT in scope (slice 1)

- Any pcb-specific code. **Fingerprints are opaque caller-supplied
  strings in slice 1** — stored, compared, rendered; never computed.
  Slice 1's staleness is item-rev-based; fingerprint-staleness
  activates in slice 2 when pcb supplies the hash. The
  Invalidation-on-change section describes the full (slice 2+)
  behavior.
- The pcb-tapeout content, tool-item bridges, `checklist_clean`
  evaluator, kind-default assignments, `tag` on pcb (all slice 2 or
  trailing independents); skills (slice 3); cad instance (slice 4);
  schematic view (slice 5).
- No new protocol hooks; no changes to pcb, se behavior, or the link
  graph.

## Open questions / decisions log

2026-09-09, post-vet: all four blockers + both advisories addressed in
place — sync precedent corrected to `jobs/oracle_sync.py` +
`mcp_read_only` guard; `checklist_id` added to `checklist_notes`;
Acceptance criteria / Target + blast radius / Explicitly NOT in scope
sections added (fingerprints opaque in slice 1); notes module home
fixed as `src/precis/utils/notes.py`; `about` semantics stated. The
vet findings below are retained as the record.

Findings from a `ready`-gate vet of slice 1 (2026-09-09), verified against
the code:

- blocker: no `## Acceptance criteria` section anywhere in this spec
  (TEMPLATE.md requires it; "done means X" for slice 1 — e.g. what a green
  gate / post-deploy look confirms about tables, put/edit/get/search,
  deploy-sync, staleness rendering, the notes extraction — is never stated).
- blocker: no `## Target + blast radius` section — slice 1's touch points
  (new handler module path, migration file, where the boot-sync code lives,
  the notes module's new home) are never named.
- blocker: no `## Explicitly NOT in scope` section — the slice 1/2 boundary
  relies solely on the informal "Slices" list; the Data model and
  Invalidation-on-change sections describe pcb-specific fingerprinting
  inline in what reads as the core (slice 1) design, without saying that
  code is deferred to slice 2.
- blocker: the "sync into the DB on boot... next to the skills, for the
  same reason skills are file-backed... arrives on deploy" precedent is
  factually wrong. Skills are not synced into any DB table today:
  `handlers/skill.py::_load_skills_map` reads `.md` files straight into an
  in-process cache; `ingest/skill_ingest.py` (the module `skill.py:2287-2289`
  itself calls "the boot-time scanner") implements only scan/plan
  (`scan_skill_dir`, `IngestPlan`, `IngestFailure`) — there is no DB-write
  stage anywhere in the codebase (only `tests/test_skill_ingest.py`
  exercises it; zero production callers). The actual working precedent for
  an idempotent, content-hash-compared, advisory-locked, read-only-DSN-aware
  boot-time DB sync of file content is `jobs/oracle_sync.py`
  (`_advisory_lock_id`, `compute_corpus_state`/sha256, `maybe_reingest`),
  wired at `dispatch.py:874-899` behind the `mcp_read_only` guard
  (`_boot_is_read_only`, `dispatch.py:444`) added in `a998e26f` specifically
  because ungated non-idempotent boot writes broke on a read-only replica.
  This spec's sync design never mentions `oracle_sync` and never addresses
  the read-only-DSN case, despite proposing the same class of boot write
  that guard exists to gate — a builder taking the "skills already do this"
  claim at face value will find no DB-write code to copy.
- blocker: `checklist_notes` (no `checklist_id` column) vs
  `checklist_verdicts` (has `checklist_id`) — a target can carry multiple
  assigned checklists ("extras explicit"), so if two assigned checklists
  ever define an item with the same `name`, `checklist_notes` anchored only
  by `(target_ref_id, item_name)` can't tell their note threads apart. The
  spec neither adds `checklist_id` to the notes table nor states an
  item-name-globally-unique constraint; a builder must silently pick one.
- advisory: no concrete target path named for "lift `precis_se/notes.py`
  into a shared home in core" (e.g. `src/precis/notes.py` vs
  `src/precis/utils/notes.py` vs a module under the new checklist package)
  — low-cost to decide at build time, but both `precis.handlers.checklist`
  and `precis_se.notes` need to agree on it.
- advisory: `checklist_notes.about`'s purpose isn't explained for this kind
  — in `se_notes` it anchors to design objects distinct from the note's own
  subject; here the note is already anchored via `item_name`, so what
  `about` anchors to (refdes/net names? another item?) is left to inference
  from the `se_notes` precedent rather than stated.
- Verified accurate (no issue): `precis_se/notes.py` is genuinely pure
  (dataclasses only, no DB import) and liftable as described; the DRC rule
  count is 15 (`precis/pcb/drc.py`, confirmed via `rule="..."` literals);
  pcb has no `tag`/`link` overrides (falls to `protocol.py`'s `Unsupported`
  default); `netlist_drc_clean.evaluate` and
  `Store.pcb_drc_findings_latest`'s not-run-vs-clean distinction exist as
  described; `refs.retraction_checked_at` TTL precedent exists
  (`export/retraction.py`, `ingest/provenance.py`); the `auto_check`
  evaluator registry (`workers/auto_check_evaluators/__init__.py`) is a
  plain dict — adding one entry is genuinely ~2 lines; `pcb_instance_neighbors`
  / `pcb_net_members` (`store/_pcb_ops.py`) return deterministic dicts a
  fingerprint hash could be built from directly, though no fingerprint
  helper exists yet (fine — that's slice 2's job per "v1 calls pcb
  directly"); current max core migration is `0157_rxn_kind.sql` (next:
  0158); `src/precis/handlers/rxn.py` + `0157_rxn_kind.sql` is a concrete,
  recent core-kind template to build slice 1 from.
