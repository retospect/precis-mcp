---
status: draft
title: Catpath reproducible bundle — one archive per finished compound (model, seeds, env, outputs, offline viewer, verify script), publishable on Zenodo
pillar: 3d-design
prio: normal
---

# Catpath reproducible bundle for Zenodo

BLUF: a finished compound exports as one `.zip` that holds the inputs,
every seed, the resolved config, the lock and pinned wheel, the weights
hashes, the outputs, an offline viewer, a provenance chain to the
finding/nanopub, and a `reproduce` + `verify` entry pair. Every file has a
SHA-256 in `MANIFEST.sha256` and in the RO-Crate metadata. Verification is
"re-run and compare within stated tolerances", not byte equality. The
Zenodo deposit is a separate step that Reto approves per deposit.
The parts now include D10 lock/weights/platform stamps and a stored-seed
envelope (table below); missing are the publication packager, complete
producer-bound provenance, the verify script and the Zenodo client.
Slice 1 needs no GPU and no outward call.

Reto, 2026-10-19: "For catpath stuff (including the Kinetics). I wish to
have a tar or zip file akin to a finding/nanopub thing. [...] When a
structure has been fully ... done, we want to share the model and what we
did with it in a reproducible package. This zip file is that. It contains
the full package (uv files, etc) that are needed to repro the file and the
viewing capability. It has a crc for each file, and all the seeds and
stuff. So, right or wrong, anyone can reproduce the specific result of this
compound. The idea is we stick the compounds on zenodo and have them
published, for anyone to nitpick over."

## Motivation / why

A claim about a catalyst is only as good as the next person's ability to
re-run it and disagree. Today the evidence lives in a prod DB (pathway
`meta`, job rows) and a git pin; an outsider cannot get from a finding to
the numbers. The nanopub pipeline (`precis.nanopub`) publishes the
*claim*; this item publishes the *computation behind it*, so a nanopub can
cite a DOI that resolves to something runnable. Reto's framing is
falsification-friendly: publish right or wrong.

## What "done" means for a compound (the trigger)

A bundle is exportable only for a result that has stopped changing.
Proposed gate (all must hold; Reto to confirm, Q1):

1. A `pathway` ref with a completed `autocatpath_aggregate` (all
   `(model, seed)` partials present, `low_confidence` known).
2. If the claim uses kinetics: a `meta.results.kinetics_v2` record and a
   `meta.validation_manifest` (kinetics-engine-v2 D10) with a `reproduced`
   verdict from at least one complete same-platform re-validation.
   D10 is implemented; `reproduced_partial` does not satisfy this gate
   (`src/precis_pathway/manifest.py::manifest_text`).
3. A finding link or `STATUS:` tag saying the result is frozen. Export from
   a mutable ref is refused with a next step (a re-mint changes the content
   address, so the bundle would be stale at once).
4. The secret gate passes on the staged tree (below).

Discovery is out of scope, as in kinetics-engine-v2 D10: the bundle
records the *validation* of a found compound, not how the search found it.

## What exists vs what is missing

