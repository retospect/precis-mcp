# Graph-locality architecture — held pending a framing pass

docs/design/graph-based.md proposes conditioning an agent's admissible
tools/context on *where it is* in the quest/document/citation graph,
replacing the per-job_type pass zoo. Held: first resolve which passes are
"mechanical prep" (LLM as a narrow, checkable retrieval utility) vs "actual
work" (judgment-laden synthesis where graph-locality might change behavior)
— that decides whether the framing applies to a given pass at all.
