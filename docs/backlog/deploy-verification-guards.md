# Deploy verification guards — bounce coverage, plist drift, model-serving

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
