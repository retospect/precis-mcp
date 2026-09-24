---
name: differentiation
description: >-
  Before writing gradient-descent code for any design objective in this repo
  (shape/level-set, SIMP density, pcb placement, atomic geometry), decide how
  each term's derivative is obtained — adjoint, reverse-mode autodiff,
  forward/complex-step, or freeze-and-anneal — and how the assembled gradient
  gets verified. Repo-dev entry point; the substance is the product skill
  precis-differentiation-help.
---

# differentiation — derivative route before descent

**The full playbook is the shipped product skill.** Read it first:

```
get(kind="skill", id="precis-differentiation-help")
```

Source of truth for editing:
`src/precis/data/skills/precis-differentiation-help.md`.

## The three rules it exists to enforce

1. **Name the derivative route per cost term before coding the descent.**
   Many design variables into one scalar ⇒ adjoint or reverse mode (cost
   independent of variable count). Few variables ⇒ forward mode. Finite
   differences are a *test*, never the production gradient.
2. **Smooth or freeze every nonsmooth term.** `max`→log-sum-exp,
   `min`→softmin, `abs`→Huber, snap→smooth wells. Rounding, sorting,
   catalogue choice and `if` on a design variable get frozen and handed to
   the annealer — fake smoothness is worse than an honest discrete variable.
3. **Ship a gradient verification test with the gradient.** Directional
   dot-product test, then a Taylor remainder that quarters when the step
   halves, then an independent route (central finite differences or
   complex-step) on a few variables at several step sizes. The shipped
   density sensitivity in `src/precis/structsolve/simp.py` is gated this way
   — match it.

## Spectral graph objects — ask which of three roles

Basis (frozen eigenvectors as reduced design coordinates) ⇒ differentiable,
one projection, forward mode. Objective (an eigenvalue) ⇒ differentiable only
while it is simple; clustered eigenvalues and mode switching need a p-norm/KS
aggregate over a mode *band*. Structure decision (Fiedler cut, clustering,
coarsening) ⇒ combinatorial, annealer only. Deciding test: does it get
recomputed when the design changes? Recomputed ⇒ outer layer.

## Two failure modes that do not crash

- Differentiating a solver's *iterations* instead of its converged state.
  Differentiate the residual (implicit relation), not the loop — and never
  let the inner tolerance vary with the outer loop, or the objective changes
  shape under the optimiser.
- A non-converged inner solve returning a confident, wrong gradient: every
  derivative formula assumes the residual is zero.
