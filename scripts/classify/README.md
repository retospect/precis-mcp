# precis-mcp / scripts / classify

Tools for the paper auto-tagging system. See
`src/precis/data/skills/precis-paper-tag-axes.md` for the taxonomy
and `src/precis/data/axes/*.yaml` for axis definitions.

## Workflow

```
1. cluster-papers          → cluster all papers by embedding; dump CSV
                             for taxonomy validation
2. sample-gold             → stratified-sample 30 papers (by cluster +
                             journal) into gold_set.yaml for hand-labeling
3. (you label by hand)     → fill in the values column for each axis
4. eval-classifier         → run classifier on gold set; show
                             per-axis accuracy + confusion (read-only)
5. classify-papers         → bulk run; writes tags + meta.processing
```

Steps 1–3 are read-only. Steps 4 calls the LLM but does not write
to the DB. Step 5 writes — versioned + idempotent.

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

(Stub; built next.) Reads `gold_set.yaml`, runs the LLM classifier,
prints per-axis accuracy and confusion. Read-only.

## classify-papers

(Stub; built last, after eval passes ≥ 85% on every axis.) The
production runner. Walks `paper` refs, applies axis gates, calls
the LLM, writes `meta.processing.<axis>` + open tag
`<axis>:<value>` per ref. Idempotent: skips axes already at current
version.

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
