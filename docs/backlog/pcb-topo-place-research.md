---
status: draft
title: "pcb topo-place research appendix — topological routing rules (R1–R15) and the 4-plane model"
pillar: 3d-design
prio: high
---

# pcb topo-place research appendix

Research behind [pcb-topo-place](pcb-topo-place.md) (gr515107). Not an
item on its own: it ships or dies with the spec. Part A is the rule set
(R1–R15); part B is the four-plane model built on it and cites rules by
number. Cross-checks: `perplexity-research:514678` (part A §A4) and
`perplexity-reasoning:514915` (part B §B6); neither is a primary source.

Every DOI was resolved against Crossref on 2026-10-24. Tags:
**[held paN]** full text in precis; **[stub N]** paper stub `paper:N`,
fetch queued (ids 514925–514942); **[DOI]** verified, not pulled;
**[no DOI]**.

# Part A — Topological routing: rules that decide routability and layer assignment before geometry

Mapped to `src/precis/pcb/route.py`, `place.py`, `layer_lock.py`,
`escape.py`.

## A0. Which branch of mathematics this is

Not knot theory. The branches are **topological graph theory**
(planarity, crossing number, thickness, book embeddings, circle graphs),
**computational topology of the punctured plane** (homotopy classes of
paths among obstacles: homotopic / rubber-band routing) and **VLSI layout
theory** (channel and river routing, via minimisation, layer assignment).
Knot theory does not apply, and the reason is the most useful rule (R13):
nets are **open arcs with free endpoints**, so no knot- or link-theoretic
obstruction stops routing anything on two layers; a via resolves every
crossing. The obstruction is cost (vias) and capacity (track density, pad
shadows), never topology. Braid groups appear only in the
two-parallel-pin-rows channel case (R14).

Terms: a **topology** is a drawing of each net as a plane tree with pins
as leaves, up to homotopy; a **crossing** is a transverse intersection of
two different nets; the **crossing (conflict) graph** C has a vertex per
net (or per wire segment) and an edge per crossing.

## A1. Rules (statement; exact or heuristic; complexity; source)

### Planarity

**R1. One layer, no vias = planarity with the given pin rotation.**
Routable on one layer iff the net graph (pins plus a hub vertex per net,
obstacles as fixed faces) is planar *and* the embedding respects the
cyclic pin order around every footprint. Abstract planarity: Kuratowski
1930 / Wagner 1937; O(n) test with certificate: Hopcroft–Tarjan 1974, JACM
21(4) (10.1145/321850.321852 [DOI]); Boyer–Myrvold 2004, JGAA 8(3)
(10.7155/jgaa.00091 [stub 514933]; `networkx.check_planarity`). Fixed
rotation system, still O(n): Angelini et al., "Testing planarity of
partially embedded graphs", SODA 2010 (10.1137/1.9781611973075.19) / ACM
TALG 2015 (10.1145/2629341 [DOI]). *Exact.* Abstract planarity alone is
necessary, not sufficient, on a PCB. Pach–Wenger 2001 (Graphs Combin.,
10.1007/pl00007258 [DOI]): any planar graph embeds with curved edges on
*any* fixed vertex positions, so the pin rotation, not the pin
coordinates, is the real constraint.

**R2. k layers, whole nets, free redrawing = graph thickness, not
colouring.** Via-free k-layer realisability with whole nets is θ(G) <= k;
θ(G) <= 2 is NP-complete (Mansfield 1983, Math. Proc. Camb. Phil. Soc.
93(1), 10.1017/S030500410006028X [stub 514934]). Shared vertex positions
give **geometric thickness** (Duncan–Eppstein–Kobourov 2004, SoCG,
10.1145/997817.997868 [DOI]). *Exact, intractable; upper-bound model only
on a PCB.*

**R3. Crossing number.** NP-complete (Garey–Johnson 1983, SIAM J. Alg.
Disc. Meth. 4(3), 10.1137/0604033 [DOI]). Lower-bounds crossings over all
topologies; not a layer, colour or via count. `place.py::_objective`'s
straight-airwire count (`W_CROSS`) is a heuristic proxy: fine for
placement, not a verdict.

### Pins on a boundary: chords, circle graphs, books

**R4. Boundary pins → circle graph.** All pins on one closed boundary: two
two-terminal nets must cross iff their endpoints interleave (Jordan-curve
fact, independent of straight chords). C is a circle graph; multi-terminal
nets conflict iff their pin sets alternate A,B,A,B. O(n log n). *Exact.*
Hsu 1983, "Minimum-via topological routing", IEEE TCAD 2(4)
(10.1109/TCAD.1983.1270041 [DOI]); Marek-Sadowska 1984, IEEE TCAD 3(3)
(10.1109/TCAD.1984.1270074 [stub 514940]).

