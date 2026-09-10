-- 0158_checklist_kind.sql
--
-- The `checklist` kind — Checklist-Manifesto gates for LLM agents
-- (design-of-record docs/backlog/checklist-kind.md, slice 1: "checklist
-- kind core"). A checklist is a named, versioned set of items; a target
-- (any ref — pcb, cad, se, ...) is explicitly assigned one or more
-- checklists, and per-target/per-item **verdicts** accumulate instead of
-- restarting. Lives in core (not a plugin) because pcb — the first
-- consumer — is core and core must not import plugins.
--
-- FIVE TABLES, disjoint write ownership (git owns shipped item content,
-- the DB owns local items + all operational state):
--
--   * `checklists`            — a named, versioned definition. NOT a
--     `refs` row: like `rxn_properties`, this is a bespoke registry
--     table, addressed by `name`, not folded into the generic ref graph.
--   * `checklist_items`       — append-only per checklist: `UNIQUE
--     (checklist_id, name, rev)`, current = highest live rev. An edit
--     (handler `edit`) or a deploy sync both insert rev+1; sync never
--     rewrites, so a rollback deploy just appends a rev matching older
--     content and pinned verdicts stay valid. `origin` is a property of
--     the REV, not the name (local rev N -> shipped rev N+1 is an
--     ordinary linear sequence, no retire-and-recreate promotion race).
--   * `checklist_assignments` — what makes silence honest: an assigned
--     target with no verdicts renders every item "not checked"; without
--     an assignment row, "no rows" would be indistinguishable from
--     "nothing to check".
--   * `checklist_verdicts`    — the ledger; append-only (a re-check is a
--     new row, never an UPDATE). `item_rev` pins what was judged — a
--     verdict whose `item_rev` is below the item's current live rev is
--     the staleness signal. `fingerprint` is an OPAQUE caller-supplied
--     string in slice 1 (stored, compared, rendered — never computed;
--     pcb starts computing it in slice 2).
--   * `checklist_notes`       — the `se_notes` shape verbatim (lifted to
--     `src/precis/utils/notes.py`, precis_se/0005_se_notes_freedom.sql
--     is the model): name-keyed (never row-id FK), append-only
--     (corrections are new notes), `kind` one of
--     question|answer|decision, open/settled derived at read time,
--     dangling `about` anchors reported at read time not rejected at
--     write time. Carries `checklist_id` (unlike se_notes, which has
--     only one checklist per design) because a target can carry
--     multiple assigned checklists that might define the same item name.
--
-- Three-valued honesty (non-negotiable): a target+checklist with no run
-- renders "not checked", never "clean" — same lesson as
-- `Store.pcb_drc_findings_latest` / `netlist_drc_clean.evaluate`.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

-- 1. the ref kind ------------------------------------------------------
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('checklist', FALSE, 'Checklist',
     'A named, versioned check ledger — Checklist-Manifesto-style '
     'argued gates for LLM agents. Items are judgment tasks or bridges '
     'to a domain''s own encoded rules (DRC/ERC); per-target verdicts '
     'accumulate instead of restarting, and staleness (item revised, '
     'target changed) is rendered honestly rather than silently '
     'dropped. See precis-checklist-help.')
ON CONFLICT (slug) DO NOTHING;

-- 2. checklist definitions -----------------------------------------------
CREATE TABLE IF NOT EXISTS checklists (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    origin      text NOT NULL DEFAULT 'local'
        CHECK (origin IN ('shipped', 'local')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz
);

COMMENT ON TABLE checklists IS
    'Checklist definitions, addressed by name. origin=shipped checklists '
    'are created only by jobs/checklist_sync.py (from '
    'src/precis/data/checklists/*.yaml); origin=local checklists are '
    'created by put(kind=''checklist''). No hand-maintained version '
    'number here — git holds file history, checklist_items.rev holds '
    'item history.';

-- 3. checklist items — append-only, revving ------------------------------
CREATE TABLE IF NOT EXISTS checklist_items (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checklist_id    bigint NOT NULL REFERENCES checklists (id) ON DELETE CASCADE,
    name            text NOT NULL,
    rev             integer NOT NULL,
    phase           text,
    severity        text NOT NULL DEFAULT 'advisory'
        CHECK (severity IN ('blocking', 'advisory')),
    decidability    text NOT NULL DEFAULT 'judgment'
        CHECK (decidability IN ('tool', 'judgment')),
    -- Mandatory in the handler (the cargo-cult filter: no failure
    -- statement, no item) — nullable here only because a CHECK can't
    -- distinguish "empty string" cleanly across every caller path; the
    -- handler enforces non-empty.
    prevents        text,
    applies         text,
    body            text,
    origin          text NOT NULL DEFAULT 'local'
        CHECK (origin IN ('shipped', 'local')),
    -- NULL = every target this checklist is assigned to. Non-NULL scopes
    -- a local item to one target (a board-specific concern that doesn't
    -- belong in the shared definition).
    target_ref_id   bigint REFERENCES refs (ref_id) ON DELETE CASCADE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    retired_at      timestamptz,
    UNIQUE (checklist_id, name, rev)
);

COMMENT ON TABLE checklist_items IS
    'Append-only per (checklist_id, name): current = the highest-rev live '
    '(retired_at IS NULL) row. An edit() or a deploy sync both insert '
    'rev+1 rather than rewriting; an item removed from a shipped file is '
    'retired, never deleted. origin is a property of the REV (not the '
    'name), so a local item promoted into the shipped file becomes the '
    'next rev in the same sequence — ordinary staleness, no retire-and-'
    'recreate. edit() REJECTS writes when the current live rev''s origin '
    'is ''shipped'' (git owns that content); local items always edit '
    'freely.';

CREATE INDEX IF NOT EXISTS checklist_items_live_idx
    ON checklist_items (checklist_id, name)
    WHERE retired_at IS NULL;

-- FK-cascade coverage for target_ref_id (checklist_id's own FK is already
-- covered by the UNIQUE (checklist_id, name, rev) index above, whose
-- leading column is checklist_id and which isn't partial). target_ref_id
-- is nullable (NULL = every target), so the covering index is partial on
-- the exact "column IS NOT NULL" predicate — a DELETE/UPDATE on refs only
-- ever cascades into the non-NULL rows anyway.
CREATE INDEX IF NOT EXISTS checklist_items_target_ref_id_idx
    ON checklist_items (target_ref_id)
    WHERE target_ref_id IS NOT NULL;

-- 4. assignments — what makes silence honest ------------------------------
CREATE TABLE IF NOT EXISTS checklist_assignments (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_ref_id  bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    checklist_id   bigint NOT NULL REFERENCES checklists (id) ON DELETE CASCADE,
    created_at     timestamptz NOT NULL DEFAULT now(),
    retired_at     timestamptz
);

COMMENT ON TABLE checklist_assignments IS
    'target_ref_id is explicitly assigned checklist_id. The assignment '
    'row is what makes silence honest: an assigned target with zero '
    'verdicts renders every item "not checked"; with no assignment row '
    'at all, get() renders "no checklist assigned", never empty-clean. '
    'v1 (slice 1) is explicit-only; kind-level default assignments '
    '(every pcb gets pcb-tapeout) arrive in slice 2.';

CREATE UNIQUE INDEX IF NOT EXISTS checklist_assignments_live_idx
    ON checklist_assignments (target_ref_id, checklist_id)
    WHERE retired_at IS NULL;

-- FK-cascade coverage: the live-assignment index above is partial on
-- retired_at, which doesn't entail either FK column non-NULL, so both
-- sides need their own unqualified covering index.
CREATE INDEX IF NOT EXISTS checklist_assignments_target_ref_id_idx
    ON checklist_assignments (target_ref_id);
CREATE INDEX IF NOT EXISTS checklist_assignments_checklist_id_idx
    ON checklist_assignments (checklist_id);

-- 5. the verdict ledger — no "run" entity ----------------------------------
CREATE TABLE IF NOT EXISTS checklist_verdicts (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_ref_id  bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    checklist_id   bigint NOT NULL REFERENCES checklists (id) ON DELETE CASCADE,
    item_name      text NOT NULL,
    item_rev       integer NOT NULL,
    verdict        text NOT NULL
        CHECK (verdict IN ('pass', 'fail', 'n/a', 'waived')),
    evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- OPAQUE in slice 1: stored, compared, rendered — never computed
    -- here. A per-kind fingerprint protocol hook (pcb calling directly
    -- in slice 2) is explicitly out of scope for this migration.
    fingerprint    text,
    checked_at     timestamptz NOT NULL DEFAULT now(),
    checked_by     text,
    retired_at     timestamptz
);

COMMENT ON TABLE checklist_verdicts IS
    'The ledger is the primitive; "a run" is the current view over it — '
    'no run-as-row. Append-only: a re-check inserts a new row (never an '
    'UPDATE), the prior row stays queryable. item_rev pins what was '
    'judged: item_rev < the item''s current live rev is the staleness '
    'signal (item changed since check). fingerprint is caller-supplied '
    'and opaque in slice 1 (never computed by this migration''s code) — '
    'staleness-by-target-change activates once a kind starts supplying '
    'a real one. Status per (target, checklist, item_name) = the '
    'live row with the latest checked_at.';

CREATE INDEX IF NOT EXISTS checklist_verdicts_latest_idx
    ON checklist_verdicts (target_ref_id, checklist_id, item_name, checked_at DESC)
    WHERE retired_at IS NULL;

-- FK-cascade coverage: the latest-lookup index above is partial on
-- retired_at, which doesn't entail either FK column non-NULL, so
-- checklist_id (not the index's leading column, and not otherwise
-- covered) needs its own unqualified index too. target_ref_id IS the
-- leading column above but that index is still partial, so it needs one
-- as well.
CREATE INDEX IF NOT EXISTS checklist_verdicts_target_ref_id_idx
    ON checklist_verdicts (target_ref_id);
CREATE INDEX IF NOT EXISTS checklist_verdicts_checklist_id_idx
    ON checklist_verdicts (checklist_id);

-- 6. argument threads — the se_notes shape, verbatim -----------------------
CREATE TABLE IF NOT EXISTS checklist_notes (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_ref_id  bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    checklist_id   bigint NOT NULL REFERENCES checklists (id) ON DELETE CASCADE,
    -- NULL = a note about the checklist run as a whole, not one item.
    item_name      text,
    name           text NOT NULL,
    kind           text NOT NULL
        CHECK (kind IN ('question', 'answer', 'decision')),
    body           text NOT NULL,
    re             text,
    about          jsonb NOT NULL DEFAULT '[]'::jsonb,
    origin         text NOT NULL DEFAULT 'user',
    created_at     timestamptz NOT NULL DEFAULT now(),
    retired_at     timestamptz
);

COMMENT ON TABLE checklist_notes IS
    'Per-item (or per-run, when item_name IS NULL) argument threads — the '
    'se_notes shape verbatim (precis_se/0005_se_notes_freedom.sql is the '
    'model), lifted to src/precis/utils/notes.py so both kinds share one '
    'implementation. Name-keyed: re names the note this one '
    'answers/decides; about anchors sub-objects of the TARGET (e.g. pcb '
    'refdes/net names) or another item name — resolved at read time, '
    'dangling anchors reported not rejected, same as se. carries '
    'checklist_id (unlike se_notes, which has exactly one checklist per '
    'design) because a target can carry multiple assigned checklists '
    'that might define the same item name. Append-only: remove_note sets '
    'retired_at rather than deleting.';

CREATE INDEX IF NOT EXISTS checklist_notes_live_idx
    ON checklist_notes (target_ref_id, checklist_id)
    WHERE retired_at IS NULL;

-- FK-cascade coverage: the live-notes index above is partial on
-- retired_at, which doesn't entail either FK column non-NULL, so both
-- sides need their own unqualified covering index.
CREATE INDEX IF NOT EXISTS checklist_notes_target_ref_id_idx
    ON checklist_notes (target_ref_id);
CREATE INDEX IF NOT EXISTS checklist_notes_checklist_id_idx
    ON checklist_notes (checklist_id);

COMMIT;

-- End of 0158_checklist_kind.sql
