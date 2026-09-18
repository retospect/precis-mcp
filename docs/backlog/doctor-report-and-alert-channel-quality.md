---
status: ready
title: Doctor report — needs-a-human todos, worker-log access, and the fix lane (rulings 2026-09-18)
prio: high
model: opus
---

# Doctor report — needs-a-human todos, worker-log access, and the fix lane

Approved by Reto 2026-09-18 off the morning doctor report (`dr346343`) and
the reading cast (`dr346440`). Pieces 1–4 of the original five shipped the
same day (doctor-report preamble strip, truncated-summary rejection,
abbreviation allowlist, orphan-alert retirement + the `alert_backlog_rot`
self-count fix); what follows is what is left.

## gr225018 — RULED 2026-09-18 (option 1: keep the invariant, stamp the roots) — executed

Reto's ruling: keep the `meta.rotation_root` invariant; stamp the live
top-level project trees strategic by hand. Done the same day on prod via
`tag(kind='todo', id=…, meta={'tier': 'strategic'})` for 16 roots (se+hexfold
paper `td344088`, Parkinson `td243956`/`td244453`, capability landscape
`td259464`, AIxMAT `td261219`, autocatpath validation paper `td262043`, pcb
preprint `td266176`, seed book `td273157`, glass ball `td328426`, photonic
arm `td330494`, EWOD applicability `td346939`, DARPin hunt `td250137`,
pareto-boxel `td262875`, place+route spec review `td264483`, EWOD-in-oil
sources `td338212`, catalyst poisoning `td261041`).

Deliberately NOT stamped (not rotation units): the 25 `waiting-for:reto`
decision rows of 2026-09-17, the 17 `OPEN:ephemeral` autocatpath aggregate
roots, the 4 `[auto] wait for <doi>` stubs, and ~12 parentless cite/assess
chores (six GROUNDING AUDIT ledgers from 2026-08-24/25, six nano-computer
citation chores of 2026-09-13). Those last ~12 are the true residue of the
gripe: re-parent under a draft root or close. The orphan detector stays
silent (`_NO_ALERT`), so nothing nags about them. Close `gr225018` once the
~12 are re-parented or closed; the comment-18 correction is its record.

## Doctor-report design — RULED 2026-09-18

| # | Proposal | Ruling |
|---|---|---|
| 1 | annotate a gripe only when classification/localization/disposition changes | **no** — cadence stays as is |
| 2 | give the doctor read access instead of the daily "no access" ritual | **provide access** → piece A |
| 3 | "needs a human" becomes a `waiting-for:reto` todo, not prose | **yes** → piece B |
| 4 | wire the fix lane (`fix_gripe` ran 0× in 14 d vs 249× `diagnose_gripe`) | **yes** → piece C |

### Piece A — a read-only `worker_logs` surface the doctor can reach

**Now.** `doctor-prompt.md` tells the model `worker_logs` lines are readable
via `search`/`get`, but no handler serves that table (`grep worker_logs
src/precis/handlers src/precis/tools` → nothing). The doctor runs
`claude_inproc` with `_REVIEWER_DISALLOWED_TOOLS` (`workers/review.py`:
Bash/Write/Edit/WebFetch/WebSearch + precis edit/delete/tag/link), so its
only reads are MCP `search`/`get`. Hence the daily "no worker_logs/Bash
access this tick" line, and two of the three "needs a human" items on
`dr346343` that were answerable in minutes with log access.

**Build.** A `get(kind='job', id='/logs?…')` view (job kind already owns
the worker/handler vocabulary; `worker_logs` rows come from
`utils/db_log_handler.py`). Params: `handler=`, `host=`, `level=` (default
`WARNING`+), `since=` (default 24 h), `q=` substring, `limit=` (cap 200
rows, newest first). Output: one line per row `ts host handler level
message` (message truncated at 300 chars), then a count line with the
cutoff. Register in `precis-toolpath-help` and the job-kind skill; list it
in `doctor-prompt.md` step where evidence is gathered, replacing the
"if you had logs" wording. No host `journalctl` digest in this piece —
the in-proc executor has no ssh, and the table already carries the worker
loggers' output; revisit only if the doctor names a gap the table can't
answer.

