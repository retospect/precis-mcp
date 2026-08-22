---
id: precis-finding-help
title: precis — register a citation target so the worker can chase it
summary: citation chasing — register a claim for the worker to source via Unpaywall/arXiv/S2/OPS
answers:
  - how do I register a claim so the worker chases down its source?
  - how do I check if someone already created a finding for this claim?
  - how do I search for claim hubs before minting a new one?
  - how do I resolve a finding with multiple candidate sources?
  - how do I use a finding's handle in my draft?
applies-to: put / get / search (kind='finding')
status: active
---

# precis-finding-help — register a citation target and let the chase pull it

A `finding` is a citation target: a claim you want sourced plus a
pointer to where you read it. The worker fetches the cited paper
(via Unpaywall / arXiv / S2 for DOIs, or OPS for patents) and walks
the chain back toward the primary source. You get a numeric `id`
back; drop it in your draft as a placeholder and run
`precis resolve` at finalisation to substitute the primary
`cite_key`.

## Register a finding so the worker can chase its source
## Create a finding to track an empirical claim
## I have a claim and a citation — log it for sourcing

```python
put(
    kind="finding",
    title="gate-bias 2.4 kV / 30 s on Si/SiO2",
    body=(
        "Device prep: 2.4 kV applied across the 50 nm gate oxide "
        "for 30 s on Si/SiO2 MOSCAPs with a Cu top contact "
        "(sputtered), N2 ambient, room temp."
    ),
    scope={
        "electrode": "Cu",
        "ambient": "N2",
        "technique": "DC ramp",
        "substrate": "Si/SiO2",
    },
    cited_in="pc42",
)
# → created finding id=42  pub_id=ab12c3
#   placeholder: [ab12c3]   (the base32 pub_id; precis resolve
#                            substitutes the cite_key once established)
```

Required: `title`, `body`, `cited_in`. Recommended: `scope` (dict —
filters search and dedups identical `(body, scope, cited_in)`
re-submissions so two agents writing the same claim collapse).

**`cited_in=` is the SOURCE you read the claim in — a paper/patent
chunk handle — never the plan/deck/project you're writing into.**
`cited_in='deck-hook'` (a plan slug) is wrong and fails with
`requires cited_in=…`; so does omitting `title=`. `finding` is not a
free-text note tied to your current plan the way `memory`/`gripe`
are — both `title=` and a source-chunk `cited_in=` are mandatory on
every `put`. Patent sources work the same way: `cited_in='patent:ep1234567b1'`.

## What `cited_in=` accepts
## Pointer formats for the source of the claim
## How do I reference the paper or patent I'm citing?

`cited_in=` takes a **handle** — the chunk where you read the claim
(`pc<id>`, copied straight from search/get output):

```python
cited_in = "pc42"  # chunk handle — what output hands you back
cited_in = "patent:ep1234567b1"  # patent source, by DOCDB
```

**A bare `doi:`/`arxiv:` is NOT accepted** — `cited_in='doi:10.1234/xyz'`
fails with `unknown kind 'doi' in link target`. `cited_in` resolves
through the link parser, which only knows corpus kinds. If the source
isn't in the corpus yet, stub it and wait for ingest first
(`put(kind='paper', doi='10.1234/xyz')` + a `paper_ingested` waiting
todo — see precis-cite-paper-help), then point `cited_in=` at the
resulting paper's chunk.

## When to create a finding

Quantitative or empirical claims whose **setup context** matters to
the next reader: *"X = 2.4 kV"*, *"0.1 mol/L NaCl"*, *"12% of
patients responded"*. Skip opinions, definitions, speculation, and
claims you're stating for the first time.

Different setups need different findings even when the bare number
is identical: 2.4 kV on Cu / N₂ is not the same finding as 2.4 kV on
Ag / Ar.

**`cited_in` is mandatory — a finding is not a free-standing note.**
Every finding must point at the corpus chunk you read the claim in.
If you have a claim but **no `cited_in` handle**, do *not* retry the
same `put` — it will keep failing. Instead:

- source in the corpus → cite it (`cited_in='pc42'`);
- source not ingested yet → `search(kind='paper', q='…')` to find it,
  or stub it (`put(kind='paper', doi='…')`) and cite the result;
