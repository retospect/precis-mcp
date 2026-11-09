# td605104 - observational baseline: precis MCP tool-call errors and next-hint uptake

Window: 2026-10-10T22:20:00Z to 2026-11-09T22:20:00Z (30 days, half-open). Source: prod ledger table tool_calls (read-only, agent_ro role). Counts are aggregate only; no payload text, hostnames or ids.

## Headline

- 300,290 calls, 6,107 errors (2.03%) across all callers. The ledger records only calls that reached dispatch; schema-level rejections by the MCP SDK are not in it.
- Restricted to agent clients (claude-code, codex, kimi, rmcp/mcp, devin; excludes the Discord bot, probes/fleet-report, unlabelled in-process callers): 81,281 calls, 4,239 errors (5.22%). The all-caller rate is diluted by about 146k Discord-bot calls with 5 errors.
- Error mix (all callers): BadInput 4,294 (70.3%), NotFound 715, Unsupported 488, Upstream 418, Gone 166, Internal 26. No Denied/permission errors in the window.
- Recovery (agent clients, per-session episodes, n=2,477): 75.2% get a later success on the same verb+kind in the same session. Median 1 same-verb+kind attempt to first success, p90 2. Counted in calls of any verb/kind: median 2, p90 13. 59.9% succeed on the first same-verb+kind retry; 71.9% within 3.
- After an error, the next call is the same verb+kind 58.0% of the time and is a success 26.2% of the time (45.3% of the same-verb+kind retries). Only 2.0% of those retries are identical re-sends; an edited retry succeeds 45.8% vs 20.8% for an identical one.
- Next-hint uptake (needs ack text, so measured on a cluster-job transcript sample: 105 jobs, 3,678 precis calls, 135 errors): every ack carried a next: line; 130 of 135 had a callable one. The next call matched a suggested verb+kind in 13 of 130 (10.0%); within the next 3 calls 29 of 130 (22.3%). 129 of the 130 callable hints were the generic get(kind='skill', id='precis-...-help') pointer; only 14 carried any other call and 3 a same-call retry. Skill pointer taken as next call: 10 of 129 (7.8%), within 3: 25 of 129 (19.4%).
- Ledger-scale proxy for the skill pointer (no ack text needed): after an agent-client error the next call is get(kind='skill') 6.3% of the time (3.9% after an ok call) and within 3 calls 12.5% (7.3% after ok).

## What the ledger can and cannot tell

- Columns used: ts, verb, kind, outcome ('ok'/'error'), error_type, error_head, arg_sample, session_id, seq, client, source. error_head is the first line of the ack only, whitespace-collapsed and masked, 200 chars. 0 of 6,107 error rows contain a next: line, so hint matching cannot be done from the ledger. The structured mcp_call log lines (precis.tools.core._log_tool_call) carry the request payload sample, not the ack body, so they add nothing for hint matching and were not queried.
- The only store of full ack text is refs.meta.transcript on job refs (stream-json). 202 of 45,039 jobs created in the window carry one (135 errors in 105 jobs with precis calls). It is a cluster-agent sample, not a random sample of callers.
- error_head and arg_sample exist from 2026-10-15T13:41Z (migration 0209); 419 error rows before that have only error_type. Shape tables use the 5,505 errors with a head (cut-off classes); class totals use all 6,107.
- session_id is one MCP connection; ordering is by seq. 1,580 errors have no session (in-process ticks, CLI, no client label) and are excluded from every sequence statistic (next call, recovery).
- An episode starts at an error whose previous call on the same (session, verb, kind) was not an error. Recovery is the first later ok call on the same (session, verb, kind), no time cap; the 24.8% that never recover include sessions that simply ended.
- About 8 rows differ between two reads of the window (late inserts), i.e. totals are good to +-10 calls.

## Caller types

The ledger distinguishes callers by the self-reported MCP client name (client) and the job source (source). It does not reliably separate cluster agents from interactive Claude Code: cluster agents run as claude-code and only those started with a job source set it. Devin: one call labelled devin-cli in 30 days; Devin builder sessions are not separable (they are not labelled devin, so they sit in another row or in claude-code).

| caller type | calls | errors | err % | sessions | sessions with an error |
|---|---|---|---|---|---|
| asa-bot (Discord bot client) | 145,999 | 5 | 0.00 | 1,128 | 3 |
| claude-code, no job source (interactive dev, plus any unlabelled cluster agents) | 71,664 | 3,682 | 5.14 | 1,733 | 641 |
| no client label (in-process, CLI, ticks) | 61,316 | 1,580 | 2.58 | 0 | 0 |
| synthetic (probe, fleet-report, cache-mcp, se-box3, scratch, hp99, dl-d1) | 11,702 | 283 | 2.42 | 11,658 | 283 |
| other client (rmcp, mcp) | 4,135 | 151 | 3.65 | 687 | 85 |
| codex | 3,223 | 215 | 6.67 | 198 | 118 |
| claude-code, job-sourced (cluster agent) | 1,720 | 128 | 7.44 | 144 | 43 |
| kimi | 538 | 63 | 11.71 | 170 | 23 |
| devin | 1 | 0 | 0.00 | 1 | 0 |

Recovery per caller type (sessioned errors only):

| caller type | errors | episodes | % episodes recovered | median / p90 same-verb+kind attempts | median / p90 calls | % next call same verb+kind | % next call same verb+kind and ok |
|---|---|---|---|---|---|---|---|
| claude-code, no job source | 3,682 | 2,066 | 79.0 | 1 / 2 | 2 / 13 | 59.1 | 25.9 |
| claude-code, job-sourced | 128 | 78 | 59.0 | 1 / 2 | 1 / 5 | 59.2 | 27.5 |
| codex | 215 | 180 | 40.6 | 1 / 1 | 2 / 8 | 20.5 | 15.2 |
| kimi | 63 | 43 | 79.1 | 1 / 2 | 1 / 6 | 68.9 | 41.0 |
| other (rmcp, mcp) | 151 | 110 | 70.0 | 1 / 3 | 1 / 6 | 68.4 | 40.4 |
| all agent clients | 4,239 | 2,477 | 75.2 | 1 / 2 | 2 / 13 | 58.0 | 26.2 |
| all callers incl. synthetic and bot | 4,527 | 2,764 | 67.6 | 1 / 2 | 2 / 13 | 58.0 | 26.3 |

