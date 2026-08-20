---
status: draft
title: "two taproot `enable_env` flags are dead switches — `hub_refine` / `chase_trigger` register unconditionally"
---

# Setting the env var does nothing

`cli/worker.py::_should_register` ends in `return bool(spec.enable_env)`: a
`ServiceSpec` carrying an `enable_env` registers **unconditionally**, and the
variable's *value* is never read. For `hub_refine` and `chase_trigger` that
makes `PRECIS_TAPROOT_REFINE_ENABLED` / `PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`
completely inert — neither pass is in the deploy-time seed loop
(`deploy/roles/precis_worker/tasks/provision.yml` seeds only `llm_summarize`,
`classify`, `llm_reconcile`, `job_claude_docker`, `cast_audio`), so the flag
is not even mirrored into a `service_config` row. `hub_refine_enabled()` and
`chase_trigger_enabled()` had no callers outside `__all__` and their own tests.

The real switch is a `service_config` prio row, live in both directions:

```
precis service prio <host> chase_trigger 1
precis service prio <host> hub_refine    1
```

Both on and off take effect within one cycle — registration is unconditional
and `pass_gate` re-reads `service_config` through a 5 s TTL cache
(`workers/service_config.py`). There is no daemon kick and no on/off
asymmetry.

## The scope is exactly two flags — do not generalize it

Measured 2026-08-20. Three other flags look like this one and are **live**;
"fixing" them would be a regression:

| Flag | Status |
|---|---|
| `PRECIS_TAPROOT_CHASE_ENABLED` | **live** — in-pass, `workers/chase.py` |
| `PRECIS_INBOUND_CHASE_ENABLED` | **live** — in-pass, `inbound_chase.py::inbound_chase_enabled`, also gating the citer sidecar in `handlers/paper.py` |
| `PRECIS_AXES_ENABLED` | **live** — seeds the `axis:<id>` gate default, an explicit documented exception to the §L cutover (`cli/worker.py::_gate_default_on`) |

The distinction is structural: a pass that is its **own service** flips via
`service_config`; a **sub-feature of another pass** has no `ServiceSpec` to
flip, so it keeps a genuine in-pass env flag.

## The trap: `enable_env` is load-bearing despite being unread

Deleting `enable_env` from these two `ServiceSpec`s — the obvious "remove the
vestige" fix — would **unregister both passes**, since its mere presence is
what makes a pass with empty `default_profiles` register at all. The field is
badly named, not vestigial. `ServiceSpec`'s own field comment
(`workers/registry.py`) already documents this correctly; the drift was
entirely in the narrative docs around it.

## Done 2026-08-20

Corrected in the same pass: the `precis.taproot` package docstring (canonical
statement of the two-mechanism model), `docs/runbooks/taproot-chase-enablement.md`
(the plist warning and the on/off-asymmetry paragraph, both stale), both worker
docstrings, and `precis-finding-help` (seed-vs-live wording). The dead
`hub_refine_enabled()` / `chase_trigger_enabled()` functions and their four
tests are deleted — a dead switch that tests green is worse than no switch.

## Left open

Rename `enable_env` to something that says what it does (a registration
marker, not an enablement flag). Cheap and mechanical, but it touches ~20
`ServiceSpec` rows plus `enable_env_for()`, so it wants its own change rather
than riding a docs pass. Until then the field comment is the guard.
