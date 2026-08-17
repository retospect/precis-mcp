---
id: precis-taproot-help
title: precis — the cross-paper claim-evidence graph (Taproot)
summary: claim hubs (finding tagged TAPROOT:claim) aggregate many papers as typed evidence edges; [fi<id>] is a living citation that resolves to the current best originator(s)
applies-to: get/search (kind='finding', tags=['TAPROOT:claim'], view='evidence'); citing [fi<id>] in prose; put/link/edit(kind='finding') hub-authoring doors; put(kind='job', job_type='taproot_backfill') for draft backfill; precis taproot mint / refine / backfill (CLI equivalents)
status: active
---

# precis-taproot-help — one claim, many papers, one citable hub

**Taproot** is the cross-paper evidence graph: instead of fifty papers
asserting the same fact as fifty disconnected citations, they converge
on one **claim hub** — a `finding` tagged `TAPROOT:claim`
(`STATUS:canonical`), the canonical node for that world-claim, citable
as `[fi<id>]`.

**`fi<id>` vs `pub_id`.** `fi<id>` (kind+serial, same family as
`pc`/`dc`/`me`) is the handle you write when citing a hub. `pub_id`
(a 6-char base32 content hash, e.g. `tbx2hd`) is the internal
mint-time dedup key — identical claim text always hashes to the same
`pub_id`, so concurrent mints of the same claim converge on one hub.
Both resolve to the same hub; `[fi<id>]` is the one to author.

## Find a claim hub to cite
## Search for existing claim hubs before minting a new one

```python
search(kind="finding", tags=["TAPROOT:claim"])  # every claim hub
get(id="fi42", view="evidence")  # originators / corroborators / contradicts
```

A hub surfaces in the **default** `finding` search — no `status=`
needed; the default cohort unions hubs in by their `TAPROOT:claim` tag
alongside `established` chase findings.

## The evidence model — typed, graded, cross-paper

Papers attach to a hub as one of three typed edges (ADR 0073):
`establishes` (originator), `corroborates`, `contradicts`. A live
`contradicts` edge also blocks the hub's nanopub mint until adjudicated
([[precis-nanopub-help]]) — attach one deliberately, never as a
softer "partially disagrees". The
originator (★) is **derived at read time**, not stored — it's whichever
supporter(s) the *other* supporters' citations converge on
(`src/precis/taproot/seniority.py::derive_evidence`, over the held
`cites` graph). No intra-supporter citation edge held → every supporter
stays `corroborates` (never guessed).

**A compound hub holds no direct evidence.** When a claim decomposes into
several atomic sub-claims (see the backfill decomposition note below), the
bundling sentence gets its own hub — cite-able, but attach-only-through-atoms:
`link(..., rel='establishes'|'corroborates'|'contradicts')` onto a compound
hub raises. Attach evidence to the atom hub the passage actually supports
instead — `get(id='fi<id>', view='links')` lists a compound's `conjunct-of`
atoms.

A compound's **trust** is derived, not absent: worst-of its atoms' own trust
states (`taproot/trust.py::_compound_trust`, status `hub-compound`). So
`get(id='fi<id>', view='evidence')` on a compound shows a trust label with no
direct evidence edges underneath it — that's the expected depth-1 rollup, not
missing data.

