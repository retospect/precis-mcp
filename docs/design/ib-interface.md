# Interactive Brokers interface — plan

Status: **queued** — plan captured for a future implementation slice.

A read-mostly bridge to a locally-running **Interactive Brokers TWS /
IB Gateway** over the socket API (`ib_async`). It exposes three new
kinds so an agent can:

- **pull portfolio status** — account summary + open positions + PnL,
- **pull quotes** — live market-data snapshots by symbol,
- **tee up orders** — stage an order into TWS *armed but un-transmitted*
  (`order.transmit = False`), **never** sending it.

The last point is the load-bearing safety property: **no code path in
this subsystem ever transmits an order.** Staging leaves the order
sitting grey/un-transmitted in the human's TWS Orders panel; the human
must click *Transmit* by hand. Precis can create and cancel staged
orders and preview their margin impact, and that is the whole of its
write surface.

## Why socket (ib_async), not the Client Portal REST gateway

Decided with the user (2026-07-04): use the socket API against
TWS / IB Gateway via **`ib_async`** (the community-maintained
successor to the archived `ib_insync`).

- Matches the common algo-trading setup (Gateway already running).
- `order.transmit = False` is the native, well-understood "stage but
  don't send" primitive.
- Cost: `ib_async` is asyncio and a **new top-level dependency** →
  needs an ADR (AGENTS.md "Don't introduce a new top-level dependency
  without an ADR") and an async-in-sync-threads seam (below).

The Client Portal REST alternative (httpx, `/whatif` preview) is
recorded under *Open decisions* as the fallback if the socket seam
proves too fragile.

## Connection seam — `src/precis/ib/`

FastMCP runs sync tool callables in a worker-thread pool; `ib_async`
is asyncio with a persistent socket. We isolate that mismatch in one
place, mirroring the `RemoteEmbedder` / dream-loop transport-seam
philosophy (a thin module that hides the awkward I/O and presents a
sync API).

- **`precis/ib/client.py`** — `IBClient`, a process singleton that owns
  a dedicated asyncio event loop running on a background thread. It
  connects lazily and marshals each handler call across the thread
  boundary with `asyncio.run_coroutine_threadsafe(...).result(timeout)`.
  A single `clientId` per process; auto-reconnect with backoff on
  socket drop. Presents a **sync** API: `positions()`, `account_summary()`,
  `snapshot(symbols)`, `whatif(order_spec)`, `stage(order_spec)`,
  `cancel(order_id)`, `staged()`.
- **`precis/ib/spec.py`** — plain dataclasses (`OrderSpec`, `Quote`,
  `Position`, `AccountSummary`) so handlers and tests never touch
  `ib_async` types directly. Keeps `ib_async` an optional import.
- **`precis/ib/fake.py`** — an in-memory `FakeIBClient` implementing the
  same protocol with canned data, injected in tests (mirrors
  `FakeEmbedder` / dream's `FakeLLM`). CI never talks to a live gateway.

`ib_async` is imported only inside `client.py` via
`require_optional("ib_async", extra="ib")`, so a deployment without the
extra installed simply reports the kinds as unavailable.

### Config / gating (env)

| var | meaning | default |
|-----|---------|---------|
| `PRECIS_IB_ENABLE` | master gate; hides all three kinds when unset | off |
| `PRECIS_IB_HOST` | gateway host | `127.0.0.1` |
| `PRECIS_IB_PORT` | `7497` paper / `7496` live / `4002` / `4001` | `7497` |
| `PRECIS_IB_CLIENT_ID` | API client id | `17` |
| `PRECIS_IB_ACCOUNT` | default account id (multi-account setups) | first |
| `PRECIS_IB_ALLOW_STAGE` | gate on the `order` write surface | off |
| `PRECIS_IB_ALLOW_LIVE` | required to stage against a live-port account | off |
| `PRECIS_IB_MAX_QTY` | per-order quantity cap | required if staging |
| `PRECIS_IB_MAX_NOTIONAL` | per-order notional cap (USD) | required if staging |

Read kinds (`broker`, `quote`) need only `PRECIS_IB_ENABLE`; the
`order` kind additionally requires `PRECIS_IB_ALLOW_STAGE`, declared via
`KindSpec.requires_env` so the dispatcher hides it until deliberately
enabled.

## Kinds

Three kinds, each with its own mental model / skill / list view.

### `broker` — portfolio status (cache-backed, read-only)

Subclasses `CacheBackedHandler` (`provider='ibkr'`), so it inherits
TTL, freshness, and the attribution footer for free.

- `get(kind='broker')` — bare get lists the default account summary
  (NetLiq, buying power, maintenance margin, realized/unrealized PnL)
  plus open positions (symbol, qty, avg cost, mkt value, unrealized).
  `id_required=False`, `role='system'`.
- `get(kind='broker', id='<accountId>')` — a specific account.
- Short TTL (default **60s**) — portfolio drifts intraday; a minute of
  staleness is a fine cost/freshness trade, and `mode='refresh'` forces
  a live pull. Human-interaction cadence, not HFT, so the
  delete-and-reinsert churn in `put_cache_entry` is acceptable.

### `quote` — market-data snapshots (cache-backed, read-only)

Subclasses `CacheBackedHandler` (`provider='ibkr'`).

- `get(kind='quote', id='AAPL')` — bid / ask / last / volume /
  bid-size / ask-size / (delayed flag). Contract resolution: bare
  symbol → US-stock `SMART` by default; richer forms
  (`AAPL:SMART:USD`, `ESZ5:CME` for futures) parsed by a small
  `_canonical_key` grammar.
- `id='AAPL,MSFT'` — multi-symbol batch in one response.
- Very short TTL (default **10s**). Falls back to *delayed* data
  (`reqMarketDataType(3)`) when the account lacks a live-data
  subscription, flagged clearly in the render.
- **Data-licensing note in attribution**: IB market data is licensed
  and generally non-redistributable. The local personal cache is fine;
  the attribution footer states the source and warns against
  redistribution.

### `order` — staged (armed, un-transmitted) orders

A numeric-ref kind (like `todo` / `job`): `put` / `get` / `delete` /
`tag` / `link`. **Not** cache-backed. `role='stream'`.

- `put(kind='order', ...)` — build an `OrderSpec` (symbol, side,
  qty, type ∈ {MKT, LMT, STP, STP LMT}, limit/stop px, TIF), run
  `whatIfOrder` for a margin/commission preview, enforce caps, then
  `placeOrder` with **`transmit=False`**. Records a ref
  (`STATUS:staged`) with `orderId` / `permId` / the whatif preview in
  `meta`. Response spells out: *"Staged in TWS, NOT sent. Open TWS →
  Orders and click Transmit to execute."*
- `get(kind='order')` — list staged orders (reconciled against TWS
  open-orders on read so out-of-band transmits/cancels show up).
- `get(kind='order', id=N)` — one staged order + its whatif preview.
- `delete(kind='order', id=N)` — `cancelOrder` (safe: reduces risk),
  mark `STATUS:cancelled`.
- **There is no transmit verb.** Staging is the terminal state Precis
  can reach.

## Safety model (hard guards, mirror the dream `supersede` guards)

1. **`transmit` is a hardcoded `False`** at the single `placeOrder`
   call site. No parameter, env var, or verb can set it True. A unit
   test greps `src/precis/ib/` and the `order` handler for
   `transmit=True` / `transmit = True` and fails if found.
2. **Staging gated off** by default (`PRECIS_IB_ALLOW_STAGE`); the
   `order` kind is invisible otherwise.
3. **Live-account fence** — if the port maps to a live gateway
   (`7496`/`4001`), staging is refused unless `PRECIS_IB_ALLOW_LIVE=1`.
4. **Caps** — reject any order exceeding `PRECIS_IB_MAX_QTY` or
   `PRECIS_IB_MAX_NOTIONAL`.
5. **Whatif gate** — refuse to stage if `whatIfOrder` reports a margin
   violation; always surface the preview.
6. **Read kinds are side-effect-free** — `broker` / `quote` issue only
   `req*` calls, never `placeOrder`.

## Storage / migration

`0053_ib_kinds.sql`:

- Register `broker`, `quote`, `order` in the `kinds` table.
- Insert the `ibkr` row in `providers`; map `broker` + `quote` to it in
  `kind_provider` (cache path).
- `order` participates in the `STATUS:` closed tag axis
  (`staged` / `cancelled` / `filled`), gated per-kind like other
  lifecycle kinds.

No new content tables — `broker`/`quote` reuse `cache_state` + `chunks`
via `put_cache_entry`; `order` reuses `refs` + numeric-ref plumbing.
Old migrations stay sealed; `precis migrate --dry-run` against a
throwaway DB should show only `0053` pending.

## Dependency + ADR

- New optional extra `[ib]` → `ib_async`. Handlers gate via
  `require_optional("ib_async", extra="ib")`; cold-start and CI without
  the extra are unaffected.
- **ADR `00NN-ib-async-dependency.md`** — records: why `ib_async`
  (only maintained IB Python client; `ib_insync` archived; official
  `ibapi` is lower-level, no asyncio ergonomics), why optional-extra
  not top-level, and the async-loop-in-background-thread seam decision.

## Testing

- **Offline unit tests** with `FakeIBClient`: portfolio render, quote
  render (live + delayed), multi-symbol batch, order staging happy path
  + every guard (caps, live fence, whatif violation, gate-off).
- **Guard regression**: assert no `transmit=True` anywhere in the IB
  subsystem; assert `order` kind hidden without `PRECIS_IB_ALLOW_STAGE`.
- **Cache-flow tests** reuse the existing cache-backed handler harness
  (TTL, `mode='refresh'`).
- **No live gateway in CI.** An optional, env-gated smoke script
  (`scripts/ib-smoke.py`) can hit a paper gateway locally.
- Full check before done:
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest`.

## Phasing

- **Phase 0** — seam (`precis/ib/`), `FakeIBClient`, optional dep + ADR,
  migration `0053` (kinds + provider), dispatch gating.
- **Phase 1** — `broker` + `quote` cache-backed read handlers + skills
  (`precis-broker-help`, `precis-quote-help`).
- **Phase 2** — `order` staging (put/get/delete) + full guard set +
  whatif preview + `precis-order-help` skill.
- **Phase 3 (optional)** — `precis ib …` CLI wrappers; a positions
  panel in `precis_web`; `alert`/`watch` hooks on position or PnL
  thresholds.

## Open decisions

- **Contract grammar** — how much beyond US equities to parse in v1
  (options/futures/FX legs) for `quote` and `order`.
- **Live-data entitlement** — auto-fall-back to delayed vs hard-error
  when no market-data subscription; delayed is the proposed default.
- **Persistence of quotes** — cache-backed (searchable holdings history,
  ref churn) vs pure live compute (no refs, like `calc`). Plan assumes
  cache-backed with short TTL.
- **Fallback transport** — keep the Client Portal REST path documented
  as plan B if the socket seam is operationally painful.
