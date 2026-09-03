---
status: draft
title: "71 handler kwargs silently dropped by tools/core.py's put/edit — triage for the parity ratchet"
---

# The parity ratchet found 71, not 4

`tools/core.py::put` and `::edit` each double as (a) the FastMCP-derived JSON
Schema advertised over MCP and (b) a hand-maintained dispatch payload dict. A
handler kwarg missing from either side of that verb function is unreachable
over MCP with **no error at all** — every handler carries a `**_kw: Any`
catch-all, so the dropped kwarg just vanishes.

Gripe 262482 / gripe 250273 diagnosed and fixed two instances (`finding.put`'s
`wants=`/`provenance=` — acquisition mode; `paper.edit`'s `doi=`/`arxiv=` —
stub upgrade). Both came with a 0.95-confidence fix and a request for a
generic guard so the class stops recurring silently. That guard
(`tests/test_mcp_verb_kwarg_parity.py`) walks every registered handler's
`put`/`edit` method via `inspect.signature` and diffs it against
`tools/core.py`'s declared kwargs — and on its first run found **71** more
instances across 25 kinds, entirely pre-existing and out of scope for that
fix.

They are frozen in `_KNOWN_GAPS` in that test file (a ratchet: entries leave
only by being fixed, never by being deleted to silence a failure) so the
guard is green today and still catches every *new* instance from here on.
This item is the triage those 71 need before anyone works through them.

## Important limitation: this only checks the signature half

The guard confirms a kwarg is **declared** on `tools/core.py::put`/`::edit`.
It does not check that the hand-maintained dispatch payload dict actually
**forwards** it to `_dispatch` — a kwarg can be added to the signature and
still silently dropped one line later if the payload dict entry is
forgotten. That half needs a value round-tripped through a live call per
kwarg (what `test_mcp_put_edit_kwarg_doors.py` does for the four kwargs this
fix wired through). Fixing any bucket-A item below needs both halves, not
just silencing the signature-diff.

## Bucket A — real gaps (the kwarg is the documented/only way to do something)

High-impact, called out by name:

- **`draft.put(image=)`** — self-evidencing: `tools/core.py:818`'s own
  comment documents `image=<base64> for an uploaded image`, and the
  signature never declares it. The door documents a parameter it drops.
