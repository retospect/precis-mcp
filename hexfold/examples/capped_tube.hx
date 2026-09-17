hexfold 0.2
prov: lib=hexfold@0.1.0

# (5,5) tube closed by a C60 hemisphere cap: cap(5,5) cuts C60
# perpendicular to a C5 axis (30 atoms, 10 dangling, 6 pentagons);
# the seam is ten hexagons.
a: tube(5,5,len=4)
b: cap(5,5)
a.out --fuse k=0--> b.in
