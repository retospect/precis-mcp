-- 0193_pcb_notes.sql
--
-- docs/backlog/pcb-argue-with-design.md slice 1: the pcb design's
-- argument ledger. The human-facing board page (/pcb/{slug}) grows one
-- text box that accumulates clickable handles (REFDES, REFDES.PIN,
-- net:NAME, feature:FTYPE); submitting it stores the typed text verbatim
-- as a `question` note and the LLM's reply as an `answer` note anchored
-- to the same handles.
--
-- Column-for-column the `se_notes` shape (precis_se/0005_se_notes_freedom.sql
-- is the model; checklist_notes in 0158 is the other core copy), served
-- by src/precis/utils/notes.py's NoteSpec. Name-keyed text throughout:
-- `re` names the note this one answers; `about` holds the handles, which
-- resolve against pcb_instances / pcb_pins / pcb_nets / pcb_features at
-- READ time — a handle naming a since-retired part reads as a dangling
-- anchor report, never a crash. Append-only: retire sets retired_at.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_notes (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id      bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name        text   NOT NULL,
    kind        text   NOT NULL
        CHECK (kind IN ('question', 'answer', 'decision')),
    body        text   NOT NULL,
    re          text,
    about       jsonb  NOT NULL DEFAULT '[]'::jsonb,
    origin      text   NOT NULL DEFAULT 'user',
    created_at  timestamptz NOT NULL DEFAULT now(),
    retired_at  timestamptz
);

COMMENT ON TABLE pcb_notes IS
    'pcb design argument ledger (pcb-argue-with-design.md slice 1): the '
    'se_notes shape verbatim. body is what the user typed, byte for byte; '
    'about lists the handles (REFDES | REFDES.PIN | net:NAME | '
    'feature:FTYPE) parsed out of it, resolved at read time (dangling '
    'anchors are a report, never a write-time error). re names the '
    'question an answer/decision settles. origin: user | proposed (the '
    'LLM''s answer). Append-only: retire sets retired_at.';

CREATE INDEX IF NOT EXISTS pcb_notes_ref_idx ON pcb_notes (ref_id);
CREATE UNIQUE INDEX IF NOT EXISTS pcb_notes_live_name_idx
    ON pcb_notes (ref_id, name) WHERE retired_at IS NULL;

COMMIT;

-- End of 0193_pcb_notes.sql
