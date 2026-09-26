---
status: idea
title: the gating CI legs are 3.13-only, but prod runs 3.12 — and the nightly that covers 3.12 is red
---

# Nothing pre-merge tests the interpreter prod actually runs

## The instance that surfaced this (fixed in this branch)

On 2026-09-26 the local container gate was RED on a sha check.yml had
passed:

```
tests/test_token_budget.py:202: AssertionError:
tools/list wire-shape JSON is 28830 bytes (cap: 28 KB)
```

Measured cause, confirmed on the host under both interpreters against the
same tree:

```
PY 3.12.13   wire total 28830   desc raw 6101
PY 3.13.14   wire total 28442   desc cleandoc 5705  (saving 396)
```

The whole 388-byte gap was verb *description* text; schema bytes were
identical. Python 3.13 dedents docstrings at compile time (gh-81283) and
3.12 does not, so `server.py` handing `__doc__` to FastMCP unchanged
shipped four extra spaces on every continuation line of all eight verb
docstrings. Fixed by `_verb_description()` (`inspect.cleandoc`) at the
registration site — which also cuts ~400 B off every real cold start in
prod, since the agent image builds on `python:3.12-slim-bookworm`.

**Delete this section once that ships.** What follows is the part that
outlives it.

## The structural gap

`check.yml`'s six gating Linux shards are all 3.13. 3.12 runs only in the
nightly matrix. So the interpreter prod actually runs is **not covered by
anything that can block a merge** — a 3.12-only behavioural difference
reaches main green and is caught, if at all, by whoever next runs the
local container gate (which is 3.12) and has to work out that their own
diff is innocent.

The token-budget case was benign and self-announcing. The same gap would
hide anything version-sensitive: schema generation, dict/set ordering,
`typing` evaluation, stdlib deprecations.

Options, none costed yet:

- Make one of the six shards 3.12 instead of 3.13 (no added runner-minutes;
  loses one shard of 3.13 coverage).
- Add a single 3.12 smoke leg to the gate (cheap, partial).
- Build the agent image on 3.13 so prod and the gate agree, and let the
  nightly keep 3.12 for the floor claim in `requires-python = ">=3.12"`.

## The nightly is red, so today there is no backstop at all

The nightly full matrix concluded `failure` on at least four consecutive
nights: 2026-09-22, -23, -24, -25. The 09-25 run was on `7707cb1c`, the
known mypy-red main since fixed in `4db6836b`, so that one is explained;
09-22 and 09-23 ran on `7ed550bf` and are not.

This matters beyond 3.12 coverage: the fast-by-default local gate
(`docs/conventions/testing.md`) deselects the `slow` cluster and names CI's
unfiltered shards *and the nightly* as what still runs it. Two of the three
safety nets named there are the same six 3.13 shards. A red nightly nobody
reads is not the third one.

Making the nightly green and keeping it green is the prerequisite for
trusting either posture.
