"""Taproot — the evidence-grounded claim graph.

A claim lives on one **claim hub**: a ``finding`` tagged ``TAPROOT:claim`` +
``STATUS:canonical`` (off the ``chase`` STATUS lifecycle, so a re-picked
finding is never mistaken for a hub). Papers attach as evidence edges. An
overlay on ``finding``/``ref_tags``/``links`` — own schema is
``claim_embeddings`` (migration 0101) plus link relations ``establishes``
(0094, no inverse — hubs read evidence via ``links_for(direction='in')``),
``refines`` (0100), ``conjunct-of`` (0126, atom -> compound, asymmetric, no
inverse), ``motivated-by`` (0135, hypothesis -> the provoking artifact, same
no-evidence-flow contract); ``corroborates``/``contradicts`` reuse existing
slugs, endpoint kinds disambiguate. A hub is **atomic** (evidence-bearing),
**compound** (an un-decomposable bundling sentence, no direct evidence —
:mod:`.hub`), or **hypothesis** (evidence-free by type — carries motivation +
a discriminating experiment instead, ``refs.meta.artifact_type``, minted via
``handlers/_finding_hypothesis.py``; the widening pass excludes hypotheses,
:func:`.canon.not_hypothesis_predicate_sql`, so it never becomes a
confirmation engine for its own guess). Design: ``docs/backlog/taproot.md``;
governance: taproot evidence relations (+ the living citation pins).

Three layers, one domain (glossary "finding / taproot / nanopub"):
``finding``, the ref kind this module overlays, is owned by
:mod:`precis.handlers.finding`; ``nanopub``, the downstream pipeline that
freezes an approved hub into a signed nanopublication, is owned by
:mod:`precis.nanopub`. This module owns the claim-graph layer only.

**Lifecycle — ten stages**, extract -> admit -> place -> ground -> widen ->
weigh -> oppose -> adjudicate -> gate -> publish:

| # | Stage | Status | Where |
|---|---|---|---|
| 1 | Extract — is this sentence a claim? | live | :mod:`.canon` |
| 2 | Admit — falsifiable/self-contained/method-attributed/single | live; advisory at mint, blocking at approve | ``nanopub/gates.py::_BLOCKING_LINT_CODES`` |
| 3 | Place — same claim already held? | live | ``place`` |
| 4 | Ground — which passage supports it | live, per-passage | |
| 5 | Widen — who else in the corpus speaks to this? | built, dark | ``workers/hub_refine.py`` |
| 6 | Weigh — how much independent support? | display-only, gates nothing | ``handlers/_finding_evidence.py`` union-find count |
| 7 | Oppose — what conflicts? | live on both judge paths: writes a ``contradicts`` edge rather than dropping the old one | ``workers/_chase_llm.py::_verify_support_with_caveats`` -> ``hub_refine._attach_contradicts`` |
| 8 | Adjudicate — is the conflict real, who wins? | **absent** | |
| 9 | Gate — publishable? | live, admissibility only | |
| 10 | Publish | :mod:`precis.nanopub`'s layer entirely | |

Two structural cautions. **The ratchet**: every stage promotes; nothing
demoted until a new ``contradicts`` edge triggers :mod:`precis.nanopub.demote`
(state rules owned there) or ``chase_trigger``'s ``TAPROOT_DUE`` re-open
marking (dark) — both re-open, neither adjudicates (stage 8 is still
absent, so a re-opened claim stays unblessed). **Scale changes the risk of
a wrong judge**: at ~1.5k hubs a
bad-verdict rate that's a nuisance by hand is a corpus-wide event, and a
demoter wired to a bad judge can un-approve as fast as a good one approves —
every automated writer here needs a confidence floor, an idempotency story,
and a dry-run mode before its first large run (``place``'s confidence gate
on ``contradicts`` is one instance, not a one-off).

**Support is a verdict, never a default.** ``links.meta.support`` is written
only together with ``support_reason`` + ``verified_by`` (+ ``verified_at``,
``verified_claim_sha`` — the hub's ``claim_sha`` at verify time); an attach
path with no verdict omits the key, so a new edge is **born withheld**
(``nanopub.preflight.withheld_edges``) until a verifier certifies it
(``hub_refine``'s re-verify arm, or ``precis taproot verify-edges``).
``verified_claim_sha`` makes invalidation forward-only: editing a claim's
wording withholds its old verdicts on sha mismatch, but a pre-sha legacy
stamp stays valid until the operational re-verify pass rewrites it.

Module map (detail lives in each module's own docstring):

- :mod:`.canon` — the canonicalizer cascade: ``extract_claim`` -> ``block``
  -> ``dedup_judge`` -> ``place``; verdict ``Placement.action`` in
  ``attach``/``new``/``new_contradicts``/``needs_review``. Invariant:
  over-merge (false ``same``) is the one dangerous direction, gated to zero
  by :mod:`.eval_canon`; under-merge is tolerated.
- :mod:`.hub` — the single write door: ``mint_hub``/``attach_evidence``/
  ``apply_placement``/``link_claims``/``apply_extraction``/``merge_hubs``.
- :mod:`.seniority` — pure read/derive, never stored: supporters split into
  ``establishes`` originators vs corroborators by walking ``cites`` among
  the supporter set at read time; also derives ``refines``/``conjunct-of``.
- :mod:`.cite` — the cite-key policy for ``precis resolve`` and draft
  export: derived originators, falling back to corroborators then
  in-flight, recomputed every run.
- :mod:`.trust` — read-time trust ladder for a finding-backed citation:
  ``clean`` < ``abstract`` < ``vouched`` < ``unverified`` < ``unsupported``.
- :mod:`.resolve` — inline ``[N]`` marker -> ``doi``/``s2_id``/``held_ref_id``
  via ``chunk_citations`` (the ``bib_mark`` sweep's output).
- :mod:`.authoring` / :mod:`.backfill` / :mod:`.lookup` — cite-seeded hub
  mint, legacy ``[pc]``/``[pa]`` draft-cite conversion, and read-only
  "what hubs does this paper ground".
- :mod:`.migrate` — the compound->atomic migration runner (``precis taproot
  migrate``): dry-run extraction with gated verdicts, JSONL persistence for
  A/B runs. Phase-2 apply mode not built; dry-run writes nothing.
- :mod:`.reground` / :mod:`.repair_evidence` — "no source, no atom": rank a
  source's body chunks against a claim, verify by LLM, then post-validate
  the quote in code (verbatim, unique in the paper).
- :mod:`.directed` — demand-driven claim minting (``precis taproot
  direct-mint``, dry-run default): one-way fit claim->evidence, then the
  same block->judge->place cascade.
- :mod:`.grounding` — the "is this chunk evidence-grounding-eligible"
  predicate, shared (not duplicated) by backfill, reground, chase, repair.

Producers (dark by default; no-op with no embedder). **Enablement, two
mechanisms — one looks like the other and isn't.** A pass that is its own
**service**
(``hub_refine``, ``chase_trigger``, the axis classifier) flips live via a
``service_config`` prio row (``precis service prio <host> <service> 1``, no
redeploy) — since the §L cutover a ``ServiceSpec``'s ``enable_env`` is
**never read**, so ``PRECIS_TAPROOT_REFINE_ENABLED`` in a plist does
nothing; the per-cycle ``pass_gate`` is the one decision point. A
**sub-feature of another pass** has no ``ServiceSpec``, so it keeps a
genuine in-pass env flag: the forward chase bridge
(``PRECIS_TAPROOT_CHASE_ENABLED``) and the inbound chase/citer sidecar
(``PRECIS_INBOUND_CHASE_ENABLED``) are live env vars — do not "modernize"
them into ``service prio``. Enable a service producer on **one host** —
``hub_refine``'s rejection memo is a read-modify-write on ``meta``
(``docs/runbooks/taproot-chase-enablement.md``).

- **Forward chase bridge** (``workers/chase.py::_taproot_bridge``) — on a
  finding's established-terminal hop, builds the claim from the finding's
  own title and runs block->judge->place->``apply_placement`` in the same
  transaction as the ``STATUS:established`` flip (savepoint-isolated).
- **hub_refine** (``workers/hub_refine.py``, stage 5 *Widen*) — revisits
  existing hubs off a due-set (``TAPROOT_DUE`` tag / sha-reopen / 90d
  backstop); excludes compound hubs (evidence attaches to atoms only);
  discovers via corpus semantic ANN + citation-following; re-verifies each
  hub's own unverified edges per pass. Grown into **reground**
  (``docs/backlog/taproot-reground.md``) — a strict per-edge KEEP/PRUNE/
  CONTRADICTS audit, deeper same-paper re-discovery, and removal through
  :func:`.hub.remove_evidence` — behind ``PRECIS_TAPROOT_REGROUND*``; the
  prune sub-stage additionally gates on :mod:`.slice_refine_eval` passing.
- **chase_trigger** (``workers/chase_trigger.py``) — the incremental
  due-set watermark: reverse ANN from newly-embedded paper/patent chunks
  marks near hubs ``TAPROOT_DUE``.
- **TAPROOT axis classifier** (``data/axes/taproot.yaml`` via
  ``workers/axis_pass.py``) — tags ``finding`` rows ``TAPROOT:claim`` vs
  ``TAPROOT:review``; fail-open (ambiguous stays re-claimable).

Authoring doors (all through :mod:`.hub`): ``put(kind='finding',
supporters=[…])`` / ``precis taproot mint`` (:mod:`.authoring`);
``link(kind='finding', id='fi<hub>', rel=…)`` onto an existing hub;
``rel='refines'`` mints claim->claim links (advisory, no evidence flow);
``precis taproot backfill`` / the ``taproot_backfill`` job (:mod:`.backfill`,
runs on the cluster worker, never in the MCP handler).

Read surfaces: ``get(kind='finding', view='evidence')`` (originators/
corroborators/contradicts tables); the default ``finding`` search cohort
unions hubs alongside ``STATUS:established``; the ``/claim/<head>`` page;
the fisheye Claims ring.

Not built: citation-card dedup, the S2 global-citation-count originator
fallback, the integrity axis, a corpus-wide backfill sweep, ``refines``
evidence-flow. Skills:
``precis-taproot-help`` (orientation, citing), ``precis-taproot-mint-help``
(authoring/minting/merging), ``precis-taproot-backfill-help`` (batch
``[pc]``/``[pa]`` conversion).
"""

from __future__ import annotations
