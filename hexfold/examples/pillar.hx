hexfold 0.2
prov: lib=hexfold@0.1.0

lattice: element=C sigma=1.42

origin substrate
substrate: sheet(20,20) - hex(0)@(9,9,A):0
post: tube(6,0,len=4)

substrate.hole --fuse k=0--> post.in

registry: post.in == post.out
terminate: substrate.rim = H
terminate: post.out = H
