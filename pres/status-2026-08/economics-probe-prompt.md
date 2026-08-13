# Side prompt: economics numbers for the status deck

Paste the prompt below into a session with cluster/prod access (main precis-mcp
checkout). It is read-only by design.

---

I need economics numbers for a status slide about precis. Work READ-ONLY —
`SELECT` queries via `scripts/prod-psql` (or the cluster-ops agent) and log
reads only; no writes, no config changes.

Questions, in priority order:

1. **Tokens per answered question.** Using the prod `jobs` table (and
   `meta`/transcript usage fields where present), estimate total LLM tokens
   (in + out) per *completed top-level task* over the last 14 days. Report
   median and p90, and state clearly what you counted as "a question"
   (completed todo? quest tick? dispatch root?) — name the definition on the
   slide, don't bury it.
2. **Local vs cloud split.** Share of calls and of tokens served by local
   models (melchior 9B, spark Qwen3-235B) vs cloud APIs (Anthropic,
   OpenRouter) over the same window. The capability-tier routing tables /
   `llm` kind selections and job meta should identify the serving endpoint.
3. **Cost per day.** Cloud $/day from whatever spend records exist (budget
   breaker's own accounting, API usage logs). For local, report tokens/day
   served locally and note "marginal cost ≈ electricity" — do NOT invent a
   $ figure for electricity.
4. **Budget breaker.** Its configured caps (warning + critical thresholds),
   what it does when tripped, and how many times it tripped in the last 30
   days (check gripes/alerts referencing it, e.g. the $85/$20-critical
   breaker gripe lineage).

Deliverable: a short markdown report with (a) one table of the numbers with
the measurement window and definitions, (b) exactly 3 slide-ready bullets in
the voice "grounded AND cheap enough to run all night", (c) a list of what
could not be measured and what instrumentation is missing — that gap list
feeds the "economics instrumentation" roadmap item.

If token usage is not recorded per job, say so explicitly and propose the
smallest schema/logging change that would capture it — that becomes a backlog
item, not something you build now.
