hexfold 0.2
# tube_reducer: (12,12) -> (6,6) through a coaxial 5/7 ring (SPEC 16,
# fuse{reducer}).  The menu generates a flat cap(24,0) - hex(1) washer:
# the wide rim fuses onto its outer rim ({5:12, 6:6, 7:6}), the narrow
# rim into its hole ({6:6, 7:6}): 12 pentagons, 12 heptagons.  Euler: a
# tube-to-tube annulus has chi = 0 over two rims, so pentagons =
# heptagons (reducer.rings).

origin wide
wide: tube(12,12, len=3)
narrow: tube(6,6, len=3)
wide.out --fuse{reducer}--> narrow.in