**R5. One layer on a boundary = non-interleaving = independent set.**
Reto's first rule, exactly (balanced parentheses). Largest via-free subset
= maximum independent set of the circle graph, O(n^2) DP: Gavril 1973,
Networks 3(3) (10.1002/net.3230030305 [stub 514930]). *Exact.*

**R6. k layers, fixed topology, whole nets = k-colouring of C.** k=2:
bipartiteness, O(V+E), certificate = odd cycle. Parity proof: each
crossing is x_u + x_v = 1 (mod 2); summing round a cycle of length r
forces r even, so any odd cycle (not only triangles) is the obstruction.
*Exact.* Circle-graph colouring: chromatic number NP-complete
(Garey–Johnson–Miller–Papadimitriou 1980, 10.1137/0601025 [DOI]); k >= 4
NP-complete (Unger 1988, STACS, LNCS 294, 10.1007/BFb0035832 [stub
514931]); **k=3 open**: Unger 1992 refuted by Bachmann–Rutter–Stumpf, "On
3-coloring circle graphs", JGAA 28(1) 2024 (10.7155/jgaa.v28i1.2991 [stub
514932]). Circle graphs are quadratically χ-bounded (Davies–McCarty 2020,
Bull. LMS, 10.1112/blms.12447 [DOI]). General (non-boundary) conflict
graphs: 3-colouring NP-complete.

**R7. Book embedding: pins on a spine, layers as pages.** Bernhart–Kainen
1979, JCTB 27(3) (10.1016/0095-8956(79)90021-2 [DOI]). One page iff
outerplanar; two iff subhamiltonian; page number <= 2 NP-complete and the
pages-as-layers VLSI reading: Chung–Leighton–Rosenberg 1987, SIAM J. Alg.
Disc. Meth. 8(1) (10.1137/0608002 [DOI]); 4 pages suffice for planar
graphs (Yannakakis 1989, JCSS 38(1), 10.1016/0022-0000(89)90032-9 [DOI]),
tight (Bekos et al. 2020, J. Comput. Geom. 11(1); arXiv:2004.07630 [no DOI
verified]). **Spine order fixed** (the PCB case): k-page = circle-graph
k-colouring (Bachmann–Rutter–Stumpf). *Exact.*

**R8. River routing: cut condition.** Two facing rows, same net order (any
inversion forces a crossing, R14), one layer. Unit pitch: t tracks suffice
iff for all i, b_{i+t} - a_i >= t and a_{i+t} - b_i >= t; min t in O(n).
*Exact.* Leiserson–Pinter 1983, SIAM J. Comput. 12(3) (10.1137/0212029
[DOI]); Dolev–Karplus–Siegel–Strong–Ullman 1981, STOC
(10.1145/800076.802484 [DOI]); Cole–Siegel 1984, FOCS
(10.1109/SFCS.1984.715902 [stub 514929]) for arbitrary module shapes.
`escape.py::_layers_for_escape` is this condition on concentric shells;
the theorem needs it on **every** cut.

### Via minimisation

**R9. Constrained via minimisation (CVM) = planar max-cut.** Topology
fixed; cut nets at candidate via points; crossing segments must differ in
layer (hard), consecutive same-net segments prefer equality (one via per
violation). Contract maximal via-free clusters; weight cluster edges by
candidate via sites; a valid 2-layer assignment is a cut of the **cluster
graph**, min vias = max cut. Posed by Hashimoto–Stevens 1971, DAC
(10.1145/800158.805069 [DOI]) and Kajitani 1980 (ICCC [no DOI]). Junction
degree <= 3 makes the cluster graph planar; planar max-cut is polynomial:
Hadlock 1975, SIAM J. Comput. 4(3) (10.1137/0204019 [stub 514927]);
O(n^{3/2} log n): Shih–Wu–Kuo 1990, IEEE Trans. Comput. 39(5)
(10.1109/12.53581 [DOI]). Pinter 1982, "Optimal layer assignment for
interconnect", ICCC pp. 398–401 [no DOI; journal version J. VLSI Comput.
Syst. 1(2) 1984, no DOI]; Chen–Kajitani–Chan 1983, IEEE Trans. CAS 30(5)
(10.1109/TCS.1983.1085357 [stub 514938]); Chang–Du 1987, "Efficient
algorithms for layer assignment problem", IEEE TCAD 6(1)
(10.1109/TCAD.1987.1270247 [stub 514925]). Junction degree >= 4:
NP-complete: Naclerio–Masuda–Nakajima 1989, IEEE Trans. Comput. 38(11)
(10.1109/12.42135 [stub 514926]); Choi–Nakajima–Rim 1989, SIAM J. Disc.
Math 2(1) (10.1137/0402004 [stub 514939]). Three layers NP-complete:
Chang–Du 1988, IEEE Trans. Comput. 37(5) (10.1109/12.4616 [stub 514941]).
Modern exact solver: Liers–Nieberg–Pardella 2011, Univ. Köln
zaik2011-630 [no DOI]. *Exact.*