Codex stands out: low recovery and low same-verb+kind follow-up. About 95 of its 215 errors are one shape (python-kind root/alias mismatch from worktree aliases), spread over about 75 sessions, so recovery is largely a switch of tool or abandonment rather than retry.

Recovery per error type (agent clients, episodes):

| error type | errors | episodes | % recovered | % first same-verb+kind retry ok | % next call same verb+kind and ok |
|---|---|---|---|---|---|
| BadInput | 2,857 | 1,629 | 76.8 | 60.7 | 26.0 |
| NotFound | 569 | 372 | 60.5 | 47.6 | 22.2 |
| Unsupported | 392 | 322 | 79.2 | 69.9 | 38.1 |
| Upstream | 286 | 81 | 86.4 | 50.6 | 15.8 |
| Gone | 131 | 69 | 88.4 | 73.9 | 36.2 |
| Internal | 4 | 4 | 25.0 | 25.0 | 25.0 |

## Error rate per verb x kind

Per verb (all callers): get 226,360 calls / 3,331 errors (1.47%); put 39,722 / 1,305 (3.29%); search 19,294 / 750 (3.89%); edit 6,290 / 380 (6.04%); link 4,527 / 75 (1.66%); tag 3,764 / 202 (5.37%); delete 333 / 64 (19.22%).
Per verb (agent clients): get 44,147 / 2,708 (6.13%); put 11,856 / 640 (5.40%); search 15,022 / 508 (3.38%); edit 3,962 / 224 (5.65%); link 3,712 / 42 (1.13%); tag 2,455 / 89 (3.63%).

Top 30 cells by error count, all callers:

| verb | kind | calls | errors | err % |
|---|---|---|---|---|
| get | youtube | 1,470 | 758 | 51.56 |
| get | gripe | 184,130 | 463 | 0.25 |
| put | finding | 2,628 | 446 | 16.97 |
| put | gripe | 8,737 | 355 | 4.06 |
| get | paper | 8,324 | 318 | 3.82 |
| get | skill | 3,733 | 231 | 6.19 |
| search | memory | 3,968 | 184 | 4.64 |
| put | memory | 1,821 | 169 | 9.28 |
| get | python | 1,081 | 165 | 15.26 |
| get | job | 3,739 | 161 | 4.31 |
| tag | gripe | 1,340 | 143 | 10.67 |
| get | memory | 4,976 | 136 | 2.73 |
| get | todo | 3,867 | 132 | 3.41 |
| search | gripe | 3,558 | 121 | 3.40 |
| put | paper | 2,069 | 121 | 5.85 |
| get | (no kind) | 3,937 | 113 | 2.87 |
| get | draft | 873 | 102 | 11.68 |
| edit | memory | 3,559 | 96 | 2.70 |
| get | pcb | 633 | 89 | 14.06 |
| edit | paper | 856 | 80 | 9.35 |
| get | web | 280 | 80 | 28.57 |
| search | paper | 2,912 | 77 | 2.64 |
| get | quest | 413 | 61 | 14.77 |
| edit | finding | 372 | 55 | 14.78 |
| get | se | 268 | 53 | 19.78 |
| put | todo | 2,267 | 52 | 2.29 |
| search | job | 1,985 | 50 | 2.52 |
| get | perplexity-research | 233 | 48 | 20.60 |
| get | finding | 3,458 | 46 | 1.33 |
| search | skill | 1,194 | 42 | 3.52 |

Top 30 cells by error count, agent clients only:

| verb | kind | calls | errors | err % |
|---|---|---|---|---|
| get | youtube | 1,462 | 757 | 51.78 |
| put | gripe | 4,991 | 243 | 4.87 |
| get | paper | 6,964 | 231 | 3.32 |
| get | skill | 3,449 | 224 | 6.49 |
| put | finding | 1,313 | 222 | 16.91 |
| get | gripe | 9,535 | 172 | 1.80 |
| get | python | 1,053 | 154 | 14.62 |
| get | job | 3,558 | 151 | 4.24 |
| get | memory | 4,489 | 128 | 2.85 |
| get | todo | 3,700 | 127 | 3.43 |
| get | draft | 808 | 91 | 11.26 |
| search | gripe | 3,037 | 86 | 2.83 |
| search | paper | 2,588 | 75 | 2.90 |
| get | (no kind) | 2,749 | 73 | 2.66 |
| edit | paper | 754 | 68 | 9.02 |
| get | pcb | 319 | 67 | 21.00 |
| get | web | 245 | 67 | 27.35 |
| tag | gripe | 832 | 60 | 7.21 |
| get | quest | 301 | 60 | 19.93 |
| get | perplexity-research | 205 | 46 | 22.44 |
| search | job | 1,933 | 44 | 2.28 |
| search | skill | 1,009 | 42 | 4.16 |
| put | todo | 1,715 | 41 | 2.39 |
| search | python | 753 | 39 | 5.18 |
| edit | memory | 1,908 | 38 | 1.99 |
| get | md | 514 | 38 | 7.39 |
| get | pathway | 270 | 37 | 13.70 |
| get | llm | 220 | 37 | 16.82 |
| get | finding | 1,788 | 36 | 2.01 |
| search | todo | 1,162 | 36 | 3.10 |

Concentration caveats: get/youtube is a burst, not a steady rate. About 620 of its 758 errors are the result-over-context-budget refusal from 5 sessions, 618 of the errors landing in three hours of 2026-10-29 (02Z, 03Z, 07Z), plus 46 upstream rate-limit errors. get/gripe is dominated by the Discord bot (184k calls, 0.25%).

## Misuse classes (all 6,107 errors)

| class | errors | % |
|---|---|---|
| result over context budget (get/search refused: reply exceeds the per-call result cap) | 970 | 15.9 |
| unknown view | 480 | 7.9 |
| bad args= / kwarg key | 656 | 10.7 |
| missing / unknown kind | 71 | 1.2 |
| BadInput on mode / extent / arg combination | 126 | 2.1 |
| invalid tag / STATUS / PRIO / relation value | 436 | 7.1 |
| write-gate / content rule (near-duplicate, credential scan, title gate, close-needs-comment, doi clash) | 660 | 10.8 |
| NotFound id / slug / doi / symbol | 683 | 11.2 |
| Gone (soft-deleted ref) | 166 | 2.7 |
| permission / denied | 0 | 0.0 |
| Upstream (db lock, quota, rate limit, outside service) | 418 | 6.8 |
| Internal | 26 | 0.4 |
| Unsupported (verb, kind) pair, not a view | 156 | 2.6 |
| other BadInput | 840 | 13.8 |
| no error_head (pre-0209 row, not otherwise classifiable) | 419 | 6.9 |

