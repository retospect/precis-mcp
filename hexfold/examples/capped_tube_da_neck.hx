hexfold 0.2
# Capped (5,5) tube with a DA-neck bud (Baowan, Cox & Hill 2010) on its
# flank: cap(5,5) closes one end over a ten-hexagon seam; the C60 pentagon
# hole meets a (5,0) neck that seats on a path3 host opening.  The open
# end stays a port.  Dogfood input for the se `hexfold` generator
# (docs/backlog/hexfold-integration.md step 4).
origin h
h: tube(5,5, len=14)
c: cap(5,5)
h.out --fuse k=0--> c.in
b: fullerene(C60)
b @ h/(7,0,A):0 [DA-neck(3)]
