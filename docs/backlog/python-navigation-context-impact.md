# Python navigation: test discovery and bounded change impact

Status: draft. Scopes 2 and 3 are unimplemented and require a new assignment
and reviewed plan before source changes. This brief retains the remaining
work from the original three-slice request.

## Problem and existing foundation

Agents need evidence for relevant tests and the likely impact of a change.
Use the existing PythonHandler alias roots, search and symbol/source views,
callers/callees, imports/importers, static callgraph, explicitly gated runtrace
and root-scoped incremental RepoCache. Checkout/indexed-byte provenance is
the foundation; root registration and result provenance are separate concerns.
Do not rebuild existing navigation or imply that Git HEAD identifies dirty
indexed bytes. Registered roots and explicit unknowns remain required.

## Second slice: test discovery

Given a symbol, return relevant test node ids/files with confidence and evidence:
static direct callers/imports, fixtures/dependencies, naming fallback and any
available measured coverage. Label evidence-based vs inferred relationships.
Do not claim complete coverage from naming/static reachability. Return bounded
ranked results and the relevant repository test invocation; do not silently
execute tests, runtrace or production code as part of navigation.

## Third slice: change-impact view

Given a Git diff/revision pair or changed symbol, show changed symbols plus
bounded transitive callers/importers, affected tests and relevant owning docs.
Use explicit depth/size limits; unresolved dynamic dispatch and excluded roots
remain visible. Compare deployed source to a registered worktree only when both
identities are available; changes to uncommitted files need correct fingerprints.
Report additions/removals and signatures/contracts as well as line moves.

## Delivery constraints

Implement evidence-labelled test discovery before stacking bounded impact.
Read applicable AGENTS, docs/codebase.md and owning package docstrings;
verify the current local source and propose the source/API plan for review.
Use registered, authorized root aliases only; expected_root grants no access
or registration. Preserve raw indexed-byte identity, same-snapshot rendering,
stat-checked reuse limitations and separately observed Git context.

Keep additive responses and existing explicit execution gates. No dependency,
schema or breaking API change without repository threshold review. Never
execute tests, runtrace or source implicitly during navigation. Registering a
local client root does not authorize production service binding.

Run scripts/test, scripts/test --typecheck and uv-run Ruff checks, including
meaningful relationship/limit regressions and changed-line coverage. Update
owning rationale/help; coordinate any version slot and normal scripts/ship
workflow separately. Coordinator owns release/deploy decisions.

No Docker prune, shared /tmp scripts, force pushes, production writes as tests,
mismatched MCP edits or unrelated science/compute/Catpath/BEEF/service work.
Preserve existing trees and scratch artifacts.

## Acceptance

A known symbol discovers meaningful test files/node ids with labelled evidence
and confidence, without claiming complete coverage from names or static edges.
A bounded impact view includes changed symbols, known callers/importers,
affected tests/docs and explicit dynamic/excluded-root uncertainty. Compare
registered source snapshots with correct uncommitted-byte fingerprints;
report additions/removals and contract changes as well as line movement.
No navigation read implicitly executes source. Normal gates and coverage pass
before landing.
