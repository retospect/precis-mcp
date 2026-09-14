-- 0160_pcb_local_footprints.sql
--
-- pcb-ewod-multitile Slice 1 ("authored copper footprints"): an authoring
-- path for real pad geometry with no LCSC part. ``part_footprints`` (0047)
-- is the wrong home — it is a GLOBAL catalog cache keyed by C-number and
-- FK-free by design (the catalog-swap boundary, 0047's own header), so a
-- design-local name like "ewod_pad" would either collide across unrelated
-- designs or need a synthetic namespacing hack. This is a new, ref_id-
-- scoped table instead: same row shape as ``part_footprints``'
-- ``{pads, pin_map, courtyard, centroid}`` (the exact dict
-- :func:`precis.pcb.padplace.place_footprint_pads` already consumes), so
-- every downstream reader (padplace/realize/gerber) takes either source
-- through one shape. ``pcb_components.footprint`` (already a free-text
-- snapshot column, 0047) is the join key when ``part_lcsc IS NULL`` — no
-- new column needed there.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot
-- after merge (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pcb_local_footprints (
    ref_id     bigint NOT NULL REFERENCES refs (ref_id) ON DELETE CASCADE,
    name       text   NOT NULL,
    pads       jsonb  NOT NULL,
    pin_map    jsonb,
    courtyard  jsonb,
    centroid   jsonb,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, name)
);

COMMENT ON TABLE pcb_local_footprints IS
    'Design-local authored footprints (pcb-ewod-multitile Slice 1) -- '
    'named pad geometry for an instance with no LCSC part, keyed by '
    '(ref_id, name); joined via pcb_components.footprint. Same {pads, '
    'pin_map, courtyard, centroid} row shape as the global part_footprints '
    'cache, so padplace/realize/gerber read either source identically.';

COMMIT;

-- End of 0160_pcb_local_footprints.sql
