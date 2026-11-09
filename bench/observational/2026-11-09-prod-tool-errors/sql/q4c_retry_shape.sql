-- q4c: when the next call in the session is the same verb+kind, was it a blind re-send (identical arg_sample) or an edited call?
WITH w AS (
  SELECT call_id, session_id, seq, verb, coalesce(kind,'(none)') AS kind, outcome, error_type, arg_sample, input_keys,
    CASE WHEN client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='' THEN 0 ELSE 1 END AS is_agent
  FROM tool_calls
  WHERE session_id IS NOT NULL AND arg_sample IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), c AS (
  SELECT w.*,
    row_number() OVER (PARTITION BY session_id ORDER BY seq, call_id) AS rn,
    lead(verb) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_verb,
    lead(kind) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_kind,
    lead(outcome) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_outcome,
    lead(arg_sample) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_arg
  FROM w
)
SELECT count(*) AS n_err_next_same_vk,
  count(*) FILTER (WHERE nx_arg = arg_sample) AS n_identical_resend,
  round(100.0*count(*) FILTER (WHERE nx_arg = arg_sample)/count(*),1) AS pct_identical_resend,
  round(100.0*count(*) FILTER (WHERE nx_arg = arg_sample AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE nx_arg = arg_sample),0),1) AS pct_ok_when_identical,
  round(100.0*count(*) FILTER (WHERE nx_arg <> arg_sample AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE nx_arg <> arg_sample),0),1) AS pct_ok_when_edited
FROM c
WHERE outcome='error' AND is_agent=1 AND nx_verb = verb AND nx_kind = kind;
