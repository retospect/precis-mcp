---
status: draft
title: Generalize the dossier owner from quest to any process (ADR 0064 §B)
model: sonnet
---

# Generalize the dossier owner from quest to any process (ADR 0064 §B)

## Motivation / why

[ADR 0064](../decisions/0064-dossier-thinking-substrate-and-paper-projection.md)
settled the framing: **a dossier belongs to a *process*, never an *artifact***,
and a paper is a *render* of a process's dossier + frontier. Deliverable **B**
(paper-as-export) is already free — the dossier is a `draft`
([ADR 0033](../decisions/0033-draft-chunks-editable-document.md)), so it exports
and tex-compiles on demand with zero new code. The **one** thing B still needs is
the enabling generalization the ADR explicitly deferred to "its own proposal":

> "Today `dossier.py` hardcodes a quest owner; generalizing the owner is part of
> **B**." … "generalize `dossier.py`'s owner beyond quest so a non-quest
> living-review process can own one."

Concretely, `src/precis/quest/dossier.py` couples every entry point to a quest:

- every public function takes `quest_id` and resolves the draft via
  `dossier_ref_id(store, quest_id)` → `links WHERE dst_ref_id = quest_id AND
  relation = 'dossier-of'`;
- `ensure_dossier` stamps `meta={"dossier_of_quest": quest_id}` and
  `project_ref_id=quest_id`, and reads the owner's title via
  `store.get_ref(kind="quest", id=quest_id)` — a **hardcoded kind**.

So a non-quest process that wants a living synthesis (a standing topic review, a
paper-writing pipeline, a catalyst pathway campaign) cannot own a dossier today,
even though the `dossier-of` / `has-dossier` relation itself is already
owner-agnostic (`store/types.py`; migration 0067 defines the pair with no kind
constraint). The coupling is entirely in the Python, not the schema.

## In scope

A mechanical, **migration-free** widening of `dossier.py`'s owner from
`quest_id: int` (a quest) to `owner_id: int` (any ref), preserving today's
quest behavior exactly:

- Rename the owner parameter `quest_id` → `owner_id` across the module's public
  functions (`ensure_dossier`, `rewrite_dossier`, `read_dossier`,
  `read_narrative`, `read_ledger`, `append_ledger_entry`, `ensure_ledger_chunk`,
  `dossier_ref_id`). `_RELATION = "dossier-of"` is unchanged (already generic).
- Resolve the owner's title **without a kind**. Today `ensure_dossier` reads it
  via `store.get_ref(kind="quest", id=owner_id)` — but `Store.get_ref`
  (`src/precis/store/_refs_ops.py:735`) requires `kind=` (keyword-only), so
  there is **no** `get_ref(id=…)` overload to swap in (the `ready` gate caught
  this). The title is only used as the draft's *default* name, so resolve it
  with a direct `refs`-table read inside the `store.pool.connection()` the module
  already opens for `dossier_ref_id` — `SELECT title FROM refs WHERE ref_id=%s
  AND deleted_at IS NULL` — rather than inventing a new `Store` method. (If a
  reusable kind-agnostic `Store.title_of(id)` helper is wanted, that is a
  separable tidy, not a blocker for this change.)
- Generalize the owner-back-pointer meta key `dossier_of_quest` →
  `dossier_of_owner`, **writing the new key while still reading the old one**
  (`meta.get("dossier_of_owner") or meta.get("dossier_of_quest")`) so existing
  prod dossiers keep resolving with **no migration and no backfill**. (The
  authoritative owner link is the `dossier-of` edge, not this meta — the meta is
  a convenience denormalization, so a mixed-key corpus is correctness-safe.)
- Update the **three** in-repo callers, which keep passing a quest id unchanged:
  `tick.py` (the quest tick — `qid`/`quest_id`), `handlers/quest.py`
  (`view='dossier'`), and `cli/quest.py` (`read_dossier`). (The `ready` gate
  corrected an earlier draft that miscounted `handlers/_integration_view.py` as a
  caller — it imports nothing from `dossier.py`; it reads the *already-resolved*
  draft ref via tags + `store.integration_ledger` / `unintegrated_papers`, so it
  is untouched by the owner rename.)

The paper-as-export half of B needs **no code** — it is the existing
`draft`→tex/docx export applied to the (now any-owner) dossier draft. This
proposal only removes the owner coupling that blocks a non-quest process from
having a dossier to export.

## Explicitly NOT in scope