| Need | Exists (anchor) | Missing |
|---|---|---|
| Engine run outputs, offline viewer | catpath `autocatpath.report::build_report` writes `index.html`, `report_assets/` with vendored 3Dmol, `manifest.json`, `methods.md`, `results.json`; `config.snapshot.yaml` via `cfg.snapshot` in the pipeline; opens over `file://`. Precis retains pathway outputs and seed job outputs plus hashed artifact pointers (`src/precis_pathway/seed_job.py::_record_artifacts`) | no retained complete report outdir/viewer inventory; seed artifacts do not supply the missing report manifest, thumbnails and viewer assets |
| Content addresses | `precis_pathway.runner::content_key`, `seed_content_key`; D10 code/lock/weights/platform record (`src/precis_pathway/manifest.py::build`); seed envelope output and bundle digests (`src/precis_pathway/bundle.py::build_envelope`) | complete execution provenance bound to each seed, wheel/image digests and the publication archive identity; a pathway D10 stamp does not attest to an earlier seed's environment |
| Seeds and job params | `search.seeds`; per-`(model, seed)` jobs `autocatpath_seed` / `autocatpath_aggregate` with a params schema (`precis_pathway.seed_job`); D10 collects search, kinetics, KMC replica and LHS seeds via engine `seeds_from_config` (`src/precis_pathway/manifest.py::build`) | one archive file collecting all params and retained aggregate membership; `src/precis_pathway/aggregate_job.py::_collect_seed_results` does not retain the selected seed job ids |
| Pinned engine | `[tool.uv.sources] autocatpath` git rev in `pyproject.toml`; `uv.lock`; host wheelhouse (`deploy/roles/autocatpath`) | the catpath repo is private, so outsiders cannot pull the rev; wheel version is reused across commits (`catpath-wheel-version-reuse.md`), so a version string does not identify a wheel |
| Manifest + re-validation | kinetics-engine-v2 D10 is implemented: `src/precis_pathway/manifest.py::build`, `src/precis_pathway/revalidate_job.py::revalidate`; code/lock mismatch refuses, CPU legs are judged against stored tolerances | archive packaging and complete validation: energy legs are not rerun by the CPU job, and missing engine commits prevent complete validation; this item consumes D10, does not duplicate it |
| Claim publication | `precis.nanopub` (mint, sign, OTS, registry); `CITATION.cff`; `fto-defensive-publication.md` Leg 1 plans a Zenodo GitHub-release snapshot of the repo | no nanopub field for a supporting-dataset DOI; no deposition client; no Zenodo deposit exists |
| Secret gate | `tests/test_deploy_tree_no_secrets.py` over the repo tree | not applied to generated archives |
| Licences | catpath and precis are GPL-3.0-or-later | licence for outputs/data undecided (Q3) |

Still missing: RO-Crate and a publication archive of a run folder.
D10 supplies weights hashes when locatable and a platform stamp;
`src/precis_pathway/bundle.py::build_envelope` supplies an internal stored-seed
envelope, not that archive. catpath's `provenance.py` is caption/methods text
and PNG tEXt only.

## First slice: stored-seed envelope (REPROENV, gr564182, td567064)

The first implemented boundary is
`src/precis_pathway/bundle.py::build_envelope`: one explicitly selected
`autocatpath_seed` job with stored `partial` outputs, its recorded pathway
id, and caller-supplied snapshots of both metadata records. It returns
detached JSON-compatible data; `src/precis_pathway/bundle.py::canonical_bytes`
serializes it. These logical paths name inline records, not files already
written into the archive layout below.

| Exact envelope inventory | Stored source / absence rule |
|---|---|
| `inputs/config.json` | `job.meta.params.config`, requested config; absent is recorded in `missing` |
| `inputs/slab.extxyz` | `job.meta.params.slab_extxyz`; absent is recorded in `missing` |
| `inputs/selection.json` | Present params among `seed`, `model_index`, `force_backend`, `only_steps`, `step_retry`, `engine_token` |
| `outputs/partial.json` | Required `job.meta.partial` object |
| `outputs/lattice.json`, `outputs/structures.json`, `outputs/bands.json`, `outputs/ts.json` | Corresponding job metadata fields when present; otherwise `missing` entries |
| `artifacts` | Every recorded `job.meta.artifacts` entry: name, relative path, producer SHA-256, byte count and observed availability; sorted by name/path/hash |
| `run` | Selected job id/type and stored `content_key`, `seed`, `model_index`, `model`; missing stamps stay explicit |
| `pathway_context` | Selected pathway id, stored content key, D10 `manifest_id` and unchanged `validation_manifest`; `execution_binding` explicitly unproven |
| `provenance` | Seed-stamped `weights_sha256` if present; code, lock, wheel, image, platform, units and effective config are null with `missing` reasons |
| Envelope controls | `schema: precis_pathway_seed_envelope`, `schema_version: 1`, `missing`, `output_id`, `bundle_id` |

Inline entries carry source, data, encoding, SHA-256 and byte count. The
seed producer (`src/precis_pathway/seed_job.py::_dispatch` / `_poll`)
records the outputs and weights stamp; `_record_artifacts` retains large
outputs via `src/precis/store/job_artifacts.py::write_artifact`. Effective
config and execution identities must be recorded by the producer when it
runs. Requested config and `engine_token` are intent, not execution proof.
The aggregate/kinetics pathway record can change after a seed finishes;
its manifest and results cannot fill that seed's missing provenance.

