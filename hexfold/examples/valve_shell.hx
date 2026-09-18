hexfold 0.2
prov: lib=hexfold@0.2.0

# rotary-ratchet-valve shell (SPEC 28.3): (12,0) neck -> flat washer ->
# (24,0) bulge with two C2 wall holes -> washer -> (12,0) neck.  Each
# washer is a cap(24,0) lid with a hex(1) hole: fusing the neck into the
# hole mints six heptagons, fusing the bulge onto the lid rim mints six
# pentagons, all as seam rings (net charge 0).  Every seam is a C6 orbit.
# (24,0) clears the lid_pillbox (12,0) rotor by a 4.7 A radial gap; a
# (18,0) bulge would clash (2.35 A) and cannot be a single washer
# (cap(18,0) - hex(1) clips its own rim: cut.overlap).  Hole sites: on
# (n,0) the circumferential coordinate is u/n, so u and u+12 are the C2
# pair; v=-3 lands in axial cell 1.

origin neck_a
neck_a: tube(12, 0, len=3)
step_a: cap(24, 0) - hex(1)@(0,0,A):0
bulge: tube(24, 0, len=fit) - hexagon@(6,-3,A):0 - hexagon@(18,-3,A):0
step_b: cap(24, 0) - hex(1)@(0,0,A):0
neck_b: tube(12, 0, len=3)

neck_a.out --fuse k=0--> step_a.hole
step_a.in --fuse k=0--> bulge.in
bulge.out --fuse k=0--> step_b.in
step_b.hole --fuse k=0--> neck_b.in
