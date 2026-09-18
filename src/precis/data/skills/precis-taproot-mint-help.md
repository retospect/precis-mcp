---
id: precis-taproot-mint-help
title: precis — author and mint Taproot claim hubs from a sourced claim
summary: mint a claim hub from a sourced claim, pass the admissibility test before tagging, and search before minting to avoid duplicates
answers:
  - how do I turn a sourced claim into a citable hub?
  - why isn't my sentence admissible as a claim?
  - how do I check for a near-duplicate hub before minting a new one?
  - the paper calls it X but my claim says Y — what do I quote?
  - what does the unsupported-term advisory mean and how do I clear it?
applies-to: put(kind='finding') hub-authoring door; precis taproot mint (CLI equivalent)
status: active
tags: workflow, design
kinds: finding
---

# precis-taproot-mint-help — turn a sourced claim into a citable hub

See [[precis-taproot-help]] for what a claim hub is, `fi<id>` vs
`pub_id`, and how citing `[fi<id>]` resolves.

## Claim admissibility — the test before the tag

Extraction order: **admissibility test → mint → notation autofix → dedup
check → park for review.** Runs first, on the sentence, before any
supporter is attached. Skip it and what gets minted is often not a claim
at all — the orphan-hub bucket (no evidence edge) turned out to be
bibliography stubs.

**Admissible is not true.** This test and every gate below it check that
a claim is well-formed, sourced, and traceable — none checks whether it
is *correct*. Passing the gates means "safe to cite," not "verified."

A sentence earns `TAPROOT:claim` only if it passes all four:

1. **Falsifiable** — asserts a finding some future measurement could
   contradict. Not a definition, not a topic label, not a bibliography
   entry, not "X was investigated", not historical narration.
   - Bad (bibliography entry): "Meir & Wingreen 1992 — Landauer formula
     for interacting electrons."
   - Bad (definition): "NUPACK is a software suite for…"
   - Bad (states a study happened, asserts no finding): "Surface
     interactions between graphene nanobuds and cerium(III) were
     investigated."
2. **Self-contained** — no "the same group", "this work", "as above", no
   dangling comparative.
   - Bad: "The same group demonstrated ultra-sensitive detection of
     vitamins B9 and B12…" — "the same group" refers to nothing once the
     sentence stands alone.
3. **Method-attributed** — the epistemic mode is readable from the
   sentence. Write to `precis-nanopub-help`'s claim-sentence grammar
   (evidence verb + epistemic mode) at authoring time — it governs the
   sentence's shape from the moment it's written, not just at approve.
4. **Single assertion** — see [[precis-notation-canon]]'s terseness rule.

An unfalsifiable entry can never be corroborated or contradicted, so it's
inert mass in the graph, not just badly worded. Something was minting
reading-list entries as claims; this test is what stops it at the door.

**Enforcement asymmetry.** Notation/sentence lint *advises* at mint —
flags, never blocks or rewrites. At approve it *blocks* on the
admissibility and grammar codes above plus every
deterministically-fixable notation code,
including `past-passive` (tense with no result — `precis-nanopub-help`'s
claim-sentence grammar); judgment-only codes (`two-denominator-solidus`,
`approx-spacing`, `tilde-approximation`, `past-tense`, `present-perfect`,
`formula-ascii-subscript`, `scope-*`, `unsupported-term`) stay advisory
even at approve — nothing mechanical can resolve them. The line is *measured*, not
assumed: a code earns blocking status by dry-running over the whole
corpus at a zero false-positive rate. `hyphen-numeric-range` and
`ascii-x-multiplier` cleared that bar and block; `formula-ascii-subscript`
did not and stays advisory forever.
Authoring stays frictionless; nothing ungoverned reaches *publishable*,
but a hub can sit `candidate` indefinitely with an advisory flag
unresolved.

**The blocking set is scoped by artifact type** — a scope, not a
loosening. A `hypothesis`
does not face `no-epistemic-mode`/`no-evidence-verb`: that pair asks how
a finding was established and a conjecture was established by nothing
yet, so its mode lives in the type plus the mandatory `testable_by`.
`claim` and `compound` face the full set, and an unlisted type inherits
it — the default is strict, so a new artifact type fails closed.

**Expect refusal at approve, not malfunction.** A legacy hub failing
approve is the intended workflow, not a bug — the sentence gets authored
properly at that point, not patched around.

