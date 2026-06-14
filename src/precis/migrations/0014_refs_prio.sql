-- 0014_refs_prio.sql
--
-- Slice 4 of docs/design/todo-tree-plan.md. Adds a `prio` column to
-- refs so the doable view's ORDER BY can sort on a small int instead
-- of joining through ref_tags + tags for a closed-prefix PRIO:
-- vocabulary. PRIO is a sort key on every doable read; the relational
-- answer (`r.prio ASC`) beats the join path on every dimension
-- (clarity, query plan, write surface).
--
-- Range: 1..10. 1 = preempts strategic rotation (chatter / user
-- writes), 2 = cron / recurring tick spawn, 3..10 = the 1/N strategic
-- share. The doable ORDER BY becomes:
--   COALESCE(r.prio, 5) ASC,   -- default to 5 when unset
--   strategic_picks_7d ASC,    -- 1/N share within PRIO ties
--   r.ref_id ASC
--
-- NULL semantics: `NULL` means "no explicit priority — use the
-- kind's default at sort time" (5 today). Most existing refs (papers,
-- memories, etc.) never sort on prio, so leaving them NULL costs
-- nothing.
--
-- Partial index on `prio IS NOT NULL` so non-todo workloads pay no
-- index-maintenance cost; the rows that get sorted on prio (todo
-- leaves) all carry a value.
--
-- Backwards compatibility: the closed-prefix `PRIO:` tag stays
-- valid at the handler boundary — TodoHandler.put / .tag translate
-- `PRIO:urgent` → `prio=1`, `PRIO:high` → `prio=3`, etc. so existing
-- skills, tests, and any cached agent prompts that still write the
-- tag form keep working. New code writes `put(prio=N)` directly.
--
-- Forward-only (ADR 0005). Additive; existing rows get NULL and
-- behave exactly as before.

BEGIN;

ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS prio SMALLINT
        CHECK (prio IS NULL OR prio BETWEEN 1 AND 10);

CREATE INDEX IF NOT EXISTS refs_prio_idx
    ON refs (prio)
    WHERE prio IS NOT NULL;

COMMIT;

-- End of 0014_refs_prio.sql
