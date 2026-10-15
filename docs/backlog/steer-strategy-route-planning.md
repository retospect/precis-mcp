---
status: in-progress
pillar: 3d-design
title: STEER — natural-language strategy constraints for route planning
---

# STEER — natural-language strategy constraints for route planning

Reto, 2026-10-15 ~16:40Z, from the crispy-riding-thimble research
shortlist: "name-to-SMILES resolver, ReactionClassifier or steer << yes we
want these."

Source: finding fi259498 and paper `bran25` §4.2 (Bran et al. 2025,
"Chemical reasoning in LLMs unlocks strategy-aware synthesis planning").
A chemist states a strategy in plain language, such as "avoid protecting
groups" or "build the ring late". AiZynthFinder proposes several routes,
and each route is rendered as text: every intermediate's SMILES and the
transformation at each step. An LLM scores how well each route fits the
strategy and writes a rationale. The scores rerank and filter the engine's
routes. The paper's stronger form uses the same scorer to guide the search
itself.

## Where precis stands (2026-10-15)

- `route` kind (`precis_chem`, dark behind `PRECIS_CHEM_ENABLED`): put plans
  through the `retrosynth` job; on castor the job runs the
  `precis-aizynth` container. The container takes only the target SMILES:
  `aizynthcli --config /models/config.yml --smiles <SMILES>` writes
  `trees.json`, and LinChemIn's `az_to_route.py` writes `route.json` for
  the **top route only**.
- `trees.json` already holds AiZynth's ranked route set. The default
  post-processing returns 5 to 25 routes. Before slice 1, precis read only
  `routes[0]`.
- Every route step now carries an RXNO reaction class
  (`precis_chem.classify`, commit 252e40f49). The route text the LLM reads
  names each transformation, which is what the paper's prompt needs.
- `constraints=['ewod-oil']` is a separate, lexical platform screen. STEER
  constraints are strategic and are judged by an LLM. The two stay
  separate.

## Slices

### Slice 1: rerank the kept routes (SHIPPED with this doc)

- The IR keeps alternatives. `RouteGraph.alternatives` holds up to 9
  lower-ranked routes. `parse_aizynth_trees` fills it from `trees.json`,
  and the retrosynth job attaches it when `route.json` wins.
- `get(kind='route', id=…, view='steer', q='<strategy>')` makes one routed
  call per candidate route (at most 10), at Tier.MEDIUM with a $0.05 cap
  per route. It shows the routes best first, each with its 0–10 score,
  the rationale and its steps.
- A reply that fails or does not parse leaves that route unscored and
  ranked last, with the reason shown. It never gets a default score.
- Results are cached on `meta.steer`, keyed by prompt version, strategy and
  the routes scored. `refresh=True` asks the LLM again.
- Code: `precis_chem/steer.py`. Tests: `tests/test_route_steer.py`, which
  uses a fake scorer and calls no model.

Acceptance:

1. [x] A route planned by AiZynth keeps up to 9 alternatives, and a blob
   stored without alternatives still parses.
2. [x] `view='steer'` refuses an empty strategy or one longer than 500
   characters, and the error suggests a fix.
3. [x] Each candidate route gets its own LLM call through the model router.
   No API key is used directly.
4. [x] Asking again with the same strategy is served from the cache and
   makes no new call.
5. [ ] Dogfood on prod once AiZynth is live on castor and the release
   containing this commit is deployed. Plan one real target, rerank it
   against two opposite strategies, and check that the order changes and
   that the rationales name real steps. Record the cost per rerank.
   **Blocked on deploy.**

### Slice 2: strategy recorded at put, surfaced on the route page (SHIPPED 2026-10-15)

`put(kind='route', …, strategy='…')` records the strategy
(`meta.steer_strategies`, ordered + deduped). When the route lands, the
rerank runs in the retrosynth job (`_steer_pending`), so no LLM call
happens in a request. The route page then leads with the best route for
the strategy (`steer.render_page`) and shows the engine's own top route
beside it. The strategy is **not** part of `cache_key`, because the search
is unchanged and only the ranking layer depends on it.

Acceptance:

1. [x] A put with a strategy returns at once — the put path never calls
   the LLM.
2. [x] The job's summary names the top-ranked route and its score.
3. [x] Two puts that differ only in strategy share one AiZynth run (cache
   hit → steer-only job: the dispatch sees the landed plan and skips the
   engine; a still-in-flight plan absorbs the new strategy via
   `meta.steer_strategies`) and get two rankings.
4. [ ] Dogfood on prod once the release is deployed (with slice 1's item
   5): put two strategies on one target, check both rankings and that the
   page leads with the pick. **Blocked on deploy.**

### Slice 3: more candidates per run (image change; code only)

The ranking can only be as good as the candidate set. Raise AiZynth's
`post_processing.max_routes` and set `all_routes` in a per-job `config.yml`
written to `/work/in`. That needs a shim argument in `precis-aizynth-run`.
`az_to_route.py` should also emit descriptors for every route, not just the
top one. The change lands in the repo and deploys with the next castor
image build. Per Reto's castor ruling, no build happens here.

Acceptance: a job can request N routes, and `route.json` carries N
normalised routes, each with descriptors.

### Slice 4: strategy-guided search (paper's full method)

Use the LLM score as a search signal inside AiZynth's MCTS, either as a
custom `aizynthfinder` scorer plugin or as a rerank of expansion nodes.
This needs compute in the container and many LLM calls per target, so cost
and latency are the questions. **Needs a decision: Reto.** Is it worth
running on castor, which carries the LLM plus light lanes only, or does it
need the twins?

## Open questions

- **Synchronous view vs job.** Slice 1 makes up to 10 MEDIUM calls inside
  `get` (about 10 to 60 s, under $0.50 cold, free when cached). Slice 2
  moves the default path into the job, and the view stays for ad-hoc
  strategies. Revisit if the latency bites.
- **Tier.** The paper used frontier models and found that small models
  rank poorly. MEDIUM is the starting point. Compare MEDIUM and BIG on a
  dogfood target before slice 2 fixes the default.
- **Batch vs per-route prompts.** Slice 1 sends one prompt per route,
  following the paper, to avoid positional bias. A single comparative
  prompt would be cheaper; measure before switching.