The "other BadInput" bucket (13.8%) is a long tail of one-off messages, each under 50 occurrences. Wrong-kind guesses are small (71); calls with a kind the server does not know may be refused before the ledger write (not verified), so this class may be undercounted.

## Top 20 misuse shapes

Misuse here excludes Upstream/Internal (server-side). Shape = ack first line with quoted strings and numbers replaced by X / N. n_sess = distinct sessions (0 means no session: in-process automation without a client label). Example is a representative call skeleton rebuilt from arg_sample (key names; values shown only for kind, view, mode, extent, rel, verdict, sort; everything else as a type).

| # | class | errors | n_sess | ack shape | example call shape |
|---|---|---|---|---|---|
| 1 | result over context budget | 944 | 50 | get: result is N bytes and exceeds PRECIS_TOOL_RESULT_CONTEXT_BYTES (N bytes); no partial result was returned | get(id=str, kind='youtube') |
| 2 | unknown view (Unsupported) | 287 | 178 | unknown view 'X' for kind='X' | get(id=int, kind='todo', view='meta') |
| 3 | invalid tag value | 245 | 0 (automation) | tag value must not contain whitespace ... | put(kind='memory', tags=array, text=str) |
| 4 | write-gate | 227 | 0 (automation) | gripe N has no comment N | get(id=str, kind='gripe', view='comments', __extras__=object) |
| 5 | bad args key | 120 | 86 | search(kind='X') does not accept ['X']; its args= keys are listed below | search(kind='skill', page=int, page_size=int, q=str, scope=str) |
| 6 | NotFound doi | 102 | 44 | paper with DOI 'X' not ingested | get(id=str, kind='paper', view='abstract') |
| 7 | Gone | 102 | 13 | gripe id=N was soft-deleted (row retained for audit; no MCP undo) | put(id=int, kind='gripe', tags=array, text=str) |
| 8 | write-gate | 93 | 42 | closing a gripe needs a newest comment starting 'X' | tag(add=array, id=int, kind='gripe') |
| 9 | NotFound symbol | 61 | 43 | symbol 'X' not found in repo 'X' | get(id=str, kind='python') |
| 10 | NotFound skill | 49 | 44 | skill 'X' not found | get(id=str, kind='skill') |
| 11 | write-gate | 46 | 19 | gripe title would be a mid-phrase fragment ... | put(kind='gripe', tags=array, text=str) |
| 12 | other BadInput | 46 | 45 | Python root mismatch: alias='X', configured='X', expected='X' (codex worktree aliases) | get(id=str, kind='python', view='outline', __extras__=object) |
| 13 | invalid STATUS | 42 | 8 | invalid STATUS value for kind 'X': 'X' | tag(add=array, id=int, kind='gripe') |
| 14 | missing kind | 35 | 24 | missing kind= | get(id=str) |
| 15 | bad args key | 34 | 4 | delete(kind='X') does not accept ['X']; its args= keys are listed below | delete(id=str, kind='pcb', reason=str) |
| 16 | invalid PRIO | 32 | 19 | invalid PRIO value: 'X' - PRIO: takes a name | put(kind='gripe', tags=array, text=str) |
| 17 | unknown view | 29 | 25 | unknown view 'X' for search(kind='X') | search(kind='gripe', page=int, page_size=int, status=str, view='index') |
| 18 | NotFound id | 28 | 11 | job id=N not found | get(id=str, kind='job') |
| 19 | invalid status= | 28 | 19 | invalid status= value for kind 'X': 'X' | search(kind='gripe', page=int, q=str, __extras__=object) |
| 20 | unknown view (list) | 27 | 22 | unknown list view 'X' for kind='X' | get(id=str, kind='llm') |

Next in line (not top 20): put(kind=X) does not accept [...] 26; get(kind=X) does not accept [...] 23; pathway view not recognised 23; paper.search keys not accepted 22; se design not found 21.
Server-side (excluded above): dedup judge failed (Upstream) 104 + 65 + 31; database row locked 56; YouTube RateLimited 46.

Most-rejected arg keys (verb:kind, rejected key, errors, sessions): search:skill scope 30 (26); delete:pcb reason 28 (0, automation); get:todo full 13 (4); put:finding meta 11 (0); search:skill view 11 (10); put:finding reason 10 (1); search:python view 10 (10); link:finding meta 9 (0); search:paper angle+like 9 (8); put:todo title 8 (4).
Most-mistyped views (verb:kind, view, errors, sessions): get:todo meta 27 (17); get:paper meta 14 (14); get:todo full 13 (6); get:gripe timeline 12 (5); get:job meta 12 (10); get:todo index 11 (5); get:llm /budget 10 (10); get:quest meta 10 (10); get:gripe status 9 (9); get:pathway raw 9 (4). view='meta' is the single most repeated wrong guess (about 70 errors over 5 kinds). A local PreToolUse hook hint in the author's own environment advertises view='meta'|'tags' for ref metadata reads; the server rejects both on most kinds. Causality not established.

## Next-hint uptake: method and numbers

Ground truth for the hint is the ack's own next: lines, which only exist in job transcripts. Sample: job refs created in the window with a stored transcript; 105 jobs contained precis tool calls (3,678 calls, 135 error results; 3.7% error rate in this sample vs 7.4% for job-sourced ledger rows). A transcript stream is the sequence of mcp precis calls of one job (sub-agent calls are a separate stream via parent_tool_use_id).

Parsing: each next: line is parsed as verb(kind=..., view=..., ...). Prose next: lines are non-callable. Match levels for the call that follows the error:
- L1: same verb and kind as a suggested call.
- L2: L1, plus the suggested view equals the call's view and every argument key the hint names is present.
- L3: L2, plus every parseable literal value (other than id, q) equal.

