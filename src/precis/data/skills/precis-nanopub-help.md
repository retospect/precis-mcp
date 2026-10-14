---
id: precis-nanopub-help
family: evidence
title: precis — publishing claims as signed nanopubs (view='nanopub' + mint pipeline)
summary: get(kind='finding', view='nanopub') renders a claim hub as TriG — a draft pre-mint, the exact signed bytes post-mint; view='mint-preflight' runs the real gates read-only; an agent may propose a hypothesis but the mint pipeline (approve → sign → OTS anchor) stays CLI/human-driven
answers:
  - how do I check whether my claim has been published as a nanopub?
  - what publish state is this claim hub in?
  - can I mint or sign a nanopub myself as an agent?
  - how do I propose a hypothesis for a human to review?
  - how do I check my payload against the mint gates without approving?
  - when does signing freeze a claim's content?
  - why does a claim hub show as unmintable?
  - how does the OpenTimestamps (OTS) batch anchor work, and when does a signed nanopub get timestamped?
applies-to: get (kind='finding', view='nanopub'|'mint-preflight'); put (kind='finding', hypothesis=True); precis nanopub CLI (human-run); reading publish state
status: active
tags: [workflow, troubleshooting]
kinds: [finding]
---

# precis-nanopub-help — the published identity of a taproot claim

A reviewed taproot claim can be **minted** as a nanopublication: a
signed, content-addressed TriG artifact (trusty URI under
`https://w3id.org/np/`) whose provenance carries only universal anchors
— the source's DOI (or, for an SEC filing, its accession number and archive
URL), `pdf_sha256` of the exact quoted copy (none for an HTML filing), a
verbatim quote, and a
normalized `searchSnip` that locates the passage in any copy. Taproot
stays authoritative; the nanopub is the frozen published form.

## What an agent can do

The web approval page's open questions show stored disputing passages,
support verdicts and reasoning when available. Below them, the
conflict-search coverage panel reads the hub's `meta.conflict_search`
ledger as a statement — "no known conflict as of <date>, method
v<n>, N passages verified" with the covered passages — or says the hub
was never swept / was swept under an older method / more than 90 days
ago. Pre-approve, a missing or stale ledger queues one `conflict_sweep`
refresh job per day (only when the `conflict_search` service is lit);
the page never runs the search itself. All of this is advisory, never
an approval veto; missing results do not establish that no opposition
exists. Details: [[precis-taproot-help]].

- `get(kind='finding', id='fi<id>', view='nanopub')` — the TriG
  rendering. Pre-mint you get an **unsigned draft** (placeholder URI,
  `#` status comments; missing grounding is flagged, not invented).
  Once signed you get the **exact frozen bytes** plus a comment header
  naming the trusty URI and the `nanopub_artifacts` row that holds the
  authoritative copy.
