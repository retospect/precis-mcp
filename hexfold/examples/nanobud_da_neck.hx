hexfold 0.2
# DA-neck (Baowan, Cox & Hill 2010): C60 pentagon hole -> (5,0) neck with
# a five-heptagon seam; the neck lands on a path3 host opening (three
# removed atoms in a zigzag path -> five dangling atoms)
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DA-neck(3)]
