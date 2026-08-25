"""Taproot — the evidence-grounded claim graph.

A claim lives on exactly one **claim hub**: a ``finding`` tagged
``TAPROOT:claim`` + ``STATUS:canonical`` (off the chase STATUS lifecycle, so
``chase``'s claim query never re-picks a hub up). Papers attach as evidence
edges. The graph is an overlay on ``finding``/``ref_tags``/``links`` — its
only own schema is ``claim_embeddings`` (migration 0101) plus link relations
``establishes`` (0094, seeded without an inverse: hubs read evidence via
``links_for(direction='in')``), ``refines`` (0100), ``conjunct-of``
(0126, atom -> compound, asymmetric, no inverse, mirrors 0100), and
``motivated-by`` (0135, hypothesis -> the artifact that provoked it, same
advisory contract: no evidence flows); ``corroborates`` / ``contradicts``
reuse existing slugs, endpoint kinds disambiguate. A claim hub is
**atomic** (evidence-bearing), **compound** (an un-decomposable bundling
sentence, no direct evidence — see :mod:`.hub`), or **hypothesis** (a
conjecture, evidence-free *by type*: it carries motivation and a
discriminating experiment instead, marked by ``refs.meta.artifact_type``
and minted through ``handlers/_finding_hypothesis.py``). The widening pass
skips hypotheses — searching a corpus for support of a guess is a
confirmation engine (:func:`.canon.not_hypothesis_predicate_sql`).
Design:
``docs/backlog/taproot.md``; governance: taproot evidence relations (+ the living citation pins).

**The claim lifecycle — ten stages, and which are real.** A claim is admitted,
placed, grounded, widened, weighed, opposed, adjudicated, gated, published:

1. **Extract** — is this sentence a claim? — live (:mod:`.canon`)
2. **Admit** — falsifiable · self-contained · method-attributed · single
   assertion — live; *advisory at mint, blocking at approve*
   (``nanopub/gates.py::_BLOCKING_LINT_CODES``)
3. **Place** — is this the same claim we already hold? — live (``place``)
4. **Ground** — which passage supports it — live, per-passage
5. **Widen** — who else in the corpus speaks to this? — **built, dark**
   (``workers/hub_refine.py``)
6. **Weigh** — how much *independent* support? — partial: the union-find
   supporter count (``handlers/_finding_evidence.py``) is display-only and
   gates nothing
7. **Oppose** — what conflicts with this? — **found but discarded** on the
   enrichment path: ``workers/_chase_llm.py::_verify_support_with_caveats``
   returns a ``contradicts`` flag that hub_refine memoes as rejected instead
   of writing the edge. Reground's strict judge (also dark) does write it:
   a CONTRADICTS verdict re-attaches as a ``contradicts`` edge rather than
   dropping the old one
8. **Adjudicate** — is the conflict real, and who wins? — **absent**
9. **Gate** — publishable? — live, admissibility only
10. **Publish** — mint · sign · anchor — live, human doors

Two structural cautions this ordering hides. **The ratchet**: every stage
promotes and almost nothing demotes, so a claim accumulates support and never
re-opens when contradicting evidence lands later — ``chase_trigger``'s
``TAPROOT_DUE`` marking is the only re-opening mechanism, and it is dark.
**Scale changes the risk profile of a wrong judge**: at a handful of
hand-written edges a bad LLM verdict is a nuisance; at ~1.5k hubs × k
candidates the same error rate is a corpus-wide event. Every automated writer
here needs a confidence floor, an idempotency story, and a dry-run mode before
its first large run — ``place``'s confidence gate on ``contradicts`` is one
instance of that pattern, not a one-off.

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
- :mod:`.reground` / :mod:`.repair_evidence` — "no source, no atom".
  ``reground`` ranks a source's body chunks against a claim (lexical
  overlap + notation folding), excludes hearsay sections, verifies support
  by LLM and then **post-validates the quote in code** (verbatim in the
  claimed chunk, unique across the paper), yielding a grounding record or
  one of four named reasons (``no-passage``/``hearsay-only``/
  ``verify-rejected``/``quote-validation-failed``). ``repair_evidence``
  points that at the July batch of edges that assert ``support: "yes"``
  with ``meta.source_handle`` jsonb-null and no ``src_chunk_id``
  (``precis taproot repair-evidence``, dry-run default): re-ground against
  ONLY the source the edge already names, then repair **in place**
  (``UPDATE links``) — ``attach_evidence`` would insert a second row and
  leave the broken one live. An empty verdict is recorded, never patched:
  no path here writes ``refs.title`` or a ``finding_body`` chunk.
- :mod:`.directed` — demand-driven claim minting (``precis taproot
  direct-mint``, dry-run default): ``qualify_claim`` (BIG; one-way fit
  claim→evidence + verbatim-quote anti-hallucination) then the same
  block→judge→place cascade through :mod:`.hub`; ``meta.demanded_by``
  accumulates the requesting passages. Directed, never harvest.

Producers (all dark by default; each degrades to a logged no-op when no
embedder is available). **Enablement, canonically —** two mechanisms, and the
difference matters because one of them looks like the other and isn't:

- A pass that is its own **service** (``hub_refine``, ``chase_trigger``, the
  axis classifier) flips live via a ``service_config`` prio row —
  ``precis service prio <host> <service> 1``, no redeploy. Since the §L
  control cutover, registration is purely structural
  (``cli/worker.py::_should_register``): a ``ServiceSpec``'s ``enable_env``
  **is never read**, so setting ``PRECIS_TAPROOT_REFINE_ENABLED`` or
  ``PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`` in a plist does nothing at all.
  The per-cycle ``pass_gate`` is the one decision point. Enable on **one
  host** — ``hub_refine``'s rejection memo is a read-modify-write on ``meta``
  (procedure: ``docs/runbooks/taproot-chase-enablement.md``).
- A **sub-feature of another pass** has no ``ServiceSpec`` to flip, so it
  keeps a genuine in-pass env flag, consulted per call: the forward chase
  bridge (``PRECIS_TAPROOT_CHASE_ENABLED``, ``workers/chase.py``) and the
  inbound chase / citer sidecar (``PRECIS_INBOUND_CHASE_ENABLED``,
  ``workers/inbound_chase.py::inbound_chase_enabled``, also gating the paper
  render at ``handlers/paper.py``). These two are live env vars; do not
  "modernize" them into ``service prio``.

- **Forward chase bridge** (``workers/chase.py::_taproot_bridge``,
  ``PRECIS_TAPROOT_CHASE_ENABLED`` — in-pass env flag, see above) — on a
  finding's established-terminal hop
  builds the claim from the finding's own title (no ``extract_claim``; a
  chase finding is already a user-asserted claim) and runs block -> judge ->
  place -> ``apply_placement`` in the SAME transaction as the
  ``STATUS:established`` flip, savepoint-isolated so a taproot failure never
  rolls back the flip. Skips on NO-SUPPORT. Intermediate chain hops attach as
  extra corroborators. Idempotent: a re-established finding's ``block`` finds
  the existing hub and ``add_link`` no-ops the repeat edge.
- **hub_refine** (``workers/hub_refine.py``; ``service prio`` — stage 5,
  *Widen*) — revisits *existing* hubs (everything
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
  **Reground** (docs/backlog/taproot-reground.md) is this same pass grown
  from additive-only enrichment into a re-grounding pass — deliberately
  NOT a second hub-improver. With ``RegroundConfig`` active it also audits
  every *existing* edge against a strict judge (primary content -> KEEP;
  asserts/defers/review-deferral/abstract-for-a-measurement/front-matter/
  bibliography -> PRUNE; primary-against -> CONTRADICTS; default KEEP on
  uncertainty), re-discovers deeper passages **inside papers the hub
  already grounds on** (the dominant fix is "right paper, wrong chunk"),
  applies a grounding-depth policy (definition/existence accepts an
  abstract, measurement/mechanism needs a body passage), and removes
  through :func:`.hub.remove_evidence` — add-first, read back from
  ``links``, strand-guarded, logged to ``meta.reground_log``. A second
  memo (``meta.reground_seen``, sha-keyed like ``last_refined_sha``) keeps
  it converging. All of it ships dark behind ``PRECIS_TAPROOT_REGROUND*``;
  the prune sub-stage additionally must not be enabled until
  :mod:`.slice_refine_eval` passes on the deployed strict rubric, and
  retire/regenerate has no env flag at all (job param + per-hub opt-in
  tag). Job glue: ``reground_claim``, which also exposes the
  intent-vs-committed diff as a read-only ``mode='verify'``.
- **chase_trigger** (``workers/chase_trigger.py``; ``service prio``) — the
  incremental due-set
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
the LLM cascade runs on the cluster worker, never in the MCP handler). A
fetched-``[pa]`` re-ground also mints a ``citation`` audit record per located
supporter (tagged ``origin:draft-backfill``), so the intermediate ``[pc]``
carries the claim the locate proved instead of a bare pointer.
Evidence grounding requires **prose**: a chunk with no assertion (a paper's
title/author front-matter block) is refused as a grounding passage in both
arms — filtered out of the re-ground candidate pool, and dropped as a ``[pc]``
supporter (action ``ungroundable`` when that leaves none). An edge grounded
there would say "this paper exists", not "this passage supports the claim".
Abstracts and numeric tables ground fine; the test is prose, not ``ord``.

Read surfaces: ``get(kind='finding', view='evidence')`` (originators /
corroborators / contradicts tables); the default ``finding`` search cohort
unions hubs in alongside ``STATUS:established``; the ``/claim/<head>`` page;
the fisheye Claims ring (pin markers + ``refines`` lines).

Not built: citation-card dedup (Phase-2 slice 2d), the S2
global-citation-count originator fallback, the integrity axis (Phase 4), a
corpus-wide backfill sweep, ``refines`` evidence-flow. Skills:
``precis-taproot-help`` (orientation, citing), ``precis-taproot-mint-help``
(authoring/minting/merging), ``precis-taproot-backfill-help`` (batch
``[pc]``/``[pa]`` conversion).
"""

from __future__ import annotations
