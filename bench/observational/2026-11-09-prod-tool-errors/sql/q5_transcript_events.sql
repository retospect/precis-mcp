-- q5: tool_use / tool_result events from cluster-job transcripts (refs.meta.transcript, stream-json lines).
-- Only source that keeps the full ack text incl. its `next:` lines. Emits ref_id|ord|line, filtered server-side.
SELECT ref_id || '|' || ord || '|' || line
FROM (
  SELECT r.ref_id, x.ord, x.line
  FROM refs r,
       LATERAL regexp_split_to_table(r.meta->>'transcript', E'\n') WITH ORDINALITY AS x(line, ord)
  WHERE r.kind = 'job' AND r.meta ? 'transcript'
    AND r.created_at >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND r.created_at < TIMESTAMPTZ '2026-11-09 22:20:00+00'
) s
WHERE line ~ '"type": ?"tool_(use|result)"' AND (line ~ 'mcp__precis__' OR line ~ 'tool_result')
ORDER BY ref_id, ord;
