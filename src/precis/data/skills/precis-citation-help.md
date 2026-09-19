---
id: precis-citation-help
title: precis — cite a claim by its finding hub [fi<id>]
summary: every cite in prose is a finding hub [fi<id>] — search hubs first, else find the grounding paper passages in the corpus and mint a hub on them, run an adversarial search for what disputes it, then cite; a paper chunk [pc<id>] grounds a hub's evidence edge, it is never the cite
answers:
  - how do I cite a claim in my prose — do I write [pc<id>] or [fi<id>]?
  - no finding hub matches my claim — how do I make one from the papers in the corpus?
  - how do I check whether the claim I'm about to cite is disputed, and what do I do with the disagreement?
  - when should I record a kind='citation' verification record?
applies-to: drafting prose; search/put/link/get (kind='finding'); put/get (kind='citation')
status: active
tags: [drafting]
kinds: [finding, citation]
---

# precis-citation-help — cite the finding, not the paper

A citation in prose is a **finding hub handle written inline**:
`[fi<id>]`. Never a bare paper chunk `[pc<id>]`, patent chunk
`[pk<id>]` or whole paper `[pa<id>]` — those are **grounding**: the
passages a hub's evidence edges point at. The hub is the cite because
it is *living* (resolves to the current best originator on every
render), *graded* (support counts, disputes, refutation are visible on
the handle) and *shared* (the next writer stacks evidence on it instead
of citing the paper again).

**Discovery is wide; the finding is exact.** Search broadly, several
phrasings, whole corpus. Cite only a hub whose sentence asserts what
your sentence asserts, at the same scope. A near miss is not a hit.

## Cite a claim — the four steps
## How do I cite this?

1. **Search hubs** for the claim. Hit → step 3.
2. **No hub** → find the grounding passages in the paper corpus, mint
   a hub on them.
3. **Adversarial search** — look for what disagrees; file it.
4. **Write `[fi<id>]`** inline. The hub carries the papers.

## Step 1 — is there already a finding hub for this claim?
## Search fi* before anything else

```python
search(kind="finding", q="<the claim as you would write it>", status="*", mode="semantic")
search(kind="finding", q="<its rarest token: a number, a compound, a phrase>", status="*")
```

`status='*'` is required — the default cohort hides most hubs. Two
queries: the sentence, then its rarest token (hybrid search ranks a
rare token high). Each hit shows `state`, `support` (`2✓ 1✗ 1?` =
affirmative / negative / withheld verdicts) and `flags` (`disputed`,
`drifted`, `refuted`, `open-questions:N`). Read them before choosing.

Judge each near hit precisely:

- **Same claim, same scope** → cite it. Read
  `get(id='fi<id>', view='evidence')` once: the originator and passages
  there are what your reader inherits.
- **Same claim, your paper adds a passage** → attach, then cite:
  `link(kind='finding', id='fi<id>', rel='corroborates', target='pc<id>')`.
- **Coarser, narrower, or a different regime** than your sentence →
  not a match. Mint yours (step 2), then `link(rel='refines')` to it.
- **Compound hub** (bundles several atoms) → cite the atom your
  sentence asserts; `get(id='fi<id>', view='links')` lists its
  `conjunct-of` atoms.
- **`refuted` or `disputed` flag** → not a supporting cite. Step 3
  says how a disagreement is cited.

## Step 2 — no hub: find the grounding papers, mint the hub
## Turn corpus passages into a citable finding

Discovery is the wide part — query the whole corpus, several ways, and
never ground on whatever paper you happen to be holding:

```python
search(kind="paper", q="<the claim's most distinctive phrase>")
search(kind="paper", q="<claim terms> <the technique you'd expect>",
       queries=["<the question form>"],
       answers=["<a sentence that would state the result>"], per_paper=2)
get(id="pc<id>")                  # read the full chunk — excerpts are triage-only
get(id="pa<id>", view="claims")   # hubs this paper already grounds — yours may be here
```

Precision rules for what you attach:

- **Read before you ground.** The chunk must state the claim's
  substantive core — a "12%" claim needs "12%" in the chunk. Reader
  loop: [[precis-check-source-help]].
- **Ground on the doer.** A passage attributing the result onward
  ("X et al. showed…"), an introduction, a related-work block
  (`get(id='pa<id>', view='toc')` shows the block) is testimony. Find
  the primary (`search(kind='paper', author='…')`) and ground there.
  Not held → stub it (`put(kind='paper', doi=…)`) and wait, or register
  a chase finding and cite that `[fi<id>]` meanwhile
  ([[precis-cite-paper-help]]). Never a hub on hearsay.
- **A review by genre is secondhand** — mint blocks on it unless the
  claim sentence declares a synthesis mode.

Mint on what you read. Write the sentence to
[[precis-taproot-mint-help]]'s admissibility test — falsifiable,
self-contained, method-attributed, one assertion, the source's
precision, no rounding:

```python
put(kind="finding",
    title="<one self-contained claim sentence>",
    scope={"<regime-key>": "<controlled term>"},
    supporters=[{"paper": "pa5", "source_handle": "pc293"},
                {"paper": "pa9", "source_handle": "pc871"}])
# → "claim hub fi<id> …" — cite it as [fi<id>]
```

