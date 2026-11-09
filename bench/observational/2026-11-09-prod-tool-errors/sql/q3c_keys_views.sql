-- q3c: (a) most-rejected arg keys; (b) most-mistyped views. Same window.
WITH e AS (
  SELECT verb, kind, error_head, session_id
  FROM tool_calls
  WHERE outcome='error' AND error_head IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), keys AS (
  SELECT verb, kind, session_id,
         (regexp_match(error_head, '\[(''[^\]]*'')\]'))[1] AS keylist
  FROM e
  WHERE error_head ~* 'does not accept \[|not accepted by'
), views AS (
  SELECT verb, kind, session_id,
         coalesce((regexp_match(error_head, 'view[= ]''([^'']*)'''))[1], (regexp_match(error_head, 'unknown [a-z]+ view ''([^'']*)'''))[1], (regexp_match(error_head, '''([^'']*)'' not recognised'))[1]) AS bad_view
  FROM e
  WHERE error_head ~* 'unknown (list |[a-z]+ )?view|not recognised|unknown view='
)
(SELECT 'rejected_keys' AS section, verb || ':' || coalesce(kind,'-') AS vk, keylist AS val, count(*) AS n, count(DISTINCT session_id) AS sess FROM keys GROUP BY 1,2,3 ORDER BY n DESC LIMIT 15)
UNION ALL
(SELECT 'unknown_view', verb || ':' || coalesce(kind,'-'), bad_view, count(*), count(DISTINCT session_id) FROM views GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 15)
ORDER BY section, n DESC;