**R10. Unconstrained / topological via minimisation (TVM).** Topology and
layers chosen jointly: NP-complete for general k (Cong–Liu 1991, "On the
k-layer planar subset and topological via minimization problems", IEEE
TCAD 10(8), 10.1109/43.85735 [stub 514935]). Two exact facts: two-terminal
nets on two layers need **at most one via per net** at optimum
(Marek-Sadowska 1984), so TVM = maximise via-free nets; on one boundary
that maximum 2-planar subset is polynomial: Sarrafzadeh–Lee 1989, IEEE
TCAD 8(8) (10.1109/43.31548 [stub 514937]); Sarrafzadeh–Lee 1991, IEEE
Trans. Comput. 40(11) (10.1109/12.102839 [stub 514942]);
Rim–Kashiwabara–Nakajima 1989, "Exact algorithms for multilayer
topological via minimization", IEEE TCAD 8(11) (10.1109/43.41502 [stub
514936]). Hsu 1983 is the original heuristic.

### Homotopic / rubber-band routing

**R11. Homotopic routability = cut condition on taut representatives.**
Fix each wire's homotopy class relative to obstacles. A single-layer
wiring with given spacing exists iff for every cut the wires forced across
it, counted on the shortest ("rubber-band equivalent") representative, fit
the cut; necessary and sufficient; polynomial (O(N log N) in Maley's
refinement). *Exact.* Leiserson–Maley 1985, STOC (10.1145/22145.22153
**[held pa266315]**); Maley 1990, *Single-Layer Wire Routing and
Compaction*, MIT Press [no DOI]; Gao–Jerrum–Kaufmann–Mehlhorn–Ruelling
1988, SoCG (10.1145/73393.73433 [DOI]); Schrijver 1991, "Disjoint homotopic
paths and trees in a planar graph", Discrete Comput. Geom. 6
(10.1007/BF02574704 [stub 514928]). The right notion of "don't cross" is
the homotopy class after pulling taut, not the straight airwire.

**R12. Rubber-band sketch: topology first, geometry last.** SURF: rubber
bands on a constrained Delaunay triangulation; per-layer routability = R11
on triangulation edges; layer assignment splits Steiner trees into
sections, assigns sections, inserts vias where adjacent sections differ
(cost K_v · vias + wiring, steepest descent: *heuristic*); geometry last.
Dai–Dayan–Staepelaere 1991, "Topological routing in SURF: generating a
rubber-band sketch", DAC (10.1145/127601.127622 **[held pa266319]**);
Dai–Kong–Jue–Sato 1990, ICCAD (10.1109/iccad.1990.129838 **[held
pa266318]**); Staepelaere–Jue–Dayan–Dai 1993, IEEE Design & Test 10(4)
(10.1109/54.245960 **[held pa266323]**); Dayan–Dai 1993, "Layer assignment
for rubber band routing", UCSC-CRL-93-04 [no DOI; PDF at tr.soe.ucsc.edu];
Staepelaere 1992, UCSC-CRL-92-39 [no DOI]. Dayan's PhD thesis (UCSC, c.
1997) unverified. Descendants: Yu–Dai 1995, ICCAD
(10.1109/iccad.1995.480175 [DOI]); gEDA Toporouter (Blake 2009); TopoR;
Kong et al. 2021 (arXiv:2105.07892).

### Knots, braids, and "flip from the back"

**R13. No knot obstruction; two layers plus vias always suffice.** Moving
one strand of a crossing to the other layer is a *crossing change* (the
unknotting operation); for open arcs the count is zero topologically and
purely a cost. Any topology with c crossings routes on two layers with
<= 2c vias. *Exact, elementary.*

**R14. Channels and braids: inversions are the exact crossing count.**
Between two parallel rows in orders id and π, every planar drawing has
>= inv(π) crossings, achievable with monotone wires; crossing parity =
sgn(π). A two-layer channel routing is a braid word for π; the braid
relation is Reidemeister III, the triple-point slide rubber-band routers
exploit. Artin 1925. *Exact* in the channel; elsewhere use R11.

**R15. Reto's flip rule, formalised.** C = crossing graph of the committed
topology; D(n) ⊆ {Top, Bottom} allowed layers per net (through-hole: both;
SMD-only: one, from `layer_lock.py`).

