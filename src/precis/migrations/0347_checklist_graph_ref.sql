-- 0347_checklist_graph_ref.sql

BEGIN;

CREATE TABLE checklist_graph_refs (
    checklist_id bigint PRIMARY KEY REFERENCES checklists (id) ON DELETE CASCADE,
    ref_id bigint NOT NULL UNIQUE REFERENCES refs (ref_id) ON DELETE CASCADE
);

COMMENT ON TABLE checklist_graph_refs IS
    'Lazily allocated endpoint for definition links. Checklist names stay in '
    'their own registry and do not consume global cite keys.';

COMMIT;
