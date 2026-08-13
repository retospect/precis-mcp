---
status: draft
title: Claim publication — signed nanopubs + OpenTimestamps anchoring, minted locally, pushed at publication time
model: opus
blocked-by: taproot-atomic-claims
---

# Claim publication — nanopubs + OpenTimestamps

Publish reviewed taproot claims and their evidence edges as **signed,
content-addressed nanopublications**, minted locally and pushed at
publication time, with an independent **OpenTimestamps** anchor for
anteriority. Taproot remains authoritative; nanopub is the published
identity and wire format, not a storage layer.

Goal is verifiability, not audience: a reader of a review paper can check
every claim's evidence graph without trusting us and without our server
being up. Trusty URI + signature + anchored timestamp *is* the "claims
release form", checkable offline by a third party.

Blocked on `taproot-atomic-claims`, but **per hub, not globally**:
publishing a compound claim would freeze mis-grained evidence into an
immutable artifact; a decomposed, reviewed hub can publish while the rest
of the graph is still compound. No flag day.

## Network reality (probed 2026-08-13)

`registry.nanodash.net` (registry 1.11.4) + `query.petapico.org`: **87k
nanopubs, 693 agents** — the whole current registry generation, and
metadata-dominated (concept declarations, FAIR profiles, a bulk
terminology dump). The literature-claim slice is thin (~3.5k distinct
DOIs, ~7k CiTO triples) and has **zero coverage in our domain**:
`electrocataly*`, `oxygen evolution`, `perovskite`, `zeolite` all 0.

**Consequence: no ingest as evidence.** The earlier requirement to search
external nanopubs and verify before use is cut — nothing out there moves a
trust rung, and the Rules below guard concurrence regardless.

