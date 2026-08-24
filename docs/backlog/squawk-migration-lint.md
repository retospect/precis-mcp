# Squawk lint on new migration SQL in the ship gate

Run [squawk](https://squawkhq.com) over the migrations a ship adds
(sealed files are frozen, so only the diff's new `NNNN_*.sql`) as a gate
step next to `scripts/migration-check`. Catches the operational hazards
the schema-design tests can't: `ADD COLUMN ... NOT NULL` without a
default, `CREATE INDEX` without `CONCURRENTLY`, lock-taking table
rewrites — exactly the class that hurts on a live prod DB with
forward-only migrations.

Blocked on packaging, not design: squawk is a Rust binary (npm/brew;
verify whether the PyPI `squawk-cli` wheel works in the gate container —
if yes it's a one-line `uv run --with` and no image rebuild; if no, it's
a precis-dev image change, see the new-core-dep memory re GH_TOKEN).
Expect a config pass to silence rules that don't apply (single-writer
maintenance windows make some lock warnings noise here).

Test: a scratch migration adding a NOT-NULL-no-default column reddens
the gate; the existing sealed chain passes untouched.
