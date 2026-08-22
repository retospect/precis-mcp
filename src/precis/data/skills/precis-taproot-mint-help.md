---
id: precis-taproot-mint-help
title: precis — author, mint, sharpen, and merge Taproot claim hubs
summary: mint a claim hub from a sourced claim, pass the admissibility test before tagging, search before minting to avoid duplicates, then attach evidence, reword, sharpen, or merge an existing hub
answers:
  - how do I turn a sourced claim into a citable hub?
  - why isn't my sentence admissible as a claim?
  - how do I check for a near-duplicate hub before minting a new one?
  - how do I attach more evidence to an existing claim hub?
  - how do I merge two claim hubs that say the same thing?
applies-to: put/link/edit(kind='finding') hub-authoring doors; precis taproot mint / refine (CLI equivalents)
status: active
---

# precis-taproot-mint-help — turn a sourced claim into a citable hub

See [[precis-taproot-help]] for what a claim hub is, `fi<id>` vs
`pub_id`, and how citing `[fi<id>]` resolves.

## Claim admissibility — the test before the tag

Extraction order: **admissibility test → mint → notation autofix → dedup
check → park for review.** Runs first, on the sentence, before any
supporter is attached. Skip it and what gets minted is often not a claim
at all — that gap produced 234 of 1,527 live claim hubs with no evidence
edge, the orphan-hub bucket that turned out to be bibliography stubs.

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
flags, never blocks or rewrites. At approve
(`nanopub/gates.py::run_mint_gates`) it *blocks* on the admissibility and
grammar codes above plus every deterministically-fixable notation code,
including `past-passive` (tense with no result — `precis-nanopub-help`'s
claim-sentence grammar); judgment-only codes (`two-denominator-solidus`,
`approx-spacing`, `tilde-approximation`, `past-tense`, `present-perfect`,
`formula-ascii-subscript`, `scope-*`) stay advisory even at approve —
nothing mechanical can resolve them. The line is *measured*, not
assumed: a code earns blocking status by dry-running over the whole
corpus at a zero false-positive rate
(`docs/conventions/corpus-normalization.md`). `hyphen-numeric-range` and
`ascii-x-multiplier` cleared that bar and block; `formula-ascii-subscript`
did not (~23% nomenclature collisions) and stays advisory forever.
Authoring stays frictionless; nothing ungoverned reaches *publishable*,
but a hub can sit `candidate` indefinitely with an advisory flag
unresolved.

**Expect refusal at approve, not malfunction.** Only 21/1,524 live hubs
(1.4%) currently lint clean. Common blockers: `no-epistemic-mode`
(1,419), `no-evidence-verb` (1,232), `no-terminal-period` (330),
`author-name` (157), `over-long` (152), `not-falsifiable` (138). A legacy
hub failing approve is the intended workflow, not a bug — the sentence
gets authored properly at that point, not patched around.

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
  compound) is written only by the automated decomposition
  (`taproot/hub.py::apply_extraction`, run through `taproot_backfill`) — not
  hand-authored. Hand-minting from a passage that bundles several atomic
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
  `link(rel='contradicts')`. Never silently restate someone else's
  quantity.
- **Two existing hubs are near-duplicates of each other** → merge rather
  than adding a third.

**`contradicts` is a heavy edge — a live one makes the other hub
unpublishable.** Fire it only for a genuine disagreement about the same
system under the same conditions. A different functional, cell size,
fullerene size, or measurement regime is a **scope mismatch, not a
contradiction**: mint independently and flag the tension for a human,
rather than marking sound work disputed.

## Notation canon

Claim sentences are hashed to derive `pub_id`
(`identity.py::normalize_text_for_hash` → NFKD-fold, lowercase,
whitespace-collapse), so notation is **load-bearing, not cosmetic** — two
spellings of one quantity mint two hubs for the same claim. Quotes are
the exception: verbatim, **never** normalized — the canon governs the
authored sentence only.

**Canon v3.1 in one line: the ASCII→UTF-8 unit rules only fire with a
numeral next to the unit** (`50 micrometres` → `50 µm`, but
`micron-scale` stays spelled out; `Zn2+-sensing` must not become
`Zn2±sensing`). The conditions are per-row in the fallback table —
don't reconstruct them from memory: [[precis-notation-canon]], which
also carries the forgiven/not-forgiven lists and the three carve-outs
(quote-containment, never-convert-the-unit, nomenclature-isn't-notation)
that outrank everything else.

**`scope` values fork hubs — free text there is not cosmetic either.**
`pub_id` hashes the sentence *plus* the `scope` object, so an identical
sentence under paraphrased scope values is two claims to the identity
layer, not one claim with metadata drift. Both live exact-sentence
duplicate pairs in the corpus are this failure, not a wording
difference: `fi191179`/`fi191260` (`scope.method` `"engineered into
printed"` vs `"engineered into printed touch sensors"`) and
`fi191192`/`fi191262` (`scope.quantity`, same content, different prose
framing). Neither encodes a real regime distinction. **A scope value is
a short controlled term naming the regime — never a paraphrase or
restatement of the sentence.** `lint_scope`'s `scope-free-text` warning
now fires at mint on both the MCP and CLI doors (advisory, per the
enforcement asymmetry above); 156 of 1,525 hubs currently trip it. Scope
*keys* are moving to a frequency-ordered registry
(`docs/backlog/scope-key-vocabulary-registry.md`); the value rule is
already in force.

## Mint a claim hub from a claim I've already sourced

`put(kind='finding', ...)` is **trimodal**: `supporters=` (no `cited_in`/
`wants=`) mints/converges a claim **hub**; `cited_in=` files an ordinary
chase-target finding; `wants=`+`provenance=` mints an acquisition-mode
finding (both non-hub modes: [[precis-finding-help]]) — mixing modes
errors. Both modes route through the same single write door
(`taproot/hub.py`, via `seed_claim_hub`), so a hub is still only ever
paper-sourced — mint **requires paper supporters**, and a draft's own
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
different `source_handle`) to attach the whole set. Omit it only when
you genuinely can't name the chunk; the edge then stays coarse
ref-level. Mints the hub (or converges onto an existing one for
identical claim content, via the content-hash `pub_id`) and attaches
each supporter's evidence edge idempotently — a re-`put` of the same
spec attaches nothing twice (the dedup key includes the grounding
chunk). Cite the resulting `[fi<id>]` afterward.

