# MCP authoring — the call contract

**A call either does exactly what its ack says or refuses with a next
step. Misuse that would succeed silently is a bug.**

Applies to every handler behind `get/search/put/edit/delete/tag/link`.
Origin: the 2026-10-17 misuse audit and gr494831 (umbrella), gr494725.

## The rules

1. **The ack is the effect.** Say what happened, including what it
   replaced or removed: `STATUS:queued -> STATUS:cancelled`, `deleted
   memory id=N 'title'`. If the effect is wider than the call looks
   (whole draft, a displaced closed-axis tag, dropped rels), the ack
   names it.
2. **A refusal names the fix.** Every `raise <PrecisError>` in a handler
   carries `next=` (one copy-pasteable call) or `options=` (the closed
   vocabulary), or its message already points at `get(kind='skill', ...)`.
   `next=` is a call that works, not prose ("try again" is not a next).
3. **Scope by kind.** Ids share one global ref space. A verb on
   `kind=X` never touches a ref of another kind: resolve it through the
   kind-scoped lookup first, so a foreign id is `NotFound`/`Gone` with
   options, not a success message about the wrong object.
4. **Wider blast radius, explicit door.** Anything that retires a whole
   document or ends a lifecycle needs its own confirm (`args={'confirm':
   ...}`) or lifecycle door, not a default path.
5. **Validate before writing.** Parse tags/args and check preconditions
   before the first write; a call that raises changed nothing.
6. **No silent drops.** An accepted kwarg is read; an unread one is
   refused (dispatcher gate), not ignored.
7. **Unsupported is typed.** An unsupported (verb, kind) pair is
   `Unsupported` with `options=` (the supported verbs) and a `next=`.
8. **Library faults are `Upstream` with a retry hint; library text never
   reaches the agent.** The dispatcher maps them (below); handlers do not
   interpolate `{exc}` of a driver or HTTP exception into a message.
9. **One reading: do it and say so. Two readings: refuse.** An alias, a
   near-miss enum value or an obvious wrong key runs as the canonical
   call, and the reply's first line names the reading
   (`handlers/_interpret.py`: `interpreted: view='full' read as
   view='comments'`). The write is exactly the announced call, nothing
   wider. Any input with a second plausible reading (`PRIO:3`,
   `STATUS:closed`) still refuses with `options=`. A call that can spend
   (a paid API, a rate-limited fetch) is never run on the caller's
   behalf: the refusal names the exact call and the cost.

## Enforced by

| rule | gate |
|---|---|
| 2 | `tests/test_mcp_raise_next_ratchet.py`: one-way floor of hint-less raises, keyed `file::function::Class` in `tests/data/mcp_raise_next_baseline.json`. New raise without `next=`/`options=` fails; a fix that lowers the count fails until the baseline is lowered (`--write`, which refuses to raise). |
| 7 | `tests/test_unsupported_verb_matrix.py`: enumerates every unsupported (verb, kind) over the live hub. |
| 8 | `runtime/dispatch.py::_boundary_rows` (psycopg lock/pool/timeout/operational, httpx timeout/status/transport, `TimeoutError`, `ConnectionError`) and `scrub_upstream`; `tests/test_dispatch_boundary_map.py` has a case per row. A new row needs a case or `test_every_row_has_a_case` fails. |
| 1, 3–6 | per-handler tests; reviewers check the ack against the effect. |
| 9 | per-pattern tests, each with an ambiguous input that still refuses (`tests/test_forgiving_inputs.py`). |

## Adding a hint to a pinned raise

Add `next=`/`options=` at the raise, run the ratchet, then
`uv run python tests/test_mcp_raise_next_ratchet.py --write` and commit the
lowered baseline in the same commit. A raise that is truly
self-describing (`chunk not found` with the id in the message) still
takes a `next=`: the cheapest honest one is the listing call.

### `# no-next:` pragma (approved exemption)

Only a raise with no useful hint may skip `next=`: put `# no-next:
<category> — <reason>` on its first line or the line directly above.
`terminal` = no caller action changes the outcome (clock read, a hard
invariant the caller cannot influence); `internal` = server bug or broken
invariant, already reads as Internal. Reason >= 4 words; other categories
fail. Exempt counts are a ceiling in `tests/data/mcp_raise_next_exempt.json`:
adding a pragma fails until that file is bumped in the same diff, removing one
until it is lowered. Burn-down builders add pragmas only for genuine terminal
or internal cases and list each one in the commit body.

## Boot path

Nothing before `mcp.run()` (`server.main()` → `_init_runtime`, instructions
preamble) may scale with a mounted tree. The dev-stdio launcher mounts all of
`~/work` as `PRECIS_ROOT`; an unbounded `os.walk` there pushed the <!-- home-path: allow — the launcher mount at incident time -->
`initialize` reply past Claude Code's 30 s CONNECT_TIMEOUT. Background-thread
it or budget it (`server.py::_FILE_COUNT_BUDGET_S`, 1 s, preamble shows `≥N`).
Diagnose by timing the handshake: pipe one `initialize` JSON-RPC line into
the launcher and timestamp the reply against the container's stderr.

## Observability

`workers/call_errors.py` clusters BadInput, Unsupported, NotFound, Gone
and Internal from the tool-call ledger (`tool_calls.error_head`) into
gripes; an Internal cluster is classed `bug` (a server fault or an unmapped
boundary). Upstream stays out: it is another system's outage.

`scripts/hint-followthrough [--days N]` reports, per (verb, kind, error
family), how often the same session's next call to that kind within 2 calls
succeeded (SQL and grouping: `workers/call_errors.py::hint_followthrough`).
Low ok% means the `next=` did not land.

## Transient retry

`dispatch` retries lock-not-available, pool-timeout, serialization and
deadlock faults up to 3 tries with jittered backoff, only for calls that
are safe to replay: `get`/`search` on local-database kinds (not kinds that
call a paid or outside service), and `put(kind='job', idem_key=...)` (job
puts dedupe on an active key). `edit`/`delete`/`tag`/`link` and any other
`put` never replay, since a handler may have committed one step before the
fault.
A fault that persists surfaces as `Upstream` saying it was retried N times.
Handlers do not retry on their own for these faults.

Calls a handler makes to an outside HTTP service go through
`utils/http.py::with_backoff` / `call_with_backoff` (limiter, jitter,
`Retry-After`, breaker); a 429 is `RateLimited`, never "not found". See
[Outbound calls are polite](invariants.md#outbound-calls-are-polite).
