# asa slice-0 ops: OAuth / run-as cutover

asa_bot's vault fallback shipped (mirrors precis's `utils/claude_oauth`); the
live cutover is an ordered ops sequence — seed vault → verify → flip run-as →
scope vault read → retire hermes — not yet applied. Ops.