## When it isn't a claim yet — mint a hypothesis instead

Every rule below assumes you are stating something a source *shows*. If
what you have is a **conjecture** — two findings whose binding nobody has
demonstrated — do not force it into a claim hub and go looking for a
paper that props it up. That is the failure mode: search a corpus this
size hard enough and something supports almost any proposition.

Mint it as what it is: `put(kind='finding', hypothesis=True, …)` with
`motivation=` (the inferential leap) and `testable_by=` (the experiment
that would settle it). A hypothesis hub carries **no evidence edges by
type** — motivation replaces grounding, and `motivated-by` edges point at
what provoked it. Full rules: [[precis-nanopub-help]].

Do not read such a hub as an *orphan hub* (the no-evidence-edge
pathology this page's admissibility test exists to stop) — an orphan is a
bibliography entry minted by mistake; a hypothesis has no evidence
because it is a guess, and says so in its type.

## What makes a mintable claim

A hub's sentence is read alone — in other drafts, years later, without its
source paragraph. The bar is therefore stricter than for an inline citation.

**Hard gates — fix or don't mint:**

- **Self-contained.** Resolve every "this / these / it / such" against the
  source passage and inline the referent. A dangling demonstrative is a
  correctness hazard on reuse, not a style nit.
  - Bad: "This strategy has been pursued across the principal families of
    2D materials." (whose strategy?)
  - Good: "Hybridization of fullerenes with 2D materials has been pursued
    across graphene, g-C₃N₄, TMDs, h-BN, and black phosphorus."
  - Temporal/discourse openers count too — "Subsequent(ly)",
    "Previous(ly)", "Further", "Earlier", "In contrast", "Similarly",
    "However", "Also" all point at prose the hub won't carry (e.g.
    "Subsequent DFT-D3 calculations reduced…" — subsequent to what?).
    Inline the referent ("Compared to X, …") or drop the connective.
  - Fixing one found later: `edit(kind='finding', id='fi<id>',
    title='<self-contained rewording>')` retitles the hub in place.
- **A world-claim.** About materials, results, mechanisms — never about the
  literature's habits, the paper's own structure ("we will discuss…"), or a
  bare pointer ("see [12]").
  - Bad: "The properties of these materials are commonly tabulated for
    comparative reference." → not a claim.
  - Salvage rule: when meta-prose wraps real content, extract the
    underlying fact (the specific properties or values being compared),
    not the practice. If the passage states only the practice, don't mint.
- **One atomic claim per hub — don't hand-bundle.** `conjunct-of` (atom →
  compound) is written only by the automated decomposition pass, run
  through `taproot_backfill` — not hand-authored. Hand-minting from a
  passage that bundles several atomic
  claims? Mint each as its own hub with its own grounded supporter,
  rather than one bundled sentence.
