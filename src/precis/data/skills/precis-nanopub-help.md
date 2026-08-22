---
id: precis-nanopub-help
title: precis — publishing claims as signed nanopubs (view='nanopub' + mint pipeline)
summary: get(kind='finding', view='nanopub') renders a claim hub as TriG — a draft pre-mint, the exact signed bytes post-mint; the mint pipeline (approve → sign → OTS anchor) is CLI/human-driven, not an agent verb
answers:
  - how do I check whether my claim has been published as a nanopub?
  - what publish state is this claim hub in?
  - can I mint or sign a nanopub myself as an agent?
  - why can't I edit a hub that's already reviewed or published?
  - why does a claim hub show as unmintable?
applies-to: get (kind='finding', view='nanopub'); precis nanopub CLI (human-run); reading publish state
status: active
---

# precis-nanopub-help — the published identity of a taproot claim

A reviewed taproot claim can be **minted** as a nanopublication: a
signed, content-addressed TriG artifact (trusty URI under
`https://w3id.org/np/`) whose provenance carries only universal anchors
— DOI, `pdf_sha256` of the exact quoted copy, a verbatim quote, and a
normalized `searchSnip` that locates the passage in any copy. Taproot
stays authoritative; the nanopub is the frozen published form.

## What an agent can do

- `get(kind='finding', id='fi<id>', view='nanopub')` — the TriG
  rendering. Pre-mint you get an **unsigned draft** (placeholder URI,
  `#` status comments; missing grounding is flagged, not invented).
  Once signed you get the **exact frozen bytes** plus a comment header
  naming the trusty URI and the `nanopub_artifacts` row that holds the
  authoritative copy.
