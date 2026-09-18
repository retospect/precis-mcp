hexfold 0.2
prov: lib=hexfold@0.2.0

# SPEC 6.4's acceptance example: a sheet with a capped pill above and a
# bump below, seamed at the pill's foot ring.  Each tube top closes over
# a cap(6,0) flat lid -- the cap(n,m) flat-lid family (SPEC 28.3, done
# 2026-09-18): the hex(0) flake fused to the tube's zigzag rim, its six
# corners becoming the fuse's six pentagons as seam rings.  Three sheets
# (s; up fused to its lid; down fused to its lid), one closed triple
# seam at the foot, no ERROR.

lattice: element=C sigma=1.42

origin s
s: sheet(20, 20) - hex(0)@(9,9,A):0
up: tube(6, 0, len=3)
down: tube(6, 0, len=3)
up_lid: cap(6, 0)
down_lid: cap(6, 0)

seam foot: s.hole == up.in == down.in
up.out --fuse k=0--> up_lid.in
down.out --fuse k=0--> down_lid.in
