-- 0365_edgar_watches.sql
-- Watches store the SEC FTS keyword parameters and accession checkpoints.

CREATE TABLE IF NOT EXISTS edgar_watches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    params jsonb NOT NULL CHECK (jsonb_typeof(params) = 'object' AND params <> '{}'),
    interval_s integer NOT NULL CHECK (interval_s > 0),
    last_run_at timestamptz,
    last_seen_accessions text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Persist downloaded bytes even when a filing fails to parse or a pass retries.
CREATE TABLE IF NOT EXISTS edgar_watch_downloads (
    downloaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    bytes bigint NOT NULL CHECK (bytes >= 0)
);

CREATE INDEX IF NOT EXISTS edgar_watch_downloads_time_idx ON edgar_watch_downloads (downloaded_at);
