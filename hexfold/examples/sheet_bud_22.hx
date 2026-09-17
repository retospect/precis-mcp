hexfold 0.2
# [2+2] cycloaddition bud on a flat sheet (Nasibulin 2007): two authored
# bonds from the C60 6-6 bond onto the graphene host at one lattice site;
# the sheet rim stays a port.  Dogfood input for the se `hexfold`
# generator (docs/backlog/hexfold-integration.md step 4).
origin h
h: sheet(12,12)
b: fullerene(C60)
b @ h/(6,6,A):0 [2+2]
