# Re-check: agents calling the native Skill tool with precis skill ids

The 2026-08 transcript-mining audit saw prod agents invoke Claude Code's
native `Skill` tool with precis skill ids (instead of
`get(kind='skill', id=…)`) — but every observed instance overlapped the
gr197478 outage window, when zero precis tools were registered, so agents
may only have reached for `Skill` because the real tool was absent. Before
touching prompt templates, re-run the check on clean post-outage evidence:
raw tool-call streams from `plan_tick` `meta.transcript` rows (post-2026-08-07
these are condensed conclusions — need old-format runs, or time for the
quest_tick `meta.transcript_raw` failure captures
(`quest/tick.py:_persist_job_transcript`) to accrue). If the
confusion persists without the outage, fix the worker prompt template
(`src/precis/workers/`) to name the precis path explicitly; if it doesn't,
delete this item. Verification task; cheap.
