---
id: precis-read-paper
family: paper
title: precis — read one paper and wire it into the mesh
summary: the paper-driven reading pass — a setup note the results chunks point at, claim hubs scoped by that setup, measures for their numbers, targeted edits to the memories that care, then tags, citations and a closing paper note; idempotent on re-run
answers:
  - read this paper and add it to the knowledge graph properly
  - how do I record a paper's methods setup (DFT functional, code, conditions) once and point its results at it?
  - which memories should a new paper update, and how?
  - when does reading a paper earn a new memory instead of an edit?
  - how do I avoid re-reading a paper someone already processed?
  - should reading a paper also extract its numbers as measures?
applies-to: get/search (kind='paper'|'memory'|'finding'|'taxon'), put/edit/link/tag (kind='memory'|'finding'|'paper')
status: active
tags: [workflow, drafting]
kinds: [paper, memory, finding, taxon]
---

# precis-read-paper — read one paper, leave structure behind

You have one paper and no single question. The output is not a summary:
it is structure that makes the paper's content findable and usable from
elsewhere. Four things, in order:

1. a **setup note** — the general conditions every result rests on
   ("DFT, PBE+U, U=3.5 eV on Fe, VASP/PAW, 520 eV") — that the
   dependent results chunks point at;
2. **claim hubs** for the key results, scoped by that setup, and a
   **measure** for each number they state;
3. **edits to existing memories** that are about what the paper found,
   and a new memory only where no node can hold the fact;
4. **mesh extras**: facet tags, cited primaries, disputes, and one
   closing paper note that records the pass.

Reading *for a question* across several papers is
`get(kind='skill', id='precis-read-for-question')`; it shares this
skill's hub door. Use this one when the paper itself is the job.

## Step 0 — was it already read?
## Make a re-run add nothing

```python
get(kind="paper", id="pa<id>", view="comments")  # a prior "read-paper:" note?
get(kind="paper", id="pa<id>", view="claims")    # hubs already grounded on it
get(kind="paper", id="pa<id>", view="links")     # setup memories, cites, disputes
```

A prior `read-paper:` note lists what that pass wrote. Start from it:
fill gaps, do not redo. Every write below checks for its target first,
so a second run on the same paper should write nothing new.

## Step 1 — orient
## Map the paper before reading it

```python
get(kind="paper", id="pa<id>", view="toc")
get(kind="paper", id="pa<id>", view="summaries")
get(kind="paper", id="pa<id>", view="measures")  # numbers extraction already caught
```

Fewer than about twenty body chunks: read whole. Otherwise read methods,
results, tables and conclusions; introduction and related work are
orientation, never grounding
(`get(kind='skill', id='precis-check-source-help')`). A review paper is
read for the primaries it attributes results to, not for hubs — see
step 5.

## Step 2 — the setup note
## Record the general conditions once; point the results at it

Find the blocks that fix the conditions: computational details, methods,
sample preparation, instrument and measurement conditions, and the
supplementary equivalents. Then:

**Reuse before writing.** Groups reuse protocols; a setup note may
already exist.

```python
search(kind="memory", tags=["paper-setup"], q="<method, code, model, key parameters>")
```

Same conditions in substance → reuse that node: link it to this paper's
blocks, do not copy it. Different parameters (another U value, another
cutoff) are a different setup.

**Write it.** One memory per distinct setup; a paper with a DFT part and
an experimental part has two. The title names the controlled method
term; the body lists the parameters, each with the chunk that states it:

```python
put(kind="memory", tags=["SPACE:research", "paper-setup"],
    title="Setup: DFT PBE+U (VASP) — pa<id>",
    text="method: DFT, PBE+U, U(Fe)=3.5 eV [pc<a>]\n"
         "code: VASP 6.3, PAW potentials, 520 eV cutoff [pc<a>]\n"
         "sampling: 4x4x1 Γ-centred k-mesh; forces < 0.02 eV/Å [pc<b>]\n"
         "model: 4-layer slab, bottom two fixed, 15 Å vacuum [pc<b>]",
    link="pc<a>", rel="derived-from",
    args={"writer": {"model": "<your model id>"}})
link(kind="memory", id="me<setup>", target="pc<b>", rel="derived-from")
```