- Read the header's publish state: `candidate → reviewed → signed →
  anchored → publishing → published → superseded/retracted` (`rejected` off
  `reviewed`). A hub with a live `contradicts` edge renders an
  UNMINTABLE warning — disputed claims are visible internally,
  unpublishable externally, until adjudicated by artifacts. A
  **frozen** (signed/published) hub that newly acquires a live
  `contradicts` edge also raises `nanopub-demote/contradicted-frozen:<hub>`
  (warn) — the freeze stands, but the claim needs a successor or a
  dispute adjudication before it can be read as settled.
- **Signing is the correction boundary.** Candidate/reviewed claims are
  internal: edit in place, returning unsigned approvals to candidate and
  flagging their consumers for re-review. Once signed, correct through a
  successor nanopublication (`npx:supersedes`), including anchored/published
  predecessors. Existing bytes and proofs remain immutable. Legacy signed
  reopen/demotion paths still need alignment; do not use them to change a
  signed claim. Re-read the current state before acting.
- `get(kind='finding', id='fi<id>', view='mint-preflight')` — run the
  **real** mint gates read-only and get the violation list back. Pass a
  candidate envelope as `args={'payload': {...}}`; omit it and whatever
  is frozen or parked on the hub is gated instead. No state change. Use
  this instead of reimplementing the gates locally — a hand-rolled
  mirror rots silently the moment a gate changes.
- **Propose a hypothesis** — see the section below. The one artifact
  type an agent can originate; minting, signing, anchoring, sign-off,
  and publishing stay human/CLI-only (`precis nanopub
  approve/sign/signoff/anchor/publish`, or the `/nanopub` web surface)
  — an attesting signature ties to the signer's own ORCID iD, so there
  is no way to attest as the machine.

## Hypothesis — the artifact type an agent can originate

Five artifact types exist: `claim` (an atomic finding, grounded in a
verbatim passage), `composite` (a conjunction of already-signed atoms),
`hypothesis`, and the two human-set non-empirical types `report` and
`definition` ([[precis-nanopub-types-help]]). A hypothesis asserts a **conjecture**, so by
definition it has no supporting passage — the gates reject a hypothesis
that arrives carrying one. What it carries instead is `motivation`
prose naming the inferential leap and `testable_by` naming the
discriminating experiment, *"what separates a conjecture from vibes"*.

Reach for it when two findings suggest a binding nobody has
demonstrated. The worked example
(`docs/reference/nanopub-example/qi-hypothesis-scaled-switching.trig`)
came from a composite that **failed** its commensurability gate: every
clause mapped but the binding was unearned, so it was re-minted
honestly as a typed Hypothesis. A signed, timestamped hypothesis is a
priority claim on an idea. The sentence stays declarative and unhedged
— epistemic status lives in the *type*, so a later confirming claim can
carry the same sentence and converge on the same content address.

Because status lives in the type rather than the prose, it is **read from
the DB at render time, never written into the text**. A hypothesis cite is
a plain `[fi<id>]` on disk but renders with its own sigil at every cite
site, and `motivation`/`testable_by` render as a falsification line in the
agent-facing finding view, the draft fisheye and `/claim`. Precedence is
**refuted → hypothesis → canonical → pending**: a refuted hypothesis renders
red, since do-not-repropose outranks the epistemic mark. Nothing needs
rewriting when a hypothesis is later refuted or promoted.

```
put(kind='finding', hypothesis=True,
    title='<claim sentence — same grammar as any claim, see below>',
    motivation='<what each source established; which transfer is unproven>',
    testable_by='<the measurement that would settle it either way>',
    motivated_by=['pc293', 'fi1234'],   # >=2 independent sources (papers/structures)
    llm_models=['claude-fable-5'],      # required: the model id(s) authoring this
    from_memory='me4567')               # optional: the note it came from
