---
status: draft
title: rotary ratchet valve — a chemically selective metering wheel as the second hexfold/precis_surface test piece, plus the four design tools it needs
prio: high
model: opus
blocked-by: hexfold-integration
---

# Rotary ratchet valve

Reto's specification of 2026-09-17, integrated. **The notation and the
smooth layer it rides on are `src/hexfold/spec.md`** (layer stack §2,
seams §6/§11.3, smooth layer §20–§24, MCP §25, self-intersection §27,
roadmap §28). This file holds the device itself: what it is, the design
rules it fixes, the four tools it demands of the smooth mapper, and where
each lands in the existing machinery. It is the *second* test piece after
the box test piece (spec §28.3) and the first one whose function is
chemical rather than mechanical.

## What it is

A tube with a bulge, containing a disc that rotates within it. Pockets
form in the clearance between the disc rim and the shell wall. The rim
and each arc of the shell carry different functional groups, so a
pocket's affinity for its cargo depends on which arc it is passing:
admitted on one side, carried round, released on the other; on the
return path the pockets do not match, so nothing comes back.

Selectivity is **chemical, not geometric**. A molecular sieve
discriminates by size; this discriminates by complementarity between rim
groups and shell groups, so the same mechanical part is retargeted to a
different molecule by changing only the lining.

Intended use: a **metering front end for a catalytic cascade** — a known
count of reagent molecules per rotation instead of a
concentration-dependent trickle — and, more speculatively, a
membrane-spanning transport element. Status: design exploration; no
synthesis route; the working assumption is that atomic assembly is
eventually available.

## Geometry — four surfaces, not two

| surface | role | travels? |
|---|---|---|
| attracting arc | hydrophilic shell wall; binding is decided here | fixed |
| rejecting arc | hydrophobic shell wall; cargo is expelled here | fixed |
| open hole arcs | no wall; free exchange with the surrounding volume | fixed |
| rotor rim | carries the pockets and the cleaner sites | rotates |

Around one rotation the shell alternates walled and open: hydrophilic
wall, no wall, hydrophobic wall, no wall. Walled arcs are where a pocket
is enclosed and its affinity is determined; open arcs are where exchange
happens and double as the cascade's input and output.

The alternation is load-bearing. Shell polarity is fixed in space while
the pocket rotates through it, so the pocket contents see a changing
environment; rectification only works if that transition is **sharp** —
a gradual polarity gradient smears the affinity change across the whole
rotation and the ratchet stops ratcheting. The open arcs are the break.

This gives a **per-arc design table** with the rotor rim as the one row
that must satisfy every column: rim groups are generalist compromises,
specialisation lives in the arcs.

