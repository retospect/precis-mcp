-- q2: error rate per verb x kind, top 30 cells by error count. All callers.
SELECT verb, coalesce(kind,'(none)') AS kind, count(*) AS n_calls,
       count(*) FILTER (WHERE outcome='error') AS n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) AS err_pct
FROM tool_calls
WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
GROUP BY 1,2 ORDER BY n_err DESC, n_calls DESC LIMIT 30;
