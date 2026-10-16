hexfold 0.2
# tunnel_two_steps: sheet | (18,18) -> (12,12) -> (6,6) | sheet -- the
# multi-radius catalysis tunnel (docs/backlog/hexfold-nanoreactor-tube.md,
# slice H2).  Each step is a fuse{reducer} (a coaxial 5/7 ring, equal
# pentagons and heptagons: 24 + 24, then 12 + 12); both ends seat into reservoir
# sheets by opening(port=) -- the wide mouth into a, the narrow into b.
a: sheet(40,40) - opening(port=t1.in)@(20,20,A)
t1: tube(18,18, len=3)
t2: tube(12,12, len=3)
t3: tube(6,6, len=3)
b: sheet(30,30) - opening(port=t3.out)@(15,15,A)
t1.out --fuse{reducer}--> t2.in
t2.out --fuse{reducer}--> t3.in
