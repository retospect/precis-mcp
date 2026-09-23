---
id: precis-differentiation-help
title: precis — how to get the derivative before you run gradient descent
summary: choosing the differentiation route for a design objective — adjoint/reverse-mode for many variables, forward/complex-step for few, finite differences for checking only; smooth surrogates for min/max/abs; implicit differentiation instead of unrolled iterations; the verification tests a gradient must pass.
answers:
  - how do I get the gradient of a design objective before descending on it?
  - should I hand-derive the adjoint, use autodiff, or use finite differences?
  - my objective has a min/max/abs/snap in it — how do I make it differentiable?
  - how do I check that a gradient is correct?
  - why is my optimiser going the wrong way even though the solver converges?
applies-to: continuous-layer design optimisation (shape, sizing, placement, geometry)
status: active
tags: [design]
kinds: [cad, se, pcb, structure]
---

# precis-differentiation-help — pick the derivative route first

Gradient descent is the easy half. The design decision is **how the
derivative is obtained**, and it is made *before* the first step, per
objective term. Picking it late is how an optimiser ends up descending a
gradient that does not belong to the objective it reports.

Order of operations for any continuous layer:

1. Write the objective as a sum of terms, each an integral or a sum over
   the domain.
2. For each term, name its derivative route from the ladder below.
3. Make every term smooth, or freeze it and hand it to the discrete
   search.
4. Verify the assembled gradient against an independent route.
5. Only then descend.

## Pick the derivative route

| Route | Use when | Cost |
|---|---|---|
| Hand-derived analytic derivative + adjoint solve | a PDE (elasticity, thermal, flow) constrains the objective, and design variables are many | one extra solve per step, independent of variable count |
| Reverse-mode autodiff over the whole cost stack | the term is already written in a differentiable array library and has no PDE constraint | ~2–4× the forward evaluation, independent of variable count |
| Forward-mode autodiff | few design variables (a feature vector: wall thickness, rib count, fillet radius), or you need directional second derivatives | one sweep per variable |
| Complex-step | verifying another route on a smooth, analytic evaluator you cannot rewrite | one complex evaluation per variable, no step-size tuning |
| Finite differences | verification only, or a black box with no other route | two evaluations per variable, step-size tuning, cancellation error |

The one decisive number is **how many design variables feed one scalar
objective**. Many variables, one number out ⇒ reverse mode or adjoint —
their cost does not grow with the variable count. Few variables ⇒ forward
mode is simpler and needs no tape.

**Finite differences are a test, not a production gradient.** They cost
one evaluation per variable, need a step size tuned per term, and lose
accuracy from cancellation exactly where you shrink the step to reduce
truncation error. Complex-step removes the cancellation (accurate down to
tiny steps, no tuning) but only works where the evaluator is analytic —
any branch, clip, or absolute value breaks it.

## Adjoint for shape and topology terms

For a boundary-shape term, the derivative is a scalar velocity on the
boundary: state solve, adjoint solve, then the two combine into the field
that moves the boundary. Every cost term adds into that same field, so
"add a term" means "add its contribution to the velocity", never "add
another optimiser".

Two consequences that decide how terms are written:

- **Pointwise maxima are not shape-differentiable.** A peak-stress term
  must be aggregated (p-norm or a smooth max over the domain) before it
  has a derivative. Pointwise stress has none.
- **Hole nucleation is a different derivative.** Moving a boundary never
  creates one. The topological derivative — the cost change from opening
  an infinitesimal hole at an interior point — is what makes nucleation a
  gradient-informed move rather than a blind one.

## Make every term smooth, or freeze it

Nonsmooth operations do not merely lose accuracy; they produce gradients
that are zero, arbitrary, or silently wrong in exactly the region where
the optimiser is working.

| Nonsmooth operation | Smooth surrogate |
|---|---|
| `max` over terms or elements | log-sum-exp |
| `min` (nearest neighbour, smallest clearance) | softmin |
| `abs`, hinge, one-sided penalty | Huber (quadratic near zero, linear outside) |
| hard threshold, indicator | sigmoid / softplus |
| snapping to a catalogue or grid value | a smooth well per allowed value, quadratic at the bottom |
| rounding, sorting, argmin, argmax, `if` on a design variable | none — freeze it |

