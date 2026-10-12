hexfold 0.2

# Two authored pentagons on one sheet, eight cells apart (gr459928).  Each
# wedge cut runs from its core to the sheet edge; their cut regions do not
# meet, so the two surgeries compose and the sheet carries both 5-rings.
# A pair whose cuts meet in every order is `cut.crosses` (ERROR), which
# names a re-cut of the same ring along another ray that does compose.

lattice: element=C sigma=1.42

origin s
s: sheet(20, 20) + pentagon@(6,10,A):0 + pentagon@(14,10,A):0