| measure (denominator = 130 errors with a callable hint, followed by at least one call) | n | % |
|---|---|---|
| L1 match on the very next call | 13 | 10.0 |
| L1 match and that call succeeded | 10 | 7.7 |
| L2 match on the very next call | 12 | 9.2 |
| L3 match on the very next call | 12 | 9.2 |
| L1 match anywhere in the next 3 calls | 29 | 22.3 |
| L2 / L3 match anywhere in the next 3 calls | 28 | 21.5 |
| (any matching call in the next 3 succeeded) | 29 | 22.3 |

By hint type (an error counts under every type it carries):

| hint type | errors | next call matches (L1) | within 3 calls (L1) |
|---|---|---|---|
| get(kind='skill', ...) pointer | 129 | 10 (7.8%) | 25 (19.4%) |
| other call (different verb/kind than the failed one, not a skill read) | 14 | 1 | 2 |
| same verb+kind as the failed call (retry form) | 3 | 2 | 3 |

Agents ignore the hint more often than they follow it, but they do act: with a callable hint the next call is the same verb+kind in 67 of 130 (51.5%), and a success in 33 (25.4%). Only 5 errors in the sample had no callable hint, too few to compare; the ledger-scale comparison (6.3% skill read after an error vs 3.9% after an ok call) is the better with/without-attention signal. Median attempts from error to first success on the same verb+kind in the sample: 1 (p90 2), 86 of 135 errors recovered.

Implication for the no-hints / desc-* arms: in prod today the executable part of a next: hint is almost always a pointer to a skill, not a rewritten call, so hint uptake is effectively skill-pointer uptake (about 8% immediate, about 19% within 3 calls in cluster-agent transcripts; the ledger proxy gives 6.3% and 12.5% for all agent clients, against baselines of 3.9% and 7.3%).

## Reproducing

Run each file with scripts/prod-psql --ro -f <file>. Window bounds are literals so reruns are comparable. SQL files are listed below verbatim. The transcript extraction writes to a local file; the analyzer reads it with python3 -I (parses lines "ref_id|ord|json", pairs tool_use with tool_result by id, matches the hints as defined above, prints counts only).

q5 command: PRECIS_PROD_PSQL_OPTS="-At" scripts/prod-psql --ro -f q5_transcript_events.sql > events.txt ; python3 -I analyze_hints.py events.txt

## Appendix: exact SQL and analyzer

Files run in order; all read-only via scripts/prod-psql --ro.

~~~sql
-- q1: calls / errors / sessions per caller type, plus grand total and per-verb totals (second statement run separately).
WITH w AS (
  SELECT *, CASE WHEN client LIKE 'asa-bot%' THEN 'asa-bot'
                 WHEN client LIKE 'claude-code/%' AND coalesce(source,'')<>'' THEN 'claude-code job-sourced (cluster agent)'
                 WHEN client LIKE 'claude-code/%' THEN 'claude-code no job source (dev/interactive)'
                 WHEN client LIKE 'codex%' THEN 'codex'
                 WHEN client LIKE 'kimi%' THEN 'kimi'
                 WHEN client LIKE 'devin%' THEN 'devin'
                 WHEN client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' THEN 'synthetic/probe/fleet-report'
                 WHEN coalesce(client,'')='' THEN 'no client label (in-proc/CLI)'
                 ELSE 'other client (rmcp, mcp)' END AS ct
  FROM tool_calls
  WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
)
SELECT ct, count(*) n_calls, count(*) FILTER (WHERE outcome='error') n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) err_pct,
       count(DISTINCT session_id) n_sessions,
       count(DISTINCT session_id) FILTER (WHERE outcome='error') n_sess_with_err
FROM w GROUP BY ROLLUP(ct) ORDER BY n_calls DESC;

-- per-verb totals (run as its own statement):
-- SELECT verb, count(*) n_calls, count(*) FILTER (WHERE outcome='error') n_err,
--        round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) err_pct
-- FROM tool_calls WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
-- GROUP BY ROLLUP(1) ORDER BY n_calls DESC;
-- q2: error rate per verb x kind, top 30 cells by error count. All callers.
SELECT verb, coalesce(kind,'(none)') AS kind, count(*) AS n_calls,
       count(*) FILTER (WHERE outcome='error') AS n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) AS err_pct
FROM tool_calls
WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
GROUP BY 1,2 ORDER BY n_err DESC, n_calls DESC LIMIT 30;
-- q2b: same as q2 but agent clients only (excludes asa-bot, probe/fleet-report/cache/se-box3/scratch/hp99/dl-d1, and unlabelled in-proc/CLI callers).
SELECT verb, coalesce(kind,'(none)') AS kind, count(*) AS n_calls,
       count(*) FILTER (WHERE outcome='error') AS n_err,
       round(100.0*count(*) FILTER (WHERE outcome='error')/count(*),2) AS err_pct
FROM tool_calls
WHERE ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
  AND NOT (client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='')
