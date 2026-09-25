---
status: idea
title: Repair architecture-record drift at load-bearing seams
prio: normal
---

# Repair architecture-record drift at load-bearing seams

The architecture record contradicts code at several load-bearing seams:
`precis.workers` says `run_handler_once` processes a batch in one transaction,
while the runner deliberately uses claim/process/write phases with two short
transactions; `precis_chem` and `precis_bio` package docstrings still describe
retired private enable flags while their handlers/tests say the plugins are
always on; `docs/codebase.md` calls the same public surface both seven and eight
verbs; `AGENTS.md` still claims Python 3.11 support while `pyproject.toml`
requires 3.12. These are the exact docs the project tells agents to trust.

Audit `docs/codebase.md` plus owning package docstrings against composition
roots and invariant tests, correct present-tense claims, and add cheap parity
assertions where code can express the contract (verb count, plugin gate state,
transaction phase shape). Do not turn this into a broad prose rewrite.

Owner anchors: `src/precis/workers/__init__.py`,
`src/precis/workers/runner.py::run_handler_once`, `src/precis_chem/__init__.py`,
`src/precis_bio/__init__.py`, `docs/codebase.md`.