The sharpness parameter of a surrogate is a real knob: too soft and the
term stops pulling, too sharp and it reintroduces the kink. Set it once
against the term's own scale, never adaptively during the descent.

**Default to freezing.** A variable with no honest continuous measure —
lattice type, fastener size from a catalogue, process choice, build
orientation, which interface in a loop to relax — belongs to the discrete
search, not to a fake smoothing of itself. Fake smoothness is worse than
an honest discrete variable: it converges to something that means
nothing.

Snapping is the one exception worth the work, because a preferred-value
well *is* a real continuous measure: the pull toward round values
competes with physics on the same gradient instead of being applied as an
end-of-run snap that undoes the physics.

## Never differentiate the iterations

An iterative solver's gradient is **not** the derivative of its
iterations. Unrolling and differentiating each step is expensive, and it
is wrong whenever the stopping criterion depends on the design variables.

Differentiate the *converged* state instead: treat it as the root of a
residual, differentiate that relation, and get the sensitivity from one
linear solve. This is what an adjoint already does — it differentiates
the residual, not the loop.

Two related traps:

- **A non-converged inner solve gives a confidently wrong gradient.**
  Every derivative formula assumes the residual is zero. If the inner
  solve stopped early, the gradient is faithful to a state that is not
  the answer. Keep the inner tolerance tight and *fixed* — never tighten
  or loosen it as the outer loop progresses, or the objective the
  optimiser sees changes shape under it. A cheap surrogate standing in
  for early iterations is fine; a sloppily-converged real solve is not.
- **Differentiating the discretisation vs differentiating the
  equations** gives different gradients on a coarse mesh. Differentiating
  what you actually solve is the safer default: the gradient is then
  consistent with the solver, and the descent cannot chase an
  inconsistency. Check that the optimum survives mesh refinement.

## Verify the gradient before you trust it

A wrong gradient does not crash. It converges to a wrong design and
reports a decreasing objective the whole way. Three checks, cheapest
first:

- **Directional (dot-product) test** — pick a random direction, compare
  the finite difference of the objective along it against the gradient's
  inner product with it. One extra evaluation, catches most sign and
  scaling errors.
- **Taylor remainder** — the residual after subtracting the linear term
  must shrink quadratically as the step shrinks. Halve the step, the
  remainder should quarter. This is the test that distinguishes "almost
  right" from right.
- **Independent route on a subset** — central finite differences or
  complex-step on a handful of variables, at several step sizes.
  Complex-step should agree to near machine precision; finite differences
  should agree over a middle band of step sizes and diverge at both ends.
  Divergence at both ends is expected; disagreement in the middle band is
  a bug.

Run these at a *representative* design point, not at a symmetric or empty
starting state where whole terms are inactive, and along directions that
matter physically, not only random ones. A verified gradient is a test to
keep, not a one-off check: the house's shipped density-based sensitivity
is gated against central finite differences through the whole map, and
that test is why term additions there are safe.

## Memory

Reverse mode stores intermediates for the backward pass, so memory —
not time — is usually what stops a large model. Checkpointing keeps only
selected states and recomputes the segments between them during the
backward pass: roughly a third more compute for a several-fold memory
cut. Use it when activation memory is the binding constraint; skip it
when each forward evaluation is already the expensive thing.

## What supplies the gradient at each scale

| Scale | Continuous variables | Gradient source |
|---|---|---|
| Parts / enclosure | boundary shape, wall thickness, fillet radius | shape derivative + adjoint solve |
| Assembly | linker length, mate orientation | force field |
| Atomic | atom coordinates within fixed bonding topology | analytic forces from the potential |
| Board | component positions, board outline | reverse-mode autodiff over the whole placement cost at once |

Composition is not differentiable at any scale — there is no derivative
with respect to "carbon or nitrogen". Bonding topology, layer count, and
process choice are the same: discrete, annealed, never descended.

## See also

[[precis-measures-help]] · [[precis-estimate-help]] ·
[[precis-cad-build-help]] · [[precis-pcb-help]]