- your own synthesis with no single source → it is **not** a finding;
  write it into the draft or record a `memory` instead.

## The claim's source isn't in the corpus yet — acquisition mode

If you have a claim but no in-corpus chunk to cite, do **not** retry
`cited_in=` with a guessed handle. Mint the finding in **acquisition
mode** instead: pass `wants=` (paper descriptors) and `provenance=`
(where the claim came from) in place of `cited_in=`.

```python
put(
    kind="finding",
    title="gate-bias 2.4 kV / 30 s on Si/SiO2",
    body=(
        "Device prep: 2.4 kV applied across the 50 nm gate oxide "
        "for 30 s on Si/SiO2 MOSCAPs, N2 ambient."
    ),
    wants=[{"doi": "10.1234/xyz"}],  # or {'arxiv':…} or {'title':…,'url':…}
    provenance="pc17",  # the research note / hunt todo / citing chunk
)
# → created finding id=43  pub_id=de45f6
#   status: STATUS:acquiring
#   awaiting evidence from 1 paper(s):
#     pa88 (minted)
```

`wants=` is a list of ≥1 descriptor dicts, each one of `{'doi':…}`,
`{'arxiv':…}`, or `{'title':…, 'url':…}` — one per paper the claim
expects grounding from. The call **atomically** mints a `DREAM:acquire`
paper stub per descriptor and links it `awaits-evidence`; a doi/arxiv
stub is auto-claimed by `fetch_oa`, a title+url one waits on the hand-
download queue. `provenance=` is a ref/chunk handle — required, and
distinct from `cited_in=`: it's where *this claim* came from (a
research note, a lit-hunt todo, the chunk that cited the not-yet-held
paper), not a pointer into a paper the corpus already holds.

The chase worker polls the linked stub(s); once one lands a PDF with
chunks, it grounds the finding — best-matching passage via embedding
search when the chase pass has an embedder configured (opt-in,
`PRECIS_TAPROOT_CHASE_ENABLED` + `--with-llm`/`PRECIS_CHASE_LLM`; off by
default, so a stock deployment falls back to the same deterministic
lexical-overlap heuristic the ordinary chase uses), plus the STANCE
verifier under `--with-llm` — sets `cited_in`/the chain, and flips
`STATUS:acquiring` → `STATUS:tracing` — the rest of the lifecycle
proceeds exactly as an ordinary finding's. If every linked stub is
still unfetched after `PRECIS_ACQUIRE_GRACE_DAYS` (default 7) with no
live fetch attempt still in flight, the finding gives up honestly:
`STATUS:dead_chain(reason=unacquirable)`, and its stub(s) surface in
the `/drive` hand-download queue.

Omitting `provenance=`, or passing an empty `wants=`, is a `BadInput`
naming the missing piece — acquisition mode never mints a thin-air
claim, it just weakens "traceable to a corpus chunk" to "traceable to
*something*" at mint time.

`acquiring` findings are excluded from the default
`search(kind='finding')` cohort (same as `tracing`) — filter explicitly
with `status='acquiring'` to see the backlog.

## Find an existing finding before creating one
## Search findings to avoid duplicates
## Has someone already chased this claim?

```python
search(kind="finding", q="2.4 kV gate dielectric 30 s")
```

Read the `setup` column of every hit. If one matches, reuse its
`id` rather than spawning a parallel chase; attach your own context
with `put(kind='memory', link='finding:<id>')`.

## Read a finding
## Look up a finding by id
## What does finding 42 say?

Read by the finding's handle `fi<id>` (copy it from output):

```python
get(id="fi42")  # by handle (prefix infers kind)
get(id="fi42", view="log")  # chase event history
get(
    id="fi42", view="evidence"
)  # taproot claim-hub evidence (originators/corroborators/contradicts)
```

```text
# finding 42
title: gate-bias 2.4 kV / 30 s on Si/SiO2
claim:
  Device prep: 2.4 kV applied across the 50 nm gate oxide
  for 30 s on Si/SiO2 MOSCAPs with Cu top contact, N2 ambient.
scope:
  ambient: N2
  electrode: Cu
primary: fischer13
begat by:                     (oldest → newest)
  fischer13
  miller23a  (primary)
status: STATUS:established
```

```python
search(kind="finding", q="...")  # default: established only
search(kind="finding", q="...", status="tracing")
search(kind="finding", q="...", status="*")  # all states
```

