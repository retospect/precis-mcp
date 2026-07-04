# precis-mcp / scripts / classify

Tools for the paper auto-tagging system. See
`src/precis/data/skills/precis-paper-tag-axes.md` for the taxonomy
and `src/precis/data/axes/*.yaml` for axis definitions.

Two families of axes, two samplers:

- **ref axes** (`domain/scale/dim/…`, one label per *paper*) →
  `cluster-papers` + `sample-gold` → `gold_set.yaml`.
- **chunk axes** (`role`, `open-question`, one label per *chunk*;
  ADR 0047) → `sample-gold-chunks` → `gold_set_chunks.yaml`.

## Running against the real corpus (cluster)

The samplers point at whatever `PRECIS_DATABASE_URL` names. For a
real-corpus gold set, run them on a cluster node against `precis_prod`
(that node has the bge-m3 embedder `cluster-papers` needs and, later,
the `~/.claude` state the eval harness needs). The DB password comes
from the node's `.pgpass` — use a passwordless DSN:

```sh
ssh <node> 'PRECIS_DATABASE_URL="postgresql://agent_rw@<pgbouncer>:6432/precis_prod" \
  /opt/precis/venv/bin/python .../classify/_sample_gold_chunks.py --n 200 --output …'
```

## Workflow

```
1. cluster-papers          → cluster all papers by embedding; dump CSV
                             for taxonomy validation                (ref axes)
2. sample-gold             → stratified-sample papers by cluster +
                             journal into gold_set.yaml             (ref axes)
2b. sample-gold-chunks     → weak-role-stratified sample of body
                             chunks into gold_set_chunks.yaml       (chunk axes)
3. (label)                 → fill each `?` (by hand, or LLM pre-label
                             + human adjudication of contested rows)
4. eval-classifier         → run classifier on gold set; show
                             per-axis accuracy + confusion (read-only)
5. classify-papers         → bulk run; writes tags + meta.processing
```

Steps 1–3 are read-only. Step 4 calls the LLM but does not write
to the DB. Step 5 writes — versioned + idempotent.

## sample-gold-chunks

Stratified sample of paper *body chunks* for the chunk-level axes
(`role`, `open-question`). The rhetorical `role` axis is
section-driven, so the draw is stratified by a **weak-label bucket**
(section_path + text regex — future-work / limitation / method /
result / interp / motiv-related / data / boilerplate / other) so the
rare roles and both open-question values are represented, capped per
paper, each row enriched with slug / title / position / section_path /
neighbor gists / ref-tags. Addresses chunks by `ref_id`+`ord` (stable
across re-chunking). See `gold_set/README.md` for the labeling loop.

## cluster-papers

Cluster all paper refs by their average bge-m3 block embedding.
Writes `clusters.csv` with one row per paper:

```csv
slug,cluster,journal,year,n_blocks,title
abazari2024design,3,Inorg Chem,2024,42,"Design of MOF..."
```

Use the cluster ids to:

- spot-check that papers in the same cluster are similar (taxonomy
  sanity)
- find missing axes (a tight cluster the taxonomy can't distinguish
  is a missing axis)
- stratify the gold-set sample so all clusters are represented

```sh
./scripts/classify/cluster-papers              # default k=30
./scripts/classify/cluster-papers --k 20
./scripts/classify/cluster-papers --output /tmp/clusters.csv
./scripts/classify/cluster-papers --top-journals 200   # also dump top-N
                                                       # journals (helps
                                                       # build journal_domains.yaml)
```

## sample-gold

Pick 30 papers stratified by cluster + journal and emit a markdown
form for hand-labeling. Default output:
`gold_set/gold_set.yaml`.

```sh
./scripts/classify/sample-gold                 # default 30 papers
./scripts/classify/sample-gold --n 50
./scripts/classify/sample-gold --clusters clusters.csv
```

The form has one entry per paper with one row per axis, pre-filled
with the value `?`. You replace each `?` with the correct value
from the axis vocabulary. Save the file.

## eval-classifier

Reads a gold set (`gold_set.yaml` or `gold_set_chunks.yaml`), runs the
LLM classifier, prints per-axis accuracy and confusion. Read-only.
`_eval_junk.py` / `_eval_role3.py` are the derived-gold variants (binary
junk; 3-way role collapse). **Grading honours accepted
alternatives:** a prediction counts correct if it equals the primary
label OR appears in that axis's `accept` / `role_accept` list (see
`gold_set/README.md`). Report both strict (primary-only) and
accept-aware accuracy.

## classify (chunk axes — built) / classify-papers (ref axes — pending)

The **chunk** cascade is built: `classify --cascade` (below). The
**ref-axis** production runner (walk `paper` refs, apply `applies_when`
gates, write `ROLE-per-axis` ref tags + `meta.processing.<axis>`) is not
yet built — the ref axes are eval'd (`material`/`transport` pass on the
free model; see `EVAL_RESULTS.md`) but only `material`/`transport` are
worth shipping as hard filters today.

## Layout

```
scripts/classify/
  README.md                         # this file
  _common.py                        # shared helpers (re-uses ../scripts/_common.py)
  _cluster_papers.py                # clusterer impl
  _sample_gold.py                   # gold-set sampler impl
  cluster-papers                    # bash wrapper
  sample-gold                       # bash wrapper
  gold_set/
    README.md                       # how to hand-label
    gold_set.yaml                   # generated; you fill values
```

## Production: the cascade (`classify`)

The chunk axes ship as a **cascade** — junk-gate → `role3` (own/background/
furniture) → optional escalate. Full design + eval:
`docs/design/chunk-classifier-cascade.md` and `EVAL_RESULTS.md`.

```sh
./scripts/classify/classify --cascade --limit 200            # DRY-RUN (default)
./scripts/classify/classify --cascade --limit 2000 --commit  # write ROLE3 tags
./scripts/classify/classify --cascade --commit \
    --escalate-model claude-haiku-4-5                        # + Tier 2 on 'own'
./scripts/classify/classify --axis role3 --limit 50          # single-axis debug
```

Runs against `PRECIS_DATABASE_URL` with `PRECIS_SUMMARIZE_MODEL=summarizer`
on a node with the litellm proxy. Dry-run is read-only; `--commit` writes
`ROLE3:<value>` chunk tags (idempotent via the `chunk_claims` lease,
artifact `classify:cascade-v1`; reversible by deleting that namespace +
artifact).

**Continuous** operation is the worker pass `workers/classify.py`
(`run_classify_pass`), registered in `cli/worker.py`, **default-OFF**:

```sh
PRECIS_CLASSIFY_ENABLED=1 precis worker --profile system   # or --only classify
```
