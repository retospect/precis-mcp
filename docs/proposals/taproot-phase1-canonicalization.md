---
status: done
title: Taproot Phase 1 — flat claim canonicalization (the gate)
model: sonnet
---

# Taproot Phase 1 — flat claim canonicalization

Build ticket for Phase 1 of `docs/proposals/taproot.md` (read that for the
*why*; this is the *how*). Deliverable: a **pure, offline-validated claim
canonicalizer** that decides, for a claim, whether it is the **same** as an
existing claim (merge), **different** (new hub), or a **contradiction** —
and passes the fixture at **over-merge ~0**. No live persistence, no
ingest wiring, no hierarchy — those are later phases.

## Goal / acceptance in one line

`dedup_judge` run over the 238 fixture pairs in `tests/fixtures/taproot/`
scores **zero over-merges** (bar = 0, see Decisions), every pair gets a
verdict, and the four functions have unit tests.

## The four functions (`src/precis/taproot/canon.py`, new package)

```python
@dataclass(frozen=True)
class CanonicalClaim:
    sentence: str                       # normalized claim sentence
    scope: dict[str, str]               # {material?, method?, quantity?, regime?} — light, may be {}

@dataclass(frozen=True)
class Candidate:
    hub_ref_id: int
    claim: str
    distance: float                     # cosine dist from the query claim

def extract_claim(chunk_text: str) -> CanonicalClaim | None:
    # SMALL/local LLM. Returns None when the chunk asserts no groundable
    # claim (pure-pointer / meta) -> the NO-CLAIM outcome. v1 returns the
    # single dominant claim; atomic-split of X∧Y is DEFERRED (a bundled
    # chunk simply under-merges later, which the metric tolerates).

def block(claim: CanonicalClaim, k: int = 10) -> list[Candidate]:
    # No model. Embed `claim.sentence` (bge-m3, same embedder as the card
    # index) and ANN-retrieve the k nearest existing FROLE:claim hubs.
    # Empty -> brand-new claim.

def dedup_judge(a: str, b: str) -> Verdict:            # Verdict = {verdict, confidence, rationale}
    # MEDIUM LLM. verdict ∈ {"same","different","contradicts"}. THE crux —
    # this is what the fixture grades. Merge only on genuinely same fact +
    # same conditions; any real difference -> "different"; opposite polarity
    # at the same scope -> "contradicts". Bias hard toward "different".

def place(claim: CanonicalClaim, judged: list[tuple[Candidate, Verdict]]) -> Placement:
    # Deterministic. If any confirmed "same" -> attach to that hub.
    # elif any "contradicts" -> new hub + a `contradicts` link to it.
    # else -> new hub. A "same" from dedup_judge with confidence below
    # threshold is re-checked by a BIG merge-confirm call before attaching.
```

