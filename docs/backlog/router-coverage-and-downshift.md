# Everything through the router; wean bulk work off opus

Reto: all LLM traffic through the precis router, then push "stupid work" down
to local/cheaper models (haiku, deepinfra/OpenRouter/EU; local best), keeping
claude as top-dog reviewer — coding and writing tasks included. Remaining
coverage holes: `claude_docker`/`sandbox_run`'s in-container `claude -p`
never touches dispatch (deferred — dark/unused today); the ADR-0046 "group B"
call-sites aren't backend-aware, so a cloud chain rung on those paths would
mis-route. Related asks: a cheap/local pre-filter tier for research surfaces
(asa, reviewers, `perplexity-research` ~$0.50/call) before paid escalation;
a "corpus before paid web" cost-ordering line in precis-research-help + asa's
SOUL; route mechanical passes (llm_summarize, triage children, CI-fix) to a
4B–14B model.