1. If C is bipartite and forced colours agree with one side of the
   bipartition in every component, the board routes on two layers with no
   inter-pad vias. List-2-colouring, O(V+E).
2. Otherwise an odd cycle, or a component forced to both colours: some
   net must be **split by a via between crossings**; splitting replaces one
   vertex of C by segment vertices and breaks every odd cycle whose
   crossings fall on different sides of the cut. Not "impossible on two
   layers".
3. Via cost of one flip: whole net on the other side between two
   through-hole pins, 0; between two SMD pads, 2; a local excursion
   returning to its layer, 2; excursion ending at a through-hole pin, 1;
   one excursion covering several crossings, 2 total. Per net
   V = [x_0 ∉ A_s] + Σ [x_j ≠ x_{j+1}] + [x_m ∉ A_t].
4. Minimum total = R9. Bracket: lower bound = maximum edge-disjoint odd
   cycles in C (no "one via per odd cycle" theorem: one split can kill
   several cycles); upper bound = 2c or the greedy "flip the net with most
   conflicts" sweep.
5. Vias consume annular-ring area and lower the cut capacities of R8/R11:
   colour first, minimise splits, then re-run the cut condition per layer
   with vias as obstacles.

## A2. Algorithm order for a code-first router

1. **Topology.** MST (or Steiner) per net; constrained Delaunay
   triangulation of pins and obstacles; airwire = dual-edge path (homotopy
   class). O(n log n).
2. **Crossing graph.** Boundary-pin regions: interleaving on cyclic pin
   order (R4). Elsewhere: crossings of taut representatives.
3. **Per-layer check.** Independent set of C; Boyer–Myrvold plus rotation
   system (R1) and the cut condition on triangulation edges (R11).
4. **Colour.** List-2-colouring with forced colours (R15.1); DSATUR for
   inner layers; emit odd-cycle certificates.
5. **Vias for odd cycles.** Planar max-cut where junction degree <= 3
   (R9), else branch-and-bound / greedy odd-cycle transversal with the
   lower bound (R15.4).
6. **Re-check capacity** with vias as obstacles (R8/R11); iterate 4–6
   once.
7. **Geometry.** Freerouting (`route.py::route_dsn`) or a
   rubber-band-to-geometry pass.

## A3. What to implement first (mapped to precis)

1. **Interleaving crossing test for boundary pins** (R4) in
   `place.py::route_feasibility`, replacing straight-segment
   `ratsnest.crossings` for pins on one outline or connector row.
2. **Bipartite test with forced layers** (R15.1): crossing graph plus
   `LayerLockReport` → bipartition or odd cycle; replace the H/V split;
   report `vias_estimate` as the transversal bracket.
3. **Odd-cycle transversal bracket** (R15.4), printed by
   `view='feasibility'` before Freerouting.
4. **Cut condition on every pad gap** (R8): extend
   `escape.py::_layers_for_escape` from shells to every adjacent-pad gap.
5. **Triangulation homotopy classes** (R11/R12): constrained Delaunay;
   prerequisite for 1–3 being placement-stable.
6. **Planar max-cut for junction degree <= 3** (R9): T-join (Hadlock) or
   a small ILP; validator for item 3.
7. **Placement objective** (R3): interleaving-crossing and odd-cycle terms
   beside `W_CROSS` in `place.py::_objective`.
8. **Certificates**: every negative verdict carries the odd cycle (net
   names) or the violated cut (pad pair, demand, capacity).

## A4. Cross-check against perplexity-research:514678

Agrees: the planarity / crossing-number / circle-graph / bipartiteness /
page-number hierarchy; 3-colouring of circle graphs open; CVM as Pinter's
two-colour-contract-planar-max-cut with junction degree <= 3; Chang–Du
heuristics; 3-layer NP-hardness; one-via-per-net TVM; river inequalities;
Leiserson–Maley cut condition; SURF pipeline; inversions and braid words;
knot theory inapplicable. Its via-cost table is R15.3. Its bottom line
matches: no rule set decides real routability from netlist topology alone;
exact criteria exist only once the routing model (widths, terminals,
layer changes, capacities) is fixed.

Added from it: graph thickness (R2); XOR parity (R6); explicit river
inequalities (R8).

Flags: (a) θ(G) <= k framed without the shared-pin-position caveat (R2).
(b) Its braid/knot section rests on lecture notes, Wikipedia and a GitHub
README, not primary sources. (c) Its SURF DOI 10.1145/127601.127629
resolves to Hu et al., "Constrained via minimization with practical
considerations for multi-layer VLSI/PCB" (same DAC); the SURF paper is
10.1145/127601.127622 (verified, held). (d) Pinter's O(n^{2.5}) is
attributed to an unlinked thesis; plausible, unverified. (e) It omits
Hadlock, Schrijver, Cole–Siegel, Boyer–Myrvold, Gavril and the degree
>= 4 primaries.

