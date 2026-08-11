# slullama HPC model — placement-chain rung + cluster access

- **Status**: leg 1 (static card) SHIPPED `04004d7a` (dark); leg 2 (chain
  rung) OPEN — blocked on external cluster access.
- **Refs**: memory `slullama_cluster_llm`; ADR 0066 (git-only ADR,
  capability tiers + placement chains); `precis.utils.llm` package
  docstring §4 ("Chain"); `precis.llm_catalog.seed_slullama_card`.

## Why

precis reaches an external Slurm HPC GPU (Meluxina, via the ICHEC interim
service — https://ichec.github.io/interim-service/interim-service.html) as
a client, over a thin SSH tunnel maintained by `slullama` (separate repo,
`~/work/projects/code/slullama`): a head-node daemon that `sbatch`-wakes a
GPU node, serves Ollama's OpenAI-compat `/v1`, reverse-proxies it to the
tunnel, and idle-tears-down (`idle_timeout≈30min`, `keep_alive=extend` on
the cluster side). Since ADR 0066 retired location-coupled tiers, this is
**not** a new tier — it's (1) a static `llm` card advertising the tunnel,
and (2) a placement-chain rung pointing an existing capability tier's local
rung at it, mirroring the DGX-pair integration
(`precis.utils.llm.local_serving` module docstring).

## Built (leg 1 — static card)

- `src/precis/llm_catalog.py::seed_slullama_card` mints the card —
  `served_by=[{host, endpoint, model, max_parallel, source:"static"}]`,
  every field falls back to a `PRECIS_SLULLAMA_*` env var
  (`docs/reference/config-variables.md` §4). CLI: `precis llm seed
  --slullama`.
- `source="static"` (`llm_catalog.SERVED_BY_SOURCES`) shields the entry
  from `workers/llm_serving.py::advertise_local_llm`'s per-heartbeat
  auto-discovery prune — the tunnel is never polled, so idle-teardown isn't
  defeated.
- `utils/llm/local_serving.py::acquire` reserves the slot (`max_parallel`
  is the nice-citizen concurrency cap) and repoints dispatch at the tunnel
  endpoint + server-side model tag.
- `deploy/roles/ssh_tunnels/` generalized for an external endpoint
  (`ssh_host`/`ssh_user`/`ssh_key`/`ssh_port` override; a human provisions
  `authorized_keys` out-of-band, Ansible only pre-seeds `known_hosts`).
- Tests: `tests/test_local_serving.py::test_seeded_slullama_card_reserves_and_caps`,
  `tests/test_llm_serving.py` (prune guard + rebuild guard both leave the
  static entry alone).
- **Footgun to respect:** `seed_slullama_card` replaces the *entire*
  `served_by` list for its `model_id` on each refresh (key-level merge). Safe
  only while `qwen-hpc`'s `model_id` never collides with a real
  llama-swap-discovered tag — keep it distinct. (A collision would survive one
  heartbeat via the source-guard but a re-`seed` would drop the auto entry.)

## Open (leg 2 — placement-chain rung)

Add a LOCAL rung to an existing tier's chain via the operator
`app_settings` override (`live_config.chain_override`, read by
`router.resolve_chain`):

```
llm.chain.big = [{"placement":"local","transport":"openai_tools","model":"qwen-hpc"},
                  {"placement":"cloud","transport":"openai_tools","model":"<fallback>"}]
```

1. **Tier: BIG vs FRONTIER — undecided.** BIG has precedent (the DGX-pair
   rung already lives there); FRONTIER would suit an opus-class OSS model
   too big for the Spark pair. Reto's call.
2. **Unserved-host behavior — verified against current code, corrects a
   prior wrong assumption.** `router._skip_unserved_local_rung` only prunes
   a `Transport.LOCAL` rung (the dead `:4000` litellm-proxy case) — it does
   **not** fire for `Transport.OPENAI_TOOLS` rungs. On a host that isn't
   `melchior`, `local_serving.acquire("qwen-hpc")` finds no slot (the
   `served_by` endpoint is loopback-only, not LAN-routable, so it's
   host-private — see `local_serving.py`'s cluster-scoped-serving
   section) → `FailoverProvider` still *attempts* rung 0 against whatever
   the hosted-OSS default endpoint is, fails (unrecognized model), logs a
   WARN, and falls to the cloud rung. End state matches "falls to cloud, no
   `sbatch` traffic" (the tunnel itself is never touched), but it's a
   failed-attempt-then-fallback on every non-`melchior` BIG dispatch, not a
   clean skip — worth knowing before wiring this live on a multi-host
   fleet; may want a loopback-aware extension of
   `_skip_unserved_local_rung` to cover `OPENAI_TOOLS` too.
3. **Cluster access.** Meluxina login node on a non-standard SSH port
   (`login.lxp.lu:8822`); the Meluxina username is distinct from the ICHEC
   account; SSH pubkey registered through the provider's helpdesk
   (out-of-band — matches the `ssh_tunnels` external-endpoint provisioning
   path, which already expects a human-provisioned `authorized_keys`). GPU
   partition (`gpu`) + a `--qos` to pick. Modules only load on compute
   nodes, which is fine — slullama's head-node proxy is pure Python +
   Slurm CLIs, Ollama runs inside the job. Open risk, unconfirmed: EuroHPC
   login nodes may reap long-running daemons — needs empirical
   confirmation once access lands.

## Acceptance

- `llm.chain.big` (or `.frontier`) rung resolves to a live tunnel response
  from a real Meluxina allocation.
- A non-serving host's dispatch falls to the cloud rung without ever
  reaching the tunnel or triggering `sbatch`.
- `docs/reference/config-variables.md` §4 stays accurate once real values
  are set.
