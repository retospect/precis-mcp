-- 0354_retraction_review.sql
CREATE TABLE IF NOT EXISTS retraction_notices (
    notice_id bigint PRIMARY KEY REFERENCES refs(ref_id) ON DELETE CASCADE,
    notice_date date,
    notice_doi text,
    source_summary text,
    nature text NOT NULL,
    found_at timestamptz NOT NULL DEFAULT now(),
    analysis text,
    analysed_by text
);

CREATE TABLE IF NOT EXISTS retraction_notice_sources (
    source text NOT NULL,
    source_id text NOT NULL,
    notice_id bigint NOT NULL REFERENCES retraction_notices(notice_id) ON DELETE CASCADE,
    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS retraction_notice_sources_notice_idx ON retraction_notice_sources (notice_id);

CREATE TABLE IF NOT EXISTS retraction_use_verdicts (
    notice_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    paper_id bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    chunk_id bigint NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    text_hash text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('unaffected', 'affects')),
    reason text NOT NULL,
    judged_by text NOT NULL,
    judged_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    closed_by text,
    resolution text,
    PRIMARY KEY (chunk_id, paper_id, notice_id, text_hash)
);

CREATE INDEX IF NOT EXISTS retraction_use_verdicts_notice_idx ON retraction_use_verdicts (notice_id);
CREATE INDEX IF NOT EXISTS retraction_use_verdicts_paper_idx ON retraction_use_verdicts (paper_id);
