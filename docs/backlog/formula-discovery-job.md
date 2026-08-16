# Formula-discovery job type (LLM symbolic regression over quest data)

A job type that distills interpretable formulas from accumulated quest
experiment data: automated feature engineering → LLM-guided symbolic
regression with self-evaluation → MCTS-based interpretation, outputting
`finding` refs (nanopub-mintable). The paper's eval domains (perovskite
synthesizability, ionic conductivity, 2D-material classification) are
precis quest domains — direct fit.

Sources: LLM-Feynman, arXiv:2503.06512 (ingested: paper id=210166; no
public code release found as of 2026-08-16); nearest open implementation
is LLM-SR (ICLR 2025), https://github.com/deep-symbolic-mathematics/LLM-SR
— use as the reference architecture.

Caution: long many-LLM-call iterative loop — needs a bounded per-round
budget and its own lane from day one (taproot_backfill lane-monopoly
precedent). All calls through the router. Owner: `precis.workers`
job_types. Test: rediscovers a known formula from a fixture dataset
within budget.
