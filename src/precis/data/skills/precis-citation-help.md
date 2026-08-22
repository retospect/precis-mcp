---
id: precis-citation-help
title: precis — cite a paper inline with its chunk handle
summary: the inline [pc<id>] cite — write the supporting paper-chunk handle in prose; kind='citation' is an optional verification record, not how you cite
answers:
  - how do I cite a claim in my prose — do I write a [pc<id>] or create a kind='citation' record?
  - when should I bother creating a verification record for a citation?
  - how do I read back a stored citation by id?
  - what happens to a paper's own inline [N] reference markers?
applies-to: drafting prose; put (kind='citation'), get (kind='citation')
status: active
---

# precis-citation-help — cite a paper inline

A citation in a draft is the **bare supporting paper-chunk handle,
written inline in your prose**: `[pc234]` (paper chunk 234). The chunk
*is* the evidence — there is no verbatim quote to copy and no citation
command to type. This is the **write side** of cite-a-passage: find the chunk that
backs your claim, read it to confirm it supports the point
([[precis-check-source-help]]), then drop its handle inline.

## Cite a claim — write the supporting chunk handle inline
## Drop a [pc<id>] cite in my prose
## Several chunks back one sentence

Write the handle directly in the sentence it supports:

```text
Aqueous synthesis yields higher quantum yields than hot-injection [pc234].
```

Several supporting chunks — in one paper or across papers — list
together, no separators:

```text
…higher quantum yields than hot-injection [pc232][pc234][pc593].
```

Patents cite the same way by their chunk handle `[pk<id>]`; an in-flight
`finding` is cited `[fi<id>]` until the chase resolves it
([[precis-finding-help]]).

The handle is a value you **copy from search / get output** — never
construct or guess it. There is no slug to assemble:

```python
search(
    kind="paper", q="<claim or key phrase>", scope="<slug>"
)  # → returns pc<id> handles
get(id="pc234")  # read it; the chunk IS the evidence
```

**The author never hand-writes LaTeX citation commands** — they are
retired (export-only). The Tier-B export engine resolves each `[pc<id>]`
→ its paper and renders the citation plus **one bibliography entry per
paper** at compile time. A made-up bibliography key matches nothing and
is wrong.

**A memory / thought / other draft is a link, not a citation.** Drop a
`[me<id>]` or `[dc<id>]` handle to record a `related-to` provenance
edge — it never reaches the bibliography. Citations are to the
literature, not to our notes.

## Optionally record a verification audit (kind='citation')

`kind='citation'` is an **optional** verification record — a claim +
`verifier_confidence` pointing at the chunk you checked. It is **not
required to cite** and **not what builds the bibliography** (the inline
`[pc<id>]` does that). Mint one only when you want an auditable record
that a verifier confirmed the chunk supports the claim.

## Write a verification record once a verifier has confirmed support
## Persist an optional claim → chunk audit
## Store a citation record for a claim

```python
put(
    kind="citation",
    text="MOF X improves CO2 reduction by 12%",  # the claim
    source_handle="pc7",  # the chunk it points at
    verifier_confidence=0.95,
    link="pa5",
    rel="cites",
)
# → created citation id=42
```

This is optional — the inline `[pc7]` in your prose is what cites the
paper. The record just captures that a verifier judged chunk `pc7` to
support the claim. `verifier_confidence` is a float in `[0.0, 1.0]` —
use any precise value the verifier emits, or stick to conventions
(0.95 strong, 0.8 moderate, 0.5 weak). The `link='pa5'` (paper handle) +
`rel='cites'` makes the source paper findable via the graph and
surfaces "who cites me?" on the paper itself.

`source_handle` accepts a **patent** chunk the same way — a `[pk<id>]`
handle or an explicit `patent:<slug>~N` — validated against the patent
corpus instead of papers; `link='patent:<slug>'` wires the same `cites`
edge. A simple-family stub (biblio only, no blocks) is rejected with the
family's full representative named in the error.