## Find claim hubs — taproot's cross-paper evidence aggregation (opt-in)

Behind `axis:taproot` (default-OFF; `PRECIS_AXES_ENABLED` seeds the boot
default, `/categorizers` is the live switch), a finding classifies `TAPROOT:claim` (a grounded
world-claim other papers' evidence can attach to) or `TAPROOT:review`
(an editorial note on a draft, excluded from the claim graph).

```python
search(kind="finding", tags=["TAPROOT:claim"])  # every claim hub
```

Claim hubs surface in the **default** `finding` search — no `status=`
needed. A hub mints `STATUS:canonical` (off the chase-status lifecycle,
never `tracing`/`established`), and the default cohort unions hubs in by
their `TAPROOT:claim` tag alongside `established` findings. Drill one with
`view='evidence'` (above).

`put(kind='finding', ...)` is **trimodal**: `supporters=` (no `cited_in`/
`wants=`) mints/converges a claim hub; `cited_in=` makes an ordinary
chase-target finding, as above; `wants=`+`provenance=` mints an
acquisition-mode finding (above) — mixing modes errors. Hub mint still
**requires paper supporters** (a draft's own novel
assertion never becomes a thin-air hub); `link(kind='finding',
rel='establishes'|'corroborates'|'contradicts', target=<pc/pa handle>)`
attaches evidence to an existing hub. Full contract:
`precis-taproot-mint-help` — the claim sentence must pass its admissibility
rules and the notation canon (`precis-notation-canon`); both advise
here at mint and **block** at approve. Evidence also accrues automatically via the
chase's forward bridge, gated behind `PRECIS_TAPROOT_CHASE_ENABLED`
(default-OFF — not yet run at corpus scale, so evidence is
sparse/absent for now beyond what's agent-minted). Once minted, a hub is
citable by its finding handle, `[fi<id>]` — the same handle you'd
`get(id='fi42')` with. It also carries an internal content-hash
`pub_id` (mint-time convergence key: identical claim text always hashes
to the same `pub_id`, so concurrent mints collapse to one hub). **That
convergence is byte-level, not semantic** — it is not dedup: two wordings
of one claim mint two hubs, each with half the evidence, so
`precis-taproot-mint-help` makes a semantic search mandatory before every
mint. Both
`[fi<id>]` and `[<pub_id>]` resolve to the same hub, but `fi<id>` is
the form to cite. A bare pub_id also works as a get id —
`get(kind='finding', id='ab12c3')` — handy when all you hold is the
placeholder token from a citation.

Both a draft cite and `precis resolve` treat a hub cite as a **living
citation**: it expands to the hub's *current* derived `establishes`
originator(s) (falling back to corroborators, then in-flight, if none
are derived yet) rather than a stored `primary_cite_key` — so a
later-discovered originator or a claim merge improves the output on
the next render, with no re-cite needed. Multiple originators render
as one multi-key cite: `\cite{a,b}` / `[a; b]`.

**Pin it inline** to override the living default (Taproot slice A2, no
storage — the pin lives in the token): `[fi<id>>pa5,pc293]` cites
exactly those handles (replace); `[fi<id>+pa5]` cites the derived
originators plus those (supplement, deduped). A `pc<id>` (paper-chunk)
handle pins a passage but resolves to its parent paper's cite_key. A
pin diverging from the current derivation prints a stderr advisory
(`--strict-pins` turns that into a CI-gate exit 3).

## Use a finding in your draft

> **⚠ Outdated — needs rewrite.** This section describes the standalone
> `precis resolve` CLI (hand-maintained `.tex`/`.md` files), which cites by
> base32 `[<pub_id>]`. It does NOT cover citing inside a `kind='draft'`
> document, where you cite a finding by its `[fi<id>]` handle instead (see
> `precis-draft-help`). The two surfaces don't interoperate; this section
> needs rewriting to say so.

Drop the pub_id in square brackets:

> The gate was held at 2.4 kV for 30 s [ab12c3].

At finalisation:

```bash
precis resolve manuscript.tex --format latex --strict
# → \cite{fischer13} substituted where established
#   in-flight placeholders kept as \cite{ab12c3}\,\textsuperscript{⏳}
#   --strict exits 3 if anything still in flight (CI gate)
```

`--keep-id` annotates dead-chain findings; `--ascii` swaps the
unicode ⏳ for `*` on non-xetex/luatex engines.

## Resolve a multi-candidate finding

`STATUS:multi_candidate` means the source chunk had `[12,13]`-style
multi-cites the chase can't disambiguate. Pick the right one:

```python
edit(kind="finding", id=42, pick_candidate="miller23a")
edit(kind="finding", id=42, pick_candidate="self")  # mark terminal
```

If the chase stalls with `STATUS:dead_chain`, the frontier chunk had
no resolvable inline citation. Mark it terminal with
`pick_candidate='self'`, or — if a fetch never ran — ask the user to
run `precis worker --only fetch`.

**`edit(kind='finding', ...)` accepts exactly one of** `pick_candidate=`
(above) | `title=` | `unacquirable_note=` — passing more than one errors.
`title=` retitles a `TAPROOT:claim` hub in place (rejects a plain finding);
see `precis-taproot-mint-help`'s "Reword a hub in place". `unacquirable_note=`
records a **claim-level** declaration — an author assertion about THIS
claim, never inherited from its source paper — that a print-only/
undigitized source is legitimately citeable despite no digital copy being
obtainable:

```python
edit(kind="finding", id=42, unacquirable_note="print-only 1962 monograph")
edit(
    kind="finding",
    id=42,
    unacquirable_note="abstract states the figure",
    unacquirable_mode="abstract",
)
```

`unacquirable_mode=` picks the trust state: `'abstract'` → **Ⓐ** (the
abstract on file backs THIS claim, full text unread) vs `'vouched'`
(**✍**, the default when omitted) — either way it no longer folds the
claim all the way to **clean**: no one read the full text.

**Five trust states** (`taproot/trust.py`, read by the smartdraft badge +
the exporters), least→most confident-that-something's-wrong:
`clean` (full text read, backs it) ‹ `abstract` (**Ⓐ** — the abstract
backs it, full text unread) ‹ `vouched` (**✍** — source unobtainable,
author vouches) ‹ `unverified` (**⚠** — not checked yet) ‹ `unsupported`
(**‼** — read and contradicts). A block badge takes the worst-of its
cites; `unsupported` is never softened by any override.

**Declaring a source paper unacquirable is a fact, not a claim-backing
assertion — it never yields Ⓐ/✍ by itself.** The *source paper's* **Meta
tab** (`Can't get it`) writes a plain `{note, by, at}` fact ("I tried hard
and could not obtain this; the metadata is correct"): `taproot.trust`
reads it two ways — it *hardens* a clean `TAPROOT:claim` hub whose every
print-visible grounding paper carries one down to `unverified` (never
straight to Ⓐ/✍ — that would fabricate an assertion nobody made), and it
enriches an unverified lifecycle finding's note with why its blocking
source can't be obtained. To actually soften a claim to Ⓐ/✍, declare it
at the **claim level**: the per-finding `unacquirable_note=`/
`unacquirable_mode=` above, or — for a `TAPROOT:claim` hub — the same
control on its `/claim/<head>` web page.

## The inbound counterpart — who cites *this* paper (dark, opt-in)

Everything above is outbound: X cites Y, chase it down to Y's
supporting chunk. `workers/inbound_chase.py` (dark by default — the
`inbound_chase` service; flip with `precis service prio '*' inbound_chase
<n>` or `/categorizers`)
runs the other direction — once a paper has been read, it exhaustively
resolves every corpus-intersecting citer at chunk granularity, no
todo/finding needed. Nothing to register from the agent side; read
`view='links'` on the cited paper for the paper-level edges, or a
citing chunk directly for its "Cites (verified):" sidecar. See
`precis-paper-help`.

## See also

```python
get(kind="skill", id="precis-citation-help")  # verifier-write side of citations
get(kind="skill", id="precis-paper-help")  # chunk-handle grammar (~N, ~A..B)
get(kind="skill", id="precis-search-help")  # query mechanics
get(kind="skill", id="precis-bibliography-help")  # who cites this paper
get(
    kind="skill", id="precis-taproot-help"
)  # claim hubs, evidence edges, living citation
get(kind="skill", id="precis-overview")  # verbs and kinds
```
