# Gate concurrency

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Test-suite setup tax — serialized template clones

_Grouped 2026-09-26; was `test-setup-tax`._

The suite is setup-dominated (~340 s fixture setup vs ~120 s test logic;
7,774 tests, ~100 s wall @ -n6): the 6 per-worker FILE_COPY template clones
run fully serialized under the session advisory lock (the last worker waits
behind all prior clones). Options, none free: cap gate workers; shrink the
template; let clones proceed with less lock overlap — measure before
touching (real correctness/speed tradeoff). The per-test TRUNCATE base is
already the cheap isolation choice. Separate gap: no coverage is measured
anywhere (no pytest-cov). Owner `tests/conftest.py::_initialise_test_db`.

## Ship gate: test-db 100-connection ceiling saturates full-suite -n6

_Grouped 2026-09-26; was `test-db-connection-ceiling`._

Under the full suite at -n6 on a loaded host, peak connections saturate
precis-test-db's default max_connections=100 — RST'd before Postgres accepts,
surfacing as psycopg "server closed the connection unexpectedly" across every
test dir with nothing logged server-side; subset runs never hit the peak,
which masks it. Workaround shipped: `PRECIS_GATE_N=3 scripts/ship`. Durable
fix is a design call: raise max_connections (~300 risks a real pg OOM on a
RAM-pressured host; maybe 150 + a gate-side pressure check) or auto-step -n
down under host pressure. Owner `docker/dev/compose.yaml` (precis-test-db) +
`scripts/ship`.
