"""Nanopub publication — signed, content-addressed claim artifacts + OTS.

The publication surface over the taproot claim graph
(``docs/backlog/claim-publication-nanopub-ots.md`` is the design spec;
this docstring carries the present state). Taproot remains authoritative;
a nanopub is the *published identity and wire format* — minted locally,
signed, OpenTimestamps-anchored, pushed to the public registry only at
release. Verifiability, not audience: trusty URI + signature + anchored
timestamp is checkable offline by a third party, no server of ours up.

Built (slices 1–3, all local / reversible):

- :mod:`.aida` — canonicalised AIDA URIs: one sentence, one URI
  (``%20`` never ``+``; lenient parse, strict mint — both encodings are
  live in the public corpus for identical sentences). Sentence-only
  identity — narrower than the taproot hub's ``pub_id``, which hashes
  sentence **+** ``scope`` (``precis.identity.make_taproot_hub_paper_id``):
  two hubs forked on scope alone with identical sentence text mint to the
  same AIDA URI (observed twice in the corpus).
- :mod:`.snip` — locator normalization (casefold, whitespace collapse,
  soft-hyphen/ligature strip) + the ``searchSnip`` contract: lowercase
  ASCII tokens, validated **unique-within-paper** against stored chunk
  text at mint; doubles as the PDF deep-link query.
- :mod:`.state` — the publish state machine (``candidate`` → ``reviewed``
  → ``signed`` → ``anchored`` → ``published`` → ``superseded`` /
  ``retracted``; ``rejected`` off ``reviewed``). Legality here; the CAS
  flip is ``store.nanopub_transition``.
- :mod:`.evidence` — read layer: a hub's claim + **bimodal** evidence
  (inbound ``corroborates``/``establishes`` AND outbound
  ``derived-from`` — prod carries both shapes), conjunct atoms,
  live-``contradicts`` detection, grounding resolution to
  DOI + ``pdf_sha256`` + chunk.
- :mod:`.assemble` — the four named graphs (head/assertion/provenance/
  pubinfo) as an rdflib Dataset, for claim / compound / hypothesis
  artifact types. Assertion carries the world-claim only — attribution
  lives in provenance (nanopub convention; also what AIDA convergence
  requires). Compounds are derivations: ``precis:conjunctOf`` edges
  naming atom AIDA URIs, ``prov:wasDerivedFrom`` their trusty URIs.
  Universal anchors only in provenance (DOI, sha, quote, snip) — chunk
  ids and ref ids never leave the internal publish row.
- :mod:`.gates` — Layer-A mechanical mint validators (spec's gate
  checklist): contradicts-edge, primary-source hearsay (four
  detectors: ``section_path`` — references/related-work/prior-art/
  background; in-quote citation markers ``[12]``/``(Moore, 1965)`` —
  catches intro-section hearsay, Miller-index ``[100]`` lookalikes
  exempt; an evidence source with no live body chunk — a ref we hold
  the metadata of but not the paper, so the primary is not in the
  corpus; the same fact as legacy needs-acquisition prose in the hub's
  body, which still blocks as a fallback — hanging mints stay allowed),
  quote verbatim-containment, snip uniqueness,
  structured-field containment, schema lint (claim-without-quote /
  hypothesis-with-quote are hard errors), quantity-bound presence,
  ``pdf_sha256`` uniqueness, publish-row cardinality, drift
  (``claim_sha`` recompute), topo/mint-order.
- :mod:`.mint` — freeze-at-review + mint+sign pipeline: approve stores
  the exact string and grounding; sign builds the artifact via the
  ``nanopub`` reference library (trusty hashing is never hand-rolled),
  writes append-only ``nanopub_artifacts`` bytes, flips the publish row.
  Signing hashes the **artifact**, not the claim: reword ⇒ new
  ``claim_sha``/AIDA URI, re-sign ⇒ new trusty URI only.
- :mod:`.keys` — key custody (vault-resident, 0059 pattern): the bot key
  is worker-invocable and **non-attesting**; the human attesting key
  loads only through the explicitly-interactive door
  (``load_profile(role='attesting', interactive=True)``) — no worker,
  job, or scheduled pass may touch it, which is what keeps "signed"
  meaning "a human checked". *Which* human is a separate question from
  which key: the attesting signature carries the signer's own ORCID iD,
  read off their ``web_users`` row (set at ``/account``) and passed down
  by the web sign button. It must match the identity the key is
  registered to (vault ``NANOPUB_ATTESTING_ORCID``) or the sign is
  refused — so the account field is an authorization check, not a label,
  and no claim is ever attributed to a person who never held the key.
- :mod:`.ots` — OpenTimestamps: nightly Merkle batch over signed
  artifacts (leaf digests = ``byte_sha256`` of the exact stored bytes),
  one calendar stamp per batch, pending→upgraded sweep with a
  stuck-pending alert, and the recompute audit (root + index extracts
  re-derived from bytes; on mismatch the bytes win). Proof store is
  append-only in the DB (0128 triggers), an upgrade INSERTs.

Built (slices 4–5, publish path — POST gated, nothing published):

- :mod:`.preflight` — the publish-time gates, standalone and always run
  before a POST: **withheld-edge enumeration** (an inbound evidence
  edge neither verified-by-refine — ``links.meta['support']`` — nor
  human-signed-off via :func:`.preflight.signoff_edge`'s interactive
  door blocks publication; no mute button), the **trust allowlist**
  (``nanopub_trust_allowlist``, 0129: pinned (identity, fingerprint)
  pairs, flat, zero transitivity; publication requires an *attesting*
  entry — a bot signature alone publishes nothing), state legality,
  drift, dependency order (atoms publish before compounds), hanging
  claims (mintable, never publishable).
- :mod:`.overview` — the "see all the things" read: every claim hub by
  publish state in one query (disputed bucket sorted by dispute age,
  drifted flags, withheld/verified counts) + the **frozen-ness ladder**
  (``reviewed`` freezes the string; ``signed``/``anchored`` freeze the
  artifact bytes; ``published`` is public forever — the rung itself is
  ``state.frozen_rung``, read by both this display and ``demote``).
  ``hub_rows(ref_ids=…)`` narrows the same query to a hit set, which is
  what puts publish posture in ``search(kind='finding')``'s table.
- :mod:`.demote` — the ladder walked **downward**, the answer to
  taproot's ratchet: a newly written ``contradicts`` edge reopens a
  ``reviewed``/``signed`` hub to ``candidate`` (frozen fields discarded,
  the append-only artifact row untouched) and, past the anchor, raises
  for a human supersede/retract instead of touching anything. The
  backward transitions were always legal in ``state.TRANSITIONS``;
  nothing had ever walked them because *evidence* turned.
- :mod:`.registry` — the registry POST, **the one true point of no
  return**, triple-gated: ``interactive=True`` (a person runs it) +
  ``live=True`` (dry run otherwise) + zero blocking preflight issues.
  POSTs the exact stored artifact bytes (``application/trig``), never a
  re-serialization. CLI-only door (``precis nanopub publish --live``) —
  deliberately no web button.
- Review-and-sign web surface — ``precis_web/routes/nanopub.py``:
  ``/nanopub`` three-pane workbench (claim forest via
  :func:`.overview.hub_tree` | per-hub review pane | paper pane, framed
  with ``?embed=1`` chrome-less mode, draggable dividers; disputed strip
  + OTS status folded in; ``/nanopub/tree`` redirects here),
  ``/nanopub/fi<id>`` per-hub review page (clickable SVG claim DAG,
  publish-row side panel, symmetric dispute rendering, one action per
  state, sign button that signs for real, approve form prefilled with a
  gate-passing quote+snip candidate per grounding chunk — or, for an
  agent-proposed hypothesis, with the envelope it parked on
  ``refs.meta.proposed_payload``),
  ``/np/<code>`` serving exact frozen bytes during embargo.
- Export appendix — a draft citing a hub whose publish row is
  signed/anchored/published gets a "Published claim artifacts"
  end-matter section (frozen AIDA sentence + trusty URI + status) in
  both exporters, zero draft edits; unminted hubs leave the export
  byte-identical (``precis.export._nanopub_appendix``, entries fed by
  the trust tracker's cited-set). First slice of the fi→np surface
  migration (``docs/backlog/retire-fi-go-nanopub.md``).

Built (registry mirror — dark, nothing pulled):

- :mod:`.mirror` — read-only sidecar caching *other people's* published
  nanopubs (``nanopub_mirror`` + ``nanopub_mirror_edges``, 0130; no
  append-only trigger — not our proof store). Parse leniently, validate
  strictly: trusty recompute over the fetched bytes plus the
  requested-code check sets ``verified``; a verified row is never
  overwritten. np→np references land in the edge table (``to_code``
  deliberately not an FK — open-world arrival order, multiple
  retraction claimants); ``retracted_by``/``superseded_by`` are
  *derived* flags under the authoritative-retraction rule (flagging
  signer == target signer). Sync is a PK diff against the registry's
  full code list (one flat array, no paging — probed 2026-08-15),
  bounded per pass, resumable by construction; fetches via
  ``safe_fetch`` with mirror-host fallback. Daily ``nanopub_mirror``
  cadence for the delta + concurrence alerts (external nanopub
  asserting one of our AIDA sentences, both wild encodings matched);
  the initial ~87k pull is the manual door
  ``precis nanopub mirror sync --live --all``.

Read surface: ``get(kind='finding', view='nanopub')``
(``handlers/_finding_nanopub.py``) — unsigned draft TriG for a hub
pre-mint (placeholder URI, draft comments allowed: comments are lexical
syntax outside the integrity envelope and are stripped at mint), the
exact frozen artifact bytes once signed.

NOT built / not run: no artifact has been POSTed anywhere (publishing a
first claim is Reto's call, via the CLI door). The OTS calendar
round-trip and the registry mirror are env-gated
(``PRECIS_OTS_ENABLED`` / ``PRECIS_MIRROR_ENABLED``); the deploy tree
now sets both ON cluster-wide on the collapsed worker
(``precis_worker_nanopub_{mirror,ots}`` kill switches — cluster-wide
because the daily cadence lease is a fleet singleton with no
eligibility check, so per-host enablement starves the pass). The
initial ~87k mirror pull is still the manual door; signing keys exist
(``precis nanopub keygen`` has run — attesting-fingerprint publication
on ORCID is Reto's pending step), and publication requires an attesting
allowlist entry — an empty allowlist means nothing is publishable.
"""

from __future__ import annotations
