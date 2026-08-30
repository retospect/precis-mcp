"""Nanopub — the publish pipeline: freeze, sign, timestamp, register.

Third layer of one domain (:mod:`precis.handlers.finding` = the ref kind,
:mod:`precis.taproot` = the claim graph); this package owns publish only —
claim-hub/evidence-edge mechanics are taproot's, not restated here. A
nanopub is the published identity + wire format for an approved claim hub:
minted locally, signed, OpenTimestamps-anchored, pushed to the registry
only at release — verifiable offline by a third party (trusty URI +
signature + anchored timestamp), no server of ours required. Design spec:
``docs/backlog/claim-publication-nanopub-ots.md``; this docstring is
present state.

Module map (detail lives in each module's own docstring):

Slices 1-3 — local, reversible:

- :mod:`.aida` — AIDA URI canonicalization: sentence-only identity,
  narrower than the taproot hub's ``pub_id`` (sentence+scope,
  ``precis.identity.make_taproot_hub_paper_id``) — two hubs forked on
  scope alone with identical sentence text mint to the same AIDA URI
  (observed twice in corpus).
- :mod:`.snip` — locator normalization + the searchSnip contract
  (unique-within-paper at mint, doubles as the PDF deep-link query).
- :mod:`.state` — the publish state machine; legality only, the CAS
  flip is ``store.nanopub_transition``.
- :mod:`.evidence` — resolves one hub into a ``HubBundle`` (bimodal
  evidence edges, conjunct atoms, live-``contradicts``, grounding) for
  the assembler and gates.
- :mod:`.assemble` — the four named graphs (head/assertion/provenance/
  pubinfo) as an rdflib Dataset; universal anchors only (DOI, sha,
  quote, snip) — internal ids never leave the publish row.
- :mod:`.gates` — Layer-A mechanical mint validators; full checklist in
  the module docstring.
- :mod:`.mint` — freeze-at-review + mint+sign pipeline; signs the
  artifact, not the claim (reword => new claim identity, re-sign => new
  artifact identity only).
- :mod:`.keys` — vault-resident key custody: bot key worker-invocable
  and non-attesting, human attesting key loads only through an
  interactive door.
- :mod:`.intro` — public-key -> ORCID introduction nanopub; the binding
  also needs the person's own out-of-band ORCID-record edit.
- :mod:`.ots` — OpenTimestamps: nightly Merkle batch over signed
  artifacts, pending->upgraded sweep, recompute audit.

Slices 4-5 — publish path (POST gated, nothing published yet):

- :mod:`.preflight` — publish-time gates run before every POST
  (withheld edges, trust allowlist, state/drift/dependency order,
  hanging claims never publishable) — standalone module doc has the
  full breakdown.
- :mod:`.overview` — every claim hub by publish state in one query +
  the frozen-ness ladder (``state.frozen_rung``), shared with
  ``demote``.
- :mod:`.demote` — the ladder walked downward: a new ``contradicts``
  edge reopens a pre-anchor hub to ``candidate``; past the anchor it
  raises for a human supersede/retract instead.
- :mod:`.registry` — the registry POST, the one point of no return:
  ``interactive=True`` + ``live=True`` + zero blocking preflight
  issues, exact stored bytes, CLI-only.
- Review-and-sign web surface — ``precis_web/routes/nanopub.py``:
  ``/nanopub`` three-pane workbench (claim forest via
  :func:`.overview.hub_tree` | per-hub review | paper pane; ``?embed=1``
  chrome-less; disputed strip + OTS status folded in); ``/nanopub/fi<id>``
  per-hub page (SVG claim DAG, publish-row panel, one action per state,
  a real sign button, approve form prefilled with a gate-passing
  quote+snip per grounding chunk, or for an agent-proposed hypothesis
  the envelope parked on ``refs.meta.proposed_payload``); ``/np/<code>``
  serves exact frozen bytes during embargo.
- Export appendix — a draft citing a signed/anchored/published hub gets
  a "Published claim artifacts" end-matter section (frozen AIDA
  sentence + trusty URI + status) in both exporters; unminted hubs
  leave the export byte-identical (``precis.export._nanopub_appendix``,
  fed by the trust tracker's cited-set). First slice of the fi->np
  migration (``docs/backlog/retire-fi-go-nanopub.md``).

Registry mirror (dark, nothing pulled):

- :mod:`.mirror` — read-only cache of other people's published nanopubs
  (``nanopub_mirror``/``nanopub_mirror_edges``, 0130; not our proof
  store). np->np references land in an edge table (``to_code``
  deliberately not an FK — open-world arrival order, multiple
  retraction claimants); ``retracted_by``/``superseded_by`` are
  *derived* flags under the authoritative-retraction rule (flagging
  signer == target signer). Module doc has the parse/verify/sync
  detail.

Read surface: ``get(kind='finding', view='nanopub')``
(``handlers/_finding_nanopub.py``) — unsigned draft TriG pre-mint
(placeholder URI, draft comments allowed, stripped at mint), exact frozen
bytes once signed.

NOT built / not run: no artifact has been POSTed anywhere (Reto's call,
via the CLI door). OTS and the registry mirror are dark switches
(``PRECIS_OTS_ENABLED`` / ``PRECIS_MIRROR_ENABLED``); the deploy tree sets
both ON cluster-wide on the collapsed worker
(``precis_worker_nanopub_{mirror,ots}`` — cluster-wide because the daily
cadence lease is a fleet singleton with no eligibility check, so per-host
enablement starves the pass). The ~87k mirror pull is still manual;
signing keys exist (``precis nanopub keygen`` has run — the introduction
nanopub's live POST + ORCID back-link are Reto's pending steps, no
self-hosted fingerprint page by decision); publication requires an
attesting allowlist entry — an empty allowlist means nothing publishable.
"""

from __future__ import annotations
