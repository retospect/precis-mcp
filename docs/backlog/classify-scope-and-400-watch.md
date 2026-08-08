# classify: --ref-ids ignores scope; 400-storm watch

gr173317: `precis classify topics --ref-ids <ids>` runs a full-corpus sweep
(5573 papers instead of 5); its 5570 "failed" made zero LLM calls — a
precondition-fail path that may be the real substance behind gr172740
(classify broken). The 2026-07-26 glm-4.7-flash HTTP-400 storm proved
transient (did not reproduce); error-body instrumentation is live (a9448ffc)
— if it recurs, read the enriched `llm_call_log.error`. Owner
`src/precis/workers/classify_topics.py` / classify CLI.