Export is a read-only caller process with selected snapshots and access to
existing artifact bytes, using
`src/precis/store/job_artifacts.py::read_artifact` by default (root/spool
fallback). The function performs no engine lookup, compute, DB mutation,
job discovery or publication; it adds no handler view or export job. The
catalyst-host job default below belongs to the eventual archive builder.
Missing historical pins are never reconstructed from the exporter host.
Absent artifact hashes/sizes remain unverified; missing files retain their
pins as unavailable; byte/hash mismatches and contradictory job/pathway
selection refuse. Other read errors propagate. Artifact paths must stay
in the selected job's directory.

`output_id` hashes inline outputs and external artifact pins, excluding
availability. The envelope's `bundle_id` hashes canonical JSON except its
own field, including context, missing fields and observed availability.
It is distinct from the proposed archive `bundle_id` (hash of
`MANIFEST.sha256` below), and leaves D10 identity unchanged. Callers must
retain canonical envelope bytes; rereading mutable refs cannot recover an
earlier snapshot. No exporter timestamp or current host provenance is added.

This slice emits no zip, RO-Crate, `MANIFEST.sha256`, `env/`, viewer,
reproduction scripts or Zenodo deposit. D10 is related context, not an
exported `manifest/validation_manifest.json` bound to this seed. The
compound done gate and public secret gate remain archive requirements;
this internal envelope neither enforces them nor certifies reproducibility.
The eventual archive must retain and reference this evidence with distinct
identities, add the missing producer records and membership, and package
and sanitize the actual files. The viewer-less first slice follows decision
1 below; it does not reconstruct a historical report with current code.

`tests/test_pathway_bundle.py` and synthetic
`tests/fixtures/pathway/repro_seed.json` cover stable detached bytes,
inventory ordering, missing evidence, artifact integrity and selection
refusals. They do not establish the compound archive acceptance criteria
below; the bundle layout, reproduction and Zenodo work remain open.

## Bundle layout

Zip: random access, per-entry CRC32 for free; tar adds nothing here. Root
directory `<compound-slug>-<bundle_id[:12]>/`:

```
ro-crate-metadata.json     RO-Crate 1.1 root: title, authors, licence, DOIs, hasPart for every file
MANIFEST.sha256            "<sha256>  <path>" per other file; verifiable with sha256sum -c
README.md                  what this is, the commands, tolerances, known non-determinism
reproduce.sh, verify.py    see below
env/pyproject.toml         the project slice the run needs (extras catalyst / catalyst-gpu)
env/uv.lock                full lock of that slice
env/wheels/                the autocatpath wheel built from the pinned rev; sha256 + source rev recorded
env/platform.json          arch, os, accelerator class, driver, torch/numpy/numba/blas versions
env/weights.json           {backend, model, file, sha256, source_url, licence}
inputs/                    slab extxyz, network, environment block (D1), config.snapshot.yaml, corrections, TPD reference set
seeds.json                 search_seeds, kinetics_seed, kmc_replica_seeds, lhs_seed, per-(model,seed) job params verbatim
manifest/validation_manifest.json   kinetics-engine-v2 D10 record: manifest_id, tolerances
outputs/                   the catpath outdir verbatim: results.json, kinetics.json, structures/, PNGs, methods.md
viewer/                    catpath index.html + report_assets/ (3Dmol vendored); opens via file://
provenance/                finding/nanopub ids + trusty URIs, pathway handle, job ids, precis git sha, build time (UTC)
```

Checksum: SHA-256 is the integrity record. CRC32 catches accidental
corruption only and is trivially forgeable; a reader nitpicking a published
result needs tamper evidence. The zip's per-entry CRC32 stays (free), so
"a crc per file" is also satisfied by `unzip -v`. The `bundle_id` is the
SHA-256 of `MANIFEST.sha256`: the identity that does not depend on the DOI,
and what a nanopub cites alongside it.