DOI corrections after Crossref resolution (11 of 38 first-draft DOIs were
off by one digit or pointed at a neighbouring paper): Hsu 1983,
Marek-Sadowska 1984, Naclerio 1989, Chang–Du 1988, Sarrafzadeh–Lee 1989,
Cong–Liu 1991, Rim et al. 1989, Schrijver 1991, SURF 1991, SURF 1993,
Angelini et al. Unverified: Dayan thesis year; Bekos 2020 JoCG DOI; exact
exponent of the boundary two-layer TVM algorithms.

# Part B — Four parallel planes: a topological model for the 4-layer router

## B0. What precis has

- `DEFAULT_STACKUP` (`src/precis/pcb/__init__.py`): F.Cu, In1.Cu, In2.Cu,
  B.Cu all `role: signal`, inner two `routable: True`, no fills (the
  2026-10-23 no-flood-fill ruling). Four true signal planes.
- Preset `4l-two-sided` (`STACKUP_PRESETS`, same file): F/B signal with
  GND fill, In1 a GND **plane** that is also routable at `route_cost` 2.5×
  (`HYBRID_IN1_ROUTE_COST`), In2 a plane carrying derived power regions,
  not routable. Alternating `direction` h/v down the stack.
- Vias: **through only**. `realize.py::through_span` returns
  (0, n_layers-1): "the router emits no blind or buried via (Reto
  2026-10-18, gr495289)"; a via that does not connect on a layer still
  occupies it (disk reserved, antipad cut). Blind/buried appear only in
  the gerber/DRC/epro readers. So below, **every via is a vertex copy on
  all four planes and a puncture on all four**.
- SMD pads live on F or B only; `layer_lock.py` gives per-pad allowed-layer
  sets.

## B1. The topological object

Reto's "4 parallel planes with via-equivalent points" has a name: a
**layered 2-complex with identified via vertices**. For layers l = 1..4
take punctured planes P_l = R^2 minus (pads on l, keep-outs on l, barrels
of every through-hole pin and every via). Glue a vertical edge at each
through site s between its copies s_1..s_4.
X = (P_1 ∪ … ∪ P_4) ∪ {vertical via edges}. A net is a Steiner tree in X
whose horizontal pieces (**segments**) each lie in one P_l and whose
vertical pieces are vias. Two nets conflict iff two of their segments lie
in the same P_l and their homotopy classes (R11) are forced to cross.

The other candidate objects, and why they are not it:

- **k-page book with shared spine** (R7): needs all pins collinear and
  every layer meeting on that line. True for one connector row or channel,
  false for a board; use it locally (R4), not globally.
- **Thickness / geometric thickness with shared vertices** (R2;
  Duncan–Eppstein–Kobourov 2004, 10.1145/997817.997868 [DOI]): each net
  whole on one layer, i.e. X with zero vias; NP-complete already for k = 2
  (Mansfield 1983, 10.1017/S030500410006028X [stub 514934]). Pach–Wenger
  (10.1007/pl00007258 [DOI]): pin *positions* never obstruct a planar
  layer, only pin *rotations* and obstacles do.
- **Layered planar embedding** (Sugiyama levels) is a different notion.
- **Homotopy classes per layer joined at vias**: this is X. The literature
  name is **k-layer topological via minimisation** (k-TVM): Cong–Liu 1991
  (10.1109/43.85735 [stub 514935], NP-complete for general k, polynomial
  channel cases); Rim–Kashiwabara–Nakajima 1989 (10.1109/43.41502 [stub
  514936], DP for channel and circular boundaries); Sarrafzadeh–Lee
  1989/1991 (10.1109/43.31548 [stub 514937], 10.1109/12.102839 [stub
  514942]). The fixed-topology version is **k-layer CVM**, NP-complete from
  k = 3 (Chang–Du 1988, 10.1109/12.4616 [stub 514941]) and polynomial for
  k = 2 with junction degree <= 3 (Hadlock 1975, 10.1137/0204019 [stub
  514927]; Pinter 1982 [no DOI]; Chen–Kajitani–Chan 1983,
  10.1109/TCS.1983.1085357 [stub 514938]).