**Edges are chunk-grounded.** An evidence edge names the *specific
passage* that supports the claim: supply a supporter's `source_handle`
(a `[pc<id>]` paper chunk) and the edge is stored `pc<id>`-granular, so
the link graph — and every reader on it (the finding's link table, the
citation tree) — resolves to that passage, not just the whole paper.
Two distinct passages of one paper become **two edges** ("the set of
chunks that support this point"). Omit `source_handle` and the edge
falls back to a coarse ref-level `pa<id>` — the whole paper, no passage
— which is exactly what makes a claim tree hard to walk. Always ground
the edge when you know the chunk. See `precis-fisheye-help`'s Claims
group (`fisheye+1hop` on prose that cites a hub) for the read-time
render of this same evidence.

**Patent evidence grounds in description text, not legal claims.** A
patent's claims section defines legal scope, not empirical support
(`docs/architecture/glossary.md`'s world-claim vs legal claim) —
`hub_refine`'s discovery leg drops legal-claim blocks before they ever
reach Verify, so the automated corroborator search never surfaces one.
That's a discovery-side filter, not an attach-time guard: hand-attaching
via `link()` still means picking a description/abstract passage yourself.

**A prophetic patent example only ever corroborates.** When an evidence
edge's grounding chunk is a patent paragraph the `patent_example` axis
tagged `PATENT_EXAMPLE:prophetic` — present/future/modal tense, proposed
rather than performed (US patent convention) — `attach_evidence`
mechanically appends a fixed caveat to `meta.caveats`: `"prophetic
example (proposed, not performed) — corroborates at best"`. It's
injected at the single evidence-edge choke point, never by the verify
LLM, so every prophetic-grounded edge carries it regardless of caller. A
worked example (past tense, actually performed), and any patent chunk
the axis hasn't tagged, get no caveat — it's a downgrade signal, never a
hard exclusion.

## Cite a claim hub — the living citation
## What does a bare [fi<id>] cite resolve to?

A bare `[fi<id>]` resolves, in a draft, the fisheye reference ring,
and the draft export, to the hub's **current** derived `establishes`
originator(s) — falling back to corroborators, then in-flight — freshly
re-derived on every render (ADR 0074). A later-discovered originator or
a hub merge improves the cite on the next render; no re-cite.
`precis resolve` (the standalone `.tex`/`.md` CLI, not draft export)
still keys on the content-hash `[<pub_id>]` form instead — same
resolution, different token.

Pin it when you know better than the derivation:

```text
[fi<id>>pa5,pc293]   # replace — cite exactly these handles
[fi<id>+pa5]         # supplement — derived originators plus these
```

A `pc<id>` (paper-chunk) handle pins a passage but resolves to its
parent paper's cite_key. A **replace** pin that diverges from the
current derivation prints a stderr advisory; `--strict-pins` promotes
that to a CI-gate exit 3. A **supplement** pin never fires the
advisory (it's purely additive).

**One paper chunk can ground more than one claim hub.** A chunk that
asserts two distinct claims can supply evidence to two different hubs
— so a given `[pc<id>]` handle doesn't map to a single `[fi<id>]`. Pick
the hub for the specific claim your sentence makes, not just "the hub
near this chunk."

**Atom vs compound — same rule, one level up.** When a claim decomposed
into a bundling **compound** hub over several atomic hubs (`conjunct-of`,
above), cite the atom when your sentence asserts just that one conjunct;
cite the compound only when your sentence genuinely restates the bundled
claim as a whole. `get(id='fi<id>', view='links')` lists a compound's
`conjunct-of` atoms if you need to pick among them.

**If a cited `[fi<id>]` errors "not a TAPROOT:claim finding":** the
finding either never was a hub, or was demoted to `TAPROOT:review` — a
2026-08-04 axis-pass race (fixed), but pre-fix casualties exist. Check
its tags (`get(id='fi<id>')`); if the sentence is meta-prose, de-cite
the draft down to the underlying `[pc<id>]`; if it passes the rubric
(above), restore the `TAPROOT:claim` tag
(`tag(kind='finding', id='fi<id>', add=['TAPROOT:claim'])`).

## Turn a draft's [pc<id>] cites into a hub cite

Most legacy prose cites raw paper chunks (`[pc<id>]`), written before claim
hubs existed. Convert a draft scope's `[pc<id>]` (and `[pa<id>]`, below) cites
into hub `[fi<id>]` cites by **enqueuing a `taproot_backfill` job**. The cascade
is LLM-heavy (`extract → block → dedup_judge → place`) and by design runs on the
cluster worker — **never in the MCP process**; the verb only mints the job:

```python
# Canonical: write the intent as a todo; the dispatch worker mints the job.
put(
    kind="todo",
    text="taproot backfill my-draft-slug",
    meta={
        "executor": "claude_inproc",
        "job_type": "taproot_backfill",
        "params": {"scope": "my-draft-slug"},
    },
)
# → the dispatch worker mints the taproot_backfill job under it (one tick).
# Ad-hoc submit skips the intent layer — parent on the draft's numeric ref_id
# (its subject ref, ADR 0044) or a todo's; parent_id is an int, not a slug:
#   put(kind="job", parent_id=<draft ref_id>, job_type="taproot_backfill",
#       params={"scope": "my-draft-slug"})
get(kind="job", id="jo<id>")  # poll: job_event stream + [pc]→[fi] as it runs
```

`params.scope` is a draft slug (every body chunk), a `dc<id>` heading (its
section), or a `dc<id>` leaf (one chunk); `params.ref_level` (default false)
controls the `[pa]` arm (below). The job runs **serially and checkpointed** on
the melchior agent worker: one chunk at a time (so hub convergence sees a stable
committed set — no parallel near-duplicate race), progress recorded in
`meta.done_chunk_ids`, and a re-claim resumes where it left off. **There is no
preview** — the prose rewrite is a DELETE+INSERT through the draft edit door, so
the chunk history is the undo if a conversion is wrong. The CLI form runs the
same cascade in a shell / batch context:

```bash
precis taproot backfill --chunk dc1652005 --apply   # one chunk / section
precis taproot backfill --draft my-draft-slug       # every body chunk in a draft
```

It anchors on the `[pc<id>]` markers (the citation grouping picks the claim
span — not a sentence split you pick yourself): each cite's preceding prose
is the claim span, and adjacent pc-cites (`[pc1][pc2]`) grounding one span
collapse to **one** written cite. Each span runs the full canonicalizer
cascade (`extract_claim → block → dedup_judge → place → apply_extraction`):
if the span bundles more than one atomic claim, extraction splits it into
several atom hubs (each with its own evidence edge) plus a non-evidence
**compound** hub `conjunct-of`-linked to them (see "The evidence model"
above) — either way the prose rewrite target is **one** `[fi<hub>]` (the
compound when one landed, else the lone atom), so a citer sees no change. A
risky merge files a review `todo` and leaves the `[pc…]` untouched, and a
pointer-only span (no groundable claim) is left as-is. The prose rewrite goes
through the draft edit door (DELETE+INSERT, embeddings re-run). Idempotent at
the draft level — a re-run finds no `[pc…]` left to convert.

It is **on-demand, per draft or section** — not a corpus sweep. Idempotent at
the draft level: a re-run finds no `[pc…]` left to convert.

**Whole-paper `[pa<id>]` cites (the `[pa]` arm).** The same command also
recognizes bare whole-paper `[pa<id>]` cites (kept in their own groups — a
`[pa]` and a `[pc]` never fold together). Each is classified by whether its
paper is fetched:

- a **stub** `[pa]` (an un-fetched paper, 0 body chunks) is **skipped**
  (`stub-fetch-first`) — there's no passage to ground an edge, and an unread
  paper is never minted as evidence. Fetch the paper first, then re-ground.
- a **fetched** `[pa]` is **re-grounded** by default: a locate (lexical pick +
  a Tier.MEDIUM confirm) finds the supporting passage and the token is
  rewritten `[pa<id>]`→`[pc<chunk>]` (action `reground`), which the existing
  `[pc]` path then promotes to a **chunk-grounded** hub on a later run
  (two-step; no hub is minted by the re-ground itself). If no passage is found
  it's `reground-nomatch` — left `[pa]`, no write. Pass `--ref-level` to
  instead promote it whole-paper: it mints a **ref-level (ungrounded)** evidence
  edge and rewrites `[pa]`→`[fi<hub>]` directly — for claims with no single
  grounding passage (e.g. "X is a landmark result"); the job_summary reports the
  `ref-level/ungrounded` count. A contiguous multi-paper `[pa1][pa2]` run
  re-grounds all-or-nothing: if any supporter fails to locate, the whole run is
  left untouched (never erase a token).

```python
# the [pa] arm rides the same job; ref_level=True promotes a fetched [pa]
# whole-paper instead of re-grounding it to a [pc] passage
put(
    kind="todo",
    text="taproot backfill dc1652005 (ref-level)",
    meta={
        "executor": "claude_inproc",
        "job_type": "taproot_backfill",
        "params": {"scope": "dc1652005", "ref_level": True},
    },
)
```

CLI equivalent: `precis taproot backfill --chunk dc1652005 --apply [--ref-level]`.

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
    "However", "Also" all point at prose the hub won't carry. Inline the
    referent ("Compared to X, …") or drop the connective.
    - Bad: "Subsequent DFT-D3 calculations reduced the sidewall binding
      energy to +0.74 eV." (subsequent to what?)
    - Good: "Including pairwise dispersion corrections (DFT-D3) reduces
      the calculated C₆₀–nanotube sidewall binding energy from ~+1.5 eV
      to +0.74 eV."
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
  claims? Mint each atom as its own hub with its own grounded supporter,
  rather than one bundled sentence.
- **Ground on the primary, not the proxy.** If the grounding passage
  attributes the fact onward ("Ganji et al. [15] showed…"), that passage
  is testimony, not the source. Search the corpus for the primary
  (`search(kind='paper', author='…')`); if held, attach a chunk of the
  primary as the supporter — seniority then derives it as originator
  automatically — and keep the citing passage as corroborator. If not
  held, it's a chase-finding candidate (`precis-finding-help`), not a
  hub grounding. Same discipline as citing generally, one level
  stricter — see `precis-cite-paper-help`'s "cite the doer, not
  hearsay."

**Soft flags — mint, but expect review:**

- **Specificity.** Carry the number / material / mechanism the passage
  states; strip empty intensifiers ("extraordinary", "remarkable"). A
  capability claim needs its conditions or contrast to have content.
  - Weak: "Graphene can be physically mixed without site-specific
    attachment."
  - Better: "Graphene–fullerene composites can be formed by physical
    mixing, without site-specific covalent attachment."
- **Grounding depth.** One supporter is mintable; definitions and
  landscape/survey claims also want a secondary source (a review) — the
  `hub_refine` pass attaches corroborators when enabled. Abstract-only
  grounding is fine for a definition/existence claim; a measurement or
  mechanism claim grounded only on an abstract/intro chunk also wants the
  body passage carrying the claim's specifics attached — `hub_refine`'s
  job when enabled, `link(kind='finding', rel='corroborates',
  target='pc<id>')` manually meanwhile.
- **Notation.** Claim sentences are plain text rendered without a math
  engine (list views, page titles, MCP output): write formulas with
  UTF-8 sub/superscripts and symbols — `C₆₀`, `g-C₃N₄`, `≈10,000 cm²/Vs`,
  `μB` — never TeX fragments (`C$_{60}$`, `$\mu_B$`).

**Sorts of claims** — the bar shifts by sort:

| Sort | Example | Bar |
|------|---------|-----|
| Measurement | "Single-wall carbon nanocones were observed with opening angles of ≈19°, 39°, 60°, 85°, and 113°." | Carry the numbers; one primary source suffices. |
| Definition | "The term 'nanobud' refers to structures in which fullerenes are directly bonded to a carbon nanotube or graphene surface." | Coining paper as originator; wants a review as corroborator. |
| Capability | "Graphene–fullerene composites can be formed by physical mixing, without covalent attachment." | Name the conditions or the contrast, else vacuous. |
| Mechanism | "Charge transfer at the C60–nanotube junction alters field-emission behavior." | Name the mechanism, not "plays an important role". |
| Landscape | "Fullerene–2D hybridization has been pursued across graphene, g-C₃N₄, TMDs, h-BN, and black phosphorus." | Most prone to dangling referents; reviews are the right grounding. |

## Mint a claim hub from a claim I've already sourced

`put(kind='finding', ...)` is **trimodal**: `supporters=` (no `cited_in`/
`wants=`) mints/converges a claim **hub**; `cited_in=` files an ordinary
chase-target finding; `wants=`+`provenance=` mints an acquisition-mode
finding (both non-hub modes: [[precis-finding-help]]) — mixing modes
errors. Both modes route through the same single write
door (`taproot/hub.py`, via `seed_claim_hub`), so a hub is still only ever
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
ref-level. It mints the hub (or converges onto an existing one for
identical claim content, via the content-hash `pub_id`) and attaches
each supporter's evidence edge, idempotently — a re-`put` of the same
spec attaches nothing twice (the dedup key includes the grounding
chunk, so re-running never duplicates a passage). Cite the resulting
`[fi<id>]` in your prose afterward.

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
time (`get(id='fi42', view='evidence')`), same as every other door on
this page. `id` must resolve to a live `TAPROOT:claim` hub (`fi<id>`, a
pub_id, or a bare ref_id); anything else, or `mode='remove'`, falls
through to the generic finding-link door.

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
`refs.title` updates (full length, never truncated — the claim sentence
carries all the meaning), the `finding_body` chunk is DELETE+INSERT
re-emitted (embedding/summary cascade re-runs), card variants (`ord < 0`) are
dropped for card_forge to re-emit, and a new content-derived `pub_id` is added
— the **old** `pub_id` is kept as an alias, so existing `[<pub_id>]` cites
keep resolving. Evidence edges are untouched. Rejects a non-hub finding and
`dry_run` (no preview; the write is direct). If the new wording's `pub_id`
already belongs to a *different* live ref, that's a duplicate-hub signal —
the call raises naming that ref rather than silently fusing it; see "Merge
duplicate hubs" below.

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
each hub keeps its own paper→hub edges), and **idempotent**. In the
Claims ring the original then shows `↰ refined by fi<sharper>` and the
sharper one shows `↳ refines fi<original>`.

The CLI equivalent: `precis taproot refine --from fi<sharper> --to
fi<original>` (`--dry-run` to preview).

### Merge duplicate hubs

No automated merge door — the `pub_id`-collision raise from a reword attempt
above is the handoff, not a self-serve button. Pick the survivor (better
wording / more evidence), then:

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

## Maturity — what's live vs dark

| | |
|---|---|
| Hub mint / evidence attach (`src/precis/taproot/hub.py`) — `put(kind='finding', supporters=…)`, `link(kind='finding', rel='establishes'\|'corroborates'\|'contradicts')`, and CLI `precis taproot mint` | live |
| Hub reword-in-place (`hub.py::refine_claim_sentence`) — `edit(kind='finding', title=…)` | live |
| Seniority derivation (originator/corroborator split) | live |
| Living-citation resolve + authorial pins (`precis resolve`) | live |
| Fisheye reference-ring Claims explosion | live |
| Claim→claim `refines` links — `link(kind='finding', rel='refines')` and CLI `precis taproot refine` | live (advisory-only, no evidence flow) |
| Whole-draft/section/chunk `[pc<id>]`→`[fi<id>]` backfill — `put(kind='job', job_type='taproot_backfill')` (serial, checkpointed, melchior `claude_inproc` lane) and CLI `precis taproot backfill` | live (on-demand; LLM runs on the cluster worker, never the MCP) |
| Atomic decomposition — `extract_claim` splits a span into atom hubs + an optional bundling **compound** hub, `conjunct-of`-linked via `taproot/hub.py::apply_extraction`; runs inside the `taproot_backfill` cascade above, no separate door | live (compound hubs are excluded from `hub_refine`'s due-set and `chase_trigger`'s embed/probe — those two touch atoms only) |
| Whole-paper `[pa<id>]` arm (stub-skip; default `[pa]`→`[pc]` re-ground; `params.ref_level`/`--ref-level` whole-paper promote) | live (slices 1+2; job + CLI) |
| Corpus-wide forward chase bridge (`PRECIS_TAPROOT_CHASE_ENABLED` — a `chase`-pass sub-feature, not its own service) | dark, default-OFF |
| Hub-refine pass (`workers/hub_refine.py`, `hub_refine` service) | dark, default-OFF — `precis service prio '*' hub_refine <n>` / `/categorizers` |
| Chase-trigger pass (`workers/chase_trigger.py`, `chase_trigger` service) — marks a hub `TAPROOT_DUE` when a near paper/patent chunk lands, so hub-refine claims it promptly instead of waiting out its backstop | dark, default-OFF — `precis service prio '*' chase_trigger <n>` / `/categorizers` |
| `axis:taproot` `TAPROOT:claim`/`TAPROOT:review` classifier (`PRECIS_AXES_ENABLED`) | dark, default-OFF |

All dark rows default off — evidence stays sparse until turned on to seed
it. Everything with its own `service_config` service (`hub_refine`,
`chase_trigger`, and every `axis:<id>`) flips live via `precis service
prio` / `/categorizers`, no redeploy; the forward chase bridge is a
`chase`-pass-internal env flag, unaffected.

## See also

```python
get(kind="skill", id="precis-fisheye-help")  # Claims explosion in the reference ring
get(
    kind="skill", id="precis-finding-help"
)  # finding lifecycle, chase, the evidence view
get(kind="skill", id="precis-citation-help")  # the inline [pc<id>] cite, write side
get(kind="skill", id="precis-draft-help")  # authoring prose that cites hubs
get(
    kind="skill", id="precis-nanopub-help"
)  # mint gates + publish pipeline downstream of a hub
```
