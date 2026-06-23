---
id: precis-auto-tasks-help
title: precis — wait-for-condition todos via meta.auto_check
summary: wait-for-condition todos — SQL-checkable evaluators, parking leaves, auto-resolve, timeouts
applies-to: put (kind='todo' with meta.auto_check); precis worker --only auto_check
status: active
---

# precis-auto-tasks-help — wait-for-condition todos

A todo can be a leaf that the agent *parks* until a SQL-checkable
condition flips. The worker polls every cycle; when the condition
holds it flips the leaf to `STATUS:done` and appends an
`auto-resolved` event. When the optional `timeout_at` is in the
past, the worker flips to `STATUS:auto-timeout` instead (so the
nursery sweep surfaces the abandoned wait for triage).

The mechanism is one JSON dict on the existing
`refs.meta` column — no schema work needed.

```python
put(kind='todo',
    text='[auto] wait for paper 10.x/y1 ingested+indexed',
    parent_id=98,
    meta={'auto_check': {
        'type': 'paper_ingested',
        'doi': '10.x/y1',
    }})
```

The handler validates the `type` against the registered evaluators
at write time, so a typo lands as an error, not a silently-broken
leaf.

## Evaluator catalogue

| `type` | Resolves true when | Required args |
|---|---|---|
| `paper_ingested` | A `paper` ref with the given identifier exists and has ≥1 embedded chunk | one of `doi` / `arxiv` / `s2` / `pubmed` |
| `discord_reply_received` | A memory tagged `replied-to:<ask_message_id>` exists | `ask_message_id` |
| `time_past` | `now() >= at` (ISO 8601 timestamp) | `at` |
| `tag_present` | At least one live ref carries the given tag | `tag` (optional `kind` to narrow) |
| `child_job_succeeded` | A non-deleted child `kind='job'` of *this leaf* hits `STATUS:succeeded`. Auto-injected by the dispatch worker when a writer sets `meta.executor` but no `auto_check` (Slice 5) | none — scoped to the calling leaf's children |

All shapes accept the optional `timeout_at` field. When the
timeout passes before the evaluator resolves, the leaf flips to
`STATUS:auto-timeout` rather than `STATUS:done`. No further
evaluation happens on a timed-out leaf.

## Pattern 1 — wait on the ingest pipeline

A discovery worker finds three DOIs, queues each for ingest, then
parks consumer work behind them:

```python
for doi in ['10.x/y1', '10.x/y2', '10.x/y3']:
    put(kind='paper', ref={'doi': doi})            # queue ingest
    wait = put(kind='todo',
               parent_id=98,
               text=f'[auto] wait for paper {doi} ingested+indexed',
               meta={'auto_check': {
                   'type': 'paper_ingested',
                   'doi': doi,
                   # Surface stalled fetches after a week.
                   'timeout_at': '2026-06-20T00:00:00+00:00',
               }})
    link(kind='todo', id=108, target=f'todo:{wait.id}', rel='blocked-by')

tag(kind='todo', id=103, add=['STATUS:done'])     # discovery is done
```

The consumer leaf (`td108`) drops out of `view='doable'` until every
linked `wait` resolves.

## Pattern 2 — ask the owner on Discord

```python
msg = put(kind='message',
          text='Owner: should §3 cite Tanaka 2024, or skip?',
          target='discord/<guild>/<channel>/<thread>')

ask = put(kind='todo',
          parent_id=98,
          text='Decide: cite Tanaka 2024 in §3 — asked the owner',
          tags=['ask-user'],
          meta={'auto_check': {
              'type': 'discord_reply_received',
              'ask_message_id': str(msg.id),
              'thread': 'discord/<guild>/<channel>/<thread>',
          }})

link(kind='todo', id=consumer_leaf, target=f'todo:{ask.id}',
     rel='blocked-by')
```

The chatter side detects the owner's in-thread reply and stamps a
memory `replied-to:<msg_id>`; the auto-check worker resolves the
ask on the next tick.

## Pattern 3 — scheduled wake

A leaf that should reappear next week:

```python
put(kind='todo',
    text='Revisit the API rate-limit decision',
    meta={'auto_check': {
        'type': 'time_past',
        'at': '2026-06-20T09:00:00+00:00',
    }})
```

The leaf carries `STATUS:open` until the timestamp passes — and
the `auto_check` flow then flips it to `done`, which moves the
"revisit" out of the doable view. (If the intent is to *re-open*
the leaf next week, write a sibling that point instead — the
auto-check surface is fire-once by design.)

## Pattern 4 — wait for a child job to succeed (Slice 5)

Mostly written by the dispatch worker automatically. When you
write a todo with `meta.executor` set and no `meta.auto_check`,
the dispatch worker auto-injects this:

```python
put(kind='todo',
    text='Fix gripe:42',
    parent_id=engineering_hygiene_strategic,
    meta={'executor': 'claude_inproc',
          'job_type':  'fix_gripe',
          # auto_check auto-injected by dispatch worker:
          # 'auto_check': {'type': 'child_job_succeeded'}
          })
```

You can write it explicitly to make the wait visible:

```python
meta={'auto_check': {'type': 'child_job_succeeded'},
      'executor': 'claude_inproc',
      'job_type':  'fix_gripe'}
```

When the dispatched job hits `STATUS:succeeded`, the parent flips
to `STATUS:done`. When the job *fails*, the parent gets a
`child-failed:<job_id>` open tag instead — see `precis-job-help`.
The `auto_check` doesn't resolve on failure; the bubble flag is
the signal that the parent needs human (or asa-bot) attention.

## Running the worker

```sh
precis worker --only auto_check            # drain alone
precis worker                              # default cycle includes auto_check
```

The pass is cheap — SQL-only, no LLM, no embeddings — so it runs
in the default rotation. Polling cadence matches the worker's
`--idle-seconds` (default 2s for active cycles, longer when idle).

## See also

```python
get(kind='skill', id='precis-tasks-help')         # the tree itself
get(kind='skill', id='precis-tags')               # STATUS / PRIO / open tag rules
get(kind='skill', id='precis-relations')          # blocked-by + note-for links
```