- Practical multilayer rubber-band formulations: Dai–Dayan–Staepelaere
  1991 (10.1145/127601.127622 **[held pa266319]**, layer assignment during
  hierarchical partitioning); Dayan–Dai 1993, UCSC-CRL-93-04 [no DOI]:
  nets → Steiner trees → **sections**, each section one layer, a via
  wherever adjacent sections differ, then single-layer rubber-band
  subproblems; Dai–Kong–Jue–Sato 1990 (10.1109/iccad.1990.129838 **[held
  pa266318]**: constrained triangulation as the dynamic data structure);
  Staepelaere et al. 1993 (10.1109/54.245960 **[held pa266323]**); Yu–Dai
  1995 BGA fan-out (10.1109/iccad.1995.480175 [DOI]). "Via-point /
  via-candidate" and "Steiner via-point insertion" are not standard
  problem names; they are the candidate-site restriction of k-TVM (§B2).

## B2. Segmentation and the ILP

**Sectioning (exact, from R9/R12).** Pull each airwire taut on the
constrained Delaunay triangulation (CDT) of all pins and obstacles
(`escape.py` builds hulls and gaps; `topo.py` builds a CDT of free space
for the vector router; a pad-vertex CDT is the missing structure). Record,
along each net's taut tree, the ordered list of crossings with other
nets. Cut the tree into **sections** between consecutive crossings (and at
Steiner branch points). A via can only usefully sit inside a section, so
one candidate via site per section suffices for the *topological* problem;
the cluster graph of R9 is exactly "sections as vertices, crossings as
hard edges, section adjacency as soft edges". This is Dayan–Dai's
sectioning and Pinter's clusters at once.

**Candidate via sites, in order of preference.**

1. **Pad-adjacent escape sites** (BGA dog-bone interstitials, QFN pad-end
   stubs): the only places a fine-pitch pad can reach another layer;
   Yu–Dai 1995; `escape.py` gaps locate them.
2. **Section midpoints on the taut path** (one per section): minimal
   topological disturbance, zero detour.
3. **CDT face centroids** adjacent to the section, when the midpoint is
   inside a pad shadow or violates via-to-via spacing: a via in a face adds
   one puncture to every layer and changes homotopy classes only for wires
   through that face.
4. **Grid** as a last resort (what Freerouting does).

Through-hole pins are free via sites with cost 0 on their own net (R15.3).

**ILP (exact for a fixed set of candidate topologies; CP-SAT scale is fine
for hundreds of nets).**

Index: nets n; sections e ∈ S(n); layers l ∈ L = {F, In1, In2, B}; via
sites s; CDT edges (cuts) c.

Variables: x_{e,l} ∈ {0,1} (section e on layer l); v_s ∈ {0,1} (through
via at s); optionally t_{n,k} ∈ {0,1} choosing among a few homotopy
alternatives per net.

Constraints:

- one layer per section: Σ_l x_{e,l} = 1;
- routability of l: x_{e,l} <= r_l (r_In2 = 0 in the hybrid preset);
- pad layers: for the section at an SMD pad on layer p, x_{e,p} = 1 unless
  a via site adjoins the pad (then v_s >= 1 - x_{e,p});
- continuity: for consecutive sections e, f of one net sharing node s,
  x_{e,l} - x_{f,l} <= v_s for every l (a layer change forces a via at s);
  if s is a through-hole pin, v_s is a constant 1 at cost 0;
- conflict: for every crossing between sections e, f of different nets and
  every l, x_{e,l} + x_{f,l} <= 1;
- cut capacity per layer (R11): for every CDT edge c and layer l,
  Σ_{e forced across c} w_e x_{e,l} + Σ_{s on c} d_via v_s <= cap(c, l),
  where cap is the edge length minus pad extents
  (`escape.py::_gap_between`), and a via consumes d_via (pad diameter plus
  clearance) on **every** layer because vias are through;
- via spacing: v_s + v_s' <= 1 for sites closer than the via pitch;
- plane integrity (§B3).

Objective: minimise Σ_s c_via v_s + Σ_{e,l} route_cost_l len(e) x_{e,l} +
c_detour Σ t_{n,k} detour_k, with route_cost_In1 = 2.5 and c_via chosen so
that one via trades against roughly one via-pitch of extra copper (SURF's
K_v).

**Complexity.** With L = {F, B} and junction degree <= 3 this is planar
max-cut, polynomial (Hadlock; Shih–Wu–Kuo 1990, 10.1109/12.53581 [DOI]).
With four layers it is 3-plus-layer CVM, NP-complete (Chang–Du 1988); with
free homotopy, k-TVM, NP-complete (Cong–Liu). It is **not** "max-cut with
k labels": it is metric labelling with hard conflict edges plus knapsack
cuts. Practice: solve the two-layer relaxation exactly, warm-start CP-SAT
from it, and let sections move to In1 only where odd-cycle vias or
capacity violations remain. Exact: sectioning, crossings on taut paths,
cut capacities, the 2-layer max-cut, the final R11 check. Heuristic: site
pruning, the limited homotopy alternatives, the ILP time limit.

