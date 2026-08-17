---
id: precis-nanopub-help
title: precis — publishing claims as signed nanopubs (view='nanopub' + mint pipeline)
summary: get(kind='finding', view='nanopub') renders a claim hub as TriG — a draft pre-mint, the exact signed bytes post-mint; the mint pipeline (approve → sign → OTS anchor) is CLI/human-driven, not an agent verb
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
  ``[\[1,2\]](#page-…)``), and any structured `quantity` value must
  appear inside some quote. The citation-marker check is a regex, so
  bracketed chemical nomenclature — "[60]fullerene", "[2+2]
  cycloaddition" — false-positives as a citation; trim or re-pick the
  quote to exclude the bracketed token (quotes stay verbatim — never
  rewrite one to dodge the gate).
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

## Claim-sentence grammar (the approved title)

The approved title IS the claim — it carries all the meaning and is
exactly as long as it needs to be (no length cap; `refs.title` syncs to
the full approved string at approve). One plain prose sentence, shaped
**general → specific**: `[epistemic mode + method] + [system] +
evidence verb + finding`.

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
- **Not colon-label style** ("DFT simulation of nanobuds: …") — the
  sentence is canonicalized and signed; it must read as a standalone
  assertion, not a filing label.
- **Self-contained.** No dangling comparatives ("among those examined"
  → name the set); no coined jargon without an inline gloss; name the
  substrate explicitly (graphene sheet vs nanotube sidewall — say
  which lattice carries the buds).
- **No author names** — provenance lives in the evidence edges, never
  in the sentence.
- **Complete sentence**, numbers with units matching the source
  exactly, no marketing adjectives.
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