The archive writer must sort entries with fixed mtimes and no extra fields,
and test that rebuilding from the same inputs gives the same archive bytes.

## Reproduce and verify

- `reproduce.sh`: `uv sync --frozen` from `env/`, installs the bundled
  wheel with `--no-index --find-links env/wheels`, checks weights against
  `env/weights.json`, then runs the engine commands recorded in
  `seeds.json` in job order (`autocatpath seed --seed N` per `(model, seed)`,
  `aggregate`, `kinetics`). Refuses, naming the mismatch, if the lock or
  weights hash cannot be satisfied; never substitutes.
- `verify.py`: runs `sha256sum -c` first (a modified bundle fails before
  any compute), then compares re-run `outputs/` to shipped ones with the
  tolerances in `manifest/validation_manifest.json` (kinetics-engine-v2 D10
  table: 1 meV same platform / 10 meV cross-platform for energies, 0.01 /
  0.05 decades for TOF, and so on). Verdict `reproduced |
  drifted(<observables>) | failed(<leg>)`, written to `verify-report.json`
  naming both platforms.
- No byte equality for numerics. GPU MLIP inference (cuEquivariance
  kernels, TF32, atomics) and libm/BLAS differences move energies at the
  meV level (repo precedent: gr495146, gr495456, cited in D10). catpath
  relaxers take no RNG (`autocatpath.coverage` docstring), so the seeds
  that matter are `search.seeds` and the KMC streams.
- No LLM calls and no network except the uv index (and an optional weights
  fetch), same rule as D10 "out of scope".
- README states the honest limits: the private repo's history is not in
  the bundle, only the built wheel and source rev; a reader on a different
  accelerator class gets the cross-platform column.

## Standard: RO-Crate

Fit check: RO-Crate 1.1 is JSON-LD in `ro-crate-metadata.json`; it lists
`File` entries with `contentSize` and `sha256`, a root `license`, authors
with ORCID, `citation`/`isBasedOn` for DOIs, and the Workflow Run Crate
profile's `CreateAction` ("this script, with these inputs and parameters,
produced these outputs"). That covers inputs, seeds (as action `object`
params), environment (`SoftwareApplication` with versions) and outputs.
It does not specify numeric-tolerance verification; that stays our
`verify.py`. This is written from knowledge of the spec, not checked live;
confirm against current RO-Crate and Zenodo docs in slice 4. Nanopubs
already carry trusty URIs and signatures, so the crate records those as
`identifier` properties; the signature lives in the nanopub (over the
`bundle_id`), not on the zip.

## Zenodo publishing

To confirm at slice 4, sandbox first (`sandbox.zenodo.org`, separate
token): REST deposition API (create deposition, upload to bucket, set
metadata, publish); each record gets a version DOI and a concept DOI, new
versions hang off the concept; default limits 50 GB per record and 100
files (the bundle is one file; put `README.md` and
`ro-crate-metadata.json` beside it so the record page is readable without
downloading); metadata: title, creators with ORCID, licence, keywords,
related identifiers (`isSupplementTo` the finding/nanopub, `isDerivedFrom`
the catpath release, `references` papers), community. A published record's
files are permanent (new version only); metadata stays editable.

Outward-facing rule: a production publish is public and irreversible, so
it is never automatic. The tooling prepares a draft deposition and by
default only dry-runs it (rendered metadata, file list, sizes, secret-gate
result, licence); Reto approves each production `publish`. Sandbox
deposits need no approval. The token lives in the gitignored overlay.

Size: dominated by weights (MACE-MP tens to ~100 MB), wheels, and
`outputs/structures`. torch is in the lock, not bundled. Expect 0.1 to
1 GB. The bundler warns at 2 GB and refuses at 40 GB.

## Secrets and the public-repo gate

Bundles are published, so the archive passes the same gate as the tree: no
tailnet/LAN addresses, node hostnames, vault blobs, tokens, absolute home
paths or DB coordinates. Slice 1 factors the scanner out of
`tests/test_deploy_tree_no_secrets.py` into a callable and runs it on the
staged tree before zipping. Known leak vectors: job `meta` (`ssh_node`,
`target_node`, worker host), snapshot paths, hostname in `platform.json`,
index URLs with credentials, and the private-repo git URL in
`[tool.uv.sources]` (replaced by the wheel and a rev). A failure refuses
the export and lists offending paths.

