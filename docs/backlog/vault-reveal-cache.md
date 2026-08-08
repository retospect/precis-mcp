# OPENROUTER_API_KEY revealed ~22k/day — the 60 s secrets cache isn't holding

vault.events shows ~15 reveals/minute against a `_CACHE_TTL_SECONDS = 60`
module cache that should cap near 1,440/day/process — either far more
processes resolve it than expected, or the cache is bypassed (a fresh
`_cache` per short-lived subprocess would do it, as would `invalidate()` on a
hot loop). The audit table is the fastest-growing thing in the DB and each
reveal is a decrypt. Diagnosable now via the host/pid/process columns from
migration 0111. Owner `src/precis/secrets.py`.

test: a hot get_secret loop issues ≤1 vault.reveal per TTL per process.
