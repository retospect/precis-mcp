hexfold 0.2
prov: lib=hexfold@0.2.0

# flanged flat doughnut: two cap(24,0) washers joined through a (12,0)
# tube wall, closed at the outer equator by a cap(36,0) annulus meeting
# both washers in a k=3 seam.  The 24 seam atoms (`outer/s<i>`, SPEC 9)
# are the Y carbons of rotary-ratchet-valve.md: trivalent, one bond into
# each of three sheets, the natural functionalisation sites.

origin top
top: cap(24, 0) - hex(1)@(0,0,A):0
bottom: cap(24, 0) - hex(1)@(0,0,A):0
wall: tube(12, 0, len=2)
flange: cap(36, 0) - hex(3)@(0,0,A):0

top.hole --fuse k=0--> wall.in
wall.out --fuse k=0--> bottom.hole
seam outer: top.in == bottom.in == flange.hole
