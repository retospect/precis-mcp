-- q2b: same as q2 but agent clients only (excludes asa-bot, probe/fleet-report/cache/se-box3/scratch/hp99/dl-d1, and unlabelled in-proc/CLI callers).
SELECT verb, coalesce(kind,'(none)') AS kind, count(*) AS n_calls,
       count(*) FILTER (WHERE outcome='error') AS n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) AS err_pct
FROM tool_calls
WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
  AND NOT (client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='')
GROUP BY 1,2 ORDER BY n_err DESC, n_calls DESC LIMIT 30;
