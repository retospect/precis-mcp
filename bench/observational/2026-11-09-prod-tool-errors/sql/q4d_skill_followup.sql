-- q4d: ledger-scale proxy for following a `next: get(kind='skill', ...)` pointer: after an error,
-- is the immediately next call (or one of the next 3) a get on kind=skill? Agent clients only.
-- Baseline = share of all agent-client calls that are get(kind=skill).
WITH w AS (
  SELECT call_id, session_id, seq, verb, coalesce(kind,'(none)') AS kind, outcome
  FROM tool_calls
  WHERE session_id IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
    AND NOT (client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='')
), c AS (
  SELECT w.*,
    (lead(verb)  OVER s = 'get' AND lead(kind)  OVER s = 'skill') AS n1_skill,
    (lead(verb,2) OVER s = 'get' AND lead(kind,2) OVER s = 'skill') AS n2_skill,
    (lead(verb,3) OVER s = 'get' AND lead(kind,3) OVER s = 'skill') AS n3_skill,
    lead(verb) OVER s AS nx
  FROM w WINDOW s AS (PARTITION BY session_id ORDER BY seq, call_id)
)
SELECT count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL) AS n_err_with_next,
  round(100.0*count(*) FILTER (WHERE outcome='error' AND n1_skill)/nullif(count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL),0),1) AS pct_next_is_skill_read,
  round(100.0*count(*) FILTER (WHERE outcome='error' AND (n1_skill OR n2_skill OR n3_skill))/nullif(count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL),0),1) AS pct_skill_read_within_3,
  round(100.0*count(*) FILTER (WHERE outcome='ok' AND n1_skill)/nullif(count(*) FILTER (WHERE outcome='ok' AND nx IS NOT NULL),0),1) AS baseline_pct_next_is_skill_read_after_ok,
  round(100.0*count(*) FILTER (WHERE outcome='ok' AND (n1_skill OR n2_skill OR n3_skill))/nullif(count(*) FILTER (WHERE outcome='ok' AND nx IS NOT NULL),0),1) AS baseline_pct_skill_read_within_3_after_ok
FROM c;
