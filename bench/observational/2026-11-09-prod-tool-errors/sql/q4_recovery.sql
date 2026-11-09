-- q4: error -> next call / first success on same verb+kind, per session (ledger only).
-- Sessions = tool_calls.session_id (one per MCP connection), ordered by seq. Rows without session_id are excluded.
WITH w AS (
  SELECT call_id, session_id, seq, ts, verb, coalesce(kind,'(none)') AS kind, outcome, error_type,
    CASE WHEN client LIKE 'asa-bot%' THEN 'asa-bot'
         WHEN client LIKE 'claude-code/%' AND coalesce(source,'')<>'' THEN 'claude-code job-sourced (cluster agent)'
         WHEN client LIKE 'claude-code/%' THEN 'claude-code no job source (dev/interactive)'
         WHEN client LIKE 'codex%' THEN 'codex'
         WHEN client LIKE 'kimi%' THEN 'kimi'
         WHEN client LIKE 'devin%' THEN 'devin'
         WHEN client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' THEN 'synthetic/probe/fleet-report'
         WHEN coalesce(client,'')='' THEN 'no client label (in-proc/CLI)'
         ELSE 'other client (rmcp, mcp)' END AS ct
  FROM tool_calls
  WHERE session_id IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), b AS (
  SELECT w.*,
    row_number() OVER (PARTITION BY session_id ORDER BY seq, call_id) AS rn,
    row_number() OVER (PARTITION BY session_id, verb, kind ORDER BY seq, call_id) AS vk_rn
  FROM w
), c AS (
  SELECT b.*,
    lead(verb)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_verb,
    lead(kind)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_kind,
    lead(outcome) OVER (PARTITION BY session_id ORDER BY rn) AS nx_outcome,
    lag(outcome)  OVER (PARTITION BY session_id, verb, kind ORDER BY rn) AS prev_vk_outcome,
    min(rn)    FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_rn,
    min(vk_rn) FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_vk_rn
  FROM b
), e AS (
  SELECT *, (nx_verb IS NOT NULL) AS has_next,
         (nx_verb = verb AND nx_kind = kind) AS next_same_vk,
         (prev_vk_outcome IS DISTINCT FROM 'error') AS is_episode_start,
         (succ_rn - rn) AS calls_to_ok, (succ_vk_rn - vk_rn) AS vk_attempts_to_ok
  FROM c WHERE outcome='error'
)
SELECT coalesce(ct,'ALL') AS ct, coalesce(error_type,'ALL') AS error_type,
  count(*) AS n_err,
  count(*) FILTER (WHERE has_next) AS n_with_next_call,
  round(100.0*count(*) FILTER (WHERE next_same_vk)/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk_ok,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE next_same_vk),0),1) AS pct_ok_given_same_vk,
  count(*) FILTER (WHERE is_episode_start) AS n_episodes,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND succ_rn IS NOT NULL)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_episodes_recovered,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS med_vk_attempts,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS p90_vk_attempts,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS med_calls,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS p90_calls,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok=1)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_on_next_same_vk,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok<=3)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_within_3_same_vk
FROM e
GROUP BY GROUPING SETS ((), (ct), (error_type))
ORDER BY (ct IS NULL AND error_type IS NULL) DESC, ct NULLS FIRST, n_err DESC;
