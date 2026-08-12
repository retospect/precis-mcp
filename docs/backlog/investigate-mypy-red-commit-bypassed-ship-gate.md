---
status: idea
title: How did a mypy-red commit (13b28625) reach origin/main past the ship gate?
---

# How did a mypy-red commit reach main past the ship gate?

## What
On 2026-08-12, `origin/main` HEAD `13b28625` ("fix(llm): bypass llama-server
prompt cache on the local rung") was **mypy-red on its own**: it added
`extra_body = {"cache_prompt": False}` in
`src/precis/utils/llm/router.py` (`_dispatch_local_tools`-ish path), which
narrowed the inferred type to `dict[str, bool]` and collided with the `else`
branch's `openrouter_routing(...) or None` (`dict[str, Any] | None`) —
`error: Incompatible types in assignment [assignment]`. The next worktree to
ship (`18f45335`) hit it during its gate and had to annotate `extra_body:
dict[str, Any] | None` to unblock. That symptom is fixed; the process
question is not.

## Why it matters
`scripts/ship` runs `mypy src tests` in full (not `--impacted`) before the
squash-merge, so a deterministic assignment error like this **should** have
blocked `13b28625`'s own ship. It didn't. Either the gate was bypassed
(force-push / `PRECIS_GATE_N=0` / merge outside `scripts/ship`) or there's a
host-vs-container mypy divergence for this construct (cf. auto-memory
`live-model-tests-need-host-claude`, and commit 2c351913 on per-module
`warn_unused_ignores` host/CI divergence). If the gate is bypassable, the
ship-gate guarantee is weaker than assumed — worth knowing.

## Investigate
- `git show 13b28625 --stat`; check its ship provenance (was it squashed via
  `scripts/ship`, or pushed another way?).
- Reproduce: `git stash`-free checkout of `13b28625` in a worktree, run
  `scripts/test` / the gate's `mypy src tests` — does it fail in-container?
  If it passes in-container but failed for `18f45335`, chase the divergence.
- If genuinely bypassable, tighten `scripts/ship` (or document the escape
  hatch that allowed it).

## Not in scope
The type error itself — already fixed in `18f45335`. This item is only the
"how did it get past the gate" forensic.
