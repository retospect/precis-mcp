-- 0021_register_renamed_perplexity_kinds — repair what 0018 missed.
--
-- Migration 0018 (kind rename sweep) used:
--   UPDATE kinds SET slug = 'perplexity-reasoning' WHERE slug = 'think';
--   UPDATE kinds SET slug = 'perplexity-research'  WHERE slug = 'research';
--
-- but the prod ``kinds`` table never held ``think`` / ``research`` rows
-- to begin with (those kinds existed as code-side registrations, not
-- as table entries), so the UPDATEs were no-ops. The handlers boot
-- fine — they self-register in the hub — but the runtime's kind
-- validator pulls from ``SELECT slug FROM kinds`` and rejected every
-- ``get(kind='perplexity-research', …)`` call with ``unknown kind``,
-- silently blocking the gather phase of the planner-coroutine cascade.
--
-- This forward migration inserts the two slugs explicitly, idempotent
-- on re-run.

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('perplexity-research', false,
     'Perplexity Sonar Deep Research',
     'Sonar Deep Research cache — slow-paced multi-step web research with citations; addressed by slug derived from the query'),
    ('perplexity-reasoning', false,
     'Perplexity Sonar Reasoning Pro',
     'Sonar Reasoning Pro cache — argumentative framing / open-question chasing; addressed by slug derived from the query')
ON CONFLICT (slug) DO NOTHING;
