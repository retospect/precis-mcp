# Nanopub registry mirror — pull-all + delta sync

Decided 2026-08-13 (spec: `claim-publication-nanopub-ots.md`, Network
reality); planned in detail 2026-08-15 (Reto asked for the import plan).
Independent of publish slices 1–5 — a **read-only sidecar**: external
nanopubs never enter taproot as evidence; the corpus has zero coverage
in our domain, so this buys infrastructure, not evidence.

## What it buys (priority order, from the spec)

1. **Mint-time concurrence detection** — converge on an existing AIDA
   URI instead of minting a near-duplicate.
2. **Definitive coverage check** — embed the claim-bearing subset
   (bge-m3), ANN against `claim_embeddings`.
3. **Real-world fixture corpus** for the parse-leniently/
   validate-strictly gate (live malformed keys, `npx:singedBy` typo'd
   predicates are exactly the test cases we need).
4. **Post-publication inbound-concurrence detection** — a delta-sync
   scan instead of polling; surface via the `alert` kind.

## Network facts (probed 2026-08-13, re-probed 2026-08-15)

- `registry.petapico.org` (registry 1.11.4): 87,256 nanopubs, 693
  agents. Endpoints: `/nanopubs.json` → flat JSON array of artifact
  codes (`"RA…"`); `/np/<code>` → the TriG (standard across mirrors);
  `/agents`, `/list` (782 accounts trust state).
- `/nanopubs.json` returned ~2k codes on an unparameterized GET against
  87k total — **paging/cursor parameters must be probed at build time**
  (registry protocol; nanopub-registry is open source, check its API
  doc; fall back to the legacy nanopub-server `?page=N` convention).
- Mirrors: `registry.knowledgepixels.com`, `registry.np.trustyuri.net`
  — same protocol; retry a failed fetch against the next mirror.

## Design

**One table, `nanopub_mirror`** (new migration):

- `artifact_code TEXT PRIMARY KEY` (`RA…`), `trig_bytes BYTEA`,
  `byte_sha256` generated column (same pattern as `nanopub_artifacts`),
  `source_url`, `fetched_at`.
- Extracted index columns (rebuildable from bytes, audited the same
  way): `aida_uri`, `signer`, `key_fingerprint`, `dois JSONB`,
  `assertion_predicates JSONB`.
- **Frozen-ness**: an external nanopub is immutable by construction —
  its name IS its content hash. At index time recompute the trusty hash
  over the fetched bytes; store `verified BOOLEAN` (hash matches code)
  — a mismatch is a corrupt/hostile mirror response, kept but flagged,
  never indexed as valid. No append-only trigger: this is a cache of
  *other people's* frozen artifacts, not our proof store; re-fetch may
  overwrite an unverified row.
- **Flags, not exclusions** (spec): `retracted_by TEXT`,
  `superseded_by TEXT` — set at index time by scanning incoming
  `npx:retracts` / `npx:supersedes` triples across the mirror (a
  retraction can arrive *after* its target, so flagging is a second
  pass over new arrivals, not an import filter). Surface non-retracted
  by default.

**Sync worker** (`workers/nanopub_mirror.py`, scheduler cadence like
`ots_sweep`):

1. **Pull-all (first run)**: page through the code list, diff against
   `nanopub_mirror` PKs, fetch missing codes via `safe_fetch`
   (`safe_get` — outbound HTTP convention even though URLs are
   registry-constant), ~87k × a few KB ≈ a few hundred MB. Rate-limit
   politely; resumable by construction (PK diff is the cursor).
2. **Delta (steady state)**: same diff, daily; new codes only.
3. **Index pass**: parse leniently (rdflib TriG; tolerate typo'd
   predicates), validate strictly (trusty recompute → `verified`),
   extract index columns, then the retraction/supersede flag scan.
4. **Concurrence scan**: new arrivals whose `aida_uri` matches one of
   ours (exact canonical-encoding match — both `%20` and `+` live in
   the wild, canonicalise before comparing) → `alert` row.
5. Gated dark like OTS: `PRECIS_MIRROR_ENABLED`, default off.

**Non-goals**: SPARQL, serving the mirror, trust computation over
mirrored agents (allowlist stays flat and hand-curated).

## Slices

1. Migration + store ops + fetch/verify/index of a single code (tested
   against the `docs/reference/nanopub-example/*.trig` fixtures — no
   network in tests, injectable fetch like `ots.stamp_batch`).
2. Paged pull-all + delta worker + cadence + `precis nanopub mirror
   sync --live` manual door (probe the paging parameters here).
3. Flag scan (retracts/supersedes) + concurrence alert.
