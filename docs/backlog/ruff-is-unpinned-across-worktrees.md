# The authoritative formatter is version-unpinned across worktrees

`pyproject.toml` declares `ruff>=0.11` — a floor, not a version. Each worktree
resolves its own ruff, so two sessions on this machine can hold different
formatters while `scripts/ship` treats `ruff format` as authoritative: it
auto-fixes, then re-runs the same tool as the gate.

**No observed instance.** This item was filed 2026-09-29 on a misdiagnosis — a
session reported five hexfold files as format-dirty on `main` and version drift
was the wrong explanation; both trees were on ruff 0.16.0 and the reporting
tree was simply behind `origin/main`, holding pre-fix copies. Main was clean.
The hazard below is read off the dependency declaration, not off an incident,
and should be weighted accordingly.

## Why it would bite if it happened

- Whoever formats last wins, and the next session reformats it back — churn
  with no semantic content, landing in unrelated squash commits.
- A gate's green stops being portable: a tree gated under one version can be
  called dirty by the next session's gate with no code change.
- The house "sibling trivial drift, just fix it" rule would aim the reformat at
  files another session is actively editing.

## Fix sketch

Pin ruff exactly (`ruff==<x.y.z>`). A floor is right for a library dependency
and wrong for a tool whose output is compared across machines and gates a
merge. Bump deliberately, in its own commit, carrying the reformat it causes so
the churn is reviewed once.

Check whether `mypy` is declared the same way — it is the other tool whose
verdict the gate treats as authoritative.

Cheap to do, low evidence that it is urgent. Close this if a pin lands or if
someone confirms uv's resolution already makes the versions agree in practice.
