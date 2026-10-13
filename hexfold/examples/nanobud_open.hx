hexfold 0.2
prov: lib=hexfold@0.1.0

lattice: element=C sigma=1.42

# an open-topped, H-terminated nanobud: sheet -> fillet -> tube -> sphere
# whose polar lid is omitted, so the bulge's open rim is H-terminated and
# the ring of H sticks out at the top where it can be instrumented
# (Reto 2026-10-13). The foot is the 3+3 frustum the scene planner writes
# for n=12 (cap(36,0) + 3@, a hex(3) hole); the top is the washer route of
# the sphere n=12 plan row k=4 L=3 — six heptagons at the washer seam, six
# pentagons at the bulge foot, none at the open top.
origin s
s: sheet(20,20) - hex(2)@(10,10,A):0
f: cap(36,0) + 3@(-1,0,A):2 - hex(3)@(-1,0,A):0
t: tube(12,0, len=4)
w: cap(24,0) - hex(1)@(0,0,A):0
b: tube(24,0, len=3)

s.hole --fuse k=0--> f.in
f.hole --fuse k=0--> t.in
t.out --fuse k=0--> w.hole
w.in --fuse k=0--> b.in

terminate: s.rim = H
terminate: b.out = H