- Read the header's publish state: `candidate → reviewed → signed →
  anchored → published → superseded/retracted` (`rejected` off
  `reviewed`). A hub with a live `contradicts` edge renders an
  UNMINTABLE warning — disputed claims are visible internally,
  unpublishable externally, until adjudicated by artifacts.

## What an agent must NOT do

- **Minting, signing, anchoring, sign-off and publishing are not agent
  verbs.** `precis nanopub approve/sign/signoff/anchor/publish` is run
  by a person (the `/nanopub` web surface is the same interactive
  door); the attesting key is invocable only from those surfaces. A bot
  signature alone never publishes anything: publication requires an
  **attesting** entry in the trust allowlist, and the registry POST
  (`publish --live`) is the one irreversible step — CLI-only, never
  automated.
- Never edit a hub that shows `reviewed` or later state to "fix" its
  wording — the approved string is frozen; an edit flips the row back
  for re-review (pre-publication) or forces a public supersede
  (post-publication). Propose the change instead.

## Mint gates (why a claim you drafted may not mint)

**Admissible is not true.** Every gate below checks that a claim is
well-formed, sourced, and traceable — none checks whether it is
*correct*. A hub that clears every gate has passed admissibility, not
verification; read "mintable" as "safe to cite," never as "confirmed."

Layer-A validators run at approve and again at sign; the common
failures an extraction agent can avoid up front:

- **Primary sources only** — grounding whose chunk lives in a
  references list / related-work / prior-art / background section is
  hearsay and rejected, even when the quote checks out. Cite the paper
  that DID the work; if only a secondhand mention is held, the claim
  stays *hanging* (mintable, unpublishable) while the original is
  hunted.
- **No source, no atom** — every claim needs its own verbatim quote
  plus a snip that matches uniquely in the paper's stored text. Snip
  contract: single-spaced lowercase ASCII letters/digits/hyphens
  tokens (~8 words), matching exactly once across the paper's body
  chunks.
- **Quantities carry bound semantics** — `exact` / `upper` / `lower` /
  `approx-range`; "up to 400:1" and "400:1" are different claims.
- **Structured fields must be quote-contained** — a
  material/method/quantity value the quotes don't state is an
  overclaim.
- **Compounds are derivations** — conjunct-of atoms minted first; the
  compound cites atoms, never papers. Cross-binding (a property from
  system B on a phenomenon from system A) is new content: it needs its
  own evidence or mints as a `precis:Hypothesis` (declarative sentence,
  type carries the epistemic status, `testableBy` names the
  discriminating experiment).
- **One DOI per passage** — provenance content is DOI + quote + snip.
  A DOI-less source (textbook/ISBN, patent) keeps its evidence *edge*
  (visible internally) but its passage stays OUT of the payload until
  non-DOI grounding lands (`docs/backlog/nanopub-book-isbn-grounding.md`).
- **The sha pin is `refs.pdf_sha256`** (the held file). TWO
  `pdf_sha256` identifier rows per ref is the metadata write-back's
  normal shape (canonical + as-downloaded alias for dedup probing) and
  does NOT block minting. Zero anywhere = unmintable until the PDF is
  re-hashed or acquired.
- **Quote mechanics** — the quote must be verbatim and contiguous
  within ONE stored chunk (adjacent sentences in the same chunk may be
  joined; never across chunks), trimmed to the bare assertion, free of
  citation markers (including markdown-link residue like
  ``[\[1,2\]](#page-…)`` and superscript residue like
  ``…report.<sup>8</sup>`` — even a quote trimmed just before the tag,
  leaving a citing sentence, is hearsay), and any structured `quantity`
  value must appear inside some quote. The citation-marker check is a
  regex, so bracketed chemical nomenclature — "[60]fullerene", "[2+2]
  cycloaddition" — false-positives as a citation; trim or re-pick the
  quote to exclude the bracketed token (quotes stay verbatim — never
  rewrite one to dodge the gate). Scientific superscripts
  (``cm<sup>-1</sup>``, ``Fe<sup>3+</sup>``, ``10<sup>3</sup>``,
  ``<sup>13</sup>C``, ``m<sup>2</sup>``) are exempt by context and do
  not trip it.
- **Snip vs chunk overlap** — consecutive chunks overlap at their
  boundary, so a snip drawn from an overlap region matches 2× and is
  refused. Remedy: extend the quote one sentence into text unique to
  its chunk and snip there — never weaken the snip.

Grounding reaches the prefill through **both** edge shapes: inbound
evidence edges carry per-edge grounding-chunk pointers; outbound
`derived-from` links ground through their `dst_chunk_id` pin. A hub
whose approve form shows no passages is missing both — re-ground it,
don't hand-type a payload.

Style: a quantitative claim corroborates in the **measured quantity**
(e.g. transistor counts for Moore's law), never in marketing labels
("65 nm node" names no physical dimension) — a passage in the wrong
currency imports a unit confusion into the artifact.

## Claim-sentence grammar — write it at authoring, verified at approve

Write to this grammar when a hub is minted or reworded — not just when
it's approved. `refs.title` still syncs to the approved string at approve,
but approve **verifies** conformance; it does not compose the sentence, so
a candidate that already reads as a claim needs no rewrite there. See
`precis-taproot-mint-help`'s "Claim admissibility": that gate decides whether a
sentence is a claim at all, this grammar governs its shape once it is.

The claim sentence IS the claim — it carries all the meaning and is
exactly as long as it needs to be (no length cap). One plain prose
sentence, shaped **general → specific**: `[epistemic mode + method] +
[system] + evidence verb + finding`.

- **Epistemic mode is mandatory.** Name the sim kind (DFT,
  spin-polarized DFT, molecular dynamics, DFT–NEGF transport, …) or the
  experimental technique (TEM, Raman, c-AFM, nanoindentation, …).
  Gloss niche method names on first use: "first-principles (DFT)".
- **Controlled evidence verbs** (small closed vocabulary — the verb
  encodes epistemic reach, so pick by meaning, not variety):
  - *predicts* — simulation/theory only, and only for a claim that
    reaches beyond the model to the physical system (testable by a
    future measurement): "DFT predicts nanobuds adsorb Li more
    strongly than graphene."
  - *finds / shows* — either mode; the study's own internal result (a
    computed comparison, an analyzed dataset): "DFT calculations find
    the bond-to-ring configuration most stable."
  - *measures* — experiment only; a quantitative technique output:
    "c-AFM measures a Seebeck coefficient of …".
  - *observes* — experiment only; imaging/qualitative: "TEM observes
    fullerene outgrowths on the sidewall."
  - *demonstrates* — experiment only; a capability or effect realized
    in the lab.
  Never *measures/observes/demonstrates* for a simulation; reserve
  *predicts* for forward-looking claims — a within-model comparison is
  *found*, not *predicted*.
- **Tense encodes how the claim relates to time — simple present is
  the default,** for both the evidence verb and the asserted content:
  "DFT calculations show that X adsorbs Y." ("Showed" reads as a
  one-time event that might no longer hold — the wrong reading of a
  published result.)
  - *Simple past* only when the claim's subject is itself a historical
    event: "Haber's 1927 gold-from-seawater program failed because…".
    Test: if rewriting to present makes the sentence false or absurd,
    past is correct. Advisory — `past-tense` (the machine can't judge
    this).
  - *Present perfect* only for an existence/achievement claim, where
    the point is that something has been realized at least once:
    "room-temperature coherence has been demonstrated in…". Elsewhere
    it hedges — hides the agent and the conditions, and weakens
    falsifiability. Advisory — `present-perfect`.
  - *Past passive with no result* is banned: "…was proposed by
    Kirkpatrick et al.", "Surface interactions … were investigated" are
    history-of-science or activity reports, not claims. Blocking —
    `past-passive`. Correlates with the not-falsifiable stub pattern
    (`precis-taproot-mint-help`'s "Claim admissibility") — like the em-dash
    rule there, a mechanical marker for a category error underneath.
  - Measured over all 1,524 live hubs: present 756 (49.6%), present
    perfect 104 (6.8%), simple past 95 (6.2%), past passive 6 (0.4%).
    Among hubs carrying a recognized verb, present is already 79% —
    this rule ratifies existing practice, not a new style.
- **Not colon-label style** ("DFT simulation of nanobuds: …") — the
  sentence is canonicalized and signed; it must read as a standalone
  assertion, not a filing label.
- **Self-contained.** No dangling comparatives ("among those examined"
  → name the set); no coined jargon without an inline gloss; name the
  substrate explicitly (graphene sheet vs nanotube sidewall — say
  which lattice carries the buds).
- **No author names** — provenance lives in the evidence edges, never
  in the sentence.
- **Complete sentence**, no marketing adjectives. Numbers must match the
  source in **value and bound** — never restate a quantity the passage
  doesn't carry, and never convert into a unit the authors didn't use.
  *Notation* is normalized per [[precis-notation-canon]] (UTF-8
  superscripts, no digit-grouping commas, negative exponents over a
  two-denominator solidus) — the canon governs
  spelling, never magnitude. Where the canon and quote-containment
  disagree, quote-containment wins: a structured quantity must appear in
  the quoted passage, so keep the source's `0.05 ps` rather than
  normalizing it to `50 fs`.
- **Quotes are verbatim and are never normalized.** The canon applies to
  the authored claim sentence only; a quote edited to match it fails the
  mint gate.
- **The title asserts what the quotes support, not what the hub body
  says.** Hub bodies routinely overclaim (a range the paper never
  states, a mechanism the passage doesn't name). Re-scope the approved
  title to the evidence rather than inheriting the hub wording, and
  note the divergence for the sign review — don't stretch a quote to
  rescue the hub's phrasing.

## Door behavior under batch load (ops notes)

- **Pace approve batches.** Rapid back-to-back approve POSTs have hung
  the web process (all routes wedge until restart). Serialize
  submissions and sleep ~6 s between POSTs; a batch of N takes ~6·N s
  by design.
- **A 502 is not reliably a no-write.** `evidence/add` has landed its
  write behind a 502 (verify before retrying, or you duplicate the
  edge); `approve` fails closed — its gates run before any write — so
  a 502 there is safe to retry.

## Publish-time gates (past mint — why a signed claim may not publish)

Distinct from mint gates; enumerated by `precis nanopub preflight` and
on the `/claim/fi<id>` page's review section (`/nanopub/fi<id>`
redirects there — one page, reader evidence + review-and-sign):

- **Withheld evidence edges** — an evidence edge neither
  verified-by-refine nor human-signed-off blocks publication; there is
  no mute button. Verification (the refine chase) is the agent-side
  remedy; the literal sign-off is human-only.
- **Trust allowlist** — only pinned (identity, key-fingerprint) pairs
  are trusted, and publishing requires the *attesting* (human) entry.
- **Order** — atoms publish before the compounds citing them; hanging
  claims never publish; a drifted or disputed hub is blocked.

## Content review is a separate axis from gate admissibility

Every gate above (mint and publish) checks *form* — well-formed,
sourced, traceable. Whether anything else in the corpus actually
**disagrees** with the claim is a different question, and today the
only edge that carries that signal is `contradicts`, so filing one is
expensive and rare.

**Know what it actually blocks.** Only a **paper- or patent-sourced**
`contradicts` edge blocks the mint mechanically. `check_contradicts` reads
`bundle.contradicts`, and that bundle is filtered twice: once to
`EVIDENCE_SRC_KINDS` in `taproot/seniority.py::_fetch_evidence_rows`, then
again to `("paper", "patent")` by `nanopub/evidence.py::load_bundle`'s
`_source`. So a **hub- or finding-sourced** dispute never fires the gate —
deliberately, so the opposing hub isn't rendered as a "contradictor" in the
evidence table — and an `edgar`- or `datasheet`-sourced one doesn't either,
which is *not* deliberate (`attach_evidence` accepts those kinds). Both
surface in the overview's `disputed` bucket and hold at human review instead.
Treat any non-paper/patent dispute as needing a person, not as blocked.

`docs/backlog/disputes-edge-nonblocking-disagreement.md` **proposes** —
not yet built — splitting that into a free, non-blocking `disputes`
edge ("these two claims appear to conflict; someone should look") and
keeping `contradicts` as the adjudicated, blocking outcome. A
`disputes` edge would resolve into exactly one of five verdicts, only
the last blocking:

- `same-claim` → attach evidence to the survivor, retire the duplicate
- `refines` → typed `refines` edge
- `scope-mismatch` → different regime; annotate scope on both, no edge
  — the expected majority
- `unit-error` → one side is arithmetically wrong; retract it
- `genuine-conflict` → `contradicts`, plus a hunt for a third
  adjudicating source

None of this exists in code yet — no `disputes` relation, no non-blocking
render. Until it ships, a suspected conflict has no free way to be
filed; raise it to a human rather than either staying silent or firing
`contradicts` on a hunch.

## Triage lane (small-model-safe: classify and file, never fix)

Gate refusals are machine-precise; classifying them needs no judgment.
For an unminted hub, read the review page / preflight refusals and file
per class — do not mutate the hub, its edges, or its sources:

- `[grounding] no DOI` + the source HAS one (check the publisher) →
  propose the `meta.doi` backfill to a human.
- `[grounding] no DOI` + genuinely DOI-less (book, patent) → note the
  edge-stays/passage-out policy on the hub; nothing to fix.
- `[pdf-sha] 0 rows` → acquisition/backfill item
  (`docs/backlog/pdf-sha256-identifier-hygiene.md` class).
- `[pdf-sha] >1 rows` → only ambiguous when `refs.pdf_sha256` is NULL;
  file that combination, otherwise it's the benign alias pair.
- `[primary-source]` (hearsay section or in-quote citation marker) →
  hunt-the-primary todo naming the cited work; the claim stays hanging.
- `[quote-verbatim]` / `[snip]` → the payload needs a human re-trim;
  point at the chunk, don't rewrite the quote yourself.

Quote-trimming, claim restructuring (atom vs compound), and every
approve/sign/signoff click stay above this lane.

## Registry mirror (read-only sidecar)

A local cache of *other people's* published nanopubs — dark behind
`PRECIS_MIRROR_ENABLED`; external nanopubs never enter taproot as
evidence. `precis nanopub mirror status` shows counts;
`sync --live [--all]` pulls missing codes (read-only GETs). External
artifacts are frozen by construction (the code IS the content hash);
`verified` means the trusty recompute over the fetched bytes matched.
Retraction/supersede are *flags derived from edges* (only a same-signer,
verified retraction counts), never exclusions. Concurrence — an external
nanopub asserting one of our AIDA sentences — raises an `alert`.