Routing (per taproot.md's tier table): `extract_claim` **SMALL/local** ·
`block` **no model** · `dedup_judge` **MEDIUM** · merge-confirm **BIG**,
only on a low-confidence `same`. Use `precis.utils.llm.router.dispatch`
with `source="taproot:extract" / "taproot:dedup" / "taproot:merge-confirm"`.

## What Phase 1 is validated against — the dedup-judge, specifically

The fixture is **pairs** `(claim_a, claim_b, relation)`. The eval harness
runs `dedup_judge(claim_a, claim_b)` for every pair and compares to the
**collapsed** label:

| fixture label | expected verdict |
|---|---|
| `equivalent` | `same` |
| `broader` / `narrower` / `orthogonal` | `different` |
| `contradicts` | `contradicts` |

- **over-merge** = predicted `same` where expected `different` (**the
  dangerous error — drive to ~0**).
- **under-merge** = predicted `different` where expected `same` (tolerated).
- Report the full 3×3 confusion + over/under rates.

`block` (ANN recall), `extract_claim` (produces a claim / returns None on
pure-pointer), and `place` (deterministic branching) get ordinary unit
tests — they are not the risk. **The dedup-judge over the fixture is the
gate.**

## Prompts (skeletons; tune during build)

**dedup-judge** (MEDIUM): "Here are two scientific claims. Are they the
SAME claim (same fact, same conditions — material/method/quantity/regime —
differing only in wording), a CONTRADICTION (same scope, opposite
conclusion), or DIFFERENT? Default to DIFFERENT unless clearly the same.
Return {verdict, confidence, one-line rationale}." Feed the labeling rubric
that produced the fixture (`tests/fixtures/taproot/` provenance) so judge
and key share definitions.

**extract** (SMALL): "Does this passage assert a specific, citable
scientific claim? If yes, return the claim as one normalized sentence plus
any of {material, method, quantity, regime} it names. If it only points to
other work without asserting anything, return none."

## Eval harness (`src/precis/taproot/eval_canon.py` + a test)

`eval_canonicalization(fixture_path) -> Report` loads `claim_pairs.jsonl`,
runs `dedup_judge` per pair, maps labels as above, prints
over/under/confusion, and asserts **zero over-merges** (bar = 0). Wire it as a
test that is **gated/skipped without an LLM** (mark it, like the repo's
other live-model tests) so it doesn't run in the offline gate — it's a
validation harness the builder runs deliberately, not a CI unit test.

## Explicitly NOT in Phase 1

- No live persistence / ingest wiring (Phase 3), no `finding` writes.
- No broader/narrower hierarchy (v2), no atomic-split, no scope beyond the
  light note.
- No integrity axis (Phase 4), no `chase` changes.
- No `FROLE` classifier build (that's the Phase-2 predecessor task) — for
  Phase-1 offline eval the fixture claims stand in for hubs.

## Decisions (locked 2026-07-28)

- **Over-merge bar = 0.** No fixture pair that should be `different` may be
  predicted `same`. Any over-merge is investigated individually, not
  averaged into a tolerated rate (an over-merge is the dangerous error).
  Under-merges are counted but tolerated.
- **Package = `src/precis/taproot/`** (not `quest/`).
- **`FROLE` is a distinct concern** (finding = claim vs editorial note), a
  standalone finding-classifier taproot *depends on* — not part of the
  canonicalizer. Not needed for offline eval; needed before live hub
  selection (Phase 2 predecessor).

## Ready to build

All pre-build items are closed:
- **Synthetic-pair spot-check** ✅ — 30 pairs human-signed-off 2026-07-28
  (`human_approved`). One borderline corpus contradiction (pair 201) stays
  flagged, non-blocking.
- **Embedder** ✅ — `bge-m3` (the fixture was built on the card index's
  bge-m3 embeddings; `block` reuses the same).

No blocking design decisions remain. Buildable against the fixture at
**over-merge = 0**.

## Built & validated (2026-07-29)

Built (`src/precis/taproot/{canon,eval_canon}.py`) and the live gate run
against all 238 fixture pairs: **over-merge = 0 / 238 — bar met.**
under-merge 51/238 (21.4%), tolerated (the safe direction). Contradictions
22/23. The eval harness (`eval_canon.py`) streams one flushed line per pair
to stderr so the ~40-min live run is observable; run it with
`uv run python -m precis.taproot.eval_canon` (host-native, uses the
authenticated `claude` transport — the dev container's CLI is unauthed and
would silently degrade every judgment to `different`).

One prompt tune was needed to reach the bar: the raw judge over-merged
**pair 113** (a qualitative principle `Eg ∝ 1/d` vs its specific
tight-binding formula — a genus-species pair the two fixture labelers
themselves split on, `opus: broader` / `fable: equivalent`). `_DEDUP_PROMPT`
now carves out quantitative elaboration: a specific formula, value, or named
mechanism one claim asserts and the other does not is **narrower →
different**, not "the same fact in more detail." Recovering the
equivalent-recall this conservatism costs (correct merges 19→10 of 63) is a
Phase-2+ quality lever, better tuned against production base rates than this
deliberately-adversarial fixture — not a Phase-1 gate concern.