GROUP BY 1,2 ORDER BY n_err DESC, n_calls DESC LIMIT 30;
-- q3: misuse shapes. Window: [2026-10-10T22:20:00Z, 2026-11-09T22:20:00Z).
-- error_head is populated from 2026-10-15T13:41Z (migration 0209); rows before have only error_type.
WITH e AS (
  SELECT t.*,
    regexp_replace(error_head, '^\[error:[A-Za-z]+\] ', '') AS head,
    CASE
      WHEN error_head ~ 'exceeds PRECIS_TOOL_RESULT_CONTEXT_BYTES' THEN '01 result over context budget'
      WHEN error_head ~* 'unknown (list |[a-z]+ )?view|view ''[^'']*'' not recognised|unknown view=' THEN '02 unknown view'
      WHEN error_head ~* 'does not accept \[|not accepted by|does not take|not applied by a source search' THEN '03 bad args=/kwarg key'
      WHEN error_head ~* 'missing kind=|unknown kind' THEN '04 missing/unknown kind'
      WHEN error_type = 'BadInput' AND error_head ~* '\mmode\M|extent=|cannot combine|select inside view|q= finds inside|dry_run=|replace-only|only supports' THEN '05 BadInput on mode/extent/arg combination'
      WHEN error_type = 'BadInput' AND error_head ~* 'tag value must|invalid (STATUS|PRIO|status)|PRIO:|unknown thread|unknown relation' THEN '06 invalid tag/STATUS/PRIO/relation value'
      WHEN error_type = 'BadInput' AND error_head ~* 'near-duplicate|credential|mid-phrase|closing a gripe|already belongs|merge title gate|requires cited_in|text= and body=|has no comment|find=.* not found' THEN '07 write-gate / content rule'
      WHEN error_type = 'NotFound' THEN '08 NotFound id/slug/doi/symbol'
      WHEN error_type = 'Gone' THEN '09 Gone (soft-deleted ref)'
      WHEN error_type IN ('Denied','Forbidden') OR error_head ~* 'denied|permission' THEN '10 permission/denied'
      WHEN error_type = 'Upstream' THEN '11 Upstream (lock/quota/outside service)'
      WHEN error_type = 'Internal' THEN '12 Internal'
      WHEN error_type = 'Unsupported' THEN '13 Unsupported (verb,kind) pair'
      WHEN error_head IS NULL THEN '99 no error_head (pre-0209 row)'
      ELSE '14 other BadInput'
    END AS cls
  FROM tool_calls t
  WHERE outcome='error' AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
)
SELECT cls, count(*) AS n_err, round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
FROM e GROUP BY cls ORDER BY cls;
-- q3b: top misuse shapes with class, sessions, and a representative call skeleton.
WITH e AS (
  SELECT t.*,
    regexp_replace(error_head, '^\[error:[A-Za-z]+\] ', '') AS head,
    CASE
      WHEN error_head ~ 'exceeds PRECIS_TOOL_RESULT_CONTEXT_BYTES' THEN '01 result over context budget'
      WHEN error_head ~* 'unknown (list |[a-z]+ )?view|view ''[^'']*'' not recognised|unknown view=' THEN '02 unknown view'
      WHEN error_head ~* 'does not accept \[|not accepted by|does not take|not applied by a source search' THEN '03 bad args=/kwarg key'
      WHEN error_head ~* 'missing kind=|unknown kind' THEN '04 missing/unknown kind'
      WHEN error_type = 'BadInput' AND error_head ~* '\mmode\M|extent=|cannot combine|select inside view|q= finds inside|dry_run=|replace-only|only supports' THEN '05 BadInput on mode/extent/arg combination'
      WHEN error_type = 'BadInput' AND error_head ~* 'tag value must|invalid (STATUS|PRIO|status)|PRIO:|unknown thread|unknown relation' THEN '06 invalid tag/STATUS/PRIO/relation value'
      WHEN error_type = 'BadInput' AND error_head ~* 'near-duplicate|credential|mid-phrase|closing a gripe|already belongs|merge title gate|requires cited_in|text= and body=|has no comment|find=.* not found' THEN '07 write-gate / content rule'
      WHEN error_type = 'NotFound' THEN '08 NotFound id/slug/doi/symbol'
      WHEN error_type = 'Gone' THEN '09 Gone (soft-deleted ref)'
      WHEN error_type IN ('Denied','Forbidden') OR error_head ~* 'denied|permission' THEN '10 permission/denied'
      WHEN error_type = 'Upstream' THEN '11 Upstream (lock/quota/outside service)'
      WHEN error_type = 'Internal' THEN '12 Internal'
      WHEN error_type = 'Unsupported' THEN '13 Unsupported (verb,kind) pair'
      WHEN error_head IS NULL THEN '99 no error_head (pre-0209 row)'
      ELSE '14 other BadInput'
    END AS cls
  FROM tool_calls t
  WHERE outcome='error' AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), sk AS (
  SELECT e.*,
    left(regexp_replace(regexp_replace(regexp_replace(head, '''[^'']*''', '''X''', 'g'), '"[^"]*"', '"X"', 'g'), '[0-9]+', 'N', 'g'), 100) AS shape,
    verb || '(' || coalesce((
      SELECT string_agg(j.key || '=' ||
        CASE WHEN j.key IN ('kind','view','mode','extent','rel','verdict','sort') AND jsonb_typeof(j.value)='string' THEN quote_literal(j.value #>> '{}')
             WHEN jsonb_typeof(j.value)='string' THEN '<str>'
             WHEN jsonb_typeof(j.value)='number' THEN '<int>'
             ELSE '<' || jsonb_typeof(j.value) || '>' END, ', ' ORDER BY j.key)
      FROM jsonb_each(e.arg_sample) j), '') || ')' AS skeleton
  FROM e WHERE error_head IS NOT NULL
)
SELECT cls, count(*) AS n_err, count(DISTINCT session_id) AS n_sessions, shape,
       mode() WITHIN GROUP (ORDER BY skeleton) AS example_call_shape
FROM sk GROUP BY cls, shape ORDER BY n_err DESC LIMIT 45;
-- q3c: (a) most-rejected arg keys; (b) most-mistyped views. Same window.
WITH e AS (
  SELECT verb, kind, error_head, session_id
  FROM tool_calls
  WHERE outcome='error' AND error_head IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), keys AS (
  SELECT verb, kind, session_id,
         (regexp_match(error_head, '\[(''[^\]]*'')\]'))[1] AS keylist
  FROM e
  WHERE error_head ~* 'does not accept \[|not accepted by'
), views AS (
  SELECT verb, kind, session_id,
         coalesce((regexp_match(error_head, 'view[= ]''([^'']*)'''))[1], (regexp_match(error_head, 'unknown [a-z]+ view ''([^'']*)'''))[1], (regexp_match(error_head, '''([^'']*)'' not recognised'))[1]) AS bad_view
  FROM e
  WHERE error_head ~* 'unknown (list |[a-z]+ )?view|not recognised|unknown view='
)
(SELECT 'rejected_keys' AS section, verb || ':' || coalesce(kind,'-') AS vk, keylist AS val, count(*) AS n, count(DISTINCT session_id) AS sess FROM keys GROUP BY 1,2,3 ORDER BY n DESC LIMIT 15)
UNION ALL
(SELECT 'unknown_view', verb || ':' || coalesce(kind,'-'), bad_view, count(*), count(DISTINCT session_id) FROM views GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 15)
ORDER BY section, n DESC;
-- q4: error -> next call / first success on same verb+kind, per session (ledger only).
-- Sessions = tool_calls.session_id (one per MCP connection), ordered by seq. Rows without session_id are excluded.
WITH w AS (
  SELECT call_id, session_id, seq, ts, verb, coalesce(kind,'(none)') AS kind, outcome, error_type,
    CASE WHEN client LIKE 'asa-bot%' THEN 'asa-bot'
         WHEN client LIKE 'claude-code/%' AND coalesce(source,'')<>'' THEN 'claude-code job-sourced (cluster agent)'
         WHEN client LIKE 'claude-code/%' THEN 'claude-code no job source (dev/interactive)'
         WHEN client LIKE 'codex%' THEN 'codex'
         WHEN client LIKE 'kimi%' THEN 'kimi'
         WHEN client LIKE 'devin%' THEN 'devin'
         WHEN client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' THEN 'synthetic/probe/fleet-report'
         WHEN coalesce(client,'')='' THEN 'no client label (in-proc/CLI)'
         ELSE 'other client (rmcp, mcp)' END AS ct
  FROM tool_calls
  WHERE session_id IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), b AS (
  SELECT w.*,
    row_number() OVER (PARTITION BY session_id ORDER BY seq, call_id) AS rn,
    row_number() OVER (PARTITION BY session_id, verb, kind ORDER BY seq, call_id) AS vk_rn
  FROM w
), c AS (
  SELECT b.*,
    lead(verb)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_verb,
    lead(kind)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_kind,
    lead(outcome) OVER (PARTITION BY session_id ORDER BY rn) AS nx_outcome,
    lag(outcome)  OVER (PARTITION BY session_id, verb, kind ORDER BY rn) AS prev_vk_outcome,
    min(rn)    FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_rn,
    min(vk_rn) FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_vk_rn
  FROM b
), e AS (
  SELECT *, (nx_verb IS NOT NULL) AS has_next,
         (nx_verb = verb AND nx_kind = kind) AS next_same_vk,
         (prev_vk_outcome IS DISTINCT FROM 'error') AS is_episode_start,
         (succ_rn - rn) AS calls_to_ok, (succ_vk_rn - vk_rn) AS vk_attempts_to_ok
  FROM c WHERE outcome='error'
)
SELECT coalesce(ct,'ALL') AS ct, coalesce(error_type,'ALL') AS error_type,
  count(*) AS n_err,
  count(*) FILTER (WHERE has_next) AS n_with_next_call,
  round(100.0*count(*) FILTER (WHERE next_same_vk)/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk_ok,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE next_same_vk),0),1) AS pct_ok_given_same_vk,
  count(*) FILTER (WHERE is_episode_start) AS n_episodes,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND succ_rn IS NOT NULL)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_episodes_recovered,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS med_vk_attempts,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS p90_vk_attempts,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS med_calls,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS p90_calls,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok=1)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_on_next_same_vk,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok<=3)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_within_3_same_vk
FROM e
GROUP BY GROUPING SETS ((), (ct), (error_type))
ORDER BY (ct IS NULL AND error_type IS NULL) DESC, ct NULLS FIRST, n_err DESC;
-- q4b (agent clients only): error -> next call / first success on same verb+kind, per session (ledger only).
-- Sessions = tool_calls.session_id (one per MCP connection), ordered by seq. Rows without session_id are excluded.
WITH w AS (
  SELECT call_id, session_id, seq, ts, verb, coalesce(kind,'(none)') AS kind, outcome, error_type,
    CASE WHEN client LIKE 'asa-bot%' THEN 'asa-bot'
         WHEN client LIKE 'claude-code/%' AND coalesce(source,'')<>'' THEN 'claude-code job-sourced (cluster agent)'
         WHEN client LIKE 'claude-code/%' THEN 'claude-code no job source (dev/interactive)'
         WHEN client LIKE 'codex%' THEN 'codex'
         WHEN client LIKE 'kimi%' THEN 'kimi'
         WHEN client LIKE 'devin%' THEN 'devin'
         WHEN client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' THEN 'synthetic/probe/fleet-report'
         WHEN coalesce(client,'')='' THEN 'no client label (in-proc/CLI)'
         ELSE 'other client (rmcp, mcp)' END AS ct
  FROM tool_calls
  WHERE session_id IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), b AS (
  SELECT w.*,
    row_number() OVER (PARTITION BY session_id ORDER BY seq, call_id) AS rn,
    row_number() OVER (PARTITION BY session_id, verb, kind ORDER BY seq, call_id) AS vk_rn
  FROM w
), c AS (
  SELECT b.*,
    lead(verb)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_verb,
    lead(kind)    OVER (PARTITION BY session_id ORDER BY rn) AS nx_kind,
    lead(outcome) OVER (PARTITION BY session_id ORDER BY rn) AS nx_outcome,
    lag(outcome)  OVER (PARTITION BY session_id, verb, kind ORDER BY rn) AS prev_vk_outcome,
    min(rn)    FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_rn,
    min(vk_rn) FILTER (WHERE outcome='ok') OVER (PARTITION BY session_id, verb, kind ORDER BY rn ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS succ_vk_rn
  FROM b
), e AS (
  SELECT *, (nx_verb IS NOT NULL) AS has_next,
         (nx_verb = verb AND nx_kind = kind) AS next_same_vk,
         (prev_vk_outcome IS DISTINCT FROM 'error') AS is_episode_start,
         (succ_rn - rn) AS calls_to_ok, (succ_vk_rn - vk_rn) AS vk_attempts_to_ok
  FROM c WHERE outcome='error' AND ct NOT IN ('synthetic/probe/fleet-report','asa-bot','no client label (in-proc/CLI)')
)
SELECT coalesce(ct,'ALL') AS ct, coalesce(error_type,'ALL') AS error_type,
  count(*) AS n_err,
  count(*) FILTER (WHERE has_next) AS n_with_next_call,
  round(100.0*count(*) FILTER (WHERE next_same_vk)/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE has_next),0),1) AS pct_next_same_vk_ok,
  round(100.0*count(*) FILTER (WHERE next_same_vk AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE next_same_vk),0),1) AS pct_ok_given_same_vk,
  count(*) FILTER (WHERE is_episode_start) AS n_episodes,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND succ_rn IS NOT NULL)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_episodes_recovered,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS med_vk_attempts,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY vk_attempts_to_ok) FILTER (WHERE is_episode_start) AS p90_vk_attempts,
  percentile_disc(0.5) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS med_calls,
  percentile_disc(0.9) WITHIN GROUP (ORDER BY calls_to_ok) FILTER (WHERE is_episode_start) AS p90_calls,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok=1)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_on_next_same_vk,
  round(100.0*count(*) FILTER (WHERE is_episode_start AND vk_attempts_to_ok<=3)/nullif(count(*) FILTER (WHERE is_episode_start),0),1) AS pct_ep_ok_within_3_same_vk
FROM e
GROUP BY GROUPING SETS ((), (ct), (error_type))
ORDER BY (ct IS NULL AND error_type IS NULL) DESC, ct NULLS FIRST, n_err DESC;
-- q4c: when the next call in the session is the same verb+kind, was it a blind re-send (identical arg_sample) or an edited call?
WITH w AS (
  SELECT call_id, session_id, seq, verb, coalesce(kind,'(none)') AS kind, outcome, error_type, arg_sample, input_keys,
    CASE WHEN client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='' THEN 0 ELSE 1 END AS is_agent
  FROM tool_calls
  WHERE session_id IS NOT NULL AND arg_sample IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
), c AS (
  SELECT w.*,
    row_number() OVER (PARTITION BY session_id ORDER BY seq, call_id) AS rn,
    lead(verb) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_verb,
    lead(kind) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_kind,
    lead(outcome) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_outcome,
    lead(arg_sample) OVER (PARTITION BY session_id ORDER BY seq, call_id) AS nx_arg
  FROM w
)
SELECT count(*) AS n_err_next_same_vk,
  count(*) FILTER (WHERE nx_arg = arg_sample) AS n_identical_resend,
  round(100.0*count(*) FILTER (WHERE nx_arg = arg_sample)/count(*),1) AS pct_identical_resend,
  round(100.0*count(*) FILTER (WHERE nx_arg = arg_sample AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE nx_arg = arg_sample),0),1) AS pct_ok_when_identical,
  round(100.0*count(*) FILTER (WHERE nx_arg <> arg_sample AND nx_outcome='ok')/nullif(count(*) FILTER (WHERE nx_arg <> arg_sample),0),1) AS pct_ok_when_edited
FROM c
WHERE outcome='error' AND is_agent=1 AND nx_verb = verb AND nx_kind = kind;
-- q4d: ledger-scale proxy for following a `next: get(kind='skill', ...)` pointer: after an error,
-- is the immediately next call (or one of the next 3) a get on kind=skill? Agent clients only.
-- Baseline = share of all agent-client calls that are get(kind=skill).
WITH w AS (
  SELECT call_id, session_id, seq, verb, coalesce(kind,'(none)') AS kind, outcome
  FROM tool_calls
  WHERE session_id IS NOT NULL
    AND ts >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND ts < TIMESTAMPTZ '2026-11-09 22:20:00+00'
    AND NOT (client LIKE 'asa-bot%' OR client LIKE 'probe/%' OR client LIKE 'fleet-report/%' OR client LIKE 'cache-mcp%' OR client LIKE 'se-box3%' OR client LIKE 'scratch%' OR client LIKE 'hp99%' OR client LIKE 'dl-d1%' OR coalesce(client,'')='')
), c AS (
  SELECT w.*,
    (lead(verb)  OVER s = 'get' AND lead(kind)  OVER s = 'skill') AS n1_skill,
    (lead(verb,2) OVER s = 'get' AND lead(kind,2) OVER s = 'skill') AS n2_skill,
    (lead(verb,3) OVER s = 'get' AND lead(kind,3) OVER s = 'skill') AS n3_skill,
    lead(verb) OVER s AS nx
  FROM w WINDOW s AS (PARTITION BY session_id ORDER BY seq, call_id)
)
SELECT count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL) AS n_err_with_next,
  round(100.0*count(*) FILTER (WHERE outcome='error' AND n1_skill)/nullif(count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL),0),1) AS pct_next_is_skill_read,
  round(100.0*count(*) FILTER (WHERE outcome='error' AND (n1_skill OR n2_skill OR n3_skill))/nullif(count(*) FILTER (WHERE outcome='error' AND nx IS NOT NULL),0),1) AS pct_skill_read_within_3,
  round(100.0*count(*) FILTER (WHERE outcome='ok' AND n1_skill)/nullif(count(*) FILTER (WHERE outcome='ok' AND nx IS NOT NULL),0),1) AS baseline_pct_next_is_skill_read_after_ok,
  round(100.0*count(*) FILTER (WHERE outcome='ok' AND (n1_skill OR n2_skill OR n3_skill))/nullif(count(*) FILTER (WHERE outcome='ok' AND nx IS NOT NULL),0),1) AS baseline_pct_skill_read_within_3_after_ok
FROM c;
-- q5: tool_use / tool_result events from cluster-job transcripts (refs.meta.transcript, stream-json lines).
-- Only source that keeps the full ack text incl. its `next:` lines. Emits ref_id|ord|line, filtered server-side.
SELECT ref_id || '|' || ord || '|' || line
FROM (
  SELECT r.ref_id, x.ord, x.line
  FROM refs r,
       LATERAL regexp_split_to_table(r.meta->>'transcript', E'\n') WITH ORDINALITY AS x(line, ord)
  WHERE r.kind = 'job' AND r.meta ? 'transcript'
    AND r.created_at >= TIMESTAMPTZ '2026-10-10 22:20:00+00' AND r.created_at < TIMESTAMPTZ '2026-11-09 22:20:00+00'
) s
WHERE line ~ '"type": ?"tool_(use|result)"' AND (line ~ 'mcp__precis__' OR line ~ 'tool_result')
ORDER BY ref_id, ord;

~~~

(The q5 SQL above was run exactly as shown. analyze_hints.py is the python below.)

~~~python
import ast, json, re, sys, statistics
from collections import defaultdict, Counter

BS = chr(92)
DQ = chr(34)
SQ = chr(39)
OPEN = "([" + chr(123)
CLOSE = ")]" + chr(125)
CALL_RE = re.compile(r"(get|search|put|edit|delete|tag|link)[(](.*)[)]" + r"\s*", re.S)

def split_top(s):
    parts, depth, q, cur = [], 0, None, []
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == BS and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
            elif ch == q:
                q = None
        elif ch in (SQ, DQ):
            q = ch
            cur.append(ch)
        elif ch in OPEN:
            depth += 1
            cur.append(ch)
        elif ch in CLOSE:
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]

def parse_hint(line):
    m = CALL_RE.fullmatch(line.strip())
    if not m:
        return None
    verb, argstr = m.group(1), m.group(2)
    args = dict()
    for p in split_top(argstr):
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        if not re.fullmatch(r"[a-z_]+", k):
            continue
        try:
            args[k] = ast.literal_eval(v.strip())
        except Exception:
            args[k] = ...
    return dict(verb=verb, args=args)

def result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return chr(10).join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""

streams = defaultdict(list)       # (ref, parent) -> list of call dicts in order
results = dict()                  # tool_use_id -> (text, is_error)
nlines = 0
for raw in open(sys.argv[1], encoding="utf-8"):
    parts = raw.rstrip(chr(10)).split("|", 2)
    if len(parts) != 3 or not parts[2].startswith(chr(123)):
        continue
    nlines += 1
    try:
        o = json.loads(parts[2])
    except Exception:
        continue
    ref = parts[0]
    msg = o.get("message") or dict()
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    parent = o.get("parent_tool_use_id")
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and str(b.get("name", "")).startswith("mcp__precis__"):
            verb = b["name"].split("__")[-1]
            inp = b.get("input") or dict()
            streams[(ref, parent)].append(dict(id=b["id"], verb=verb, kind=inp.get("kind"),
                                               view=inp.get("view"), keys=set(inp), inp=inp))
        elif b.get("type") == "tool_result":
            t = result_text(b.get("content"))
            results[b.get("tool_use_id")] = (t, bool(b.get("is_error")) or t.lstrip().startswith("[error:"))

def is_err(call):
    r = results.get(call["id"])
    return None if r is None else r[1]

def matches(hint, call, level):
    if call["verb"] != hint["verb"] or (call["kind"] or None) != (hint["args"].get("kind") or None):
        return False
    if level == 1:
        return True
    hv = hint["args"].get("view")
    if hv is not None and hv is not ... and call["view"] != hv:
        return False
    hk = set(hint["args"])
    if not hk <= call["keys"]:
        return False
    if level == 2:
        return True
    for k, v in hint["args"].items():
        if v is ... or k in ("id", "q"):
            continue
        if call["inp"].get(k) != v:
            return False
    return True

stat = Counter()
rec_attempts = []
by_cls = defaultdict(Counter)
for key, calls in streams.items():
    for i, c in enumerate(calls):
        if is_err(c) is not True:
            continue
        txt = results[c["id"]][0]
        stat["errors"] += 1
        m = re.match(r"\s*\[error:([A-Za-z]+)\]", txt)
        et = m.group(1) if m else "Unknown"
        nxt = [l.split("next:", 1)[1].strip() for l in txt.splitlines() if l.strip().startswith("next:")]
        hints = [h for h in (parse_hint(n) for n in nxt) if h]
        has_call = bool(hints)
        stat["with_next_line"] += bool(nxt)
        stat["with_callable_hint"] += has_call
        following = calls[i + 1:i + 4]
        same = [x for x in calls[i + 1:] if x["verb"] == c["verb"] and x["kind"] == c["kind"]]
        for n, f in enumerate(same, 1):
            if is_err(f) is False:
                rec_attempts.append(n)
                break
        if not following:
            stat["no_following_call"] += 1
            continue
        f1 = following[0]
        grp = "hint" if has_call else "nohint"
        stat[grp + "_n"] += 1
        sv = (f1["verb"] == c["verb"] and f1["kind"] == c["kind"])
        stat[grp + "_next_same_vk"] += sv
        stat[grp + "_next_same_vk_ok"] += (sv and is_err(f1) is False)
        by_cls[et][grp + "_n"] += 1
        if has_call:
            for lvl in (1, 2, 3):
                hit1 = any(matches(h, f1, lvl) for h in hints)
                stat["L%d_next_match" % lvl] += hit1
                stat["L%d_next_match_ok" % lvl] += (hit1 and is_err(f1) is False)
                stat["L%d_within3_match" % lvl] += any(matches(h, f, lvl) for h in hints for f in following)
                stat["L%d_within3_match_ok" % lvl] += any(matches(h, f, lvl) and is_err(f) is False for h in hints for f in following)
            by_cls[et]["L1_hit"] += any(matches(h, f1, 1) for h in hints)
            by_cls[et]["L2_hit"] += any(matches(h, f1, 2) for h in hints)
print("event lines parsed:", nlines, "streams:", len(streams), "precis calls:", sum(len(v) for v in streams.values()))
print("jobs:", len(set(k[0] for k in streams)))
for k in sorted(stat):
    print("%s\t%s" % (k, stat[k]))
if rec_attempts:
    rec_attempts.sort()
    print("recovered_errors", len(rec_attempts), "median_attempts", statistics.median(rec_attempts),
          "p90", rec_attempts[int(0.9 * (len(rec_attempts) - 1))])
for et, c in sorted(by_cls.items()):
    print("cls", et, dict(c))

print("--- by hint class (an error counts in every class it carries a hint of) ---")
def hclass(h, c):
    if h["verb"] == "get" and h["args"].get("kind") == "skill":
        return "skill_read"
    if h["verb"] == c["verb"] and h["args"].get("kind") == c["kind"]:
        return "retry_same_verb_kind"
    return "other_call"
st3 = Counter()
for key, calls in streams.items():
    for i, c in enumerate(calls):
        if is_err(c) is not True:
            continue
        txt = results[c["id"]][0]
        nxt = [l.split("next:", 1)[1].strip() for l in txt.splitlines() if l.strip().startswith("next:")]
        hints = [h for h in (parse_hint(n) for n in nxt) if h]
        following = calls[i + 1:i + 4]
        if not following or not hints:
            continue
        f1 = following[0]
        for name in ("skill_read", "retry_same_verb_kind", "other_call"):
            hs = [h for h in hints if hclass(h, c) == name]
            if not hs:
                continue
            st3[name + "_n"] += 1
            st3[name + "_L1_next"] += any(matches(h, f1, 1) for h in hs)
            st3[name + "_L2_next"] += any(matches(h, f1, 2) for h in hs)
            st3[name + "_L2_next_ok"] += any(matches(h, f1, 2) for h in hs) and is_err(f1) is False
            st3[name + "_L2_within3"] += any(matches(h, f, 2) for h in hs for f in following)
            st3[name + "_L1_within3"] += any(matches(h, f, 1) for h in hs for f in following)
for k in sorted(st3):
    print("%s\t%s" % (k, st3[k]))

~~~
