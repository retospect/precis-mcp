-- 0019_chunk_kind_job_result — register chunk_kind='job_result' + 'tag_overflow'.
--
-- T1.6 introduced ``chunk_kind='job_result'`` for the per-tick audit
-- chunk the planner-coroutine writes from the worker's stdout; T3.2
-- introduced ``chunk_kind='tag_overflow'`` for the long-tag-value
-- redirect chunk that the todo handler emits when a tag value exceeds
-- 80 chars. Both code paths went out without the corresponding
-- chunk_kinds row, so the chunk INSERT trips the FK every time a
-- plan_tick finalises. This migration registers them both so the
-- worker can write the audit chunk and the cascade can proceed.
--
-- Idempotent: ``ON CONFLICT (slug) DO NOTHING`` so re-running on an
-- already-migrated DB is a no-op.

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('job_result',
     FALSE,
     'Per-tick audit chunk written by the planner-coroutine '
     'when a plan_tick job finalises (verdict + summary + files). '
     'Read by the parent todo''s next tick for context.'),
    ('tag_overflow',
     FALSE,
     'Long tag-value redirect chunk: when a put attempts to land a '
     'tag value longer than 80 chars in a redirectable namespace '
     '(ask-user / halt), the full value lands here and the tag '
     'becomes ``<ns>:see-chunk-<pos>``.')
ON CONFLICT (slug) DO NOTHING;