## Slices (ranked)

1. **Bundle writer, offline.** The stored-seed envelope now exists in
   `src/precis_pathway/bundle.py`; the remaining archive writer must, from a
   pathway handle, stage the layout from what exists today (config snapshot,
   results, retained catpath outdir/viewer when available,
   job params, seeds, nanopub ids), write `MANIFEST.sha256`,
   `ro-crate-metadata.json`, deterministic zip, secret gate. A
   `view='bundle-plan'` previews; export writes to a scratch dir and never
   mutates the ref. Test: bundle of a fixture EMT pathway passes
   `sha256sum -c`; two builds are byte-identical; a planted tailnet IP fails
   the gate; the viewer HTML has no network references.
2. **Env stamps.** Lock hash, `env/` slice, `platform.json`, weights hash,
   built autocatpath wheel with sha256 + rev; identify wheels by sha, not
   version (closes `catpath-wheel-version-reuse.md`). Test: flipping one
   weights byte changes the manifest; platform is recorded but not hashed
   into `manifest_id`.
3. **`verify.py` + `reproduce.sh`.** Tolerance contract comes from
   kinetics-engine-v2 slice 10 (manifest, `autocatpath_revalidate`); an
   energy-only version can ship earlier. Test: in a clean temp venv,
   `reproduce.sh` on the EMT fixture returns `reproduced`; perturbing one
   energy past tolerance returns `drifted(energies)`; editing a shipped
   file fails `sha256sum -c` first.
4. **Zenodo client, sandbox only.** `precis/publish/zenodo.py`: draft
   deposition, upload, metadata from the RO-Crate, dry-run default,
   production `publish` behind an explicit per-deposit confirmation, token
   from the overlay. Test: with an HTTP stub, dry-run issues no publish
   call; a sandbox deposit round-trips metadata; the outbound-politeness AST
   ratchet (gr493222) passes.
5. **Link back.** Finding/nanopub gain a supporting-dataset DOI + bundle id
   (design with `claim-publication-nanopub-ots.md`); the pathway page shows
   the DOI and a download link; `CITATION.cff` template per bundle. Test: a
   minted nanopub fixture carries the bundle id in signed content; changing
   the id changes the trusty URI.
6. **First real compound.** One finished compound, built, verified on a
   second machine, sandbox-deposited, then Reto approves the production
   deposit. Test: an independent `verify.py` run on a different platform
   returns `reproduced` or a documented cross-platform `drifted`.

Slices 1 to 3 are the useful core; 1 and 2 do not wait on kinetics v2.

## Explicitly NOT in scope

- Reproducing discovery (quest search, LLM proposals).
- Bundling the corpus, the paper DB or other candidates' results.
- Bitwise-identical numerics.
- Auto-publishing; any production deposit without Reto's approval.
- A general "export any ref"; this is pathway/catpath (kinetics included).
- Re-running licensed DFT codes: recorded as inputs/outputs only, unless
  the code is open.

## Acceptance criteria

- Slice 1 to 3 tests pass in CI; a fixture bundle verifies on macOS arm64
  and Linux x86_64 under the cross-platform tolerances.
- No bundle string matches the repo secret gate.
- One real compound sits on sandbox Zenodo and a machine with only the zip
  and `uv` verifies it.

## Target + blast radius

Existing: `src/precis_pathway/bundle.py` (stored-seed envelope). Still new:
the archive writer, a Zenodo publish module, a
`view='bundle-plan'` and an export mode on the `pathway` handler
(`precis_pathway.handler`), skill text for the verbs. Engine side:
the D10 manifest is implemented; `report::rebuild` still needs a complete
retained report outdir, which the seed envelope does not supply. No
migration through slice 4 (bundles are files; the DOI goes in `meta`);
slice 5 may need one.

## Decisions (Reto 2026-10-21)

1. Slice-1 persistence: synthesise `manifest.json` from the stored artifact
   for `report::rebuild` if catpath's `rebuild` accepts it; otherwise slice 1
   ships without the viewer and viewer persistence becomes its own item.

