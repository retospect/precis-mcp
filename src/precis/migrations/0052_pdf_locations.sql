-- 0052_pdf_locations.sql
--
-- Per-host presence ledger for held-paper PDFs.
--
-- The draft reader flags a cited paper whose PDF is *held* (`refs.pdf_sha256`
-- set) but whose file the web process can't find on disk (the red ▲). Deriving
-- that at request time by re-stat-ing the cite_key convention across the web
-- process's own `PRECIS_CORPUS_DIR` roots conflated two very different things:
-- "no node has this PDF" (worth chasing) and "this particular web host doesn't
-- mount the corpus" (a config fact, ADR 0029). Adding/removing a mount silently
-- changed the marker.
--
-- This table makes presence a corpus-wide fact. The `corpus_reconcile` worker
-- runs on every node (system profile); each node stats the held PDFs under its
-- own roots and records a *verdict* per (pdf_sha256, host):
--
--   * `path` = where this host found the file, or '' when it looked and the
--     file was absent (a checked-and-absent verdict — distinct from
--     never-checked, which is simply no row). Recording absence keeps a
--     genuinely-missing PDF from busy-looping the reconcile pass's "due" set.
--   * `seen_at` = when the verdict was taken. A verdict is trusted for
--     `PRECIS_PDF_LOCATION_TTL_DAYS` (default 7); the reconcile refresh cadence
--     is far shorter, so a live node keeps its rows fresh and an offline node's
--     rows lapse (its PDFs stop counting as held).
--
-- The reader's marker then reads only this table (`Store.pdf_missing`): a held
-- PDF is missing iff it has been checked (some row exists) yet no host holds a
-- fresh, non-empty copy. A never-checked sha is *unknown* — never flagged — so
-- the marker doesn't false-fire before the first sweep. Zero request-time FS
-- stats; the web host needs no corpus mount to render the marker.
--
-- FK to `pdfs(pdf_sha256)` with ON DELETE CASCADE: a location is meaningless
-- once the held PDF row is gone. `pdfs.pdf_sha256` is the table's PK (char(64)),
-- so the reference is to a unique key.
--
-- Forward-only (ADR 0005). Regenerate the baseline snapshot at release
-- (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS pdf_locations (
    pdf_sha256 char(64)    NOT NULL REFERENCES pdfs(pdf_sha256) ON DELETE CASCADE,
    host       text        NOT NULL,
    path       text        NOT NULL,
    seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pdf_sha256, host)
);

-- Presence lookups key on the sha (held-anywhere / missing predicates read
-- every host's row for one sha).
CREATE INDEX IF NOT EXISTS pdf_locations_sha_idx
    ON pdf_locations (pdf_sha256);

COMMIT;

-- End of 0052_pdf_locations.sql
