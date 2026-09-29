# mine-sessions

The committed extraction layer for every pass that asks *what happened in
recent agent sessions*. It reads transcripts and telemetry, normalizes them
into one event stream, computes a scoreboard, runs a catalogue of friction
detectors, and renders evidence cards small enough for a subagent to judge.

It judges nothing. Judging is a pass — see `docs/runbooks/surface-review.md`
and the `/surface-review` command.

## Why this exists

Three passes already mine these transcripts (`surface-review`,
`token-review`, `skill-search-review`) and until now every one of them
re-derived its own parser in a session scratchpad. `skill-search-review.md`
said so outright: *"the 2026-07 audit's scratch scripts are the reference
implementation; re-derive them (they live in a session scratchpad, not the
tree)."* That is why passes were rare and why pass N's numbers could not be
compared with pass N−1's. This directory is the fix: the extraction is code,
`scoreboard.json` carries a `schema_version`, and a pass starts by reading
numbers rather than by writing a parser.

## Corpora

| Flag | Source | What it uniquely gives |
|---|---|---|
| `--local` | `~/.claude/projects/*/*.jsonl` **and** `*/subagents/agent-*.jsonl` | The richest corpus: full tool stream, exact per-turn tokens from `message.usage`, non-precis detours, and user corrections. 91% of files are sidechains — an earlier audit's glob missed them entirely, so the sidechain shape is pinned by a test. |
| `--ledger` | `tool_calls` (migration 0133) | Fleet-wide verb/kind/outcome/latency rates. **No payload, ever** — `input_keys` is argument *names* only. Targeting, not causes. |
| `--llmlog` | `llm_call_log` | Model/tier/cost/latency per LLM call, and `data_parsed` as a prompt-quality signal. |
| `--jobs` | `refs.meta->>'transcript'` on `kind='job'` | Server-side tool streams. Nearly empty now (`plan_tick` is down to a couple of runs a week) — kept because the window predates that, and because `doctor_tick` still writes here. |

The prod three hop the cluster read-only through `scripts/prod-psql`. Without
`--prod` the run is local-only and needs no network.

`--jobs` must export with `\copy (…) TO STDOUT WITH (FORMAT csv)`. Plain
`TO STDOUT` COPY-escapes `\n`/`\t`/`\\` and the lines will not re-parse as
JSON.

## Redaction contract

Session transcripts contain the exact things this repo's secret gate exists
to keep out — tailnet and LAN addresses, prod DSNs, pasted credentials —
because a session that debugged the cluster quotes the cluster. Evidence
cards get read by subagents and pasted into gripes, and the repo is public.

So: every free-text field is passed through `redact.scrub()` on the **write**
side, before truncation (a secret split across a head-slice boundary is still
caught). The address patterns are the same ones
`tests/test_deploy_tree_no_secrets.py` enforces tree-wide, so a card that
passes here also passes the gate.

`scrub()` is a filter, not a proof — which is why the artefacts land
**outside the repo**.

### Artefacts go to a cache dir, not into the tree

`outdir.py` puts everything under
`${XDG_CACHE_HOME:-~/.cache}/precis-mine-sessions/<worktree>/`. This is not a
tidiness choice. `tests/test_deploy_tree_no_secrets.py` scans the **working
tree**, not git's index, so a gitignored `out/` is still scanned — and mined
transcripts are the single most likely content in this project to hold a
tailnet address or a prod DSN. An in-tree `out/` meant that *running the
miner reddened the secret gate*, whose only tempting fix is an exemption
marker. That is precisely the wrong lesson. Override with `MINE_OUT`.

## Layout

```
outdir.py    where artefacts go (cache dir, outside the repo)
extract.py   every corpus  → events.jsonl   (normalized Event JSONL)
stats.py     events        → scoreboard.{md,json}
detect.py    events        → candidates.json
cards.py     both          → cards/**, cards/INDEX.md
schema.py    the Event contract shared by all of them
redact.py    the scrub, shared by every writer
run.sh       the stages in order, for one window
```

## Use

```sh
scripts/mine-sessions/run.sh --since 7d             # local corpus
scripts/mine-sessions/run.sh --since 14d --prod     # + ledger/llmlog/jobs
scripts/mine-sessions/run.sh --since 2d --limit 20  # smoke run
```

`run.sh` prints the artefact paths when it finishes. Read `scoreboard.md`
(one screen), then hand `cards/<detector>/` to a `forensics` agent, one
directory per agent. Raw transcripts should never reach a main loop — that is
what the cards are for.

## Detectors

`detect.py` holds the catalogue with a docstring per detector stating what a
hit *means* and which fix class it implies. The short version, in the order
that matters:

`exec_class` runs first and segments blocked / rejected / hang / zero / ok —
only `ok` and `zero` are surface-quality data, and an earlier audit's
conclusions inverted when it skipped this step. Then the error family
(`hard_error`, `retry_to_success`, `spin`), the shape-hunting family
(`reformulation`, `vocab_near_miss`, `skill_taught_wrong`), the
capability-gap family (`abandon_detour`, `detour_census`), and the economy
family (`render_obesity`, `read_then_unused`). `correction_roundtrip` is
local-only and is the cheapest class to fix: a human saying "no, I meant…"
right after a tool sequence names the doc that taught it wrong.

`cards.py --random N` adds an unbiased arm: uniformly sampled windows with no
detector attached. Without it the catalogue only ever confirms shapes we
already thought of.

## Calibration

The measured error rate on this surface is **0.8%** (609 errors in 73,472
calls over 10 days). Errors are not where the waste is. Read the scoreboard's
byte and retry columns before the error table.
