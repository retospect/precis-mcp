# memory-lint currency auditor — extract as a pip? (decide ~2026-08-19)

`scripts/memory-lint --currency` verifies memories against repo ground truth
(git+fs oracles); a prior-art scan found no open-source equivalent
(claude-mem, server-memory, Mem0/Zep/Letta all store/retrieve, none audit) —
the only novel slice. After a month of use decide: extract as a standalone
pip/plugin (genericize oracles off precis coupling, own maintenance) vs stay
a repo-local script + a line in docs/how-to-setup-like-this.md. Prior is
transient-at-best — only extract if the month proves recurring value.
