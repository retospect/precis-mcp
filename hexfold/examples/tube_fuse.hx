hexfold 0.2
# two (5,0) tube ends fused tip-to-tip: the seam is five hexagons
a: tube(5,0, len=3)
b: tube(5,0, len=3)
a.out --fuse k=0--> b.in
