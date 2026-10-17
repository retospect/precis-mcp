---
status: in-progress
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

## Built 2026-10-16 (importer + test 4c; no convention file deleted)

- **Manifest:** `src/precis/data/conventions/rules.yaml`, one entry per
  rule: id `<file stem>/<rule slug>`, file, section, title, body, and
  `enforcers` (a repo path or `ruff:<id>`). It holds 108 rules from all 18
  files: 42 have an enforcer and 66 have none. An enforcer is listed only
  if it goes red on a violation. Advisory nudges, warn-only hooks,
  partial-scope checks and tests of a helper rather than its callers were
  left out.
- **Importer:** `cli/conventions.py::import_conventions`, run as `precis
  conventions import [--dry-run] [--sync]`.
  - It mints one `finding` per rule (`meta.convention_rule`, tags
    `SPACE:repo-dev` and `convention`) and one per distinct enforcer
    (`meta.convention_enforcer`, tags `SPACE:repo-dev` and `enforcer`).
  - The edge runs enforcer `-tests->` rule, because the `tests` relation
    reads "source tests target" (`store/types.py`, migration 0142). The
    brief's "edge to the test" is this edge, read from the rule's side.
  - Re-runs are idempotent: the importer keys on its own meta among
    importer refs, and a retired importer ref blocks re-minting.
  - `--sync` rewrites the title and body of importer-owned refs and drops
    the importer's stale `tests` edges. It never retires anything.
- **Test 4c:** `tests/test_conventions_retired.py` checks three things.
  Every convention file is in the manifest; every enforcer path exists
  (a `ruff:` id needs a selected prefix); and the rules with no inbound
  `tests` edge equal `_ALLOWLIST` exactly.
- **Left:** drain `_ALLOWLIST` by writing enforcers. The `related-to`
  edge to the `CLAUDE.md` one-liner waits for a repo root in the graph
  (`file-mirror.md` §deltas 2). File retirement is Reto's call, per
  batch.
- **Prod import, 2026-10-16 ~22:25Z:** created 151 findings (108 rules,
  43 enforcers) and 54 `tests` edges; the re-run planned 0. Example:
  fi493530 (`invariants/body-chunks-append-only`) ← fi493629
  (`tests/test_chunk_append_only.py`).

## Enforcer wave 2026-10-17 (66 → 52 unenforced)

Fourteen rules gained real enforcers (manifest `enforcers:` set, allowlist
entries deleted): skill-authoring-style (3), code-anchors (2),
poster-typography (2), safe-get, kind-enablement (2), deploy-is-opt-in,
raw-sql companion, take-agent, no-float-equality. Each new test
file carries a permanent negative test that feeds its checker a synthetic
violation. Docs checks are exact-zero outside `docs/backlog/` (`sha-gate: allow` marks
a gate value); other offenders are one-way ratchets (18 grandfathered
precis_web SQL modules, 5 persona `last-updated:` lines, 15 internal-name
mentions). The `2>/dev/null` count (359 legitimate uses) was dropped as a
flapping count (Reto 2026-10-17); rtk/no-blanket-stderr-suppression is back
on the allowlist as judgement.

Partial checks exist but are not linked (a partial check is not an enforcer
of the whole rule): update-doc-same-commit (sha half), math-and-units and
layout-and-face (the `\boldmath` group and `\hyphenpenalty` halves),
api-stop-and-ask (`tests/test_tool_registry.py` pins the verbs only),
schema-stop-and-ask, qland-lint-and-drift-guard (no test of the 24h/48h
thresholds: a real gap).

Judgement-only, stay allowlisted (52), grouped by why:

- Authoring voice, no regex separates good from bad: llm-facing-prose (6),
  skill-authoring goal-voice-h2 and contract-not-implementation,
  poster-typography band/ISO-units/face details, code-anchors
  citation-surfaces-only (86 legitimate `Sym` pointers in src comments).
- Agent or operator behaviour, no code path: shipping read-gate-failure,
  mark-round, peer-ping-cadence, release-owner, no-deploy-window,
  gripe-sha-on-backlog-delete; all thresholds/* (9); rtk digest and
  prefix rules and no-blanket-stderr-suppression; testing long-runs, narrow-lanes, note-what-caught,
  main-sha-not-gated, rehome-db-tests, measured-budgets; skill-evals;
  corpus-normalization (3); reviewed-non-problems (3); dark-switches (2);
  discovery-layer-policy (2); container-ops wide-docker-address-pool
  (per-machine daemon setting).
- Design calls: env-vars (3: tier choice; 33 vars are read raw in 2+ files,
  so a per-var check starts red), time/coerce-at-boundary (heuristic).
