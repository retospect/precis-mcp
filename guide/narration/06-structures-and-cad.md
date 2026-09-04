# Structures and CAD

An atomic structure or a CAD part is never a picture to the model —
it's typed data, atoms and bonds, or a parametric solid, reasoned
about with analytical probes instead of image recognition recovering a
flattened projection. That's what lets an agent build these things,
not describe one.

You still get to see it. The structures tab renders an interactive
three dimensional cell — atoms colored by element, bonds as clickable
cylinders — beside a run cube: every simulation pass at every
fidelity, quick relaxation through full density functional theory,
including a cached run that cost nothing to reuse.

CAD works the same way: rotate the solid, click a feature, tell it
what to change in plain language, and a proposal comes back to review
before it's applied — exported as an STL, a STEP file, or an OpenSCAD
script.
