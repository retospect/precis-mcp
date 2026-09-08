---
status: draft
title: light-driven deformation — bistable switches as discrete block states, and the spectral channel budget
prio: high
model: opus
---

# Photoswitches in the block model

Reto, 2026-09-07: small atomic modelling should support light-based
(spectrum/wavelength) deformation; model spectral overlap and HOMO–LUMO;
bistable — *"one wavelength short, stable; another wavelength long, stable
(or twist)"*; and the worry that **spectral separation for very many colours
= degrees of freedom**. Plus photo-charge coupling and band narrowing.

Chemistry evidence is being gathered (four `perplexity-research` passes,
2026-09-07). **This section is the architecture only** — it holds regardless
of which switch families the evidence favours.

## The key realisation: bistability dissolves the rigid-body problem

`boxel-exercise-tooling-gaps.md` records that block envelopes are **rigid** —
cad transforms are translate+rotate, no scale — and that this is a real limit
for molecules, which stretch and bend. Light-driven *deformation* sounds like
it needs exactly the deformable envelope we do not have.

It does not, and the reason is Reto's own framing. A **bistable** switch is
not a continuously deforming body. It is a body with **two discrete states,
each of which is rigid and thermally stable**, and light is the operator that
moves between them. Trans-azobenzene is rigid. Cis-azobenzene is rigid. The
isomerisation is a *transition*, not a deformation.

So the model we need is not deformable geometry. It is:

> a block with **N discrete conformational states**, each carrying its own
> rigid envelope and port poses, plus **transitions** between states labelled
> with what drives them.

That is representable today, and it sidesteps the rigid-body limitation
entirely rather than fighting it. It also explains why bistable (P-type)
switches matter architecturally and not just chemically: a switch that
thermally reverts is a *spring*, and a spring genuinely does need continuous
mechanics. A bistable switch is a *latch*, and a latch is discrete.

## Most of the machinery already exists

`cad` already articulates. From `precis-cad-help`:

- `joint <part> revolute|prismatic|cylindrical at:<port> limits:a..b`
- pose it: `get(..., args={"state": {"jib": 45}})`
- and the payoff probe: `view='sweep'` — *does anything collide anywhere in
  the travel*, not just in one pose.

`nm` has `declare_dof` with `("rotational", "translational")` over two named
axis ports.

What is missing is small and specific:

1. **Discrete named states**, not just a continuous parameter with limits. A
   photoswitch is `{trans, cis}`, not `0..180`. (A joint with two allowed
   values is a degenerate case, but naming the states is what lets the rest
   of this hang together.)
2. **A stimulus on the transition** — the edge from state A to state B is
   driven by *something*: a wavelength, a redox potential, a pH change. Today
   a joint's state is set by fiat; nothing records what would set it.
3. **The chemistry payload** on the switch: which photochrome, λ_on/λ_off,
   Δ(end-to-end distance) or Δangle, quantum yield, thermal half-life,
   fatigue. This is exactly a **sourced-fact** shape — see below.

## Where each piece belongs (no new kind)

- **The switch's identity and measured properties** → the `rxn`-shaped
  sourced-value pattern, or `material`. λ_max, quantum yield, thermal
  half-life, cycles-to-fatigue and Δlength are all *reported measurements
  with conditions and a citation*, which is precisely the star schema
  (registry + values, many rows per property, spread is the answer). A
  photoswitch's λ_max in solution and in a rigid matrix are different
  numbers and both are true — the multi-row design already handles that.
- **The two rigid states and the transition** → the block model
  (`nm`/`se` via `precis.blocktree`), as states + stimulus-labelled edges.
- **HOMO–LUMO, excitation energies, oscillator strengths** → computed
  results, so the attached-models layer (`analyzed-by`, mig 0153) with
  fidelity tier and staleness — same discipline as any other computed number.
- **Spectral overlap between two chromophores** → derived from the two
  spectra; a computed result, not a stored fact.

**No new kind is needed.** This is a states-and-stimulus extension to the
block model plus ordinary use of the fact store and the attached-models layer.

## The channel budget is the real design constraint

Reto: *"spectral separation for very many colours = degrees of freedom."*
This is the binding constraint on the whole idea, and the honest prior before
the evidence lands:

- organic absorption bands are broad (tens of nm FWHM);
- the usable window is roughly UV-damage floor to near-IR water absorption;
- photostationary states are never 100 %, so **crosstalk accumulates with N**;
- therefore room-temperature solution plausibly affords only a **handful** of
  orthogonal channels, not dozens.

If that holds, "one colour per DOF" does not scale, and the design must get
its degrees of freedom elsewhere. Three routes, in increasing exoticism, all
under investigation:

1. **Sequential addressing instead of parallel.** With bistable switches you
   do not need N simultaneous channels: set switch A, then switch B; A stays
   because both its states are thermally stable. This turns a bandwidth
   problem into a *program*. It is the strongest architectural reason to
   prefer P-type switches, and it costs no spectrum at all.
2. **Non-spectral axes.** Polarisation (aligned chromophores respond to the
   angle), two-photon excitation (different selection rules), dose
   thresholding, spatial/near-field addressing.
3. **Narrower lines.** Rigidified chromophores, J-aggregate exchange
   narrowing, and — the extreme — lanthanide f–f transitions (shielded 4f
   orbitals, hence nearly environment-independent lines) and persistent
   spectral hole burning, which was researched precisely as many-channel
   frequency-domain storage. Both buy channels at the price of temperature,
   matrix, or weak absorption.

