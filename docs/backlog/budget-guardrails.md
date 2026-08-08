# budget-guardrails

## Residuals (from OPEN-ITEMS)

Piece B (global breaker) + real-cost capture are SHIPPED; the design doc's
"not built" header is stale — treat it as design-of-record. Open:
- Piece A cost-band affordance: `src/precis/budget/bands.py` has the
  Cost/Pace enums + Band.label(), but nothing surfaces the label to any
  model — wire it + a permissive "escalate freely when needful" line into
  the agent system prompts.
- Piece C attribution remainder: stamp `precis_web/ask.py` (conv_ref_id
  accepted but not threaded onto LlmRequest) and
  `src/precis/workers/_chase_llm.py` ×3 (dispatches carry no ref_id);
  pass-level passes (dream, review) legitimately stay unstamped.
- Non-LLM compute (spark DFT/relax/fold, container jobs) never touches
  dispatch — build the service_calls (pass, host, day) rollup only if the
  data says local *compute* capacity is the constraint.
- Open decisions: ledger union without double-count; price-table source +
  upkeep; cheap-band threshold; real cap defaults.
