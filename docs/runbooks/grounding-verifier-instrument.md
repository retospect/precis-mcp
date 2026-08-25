# Grounding-verifier instrument (v3, frozen 2026-08-25)

The verifier prompt used by the dr42995 grounding audit lived only in
session transcripts and is lost; this file is its reconstruction from
`docs/backlog/grounding-verification-rubric.md` and the audit's rubric
corrections, frozen so that reliability measurements (test-retest,
cross-rung) run the *same instrument* every time. **Do not edit wording
casually** — any change makes new runs incomparable with old ones. Revise
by copying to a new dated version and noting the change.

## Task

You are given a roster of **hub-edge pairs**. Each row is one evidence
edge: a claim hub (`finding`, handle `fi<hub>`) whose sentence is asserted
to be supported by one anchored passage (`chunk`, handle `pc<chunk>`) of
one source paper (ref id `src`, search scope `pa<src>`). Score **each edge
independently** — a hub appearing twice gets two independent verdicts.

Verdict strictly on the anchored passage first; then check the wider paper.
Both facts are recorded separately because they drive different repairs:
the passage verdict drives edge repair, the paper verdict protects the
claim.

## Per-edge protocol

1. Read the hub: `get(kind='finding', id='fi<hub>')`. The claim under test
   is the title sentence.
2. Read the anchored passage: `get(id='pc<chunk>')`.
3. Score the **passage verdict** (vocabulary below) on the anchored chunk
   alone.
4. If the passage verdict would be anything below SUPPORTED, you MUST
   check further before recording it:
   - Read neighbouring chunks of the same source (the anchored chunk's
     `ord ± 1` at minimum). A chunk boundary is not a grounding defect: if
     the anchored chunk is on-topic and the confirming detail sits in
     *another chunk of the same source*, the verdict is ADJACENT_CHUNK
     (benign), regardless of distance. Topicality, not distance, is the
     signal.
   - Run a bounded keyword probe over the whole source
     (`search(kind='paper', scope='pa<src>', q='…', mode='lexical')`) for
     the claim's key entities and quantities.
5. Probe discipline:
   - **Word-boundary the probes.** Substring hits produce false zeros and
     false positives (`amino` inside *aminoterephthalate*, `2 V` inside
     *0.2 V*). Before recording that a term has zero hits, re-probe with
     boundaries/variants; before recording a hit, read it in context.
   - ASCII queries are fine (`kOhm` finds `kΩ`), but exact quantities are
     best probed with `mode='lexical'`.
   - **Check ingestion completeness before reading silence as absence.**
     If the source's chunks are only supplementary material, front matter,
     or otherwise clearly not the full text, absence of support is
     UNDECIDED, not counter-evidence.
6. Score the **paper verdict**: does the source *anywhere* (excluding
   hearsay sections — references, related work, background citing other
   groups' results) support the claim?
7. Record the **disposition** (vocabulary below) implied by the two
   verdicts.
8. Budget: aim for ≤6 tool calls per edge; spend more only when a verdict
   would otherwise be a guess.

## Passage verdict (one of)

- `SUPPORTED` — the anchored chunk carries the claim's full content.
- `ADJACENT_CHUNK` — the anchored chunk is on-topic but the confirming
  detail is in another chunk of the same source. Benign.
- `PARTIAL_MINOR` — a qualifier is dropped; meaning survives intact.
- `PARTIAL_MATERIAL` — the sentence asserts more structure than the source
  carries: a superlative, cause, comparison, priority claim, unit upgrade,
  or extra subject.
- `PARTIAL_FABRICATED` — an element of the sentence has zero support
  anywhere in the source.
- `MISATTRIBUTED` — a technique, quantity, or named entity (material,
  molecule, reagent, instrument) in the sentence belongs to something else
  in the source, or to a different method than stated.
- `UNSUPPORTED` — the anchored chunk does not support the claim and
  nothing above applies.
- `UNDECIDED` — cannot be scored (source not fully ingested, passage
  unreadable).

## Paper verdict (one of)

`pass` (the source supports the claim somewhere outside hearsay sections),
`fail` (it does not), `undecided` (ingestion incomplete or unreadable).

## Disposition (one of)

- `NONE` — no repair needed (passage SUPPORTED or ADJACENT_CHUNK, claim
  fine).
- `CLAIM_DEFECT` — the sentence is wrong or overreaches; fix the sentence.
  Includes unit-magnitude errors, misattributions, comparison-device
  numbers, structure the source never asserts.
- `WRONG_CHUNK` — the paper supports the claim but the anchored passage
  does not and no same-source chunk is merely "adjacent" — the edge points
  at the wrong place. Re-ground the edge; **never edit the sentence**.
- `WRONG_SOURCE` — the paper does not contain the result at all;
  re-grounding within it cannot succeed. Re-cite.
- `NEEDS_SECOND_EDGE` — comparative claim on a one-sided source: the
  source measures the subject but not the baseline. Repair is adding an
  edge, not fixing one.
- `SCOPE_DRIFT` — the source addresses a broader/narrower subject; nothing
  false. Benign.
- `FRONT_MATTER_ANCHOR` — the edge anchors in the source's own
  introduction/background where it cites *other groups'* work (hearsay);
  the source is secondary *for this claim*. Re-anchor.
- `UNDECIDED` — cannot be scored.

## Known traps (each has produced a wrong verdict before)

- **The number belongs to the comparison device, not the subject.** A
  figure quoted near the subject may be the comparator's. Mis-scores in
  both directions.
- **A quote can verify while the reading is wrong** ("~430 kΩ for two
  bonds" read as per-bond). Verify the *reading*, not just the string.
- **Extraction scars**: LaTeX-escaped Greek (`\mu`, `\pi`) is not
  corruption; but other scars exist (`±` rendered as `(`). If a disputed
  quantity hinges on a possibly-mangled glyph, say so and score UNDECIDED
  rather than guessing.
- **Author-name overlap does not make a source right** — an edge can point
  at the right author's wrong paper.

## Output

One JSONL row per roster row, same order:

```json
{"link_id": 142632, "hub": 176620, "passage_verdict": "…", "paper_verdict": "pass|fail|undecided", "disposition": "…", "note": "<one short sentence>"}
```

`note` is one sentence, only what a repairer needs. No prose outside the
JSONL file.

## Ground rules

- **Read-only.** Use only `get` and `search` on the precis MCP. Never
  `put`/`edit`/`delete`/`tag`/`link`; no SQL writes. You are measuring,
  not repairing.
- Verdict from the source text you actually read — never from memory of
  the literature, plausibility, or the claim's own confidence.
- Do not consult repo files under `docs/backlog/` or any prior verdict
  files: independent replicates are the point of the exercise.