**Design consequence, independent of the numbers:** the tool should treat
*the number of orthogonal channels as a scarce budget the designer spends*,
and check a proposed machine against it — the same shape as the
stock-termination DRC. A design wanting eight independently-addressed
switches at room temperature should be told it is over budget, with the
number quoted, rather than discovering it in the lab.

## Photo-charge coupling

A separate coupling mode Reto raised: light → **charge** rather than light →
geometry (photoinduced electron transfer, photoacids/photobases, photoredox).
Architecturally it is the same shape — a stimulus-driven transition between
two states — but the state variable is a formal charge rather than a pose.
`structure`'s `add_atom` already carries a declared `charge`, and the
validator's valence budget is charge-aware, so the *representation* exists at
L5. What is missing is the same as above: a labelled transition saying light
moves it.

## Evidence (four perplexity-research passes, 2026-09-07)

Cached and citable: `perplexity-research` on photoswitch families + computation,
on bistable two-wavelength switches, on spectral multiplexing capacity, and on
photo-charge coupling + band narrowing.

**Read caveat, stated so nobody over-trusts what follows:** each report is
~100 KB and paginates; under fleet load the page cursors expired before the
follow-up could run (gripe 330197). The numbers below are from each report's
own abstract — complete sections, not mid-sentence truncations — not from the
full bodies. Treat them as headline findings to verify against the cited
primary sources before anything depends on them. One report was also flagged
by the injection scan (`role-reassign` signals) and was read strictly as data.

### The channel budget — the answer to the DOF worry

Two numbers, and the gap between them is the finding:

- **Demonstrated: three to four.** Experimental multi-photochrome systems
  claiming orthogonal control "generally demonstrate on the order of three to
  four separately addressable switches rather than ten or more."
- **Ceiling at room temperature: "on the order of tens"** of spectrally
  distinct channels with low crosstalk — "reaching into the hundreds would
  require substantial physical advances."

So the pessimistic prior in the section above was roughly right for *practice*
and too pessimistic for the *ceiling*. Plan for a handful today; tens is a
research programme, not a design assumption.

**Why it caps:** no photoswitch reaches 100 % conversion, so each channel
leaves a residual population in the wrong state, and that crosstalk
**accumulates with N**. The usable window is bounded below by UV photodamage
and above by water absorption (~900–1000 nm).

### Bistability — confirmed, and the latch/spring framing is the literature's too

P-type (diarylethenes, fulgides) are thermally irreversible: "nothing changes
in the absence of light." T-type (azobenzene, spiropyran, DASA) drift back in
the dark. The report reaches the same architectural conclusion this doc
proposed independently: **"a P-type switch can act as a latching element …
whereas a T-type switch is better suited to timed relaxation or reset
functions."** So the discrete-states model is not a modelling convenience —
it is what the chemistry actually is.

Numbers worth carrying:

| | |
|---|---|
| azobenzene (T-type) | Δend-to-end ≈ **3–4 Å**, N=N bend ≈ **60°**, cis half-life **2–4 days**, φ ≈ 0.44 |
| pull–pull dithienylethene (P-type) | **>95 % PSS both directions with visible light**, φ_switch 0.15, φ_decomposition **2.6 × 10⁻⁵** (~5000× slower than switching) |
| diarylethene fatigue | >10,000 cycles typical; **23,900** and **423,000** cycles reported to 50 % bleaching |

That decomposition-to-switching ratio is the number that decides whether a
machine survives its own operation, and it belongs on the switch's fact rows
alongside λ and Δdistance.

### Narrowing bands — the escape routes, priced

- rigidified/planar chromophores at room temperature: **10–20 nm FWHM**
  (hundreds of cm⁻¹) for selected dyes;
- J-aggregate exchange narrowing, ordered solid matrices;
- **persistent spectral hole burning at low temperature: kHz-scale
  linewidths** — the frequency-domain optical storage route;
- **lanthanide 4f–4f lines**: extremely sharp because the 4f manifold is
  shielded from its environment — but **low oscillator strengths and limited
  direct coupling to mechanical change**, which is the catch.

The honest read: narrow lines and mechanical coupling pull in opposite
directions. What makes a line narrow (decoupling from the environment) is
close to what makes it useless as an actuator (no coupling to move anything).

### Photo-charge coupling

Real and reversible, with lifetimes "from picoseconds to seconds, and in some
specially engineered systems, to minutes and beyond." The design-relevant
regime is where the charge state is **bistable** — a distinct minimum
separated by a barrier — which the report identifies in rigid donor–acceptor
dyads/pentads, metastable photoacids (seconds to hours), and photoredox
high-valent metal intermediates. Architecturally this is the same
states-and-stimulus shape; only the state variable differs.

## Open, pending the evidence

- Which switch families are genuinely bistable and fatigue-resistant enough
  to build with, and the actual Δdistance each delivers.
- What the realistic orthogonal-channel count is, with citations.
- Whether spectral overlap can be computed cheaply enough for screening, or
  only for verification.
- Whether lanthanide narrow lines can be coupled to mechanical change at all,
  or are a dead end for actuation.
- What fails when a switch is embedded in a rigid scaffold — the boxel cage
  is exactly such a scaffold, so this one is directly on the critical path.
