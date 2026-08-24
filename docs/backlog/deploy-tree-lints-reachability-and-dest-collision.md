---
status: draft
title: lint the deploy tree — unreachable role edits and colliding render targets
prio: normal
---

# lint the deploy tree — unreachable role edits and colliding render targets

## Motivation / why

Three instances in two days of the same failure, all shipped green:

1. `roles/precis_worker/tasks/units.yml` plist mode set to `0640` — playbook 20b
   invokes that role as `import_role: tasks_from: provision`, deliberately
   skipping `units.yml`, so the edit was never applied. Worse, the file that
   *does* render the live plist (`roles/service_unit/tasks/main.yml`) rendered
   it `0644` and **reverted the fix on the next deploy**, re-exposing a
   credential-bearing plist world-readable.
2. `roles/asa_bot/tasks/main.yml` plist mode — role is in no playbook that
   `redeploy-precis.yml` imports, so still unapplied.
3. `roles/autocatpath/tasks/main.yml` build prereqs — `playbooks/44-autocatpath.yml`
   is a standalone entry point imported by *neither* `site.yml` nor
   `redeploy-precis.yml`, so it would have sat unapplied indefinitely.

Root cause is structural, not carelessness: **the gate proves the code is
correct and nothing proves the change is reachable.** For a `deploy/` edit,
"green" is a statement about the Python test suite — something else entirely —
but it reads as approval, and the author moves on.

Cost of not fixing: a deploy-tree edit can ship, read as done, and change
nothing — or be silently reverted by another role that owns the same target.
The 2026-08-23 case had a live credential exposure on the wrong side of it.

## In scope

Two static checks, as **tests** (matching how the `encoding="utf-8"`
convention is enforced by an AST-walk test), ordered by value-per-effort:

- **Dest-collision lint** (do first, ~1-2h). Parse every `template`/`copy`
  task's `dest:` across `deploy/roles/**`; flag any absolute path rendered by
  more than one role with differing `mode`/`owner`/`group`. Catches instance 1
  exactly — two renderers, two modes, winner not visible from either file.
- **Reachability lint** (~half a day, mostly parser). Walk the import graph
  from `site.yml` and `redeploy-precis.yml` (`import_playbook` -> playbook ->
  `roles:` / `import_role` / `include_role`, honouring `tasks_from`) and
  classify every file under `deploy/roles/**` and `deploy/playbooks/**` as
  reachable-from-redeploy / site-only / standalone-only / orphan. Catches
  instances 2 and 3.

**Snapshot-based, not absolute.** A large fraction of `deploy/` is legitimately
standalone, so "fail if unreachable" would red constantly and be muted within a
week. Check the classification in as a golden file and fail only on a *change*:
a file that was reachable becoming unreachable, or a new file reachable from
nothing. The required allowlist of genuinely-standalone playbooks
(`retire-*.yml`, 44) is a feature — it turns "standalone" from accidental into
declared, which is the documentation that does not exist today.

## Explicitly NOT in scope

- Making `scripts/deploy` *block*. A deploy-time advisory ("these paths need
  `playbooks/44-autocatpath.yml`") is welcome, but `scripts/deploy` already
  aborts on an unreachable host and that abort is load-bearing; a second abort
  class raises the cost of the fleet's most important recovery tool for a
  reason unrelated to the deploy.
- Fleet-state assertions (does the live plist actually have mode X). Better
  served by putting each invariant in the role that owns it — see the Triton
  header compile-probe in `roles/autocatpath/tasks/prereqs.yml` (204b8715) for
  the shape.
- Restructuring the standalone playbooks so everything is reachable. The lint
  should describe the tree, not force a reorganisation of it.

## Acceptance criteria

- A role edit to a file unreachable from both entry points reddens the gate
  with a message naming which explicit playbook would apply it.
- Two roles rendering the same `dest:` with different mode/owner/group reddens
  the gate, naming both task files.
- Re-running against the tree as of 204b8715 produces a stable golden
  classification with no findings (the three known cases are fixed).

## Target + blast radius

New test module under `tests/`, plus a golden file. No source or deploy
behaviour change, so no deploy risk. Do NOT add this while the gate is the
flakiest part of the loop (see the colima resize) — a new gate test is worth
adding only when a green gate means something.

## Open questions / decisions log

- Does any existing YAML dependency exist in the test env, or does the lint
  need `pyyaml` added? `scripts/test` container may already have it via
  ansible-lint; unchecked.
- `include_role` under a runtime `when:`, `tasks_from` as a variable, and
  templated `include_tasks` names cannot be resolved statically. Decide whether
  those become "unknown" (ignored) or "reachable" (optimistic). Optimistic is
  probably right — a false green is no worse than today, a false red is worse.
