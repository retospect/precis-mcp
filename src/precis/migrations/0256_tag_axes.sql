-- 0256_tag_axes.sql
--
-- Display-rank registry for tag axes (gr520321, ruling in comment 1).
-- Neighbour rows and search hits render a ref's tags as chips ordered by
-- a per-axis rank; ``tags`` itself is just (namespace, value) with no
-- registry, so the axis order lives here. ``display_rank`` is UNIQUE so
-- the sort key never ties across axes; the categorizer axes each take
-- their own row for that reason. ``OPEN`` is the sentinel namespace the
-- store gives free-form tags (``_tags_ops``), so rank 4 covers every
-- un-prefixed tag; an axis with no row here sorts at 4.5 — after
-- free-form, before the system tail (SRC onward).
--
-- Seed order = the ruling's "most informative first": lifecycle status,
-- priority, audit flags, then free-form tags, then provenance, the
-- categorizer axes, and finally the partition/housekeeping tail
-- (SPACE … STALE).
--
-- Forward-only (ADR 0005). Idempotent: ON CONFLICT DO NOTHING.

BEGIN;

CREATE TABLE IF NOT EXISTS tag_axes (
    namespace text PRIMARY KEY,
    display_rank integer UNIQUE NOT NULL
);

INSERT INTO tag_axes (namespace, display_rank) VALUES
    ('STATUS',     1),
    ('PRIO',       2),
    ('AUDIT',      3),
    ('OPEN',       4),
    ('SRC',        5),
    ('DOMAIN',     6),
    ('MATERIAL',   7),
    ('PROPERTY',   8),
    ('STUDYTYPE',  9),
    ('SCALE',     10),
    ('DIM',       11),
    ('TRANSPORT', 12),
    ('SPACE',     13),
    ('MOVE',      14),
    ('CACHE',     15),
    ('WATCH',     16),
    ('DREAM',     17),
    ('STALE',     18)
ON CONFLICT (namespace) DO NOTHING;

COMMIT;

-- End of 0256_tag_axes.sql