**Decided 2026-08-13: mirror the registry anyway.** Sync all nanopubs
locally — 87k is a few hundred MB at most, and replication is what the
registry protocol exists for. Store the raw TriG plus an extracted index
(AIDA URI, signer key, DOIs, predicates); outbound fetch via `safe_fetch`.
Sync *everything* and **flag** retracted/superseded at index time rather
than excluding at import: a retraction is itself a record, and it can
arrive after its target — surface non-retracted by default. What the
mirror buys, in order: **mint-time concurrence detection** (converge on an
existing AIDA URI instead of minting a near-duplicate); the **definitive
coverage check** (embed the claim-bearing subset with bge-m3, ANN against
`claim_embeddings` — the keyword scan above was lossy); a **real-world
fixture corpus** for the parse-leniently/validate-strictly gate (the live
malformed keys and typo'd predicates are exactly the test cases we need);
and **post-publication inbound-concurrence detection** as a delta-sync
scan instead of polling. The mirror is a read-only sidecar: external
nanopubs never enter taproot as evidence.

## Vocabulary (corrected against observed usage, not guessed)

| Thing | Term | Note |
|---|---|---|
| the claim | `aida:Sentence` | the sentence *is* the URI |
| evidence edge | `cito:obtainsSupportFrom` | the dominant term in the corpus |
| passage grounding | `cito:hasQuotedText`, `cito:includesQuotationFrom` | how the community grounds quotes |
| passage precision | `oa:TextPositionSelector` | supplement only — `oa:` is rare network-wide |
| extraction / verification provenance | PROV-O | provenance graph is PROV-shaped by convention |
| retract / supersede / sign | `npx:` | mandatory; heavily used |
| agents | ORCID | `orcid` kind already exists |
| decomposition, `refines` | local `precis:` predicates | no standard term; define at a resolvable URI |

Rejected: Web Annotation selectors as *primary* grounding (too rare to
interoperate); the micropublication ontology (0 uses network-wide, and
`cito:obtainsSupportFrom` already covers document→claim).

**Encoding must be normalised.** Both `%20` and `+` appear for spaces in
live AIDA URIs — *different URIs for identical sentences*. Content-
addressed convergence is the entire benefit, so canonicalise encoding
before minting or matching; parse leniently on ingest (`npx:singedBy` is a
live typo'd predicate in the corpus).

## Rules

- **Publish stored facts, never derivations.** Seniority
  (`taproot/seniority.py`) and the trust ladder (`taproot/trust.py`) are
  recomputed from moving inputs and recomputable by a third party from the
  published claims, edges and DOIs — freezing them publishes staleness.
  `vouched` encodes "we could not obtain the source" and must never leave
  the building.
- **Freeze at human review.** Before review the hub is mutable and the
  canonicalizer merges freely. At review a specific claim string is
  approved and the trusty URI minted over it. After review, new evidence
  attaches as *new* nanopubs; a changed claim is a new nanopub plus a
  supersede, never an edit.
- **Claim and evidence-edge nanopubs are separate artifacts** —
  load-bearing: it is what allows post-publication corroborators without
  superseding the reviewed claim.
- **A published hub never auto-merges.** `canon.place` escalates a
  low-confidence `same` to `merge_confirm`; against a published hub any
  `same` verdict must hard-stop to `needs_review`, no LLM escalation — a
  merge there is a public supersede. The one failure mode with public
  consequences.
- **Concurrence is not evidence.** An external nanopub asserting the same
  sentence is an *agent's assertion* — no paper, passage or measurement.
  Distinct category, hard-guarded: it must never inflate a trust rung that
  means "a passage in a paper supports this".
- **Attestation semantics.** What is signed is not "this claim is true"
  but "this claim was extracted from this passage, and the passage was
  verified to support it". Matches what review actually checked; expressed
  in PROV-O plus an explicit attestation predicate. This decision matters
  more than any crypto choice.

## OpenTimestamps

`npx:wasCreatedAt` is **self-asserted** and proves nothing against a lying
author. OTS closes that gap: a Bitcoin-anchored proof that a hash existed
by time T — free, serverless, no account. **Greenfield** in precis, all
new code. Verified 2026-08-13: `opentimestamps-client` 0.7.2 on PyPI
(`ots stamp/upgrade/verify`); `alice.btc.calendar.opentimestamps.org`
reachable.

**What it proves:** existence no later than T — an *upper* bound only.
Nothing about authorship (the signature's job) or truth. It can prove a
signature predates an event; it can never prove one was made late.

**Primary justification is key rotation, not priority.** If a key is
compromised at T, signatures before T remain trustworthy — but
`wasCreatedAt` is attacker-writable, so only an anchor distinguishes
before from after. Without anchors one compromise forces repudiating
everything ever signed with the key; with them, repudiation is scoped by
date. The same property makes a retraction credible: it proves *when* we
retracted, hence whether it preceded a challenge.

**One anchor point — decided 2026-08-13 (simplification 1).** Anchor the
*signed nanopub* only; that anchor shows when it was signed, and the
artifact contains the text, so the text's existence is bounded by the
same proof. Rejected: a second, embeddable pre-signature text anchor —
its only gain is priority between text-freeze and signing, which happen
in the same review session.

**Batching — decided: occasional Merkle batches, not per-item.** Hash each
item, Merkle-root whatever is waiting, stamp the root once, store the root
proof plus each item's inclusion path. Cost per batch is one calendar
request and one proof file. Cadence — decided 2026-08-13
(simplification 2): **one daily sweep**, one cron. Granularity caps at
24h, which is enough when the anchor's primary job is key-rotation
scoping rather than fine-grained priority.

**Single calendar — accepted, risk named.** Multiple calendars buy
*availability*, not trust: a calendar cannot forge an anchor, since
verification runs against Bitcoin block headers. The exposure is the
pending window (hours to ~a day between stamp and upgrade): a lost pending
commitment means re-stamping at a later date — survivable because the raw
bytes are retained (below). The upgrade sweep must therefore **alert on a
proof still pending past a threshold** and re-stamp. Adding `bob.btc.*`
later is a config line, not a redesign.

Anchor: **reviewed claim nanopubs** at approval; **review attestations**
(the *date* of verification is exactly the self-asserted fact worth
anchoring); **retractions/supersedes** (before or after a challenge?);
**draft/paper snapshots** at submission. Do **not** anchor ingested
chunks, embeddings, or unreviewed machine-minted edges — working state,
uncontested.

**Operational:** proofs are *pending* until the Bitcoin tx confirms and
the calendar upgrades them, so the workflow tolerates a pending state and
runs an upgrade sweep. Verification needs a block-header source (node,
explorer, or trusted calendar); state that source alongside the proofs.

### The proof store must be immutable and complete

A proof commits to a hash: lose the exact bytes that were hashed and it is
unverifiable. Merkle batching adds the same failure one level up — the
root proof stays valid while every individual item becomes unprovable,
because an item's proof *is* its inclusion path. **The raw bytes and the
leaf table are part of the proof, not metadata about it.**

Retain per batch: the **exact serialized bytes** of every anchored item
(not a re-serialization — normalization differences change the hash); each
**leaf hash, index, and sibling path**; the **construction rule** (hash
function, domain separation, odd-node handling, ordering — a bare hash
list cannot reproduce a root); the root, the `.ots` binary, and its
pending/upgraded state.

Fragility is uneven: a signed nanopub is self-addressing and re-fetchable
from any registry holding it. Irreplaceable are the **leaf table +
construction rule** (exist nowhere else), the **`.ots` binary**
(re-stampable only at a later date, losing paid-for priority), and
anchored items with no self-addressing: unpublished attestations, draft
snapshots.

Immutability by construction, descending value:

1. **Verifiable** — recompute the root from retained leaves and bytes; a
   mismatch means alteration. Detection *without trusting the store*; run
   as a periodic audit, not only on demand.
2. **Append-only** — no UPDATE/DELETE on anchored rows; corrections are
   new batches. Same convention the codebase keeps for `chunks` body rows.
3. **Replicated off-box** — detection is not recovery; the nightly
   `pg_dump` rotation covers the DB.

Storage home — **decided 2026-08-13: table.** (Rejected en route: the
`provenance` kind is Crossref retraction monitoring
(`ingest/provenance.py`); files under `PRECIS_ROOT` split the killing
consistency guarantee across two stores.) Row shape: the **plain signed
text** — the exact bytes, the authority; the **signature / proof
material** (base64 `.ots`); and **indexed columns extracted from the
bytes** (AIDA URI, `claim_sha`, DOIs, state). Deliberately not DRY, and
safe here precisely because rows are append-only: derived columns cannot
go stale when nothing mutates, and the periodic recompute audit checks
them against the bytes along with the Merkle root. On any mismatch the
bytes win — index columns are rebuildable, the bytes are not.

Postgres enforces most of the tie natively. One INSERT writes all fields
atomically. A raw-hash column over the stored bytes can be
`GENERATED ALWAYS AS (sha256(…)) STORED` — not writable at all, so never
wrong. A `CHECK` constraint pins any writable derived column expressible
in SQL to the bytes. And append-only becomes a DB property, not a
convention: `REVOKE UPDATE, DELETE` plus a `BEFORE UPDATE OR DELETE`
trigger that raises — after which post-insert divergence is impossible by
construction, because nothing can change. Two limits: parse-derived
extracts (AIDA URI, DOIs) exceed what a SQL expression can check — the
recompute audit covers those; and do **not** replicate `canon.claim_sha`
in SQL if it normalizes before hashing — two implementations of one hash
drift, so SQL-enforce only hashes taken over the exact stored bytes.

## Publication-time trust gate — an explicit `(ORCID, key)` allowlist

Only signatures from an explicit allowlist are trusted at publication
time. Properties forced by the corpus (probed 2026-08-13):

**Pin keys, not bare ORCIDs.** The key→ORCID binding is self-asserted:
`npx:declaredBy` lives in an introduction nanopub signed by the very key
being declared, and ORCID never vouches — so a bare-ORCID check is
defeated by a forged introduction. Entries are `(ORCID, fingerprint set)`
— multiple keys per identity is normal (one per tool; one observed
identity has 11) — and a **new key for an allowlisted ORCID is not
automatically trusted**. Reject malformed identifiers: non-ORCID
`signedBy` values and a shared `0000-0000-0000-0000` placeholder are live
in the corpus.

**Flat, zero transitivity — deliberate divergence from the network.** The
registry computes rooted transitive weighted trust; transitivity at
publication time lets an endorsee's endorsee into the trust set unvetted.
`npx:approvesOf` may inform adding an entry by hand, never automatically.

**The allowlist is a published artifact, not a config file.** A later edit
would make "only allowlisted signers were trusted" unfalsifiable. Version,
sign, OTS-anchor it; each published claim references the allowlist version
that gated it — the gate itself becomes third-party verifiable.

**Scope, honestly:** this gates only the nanopub-sourced surface — papers
and patents carry no signature, and external coverage is zero, so as an
external filter it is policy-in-advance. Where it is live immediately is
**our own identities**: bot mints, human attests, and the allowlist is the
enforcement point for "only human-attested claims are publishable" —
freeze-at-review, mechanised.

### Key custody

**Decided 2026-08-13: keys live in the secret store** (the `0059`
secrets-vault pattern) — the deployment is local-ish and that trust is
accepted. The boundary that matters moves from *storage* to
**invocation**: if an autonomous job can sign with the attesting key, a
signature means "precis said so", not "a human checked" — voiding the
attestation semantics the design rests on. So:

- **Human (attesting) key** — in the vault, invocable **only from the
  interactive review surface** (the sign button, or a CLI a person runs).
  No worker, job, or scheduled pass may touch it. The only key the
  allowlist marks attesting.
- **Bot key** — a key costs nothing: for machine-derived provenance
  artifacts, invocable by workers. Allowlisted explicitly as
  **non-attesting** — a bot signature alone authorizes nothing; every
  publication requires the human key's attestation.

Accepted risk, recorded: anything holding `agent_rw` can read the vault,
so cryptographically a signature proves "vault access" — "a human checked"
rests on the invocation convention plus the local-ish deployment. Cheap
hardening if that ever itches: keep the attesting key
passphrase-encrypted at rest and prompt at sign time. Revisit the posture
if the deployment stops being local.

**One key per identity, deliberately** — corpus multi-key is a tooling
artifact (each web tool silently generates its own pair). Sign only
through our tooling; a stray web-tool signature creates an accidental
second identity.

**Key size floor — hard requirement.** RSA-1024 is the *plurality* of
distinct corpus keys and below current recommendations; sign at **2048
minimum, 4096 preferred**. At the gate: parse the DER, require valid SPKI,
check modulus size, reject unknown algorithms — truncated keys and junk
`hasAlgorithm` values are live in the corpus. Parse leniently, validate
strictly.

**Validity windows.** Rotation does not invalidate old signatures, so an
entry is `(identity, fingerprint, valid_from, valid_until)`, checked
against the window in force *when the signature was made* — which needs a
trustworthy time, i.e. the OTS anchor; the two features are coupled. No
key-revocation convention verified in the wild; the allowlist is
authoritative here too.

**Losing the key is unusually costly** — published nanopubs could never be
superseded or retracted under that identity, and they are immutable.
Backup is a requirement; publish a succession/rotation statement while
still possible.

**Publish our fingerprint out-of-band** (a well-known URL we control, and
the review paper itself) — `signedBy` proves nothing, so readers need an
independent binding. Symmetry: the allowlist governs inbound trust; the
published fingerprint governs outbound.

Open: revocation policy — an identity leaving the allowlist after its
claims were cited cannot be unpublished; the remedy is a supersede or
qualification nanopub recording the changed basis, but the policy is a
judgement call.

## No nanopub server

POST to an existing registry and it propagates. The verifiability we want
comes from trusty URI + signature + OTS anchor, all checkable offline from
the files; a server only adds *discovery*, worth little at 87k nanopubs
with zero field coverage. Serve the TriG files from `precis_web` alongside
the existing `/claim/<head>` page — a route handler, not a daemon.
Revisit only if SPARQL over our own claim set becomes desirable.

## Signing and web of trust

Signing is universal in the corpus and works standalone — a locally
minted, locally signed nanopub is verifiable by anyone. The registry
additionally computes rooted transitive weighted *agent* trust; joining
needs an introduction nanopub plus an `approvesOf` path from an existing
agent — a social step, deferrable, not a prerequisite. It rates **agents,
not claims**; what is checkable is the evidence graph, which is the point.
Nanopubs are single-signature artifacts (the signature is inside the
hashed content), so no co-signing; independent concurrence happens via
content-addressed AIDA URIs instead — two agents asserting the same
sentence converge on the same claim node with separate signatures.

## Pre-mint identity and reference integrity

The scaffold problem: an LLM proposes a tree of candidate claims and
edges, a human walks it approving and rewording, and both approval and
signing change content hashes. What do pre-minted references point at?

**`ref_id` — and this already works.** Candidates are `finding` refs;
edges between them are `links` rows written through
`taproot/hub.py::link_claims`. A ref id survives any rewording; no
pre-mint identifier scheme is needed — inventing one would be the mistake.

**Content hashes are never referents — existing repo precedent.**
`taproot/canon.py::claim_sha` is pure over `finding.title` and appears in
the schema only as a *staleness gate*: `claim_embeddings` keys on
`(claim_ref_id, embedder)` with `claim_sha` alongside (migration 0101);
`workers/hub_refine.py` gates reopens off the same hash. The publish-state
row is a third instance of that pattern — so rewording during the review
walk auto-invalidates exactly the derived rows it should.

**Three hashes; signing touches only one:**

| hash | derived from | changes on reword | changes on signing |
|---|---|---|---|
| `claim_sha` | claim sentence (`finding.title`) | yes | **no** |
| AIDA URI | claim sentence, encoding canonicalised | yes | **no** |
| trusty URI | all four graphs, *including* the signature | yes | **created by it** |

Signing does not re-hash the claim; it hashes the **artifact** — two
objects, two identities. The claim's identity is fixed the moment its text
is approved, before any key is touched — which is why an evidence edge can
name a claim by AIDA URI with no ordering dependency on the claim nanopub.

**Approval order is free; only mint order is constrained.** Edges carry
ref ids, so claims can be approved in any order. Minting requires text
frozen before any edge naming it is minted, and a compound minted after
its atoms.

**One hub, one live publish row, N immutable artifacts — no
unsigned/signed duplicate.** Never a second claim row. The frozen approved
string *is* duplicated into the publish row and must be: the signature
covers those exact bytes, underivable from a hub that has since drifted.
Working-copy vs frozen-copy is the duplication the crypto requires;
unsigned-vs-signed is required by nothing.

Sketch, one row per (hub, published identity):

    claim_ref_id      -- the referent, stable under edits
    approved_title    -- frozen string; what the signature covers
    claim_sha         -- canon.claim_sha(approved_title) at approval
    aida_uri          -- derived from approved_title
    trusty_uri        -- null until signed
    state             -- candidate|reviewed|signed|anchored|published|…
    batch_id          -- null until anchored

**Drift detection falls out free** — the point of storing `claim_sha`:
recompute from `finding.title` and compare; a mismatch means the hub
drifted from what was approved (pre-publication: re-review; post: the
supersede trigger). Enforce **at most one non-terminal publish row per
hub**; publication state derives from that row — no new tag axis, and
`meta.taproot_rejected` continues to carry rejections.

**Re-jiggering before push is free — two leak channels.** Everything
upstream of the registry POST can be rebuilt at will: reword,
re-decompose, re-derive every hash, re-sign, discard the files. Two things
escape early: **anchors already taken** (harmless — they attest a
different string — but no longer the priority date; anchor when text stops
moving, treat a superseded anchor as a dated artifact never cited) and
**the manuscript** (a submitted paper citing trusty URIs commits to them
even unpublished; keep mint-time identifiers out of prose — cite the
resolvable `precis_web` claim route, emit nanopub URIs in a
machine-readable appendix generated once at acceptance — decided
2026-08-13: prose never carries trusty URIs).

## Lifecycle, mint order, and irreversibility

**Signing re-identifies the artifact.** Order inside one nanopub: build
with a placeholder URI → sign the normalized quads → insert the signature
→ hash all four graphs → rewrite self-references to the final trusty URI.
A signature cannot be added to a finished nanopub. **Do not hand-roll
trusty-URI computation** — the normalization is finicky and a mismatch
fails silently; use the reference library.

**Reference claims by AIDA URI, not trusty URI.** AIDA URI = "supports
*this claim*, whoever asserted it" (converges across agents); trusty URI =
"the claim *as asserted in that nanopub*" (provenance-pinned). Use AIDA
for the semantic edge, optionally carrying our claim nanopub's trusty URI
as provenance. Genuinely mint-ordered: **attestations** and
**retractions/supersedes**, which reference artifacts by nature and come
last anyway.

**State machine:** `candidate` → `reviewed` → `signed` → `anchored` →
`published` → `superseded`/`retracted`; `rejected` terminal branches off
`reviewed`.

| step | reversible |
|---|---|
| extract, decompose, attach evidence | yes, local |
| human review / attestation | yes, re-reviewable |
| mint + sign locally | yes — delete the file |
| OTS anchor | irreversible, **but discloses nothing** |
| publish to registry | **irreversible — the only true point of no return** |

An anchor over unpublished content is a *commitment*, not a disclosure —
so **anchor early, publish late**: the priority date costs no public
commitment.

**Two-phase publish.** Mint, sign and anchor **at submission** (private —
the anchor carries the priority date); publish to the registry **at
acceptance**, re-minting whatever peer review changed and citing final
URIs. Supersede churn stays local.

## Other pathways

- **Three signable objects; sign only the second.** The claim, the
  evidence edge, the prose synthesis. The edge is what review actually
  verified; claim truth is not attested, and prose authorship is already
  carried by the paper. Signing all three dilutes what a signature means.
- **Correction vs retraction vs invalidation** (`supersedes` /
  `invalidates` / `retracts`, all live in the corpus): which applies when
  the wording was wrong vs the claim wrong vs the evidence misread —
  unmade policy.
- **Upstream retraction — half already exists.** Detection is built
  (`ingest/provenance.py`: Crossref, `refs.retraction_status`,
  `retracted-by`/`corrected-by`/`concern-raised-by` links). New work is
  only the outbound half: emitting a retraction/qualification nanopub for
  published edges grounded in a now-retracted source.
- **Rejection recorded** via the existing `meta.taproot_rejected` memo so
  the canonicalizer never resurfaces a human-rejected claim.
- **Negative results as their own pathway.** `workers/hub_refine.py`
  already computes verified non-support (`meta.citation_misses`);
  publishing grounded negative checks is expressible
  (`cito:disagreesWith`) and almost nobody does it — plausibly higher
  value than the positives, computation already paid for.
- **Inbound concurrence** post-publication (another agent asserting one of
  our AIDA sentences) is detectable by content address — the registry
  mirror's delta sync is the detector; surface via the `alert` kind.

## Migration from taproot claims to nanopubs

**No bulk backfill, by design.** Publication is gated on human
attestation, and an attestation cannot be batched — being one person's
check of one passage is its entire content. The review pass already
planned for the outstanding hubs *is* the mint queue; throughput equals
human review throughput.

- **Rehearse at full scale, publish almost nothing.** Slices 1–3 are all
  reversible per the irreversibility map, so the whole pipeline can run
  over every existing hub locally and be diffed before anything is public.
  Mint broadly, publish narrowly.
- **First tranche is what a paper cites** — a few dozen claims, not the
  ~500 atoms. **Unpublished is a good terminal state**, not a backlog to
  burn down.
- **Wording frozen before minting.** The AIDA URI is text-derived, so any
  later fix — typo, hedge, unit — is a new identity requiring a supersede.
  Review must approve and store the exact string. `TAPROOT:review` cannot
  carry this: it means "editorial note, excluded from the graph"
  (`taproot/__init__.py`, `store/types.py`), and tags carry no payload
  regardless — hence the publish-row table above. Publish rows and the
  proof store are one schema family (publish row → `batch_id` → batch/leaf
  rows); design and build them together.
- **Publish after the canonicalizer settles a hub, not during.**
  Publication flips the hub's merge rules; publishing mid-reground
  converts routine local merges into public supersedes. Order: reground
  settles → decompose → review → mint.
- **Sequencing (decided 2026-08-13): implement first, migrate in a quiet
  window.** Land the design → build the slices → then design the
  migration pass over all existing claims → ship and run that migration
  as a quiet-window operation (no concurrent reground/refine churning
  hubs mid-pass).
- **Edges: withhold unverified — and the publish preflight complains.**
  Decided 2026-08-13. Only verified-by-refine or human-attested edges
  publish; the separate-artifact rule means a withheld edge publishes the
  day it verifies, as a new nanopub, so withholding costs nothing
  permanent. The checking step (runnable standalone, always run at
  publish) enumerates every withheld edge on a to-be-published claim;
  making it silent requires fixing the cite tree — verify the edge, or
  sign it off *literally*: a human attestation, which is exactly what
  makes it publishable. There is no mute button, so unverified evidence
  can neither slip out nor be silently ignored.

## Web view — the review-and-sign surface

The review walk gets a `precis_web` surface; the invariants above do the
integrity work, the UI only surfaces the state machine.

- **Per-hub mermaid graph, not one global graph.** The hub's
  neighborhood: compound, its `conjunct-of` atoms, evidence edges,
  supersede chain — nodes coloured by publish state (candidate → published,
  plus a **drifted** marker). Extends the existing `/claim/<head>` route;
  ~500 atoms in one diagram is unreadable and mermaid won't lay it out.
- **Side panel = the publish row, rendered.** The approved string
  (editable while unsigned), state, live `claim_sha` match against
  `finding.title`, batch/anchor status, and one action button whose label
  is the current state's transition.
- **"See all the things" is a table, not a graph** — every hub by publish
  state, drifted rows flagged, proofs pending past threshold. Derived
  entirely from publish rows + the drift recompute; no new state.
- **Editing maintains integrity automatically, per state.**
  Pre-approval: edit freely — `claim_sha` staleness re-runs embed/refine.
  Post-approval, pre-signing: an edit flips the row back to `candidate`
  (drift detection is the mechanism, the UI just shows it).
  Post-signing, pre-publication: an edit discards the local artifact and
  re-mints — reversible per the irreversibility map.
  Post-publication: the edit path *is* the supersede flow; a silent edit
  does not exist.
- **The sign button signs for real.** The attesting key is in the secret
  store (Key custody), so the button works wherever the UI runs. The
  guard is invocation, not location: only the interactive sign route may
  read the attesting key, never worker/job code. Browser-side WebCrypto
  stays deferred — it adds a what-you-see-is-what-you-sign problem for no
  gain here.

## Slices

1. `get(kind='finding', view='nanopub')` — unsigned TriG for a reviewed
   hub and its stored edges. Pure read, no keys, no network; doubles as a
   draft-export format.
2. Local mint + sign: keypair, canonicalised AIDA URI, trusty-URI hashing,
   publish-state rows. Still no network.
3. OTS: Merkle batching, stamp, pending→upgrade sweep with stuck-pending
   alert, append-only proof store (raw bytes + leaf table + construction
   rule) with a periodic recompute audit. All greenfield.
4. Review-and-sign web surface (per-hub mermaid + publish-row side panel +
   queue table) + publish preflight (complains per withheld edge until
   verified or signed off) + serve TriG from `precis_web`.
5. POST to the public registry; optionally an introduction nanopub.

Independent of 1–5: **registry mirror sync** — pull-all plus periodic
delta via `safe_fetch`, retracted/superseded flagging at index time, raw
TriG + extracted index. No coupling to the publish path; can land anytime.

## Open

1. Patent grounding: the ecosystem is DOI-centric; likely a local scheme.

Resolved 2026-08-13 — **bot key identity = a precis identity URI on our
domain, no ORCID.** ORCID does not issue to software, and reusing the
operator's ORCID would blur the human/bot boundary the allowlist exists
to draw. The two-key structure itself was already decided (Key custody).

Resolved 2026-08-13: proof store = one table, exact bytes + signature +
indexed extracts (see proof store); unverified edges = withhold, with the
publish preflight complaining until verified or literally signed off (see
migration); trusty URI as cite handle = no, addable later if desired.

Resolved 2026-08-13 — **provenance content = DOI + quote + search snip.**
The full *relevant* quote (sentence-scale, fair-use quotation — never
passages) plus a short normalized snip that locates the passage
unambiguously in any copy of the PDF or its OCR — the chunk-navigation
anchoring trick reused: casefold, collapse whitespace, strip soft
hyphens/ligatures, and **validate unique-within-paper against the stored
chunk text at mint time** (no unique match → mint fails). The quote is
covered by freeze-at-review: shown beside the source at sign time, part
of what the signature attests, so a garbled quote is a supersede; a wrong
snip is only a locator inconvenience, never an integrity issue.

Plus two policy opens named inline: revocation (trust-gate section) and
correction-vs-retraction-vs-invalidation (other pathways).

## Proposed simplifications (2026-08-13 review — awaiting call)

Scope cuts that leave the verifiability core (freeze-at-review, sign,
anchor, immutable proof store, two-phase publish) untouched:

1. ~~One anchor point, not two~~ **Accepted 2026-08-13** — anchor only
   signed artifacts; the artifact contains the text, so its anchor bounds
   the text's existence (see OpenTimestamps).
2. ~~Daily anchor sweep only~~ **Accepted 2026-08-13** — one cron,
   granularity caps at 24h (see OpenTimestamps).
3. ~~v1 signs with one personal key, no bot key.~~ **Declined 2026-08-13**
   — the bot gets a key too (it costs nothing) but is never sufficient by
   itself: non-attesting, publication always requires the human key.
4. ~~Defer the inbound-gate machinery~~ **Accepted 2026-08-13** — validity
   windows, the DER/malformed-key gauntlet, and
   allowlist-as-published-artifact wait until anything inbound exists.
   Kept: pin-keys-not-ORCIDs as the recorded principle, the 2048/4096
   floor for our own keys, invocation-guarded custody.
5. ~~Proof-store home~~ **Accepted 2026-08-13** — table; exact bytes +
   signature + indexed extracts, non-DRY by design.
6. ~~Unverified machine edges~~ **Accepted 2026-08-13** — withhold, plus
   the publish preflight that complains until each edge is verified or
   literally signed off.
7. ~~Trusty URI as cite handle~~ **Accepted 2026-08-13** — no; addable
   later if desired.
