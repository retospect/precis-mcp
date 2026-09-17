hexfold 0.2
# DB-neck (Baowan, Cox & Hill 2010): C60 hexagon hole -> (6,0) neck ->
# hexagon hole in the host; P5=9 balanced by P7=9 (3 at the bud seam,
# 6 at the host seam), single component, chi=0.
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DB-neck(3)]