- **Ground on the primary, not the proxy.** If the grounding passage
  attributes the fact onward ("Ganji et al. [15] showed…"), that passage
  is testimony, not the source. Search the corpus for the primary
  (`search(kind='paper', author='…')`); if held, attach a chunk of it as
  the supporter — seniority then derives it as originator automatically
  — and keep the citing passage as corroborator. If not held, it's a
  chase-finding candidate (`precis-finding-help`), not a hub grounding.
  Same discipline as citing generally, one level stricter — see
  `precis-cite-paper-help`'s "cite the doer, not hearsay."
  A source whose *title* marks it a review/perspective ("…: a review",
  "Recent advances in…") is secondhand by genre — its prose attributes
  findings to the doers it surveys — and now hard-blocks the mint gates
  (`review-source`), even when the quoted passage reads primary. One
  escape: the claim sentence itself declares a synthesis mode ("Review
  synthesis identifies…", "Meta-analysis of…"), which makes the review
  the primary. Remedies: re-ground in the primary, drop the review
  edge, or mint explicitly hanging.

## What makes a mintable claim — soft flags and claim sorts

**Soft flags — mint, but expect review:**

- **Specificity.** Carry the number / material / mechanism the passage
  states; strip empty intensifiers ("extraordinary", "remarkable"). A
  capability claim needs its conditions or contrast to have content.
  - Weak: "Graphene can be physically mixed without site-specific
    attachment."
  - Better: "Graphene–fullerene composites can be formed by physical
    mixing, without site-specific covalent attachment."
- **Grounding depth.** One supporter is mintable; definitions and
  landscape/survey claims also want a secondary source (a review) —
  `hub_refine` attaches corroborators when enabled. Abstract-only
  grounding is fine for a definition/existence claim; a measurement or
  mechanism claim grounded only on an abstract/intro chunk also wants
  the body passage carrying its specifics attached — `hub_refine`'s job
  when enabled, else `link(kind='finding', rel='corroborates',
  target='pc<id>')` manually.
- **Notation.** Claim sentences render as plain text (list views, page
  titles, MCP output) — write formulas with UTF-8 sub/superscripts and
  symbols (`C₆₀`, `g-C₃N₄`, `≈10⁴ cm² V⁻¹ s⁻¹`, `μB`), never TeX
  fragments (`C$_{60}$`, `$\mu_B$`); it feeds the identity hash. Full
  rules: [[precis-notation-canon]].
- **Numeric-value policy** (2026-08-20). A hub's sentence is the citable
  artifact; the source paper's prose is a rendering of it — so a number
  in a hub follows the source's precision, not the draft's:
  1. Prefer the range wherever the source supports a spread, **and state
     what varies** (anisotropy, measurement method, batch/sample, CI) — a
     bare range without its cause is under-specified.
  2. If the source designates a typical value, use typical-plus-range —
     the most informative shape: `≈9 GPa across a reported 9–12 GPa`.
  3. Source gives only a range → state the range alone. Never synthesize
     a typical value; a midpoint is arithmetic, not measurement.
  4. A bare point value is admissible only when the source reports it as
     a point (one measurement, one computed value).
  5. Hubs don't round — rounding is a draft concern (drafts are
     rewritable; hubs destroy precision irrecoverably). Form rules
     (dash, unit placement): [[precis-notation-canon]].

**Sorts of claims** — the bar shifts by sort:

| Sort | Example | Bar |
|------|---------|-----|
| Measurement | "Single-wall carbon nanocones were observed with opening angles of ≈19°, 39°, 60°, 85°, and 113°." | Carry the numbers; one primary source suffices. |
| Definition | "The term 'nanobud' refers to structures in which fullerenes are directly bonded to a carbon nanotube or graphene surface." | Coining paper as originator; wants a review as corroborator. |
| Capability | "Graphene–fullerene composites can be formed by physical mixing, without covalent attachment." | Name the conditions or the contrast, else vacuous. |
| Mechanism | "Charge transfer at the C60–nanotube junction alters field-emission behavior." | Name the mechanism, not "plays an important role". |
| Landscape | "Fullerene–2D hybridization has been pursued across graphene, g-C₃N₄, TMDs, h-BN, and black phosphorus." | Most prone to dangling referents; reviews are the right grounding. |

## Search before you mint — strengthen, don't duplicate

**A hard gate: never mint without searching first.** `pub_id` convergence
is a *content hash* — it catches only byte-identical (post-NFKD)
sentences. Two agents phrasing one claim two ways mint two hubs, each
carrying half the evidence that should have stacked on one. Live
example: `fi191132`/`fi211518` are the same pentagon–heptagon
defect-pair claim, minted independently and never merged.

Before every mint, search the claim sentence you are about to write:

```python
search(kind="finding", q="<the claim sentence>", status="*", mode="semantic")
```

`status='*'` is **required** — the default filter is `status='established'`
and silently hides most hubs. If a search returns nothing on a topic the
corpus plainly covers, round-trip a hub you know exists before trusting
the empty.

Then judge each near hit:

- **Same claim, same scope** → *don't mint*. Attach your evidence to the
  hub that exists: `link(kind='finding', id='fi<existing>',
  rel='corroborates', target='pc<your chunk>')`. One hub with three
  independent groundings outweighs three hubs with one each — the
  strengthening move, and **the default outcome, not the exception**.
- **Same claim, grounded only ref-level** → attach your `pc<id>` passage,
  sharpening it from paper-level to passage-level grounding.
- **Same claim, your wording is better** → reword in place
  (`edit(kind='finding', id='fi<existing>', title=…)`; keeps the old
  `pub_id` as an alias, evidence untouched), then attach.
