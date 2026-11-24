# Runbook — enabling the taproot chase pipeline (Phase 2)

Turns on the two dark passes shipped in Phase 1 (plan
`transient-napping-parrot`): `chase_trigger` (ingest→claim due-marker) and
`hub_refine` (drains the due-set, LLM-verifies, attaches evidence). Both ship
dark, gated by `service_config` alone. This is the *deliberate flip* — never
enable blind (the plan's gate); the pipeline has never run at corpus scale.

## Non-negotiable: single-instance

`hub_refine`'s claim query commits and releases its `FOR UPDATE ... SKIP
LOCKED` locks **before** the per-hub write connection opens
(`workers/hub_refine.py::run_hub_refine_pass`, the two-phase shape it shares
with `inbound_chase.py`). Two concurrent instances can both select the same
never-refined / sha-reopened / backstop hub → duplicate LLM verify. The
memo lost-update this paragraph used to warn about is closed in code since
gr693791 residual 5 — `taproot_rejected` / `reground_seen` /
`citation_misses` / `reground_log` writes all merge under a `FOR UPDATE`
hub-row lock (`hub_refine._memo_safe_update`, `taproot.hub.append_reground_log`,
`taproot.hub.remove_evidence`'s strand guard), and `remove_evidence`'s
strand guard serializes on the same lock — so a second instance now costs
duplicate spend, not a corrupted memo. Single-instance stays the standing
rule regardless: the duplicate verify spend is real, and multi-host
enablement of reground additionally waits on the prune rubric gate
(`PRECIS_TAPROOT_REGROUND_PRUNE`, `hub_refine.PRUNE_INTERLOCK_TOKEN`).

Both passes carry **empty `default_profiles`** (registry.py), so `service_config`
is the only thing that turns them on. **A `service_config` row scoped to `'*'`
is the hazard** — it enables the pass on *every* worker host at once, which is
exactly the multi-instance bug above. Always name a single host:

    precis service prio <host> chase_trigger 1     # e.g. host = the agent host
    precis service prio <host> hub_refine    1

`prio 1..10` = on at that claim weight; `prio 0` / `precis service clear` =
off. Pick ONE host (the agent node that already carries the embedder + LLM
router). Verify with `precis service list` — check the host column, not just
that a row exists.

**The env flags are not the switch, in either direction.** Since the §L
control cutover `_should_register` (`cli/worker.py`) never reads a
`ServiceSpec`'s `enable_env` value — its mere *presence* makes the pass
register — so setting `PRECIS_TAPROOT_REFINE_ENABLED` /
`PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED` in a plist does nothing at all, and
neither pass is in the deploy-time seed loop that mirrors older flags into
rows. Do not remove `enable_env` from either spec either: that would
*unregister* the pass entirely.

**On and off are both live**, no daemon kick: registration is unconditional
and the per-cycle `pass_gate` re-reads `service_config` through a 5-second TTL
cache (`workers/service_config.py`), so a prio flip takes effect within one
cycle in either direction.

## Sequence

1. **Tune the spend floor first** (dry-run, writes nothing) — see below. Land
   the chosen `PRECIS_TAPROOT_REFINE_MIN_SIM` / `TOPK` before enabling.
2. **Enable `chase_trigger`** on the chosen host. It refreshes
   `claim_embeddings`, sweeps paper/patent chunks (`CHASETRIG` marker), and
   marks near claims `TAPROOT_DUE`. Cheap (no LLM). Let it drain the backlog
   sweep once — watch `chunks_swept` fall to ~batch-churn.
3. **Enable `hub_refine`** on the *same* host. It claims the due-set,
   discovers + LLM-verifies + attaches. **Expect a one-time re-verify wave:**
   every previously-refined hub predates `last_refined_sha`, so all reopen
   once (predicate condition 3). Bound it — see LIMITs.
4. **Watch** nursery + spend for a few cycles before walking prio up.

## Floor tuning (host-native, LLM spend)

The knob that gates hub_refine's LLM calls is its discovery floor, not the
trigger floor. Tune it dry over a real slice:

    python -m precis.taproot.slice_refine_eval <hub_ref_id> [...] \
        --min-sim <candidate> --topk <candidate> --json /tmp/eval.json

Must run **host-native** (container claude is unauth'd and fakes a pass —
`live-model-tests-need-host-claude`). It writes nothing but does spend LLM on
verify. Sweep a few `--min-sim` values over ~10–20 representative hubs; pick
the floor where attach precision stays high without the candidate set
exploding. `chase_trigger`'s `PRECIS_TAPROOT_TRIGGER_MIN_SIM` (0.45) is
deliberately loose (it only marks due-ness; hub_refine re-discovers with its
own floor) — leave it unless the slice shows it under-marking.

## Bounds & rollback

- **Per-pass LIMITs:** `PRECIS_TAPROOT_REFINE_*` — `HUBS_PER_PASS` (worst-case
  LLM = `HUBS_PER_PASS × TOPK`), `chase_trigger`'s `*_BATCH_SIZE` (200) and
  `*_CLAIM_REFRESH_LIMIT` (64). Keep HUBS_PER_PASS low through the re-verify
  wave, raise once memos fill.
- **Backstop:** `PRECIS_TAPROOT_REFINE_BACKSTOP_H` (2160 = 90d) — the safety
  net that re-refines even a hub whose due-tag was lost. Leave it.
- **Rollback:** `precis service prio <host> hub_refine 0` (live, no restart).
  Then `chase_trigger 0`. The claim_embeddings table + tags persist and are
  harmless while dark.
- **Monitor:** nursery `critical` alerts (worker-restart / dead-worker /
  dispatch-stall) + LLM spend. A stall on the agent host is the signal to back
  off (see `worker-agent-silent-outage`).

## Follow-ups (not blockers)

- Harden the memo write to be conflict-safe (upsert / advisory lock) as
  defence-in-depth, so accidental multi-instance is harmless — then the
  single-host discipline above becomes belt-and-suspenders (gripe: coverage
  ledger / Option-B, 182230).
- Categorize `finding`/claim kinds so the multi-signal candidate union
  (topic co-membership) lights up (gripe 182078).

## Inbound citer chase (td489406)

Read `get(kind='job', id='/settings')` for the effective
`inbound_chase.enabled` flag and its source. A DB override wins over the
bootstrap `PRECIS_INBOUND_CHASE_ENABLED` environment variable, which falls
back to false. Paper reads and worker passes pick up changes within the
settings cache TTL (60 seconds).

With the flag on, system and all worker profiles require a positive
effective service priority to run the pass. An exact-host service priority
overrides the wildcard row. Missing placement or zero priority keeps the
pass idle.
Manual `--only inbound_chase` runs still require the flag.

Set the flag to false to stop new triggers and batches fleet-wide. An
in-flight batch can finish, and pending papers resume after re-enablement.
Operators can use `precis settings set inbound_chase.enabled false`. For
a confirmed MCP flip or revert and its audit history, see
`precis-job-observe-help`.

Candidate caps are `PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER` (25)
and `PRECIS_INBOUND_CHASE_DAILY_LIMIT` (200/day).

## Run grounding over older papers

Enable the claims-first backfill on a worker that can reach the local
verifier. The runtime switch is a `service_config` priority, exactly like
the passes above:

```sh
precis service prio <host> inbound_ground_backfill 1
precis logs --pass inbound_ground_backfill --payload
```

`PRECIS_INBOUND_GROUND_BACKFILL_ENABLED` (the registry entry's
`enable_env`) is the seed-time name only — since the §L cutover
`_should_register` never reads an `enable_env` *value*, the flag's mere
presence registers the pass; the prio row turns it on. Pick ONE host: the
pass holds row locks per pair and its retraction drains assume a single
drainer.

The digest includes claims visited, pairs checked, new supports and
denials, and reserved tokens. The backfill reuses
`PRECIS_INBOUND_GROUND_MAX_AGE_DAYS` (30 by default) for the age cutoff
and `PRECIS_INBOUND_GROUND_TOPK` (5) for papers per claim visit.
`PRECIS_INBOUND_GROUND_BACKFILL_PAIRS_PER_PASS` defaults to 20.
`PRECIS_INBOUND_GROUND_DAILY_TOKENS` (250,000) is a **new ceiling on the
existing recent-paper pass as well as the backfill**: both passes reserve
against the one shared `inbound_ground_budget` ledger (UTC day) before
dispatch, so the two passes cannot each spend the old implicit amount —
one pool caps the pair.
