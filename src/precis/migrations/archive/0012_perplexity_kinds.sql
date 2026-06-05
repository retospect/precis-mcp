-- ===========================================================================
-- 0012_perplexity_kinds.sql — seed the three perplexity-backed kinds.
--
-- ``PerplexityHandler`` (in ``src/precis/handlers/perplexity.py``)
-- registers three sub-kinds: ``websearch`` / ``think`` / ``research``.
-- Each stores cached responses via ``Store.put_cache_entry`` → which
-- calls ``insert_ref`` → which validates against the ``kinds`` table.
-- None of them were in the 0001 seed, so any cache write raised
-- ``BadInput: unknown kind`` at the validation gate.
--
-- Surface impact: identical to migration 0011 — silent in production
-- (no one had exercised the cached path) but surfaced as ~24 test
-- failures in ``tests/test_perplexity.py`` once the PG-gated suite
-- started running against the shared precis_test DB.
--
-- All three are slug-addressed (cache_key-derived). See
-- ``src/precis/handlers/perplexity.py:431-495`` for the per-kind
-- KindSpec definitions.
-- ===========================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('websearch', FALSE, 'Web search',
     'Cached perplexity-style web search response. Slug derived from '
     'the canonical query + model + freshness window. See '
     'src/precis/handlers/perplexity.py.'),
    ('think',     FALSE, 'Think',
     'Cached perplexity ``think`` (chain-of-thought) response. Slug '
     'derived from the question + model + freshness window. See '
     'src/precis/handlers/perplexity.py.'),
    ('research',  FALSE, 'Research report',
     'Cached perplexity ``research`` (deep-research) response. Slug '
     'derived from the prompt + model + freshness window. See '
     'src/precis/handlers/perplexity.py.')
ON CONFLICT (slug) DO NOTHING;

-- ===========================================================================
-- End of 0012_perplexity_kinds.sql
-- ===========================================================================
