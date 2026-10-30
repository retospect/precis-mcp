# topo S4 — crossing resolution (gr520902) — working spec

Fleet brief `/tmp/gripe-fix/briefs/topo-s4.md`. Size L, three commits
(a)/(b)/(c), red test first each, then the offline bench:
`scripts/pcb-topo-bench /tmp/pcb-topo/bench/s4`. Acceptance: esp32c3
unrouted <= 1, connectivity 0, routed >= 10/11. Branch
`worktree-topo-s4` off origin/main in tree `topo-2b`.

## Symptom (gripe)

esp32c3: 8 unresolved crossings strip 12 branches; 5/11 nets fail
`same-layer-crossing`, connectivity errors on stripped nets.

## Root cause (verified in code)

- `realise.py::assign_layers` flips a branch's whole layer between
  `{0, n-1}` and stops at the first non-improving flip.
- `plan_route` derives joins from forced ends only: a both-ends-F branch
  flipped to B emits `[F stub, via, B piece]` — the B piece ends at a
  top-SMD pad → connectivity error. Pieces bracketed by two vias do not
  exist in the model.
- `wires.py::_net_layers` returns outer-only candidates `{0, n-1}`.
- `sketch_rows` fails the whole net when one branch is in an unresolved
  pair; `tree`/`layer_assign` still list the stripped branch's pin pair
  (claims a connection with no committed copper).

## Design — segment assignment model

Per branch: `cuts[b]` = sorted edge indices; a cut at `c` mints a via on
`edges[c]`; run `p` covers edge indices `(cuts[p-1] .. cuts[p]]`
(sentinels −1 / E−1). A cut at `E−1` yields an empty tail run — the
pad-side stub past the end via. `runs[b]` = layer per run.
`plan.layers[b]` = `runs[b][0]` (first-piece layer, unchanged meaning).

Initial segments (replaces `_initial_layers` + plan_route's join
derivation — same behavior): F↔B both-forced with `len(edges)>=2` gets
`runs=[la,lb], cuts=[E//2]`; F↔B with E==1 gets `cuts=[0]` tail-stub
form (`runs=[la,lb]`); else single run `[L]`, `L = la or lb or
min(cand ∩ outers)`.

Helpers: `_run_bounds(cuts, E)` → `[(lo,hi)]` per run;
`_span_layers(cuts, runs, E, lo, hi)`; `_set_span(d, b, cuts, runs, lo,
hi, layer)` — insert cuts at `lo-1` and `hi`, set the span run's layer,
then `_fix_terminals` (insert cut 0 / E−1 stub runs when a terminal run
disagrees with the forced pad side; relabel when the boundary already
exists) and `_merge_same` (drop cuts between equal-layer runs — a via
joining a layer to itself is dead).

`_pair_live(wires, runs, cuts, a, b)`: for every shared-edge pair with
flipped order sign (`order[e]` position of a vs b), the crossing region
is edge indices `(min..max]` on each branch's own index space; the pair
is live iff `_span_layers` over the two regions intersect. Conservative:
over-reports partial splits, never misses a real crossing.

## Stage (a)

- `_net_layers`: candidates = class `net_layer_force` over the whole
  stackup — force ≥0 → `{force}`, force −2 → inners `{1..n−2}`, else all
  layers. Drop `net_pin_force` from the AND (it forces terminal pieces
  only — that is Decision 6, enforced by `_pin_forced`). Empty set stays
  the uncolourable case (`_cand_mask` already treats empty as
  outer-free). NOTE: `test_build_wires_example_a_branches` asserts
  `net_layers == ({0,1},)*3` on a 2-layer board — unchanged there.
- `assign_layers` becomes the segment-aware greedy (yielder per
  `_yield_rank`, span piece to each candidate layer incl. inners), old
  stop rule for this commit.
- `plan_route` consumes runs/cuts directly: each cut → `_site_via`,
  each run → a `RoutePiece`.
- Red test (`test_pcb_topoplace_realise.py`): both-ends-F branches
  crossing on a 4-layer board resolve with a mid-piece + 2 vias and
  terminal pieces still on F; and/or a `net_layer_force=-2` net's piece
  on `{1,2}`.

Move legality (nice-to-have, decide at write time): restrict candidate
layers to those open on every span edge — needs a per-layer open mask
out of `wires._blocked_masks` (refactor its tail into
`_open_masks(...) -> (E,) bitmask`, keep `!= 0` as `base_pass`); also
fixes today's latent free-net-on-F-over-F-blocked-edge exposure.

## Stage (b)

Replace the stop rule: per unresolved pair (sorted), candidates =
whole-branch flip a, flip b (both are `_set_span(0, E−1, other_outer)`),
mid-piece flip of the shared span `[min..max shared idx]` to each
candidate layer, and re-thread: `_net_tree(ctx, mesh, d, k, n_layers,
trace, clr, occ, extra_blocked=shared_edges)` — needs new
`extra_blocked` param on `_net_tree` (`base_pass &= ~mask`) and a
factored `_build_branch` (taut→shortcut→retrack→t pipeline) out of
`build_wires`; rebuild `order` via `wires._order`. ctx built lazily:
`_make_ctx(mesh, d, n_layers)` once in `assign_layers` when any pair
stays unresolved. Re-threaded branch gets fresh initial segments.
Keep the cheapest candidate by `(n_live_pairs, n_vias)`; bound by
`_MAX_FLIPS`; keep best-ever snapshot.

`assign_layers` signature → `assign_layers(d, mesh, wires, n_layers,
*, net_rules=None)` returning `(wires', runs, cuts, crossings)` (wires
may carry re-threaded branches). Only caller is `plan_route`.

Red test: `example_a()` fixture (`tests/fixtures/pcb/topo_example_a.py`
— K3, already exists) through build_mesh+build_wires+plan_route →
`plan.crossings == ()`. On 2L SMD expect ~4 vias (§B5); all-TH variant
resolves with fewer/none (free yield rank). Verify on real run.

## Stage (c)

- Bounded rip-up in `route_design` around `plan_route`: ≤4 rounds; nets
  = those in remaining crossing pairs; re-run their `_net_tree` with
  crossing-shared edges penalized in `occ` (e.g. `1 + 50*mask`, plus
  kept-nets' `used` occupancy via `wires.gap_cap`/`capacity_mm` →
  `inv_cap` same formula as `build_wires`); replace the nets' branches,
  rebuild `order`, re-plan; keep best plan by `(crossings, vias)`; stop
  early on no improvement.
- `sketch_rows`: fail only the unresolved branch — `tree`/`layer_assign`
  list only branches NOT in `plan.crossings`; the failed connection
  appears in `fail.problems` with `from`/`to` pin keys
  (`check_unrouted` already accepts from/to). Net `status` stays
  `"failed"` (a net with an unrouted connection is not realized — the
  route_state/unrouted-finding path must keep seeing it).
- Red test: 3-pin net, one branch in an unresolved pair → tree keeps the
  good connection, problem names the failed pin pair.

## Standing rules (keep)

power/GND at signal width via `route_net_rules`; no pours; every copper
layer routes; `CorridorOverflow` refusal stays; `route_design` still
strips unresolved-pair branches from emitted copper.

## Gates (brief §5)

touched test files one-per-call; `test_posix_only_guards`;
`scripts/test --selected` once after per-file runs; the 4-ratchet pack
in one call; `--typecheck`, lint-imports, ruff check + format --check.
Grep tests/ for changed-signature functions (fakes included) and run
each hit.