- **A new owner kind, or wiring any specific non-quest owner** (topic review,
  paper pipeline). This proposal makes the seam owner-agnostic; the *first
  non-quest consumer* is a separate change with its own proposal — no speculative
  second caller is added here.
- **The periodic "bundle up and share" digest/newsletter cast** — that is a
  *separate process* ([ADR 0060](../decisions/0060-topic-dossiers.md)'s digest
  cast), explicitly out of scope per ADR 0064 §B.
- **Any dossier *structure* change** — the narrative/pinned-ledger split (ADR
  0064 §A, shipped) is untouched; this is purely an owner-identity widening.
- **A schema migration or a data backfill** — the relation is already generic;
  the meta-key change is handled by dual-read, so prod rows are never rewritten.
- **Renaming the `dossier-of` relation or the `precis-quest-help` skill's dossier
  section** — the relation name is already owner-neutral; the quest skill stays
  the quest's reference.

## Acceptance criteria

- `dossier.py`'s public functions accept an `owner_id` of **any** ref kind:
  a unit test creates a dossier owned by a **non-quest** ref (e.g. a `folder`
  or `memory`), rewrites it, appends a ledger entry, and reads narrative +
  ledger back — all succeed, and the `dossier-of` edge points at the non-quest
  owner.
- **Zero behavior change for quests**: the existing dossier-function tests
  (`tests/test_quest_tick.py` — the §A ledger/narrative/round-trip suite; there
  is no `test_quest_dossier*` file) pass unmodified (same behavior via the
  renamed param, same title seed, same ledger round-trip).
- **Backward-compatible resolution**: a dossier ref carrying only the *old*
  `meta.dossier_of_quest` key (no `dossier_of_owner`) still resolves through
  every read path — a regression test stamps the old key and asserts
  `read_dossier` / `dossier_ref_id` find it.
- `mypy` + `ruff` clean; `scripts/test` green; no new migration file under
  `src/precis/migrations/`.

## Target + blast radius

- **Primary**: `src/precis/quest/dossier.py` (all public function signatures +
  the two hardcoded-quest reads).
- **Callers (the complete set — signature-compatible; pass a quest id as
  before)**: `src/precis/quest/tick.py`, `src/precis/handlers/quest.py`
  (`view='dossier'`), `src/precis/cli/quest.py`. (`handlers/_integration_view.py`
  is **not** a caller — it reads the resolved draft directly, not through
  `dossier.py`.)
- **Schema**: none — `dossier-of`/`has-dossier` (`store/types.py`, migration
  0067) already owner-agnostic.
- **Docs to refresh on ship**: `docs/architecture/state-map.md` (quest dossier
  line — note the owner is now any process), ADR 0064 §B (mark the owner
  generalization built), `precis-quest-help` skill only if its dossier prose
  asserts a quest-only owner.

## Open questions / decisions log

- **Naming the generalized owner meta key** — proposed `dossier_of_owner`
  (dual-read the legacy `dossier_of_quest`). Alternative: drop the denormalized
  meta entirely and always resolve via the `dossier-of` edge. Leaning: keep a
  meta key (cheap owner lookup without a link query) but stop trusting it as
  authoritative. *(Non-blocking — either is migration-free.)*
- **Does `project_ref_id=owner_id` (placing the draft under the owner) hold for
  every owner kind?** For a quest it does; for a `folder` owner the draft would
  be placed *in* the folder, which is arguably correct. Confirm no placeable-kind
  rule (ADR 0045) rejects a draft under a non-quest owner during build.
  *(Non-blocking. Note the `ready` gate flagged that `Store.create_draft` takes
  `project_ref_id: int`, not `Optional`, so the earlier "fall back to
  `project_ref_id=None`" escape hatch isn't available — if a kind ever refuses
  the placement, the fix is to widen `create_draft` or place under a neutral
  parent, decided at build time. In practice the current owners (quest, and any
  near-term living-review process) are placeable, so this stays a build-time
  confirmation, not a redesign.)*

### `ready` gate findings (ADR 0048 §1) — 2026-07-24

