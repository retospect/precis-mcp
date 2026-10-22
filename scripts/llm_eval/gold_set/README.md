# Gold sets

- `corpus_v1.json`, `full.json` — llm-eval capability corpus (`run_matrix.py`).
- `local/` — gitignored; model-built sets that carry sampled prod text
  (`build_*_gold.py` output). Never commit.
- `retrieval_qu164903.jsonl` — kg-mesh-loop slice 0
  (`docs/backlog/kg-mesh-loop.md`): retrieval gold for quest qu164903,
  the Pd-hydride / Pd(111) HER cluster (hubs fi504759–fi504782, pilot
  ledger me504790). Hand-written from the cited findings and chunks;
  questions and answers are paraphrased, no verbatim chunk text, so it is
  committed.

## `retrieval_qu164903.jsonl` format

JSONL in the `precis.mesh_eval.retrieval.load_gold` shape: a `_meta`
header, then one record per line with `{question, quest, gold_chunks,
labelled_by, at}`. The loader ignores the extra fields:

- `id` — stable `pdh-NN`.
- `type` — `lookup` (one hub), `cross-paper` (two papers agree or
  combine), `stance` (the answer needs a disputes/qualifies edge),
  `grounding` (the answer depends on the right chunk of a paper).
- `status` — `answered`, or `open` when the cluster records a
  disagreement or an untested hypothesis instead of a settled answer.
- `answer` — the expected answer, grounded in `evidence`.
- `evidence` — finding/paper/chunk/memory handles that support it;
  `gold_chunks` is a subset.

`labelled_by` stays `model:<id>` until Reto reviews it, then becomes
`reto`. Run: `precis mesh retrieval --prod --gold
scripts/llm_eval/gold_set/retrieval_qu164903.jsonl`. Form check:
`tests/test_kg_mesh_gold.py`.
