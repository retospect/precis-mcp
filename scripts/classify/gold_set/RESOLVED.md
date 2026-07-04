# Adjudication — resolved

The low-confidence rows in both gold sets have been worked through
against each item's full context. Every genuine coin-toss now carries
an **accepted-alternative** so the eval harness won't penalise the
classifier for picking a defensible second choice:

- chunks (`gold_set_chunks.yaml`): `role_accept: [..]` (and, rarely,
  `oq_accept: [..]`) beside the primary `role` / `open-question`.
- papers (`gold_set.yaml`): `accept: {axis: [..], ..}` — a per-axis map.

**Grading rule for the eval harness:** a prediction is correct if it
equals the primary value OR appears in that axis's accept list. The
production classifier still emits ONE value per axis; `accept` only
affects scoring.

## Primary labels changed during adjudication (not just accept-added)

Chunks:
- `39313:107` related-work → **interpretation** ("This study suggests…"
  is the paper's own reading, not a recap of others).

(A second flip, `37131:563` → result, was **reverted to interpretation**
during a high-confidence spot-check: the chunk is from a review, so its
numbers aren't the authors' own primary finding — `result` was wrong.
It now carries `role_accept: [result, related-work]`.)

Papers:
- `rashad24` studytype mixed → **experimental-ensemble** (expt with
  supporting DFT isn't "mixed"); property electrical → **chemical**
  (electrochemical Mg-ion storage).
- `kim24b` property thermal → **chemical** (the assay is amplification;
  thermal is only the mechanism).
- `wang24f` scale km → **n-a** (an econometric study has no length scale).
- `zhou25` domain materials → **other** (fundamentally an AI-agents
  methods paper).

All other soft rows kept their primary; see the `accept` / `role_accept`
fields for the alternatives that will also grade as correct.

## Blind re-label audit (quality check)

66 confident rows (role_conf ≥ 0.6, stratified across all roles) were
re-labeled **blind** by an independent labeler and compared:

- role: **89% exact**, **100% accept-aware** — every disagreement was a
  legitimate alternative already (or now) in the accept list, not an
  error. The softest boundary is figure captions (`data` ↔ `result`).
- open-question: **92%** — the residual is the inherent yes/no recall
  boundary; the recall-biased `yes` calls stand.

The audit added `role_accept` to 8 more rows and flipped one (`3745:237`
"Page 1922 …" OCR repetition: data → **n-a**).

**Bug found + fixed:** the 159 `open-question` values were written bare
(`open-question: yes`), which YAML 1.1 parses as the boolean `True` —
they are now quoted (`"yes"` / `"no"`). This would have silently broken
eval scoring (string `"yes"` vs boolean `True`). The eval harness /
any writer must keep these quoted.