Citation records are write-once. Re-verifying the same claim against a
different chunk creates a new record; the old one stays as the audit
trail.

## Read back a citation by id
## Fetch a stored citation
## Show me citation 42

```python
get(id="ci42")  # by handle (prefix infers kind)
```

```text
# citation 42
_MOF X improves CO2 reduction by 12%_

source: `pc7`
verifier_confidence: 0.95
verified_at: 2026-05-31T14:23:00Z
```

## Browse recent or matching citations

```python
search(kind="citation", q="MOF CO2 reduction")
get(kind="citation", id="/recent")
```

## How does this become LaTeX? (you don't write the citation commands)

You write only the inline `[pc<id>]` handles. At compile time the
Tier-B export engine resolves each handle → its paper and emits the
LaTeX citation plus **one bibliography entry per paper** — you never
hand-write citation commands or bibliography keys; they are retired. A
made-up key matches nothing in the export resolver and is wrong: it is
the inline handle, not a key you author, that carries the cite.

The review-pass citation-faithfulness verifier reads each `[pc<id>]`,
resolves it, and confirms the chunk supports the claim it sits beside —
the chunk itself is the evidence, so there is no quote string to drift.

## Discipline that keeps cites honest

**Cite the chunk that actually supports the claim.** Read it by handle
(`get(id='pc234')`) and confirm support before you write `[pc234]`. The
chunk is the evidence; a handle next to a claim it doesn't back is a
hallucinated citation.

**Triage excerpts are not citation-grade.** The `excerpt @ ~N:`
sub-lines in search results are picked for triage — they help you
decide whether to drill in. Fetch the full chunk with
`get(id='pc<id>')` (or the `~A..B` range) and read it before you cite.

**Numeric claims need numeric support.** A "12%" claim demands "12%"
(or "twelve percent") in the cited chunk. A claim of 12% FE backed by
a chunk that only says "an improvement was noted" is a hallucination —
cite a different chunk or chase a finding.

**Cite the doer, not the mention.** A chunk that supports the claim but
is itself quoting/citing another paper is hearsay, not the source —
walk back to the paper that did the work. Full policy + rationale:
[[precis-cite-paper-help]].

## What happens to a paper's OWN inline `[N]` markers (automatic)

The `[pc<id>]` handles above are how *you* cite while drafting. Separately,
the system resolves the numbered `[N]` markers that already sit in an
**ingested paper's body** ("as shown previously [34]"): the `bib_mark`
pass maps each to the paper's parsed bibliography entry (`chunk_citations`
→ `paper_bib_entries`), and `taproot.resolve_citation(store, chunk_id,
marker)` returns the cited paper's identity (`doi` / `held_ref_id`). This
is machinery, not a verb you call — but it powers a real check: taproot's
hub-refine now **follows a claim's own citation** and verifies the claim
against the paper it actually cites. When "we read the cited paper and the
claimed content isn't there", the claim page shows a red *"cited source
does not support this claim"* line (`meta.citation_misses` on the hub).
Hub trust itself is unchanged — the miss is a surfaced fact, not a
trust-state flip.

## See also

```python
get(
    kind="skill", id="precis-cite-paper-help"
)  # the cite-a-paper router (in/out of corpus, which branch)
get(
    kind="skill", id="precis-check-source-help"
)  # reader side: find the chunk, read surrounds, judge support
get(
    kind="skill", id="precis-finding-help"
)  # chase side: claim → primary source, cite [fi<id>] meanwhile
get(kind="skill", id="precis-search-help")  # find the chunk handle to cite
get(kind="skill", id="precis-paper-help")  # fetch chunks; pc<id> / ~N grammar
get(kind="skill", id="precis-link-help")  # cites and other graph relations
get(
    kind="skill", id="precis-taproot-help"
)  # cross-paper claim hubs, [fi<id>] living citation
get(kind="skill", id="precis-overview")  # verbs and kinds
```
