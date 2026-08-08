# material: off-sample estimate / fitting layer

Deferred from ADR 0070. Trust-ordered off-sample read: evaluate a published
correlation (a `model` value-type: Sutherland/Arrhenius/Antoine/NASA-poly —
form + coeffs + validity range) → else return bracketing sourced points →
else, only on an explicit `estimate=`, a labeled in-range interpolation
(`method='estimated'`, basis points recorded, extrapolation refused). Never a
silently-chosen fit. Define the point-query call shape + model_spec JSON +
one-sided-bracket behavior in a spec first. Owner material handler; the same
model value-type applies to component specs. Needs design.