Working defaults (the file's recommendations, carried by the 2026-10-21
sweep; not individually ruled, Reto may override):

2. Export runs as a job on a catalyst-extra host; the handler refuses with
   `next=` when `import autocatpath` fails.
3. Done gate: slice 1 enforces "aggregate complete + frozen marker" and
   writes `preliminary: true` in the crate otherwise; the kinetics gate joins
   through the implemented D10 complete-revalidation contract. Frozen marker:
   open tag `repro:frozen` on the pathway, set by Reto.
4. Weights: hash + fetch recipe, no weights in the bundle.
5. Licence: CC-BY-4.0 for outputs. Bundle the catpath wheel only if catpath
   goes public; otherwise record rev + sha and say so in the crate.
6. Checksums: SHA-256 (zip CRC32 as a free extra).
7. Identity: one `bundle_id` per (compound, engine version), all under one
   Zenodo concept DOI.
8. Include failed and low-confidence seeds and trust records.

Slices 1 and 2 are buildable once the builder writes the slice-1 file list
and the slice-2 wheel/lock/platform sources (reuse
`scripts/lib/autocatpath-wheel.sh`), and drops the "platform not hashed"
claim from the slice-2 test (`platform.json` stays in `MANIFEST.sha256`).
Still open, not blocking slices 1-2: Zenodo account (personal or org) and
community; which compound goes first.

### Ready-check (slices 1 and 2 only), 2026-10-21

