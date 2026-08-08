# Nursery digest pages truncate at ORDER BY ref_id LIMIT 50

Most nursery check queries page `ORDER BY r.ref_id LIMIT 50`
(`src/precis/workers/nursery.py`) — past 50 stuck items the tail never
surfaces in the digest; same head-of-line family as the dispatch findings but
for *visibility*, not execution. Random-sample or oldest-first-by-staleness
per check. The 2026-08-08 dispatch-review residual not covered by the
fair-dispatch-two-currencies proposal. Mechanical.

test: item #51 appears within k digests.
