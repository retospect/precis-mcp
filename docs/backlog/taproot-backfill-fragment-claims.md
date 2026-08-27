---
status: draft
title: backfill cite-group segmentation mints fragment/meta claim hubs from literature-narrative prose
---

# Backfill segmentation mints fragment claim hubs

## Motivation / why

`segment_cite_groups` defines a claim as "the prose since the previous cite
marker, or chunk start" — deliberately, and its docstring argues the case: the
cite markers are a better, deterministic segmenter than a sentence splitter,
and "a claim is whatever a citation grounds."

That premise holds for **citation-follows-claim** prose, which is what it was
designed against:

> Nanobuds exhibit enhanced field emission[pa1]. Their surfaces are more
> reactive than pristine CNTs[pa2].

Two cites, two self-contained claims, two clean spans.

It breaks on **serial-cite literature narrative**, which is what a
lit-review paragraph actually looks like. From `dc2445930` (nanobud draft):

> …Anoshkin et al. demonstrated that ‹A›[pa1], subsequently into flexible
> transparent conductors and touch sensors[pa2], and later ultra-sensitive
> detection of B9 and B12[pa3].

Span 1 gets the subject and the verb. Spans 2 and 3 get **continuation
clauses** — grammatically dependent on span 1, meaningless standing alone.
`extract_claim` (SMALL tier) is then handed
`"subsequently into flexible transparent conductors and touch sensors"` and,
instead of returning `NO-CLAIM`, either restates the fragment verbatim
(→ `fi191261`) or describes it (→ `fi191180`, "The passage describes…"). Both
become claim hubs.

Three consequences, all observed live:

1. **Vacuous hubs.** A hub whose `title` is a dependent clause or a
   text-about-text gloss asserts nothing about the world, so no evidence can
   support it and no reader can check it.
2. **Bad grounding, same cause.** A bibliographic gloss is a restatement of the
   cited paper's *title*, so the fragment's best unigram overlap in that paper
   is its title block — which is how gripe 245842's front-matter groundings
   arose. That gripe's fix (a prose gate on the candidate pool) suppresses the
   symptom: these now skip rather than mis-ground. The fragment hub is still
   minted.
3. **Dedup defeated.** A fragment does not ANN-match its own full-sentence
   twin, so the pass mints near-duplicate hubs (`fi191180`/`fi191261`,
   `fi191265`/`fi191257`). Those pairs are the dedup sweep's problem
   (`claim-hub-dedup-sweep.md`), but this is where they come from.

Measured 2026-08-25 over the live `origin=draft-backfill` edges (62 edges, 57
hubs): 4 RETIRE-bucket hubs, 3 of them from this one draft chunk. `fi245753`
("dopaminergic degeneration diminishes ventilatory drive", Parkinson draft) is
a fragment grounded on perfectly good body prose — so this class is **wider**
than gripe 245842's title-grounded class and is not fixed by it.

## In scope

Stop minting a claim hub from a span that carries no subject-verb core.

Three candidate shapes, to be decided (see the decisions log):

- **Extend the span** to the enclosing clause/sentence boundary when it has no
  finite verb, so the fragment inherits its subject. Cheapest, deterministic —
  but weakens the "the cite is the anchor" invariant the module is built on,
  and two cites in one sentence would then extract overlapping claims.
- **Reject the span** — return the empty `ClaimExtraction` (`NO-CLAIM`) when
  the span has no subject-verb core, leaving the prose as `[pa]`/`[pc]`. Safe
  and cheap; loses the citation until the draft prose is rewritten.
- **Escalate the extraction** to `extract_claim_strict_big` on a suspicious
  span — the module already carries that hook for "extractions the SMALL pass
  got wrong". Kills the `The passage describes…` class, but a genuinely
  subject-less fragment has no claim for any tier to find.

## Explicitly NOT in scope

- Retiring / re-grounding the hubs already minted — that is the triage pass,
  `docs/runbooks/taproot-backfill-hub-triage.md`.
- The near-duplicate pairs this produced — `claim-hub-dedup-sweep.md`.
- The grounding-prose gate — shipped under gripe 245842.
- Rewriting the drafts' literature-narrative prose.

## Acceptance criteria

- A cite-group span with no subject-verb core does not mint a claim hub; the
  plan reports it distinctly (not silently as `no-claim`, which already means
  "the extractor found nothing") and leaves the prose untouched.
- Regression over the real shape: a paragraph of the form
  `X et al. showed A[pa1], subsequently B[pa2], and later C[pa3]` mints at most
  one hub per *assertion*, never a hub titled `subsequently …` or
  `The passage describes …`.
- Re-running the triage runbook over the backfill edges shows an empty RETIRE
  bucket for chunks processed after the fix.
- A span that IS a self-contained claim still mints exactly as today — the
  existing `segment_cite_groups` tests stay green unmodified.

## Target + blast radius

`src/precis/taproot/backfill.py` (`segment_cite_groups`, `_run_cascade`),
possibly `precis.taproot.canon.extract_claim`'s prompt. Consumers: the
`taproot_backfill` worker job, `precis taproot backfill` CLI, the web
convert-cites route. No schema change.

## Figure captions are a systematic generator of this bug (2026-08-27)

A second, cleaner shape than literature-narrative prose, found while working
the nanobud remediation (td263083 / td264356): **figure captions**.

A caption ends in an attribution line — `Reproduced from [pa2069].` — and that
is a cite marker like any other, so `segment_cite_groups` hands the extractor
"the prose since the previous marker", i.e. the entire caption, as a claim
grounded by that cite. Captions describe an *image*, so what comes back is
reliably a meta-claim. `dr173020` ords 181-190 are ten consecutive caption
chunks of exactly this form, and fi189540 — "The passage illustrates four
representative nanobud junction geometries and shows transmission electron
microscopy images of the original NanoBuds at increasing magnification" — is
the minted result of one of them.

This shape is worth calling out separately because it is **mechanically
detectable**, unlike the narrative case: the span is a caption, the trailing
marker is an attribution rather than an evidential cite, and no subject-verb
or NO-CLAIM judgement is needed to skip it. If the fix ends up being a
segmentation change rather than a prompt change, captions are the cheapest
first cut.

It also settles a related question the other way. The nanobud todo carried an
item to convert those same ten `[pa<id>]` markers to `[fi<id>]` for
provenance-ladder granularity. That would be **wrong**: you reproduce a figure
from a *paper*, not from a *proposition*, so the bare `[pa]` is the correct
citation there and the item is mis-specified. Claim-level provenance in a
caption would mean adding a cite to the descriptive sentence, leaving the
"Reproduced from" attribution alone — a prose rewrite, not a marker swap.

## Open questions / decisions log

- Which of the three shapes? Needs more evidence than one draft chunk — run
  the triage runbook over td244453's remaining ~236 Parkinson chunks first and
  count the fragment class at corpus scale.
- Does a subject-verb test need a parser, or does the extractor's own
  `NO-CLAIM` verdict suffice once the span is presented honestly (i.e. is this
  a segmentation fix or a prompt fix)?
