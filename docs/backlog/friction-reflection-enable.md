# Tool-friction reflection — enable Part A + agentlog stitching

Part A (end-of-run friction footer, `src/precis/utils/friction_reflect.py`)
is built default-OFF. Flip PRECIS_FRICTION_REFLECT=1 on the melchior agent
worker only once a downstream grouping/dedup lane exists to absorb `friction`
gripes — else raw wishes pile up untriaged; gauge junk-rate. Also: link each
friction gripe to the run's 30-day agentlog — the filing agent doesn't know
its own agentlog id at put time → post-hoc stitching (time+source join) or an
id threaded into the run context (stopgap: friction-model:<model> self-tags).