The `precis taproot mint` CLI is the batch equivalent — many claims
from one spec file:

```bash
precis taproot mint --spec spec.json
precis taproot mint --dry-run --spec spec.json  # resolve + report, write nothing
```

`spec.json` is a JSON array of `{sentence, scope, supporters}` — same
shape as the `put()` call above, one entry per claim.

## Attach evidence to an existing hub

To add a supporter to a hub that already exists (not at mint time),
`link(kind='finding', ...)` is the write door — no CLI equivalent, this
is MCP-only:

```python
link(kind="finding", id="fi42", rel="corroborates", target="pc293")
```

`rel` ∈ `establishes` / `corroborates` / `contradicts`; `target` is the
supporting paper/chunk handle — `pc<id>` grounds the edge at that
passage, `pa<id>` lands it ref-level. `rel` is a conservative write-time
label only — the originator/corroborator split is **derived** at read
time (`get(id='fi42', view='evidence')`), same as every other door here.
`id` must resolve to a live `TAPROOT:claim` hub (`fi<id>`, a pub_id, or a
bare ref_id); anything else, or `mode='remove'`, falls through to the
generic finding-link door.

## Reword a hub in place

Same claim, better wording — a rubric fix (dangling referent, meta-prose,
empty intensifier, TeX→UTF-8 notation) rewords the hub, it doesn't mint a
new one:

```python
edit(
    kind="finding",
    id="fi42",
    title="Hybridization of fullerenes with 2D materials has been pursued "
    "across graphene, g-C₃N₄, TMDs, h-BN, and black phosphorus.",
)
```

Retitles the hub in place (`src/precis/taproot/hub.py::refine_claim_sentence`):
`refs.title` updates (full length, never truncated), the `finding_body`
chunk is DELETE+INSERT re-emitted (embedding/summary cascade re-runs —
this is also the chunk hub dedup retrieves over, so the reword is
picked up automatically), stale card variants (`ord < 0`) drop, and a new
content-derived `pub_id` is added — the **old** one is kept as an alias,
so existing `[<pub_id>]` cites keep resolving. Evidence edges are
untouched. Rejects a non-hub finding and `dry_run` (no preview; the
write is direct). If the new wording's `pub_id` already belongs to a
*different* live ref, that's a duplicate-hub signal — the call raises
naming that ref rather than silently fusing it; see "Merge duplicate
hubs" below.

**Not this door for a materially sharper/narrower claim** — that's a new
mint + `refines` link, below, not a retitle.

## Sharpen, refine, or merge a claim hub

Three different operations on an existing hub:

- **Same claim, better wording** → reword in place, above.
- **Materially sharper/narrower claim** → mint a new hub and link it
  `refines` the original, below. Both wordings stay independently citable,
  and the fisheye Claims ring shows the next editor that a sharper version
  exists.
- **Duplicate hubs** (two hubs converged separately on the same claim) →
  merge, below.

```python
# 1. mint the sharper claim (its own hub / fi<id>)
out = put(kind="finding", title="…sharper wording…", scope={}, supporters=[…])
# 2. link sharper --refines--> original
link(kind="finding", id=f"fi{out['hub_ref_id']}", rel="refines", target="fi<original>")
```

`id`/`target` each accept an `fi<id>` handle, a pub_id, or a bare
ref_id; both must resolve to live `TAPROOT:claim` hubs. The link is
**directed** (sharper → coarser), **advisory-only** (no evidence flows —
each hub keeps its own paper→hub edges), and **idempotent**. The Claims
ring then shows `↰ refined by fi<sharper>` on the original and `↳
refines fi<original>` on the sharper one.

CLI: `precis taproot refine --from fi<sharper> --to fi<original>`
(`--dry-run` to preview).

### Merge duplicate hubs

No automated merge door — the `pub_id`-collision raise from a reword
attempt above is the handoff, not a self-serve button. Pick the
survivor (better wording / more evidence), then:

```python
# 1. repoint every citing draft chunk from the dup to the survivor
edit(
    kind="draft",
    id="dc1652005",
    mode="find-replace",
    find="[fi<dup>]",
    text="[fi<survivor>]",
)
# 2. move evidence unique to the dup onto the survivor
link(kind="finding", id="fi<survivor>", rel="corroborates", target="pc<chunk>")
# 3. retire the dup
delete(kind="finding", id="fi<dup>")
```

Repeat step 1 for every draft chunk citing `[fi<dup>]` (`search(kind='draft',
q='[fi<dup>]')`) and step 2 for every evidence edge the dup holds that the
survivor doesn't; delete last — a draft still citing the dup would 404 once
it's gone.

## See also

```python
get(kind="skill", id="precis-taproot-help")  # what a hub is; citing [fi<id>]
get(kind="skill", id="precis-notation-canon")  # claim-sentence notation rules
get(kind="skill", id="precis-nanopub-help")  # mint gates + publish pipeline
get(kind="skill", id="precis-finding-help")  # non-hub finding modes
```