- blocker: slice 1 "outdir regenerated through `report::rebuild`" cannot work from stored data: `rebuild(outdir)` reads `manifest.json` (state energies, thumbs, selection, side routes), `graph.json`, `results.json`, `structures/*.extxyz` from a catpath run folder, but `PathwayArtifact`/`pathway_meta` hold only config, snapshot, results_json, graph_json, methods_md, structures_extxyz. No `manifest.json`, thumbs, kinetics.json or PNGs are persisted, so the "What exists" row ("outputs not archived where precis can hand them out") is the gap slice 1 must close, not assume away. Decide: persist the catpath outdir at run time (new storage), or synthesise `manifest.json` from stored artifact, or ship viewer-less outputs in slice 1.
- blocker: where slice 1 runs is unspecified. `report::rebuild` needs `autocatpath` + ase/rdkit; pyproject says the web export excludes autocatpath (`catalyst` extra, host wheelhouse). Name the process/extra the export mode and `view='bundle-plan'` run in, and what happens when autocatpath is not importable (refuse with next=).
- blocker: slice 1 vs layout mismatch: layout lists env/, platform.json, weights.json, validation_manifest.json, corrections, TPD set, kmc seeds; slice 1 says "from what exists today" without naming which layout paths it writes and which are omitted/placeholder (slice 2 adds env/). RO-Crate hasPart and MANIFEST must be defined for the slice-1 subset; `bundle_id` changes when slice 2 adds files.
- blocker: done-gate (Q1) remains an archive-policy question; gate item 2 now has an implemented D10 `validation_manifest`, but CPU-only `reproduced_partial` is insufficient. The internal seed envelope does not enforce the publication gate; the archive writer must enforce the agreed frozen marker and complete-revalidation requirement.
- blocker: open Qs affecting slices 1-2 still unresolved: Q3 (outputs licence goes into crate root `license`; bundling the private catpath wheel publishes it), Q2 (weights shipped vs hash-only changes slice 2 `env/weights.json` and size), Q4 (checksum format), Q5 (bundle_id per compound vs engine version).
- blocker: slice 2 "closes `catpath-wheel-version-reuse.md`" but that spec prefers git-derived versioning in catpath and says repro-bundle slice 2 "takes the sha-assert option"; the wheel sha256+rev stamp for a wheel built where, from which checkout (catpath path, clean-tree rule), and how the rev is read from `uv.lock` is not specified. Also unspecified: where weights come from for the hash (which backend/model file paths, MACE cache location), what "lock slice" means (which pyproject extras/pins are pruned), and the `platform.json` field source for accelerator class/driver.
- blocker: slice 2 test "platform is recorded but not hashed into `manifest_id`" - `manifest_id` is a kinetics-engine-v2 D10 concept and slice 2 does not build it; the bundle's own id is `bundle_id` (hash of MANIFEST, which includes platform.json, contradicting "not hashed"). Reconcile.
- advisory: secret gate: scanner lives as private `_scan`/`_credential_findings` in the test with patterns; `precis.utils.secret_scan.find_secrets` already exists. Name the module the factored callable lands in (tests must not be imported by src) and how `secret-gate: allow` and binary/zip handling apply to staged files; hostname/absolute-home-path checks are not in the current gate and are new patterns.
- advisory: determinism test (byte-identical) conflicts with "build time (UTC)" in provenance/ and any generated viewer timestamps; say whether build time is an input or excluded.
- advisory: acceptance criteria are per-spec, not per-slice; "fixture bundle verifies on macOS arm64 and Linux x86_64" needs slice 3. Slice 1/2 criteria = the listed slice tests; fixture "EMT pathway" does not yet exist as a persisted pathway fixture (verify under tests/).
- advisory: model tier: not declared; this is new-abstraction work (bundle format, RO-Crate) - set `model: opus`.
- advisory: viewer "no network references" test must define the check (regex over http(s):// excluding xmlns/doc links?).
- note: blocked-by not declared; sibling specs exist (catpath-wheel-version-reuse, kinetics-engine-v2, claim-publication-nanopub-ots) and were checked. Split: slices 1 and 2 are separable (2 depends on 1's manifest; both fine as separate items), but 1 itself hinges on the persistence decision above.

## External replay acceptance (gr564215, 2026-11-04)

The acceptance contract (fresh external runner, staged pinned inputs, D10
judgment, negative tests, coverage table) is implemented reader-side as
`src/precis_pathway/replay.py` — the external runner's engine, consuming an
exported bundle directory with no precis checkout, store, MCP or credentials:

- integrity: `sha256sum -c` semantics over `MANIFEST.sha256` before any
  compute; undeclared files and mismatches refuse naming the path.
- staging: declared files copy into a fresh read-only-inputs work dir;
  `env/weights.json` files hash-verify (a mismatch refuses naming both
  digests, never substitutes; hash-only private-engine records stay
  `unavailable`, never silently fetched) and the staged `env/uv.lock` must
  hash-match the D10 record's pin.
- execution: the declared entrypoint runs under a socket guard (an
  undeclared access fails naming the address), index/cache-isolation env,
  immutable inputs and a new `out/`; shipped `outputs/` are never handed to
  the entrypoint, so they cannot be reused as fresh results.
- numerical agreement: fresh observables judged by the engine's D10
  `autocatpath.manifest.compare` against `manifest/validation_manifest.json`
  tolerances (same/cross-platform column by platform) — no second tolerance
  policy anywhere in the repo.
- report: `pathway_replay_report` JSON with bundle/manifest ids, image
  digest (`provenance/build.json`), code/lock/weight ids, commands, exit
  codes, logs, per-leg status, tolerances; integrity, execution, numerical
  agreement and scientific support stay distinct scopes, claim support
  always `unavailable`.
- coverage: `coverage.json` carries claim/run/required-leg/acceptance-test
  rows; legs the replay did not run stay `not-run` (named, with why) and
  block their claims — one fixture cannot read as paper-wide coverage.

`tests/test_pathway_replay.py` covers all of it engine-free on a synthetic
CPU fixture (the always-run packaging check) and replays the pw455722 case
under the real D10 judge where the `catalyst` extra is installed. Legs the
replay did not produce (GPU/HPC energies, sweep, TPD) stay `not-run`.

Recorded blockers, not weakened: the catpath repo is private, so a
hash-only engine/weights record fails the full external-reproduction
acceptance (inspection yes, independent replay no) until an
outsider-accessible engine/weights distribution exists; and the container
build leg of contract point 1 (pinned base-image build from the archive
alone, empty caches, `--network none`) still needs the archive writer
(slice 1) to produce a real bundle — this harness consumes it and is tested
against synthetic fixtures until then. This item does not authorize
publishing private code or weights.
