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
never-refined / sha-reopened / backstop hub → duplicate LLM verify + a
lost-update on the `meta['taproot_rejected']` read-modify-write. It self-heals
next cadence, but it wastes spend and muddies the memo.

Both passes carry **empty `default_profiles`** (registry.py), so `service_config`
is the only thing that turns them on. **A `service_config` row scoped to `'*'`
is the hazard** — it enables the pass on *every* worker host at once, which is
exactly the multi-instance bug above. Always name a single host:

    precis service prio <host> chase_trigger 1     # e.g. host = melchior
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