**Tests.** view returns rows filtered by each param; cap honoured; empty
window says so; the deny list still blocks nothing on `get`.

### Piece B — "Needs a human" bullets become `waiting-for:reto` todos

**Now.** The section is prose the model writes and nothing parses
(`doctor_report.py` only strips the preamble). Reto's queue is
`search(kind='todo', tags=['waiting-for:reto'])`; the report's paragraphs
never reach it.

**Build.** In `doctor_report.py`, after `strip_preamble`, parse the
`## Needs a human` section: each top-level bullet is one item. For each,
mint `put(kind='todo', text=<bullet>, tags=['waiting-for:reto'],
parent_id=<doctor recurring root>)` — under the doctor's own recurring
parent so it is not an orphan and not a rotation unit. Dedup: normalise the
bullet (lowercase, strip `gr<id>`/`al<id>`/numbers/dates), sha1 it into
`meta.doctor_ask_key`; skip when an open todo with that key exists (bump
`meta.seen_count` instead). Rewrite the section in the filed report as
`- td<id>: <first line>` so the report links to the queue. Close-out is
Reto's: swapping `waiting-for:reto` for `STATUS:done`.

**Tests.** three bullets → three todos under the recurring root; re-run
with same bullets → zero new, seen_count bumped; a bullet whose only change
is a gr-id/number dedups; section rewritten with td ids; no section → no
todos.

### Piece C — wire diagnosis → fix — **SHIPPED** 2026-09-18

**Now (pre-ship).** `diagnose_scan` mints `diagnose_gripe` per open undiagnosed gripe;
on success it appends a `DIAGNOSIS (auto, job …)` comment and, only if
`PRECIS_DIAGNOSE_AUTOPROMOTE=1` and confidence ≥ 0.8, tags `OPEN:auto-fix`.
The ONLY minter of `fix_gripe` todos is `backlog_groom`
(`_mint_todo_for_gripe`), which grooms EVERY open gripe not tagged
`no-groom`, has no `default_profiles`, and registers only under
`PRECIS_BACKLOG_GROOM_ENABLED` — unset in prod (not in the worker-agent
deploy template). So the lane is dark by construction: the groomer was too
indiscriminate to turn on, and nothing narrower exists.

**Build.**
1. `backlog_groom`: select only gripes tagged `OPEN:auto-fix` (a diagnosis
   said "fixable, confidence ≥ 0.8") that have no live `fix_gripe` todo
   and no `no-groom`. Drop the "every open gripe" behaviour; keep the
   6 h cadence and the advisory lock.
2. `fix_gripe` todo mint carries `meta.params.diagnosis_job_id` so the fix
   brief is the diagnosis comment + gripe body, not the whole timeline.
3. Deploy: export `PRECIS_DIAGNOSE_AUTOPROMOTE=1` and
   `PRECIS_BACKLOG_GROOM_ENABLED=1` in the worker-agent template on the
   node that runs `fix_gripe` (needs `PRECIS_FIX_REPO_DIR`/`WORK_DIR` and
   the agent container on; the job is fail-closed without it — gr179498).
   Env-config-vs-CLI gap applies: unset in the template = default off.
4. Budget: cap fix mints at N per groom pass (env, default 3) so a burst of
   confident diagnoses cannot fan out into a dozen clones.

**Tests.** groomer skips an open gripe without `auto-fix`; mints for one
with it; skips when a live fix todo exists; cap honoured; diagnosis job id
lands in params.

**Order.** C first (it is why the doctor's output is unread), then B, then
A. Each is one coder round; A and B are independent of C.
