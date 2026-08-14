"""Taproot — the evidence-grounded claim graph.

A claim lives on exactly one **claim hub**: a ``finding`` tagged
``TAPROOT:claim`` + ``STATUS:canonical`` (off the chase STATUS lifecycle, so
``chase``'s claim query never re-picks a hub up). Papers attach as evidence
edges. The graph is an overlay on ``finding``/``ref_tags``/``links`` — its
only own schema is ``claim_embeddings`` (migration 0101) plus link relations
``establishes`` (0094, seeded without an inverse: hubs read evidence via
``links_for(direction='in')``), ``refines`` (0100), and ``conjunct-of``
(0126, atom -> compound, asymmetric, no inverse, mirrors 0100); ``corroborates``
/ ``contradicts`` reuse existing slugs, endpoint kinds disambiguate. A claim
hub is either **atomic** (evidence-bearing) or **compound** (an
un-decomposable bundling sentence, no direct evidence — see :mod:`.hub`).
Design:
``docs/backlog/taproot.md``; governance: taproot evidence relations (+ the living citation pins).

Module map (each module's docstring carries its own detail):

- :mod:`.canon` — the canonicalizer cascade: ``extract_claim`` (SMALL; chunk
  -> a :class:`~.canon.ClaimExtraction` — zero or more AIDA-atomic claims, an
  optional surviving ``compound`` bundling sentence, and the rejected
  conjuncts (:class:`~.canon.NotClaim`); NO-CLAIM is an *empty* extraction
  (``is_empty``), never ``None``) -> ``block`` (no model; ANN
  over ``TAPROOT:claim`` hub embeddings) -> ``dedup_judge`` (MEDIUM;
  ``same``/``different``/``contradicts``, biased hard toward ``different``)
  -> ``place`` (deterministic; a low-confidence ``same`` escalates to
  ``merge_confirm``, BIG; a still-unconfirmed merge returns ``needs_review``,
  never auto-merges). Verdict: ``Placement.action`` in ``attach`` / ``new`` /
  ``new_contradicts`` / ``needs_review``. Invariant: **over-merge is the one
  dangerous direction** — the live eval gate (:mod:`.eval_canon`) requires
  zero false ``same``; under-merge is tolerated. Every model call routes
  through ``precis.utils.llm.router``.
- :mod:`.hub` — the single write door: ``mint_hub`` / ``attach_evidence`` /
  ``apply_placement`` / ``link_claims`` / ``apply_extraction``. Evidence
  sources are paper/patent refs only; grounding is per-passage
  (``meta.source_handle`` -> ``src_chunk_id``, so two passages of one paper
  are two edges); role is always written ``corroborates``; ``needs_review``
  files a ``kind='todo'``. ``apply_extraction`` is the decomposition-aware
  orchestrator over a full ``ClaimExtraction``: each atom mints/converges +
  attaches evidence through ``apply_placement``; the **compound** hub (when
  a chunk actually split) mints/converges with **no** direct evidence edge —
  ``attach_evidence`` raises on a compound target — linked to its atoms
  ``conjunct-of`` (migration 0126), with the rejected conjuncts recorded on
  ``meta['taproot_not_claims']`` keyed by claim sha.
- :mod:`.seniority` — pure read/derive, never stored: supporters split into
  ``establishes`` originators vs corroborators by walking ``cites`` edges
  among the supporter set only, at read time (no intra-set cites -> all stay
  corroborators + ``coverage_note``; an originator is never guessed).
  ``contradicts`` is a separate group, never folded in. ``derive_refines``
  reads the claim->claim ``refines`` links; ``derive_conjuncts`` /
  ``conjunct_atoms_bulk`` read the ``conjunct-of`` links — a compound hub
  derives no evidence originators of its own (it holds none).
- :mod:`.cite` — the ONE cite-key policy for ``precis resolve`` and draft
  export: derived ``establishes`` originators, falling back to corroborators,
  then in-flight; recomputed every run, so a later-discovered originator or
  hub merge improves the next export with no manual re-cite. Pins
  (``[pub_id>…]`` replace / ``[pub_id+…]`` supplement) share one
  ``apply_pin`` across the resolve-token and draft-mentions grammars.
- :mod:`.trust` — read-time trust ladder for a finding-backed citation:
  ``clean`` < ``abstract`` < ``vouched`` < ``unverified`` < ``unsupported``,
  worst-of reduction across a block's cite heads. A compound hub's own trust
  is the worst-of its atoms' trust (status ``hub-compound``), depth-1 only.
- :mod:`.resolve` — inline-marker resolution: the ``bib_mark`` sweep
  (``workers/bib_mark.py``) extracts a paper's inline ``[N]`` markers into
  ``chunk_citations`` (migration 0109; only numbers that are a real bib
  marker for that paper); ``resolve_citation(store, chunk_id, marker)`` joins
  ``paper_bib_entries`` -> ``doi``/``s2_id``/``held_ref_id``. Consumers: the
  web Sources tab and hub-refine citation-following (below).
- :mod:`.authoring` / :mod:`.backfill` / :mod:`.lookup` — cite-seeded hub
  mint (``seed_claim_hub``), legacy ``[pc]``/``[pa]`` draft-cite conversion
  through the same cascade, and read-only "what hubs does this paper ground".
- :mod:`.migrate` — the compound→atomic migration runner (``precis taproot
  migrate`` CLI): scores the *body* claim sentence (never ``refs.title`` —
  they differ on 572/1346 hubs), dry-run extraction with gated verdicts
  (``split``/``pass``/``no-claim``, plus ``lossy``/``nested`` held for
  review rather than applied), JSONL persistence for A/B runs against
  ``tests/fixtures/taproot/migration_pilot_25.jsonl``, seeded random
  controls. Phase-2 apply mode not built; dry-run writes nothing.
- :mod:`.directed` — demand-driven claim minting (``precis taproot
  direct-mint``, dry-run default): ``qualify_claim`` (BIG; one-way fit
  claim→evidence + verbatim-quote anti-hallucination) then the same
  block→judge→place cascade through :mod:`.hub`; ``meta.demanded_by``
  accumulates the requesting passages. Directed, never harvest.

Producers (all default-OFF env flags; each degrades to a logged no-op when no
embedder is available):

- **Forward chase bridge** (``workers/chase.py::_taproot_bridge``,
  ``PRECIS_TAPROOT_CHASE_ENABLED``) — on a finding's established-terminal hop
  builds the claim from the finding's own title (no ``extract_claim``; a
  chase finding is already a user-asserted claim) and runs block -> judge ->
  place -> ``apply_placement`` in the SAME transaction as the
  ``STATUS:established`` flip, savepoint-isolated so a taproot failure never
  rolls back the flip. Skips on NO-SUPPORT. Intermediate chain hops attach as
  extra corroborators. Idempotent: a re-established finding's ``block`` finds
  the existing hub and ``add_link`` no-ops the repeat edge.
- **hub_refine** (``workers/hub_refine.py``,
  ``PRECIS_TAPROOT_REFINE_ENABLED``) — revisits *existing* hubs (everything
  else attaches evidence only as a side effect of a chase or a mint); a
  **compound** hub is excluded from the due-set entirely (its only possible
  write is a direct evidence attach, which ``attach_evidence`` refuses) —
  refine/re-embed touches atoms only. Claims
  due hubs (``TAPROOT_DUE`` ref tag, edited-claim sha-reopen via
  :func:`.canon.claim_sha`, or a 90d stuck-row backstop), discovers
  candidates from two merged sources — corpus semantic ANN and
  **citation-following**: the hub's own grounding chunks ->
  ``chunk_citations`` -> ``resolve_citation`` -> the held cited paper's top
  passage (citation candidates win the per-paper dedup slot) — prechecks
  existing-edge + rejection memo before any LLM spend, verifies, then
  attaches ``corroborates`` or appends the memo. A citation-reached
  ``supports=no`` records ``via='citation'`` + ``meta.citation_misses``
  (rendered on the claim page); resolved-but-not-held cites land in
  ``meta.unresolved_citations``, never auto-fetched; a miss never flips hub
  trust. Converging by construction, not a periodic re-scan.
- **chase_trigger** (``workers/chase_trigger.py``,
  ``PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED``) — the incremental due-set
  watermark: sha-gated hub vectors in ``claim_embeddings``, reverse ANN from
  newly-embedded paper/patent chunks (loose similarity floor — over-triggering
  is cheap, hub_refine prechecks), marks near hubs ``TAPROOT_DUE``; drains
  via ``CHASETRIG:<version>`` chunk tags. Excludes compound hubs from both
  the embed and the probe query, same predicate as hub_refine's exclusion.
- **TAPROOT axis classifier** (``data/axes/taproot.yaml`` via
  ``workers/axis_pass.py``; default-OFF ``axis:taproot`` service) — tags
  ``finding`` rows ``TAPROOT:claim`` (grounded world-claim) vs
  ``TAPROOT:review`` (editorial note, excluded from the graph). Fail-open: an
  ambiguous read stays re-claimable, never mis-tags.

Authoring doors (all through :mod:`.hub`): ``put(kind='finding',
supporters=[…])`` / ``precis taproot mint`` (:mod:`.authoring`);
``link(kind='finding', id='fi<hub>', rel=…)`` onto an existing hub;
``rel='refines'`` / ``precis taproot refine`` mints claim->claim links
(advisory only — no evidence flow, each hub keeps its own paper->hub edges);
``precis taproot backfill`` / the ``taproot_backfill`` job (:mod:`.backfill`;
the LLM cascade runs on the cluster worker, never in the MCP handler).

Read surfaces: ``get(kind='finding', view='evidence')`` (originators /
corroborators / contradicts tables); the default ``finding`` search cohort
unions hubs in alongside ``STATUS:established``; the ``/claim/<head>`` page;
the fisheye Claims ring (pin markers + ``refines`` lines).

Not built: citation-card dedup (Phase-2 slice 2d), the S2
global-citation-count originator fallback, the integrity axis (Phase 4), a
corpus-wide backfill sweep, ``refines`` evidence-flow. Skill:
``precis-taproot-help``.
"""

from __future__ import annotations