Write only what the paper states, at its precision; a parameter it does
not give is absent, not guessed. Supplementary-only parameters are
worth the read: the main text often omits the one that matters.

**Point the results at it.** For each results block, table or figure
legend whose numbers depend on this setup:

```python
link(kind="memory", id="me<setup>", target="pc<result>", rel="qualifies")
```

A reader who lands on "ΔG = −0.42 eV" and opens
`get(id='pc<result>', extent='fisheye+1hop')` sees `qualified-by
me<setup>` under Argument, so the conditions travel with the number; the
edge stays pinned to that chunk for passage-level consumers. Link only
blocks that state a result; methods, introduction and discussion of
others do not need it. Several results chunks fold into one ring row, so
a dozen edges stay readable.

**Explain the general terms through the taxonomy, not the note.** What
"PBE+U" or a named model *is* belongs to a `taxon`, shared by every
paper that uses it; the setup note says how *this* paper used it.

```python
search(kind="taxon", q="<the method or model, described by meaning>")
link(kind="memory", id="me<setup>", target="taxon:<id>", rel="instance-of")
```

Absent and likely to recur → mint the term with a one-sentence
definition under its broader node
(`get(kind='skill', id='precis-taxon-help')`,
`get(kind='skill', id='precis-classify-help')`). A one-off in-house
model is described in the setup note, not minted.

The setup's controlled method term (`DFT/PBE+U`) is the `scope.method`
value for every hub in step 3. Use it verbatim: paraphrased scope values
fork hubs.

## Step 3 — key results become claim hubs
## One hub per world-claim, grounded on the passage

