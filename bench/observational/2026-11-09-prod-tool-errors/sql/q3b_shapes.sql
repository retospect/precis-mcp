-- q3b: top misuse shapes with class, sessions, and a representative call skeleton.
WITH e AS (
  SELECT t.*,
    regexp_replace(error_head, '^\[error:[A-Za-z]+\] ', '') AS head,
    CASE
      WHEN error_head ~ 'exceeds PRECIS_TOOL_RESULT_CONTEXT_BYTES' THEN '01 result over context budget'
      WHEN error_head ~* 'unknown (list |[a-z]+ )?view|view ''[^'']*'' not recognised|unknown view=' THEN '02 unknown view'
      WHEN error_head ~* 'does not accept \[|not accepted by|does not take|not applied by a source search' THEN '03 bad args=/kwarg key'
      WHEN error_head ~* 'missing kind=|unknown kind' THEN '04 missing/unknown kind'
      WHEN error_type = 'BadInput' AND error_head ~* '\mmode\M|extent=|cannot combine|select inside view|q= finds inside|dry_run=|replace-only|only supports' THEN '05 BadInput on mode/extent/arg combination'
      WHEN error_type = 'BadInput' AND error_head ~* 'tag value must|invalid (STATUS|PRIO|status)|PRIO:|unknown thread|unknown relation' THEN '06 invalid tag/STATUS/PRIO/relation value'
      WHEN error_type = 'BadInput' AND error_head ~* 'near-duplicate|credential|mid-phrase|closing a gripe|already belongs|merge title gate|requires cited_in|text= and body=|has no comment|find=.* not found' THEN '07 write-gate / content rule'
      WHEN error_type = 'NotFound' THEN '08 NotFound id/slug/doi/symbol'
      WHEN error_type = 'Gone' THEN '09 Gone (soft-deleted ref)'
      WHEN error_type IN ('Denied','Forbidden') OR error_head ~* 'denied|permission' THEN '10 permission/denied'
      WHEN error_type = 'Upstream' THEN '11 Upstream (lock/quota/outside service)'
      WHEN error_type = 'Internal' THEN '12 Internal'
      WHEN error_type = 'Unsupported' THEN '13 Unsupported (verb,kind) pair'
      WHEN error_head IS NULL THEN '99 no error_head (pre-0209 row)'
      ELSE '14 other BadInput'
    END AS cls
  FROM tool_calls t
  WHERE outcome='error' AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), sk AS (
  SELECT e.*,
    left(regexp_replace(regexp_replace(regexp_replace(head, '''[^'']*''', '''X''', 'g'), '"[^"]*"', '"X"', 'g'), '[0-9]+', 'N', 'g'), 100) AS shape,
    verb || '(' || coalesce((
      SELECT string_agg(j.key || '=' ||
        CASE WHEN j.key IN ('kind','view','mode','extent','rel','verdict','sort') AND jsonb_typeof(j.value)='string' THEN quote_literal(j.value #>> '{}')
             WHEN jsonb_typeof(j.value)='string' THEN '<str>'
             WHEN jsonb_typeof(j.value)='number' THEN '<int>'
             ELSE '<' || jsonb_typeof(j.value) || '>' END, ', ' ORDER BY j.key)
      FROM jsonb_each(e.arg_sample) j), '') || ')' AS skeleton
  FROM e WHERE error_head IS NOT NULL
)
SELECT cls, count(*) AS n_err, count(DISTINCT session_id) AS n_sessions, shape,
       mode() WITHIN GROUP (ORDER BY skeleton) AS example_call_shape
FROM sk GROUP BY cls, shape ORDER BY n_err DESC LIMIT 45;