In hexfold terms (spec §7, §11.4, §28.3): the shell is a surface of
revolution — `tube(n,m)` → symmetric collar → wider tube → collar →
`tube(n,m)` — with two holes in the bulge wall (the open arcs); the rotor
is a disc, i.e. two `cap(n,m)` flat lids on a short tube or fused
directly (a pillbox). Every seam is flat (§22.2), so nothing here needs
the bent collar. **Above ~1–2 nm radius** the shell and rotor are smooth
surfaces with a tolerance and the lattice is snapped on afterwards (spec
§20, the smooth/atomic split); below that atoms are placed explicitly by
the discrete notation. The first instance ("a few hundred carbons with
pendant groups", §Drive) sits at that boundary and is a discrete build.

## Metering and the concentrating cascade

The point of the wheel is a known count per crank. If pockets are only
sometimes occupied at the loading position, downstream stoichiometry is a
Poisson lottery and the cascade is oversupplied with one reagent relative
to another. Two levers set occupancy and they fight:

- **Binding affinity.** Occupancy goes as c/(c + K_d); a tight binder
  saturates even at low concentration.
- **Dwell time.** If rotation outruns the on-rate, pockets pass the
  loading arc empty regardless of affinity.

Tight binding implies a slow off-rate, so a pocket that fills readily
will not release at the far side. **The cascade dissolves this**: a chain
of stages that are individually poor (each capturing perhaps one in a
hundred) but rectifying. Concentration rises stage by stage, binding
becomes progressively more likely, and ~99.9 % occupancy is the target
only at the final stage; early stages use weak, fast binders, tight
binding appears only where concentration is already high.

Enrichment per stage is net forward minus back, so **leakage is what
kills it** — a hundred-fold gain per stage collapses if each stage leaks
backwards. Rectification therefore comes from the shell polarity itself,
not from statistics: the hydrophobic output arc makes release
thermodynamically forced, not merely likely. Counter-running pumps at
each level remove waste from the intermediate chambers and link the
stages; without them the chambers fill with rejected species and
enrichment stalls (countercurrent separation, as in industrial cascades —
source needed, §Sources).

**Arc angles are the natural parameter set**: walled arcs set dwell time,
open arcs set exchange time, both tuned against crank rate.

## Poisoning and scrubbers

Design target: a **durable assembly**, not a consumable rotor. That
decision is what makes scrubbing machinery necessary rather than
optional; the power the scrubbers cost is the running cost of lifetime.

- **Rim cleaner sites.** Dedicated rim sites bind poisons rather than
  cargo, so every rotation sweeps the rejecting arc and depoisoning is
  continuous. Cleaner sites are another row in the per-arc table: pocket
  count trades directly against cleaner count.
- **Saturation.** A cleaner site is atom-sized and holds one molecule; a
  tight scavenger has the same release problem as the pockets and
  saturates after a few rotations without its own dump path.
- **Waste wheel.** A second wheel picks up the dirt and deposits it
  elsewhere. It inherits the release problem one level up, harmlessly:
  it dumps into bulk, not into a metered pocket, so a crude hydrophobic
  arc suffices and precision does not matter.
- **Parallel scrubbers.** For several poison species, small perpendicular
  wheels ride the return rim (three or four fit on a radius), each tuned
  to one species, axes radial so each is driven independently and the
  rim presents itself to them in turn — a service station, not a filter.

Scrubbers need no independent schedule: poisons arrive at roughly the
cargo rate, so one sweep per main rotation is the cadence and tying the
rates together is a feature. Independent addressing (via FRET) is
complexity to add only if one scrubber saturates faster than the others.

## Drive and coupling

**Motor.** Plain azobenzene is insufficient: cis–trans is bistable but
not directional, giving a rocking motion. Directionality needs a
Feringa-type overcrowded alkene, where helical strain plus a one-way
thermal relaxation step sets the sense of rotation [V1]; a
thermal-ratchet-free variant with three consecutive photoreactions also
exists [V3]. The thermal helix inversion is rate-limiting and classically
slow, but second- and third-generation designs reach **megahertz**
unidirectional rotation [V1]. Whether the valve needs that, or kilohertz
suffices, is an open sizing question (Q1). Load: inertia is negligible;
the limit is viscous drag, ∝ radius³. A few hundred carbons with pendant
groups is likely tractable at kilohertz; floppy pendants that drag water
are the expensive part — an argument for rigid mounting.

**Gearing — rejected.** No credible precedent at this scale. Biology's
rotary machines (ATP synthase, the flagellar motor) are single rotors on
compliant shafts, not meshing teeth [V4]. Synthetic gearing is at the
two-cog stage: rotation transmitted between cogwheels anchored on a lead
surface with single copper atoms as pinning centres, STM-driven, at 5 K
[V2]; correlated motion in longer trains is the stated open challenge.
Room-temperature, solution-phase, self-assembled gearing is well beyond
demonstrated work.

**Charge-pattern interlocking — preferred.** Coupling by electrostatic
complementarity across a gap rather than by contact: no wear, no meshing
tolerance, no surface-energy fight. Debye screening in salt water gives a
screening length under a nanometre, but the rotor–shell gap is smaller
still (3–4 Å), so the coupling survives; small gaps are what make this
work. The consequence: **the arc functionalisation is already a charge
pattern** — affinity and torque transmission collapse into one
mechanism, and the same groups that decide binding transmit force. Cost:
they are no longer independent (retuning an arc's charge for binding
changes its gearing). Prior art to build on: torque analyses of
surface-mounted dipolar rotors in external fields and third-generation
motors driven on Cu(111) (reviewed in [V5]); the usable method is a
**constrained rotation scan** — freeze the stator, step the rotor
dihedral through 360° in ~15° increments, read the torque curve —
directly runnable on this geometry.

**Optical broadcast.** No spatial addressing (wavelength hundreds of nm
vs a rotor of a few), but polarisation and phase couple to orientation,
so a rotating polarisation drives every wheel in the region at once, no
wiring, no torque budget divided N ways. This answers the ganging problem:
drag adds linearly while torque stays fixed, so four scrubbers off one
crank means roughly a quarter rate or a stall; broadcast drives them in
parallel. Energy comes from the absorbed photon relaxing through a
biased pathway, not from the field's rotation — the rotating polarisation
only selects when and which orientation absorbs; a timing signal riding
on a power supply. Torque per photon is set by the excited-state energy,
so the ceiling is photon flux per wheel: a few ns per cycle, a few
hundred MHz at the absolute best.

**Hydrodynamic coupling — noted, parked.** A spinning wheel drags
solvent and a neighbour a nanometre away feels it; weak and lossy, but
it is how flagella synchronise into bundles without touching (source
needed). Recorded as available, not designed in.

## Surface chemistry and attachment

**The problem.** Pocket walls are graphene-derived, aromatic and
nonpolar by default. H-bond donors and charges mean functionalising,
which breaks conjugation locally — so lining chemistry and charge-pattern
gearing compete for the same ring positions.

**Y-junction sites.** Y junctions (the k = 3 seam atoms of spec §6.2,
§11.3) are already sp³-like breaks in the conjugation, so attaching
there sacrifices nothing. A ring can carry three Y carbons with a free
site each, possibly four. The useful arrangement is **two inside, two
outside**: the inner pair does binding chemistry, the outer pair does
charge patterning for the gearing, on opposite faces rather than
competing for positions. It also simplifies the enumerator: count Y
carbons and their face assignment rather than surveying every ring
position for exposure.

**Sugars, four-point anchored.** Four Y sites used together rigidly
anchor a pendant sugar, which is then itself functionalised: the
scaffold sets position and orientation, the sugar supplies hydroxyls,
stereochemistry and H-bonding hard to get from carbon alone. Rigid mount,
rich surface — rigidity matters given the drag argument above. The
constraint is conformational: four-point attachment must match the
sugar's own geometry, so only certain ring conformations reach all four
sites without strain. On a pyranose chair substituents alternate
axial/equatorial; a fully substituted carbon carries one of each, giving
"up and outward" not "up and down"; genuinely opposed directions need
two carbons across the ring, each with an axial group. **A sugar
stitched into a sheet is a chemical hybrid, not a topological one**: sp³
puckered rings cannot continue the sp² lattice, so it is a patch bonded
across a defect or edge, not a tile — a fitted component with an
interface contract (like a lens or cap on a rim), not something the ring
taxonomy must describe. Recorded in spec §30.

**Lipids.** One attachment point, tail dangling, no multi-point
matching. Attaching to a conjugated rod carries the lipid away from the
surface and gives a proper brush; a lipid-coated face opens the
membrane-spanning application.

**Electronic isolation.** A closed loop of sp³ carbons (hydrogenated or
fluorinated) stops conjugation, making the enclosed patch its own
electronic island; that loop is also the right quantum-region cut line,
since terminating at saturated bonds does not sever a conjugated system.
A ring of Y junctions is the softer option and lets the isolation
boundary double as the attachment sites. A Y junction radiating three
sheets at 120° closes to 360°, so the seam carries no curvature charge
(spec §20.3) and an sp³-ringed island can sit on each leg: three
independently terminable quantum regions on one topological feature — a
natural **three-port node**. This is the sp³ seam item's territory
(`hexfold-sp3-seam.md`) once it gets there.

## Design tool set (build in this order)

Each maps onto machinery that exists or is already planned; none is a
new kernel.

1. **Clearance field.** Given rotor and shell surfaces, the gap as a
   function of angle and position. Everything else reads from it. Both
   surfaces are smooth objects in the representation (spec §20/§24), so
   this is sampling the rotor mesh for nearest distance to the shell,
   parameterised by rotor angle — the §27 broad-phase machinery
   *measuring* the gap instead of rejecting overlap. Lands as
   `view='surface'` (spec §25.3) with a `clearance` query, or the se
   measure vocabulary once the surface binding kind exists.
2. **Pocket extractor.** Connected-component labelling on the region
   where clearance drops below a threshold → enclosed voids with volumes.
   The **pocket count falls out of geometry**, which is the metering
   number.
3. **Attachment-site enumerator.** Which positions on each surface can
   carry a group given seam and curvature constraints: the ring taxonomy
   (spec §6, §9) filtered by steric exposure, plus the Y-carbon face
   assignment above. Runs on all four surfaces independently; only the
   rim's sites travel.
4. **Complementarity scorer.** Arc-aware: the same pocket against three
   shell environments as it turns, checking that affinity actually flips
   between arcs. **Stub first** with a crude shape-and-polarity match so
   the pipeline runs end to end; real energetics only once it does.
   Ladder: the se atomic-mode charge/optical panel and mechanism
   analysis (`se-atomic-round2.md` later phases) are the rungs above the
   stub.

Inner shell radius is then a continuous parameter optimised against
pocket volume for the specific target. **Additional check:** drag versus
torque — total drag torque across all ganged wheels against available
motor torque, with margin; drag adds linearly, torque stays fixed, so
this is what catches a stalled design.

## Pocket lining — inverse design

Three passes: (1) dock the target in the empty pocket, find its preferred
pose → the surface patch it actually touches, usually much smaller than
the pocket; (2) project the target's exposed chemistry onto the facing
wall — donors want acceptors opposite, hydrophobic faces want alkyl or
aromatic patches, charges want counter-charges — a wish list of group
types per site; (3) reconcile against what is attachable: rank sites by
contact area, satisfy the best first. Then score the **reject list**
against the same lining to confirm nothing unwanted binds. Tractable: a
bounded enclosed system means no k-point sampling, no surface
reconstruction, no periodic images — a few hundred atoms in a cluster
calculation; confinement limits the conformational search to something
enumerable.

## MCP surface

The LLM works in intent terms, not coordinates (spec §25.2). The
specification object is a **whitelist** (the molecule to pass), a
**blacklist** (molecules to reject) and a **throughput target**;
radius, arc angles, pocket count and group choice are left for the
solver — the same underdetermined-wish pattern as caps and tubes, i.e.
`options(handle, wish)`. The reject list is the more interesting input:
arc chemistry is defined as much by what must not bind as by what must,
and it is the reject list that closes the design. The clearance field and
pocket extractor are read-only query tools the LLM calls to inspect a
candidate, never hand-specified.

Search space: large but not hopeless — the discrete choices collapse it
(pocket count is an integer, arc count is four, attachment sites come
from the ring taxonomy); the continuous parameters (radius, arc angles,
fillet) are a handful, exactly what the existing annealer
(`multiscale-design-system-spec.md`) handles. **The expensive part is
scoring, not searching**, hence the stubbed scorer: reject 99 % of
candidates cheaply, run energetics only on survivors.

## Design rules (fixed; do not re-ask)

- **Bond stability against drive energy.** The energy to detach any part
  of the machinery must be significantly larger than the energy pumped
  through it per cycle. Per-bond check: bond dissociation energy vs
  excitation energy, weighted by proximity to a chromophore. The margin
  is uncomfortable: drive photons are 2–3 eV, typical C–C bonds 3.5–4 eV
  — a factor ~1.5, not ten; every absorbed photon is near breaking
  something, which is why photobleaching is the failure mode it is. Tool:
  a **bond-energy audit** flagging any bond within a factor of two of the
  drive photon energy, chromophore-weighted.
- **Sharp arc transitions.** Rectification needs an abrupt polarity
  change; the open arcs provide it.
- **Rim as universal row.** Rim groups work against every arc →
  generalist; specialisation belongs in the fixed arcs.
- **Charge is not a free parameter.** Arc charge serves affinity and
  torque transmission; optimise jointly.
- **Durability over replacement.** Scrubber power is the accepted running
  cost.
- **Charge-pattern coupling over gearing**; optical broadcast for
  ganging; hydrodynamic coupling parked.

## Open questions

| # | question | severity |
|---|---|---|
| Q1 | Throughput sizing: does the valve need MHz rotation or does kHz suffice? Sets which motor generation and the drag budget. | high — decide before the drag/torque check is meaningful |
| Q2 | Scrubber cadence: is one sweep per main rotation right for every poison species, or does one saturate faster (→ FRET addressing)? | medium — instrument on the first lining |
| Q3 | First-instance scale: at "a few hundred carbons" the rotor radius is ~1 nm, the discrete/smooth boundary. Build the first one discrete (hexfold) and use the smooth mapper only for larger shells? Leaning yes. | medium |
| Q4 | Tools 1–2 on the smooth mesh vs on realised atoms: the mesh exists only once `precis_surface` stage 3 lands; before that, clearance from the `stick` atoms + vdW radii is a usable stub. | medium — decides the first build |

## Sources

Store ids `pa…` for us, DOI for readers. Not Crossref-verified yet
except where a store id is given (those were verified at import).

- **[V1]** M. Klok, N. Boyle, M. T. Pryce, A. Meetsma, W. R. Browne,
  B. L. Feringa, "MHz unidirectional rotation of molecular rotary
  motors," *J. Am. Chem. Soc.* 130 (2008) 10484–10485.
  doi:10.1021/ja8037245. **To import.**
- **[V2]** molecular gear train on Pb(111) with single Cu atoms as
  pinning centres, STM-driven, 5 K (Joachim group). **DOI to
  establish; to import.** Related, on Au(111):
  doi:10.1021/acs.jpclett.0c01747 (transmitting stepwise rotation among
  three molecule-gears).
- **[V3]** Dube group, light-driven motors: **`pa179409`** (constitutional
  alteration + proton transfer mechanism) and **`pa1088`** (rotation
  without thermal ratcheting, three consecutive photoreactions).
- **[V4]** rotary motors as molecular ratchets; F0F1 ATP synthase driven
  by proton gradient or electric potential: **`pa165135`**.
- **[V5]** molecular-machines review carrying the electric-field
  dipolar-rotor torque analyses (Zhao, Zhang, Van Hove, *ACS Omega* 2022,
  doi:10.1021/acsomega.2c04128), chirality-specific rotation on Cu(111)
  (Schied et al., *ACS Nano* 2023, doi:10.1021/acsnano.2c12720) and the
  electron-driven third-generation motor (Srivastava et al., *ACS Nano*
  2023): **`pa342603`**. The three primaries are **to import**.
- **Source needed:** countercurrent cascade enrichment (separation
  textbook); Debye length in physiological saline; flagellar
  hydrodynamic synchronisation; C–C bond dissociation energies vs
  photobleaching; the c/(c + K_d) occupancy law (Langmuir).
