# Classifier eval — results & model finding

Ran the LLM classifier (`eval-classifier`) over the gold sets on melchior
against the litellm proxy. Grading is **accept-aware** (correct if the
prediction equals the gold primary OR an accepted alternative); gate is
**≥ 85%**. Gold: 200 chunks (role, open-question) + 30 papers (7 ref axes).

## Model comparison (accept-aware accuracy)

| axis            | qwen/summarizer | qwen-heavy | reasoner | claude-haiku |
|-----------------|:---:|:---:|:---:|:---:|
| **role** (chunk)          | 72% | 72%¹ | 50%² | — ³ |
| **open-question** (chunk) | 81% | 81%¹ | —   | — ³ |

¹ `qwen-heavy` returned **byte-identical** predictions to `summarizer`
  (same confusion matrix at temp 0) → the alias routes to the same
  backend; not a larger model in practice.
² `reasoner` was *worse* (50%) with 63/200 format-failures — its
  reasoning consumes the token budget and truncates the JSON.
³ `claude-haiku-4-5` (proxied to Anthropic) was **HTTP 429 rate-limited**
  for the whole session (even a single serial call) — the proxy's
  Anthropic quota was saturated. Could not measure. `deepseek-v4-*` →
  401 (no key). Few-shot exemplars did **not** move qwen (72% → 72%).

### Ref axes (paper-level), qwen/summarizer, n=30 (noisy)

| axis | accept-aware | gate |
|---|:---:|---|
| transport | 97% | **PASS** |
| material  | 93% | **PASS** |
| dim       | 83% | close |
| scale     | 80% | close |
| studytype | 73% | below |
| domain    | 70% | below |
| property  | 70% | below |

## Finding

A clean split by axis *type*:

- **Lexically-cued axes pass on the free local model** — `transport`,
  `material` (and nearly `dim`, `scale`): the answer is essentially
  keyword-present in the text.
- **Semantically-nuanced axes miss** — `role` (72%), `domain` (70%,
  chem↔materials), `studytype` (73%, experimental↔synthesis), `property`
  (70%). The load-bearing failure is `role`'s **attribution test**:
  `related-work` recall is only 10/39 — the local model cannot reliably
  tell "what THIS paper did" from "what it says OTHERS did", scattering
  related-work into interpretation/result/method. This is the exact
  distinction citation-grounding depends on, so 72% is not shippable for
  `role`.

The confusion patterns mirror the human coin-tosses from adjudication
(the `accept:` set already absorbs those), so the residual really is
model capability, not gold-label noise.

## Recommendation

1. **Ship the lexical ref axes now** on the local model — `transport`,
   `material` clear the gate; `dim`/`scale` are one prompt-tweak away.
2. **The semantic axes need a stronger model.** Two paths, both your
   call (cost / scope):
   - **claude-haiku** — retry when the proxy's Anthropic quota is free
     (the task is very likely within haiku's reach). Cheap per call, but
     ~1.3M chunks × role is real money; suits a **cascade** (local model
     for the confident/lexical cases, escalate only the
     related-work/result/interpretation ambiguous ones to haiku).
   - **Teacher-student (ADR 0047's design)** — haiku labels a silver set,
     distil a linear head over the existing bge-m3 chunk vectors, sweep
     the corpus for ~free. Higher build cost, near-zero run cost.

## What's built and proven

- `eval-classifier` — strict + accept-aware scoring + confusion, both
  gold families, any proxy model (`--model`).
- `_llm.py` — every model via the litellm HTTP proxy (local qwen* +
  proxied claude/deepseek/gemini); `cli:` prefix for the `claude -p`
  path.
- `classify` — the production chunk-axis pass (`chunk_claims` lease,
  `Tag.closed("ROLE",v)` chunk tags). **Dry-run-proven against prod**
  (claim → classify → label distribution, no writes). A `--commit` run is
  deliberately gated on a passing model + a human go — not run here, so
  the corpus stays clean.

## Cascade validation (junk-filter first)

Most of the corpus is furniture; the cheap model is far better at
"junk vs substance" than at the 11-way `role`, so filter first and only
spend the strong model on what's left.

**Tier 0 — free regex/heuristics on prod (1.29M body chunks):**

| bucket | chunks | % |
|---|---:|---:|
| furniture (references/copyright/ORCID/Elsevier regex) | 266,371 | 20.7% |
| tiny (<120 chars) | 36,482 | 2.8% |
| table/numeric (numeric_ratio>0.35) | 673 | 0.1% |
| substantive? (reaches the LLM) | 983,682 | 76.4% |

~24% removed before any token is spent. (The `numeric_ratio` table
heuristic is near-useless — tables aren't digit-dense enough; tables
fall to the LLM, which is fine.)

**Tier 1 — local binary junk detector (`junk.yaml`, summarizer, n=200):**

- accuracy 93.5%, **discard precision 93.9%**, **false-discard 1.3%**
  (2/158 substantive wrongly dropped — a method + a figure-caption).
- junk recall 74% (conservative: keeps mixed chunks). The 11 missed are
  mostly descriptive figure captions — borderline, harmless to keep.

**What this buys.** Junk-discard alone doesn't reach 10% cost — it
removes ~24% free + more via the local model. The 10–20% target comes
from *also* only escalating the **attribution-ambiguous residual** (the
result/related-work/interpretation triangle) to haiku: the local model
already nails method/future-work/interpretation/motivation (~85%), so
only ~15–25% of the corpus needs the strong model. Cost then ≈ that
fraction of the naive all-haiku spend (~$200–400 vs ~$1.3–2.6k).

Two escalation policies:
- **Safe/max-quality:** haiku on ALL substantive (~60% of corpus) — still
  ~40% cheaper, and every substantive chunk gets a strong label.
- **Frugal:** haiku only on the ambiguous triangle (~15–25%) — hits the
  ~10–20% target; the confident local labels stand for the rest.

The junk filter + the lexical ref axes (`material`, `transport`) are
shippable on the free local model **now**; haiku is reserved for the
attribution-hard residual, on your cost call.

## 3-way collapse (`role3`) — the hard axis, fixed on the free model

Collapsing the 11-way `role` to **own / background / furniture** (the only
distinction citation-grounding needs) flips it over the gate on the free
local model:

| metric | 11-way `role` | **3-way `role3`** |
|---|---|---|
| accept-aware accuracy | 72% ❌ | **88% ✅ PASS** |
| strict accuracy | 66% | 82% |
| own-claim precision  | ~78% | **91%** |

Confusion (summarizer, n=200): `background` 46/55 correct (only 8 leak to
own, vs the 11-way where related-work scattered massively into
interpretation/result/method); `furniture` 38/42; `own` 80/103.

**Why it works:** the model only has to make the *one* attribution call it
can make ("this paper's own claim vs background"), not also split
result/interpretation/method. The dedicated own-vs-background prompt cut
background→own leakage to 15%.

**Citation-safety:** own-claim **precision 91%** — when the filter marks a
chunk as this paper's own claim (a citable primary source), it's right 9
times in 10 on a *free* model. Recall 78% — it conservatively calls some
own-work "background", the safe direction (won't surface others' work as
citable). Feeding that 91%-precision candidate set to the agentic search
(which reads + verifies) recovers the last few points.

### Revised recommendation

Ship on the free local model **now**: `junk` filter (94% discard
precision), `role3` (88%, 91% own-precision), and the lexical ref axes
(`material` 93%, `transport` 97%). Keep the 11-way `role` as a *refinement*
of `own` chunks only, and reserve haiku for pushing own-precision past 91%
if a use demands it. The 3-way is the citation-grounding workhorse and it
runs for free.