- **`todo.put(prio=)`** — self-evidencing: `handlers/todo.py:193` raises an
  error whose text tells the agent to `put(prio=N)`. Same self-inflicted
  shape as `planner_prompt.py` teaching the broken `wants=` call
  (gr262482's "NEW EVIDENCE" comment) — the door teaches a call it then
  rejects. This is why the operational workaround for expediting a job is
  raw SQL (`UPDATE refs SET prio=1`, see memory
  `job_claim_prio_direction_flipped`) instead of the tool surface.
- **`protein.put(sequence=)`** (+ `engine=`/`requested_by=`/`seeds=`) — a
  protein-structure mint with no `sequence=` reachable is not a mint at all;
  worth confirming whether `protein.put` is callable over MCP in *any* shape
  today.
- **`llm.put(model_id=)`** (+ `capability=`/`offerings=`/`served_by=`/
  `tier_floor=`) — the entire variant-precise catalog-mint surface from the
  `llm` catalog proposal (memory `llm_catalog_proposal`: "shipped but dark").
  Consistent with that memory note — this may simply be unactivated rather
  than actively wanted, but it means the catalog can't be seeded via MCP at
  all right now.
- **`route.put(engine=)`** (+ `max_steps=`/`requested_by=`) — same shape as
  protein: a reaction-route mint with no `engine=` looks unusable over MCP.
- **`paper.edit(year=)`** (+ `abstract=`/`journal=`/`entry_type=`), and the
  same four fields on **`cfp.edit`**, plus **`datasheet.edit(part_lcsc=/
  subtype=/vendor=)`** — bibliographic/metadata repair. `PaperHandler.edit`'s
  own docstring documents these as the intended "fix a wrong year, missing
  abstract" affordance; before this fix pass only `title=`/`authors=`
  reached the wire (this fix adds `doi=`/`arxiv=` — these four remain
  unreachable). Directly adjacent to what this fix touched; deliberately
  left out of scope (see below).
- **`memory.put(rule=)`** / **`memory.put(warrant=)`** (+ the `edit` twins)
  — the documented D3 argument-graph shortcut
  (`put(kind='memory', rule='modus-ponens', warrant='...')`) is completely
  unreachable over MCP; only the two-step create-then-edit path works.
- **`finding.edit(unacquirable_mode=)`** / **`unacquirable_note=`** — the
  dead-end flip side of the exact acquisition-mode feature this fix just
  opened the mint door for. Worth fixing in the same pass as any follow-up
  acquisition-mode work.
- ~~**`pcb.put(args=)`** / **`structure.put(args=)`**~~ — **CLOSED
  2026-08-28** (`gr267461`). `tools/core.py::put` grew the top-level
  `args:` tunnel, mirroring `get()`. This was not a long-tail nicety: every
  pcb write op (`place`, `route`, `plane_net`, `pin_side`) travels through
  `args`, so the entire pcb write surface was unreachable from the MCP tool
  while looking fully wired. **`structure.edit(args=)` remains open** — no
  verb needs it today, and it stays on the ledger rather than being
  exempted so that wiring it is a deliberate act.
- **`plan.put(belief=/status=)`** / **`plan.edit(belief=/cursor=/status=)`**
  — `precis-plan-help.md` documents `plan` as agent-facing (not purely
  internal planner state), so this is reachable-in-principle debt too.

Lower-urgency / administrative:

- `auto_refresh_days` on `anki`/`concept`/`folder`/`memory`/`todo` `put` —
  the cache-decay refresh knob (Model A relevance decay); a backend tuning
  parameter, not something an agent typically needs to set at mint time.
- `figure`/`mermaid` `put(viewbox=/vocab=)` — diagram-kind rendering knobs.
- `draft.put(copy_of=/lang=/mime=/origin=/permission=/voice=)` — note that
  `voice=`/`lang=`/`origin=`/`permission=` **already exist** on `edit()`
  (narration routing / figure provenance) but not on `put()` — so setting
  them at *creation* time is broken while editing an existing chunk to add
  them works. Narrower gap than it looks.
- `draft.edit(list_kind=/source=/style=)`, `pres.edit(bibtex_type=/date=/
  note=/url=/venue=)` — more metadata-repair fields, same shape as the
  paper/cfp/datasheet bucket above but lower-traffic kinds. (`meta=` on
  both was fixed by gr301897 — routed through the `__extras__`
  accepted-kwargs gate, with a loud `BadInput` on kinds that don't
  declare it, e.g. `todo`.)
- `job.put(requires=/select=)` — job submission gating; unclear how often
  an agent (vs. only internal callers) needs to set these directly.
- `message.put(attachments=)` — conversation/thread attachments.

## Bucket B — legitimately exempt

**None found.** The obvious candidate — `args=`, the generic MCP
extras-tunnel `get()`/`search()` already expose — turned out NOT to be a
different legitimate door for `put`/`edit`. See below. `_EXEMPT` in the test
file is deliberately left empty rather than seeded with a guess.

### `args=` is not actually exempt

`precis-pcb-help.md` documents `put(kind="pcb", id="s", args={"autoplace":
{...}})` as the real calling convention, and `cli_adapter.py` explicitly
**skips** `args` when building the CLI's flag mapping ("Skip 'args'
parameter for CLI - it's complex and rarely used") — so `args=` isn't
CLI-only either. The actual gap: `tools/core.py::put`/`::edit` never
declared a top-level `args: dict | None = None` extras-tunnel parameter at
all (only `get()`/`search()` did — see `_invoke_handler`'s "Handlers that opt
into an explicit `args: dict` parameter (today: `random.get`...)" comment,
which is now stale: `pcb.put`, `structure.put`, `structure.edit` also opted
in, but `put`/`edit` never grew the top-level tunnel to carry it). Fixing
this is a small, structural, one-time change (add `args=` to `put()`/
`edit()`'s signature + the `__extras__` forward, mirroring `get()`) that
would clear three ledger entries at once — a good first pick for whoever
takes this backlog item. **Done for `put` on 2026-08-28**; `edit` still has
no tunnel. Note this was the THIRD time the same remedy was written down in
`tools/core.py` (see the notes at `put`'s `finding` and `paper.edit` call
sites, citing gr262482/gr250273) without anyone adding the parameter —
`tests/test_tool_args_reachability.py` is the first mechanism that can
actually fail when a handler declares `args=` and the tool cannot pass it.

## Bucket C — unclear (needs someone to check before triaging further)

- `job.put(requires=/select=)` — didn't verify whether these are meant to be
  agent-set at submit time or are internal-only fields the job worker
  writes; check `handlers/job.py` before deciding real-gap vs. exempt.
- `llm.put(...)` catalog fields — per memory `llm_catalog_proposal` the
  surface is "shipped but dark"; unclear whether activating MCP reachability
  is wanted yet or premature (the catalog itself may not be seeded).
- `protein.put`/`route.put` — didn't confirm whether either kind is reachable
  over MCP in *some* working shape today (e.g. via a different required-arg
  combination) or is fully dead; check before assuming these are the
  blocking gap.

## Sizing: declaring all 71 has a wire cost, and `args=` is how you avoid it

Every kwarg added to a verb signature is permanent `tools/list` payload — sent
to every agent, every session. `tests/test_token_budget.py` guards it, and
wiring just the four kwargs this pass added pushed the cap from 22 KB to
23 KB. That file's own recorded per-param costs run ~30 B (the 2026-08-05
draft-edit batch: "~240 B" for eight) to ~110 B (the 2026-07-05 search batch:
"~330 B" for three), so clearing all 71 by declaration would add roughly
**2–7 KB** — call it 23 KB → 25-30 KB.

That is not a cliff, and no single bucket-A fix should be blocked on it. But
it compounds in a way worth naming: **the cap has been raised 10 times, 16 KB
→ 23 KB**, and every bump is individually justified with the same phrase —
"schema-side growth only, same shape as the prior bumps." Each one is true.
The aggregate is a 44% growth in a payload every agent pays for before it does
any work.

This is the real argument for the `args=` extras tunnel above. It is not just
"a good first pick that clears three entries" — it is the only fix shape whose
cost does not scale with the number of gaps. Recommended split:

- **Declare** the bucket-A kwargs an agent reaches for by name in normal work
  (`draft.put(image=)`, `todo.put(prio=)`, `paper.edit(year=)`, …). Discoverability
  in the schema is the whole point for these; pay the bytes.
- **Tunnel** the long tail through `args=` — the administrative knobs
  (`auto_refresh_days`, `viewbox`/`vocab`, `job.put(requires=/select=)`) that
  a caller only ever sets deliberately, having already read the help skill.

Whoever takes this item should decide that split *first*; it changes what
"fixing" each ledger entry means.

## Note: this bug class recurs on a schedule

`tests/test_token_budget.py`'s 2026-08-05 entry describes gr192827 item 5 in
exactly the words of this gripe pair — params "documented in
`precis-draft-help` and accepted by the draft handler, but previously absent
from the tool's JSON schema so strict-schema clients could not send them."
That is the same defect, found and fixed three weeks earlier, by someone who
then wrote a one-off test for it rather than a guard. Counting that one:
`put(kind='job', parent_id=…)`, the hypothesis kwargs, gr192827 item 5, and
now gr262482/gr250273 — **four independent discoveries of one class**, each
fixed narrowly. The ratchet exists so there is no fifth.

## Fix recipe (once triaged)

For any bucket-A item: add the kwarg to `tools/core.py`'s verb signature
(with a comment mirroring the surrounding kwarg blocks — kind, purpose, the
owning skill) **and** the dispatch payload dict, add a live round-trip test
(mint/edit through `tools_core.put`/`.edit` against a real store, not just
the handler directly — see `test_mcp_put_edit_kwarg_doors.py` for the
pattern), then delete the corresponding line from `_KNOWN_GAPS` in
`tests/test_mcp_verb_kwarg_parity.py`. The ratchet test fails until the
ledger line is removed, so there's no way to fix the kwarg and forget the
ledger.