```

## Hypothesis — the mint door's rules

Rules the door enforces:

- **≥2 independent motivating sources.** A distinct source paper (papers,
  patents, and claim hubs count through their underlying source papers) or
  a distinct measured `structure` ref each count as one; a conjecture that
  leaps from one source restates that source. Two claim hubs grounded
  in the same single paper are one source, not two; one structure alone
  is one source, not two.
- **Papers, patents, claim hubs, and measured structures only** — a memory
  is something you thought *with*, not a source an artifact can cite (name
  it in the `motivation` prose instead); a quest is a container, not an
  observation.
- **`llm_models` is required** — the model id(s) you are running as (plus
  any co-authoring model). It freezes into the envelope at approve and
  lands as `precis:llmModel` in the signed pubinfo: a machine-written
  artifact attributes its machine author. This holds for ANY payload an
  agent drafts, not just this door — the `llm-attribution` gate refuses
  an agent-parked payload without it, at approve, sign, and
  `mint-preflight` alike.
- Naming a passage (`pc<id>`) rather than a whole paper records *which*
  passage provoked the conjecture, as a chunk-granular `motivated-by`
  edge. Those edges are motivation, never support: `hub_refine` is
  deliberately blind to them, because widening a guess by searching for
  evidence that agrees with it is a confirmation engine.
- The sentence faces the blocking lint, **minus the epistemic pair**.
  `no-epistemic-mode` and `no-evidence-verb` ask how a finding was
  established, and a conjecture has no answer — so naming a technique in
  the sentence would name one that never ran. Put the discriminating
  experiment in `testable_by` instead, which the door requires anyway.
  Everything else still applies: one falsifiable assertion, no author
  names, no dangling reference, terminal period, UTF-8 canon. The
  reference `.trig` above is the witness — copy its sentence *and* its
  shape.

The door **prepares**; it never approves. It lints the sentence first
and refuses without writing anything if it fails, so a rejected
proposal leaves no hub behind for a human to clean up. On success it
mints the hub, attaches the motivation edges, and parks the prepared
envelope so the human's approve form comes pre-filled. Confirm with
`view='mint-preflight'`, which runs the full gates (not just the
sentence) against what you parked.

**Sharpening.** `motivation=`/`testable_by=` aren't frozen at mint: while
the claim is unsigned (`candidate`/`reviewed`), `edit(kind='finding',
id='fi<N>', testable_by=…)` (and/or `motivation=…`) patches the parked
envelope and keeps the prior wording in `meta.testable_by_history` /
`meta.motivation_history`. An unsigned review reopens in place and consumers
are flagged/listed for re-review. Once signed, correction requires a successor
nanopublication; this edit door refuses.

## Report and definition types

Non-empirical hubs (a company says X; a term means Y) use the human-set
types `report` / `definition`, set only as `artifact_type` on the approve
payload — agents cannot create them and must not propose one to get past a
lint failure. Rules, gate slugs, genre lists: [[precis-nanopub-types-help]].

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
- **Composites are derivations** — conjunct-of atoms minted first; the
  composite cites atoms, never papers. Cross-binding (a property from
  system B on a phenomenon from system A) is new content: it needs its
  own evidence or mints as a `precis:Hypothesis` (declarative sentence,
  type carries the epistemic status, `testableBy` names the
  discriminating experiment).
- **One public identifier per passage** — provenance content is the
  source's identifier + quote + snip. A paper is cited by its DOI; an
  `edgar` filing by its SEC accession number (the ref slug; the source
  node is its archive URL, with a `precis:secAccession` literal; no
  `pdf_sha256` needed); a `datasheet` by `urn:sha256:` of its PDF, with
  its fetch URL as `rdfs:seeAlso` when known; a `web` page by
  `urn:sha256:` of the raw bytes of its last fetch (not of the extracted
  text), with the URL as `rdfs:seeAlso`. The page's sha is frozen onto
  the passage at approve; it is never read from the payload, and a web
  page with no sha on record (cached before the sha was recorded) is
  refused until you re-fetch it with `get(kind='web', id='<url>',
  mode='refresh')`. If the page is later re-fetched and its content has
  changed, the claim page shows "source changed since approval" — a
  display notice only: nothing is blocked or un-approved, and sign still
  proceeds on the frozen quote and sha. A refresh whose bytes are
  identical keeps the page's chunks; one whose bytes differ replaces
  them, so judge whether the quote still holds. A source with no ruled
  identifier — textbook/ISBN, patent — keeps its evidence *edge* (visible
  internally) but its passage stays OUT of the payload until its
  grounding lands (`docs/backlog/nanopub-book-isbn-grounding.md`).
- **The sha pin is `refs.pdf_sha256`** (the held file). TWO
  `pdf_sha256` identifier rows per ref is the metadata write-back's
  normal shape (canonical + as-downloaded alias for dedup probing) and
  does NOT block minting. Zero anywhere = unmintable until the PDF is
  re-hashed or acquired.

## Mint gates — quote mechanics

- **Quote mechanics** — the quote must be verbatim and contiguous
  within ONE stored chunk (adjacent sentences in the same chunk may be
  joined; never across chunks). **Span policy: pick the minimal
  single-chunk span that contains every structured field** (material,
  method, quantity, …) — one contiguous passage that states the whole
  claim beats a scatter of lean fragments, because the artifact's public
  reader sees only the quotes. Fall back to multiple tight quotes only
  when no single-chunk span covers all fields. Keep it free of
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

## Mint gates — grounding and style

Grounding reaches the prefill through **both** edge shapes: inbound
evidence edges carry per-edge grounding-chunk pointers; outbound
`derived-from` links ground through their `dst_chunk_id` pin. A hub
whose approve form shows no passages is missing both — re-ground it,
don't hand-type a payload.

Style: a quantitative claim corroborates in the **measured quantity**
(e.g. transistor counts for Moore's law), never in marketing labels
("65 nm node" names no physical dimension) — a passage in the wrong
currency imports a unit confusion into the artifact.

Reviewer check at approve/sign: a measurement or mechanism claim needs at
least one grounding passage from the body that describes the measurement
(methods, results or a figure caption) — an abstract or a definition
sentence alone is not enough. Every method, instrument or quantity the
sentence names (TEM, STS, a value) must appear in some grounding passage,
matching acronym and expansion both ways as the paper writes them. The
preflight list, `precis nanopub check` and `precis nanopub sign` warn
(`term-coverage`, never blocking) on any method, acronym or number the
claim names that no grounding passage carries, and name up to three chunks
of the hub's papers that do, figure captions and methods first. Sign
refuses when evidence was attached after the grounding froze (gate
`grounding-stale`); for an unsigned row, re-review with
`precis nanopub reopen fi<id>` then approve again, or sign with the explicit confirm after checking the newer
evidence. A row approved before 2026-10-04 has no recorded freeze time; its
last state change stands in, so evidence linked before that change is not
listed — compare the hub's evidence against the grounding by hand.

## Claim-sentence grammar — moved to its own skill

The approved title's shape — mandatory epistemic mode, the controlled
evidence-verb vocabulary, tense rules (`past-passive` blocking,
`past-tense`/`present-perfect` advisory), self-containment, no author
names, numbers matching the source in value and bound, notation per
[[precis-notation-canon]] with quote-containment winning — is in
[[precis-nanopub-claim-grammar-help]]. Write to it at authoring; approve
verifies conformance and does not compose the sentence.

## Publish-time gates (past mint — why a signed claim may not publish)

Distinct from mint gates; enumerated by `precis nanopub preflight` and
on the `/claim/fi<id>` page's review section — one page, reader
evidence + review-and-sign (`/nanopub/fi<id>` is the workbench deep
link framing it):

- **Withheld evidence edges** — evidence edges are **born withheld**:
  an edge neither verified nor human-signed-off blocks publication;
  there is no mute button. `meta.support` is a *verdict*, stamped only
  by something that read the pinned passage and only with its
  `support_reason` + `verified_by` (plus `verified_at`,
  `verified_claim_sha`) — never a mint-time default. Editing the claim
  sentence invalidates sha-carrying verdicts, so an edited hub's edges
  re-enter verification rather than keeping a stale stamp. `precis
  taproot verify-edges` (dry-run default) is the certification sweep;
  the literal sign-off stays human-only.
- **Trust allowlist** — only pinned (identity, key-fingerprint) pairs
  are trusted, and publishing requires the *attesting* (human) entry.
- **Order** — atoms publish before the composites citing them; hanging
  claims never publish; a drifted or disputed hub is blocked.

## Content review is a separate axis from gate admissibility

Every gate above (mint and publish) checks *form* — well-formed,
sourced, traceable. Whether anything else in the corpus actually
**disagrees** with the claim is a different question, and two relations
carry that signal, split along who has decided
(`docs/backlog/disputes-edge-nonblocking-disagreement.md`):

- **`disputes`** — free to file (agent, human, or an LLM judge), **never
  blocks**, from any source kind or direction. "These two claims appear
  to conflict; someone should look." File one whenever you spot a
  possible conflict — a duplicate, a scope mismatch, a unit error, a
  genuine contradiction — via `link(rel='disputes', ...)` between the
  two claim-hub findings. It renders as a visible, non-red "open
  question" on the claim page and in the overview's
  `open_disputes_count` — a question, not a demerit against either hub.
- **`contradicts`** — adjudication-only (Part 2 of that item; not yet
  built). **Blocks the mint mechanically**, and does so unconditionally:
  `check_contradicts` reads `evidence.live_contradicts`, which counts
  ANY live `contradicts` edge touching the hub, either direction, any
  counterpart ref kind — the source-kind/direction filtering an earlier
  revision of this doc described is gone; adjudication is the warrant
  now, so there is no narrower reading of "blocks." No code path mints
  a `contradicts` edge today — the only route to one is Part 2's
  (unbuilt) adjudication workflow, so a live claim-graph `contradicts`
  row should be rare and always traceable to an adjudication decision.

**When in doubt, file `disputes`.** It is the free, always-available,
never-wrong move for "I think these disagree" — it can never mis-block
a claim, and a suspected conflict you can't confidently classify (is
this the same system? same conditions? a real contradiction or a units
slip?) is exactly what it exists for. Never file `contradicts` by
hand — it only ever derives from adjudication (Part 2); filing one
yourself is against the contract regardless of what any one write door
happens to check.

## Triage a gate refusal (small-model-safe: classify and file, never fix)

Gate refusals are machine-precise; classifying them needs no judgment.
For an unminted hub, read the review page / preflight refusals and file
per class — do not mutate the hub, its edges, or its sources:

- `[grounding] no DOI` + the source HAS one (check the publisher) →
  propose the `meta.doi` backfill to a human.
- `[grounding] no DOI` + genuinely DOI-less (book, patent) → note the
  edge-stays/passage-out policy on the hub; nothing to fix.
- `[grounding] … has no DOI on record` → the passage names a DOI the
  source ref does not carry. Approve cites only the ref's own DOI (it
  overwrites a typed-in one), so backfill the ref's DOI first, then
  re-approve.
- `[grounding] datasheet has no single pdf_sha256` → the datasheet's
  PDF identity is missing or ambiguous; a re-ingest/sha-hygiene item for
  a human, not a payload fix.
- `[grounding] web page has no content_sha256 on record` → the page was
  cached before the sha was recorded; `get(kind='web', id='<url>',
  mode='refresh')` re-fetches it (stamping the sha), then re-approve.
  Re-fetch replaces the page's chunks, so re-pick the passage's chunk_id.
- `[grounding] edgar source has no parseable SEC accession` → the ref's
  slug is not a dashed accession; a re-ingest/slug-repair item for a
  human, not a hub fix.
- `[pdf-sha] 0 rows` → acquisition/backfill item
  (`docs/backlog/pdf-sha256-identifier-hygiene.md` class).
- `[pdf-sha] >1 rows` → only ambiguous when `refs.pdf_sha256` is NULL;
  file that combination, otherwise it's the benign alias pair.
- `[primary-source]` (hearsay section or in-quote citation marker) →
  hunt-the-primary todo naming the cited work; the claim stays hanging.
- `[report-attribution]` / `[report-genre]` / `[definition-copula]` →
  the sentence lacks the named shape; a human rewords it in the approve
  form. If the sentence is really an empirical result, the type is wrong:
  approve it as a claim (name the method), never loosen the check.
- `[report-source-genre]` → the report is grounded in a journal article.
  File it: either the real reporting document (proceedings, company
  report, filing) must be acquired and re-grounded, or the sentence is a
  claim.
- `[quote-verbatim]` / `[snip]` → the payload needs a human re-trim;
  point at the chunk, don't rewrite the quote yourself.

Quote-trimming, claim restructuring (atom vs composite), and every
approve/sign/signoff click stay out of scope for this triage.

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

## Correcting a claim: signing is the boundary

Before signing (candidate/reviewed), edit the claim in place with
`edit(kind='finding', id='fi<N>', title='...')` or `meta={'scope': {...}}`.
Hypotheses also accept `testable_by='...'` / `motivation='...'` with prior
prose retained in metadata history.
No version row is created. A changed claim returns its unsigned review row to
candidate and lists every direct graph user flagged for re-review: drafts,
linked findings/claims, pathways, quests and todos. Draft approval watermarks
are revoked; each affected ref stores `meta.claim_review_required['fi<N>']`
with the UTC change time and reason (numeric kinds: `view='raw'`; drafts:
`view='review'` lists notices while review is pending). Chunk-addressed users
currently cause title/scope edits to refuse atomically until reference
retargeting is supported.
No-op and scope dry-run edits leave review state unchanged.

Once signed (including anchored/published), use a new nanopublication whose
pubinfo asserts `<successor> npx:supersedes <predecessor trusty URI>`, signed with
the same key. Existing bytes and OTS proofs are immutable. `npx:retracts` is
withdrawal without a successor. The local supersede door exists only for an
anchored, unpublished predecessor (`precis nanopub supersede`, below); a
signed-only predecessor (reopen it instead, nothing is out yet) and a published
one are still refused, and until a successor is staged the in-place edit refuses
signed claims. The older reopen/demotion paths still need alignment with this
boundary; do not use them to edit a signed artifact's claim. Actual
approval/signing and publication of successors remain the normal human flow.

## Correct an anchored, unpublished artifact

Run `precis nanopub supersede FI` to stage a local candidate linked to the
previous artifact. It refuses published predecessors. Review the candidate
against current evidence, then approve and sign through the ordinary doors
using the predecessor's key. Staging performs no signing or network calls.
The successor pubinfo carries `npx:supersedes` pointing to the predecessor's
trusty URI; previous signed bytes and OTS proofs remain unchanged. Reopening
or discarding/restaging a candidate retains that predecessor obligation.
Published supersession and retraction are not supported by this door.

## Publishing state and reconcile

`precis nanopub publish --live` claims the row (`anchored` -> `publishing`,
stamped `publishing_since`) in a short transaction, POSTs with no database
transaction open, then flips `publishing` -> `published`. While `publishing`,
supersede, reopen, restage and a second publish all refuse the row. A registry
4xx response reverts it to `anchored`. A 5xx, timeout or dropped connection is
ambiguous (the registry may hold the bytes), so the row stays `publishing`:
run `precis nanopub reconcile [--older-than MINUTES]` to look up its trusty URI
at the registry (present -> `published`, 404 -> `anchored`, else untouched).

## OpenTimestamps (OTS) anchoring — the daily batch sweep

A daily `ots_sweep` pass anchors signed artifacts to Bitcoin via
OpenTimestamps (`src/precis/nanopub/ots.py`). An anchor proves existence
**no later than** T — an upper bound only, nothing about authorship — and
exists for key-rotation scoping, so granularity caps at one batch per day.
Only a 32-byte content-blind digest leaves the box. The sweep ships dark
until an operator sets `PRECIS_OTS_ENABLED`; the calendar defaults to
`DEFAULT_CALENDAR` (`PRECIS_OTS_CALENDAR` overrides), with a 30 s
per-request timeout (`CALENDAR_TIMEOUT_S` — an unreachable calendar once
hung a manual anchor for 12 h).

- **Stamp** (`ots.stamp_batch`): every publish row in `signed` whose
  artifact exists becomes one leaf; dependency-dirty and title-drifted
  rows are skipped (an anchor over soon-superseded bytes buys nothing).
  Leaf digest = `sha256(exact stored trig bytes)`, re-checked against the
  generated `byte_sha256` column; leaves sort by ascending artifact id;
  `make_merkle_tree` builds the root; **one calendar request per batch**.
  The batch row retains the construction-rule string, the Merkle root,
  each leaf's serialized inclusion path, and the root proof as `pending`
  — the leaf table plus the rule string ARE the proof and exist nowhere
  else.
- **Upgrade** (`ots.upgrade_sweep`): polls the calendar for pending
  batches; a completed proof INSERTs a new `upgraded` row (append-only —
  the `pending` row stays as history). A batch still `pending` past 3
  days (`STUCK_PENDING_DAYS`) raises the `nanopub_ots` alert
  `stuck-pending:{batch_id}` at `warn`; the remedy is
  `precis nanopub re-stamp BATCH_ID` — the raw bytes are retained, so a
  lost commitment re-anchors under a later date, and a superseded or
  late-upgraded batch's alert auto-resolves.
- **Audit** (`ots.audit`): recompute-everything detection over the
  retained bytes — `byte_sha256` vs a live sha256, the TriG re-parses,
  the trusty URI / AIDA URI / DOI extracts appear in the parsed quads,
  each leaf's inclusion path re-derives the recorded root, the latest
  proof deserializes. Any mismatch raises a `critical` `audit-mismatch`
  alert. Runs even while the network flag is dark, once proof rows exist.
