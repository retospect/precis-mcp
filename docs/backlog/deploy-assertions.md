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

## Drive the §L service_config seed from the registry, not a hardcoded list

_Grouped 2026-09-26; was `service-seed-from-registry`._

The deploy seed enables passes from a hardcoded 4-service list; role-gated
passes (cast_audio's flags live in the tts role's env) got no row and
silently went dark when §L deployed. cast_audio itself is fixed (3eec86d0,
capability-gated seed entry); the generalization is open: derive the seed
from the registry's `enable_env` set × advertised capability so no future
role-gated pass regresses the same way. Owner
`deploy/roles/precis_worker/tasks/main.yml` §L. Mechanical.
