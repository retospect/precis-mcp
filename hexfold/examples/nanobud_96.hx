hexfold 0.2
# 9-6 junction (Wang & Li 2009): C54 = C60 minus one hexagon; its six
# dangling atoms bond onto the intact armchair host in the registration
# whose seam is three nonagons + three hexagons
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [9-6]