- **Different scope, or your source carries a quantity bound the
  existing hub lacks** → mint, then `link(rel='refines')` to the
  coarser one.
- **Your source disagrees with the existing hub's number** → mint, and
  `link(rel='disputes')`. Never silently restate someone else's
  quantity.
- **Two existing hubs are near-duplicates of each other** → merge rather
  than adding a third.

**File `disputes`, not `contradicts`.** `contradicts` is
adjudication-derived (claim-graph disagreement, Part 2 of
`docs/backlog/disputes-edge-nonblocking-disagreement.md`, not built) —
a live one blocks the hub's nanopub mint, and you don't have the
warrant to file it by hand. `disputes` is the free, always-safe move:
"these two claims appear to conflict; someone should look" — it never
blocks either hub, so fire it for any genuine disagreement about the
same system under the same conditions. A different functional, cell
size, fullerene size, or measurement regime is a **scope mismatch, not
a disagreement**: mint independently and flag the tension for a
human, rather than filing `disputes` on sound work that's simply
describing something else.

## Notation canon

Claim sentences are hashed to derive `pub_id`, so spelling is
**load-bearing, not cosmetic** — two spellings of one quantity mint two
hubs for the same claim. Full rules (UTF-8 unit forms, forgiven/
not-forgiven lists, the carve-outs that outrank everything else):
[[precis-notation-canon]].

**`scope` values fork hubs too.** `pub_id` hashes the sentence *plus*
the `scope` object, so an identical sentence under paraphrased scope
values mints two hubs, not one claim with metadata drift. A scope value
is a short controlled term naming the regime — never a paraphrase or
restatement of the sentence; a `scope-free-text` lint warns (advisory)
when one reads like prose instead.

## Mint a claim hub from a claim I've already sourced

`put(kind='finding', ...)` is **trimodal**: `supporters=` (no `cited_in`/
`wants=`) mints/converges a claim **hub**; `cited_in=` files an ordinary
chase-target finding; `wants=`+`provenance=` mints an acquisition-mode
finding (both non-hub modes: [[precis-finding-help]]) — mixing modes
errors. Both modes route through the same single write door, so a hub
is still only ever paper-sourced — mint **requires paper supporters**,
and a draft's own
novel assertion (no `supporters`, no `cited_in`) errors rather than
silently becoming a thin-air hub:

```python
put(
    kind="finding",
    title="Pd/C catalyzes Suzuki coupling at room temperature.",
    scope={"catalyst": "Pd/C"},
    supporters=[{"paper": "pa5", "source_handle": "pc293"}],
)  # -> "claim hub fi<id>  pub_id=…" — cite it as [fi<id>]
```

`supporters` is a list of `{paper, role, source_handle}`: `paper` is the
supporting paper (its `pa<id>` handle, cite_key, or pub_id — a patent
handle also resolves); `role` defaults `corroborates`; **`source_handle`
is the grounding `[pc<id>]` paper chunk and you should always supply
it** — it lands on the edge as `src_chunk_id`, so the edge cites the
passage (`pc<id>`), not just the paper (`pa<id>`). List the same paper's
different supporting passages as separate supporters (same `paper`,
different `source_handle`) to attach the whole set. Mints the hub (or
converges onto an existing one for identical claim content, via the
content-hash `pub_id`) and attaches each supporter's evidence edge
idempotently — a re-`put` of the same spec attaches nothing twice (the
dedup key includes the grounding chunk). Cite the resulting `[fi<id>]`
afterward.

**Don't have a chunk handle? Search for one — don't fall back to
ref-level.** Whatever paper you happen to be holding is not the corpus.
Query the whole corpus for the passage that carries this claim's
specifics, then ground the edge on what you read:

```python
search(kind="paper", q="<the claim's most distinctive phrase>")
search(kind="paper", q="<claim terms> <the technique you'd expect>")
get(id="pc<id>")  # read it before you attach it — excerpts are clipped
```

Two queries, because the passage naming a *method* often doesn't repeat
the claim's wording. Search is hybrid lexical + semantic over the whole
corpus, so a rare token (a compound name, a number, a DOI) ranks high —
quote the most distinctive phrase you have. [[precis-check-source-help]]
is the full find → read-surrounds → judge loop; run it before attaching,
not after.

## The paper calls it X, your claim says Y — quote the definition too

