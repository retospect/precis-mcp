---
id: precis-nanopub-claim-grammar-help
family: evidence
title: precis — claim-sentence grammar for taproot claims (epistemic mode, evidence verbs, tense, self-containment)
summary: the approved title of a taproot claim hub is one plain prose sentence shaped general → specific — [epistemic mode + method] + [system] + controlled evidence verb + finding — in simple present, self-contained, no author names, numbers matching the source in value and bound; write to it at authoring, approve only verifies it
answers:
  - how do I word a claim sentence so it passes approve?
  - which evidence verb do I use for a simulation vs an experiment?
  - why was my claim refused as past-passive or present-perfect?
  - may I normalize a quote to match the notation canon?
  - what does the claim title assert — the hub body or the quotes?
applies-to: authoring and rewording taproot claim hubs (put/edit kind='finding'); approve (`precis nanopub approve`) verifies conformance
status: active
tags: [workflow, troubleshooting]
kinds: [finding]
---

# precis-nanopub-claim-grammar-help — the shape of an approved claim sentence

Split from [[precis-nanopub-help]] 2026-10-13 (size cap). The mint gates,
hypothesis door, publish pipeline and correction boundary are there; this
skill is the grammar of the sentence those gates verify. Whether a sentence
is a claim at all is `precis-taproot-mint-help`'s "Claim admissibility".

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
  Spelled-out names count ("density functional theory"), and so does a
  way-of-knowing phrase built on a generic head noun — *measurements,
  simulations, calculations, spectroscopy, microscopy, experiments,
  analysis, theory, trial, imaging, assay, modelling* — with the
  specific qualifier in front: "current-voltage measurements",
  "Williamson–Hall analysis", "a randomized double-blind trial". Take
  the mode from the evidence; never guess one the source doesn't state.
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
  - *calculates / computed* — simulation/theory only; a quantitative
    model output: "DFT calculates a formation energy of 7.63 eV."
  - *estimates* — either mode; an approximate or fitted quantity,
    stated as such.
  - *reveals* — either mode; a structure or mechanism made visible.
  - *confirms* — either mode; independent corroboration of a result
    already on record.
  - *identifies* — either mode; an assignment or classification:
    "Raman spectroscopy identifies breathing-mode bands at 23 cm⁻¹."
  - *indicates* — either mode; an inference from indirect evidence —
    weaker reach than *measures/shows*, pick it only when the evidence
    really is indirect.
  Never *measures/observes/demonstrates* for a simulation; reserve
  *predicts* for forward-looking claims — a within-model comparison is
  *found*, not *predicted*.
## Claim-sentence grammar — tense and remaining rules

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