For each key result, write a proposal to the admissibility test
(falsifiable, self-contained, method-attributed, one assertion, the
source's precision — `get(kind='skill', id='precis-taproot-mint-help')`),
then call the hub door, which converges onto an existing hub when one
already states the claim:

```python
put(kind="finding", title="<sentence>",
    args={"scope": {"method": "DFT/PBE+U", "material": "<term>"},
          "supporters": [{"paper": "pa<id>", "source_handle": "pc<result>"}]})
```

Act on the door's reply: minted, converged, or minted with a review todo
naming the nearest hub. A number that disagrees with an existing hub is
a new hub plus `link(rel='disputes')`; a sharper statement is a new hub
plus `link(rel='refines')`. Group your own proposals by claim before the
door — it cannot see hubs you minted minutes ago (the step-4 rule in
`precis-read-for-question`).

Caps: the paper's key results (typically three to six), plus at most two
quantified side-facts. A definition, a "was investigated", or a
restatement of prior work is not a hub.

## Step 3b — the numbers become measures
## Make every key value searchable by range

A hub is found by its sentence; a measure is found by value
(`search(kind='measure', property=…, min=…)`). Each key result that
states a number gets one measure run, unless step 1's
`view='measures'` already lists it. Write it while the chunk is in hand,
with the hub as its group and the setup's conditions as input rows:

```python
put(kind="measure", text="-0.42", reason="read-paper pa<id>",
    meta={"measurand": "<taxon path, from search(kind='taxon')>",
          "subject": "pa<id>", "subject_label": "<material/site>",
          "subject_group": "fi<hub>", "reported_unit": "eV",
          "tier": "computed", "model": "<your model id>",
          "anchor": {"chunk": "pc<result>", "anchor_scheme": "sentence", "span": "s2"}},
    items=[{"measurand": "<condition taxon>", "literal": "<value>",
            "condition": "<label>"}])
```

One run per value and condition set ("95% at −0.5 V and 61% at −0.9 V"
is two runs). The literal must appear in the anchored span exactly as
printed; act on any `flagged:` line in the reply. A duplicate refusal
means the number is already written. A measurand with no taxon is
minted first (`precis-measure-help`, `precis-taxon-help`).

Hand-write the key results only. A paper whose value is in its tables
(dozens of numbers) gets the extraction job, which reads the whole
paper, skips what you wrote as duplicates, and queues its own sampled
review:

```python
put(kind="job", job_type="survey_extract", params={"paper": "pa<id>"})
```

Rows are append-only: a wrong value is corrected by a new `put` with
`meta.supersedes`, never an edit (`get(kind='skill',
id='precis-measure-help')`).

## Step 4 — update the memories that care
## Put the specific fact where it will be used

A hub states the claim; a memory holds what it means for something the
graph already tracks — a design decision, an open question, a comparison
table, a material's running dossier. For each key result, find those
nodes by subject, not by the new value, so contradictions surface:

```python
search(kind="memory", tags=["SPACE:research"], page_size=5, args={"batch": [
    {"key": "r1", "q": "<subject of result 1: the material, reaction, property>"},
    {"key": "r2", "q": "<subject of result 2>"},
]})
```

Read each plausible hit's body before deciding. Then apply the
reconciliation policy in `get(kind='skill', id='precis-make-memory')`;
for papers it comes out as:

- **About the subject and missing the fact** → one anchored edit adding
  the specific input: the number, the setup, the hub.
  `"PBE+U puts the O* binding at −0.42 eV on Fe(110) [fi<id>, me<setup>]"`
  — not "a new paper discusses binding". Then
  `link(kind="memory", id="me<n>", target="pc<result>", rel="cites")`.
- **Already holds the fact** → no edit; add the `cites` link if this
  paper is new evidence for it.
- **Holds a conflicting value** → do not overwrite. Edit in the
  discrepancy with both setups named, and `link(rel='disputes')` to the
  chunk. Different setups often explain the gap; say which.
- **Mentions the topic in passing** → leave it; not every keyword hit is
  interested.

A **new memory** is earned when a result matters to ongoing work and no
node can hold it: a new design consequence, a new open question the
paper raises, or a subject that now has two or more papers and no
dossier. Write it per `get(kind='skill', id='precis-memory-help')`, cite
the chunk, link its hub. A memory that only restates a hub is a
duplicate; cite the hub instead.

## Step 5 — mesh extras
## The cheap edges that make the paper reachable

- **Facet tags** — `material:`, `domain:`, `studytype:` per
  `get(kind='skill', id='precis-paper-tag-axes')`. These reach the paper
  from queries no phrasing ranks.
- **Cited primaries** — for each key comparison the paper draws against
  prior work, resolve the cited paper
  (`get(kind='skill', id='precis-cite-resolve-help')`). A review, or a
  result the paper attributes to someone else, yields the primary's DOI:
  stub it (`put(kind='paper', doi=…)`) rather than grounding a hub on the
  secondhand passage.
- **Quests** — a hub that serves an open quest attaches to it
  (`get(kind='skill', id='precis-quest-help')`).
- **Cross-paper inference** — this paper's A plus another's B suggesting
  C is a hypothesis finding, at most one per paper
  (`precis-read-for-question` step 6).

## Step 6 — close the pass
## Leave one note that says what was done

```python
put(kind="paper", id="pa<id>",
    text="read-paper: setup me<a>; hubs fi<1> (new), fi<2> (converged); "
         "measures mx<p>, mx<q>; survey_extract jb<j>; "
         "memories edited me<x>, me<y>; new me<z>; gaps: SI not ingested")
```

This note is step 0's marker for the next reader. Report the same list
to the caller, with each hub labelled new or existing.

## Guards

- **Ground on the full chunk.** Fisheye previews pick neighbours; judge
  on `get(kind='paper', id='<slug>~A..B')` reads.
- **Machine-writer edges.** Run as a worker with a model identity,
  `related-to` and `see-also` are refused and `instance-of` becomes a
  gardener proposal; the typed edges above need a chunk pin in
  `meta.evidence` and the `meta.{model,prompt_version,run_id,why}`
  annotation (`get(kind='skill', id='precis-relations')`, machine-written
  edges).
- **Write nothing to the body.** Body chunks are the paper; notes,
  memories, hubs and links are the reading.
- **Caps keep approvals cheap.** Every hub and every new memory is a
  review later. One to three setup notes, three to six key hubs, two
  side-facts, one hypothesis.

## See also

- [[precis-read-for-question]] — the question-driven loop; same hub door.
- [[precis-paper-help]] — toc, summaries, ranges, scoped search, notes.
- [[precis-taproot-mint-help]] — admissibility, scope keys, convergence.
- [[precis-measure-help]] — measure runs, conditions, flags, the extraction job.
- [[precis-make-memory]] — the reconciliation policy step 4 applies.
- [[precis-relations]] — `qualifies`, `derived-from`, `cites`, `disputes`.
- [[precis-classify-help]] — attaching refs to taxonomy nodes.
