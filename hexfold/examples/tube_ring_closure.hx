hexfold 0.2
# two (5,0) tubes fused end-to-end at both pairs of ends: a torus-like
# ring whose two fuses disagree in phase (k=1 vs k=0).  registry.closure
# reports the residual: 1 step of 5 (the rim symmetry N=5), not zero.
# A topology fixture, not a geometry: two straight rigid tubes cannot
# close a ring, so the joint cycle placement stacks b exactly on a and
# check(geometry=True) reports geom.seed_overlap ERROR (gr462074). That
# ERROR is the correct answer for this spec.
a: tube(5,0, len=3)
b: tube(5,0, len=3)
a.out --fuse k=1--> b.in
a.in --fuse k=0--> b.out
