hexfold 0.2
prov: lib=hexfold@0.2.0

# flanged_doughnut with six hydroxyl pendants on the Y-carbon seam atoms
# (`outer/s0`, `s4`, `s8`, `s12`, `s16`, `s20`): demonstrates `--bond-->`
# resolving `<seam>/s<i>` refs (SPEC 9, [spec 0.2]).

origin top
top: cap(24, 0) - hex(1)@(0,0,A):0
bottom: cap(24, 0) - hex(1)@(0,0,A):0
wall: tube(12, 0, len=2)
flange: cap(36, 0) - hex(3)@(0,0,A):0

top.hole --fuse k=0--> wall.in
wall.out --fuse k=0--> bottom.hole
seam outer: top.in == bottom.in == flange.hole

frag: oh0 = smiles(O)
frag: oh4 = smiles(O)
frag: oh8 = smiles(O)
frag: oh12 = smiles(O)
frag: oh16 = smiles(O)
frag: oh20 = smiles(O)
oh0.1 --bond--> outer/s0
oh4.1 --bond--> outer/s4
oh8.1 --bond--> outer/s8
oh12.1 --bond--> outer/s12
oh16.1 --bond--> outer/s16
oh20.1 --bond--> outer/s20
