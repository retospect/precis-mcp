-- 0033_news_kind.sql
--
-- Register `kind='news'` — multi-source news aggregation into the shared
-- corpus, plus the `news_sources` feed registry that the `news_poll`
-- ingestion worker walks.
--
-- Lineage: this replaces the retired `daily_briefing` / `rss_ingest`
-- stack that lived in the old monolith (`cluster`/`openclaw`) database.
-- Rather than a bespoke `news_items` table + a separate briefing DB, a
-- news article is now a first-class ref: fetched, block-split, embedded
-- and searchable like `web`/`wikipedia`. The morning briefing (see
-- `precis.workers.briefing`) reads recent `news` refs back out.
--
-- Anatomy (cf. 0026_wikipedia_kind.sql):
--   * `kinds.news`     — runtime kind validator (SELECT slug FROM kinds)
--   * `providers.news` — cache_state/refs.provider FK target
--   * `news_sources`   — operator-editable feed list the poller claims
--
-- Articles are URL-addressed (slug from the article title), pinned in
-- cache (ttl NULL — a news item is a historical record, not a TTL'd
-- lookup). Volume is bounded by `news_sources.max_items` per feed and a
-- retention sweep is left to a follow-up (tag `source:ephemeral` +
-- batched soft-delete) once real volume is observed.
--
-- Each article carries `category:news` + `source:<slug>` tags so it can
-- be filtered in/out of search without a hard fence (operator choice —
-- a dedicated NEWS fence axis can land later if news starts crowding
-- default cross-kind search).
--
-- Forward-only (ADR 0005). Idempotent under ON CONFLICT DO NOTHING.

BEGIN;

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('news', FALSE, 'News',
     'Multi-source news aggregation. Articles pulled from RSS/Atom feeds '
     '(the news_sources registry) by the news_poll worker, fetched + '
     'extracted + embedded like web pages, so search(kind=''news'', q=...) '
     'lands hits inside article bodies. URL-addressed, pinned in cache. '
     'Tagged category:news + source:<slug> for filtering. The morning '
     'briefing summarizes recent items back out. See ``precis-news-help``.')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO providers (slug, description) VALUES
    ('news', 'RSS / Atom news feeds (news_sources registry)')
ON CONFLICT (slug) DO NOTHING;

-- ── Feed registry the news_poll worker walks ──────────────────────────
CREATE TABLE IF NOT EXISTS news_sources (
    source_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url           text NOT NULL UNIQUE,        -- feed URL (RSS/Atom)
    title         text NOT NULL,               -- human label
    source_slug   text NOT NULL,               -- → source:<slug> tag (normalized, e.g. 'bbc')
    category      text,                         -- coarse bucket (world, tech, …)
    default_tags  text[] NOT NULL DEFAULT '{}', -- extra tags stamped on every item
    max_items     integer NOT NULL DEFAULT 50,  -- per-poll cap (firehose guard)
    enabled       boolean NOT NULL DEFAULT true,
    etag          text,                         -- conditional-GET cache validators
    last_modified text,
    last_polled_at  timestamptz,
    last_status     text,                       -- 'ok' | 'error: …'
    consecutive_errors integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE news_sources IS
    'Operator-editable RSS/Atom feed list for the news_poll worker. '
    'One row per feed; disable with enabled=false rather than deleting.';

-- Starter feeds — stable, key-free RSS endpoints. Operators add/remove
-- rows freely; these just keep the first poll from being a no-op.
INSERT INTO news_sources (url, title, source_slug, category, default_tags) VALUES
    ('https://feeds.bbci.co.uk/news/world/rss.xml', 'BBC News — World', 'bbc', 'world', '{}'),
    ('https://feeds.npr.org/1001/rss.xml',          'NPR — News',       'npr', 'world', '{}'),
    ('https://www.theguardian.com/world/rss',       'The Guardian — World', 'guardian', 'world', '{}'),
    ('https://feeds.arstechnica.com/arstechnica/index', 'Ars Technica', 'arstechnica', 'tech', '{topic:tech}'),
    ('https://hnrss.org/frontpage',                 'Hacker News — Front Page', 'hn', 'tech', '{topic:tech}')
ON CONFLICT (url) DO NOTHING;

COMMIT;

-- End of 0033_news_kind.sql
