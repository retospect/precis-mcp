# Decide the LLM: planner tag vocab's scope

plan_tick always passes tools_needed=True, so `LLM:small`/`LLM:medium` todo
tags no-op through the local-fallback path instead of routing distinctively
(ADR 0066 §"Still genuinely open"). Decide what the tag vocabulary should
mean and wire it. Needs design.
