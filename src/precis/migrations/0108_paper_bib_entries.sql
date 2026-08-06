-- 0108_paper_bib_entries.sql
--
-- Base slice of the citation-resolution work
-- (docs/proposals/citation-bib-parse.md; siblings citation-sources-tab.md,
-- citation-taproot-resolve.md are `blocked-by` this). Reference lists are
-- already ingested as chunk text (markdown lines like
-- `- [126] Z. Ali, ... ChemCatChem 2020, 12, 360.`, with chunk-overlap
-- duplicates) but nothing parses them or resolves them to paper
-- identities. This table turns each held paper's bibliography into
-- structured rows: `marker -> fields -> DOI -> held_ref_id` (when we hold
-- the cited paper).
--
-- `ref_id` naming matches sibling `s2_neighbors` (migration 0106) — the
-- paper whose bibliography this entry belongs to. `marker` is the
-- numeric-bracket citation number (`[126]`); unique per `(ref_id,
-- marker)` — the `bib_parse` worker pass dedupes chunk-overlap duplicate
-- markers before insert (first occurrence wins), so a collision here
-- would mean a pass bug, not expected data.
--
-- `authors`/`journal`/`year`/`volume`/`first_page` are the regex/LLM
-- -extracted bibliographic fields (ACS/Wiley shape); `doi`/`s2_id` are the
-- resolved-identity columns the matcher fills in (local DOI-exact against
-- this paper's own `s2_neighbors` rows, else a Crossref bibliographic
-- query) — NULL until (or unless) matched. `held_ref_id` is resolved
-- against `ref_identifiers` once a `doi` is known (NULL means we don't
-- hold the cited paper, or no match was ever found).
--
-- `parse_conf` / `match_conf` are independent confidence scores (parse:
-- how sure the field extraction is; match: how sure the identity
-- resolution is) — either may be non-NULL while the other is NULL. A
-- non-NULL `match_conf` also doubles as the matcher's own memoization
-- marker (attempted, whether or not it resolved a `doi`) so a later pass
-- doesn't re-query Crossref for the same entry; re-matching only happens
-- on a `parse_version` bump.
--
-- `parse_version` mirrors the `bib_parse` worker's own `BIB_PARSE_VERSION`
-- constant (stamped identically onto the parent paper's
-- `refs.meta.bib_parse_version` for the paper-level convergence check —
-- see `workers/bib_parse.py`); bumping it re-parses (and re-matches) the
-- corpus lazily, same discipline as `paper_glossary`'s
-- `meta.glossary_version`.
--
-- `id` is a plain surrogate PK (not composite) so the sibling
-- citation-taproot-resolve slice's `chunk_citations.bib_entry_id` has a
-- single-column FK target.
--
-- Forward-only (ADR 0005). Idempotent (`IF NOT EXISTS`). Regenerate the
-- baseline snapshot at release time (ADR 0031): `scripts/bump` /
-- `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS paper_bib_entries (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_id        bigint      NOT NULL
        REFERENCES refs (ref_id) ON DELETE CASCADE,
    marker        int         NOT NULL,
    raw_text      text        NOT NULL,
    authors       text,
    journal       text,
    year          int,
    volume        text,
    first_page    text,
    doi           text,
    s2_id         text,
    held_ref_id   bigint
        REFERENCES refs (ref_id) ON DELETE SET NULL,
    parse_conf    real,
    match_conf    real,
    parse_version int         NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT paper_bib_entries_ref_marker_uniq UNIQUE (ref_id, marker)
);

-- The matcher's per-paper local DOI-exact step + the sweep that finds
-- entries still needing a match attempt (`match_conf IS NULL`).
CREATE INDEX IF NOT EXISTS paper_bib_entries_unmatched_idx
    ON paper_bib_entries (ref_id)
    WHERE match_conf IS NULL;

-- "Who cites held paper X via its parsed bibliography?" — mirrors
-- `s2_neighbors_held_ref_id_idx`; partial since most rows are non-held.
CREATE INDEX IF NOT EXISTS paper_bib_entries_held_ref_id_idx
    ON paper_bib_entries (held_ref_id)
    WHERE held_ref_id IS NOT NULL;

COMMIT;

-- End of 0108_paper_bib_entries.sql
