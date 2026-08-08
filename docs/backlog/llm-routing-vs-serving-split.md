# served_by conflates "serves the model" with "may route to it"

Local BIG (Qwen3-235B on castor+pollux) is unreachable from melchior, where
casts compose — the BIG chain silently lands on its cloud fallback.
`local_serving.acquire()` is host-scoped through `resource_slots`, and the
heartbeat probe reverts any hand-registered row within a minute (tried;
reverted — prod left honest). caspar's own row is already the same fiction
(castor/pollux do the serving). Needs a routing-vs-serving split in
`src/precis/utils/llm/local_serving.py`, not a config edit. Needs design.
