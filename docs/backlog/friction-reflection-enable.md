# Tool-friction reflection — enable Part A + agentlog stitching

Part A (end-of-run friction footer, `src/precis/utils/friction_reflect.py`)
is built default-OFF. Flip PRECIS_FRICTION_REFLECT=1 on the melchior agent
worker only once a downstream grouping/dedup lane exists to absorb `friction`
gripes — else raw wishes pile up untriaged; gauge junk-rate. Also: link each
friction gripe to the run's 30-day agentlog — the filing agent doesn't know
its own agentlog id at put time → post-hoc stitching (time+source join) or an
id threaded into the run context (stopgap: friction-model:<model> self-tags).

## Second half, wanted 2026-09-29: the forward-looking note

Design session (Reto + agent, big-model-manage). Part A asks the finished run
what it *lacked*; the eval loop also wants what the **next** run should start
with. Both halves are end-of-run self-report and should be captured together
rather than bolted on separately later.

Trust behavioural evidence over self-report where the two disagree: which
injected items a run actually referenced or drilled into is now measurable
from the `tool_calls` ledger (migration 0133), and that reading is
`mcp-surface-economy.md` §3's job. Self-report is the cheap signal; the
ledger is the check on it. Aggregate across runs before acting on either —
a single run's wish list is noise.

Consumer: `eval-run-spine.md`, which owns the run row these notes hang off.
