hexfold 0.2
prov: lib=hexfold@0.2.0

# pillbox rotor = zigzag tube closed by a flat lid at each end; the
# twelve pentagons are seam rings at the lid corners.

origin a
a: tube(12, 0, len=4)
b: cap(12, 0)
c: cap(12, 0)
a.out --fuse k=0--> b.in
a.in --fuse k=0--> c.in
