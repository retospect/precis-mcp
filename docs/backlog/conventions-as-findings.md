---
status: idea
pillar: memory-graph
title: conventions as findings — every docs/conventions rule becomes a finding with a `tests` edge, and the file retires
---

# conventions as findings

`file-mirror.md` §"Pillar-review deltas" item 3 sets the retirement
condition for a convention file: its rule is a `finding` with a `tests`
edge to the hook or test that enforces it. Nothing mints those findings.
`unify-backlog-gripes-discoverable.md` names the hole as a pointer;
`docs-and-skills-redesign.md` defers "reviewer-finding wiring into
kind='finding'".

What: one `finding` per rule in `docs/conventions/*.md` (a file with three
rules yields three), tagged `SPACE:repo-dev`, `tests` → the enforcing
test or hook (`tests/test_deploy_tree_no_secrets.py`, the ruff rule id,
`scripts/hooks/*`), `related-to` the `CLAUDE.md` one-liner that routes to
it. A rule with no enforcer gets the finding and no edge — the allowlist in
`tests/test_conventions_retired.py` (`memory-native-authoring.md` in-scope
4) is the list of rules still unenforced, which is a useful thing to see.
A convention file retires when every rule in it has its edge; `CLAUDE.md`
§Hook/gate-enforced then points at findings, not files.

Ranks after `memory-native-authoring.md` (needs the `SPACE:` axis and the
retirement-test shape it ships). Owner: `src/precis/taproot/` mint path
for the findings, `docs/conventions/`.

test: `tests/test_conventions_retired.py` allowlist shrinks to empty;
`get(kind='finding', id=F, view='fisheye')` from a gripe reaches the
convention it violates in one hop.