Always give `source_handle`: a supporter without one grounds the whole
paper, which is what makes a hub unreadable later. Two passages of one
paper are two supporters. A re-`put` of the same spec attaches nothing
twice.

## Step 3 — adversarial search: is it disputed?
## Look for the paper that disagrees before you cite

Phrase one to three negations yourself — the opposite of the claim, or
a conflicting value / mechanism / tendency under the same conditions —
and search those, in both corpora:

```python
search(kind="paper", q="<negated claim>",
       answers=["<conflicting version 1>", "<conflicting version 2>"], per_paper=1)
search(kind="finding", q="<negated claim>", status="*", mode="semantic")
get(id="fi<id>", view="evidence")   # contradicts / disputes edges already on the hub
```

Read every hit in full. A passage about a different system,
functional, cell size or measurement regime is a **scope mismatch, not
a disagreement** — leave it. Then act:

- **Genuine opposing passage, same conditions** →
  `link(kind='finding', id='fi<id>', rel='disputes', target='pc<id>')`.
  Free and non-blocking: "these appear to conflict; someone should
  look". Never file `contradicts` by hand — it is adjudication-derived
  and blocks the hub's publish.
- **An opposing hub exists** →
  `link(kind='finding', id='fi<id>', rel='disputes', target='fi<other>')`.
- **Your claim is the dissenting one** → mint it anyway (step 2), file
  `disputes` toward the incumbent, cite yours. A hub with a live
  dispute is citable; the reader sees `open-questions:N`.
- **Nothing found** → cite. Claim nothing about absence in prose: the
  hub's `meta.conflict_search` ledger, once the sweep has run it, is
  the checkable statement; your search is not.

Cite the disagreement, don't hide it. Where the text acknowledges a
conflict, the opposing hub sits next to yours:

```text
…higher quantum yields than hot-injection [fi41], though one report
finds no effect at the same ligand ratio [fi77].
```

A `refuted` hub is cited only as the thing that was refuted.

## Step 4 — write the cite
## Drop a [fi<id>] in my prose

```text
Aqueous synthesis yields higher quantum yields than hot-injection [fi41].
…and Pd loading reverses the trend above 5 wt% [fi41][fi92].
```

Several hubs list together, no separators. Pin when you know better
than the derivation: `[fi41>pc293]` cites exactly that passage,
`[fi41+pa5]` adds to the derived originators ([[precis-taproot-help]]).
Export resolves each `[fi<id>]` to its current originator paper(s) and
renders one bibliography entry per paper — you never write LaTeX
citation commands or bibliography keys. The handle is a value you copy
from search / put output, never constructed.

**A memory or another draft is a link, not a citation.** `[me<id>]` /
`[dc<id>]` record provenance and never reach the bibliography.

**Legacy `[pc<id>]` / `[pa<id>]` cites in an existing draft** convert
in bulk ([[precis-taproot-backfill-help]]) or by hand with steps 1–3.
The draft lint's `◆ taproot:` line names the hub a cited paper already
grounds — take it, or pin it (`[fi<hub>>pc<id>]`) to keep the passage.

## Optionally record a verification audit (kind='citation')
## Persist a claim → chunk audit a verifier confirmed

`kind='citation'` is an **optional** verification record — a claim +
`verifier_confidence` pointing at the chunk a verifier checked. It is
not how you cite and not what builds the bibliography. Mint one only
for an auditable record that a verifier confirmed the passage supports
the claim:

```python
put(
    kind="citation",
    text="MOF X improves CO2 reduction by 12%",  # the claim
    source_handle="pc7",  # the chunk it points at; a patent chunk pk<id> works too
    source_quote="CO2 reduction rose 12% on MOF X",  # verbatim from that chunk; required
    verifier_confidence=0.95,  # 0.95 strong, 0.8 moderate, 0.5 weak
    link="pa5",
    rel="cites",
)
# → created citation id=42
get(id="ci42")  # read it back
search(kind="citation", q="MOF CO2 reduction")
get(kind="citation", id="/recent")
```

Records are write-once: re-verifying against a different chunk creates
a new record; the old one stays as the audit trail.

## A paper's own inline [N] markers (automatic)

The numbered `[N]` markers inside an ingested paper's body resolve by
machinery, not by you: each maps to the paper's parsed bibliography
entry, and hub-refine follows a claim's own citation to verify the
claim against the paper it cites. A miss shows on the claim page as a
red *"cited source does not support this claim"* line — a surfaced
fact, not a trust change.

## See also

- [[precis-cite-paper-help]] — the router: which branch when the paper is / isn't held.
- [[precis-taproot-help]] — what a hub is; what `[fi<id>]` resolves to; pins.
- [[precis-taproot-mint-help]] — admissibility test, notation, search-before-mint.
- [[precis-taproot-hub-edit-help]] — attach evidence, reword, merge an existing hub.
- [[precis-check-source-help]] — reader side: find the chunk, read surrounds, judge support.
- [[precis-finding-help]] — chase side: claim → primary source, cite `[fi<id>]` meanwhile.
- [[precis-taproot-backfill-help]] — bulk-convert legacy `[pc<id>]` cites.
- [[precis-search-help]] — broad retrieval (`queries=`, `answers=`, `per_paper=`).
- [[precis-link-help]] — `corroborates`, `refines`, `disputes` and the other relations.
