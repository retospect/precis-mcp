hexfold 0.2
prov: lib=hexfold@0.2.0

# SPEC 6.4's acceptance example is a sheet with a capped pill above and
# a bump below, seamed at the pill's foot ring -- caps that size need
# the cap(n,m) flat-lid family (roadmap, SPEC 28), not in 0.2.  This is
# the 0.2 stand-in: the same hex(0)-hole sheet seamed to two OPEN
# tube(6,0) rims instead of capped ones (the caps follow with the
# flat-lid item).  Three sheets, one closed triple seam, no ERROR.

lattice: element=C sigma=1.42

origin s
s: sheet(20, 20) - hex(0)@(9,9,A):0
up: tube(6, 0, len=3)
down: tube(6, 0, len=3)

seam foot: s.hole == up.in == down.in
