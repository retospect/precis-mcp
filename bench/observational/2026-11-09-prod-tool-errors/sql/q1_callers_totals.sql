-- q1: calls / errors / sessions per caller type, plus grand total and per-verb totals (second statement run separately).
WITH w AS (
  SELECT *, CASE WHEN client LIKE 'asa-bot%' THEN 'asa-bot'
                 WHEN client LIKE 'claude-code/%' AND coalesce(source,'')<>'' THEN 'claude-code job-sourced (cluster agent)'
                 WHEN client LIKE 'claude-code/%' THEN 'claude-code no job source (dev/interactive)'
                 WHEN client LIKE 'codex%' THEN 'codex'
                 WHEN client LIKE 'kimi%' THEN 'kimi'
                 WHEN client LIKE 'devin%' THEN 'devin'
                 WHEN client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' THEN 'synthetic/probe/fleet-report'
                 WHEN coalesce(client,'')='' THEN 'no client label (in-proc/CLI)'
                 ELSE 'other client (rmcp, mcp)' END AS ct
  FROM tool_calls
  WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
)
SELECT ct, count(*) n_calls, count(*) FILTER (WHERE outcome='error') n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) err_pct,
       count(DISTINCT session_id) n_sessions,
       count(DISTINCT session_id) FILTER (WHERE outcome='error') n_sess_with_err
FROM w GROUP BY ROLLUP(ct) ORDER BY n_calls DESC;

-- per-verb totals (run as its own statement):
-- SELECT verb, count(*) n_calls, count(*) FILTER (WHERE outcome='error') n_err,
--        round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) err_pct
-- FROM tool_calls WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
-- GROUP BY ROLLUP(1) ORDER BY n_calls DESC;
