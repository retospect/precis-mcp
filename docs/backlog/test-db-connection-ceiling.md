# Ship gate: test-db 100-connection ceiling saturates full-suite -n6

Under the full suite at -n6 on a loaded host, peak connections saturate
precis-test-db's default max_connections=100 — RST'd before Postgres accepts,
surfacing as psycopg "server closed the connection unexpectedly" across every
test dir with nothing logged server-side; subset runs never hit the peak,
which masks it. Workaround shipped: `PRECIS_GATE_N=3 scripts/ship`. Durable
fix is a design call: raise max_connections (~300 risks a real pg OOM on a
RAM-pressured host; maybe 150 + a gate-side pressure check) or auto-step -n
down under host pressure. Owner `docker/dev/compose.yaml` (precis-test-db) +
`scripts/ship`.
