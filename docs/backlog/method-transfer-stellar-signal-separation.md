---
status: idea
pillar: 3d-design
---

# Method transfer — separating entangled signals, from stellar RV work

Source: Alexander Shapiro's talk at the OePG-CMD Joint Meeting 2026, Graz
(notes: draft `graz-oepg-cmd-2026`, section `dc4086718`). His group separates a
planet's radial-velocity signal from stellar surface convection, a problem
structurally identical to several in this repo: one wanted signal buried in a
coherent, non-averageable confounder. Four moves look transferable.

1. **Ergodic substitution.** Sowmya et al. (2025), *Sensitivity of Spectral
   Lines to Granulation: The Sun* (`pa448055`, doi 10.3847/1538-4357/ae6102)
   compute *temporal* variability from the *spatial* variability of one
   snapshot, replacing a long trajectory with a single expensive frame.
   Candidate wherever we run a long simulation to get a variance over a
   statistically homogeneous field.

2. **Ablated synthetic training data.** Frame et al. (2026), *Synthetic
   disk-integrated absorption lines isolating stellar granulation for
   high-precision RV studies* (`pa448047`, doi 10.1093/mnras/stag418) build
   1000 model-star realisations per spectral line with only granulation
   switched on, magnetism and oscillations deliberately off, giving labelled
   ground truth for one confounder in isolation. General recipe: don't fit the
   mixture, simulate each term alone. Relevant to LLM-judge reliability
   (`llm-judge-reliability-data/`) and to any evaluation where confounders
   co-vary in real data.

3. **One resolved instance calibrates an unresolvable population.** The Sun is
   spatially resolved, so solar-calibrated physics is what the stellar forward
   models are built from: Işık et al. (2018), *Forward modelling of brightness
   variations in Sun-like stars I* (`pa448042`, doi 10.1051/0004-6361/201833393)
   carries solar flux-emergence and surface-transport relations over to stars
   of other rotation rates; Işık et al. (2020) (`pa448043`, doi
   10.3847/2041-8213/abb409) uses it to explain Kepler stars whose variability
   the Sun does not match. Maps onto having one fully-instrumented example and
   a population of partial observations.

4. **Find a separating invariant, not a better fit.** A planet shifts a
   spectral line rigidly; granulation changes its shape, line-dependently. They
   hunted for the diagnostic that responds to one and not the other instead of
   improving the model of the sum. The precondition is knowing the confounder
   budget is closed — Shapiro et al. (2017), *The nature of solar brightness
   variations* (`pa448039`, doi 10.1038/s41550-017-0217-y) establishes that
   surface magnetism and granulation together account for all observed solar
   brightness variation across six orders of magnitude in timescale, so nothing
   unmodelled is hiding in the residual.

Further context from the same programme, not itself a transferable move:
Shapiro et al. (2011) long-term irradiance reconstruction (`pa448041`, doi
10.1051/0004-6361/201016173); Shapiro et al. (2020) solar-cycle irradiance over
four billion years (`pa448044`, doi 10.1051/0004-6361/201937128); Dalal et al.
(2023) stellar metallicity and surface UV (`pa448040`, doi
10.1038/s41467-023-37195-4).

Not yet scoped to a deliverable: the value is deciding which repo problem each
move actually lands on. Compare `multiscale-optimisation-method.md`, the
existing method-transfer note. Design call, Opus-tier; no owner file yet.
