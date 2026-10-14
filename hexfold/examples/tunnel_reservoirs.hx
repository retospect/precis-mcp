hexfold 0.2
# tunnel_reservoirs: sheet | tube | sheet -- the catalysis tunnel's
# reservoir stage (docs/backlog/hexfold-nanoreactor-tube.md, slice H1):
# each reservoir sheet is pierced by a hex(1) hole seating a (6,6) tube
# end by fuse; each seam mints six heptagons.  For arbitrary rim counts
# and rail-tube ends see the opening solver (hexfold.opening) and the
# tunnel-reservoirs scene feature.
a: sheet(20,20) - hex(1)@(10,10,A):0
t: tube(6,6, len=6)
b: sheet(20,20) - hex(1)@(10,10,A):0
a.hole --fuse k=0--> t.in
t.out --fuse k=0--> b.hole