- **blocker** — `store.get_ref(id=owner_id)` (the kind-agnostic call the "In
  scope" section names for the title lookup) does not exist as an API. The
  only `get_ref` in `src/precis/store/_refs_ops.py:735` is
  `get_ref(self, *, kind: str, id: int | str, include_deleted: bool = False)`
  — `kind` is a required keyword-only param, and a repo-wide grep finds zero
  existing call sites of `get_ref(id=…)` without `kind=`. The "mechanical
  replace" as written cannot be built as stated; the builder must invent
  either a new kind-agnostic `Store` lookup method or an inline `refs`-table
  query bypassing the `Store` abstraction, and the spec picks neither — an
  unresolved design decision inside a step described as mechanical.
- **blocker** — the caller list in "In scope" / "Target + blast radius" is
  factually wrong: `src/precis/handlers/_integration_view.py` does not
  import or call anything from `precis/quest/dossier.py` (no
  `dossier_ref_id`/`read_dossier`/etc., no `get_ref(kind="quest", …)`, no
  `meta.dossier_of_quest` read) — it only reads the already-resolved draft
  `ref` via `tags_for`/`store.integration_ledger`/`store.unintegrated_papers`
  (`src/precis/store/_integration_ops.py`). It needs zero changes and isn't a
  caller of the module being generalized. The true and complete caller set
  (confirmed via `grep -rn "from precis.quest.dossier\|quest import dossier"`)
  is exactly three files: `src/precis/quest/tick.py`,
  `src/precis/handlers/quest.py`, `src/precis/cli/quest.py`. The "Target +
  blast radius" section — which seeds the post-deploy check — points partly
  at the wrong place.
- **blocker** — the acceptance criterion "the existing
  `tests/test_quest_dossier*` suite passes unmodified" is unverifiable as
  written: no file matching that glob exists anywhere in the repo
  (`find tests -iname "*dossier*"` → zero matches). The real tests
  exercising `dossier.py`'s functions live in `tests/test_quest_tick.py`
  (confirmed via grep for `dossier_ref_id`/`ensure_dossier`/`read_dossier`/
  etc. across `tests/`). A builder or CI step invoking the named glob would
  silently no-op (vacuous green) rather than exercise real coverage.
- **advisory** — open question #2's stated fallback ("falls back to
  `project_ref_id=None` if a kind refuses") isn't actually available:
  `Store.create_draft`'s `project_ref_id` parameter is typed `int`, not
  `int | None` (`src/precis/store/_draft_ops.py:1205`), and no
  placeable-kind check exists inside `create_draft` itself to trigger such a
  fallback (the only placeable-kind enforcement found is
  `handlers/_placement.py`'s `link(rel='parent')` intercept, a different
  code path). In practice nothing currently rejects the placement, so the
  question can likely just resolve to "no such rule exists" — but the
  fallback as described would need a signature change not in this
  proposal's scope.
- **advisory** — `model: opus` is declared for what the "In scope" section
  itself describes as a mechanical rename + one kind-pinned-read swap +
  dual-read shim — CLAUDE.md's agent-sizing table places "a decided change
  or bounded op" (rename/dual-read, no new abstraction) in the Sonnet
  (`coder`) tier, not Opus. Not a correctness risk, just likely
  over-provisioned relative to the work as scoped.
- Positively verified (no issue): `dossier.py` does hardcode `quest_id` and
  `kind="quest"` exactly as claimed (`dossier_ref_id`/`ensure_dossier`/etc.,
  and the `store.get_ref(kind="quest", id=quest_id)` title lookup at
  `dossier.py:158`); the `dossier-of`/`has-dossier` pair
  (`store/types.py:137-143`, migration `0067_dossier_relation.sql`) carries
  no kind constraint, so the "no migration" claim holds structurally; and
  `meta.dossier_of_quest` is written at exactly one site
  (`dossier.py:164`), so the dual-read shim is not underspecified.

### Resolutions (2026-07-24) — all three blockers closed in the body above

- **get_ref(id=…) doesn't exist** → resolved in *In scope*: the title lookup
  becomes a direct `SELECT title FROM refs WHERE ref_id=%s` inside the
  connection the module already opens (no new `Store` method invented). No
  longer a "mechanical replace" of a nonexistent API.
- **Wrong caller set** → resolved in *In scope* + *Target + blast radius*:
  `_integration_view.py` dropped; the complete set is the three real callers
  (`tick.py`, `handlers/quest.py`, `cli/quest.py`).
- **Nonexistent test glob** → resolved in *Acceptance criteria*: the
  zero-behavior-change check now names `tests/test_quest_tick.py` (the actual
  home of the dossier-function tests), so it exercises real coverage.
- Advisories accepted: `model:` lowered to `sonnet` (mechanical rename +
  dual-read = `coder` tier); the `project_ref_id=None` escape hatch removed from
  OQ#2 (`create_draft` requires `int`), with the real build-time options noted.

**Status: draft (build deferred).** Blockers are resolved in the spec, but this
is a deferred deliverable — flip to `status: ready` (turning the human's key)
only when the owner-generalization is actually scheduled to build.
