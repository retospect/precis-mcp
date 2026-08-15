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
  live in the public corpus for identical sentences).
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
  checklist): contradicts-edge, primary-source ``section_path``
  (references/related-work/prior-art/background grounding is hearsay —
  rejected), quote verbatim-containment, snip uniqueness,
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
  meaning "a human checked".
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
  drifted flags, withheld counts) + the **frozen-ness ladder**
  (``reviewed`` freezes the string; ``signed``/``anchored`` freeze the
  artifact bytes; ``published`` is public forever).
- :mod:`.registry` — the registry POST, **the one true point of no
  return**, triple-gated: ``interactive=True`` (a person runs it) +
  ``live=True`` (dry run otherwise) + zero blocking preflight issues.
  POSTs the exact stored artifact bytes (``application/trig``), never a
  re-serialization. CLI-only door (``precis nanopub publish --live``) —
  deliberately no web button.
- Review-and-sign web surface — ``precis_web/routes/nanopub.py``:
  ``/nanopub`` queue table, ``/nanopub/fi<id>`` per-hub review page
  (clickable SVG claim DAG, publish-row side panel, symmetric dispute
  rendering, one action per state, sign button that signs for real),
  ``/np/<code>`` serving exact frozen bytes during embargo.

Read surface: ``get(kind='finding', view='nanopub')``
(``handlers/_finding_nanopub.py``) — unsigned draft TriG for a hub
pre-mint (placeholder URI, draft comments allowed: comments are lexical
syntax outside the integrity envelope and are stripped at mint), the
exact frozen artifact bytes once signed.

NOT built / not run: no artifact has been POSTed anywhere (publishing a
first claim is Reto's call, via the CLI door); the registry mirror is
planned in ``docs/backlog/nanopub-registry-mirror.md``; the OTS
calendar round-trip ships dark (``PRECIS_OTS_ENABLED``); keys are not
yet generated (``precis nanopub keygen``), and the allowlist starts
empty — an empty allowlist means nothing is publishable.
"""

from __future__ import annotations