A signed hub carries only its quoted passages. If your claim uses a term
the passages don't, a third party reading the artifact cannot get from
one to the other. fi191121 reads "…a C60 nanobud on a semiconducting
(10,0) single-walled carbon nanotube…"; both grounding quotes say only
**CNB100**, the label the paper coins for exactly that system. The
equivalence is real and the claim is correct — but the sentence that
says so sits two paragraphs earlier, unquoted, so the artifact cannot
show it.

**The bridge is a quoted passage, not a table.** Do not keep a
synonym list or translation table anywhere: "CNB100 = C60 on (10,0)
SWCNT" asserted on our authority is unsigned, unsourced, and silently
wrong the first time another paper uses the label differently. The
paper's own definition sentence *is* the evidence for the equivalence;
quoting it makes the claim carry its translation, signed and scoped to
the paper that coined it.

The advisory `unsupported-term` lint (approve page, beside a passing
claim-sentence gate) names each notation-shaped claim term — a label
like `CNB100`, an index pair like `(10,0)`, a formula like `C₆₀`, a
hyphenated label like `UiO-66` — that appears in no quoted passage and
not in the source's title. Ordinary words never trigger it. It never
blocks: a claim that deliberately generalizes past the quote's literal
wording is sometimes exactly right, and the reviewer decides. When it
fires and the term is the paper's own coinage:

1. Find where the paper first uses the label — the definition is at
   first use, often a figure caption or the methods opening. Search on
   the label, not the claim wording:
   `search(kind='paper', q='<label>', scope='pa<id>')`, then read the
   hit (`get(id='pc<id>')`). Search, don't scan: the same paper may
   write `$C_{60}$` in one passage and `C60` in another.
2. Attach that passage as a further grounding of the same paper —
   another supporter entry with the same `paper` and the defining
   `source_handle`, or `link(kind='finding', id='fi<id>',
   rel='corroborates', target='pc<id>')` on an existing hub.
3. Re-run the dry-run; the advisory clears once the defining passage is
   in the grounding set.

If the term is a community synonym with no defining passage (SWCNT vs
SWNT), reword the claim to the paper's own spelling — the hub is read
alone, and the source's spelling is the one a reader can check.

(This is the upstream direction — a claim being minted against its
passages. `precis-claim-fidelity-help` governs the downstream one, draft
prose restating an existing hub.)

## Mint a claim hub — link what you find, and the batch CLI

**What you link is what the next agent can see, and that is a smaller
world than you think.** Measured over the live claim cohort
(2026-08-24, n=60): of the claims that no later pass could repair —
every one rejected for having no honest technique available — **83% had
a usable passage sitting in the corpus, unlinked**, and *none* failed
because the corpus lacked the source. The rest were claims no instrument
establishes (design rules, recited constants, combinatorial results),
not gaps in coverage. The rate did not depend on whether the hub's
existing evidence was a read passage or a bare paper reference, so this
is not about edge granularity: each of those passes could only read what
was already attached, and inherited the link set as the boundary of the
knowable. Naming the passage at attach time is far cheaper than
reconstructing it later.

**"I found nothing" is only true if you searched, with a search that
works.** Absence in the linked set is not absence in the corpus — the
two are different claims and only the second justifies giving up on a
hub. An earlier pass over this same cohort put the rescuable fraction at
70% with 3% absent; those numbers were an artifact of a silently
degraded embedder, and the honest figures above are what a working
search returns. Before you conclude a passage does not exist, confirm
your search can find one that does.

The `precis taproot mint` CLI is the batch equivalent — many claims
from one spec file:

```bash
precis taproot mint --spec spec.json
precis taproot mint --dry-run --spec spec.json  # resolve + report, write nothing
```

`spec.json` is a JSON array of `{sentence, scope, supporters}` — same
shape as the `put()` call above, one entry per claim.

## Change a hub that already exists — evidence, wording, sharpening, merging

See [[precis-taproot-hub-edit-help]].

## See also

- [[precis-taproot-help]] — what a hub is; citing [fi<id>]
- [[precis-taproot-hub-edit-help]] — attach evidence, reword, sharpen, or merge an existing hub
- [[precis-notation-canon]] — claim-sentence notation rules
- [[precis-nanopub-help]] — mint gates + publish pipeline
- [[precis-finding-help]] — non-hub finding modes