## B3. Plane layers: the capacity cost of punching them

In the hybrid preset In1 is GND and In2 carries power regions. A through
via for a signal net punches both. Model each via as a puncture of
diameter d_ap (drill + 2 × annular ring + 2 × plane clearance) on every
plane. Three consequences, all expressible as cut conditions:

1. **Plane web width.** Between two adjacent punctures at centre distance
   p the remaining copper web is p - d_ap. A row of vias at pitch p with
   p - d_ap < w_min severs the plane along that row (a slot). Constraint:
   for every pair of candidate via sites (s, s') closer than d_ap + w_min,
   v_s + v_s' <= 1, unless the pair is on a line the plane may be severed
   along (board edge). On a 0.8 mm BGA with d_ap = 0.9 mm every full-grid
   dog-bone field severs the plane under the part: the plane there is lost,
   and the model must treat In1/In2 under the BGA as unavailable for return
   current and route cost.
2. **Plane connectivity as a planar dual problem.** The plane's free
   region is R^2 minus (punctures ∪ In1 traces, in the hybrid case). It
   stays connected with width >= w_min iff for every cut between two
   punctures the gap exceeds w_min: the same gap test
   `escape.py::_gap_between` runs on pads, applied to via sites and In1
   traces. Exact, O(n log n) over the CDT of the puncture set.
3. **Return path.** A signal on F whose taut path crosses a plane cut with
   web width < w_min has its return current detoured; penalise c_slot per
   such crossing or forbid it for high-speed classes. (Heuristic proxy; the
   exact statement needs a field solver.)

So c_via(s) = c_base + c_antipad × (planes punched) + c_slot × (web
violations). With through vias only, every via punches both planes on the
hybrid stackup, which is why the 2-layer relaxation is the right warm
start: In1 routing buys little unless a net reaches In1 free through a
through-hole pin.

## B4. Staged algorithm (exact parts first)

- **Stage 0 (exists, exact).** `escape.py` shells and gaps;
  `layer_lock.py` allowed-layer sets per pad; `ratsnest.build_airwires`
  MST.
- **Stage 1 (exact, cheap).** Build the CDT over pads, keep-outs and
  through-hole barrels. Pull each airwire taut (funnel algorithm over the
  CDT dual). Detect crossings on taut paths; for pins on a common footprint
  boundary use R4 interleaving directly. Output: crossing graph C over
  sections, with forced layers.
- **Stage 2 (exact).** List-2-colouring of C over {F, B} with forced
  colours (R15.1). If bipartite: done with zero inter-pad vias; report.
  Else emit the odd cycles.
- **Stage 3 (exact when junction degree <= 3).** Section the odd-cycle
  nets at their crossings; cluster graph; planar max-cut (T-join on the
  dual, Hadlock) with via cost c_via(s) including the plane antipad
  penalty; output sections, layers, via sites. Run the per-layer cut check
  (R11) on F and B with vias as punctures; if a cut fails, raise c_via on
  sites across that cut and re-solve (one iteration).
- **Stage 4 (ILP, heuristic in time).** Only if Stage 3 still has capacity
  failures or exceeds the via budget: open In1 (hybrid) or In1+In2
  (default stackup) with route_cost, warm-start CP-SAT from Stage 3, hard
  plane-integrity constraints from §B3, 30 s limit.
- **Stage 5 (exact check).** Per-layer R11 on every CDT edge, plane web
  check, via spacing. Certificates: odd cycle (net names), violated cut
  (pad pair, demand, capacity), severed plane (via pair, web width).
- **Stage 6 (geometry).** Emit the `.dsn` with per-net layer rules and via
  sites as fixed vias (`route.py::route_dsn`), or spread the rubber bands
  directly (Staepelaere 1992). `place_route_round_trip` re-places on
  failure; add the Stage 2 odd-cycle count to `place.py::_objective` so
  re-placement attacks the right thing.

## B5. Worked examples

**A. Three mutually crossing nets (odd cycle K3).** Pins: A (0,2)–(4,2);
B (2,0)–(2,4); C (0,0)–(4,3). Taut paths are the straight lines; crossings
A–B at (2,2), B–C at (2,1.5), A–C at (2.67,2). C = K3, not bipartite.

- *2 layers, all pads SMD on F.* Any net leaving F costs 2 vias (down and
  back). One flipped net removes two crossings but the third remains, so
  at least two nets need excursions: minimum 4 vias. Realisation: B on
  B.Cu between two dog-bone vias (2); C split at a site between its two
  crossings, e.g. (2.3,1.7): section C1 on F crosses B (now on B.Cu,
  fine), section C2 on B.Cu crosses A (on F, fine), returning to its F pad
  at (4,3) through a second via (2). Total 4. Max-cut confirms: the
  cluster graph has 5 clusters (A, B, C1, C2 plus pad constraints) and 4
  is optimal.
- *2 layers, all pins through-hole.* B whole on B.Cu (0 vias, R15.3), A on
  F, C split once at (2.3,1.7): C1 on F, C2 on B.Cu. Total **1 via**.
  Reto's rule exactly: flipping B "from the back" is free because the pins
  pierce both layers, and the odd cycle costs one split.
- *Change topology instead:* route C below B's pin (2,0); its homotopy
  class no longer crosses B, C becomes the path B–A–C, bipartite.
  Through-hole: 0 vias; SMD: C on B.Cu for 2 vias. Detour about 1 mm of
  copper: the TVM trade SURF prices with K_v.
- *4 layers (`DEFAULT_STACKUP`), SMD pads on F.* K3 is 3-colourable (A on
  F, B on In1, C on In2) but B and C still each need two through vias to
  leave and return to F: **4 vias, same as 2 layers**, each now punching
  In1 and In2 too. Inner layers do not cut odd-cycle cost for SMD-only
  nets; they help when vias are already paid for (through-hole pins,
  connectors) or when one excursion covers many crossings. *Through-hole
  pins:* A on F, B on In1, C on In2: **0 vias**, versus 1 on two layers.
- *Hybrid preset:* In1 at 2.5×, In2 not routable: the SMD case collapses
  to the 2-layer answer (4 vias) plus antipads in both planes.

**B. BGA and QFN escape.** QFN-32, 0.5 mm pitch, one ring: all pads on
`escape.py` shell 0, so every pin escapes outward on F with no via.
Topology: the 32 pads form a boundary (R4); two nets interleave iff their
destinations lie in the opposite cyclic order; each interleaving pair is a
crossing in C, and an odd cycle among QFN nets is resolved by dog-boning
one net to B.Cu at the pad end (candidate site type 1, 2 vias) or, on a
board with a through-hole connector, for 1 via at the connector end.

BGA 10 × 10, 0.8 mm pitch, 0.4 mm ball, trace 0.1 mm, clearance 0.1 mm:
0.4 mm gaps, 1 trace each (`gap_capacity`). Shells 36 / 28 / 20 / 12 / 4.
On F: shell 0 escapes directly; shell 1 (28) uses 28 of shell 0's 36 gaps;
shell 2 (20) finds only 8 left: **fails on F**, as
`_layers_for_escape`'s ceil(demand/capacity) and the R8 cut condition say.
Shells 2–4 (36 pins) dog-bone to interstitial vias (site type 1). On B.Cu
the obstacles are via barrels (0.6 mm pad at 1.13 mm diagonal pitch, gap
0.53 mm, 1 trace): rerun the shell computation with via pads in place of
balls. Plane cost: with d_ap = 0.9 mm > 0.8 mm pitch, adjacent dog-bone
vias sever In1/In2 between them (§B3.1); the plane under the inner 6 × 6
is lost, so thin the via field (shells 2–3 on B.Cu, shell 4 on In2 in the
default stackup) or accept no plane under the part. Dimensions are
illustrative; use `capabilities.py` numbers.

## B6. Cross-check against perplexity-reasoning:514915

Agrees: the vertex-copy-per-layer model is k-TVM; general k-TVM is
NP-complete (Cong–Liu); k-layer CVM is not an exact max-cut generalisation
beyond two layers; Dayan–Dai is a sectioning heuristic, not a complexity
result; geometric thickness / simultaneous embedding with fixed vertex
mapping is the whole-net special case. It corrected three IEEE DOIs
(Sarrafzadeh–Lee 1989, Cong–Liu, Rim et al.), confirmed by Crossref.
Flags: its SURF DOI (10.1145/127601.127629) is again the Hu et al. CVM
paper, not SURF; "10.5555/949970.950073" is an ACM DL id, not a DOI;
several DOIs are cited to USPTO petition PDFs rather than the publisher;
it offers no via-site-selection primary beyond Dayan–Dai, and none was
found (the candidate-site problem appears only as an engineering choice
inside SURF and Yu–Dai). Unverified: Dayan thesis year; Bekos 2020 JoCG
DOI.

Not stubbable (no DOI): Pinter 1982, Dayan–Dai 1993, Maley 1990, Liers et
al. 2011. The IEEE/SIAM/Wiley stubs will likely need the paid OpenAlex
fetch leg.
