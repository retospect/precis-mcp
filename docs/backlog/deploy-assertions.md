# Deploy assertions

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Deploy verification guards — bounce coverage, plist drift, model-serving

_Grouped 2026-09-26; was `deploy-verification-guards`._

Three post-deploy assertions, one owner (`deploy/redeploy-precis.yml`):
- Bounce coverage: after bootout+bootstrap some precis processes kept stale
  start times/env (child procs, or a silent `failed_when: false` skip) —
  confirm which, then make the bounce cover all managed daemons. Not urgent:
  the nightly boot cycle picks up env (Reto).
- Config drift: assert deployed launchd plists match rendered templates
  (analogue of the venv-commit assert).
- Model serving: assert each host's resolved PRECIS_SUMMARIZE_MODEL /
  PRECIS_LOCAL_* equals a model_id in that host's own resource_slots `llm:`
  rows — the balthazar ~15.6k-WARN/day drift class, caught at deploy time.
  Open decision alongside: what should the fleet summarize with — the served
  local model or rake-lemma? (the `--summarizer-model` CLI arg overrides the
  env host_var, which made a host_var fix inert).
- **`serving` is provisioned but never asserted** (found 2026-09-29). The
  "Verify every managed precis venv is on the deployed commit" play targets
  `gateway, scheduler, data, inference` — not `serving`. But
  `playbooks/20c-precis-heartbeat-serving.yml` runs
  `precis_worker/provision` there, so `/opt/precis/venv` DOES exist on the
  Spark pair, carrying a real `precis_mcp` dist. So that venv can sit
  arbitrarily far behind `precis_target_sha` while the deploy reports green:
  it is pinged for reachability, provisioned, and then excluded from the one
  check that would notice.
  **The symptom that prompted this was WRONG; the gap is real anyway.** A
  session-carried note said spark ran a month-stale `precis_mcp 8.32.0`
  (2026-08-29). Reading both serving hosts directly on 2026-09-29 refuted
  it: both are on `8.35.1`, built from what was `origin/main`'s head at the
  time of the read — current, not stale. So this item is not
  "spark is behind"; it is "nothing in the deploy would tell us if it
  were", which is the condition under which a wrong belief about a host's
  version survives a month unchallenged.
  Fix: add `serving` to that play's `hosts:` with a `_precis_venv_refs` map
  of `/opt/precis/venv` ONLY — 19a-precis-embedder.yml deliberately excludes
  `serving`, so asserting `embedder-venv` there would false-fail. Note the
  existing `data`-node precedent in that play: it already gates its map per
  host, so this is the same shape, not a new mechanism.
  **A negative control is required before believing the fix**: the omission
  means the deploy has never once failed on a stale serving host, so a green
  run after the change proves nothing on its own — point the assert at a
  deliberately stale venv (or a wrong `precis_target_sha`) and confirm it
  goes red.

## Drive the §L service_config seed from the registry, not a hardcoded list

_Grouped 2026-09-26; was `service-seed-from-registry`._

The deploy seed enables passes from a hardcoded 4-service list; role-gated
passes (cast_audio's flags live in the tts role's env) got no row and
silently went dark when §L deployed. cast_audio itself is fixed (3eec86d0,
capability-gated seed entry); the generalization is open: derive the seed
from the registry's `enable_env` set × advertised capability so no future
role-gated pass regresses the same way. Owner
`deploy/roles/precis_worker/tasks/main.yml` §L. Mechanical.
