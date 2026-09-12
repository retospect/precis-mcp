---
status: draft
title: units cutover dossier — generated touchpoint inventory (working doc for units-policy-cutover; delete with it)
prio: high
---

# Units cutover dossier (generated 2026-09-12, Explore sweep)

Implementation working doc for `units-policy-cutover.md` +
`box-full-dims-cutover.md`. Four inventories: dimensioned op args/DSL
tokens · absolute epsilons · nm Å storage columns · display-format
sites. Anchors are `file:line` at sweep time — treat as pointers, not
gospel; re-grep before editing.

## 1. Dimensioned op arguments & DSL tokens

### cad DSL (`src/precis/cad/dsl.py` — docstring says mm; kernel is unit-agnostic)
- Module docstring line 3 "millimetres"; `ShapeSpec` docstring (dsl.py:54) "params (mm)".
- `_ALIAS_KEYS` (dsl.py:68) length tokens: box w/d/h · cyl/cone/hex r,h · tcone rb,rt,h · sphere r · torus R,r · ngon/pyramid r,h (+n) · frustum rb,rt,h (+n) · chamfer size (length) + angle (DEGREES, dsl.py:172).
- `_NUM`/`_TOKEN_RE` (dsl.py:63-64) accept exponents (gr332020).
- `_fmt_num` (dsl.py:188) — DSL's own formatter; hard-codes 1e-4 sci switch + round(x,6). Absolute, unit-blind. Its output MUST re-parse.

### cad scene language (`src/precis/cad/scene.py` — lengths in design unit)
- `@x,y,z` (_LOC_RE scene.py:145) length · `rot:` degrees (_ROT_RE :146) · `polar:nNrR` r length (:147,182) · `linear:` dx/dy/dz lengths (:170,192) · `spin:<deg>` (:148) · `limits:lo..hi` MIXED by joint kind — deg for revolute/screw/cyl-angle, mm for prismatic/slide (:65-69,166,169) · `pitch:<mm>` screw lead (:153) · `ratio:` dimensionless (:154) · `dim <name> = <mm>` (:155, substitution :44-50) · joint `state=` deg-or-mm per kind (:76-78, _joint_xform :1849) · `port`/`payload` @/rot tokens.

### cad catalog (`src/precis/cad/catalog.py` — mm, :18)
- `bolt:m<size>x<length>` (_bolt :236) · `extrusion:<profile>x<length>` (:301) · `rail:<series>x<length>` (:317) · bearing/nut/washer/nema/gear tables (:75). `_g` (:70) mm→token formatter.

### cad handler get-views (`src/precis/handlers/cad.py`)
- section z (:1528) · arc r/c/axis (:1509) · point p / ray o,d (:1469,1485) · connectivity tol mm (:1708) · put/edit state deg-or-mm (:144,204) · mass view density kg/m3 (:1624) with hard-coded mm³ factor `*1e-6` (:1626).

### precis_se — METRES, degrees (ops.py:8,29)
- add_block/set_envelope envelope DSL in m (handler.py:102) · pose m / rot deg (SeBlock ops.py:212) · array linear pitch m (:406-408) / polar radius m (:415-419) · add_port direction unit-vec · add_measure/set_measure value/min/max/relation.offset/tol in measure's `unit` · set_load force N, torque N·m · formfind q force densities N/m.
- `measures.py` `UNITS = ("m","count","ratio","deg")` (:50); relation offset/tol in target unit (:92,133-141).
- `joints.py`: lead m/rev (:46,206-221) · free_length m, rate N/m, tension/compression_capacity N (:226-247) · preload N (:248-262) · OBJECTIVE_KEYS (:269).
- `catalog.py`: `to_metres`/`_TO_METRES` {m,cm,mm,um} (:74-91) — the ONLY conversion bridge in se · `_fmt` .9f fixed-point (:118, DSL regex rejects exponents; caps min length).
- `fasten.py` derived m lengths (:112 ff) · `stability.py` length m, forces N (:555-556).

### precis_nm — ÅNGSTRÖM, degrees (ops.py:8)
- pose Å / rot deg (migration 0001:59) · envelope DSL at Å (handler.py:141-142) · connect objectives jsonb FREE PASSTHROUGH (may hide lengths; only 'role' consumed, ops.py:379) · declare_dof names only.
- generators: cnt/cone `length_A` (sp2.py:276,305-316,587,626-635) · fullerene atoms count · `_fmt_len` (sp2.py:237) emits Å envelopes (:424) · cyclodextrin `_CD_VARIANTS` O4 Å targets (sugars.py:151).
- `mechanics.py`: `euler_buckling_ceiling_nN(radius_A, length_A)` (:179) hard-codes *1e-10 (:182-184), returns nN *1e9 (:187); `tube_geometry_from_envelope` (:190) reads Å − VDW_MARGIN_A. KEEPS Å/nN/eV signatures — handler converts at the seam.
- `validate.py` `envelope_fit(..., margin_A)` (:84) returns Å protrusion.

### structsolve — unit-agnostic but dimensioned
- `simp_optimize(h=1.0)` voxel pitch in caller unit (simp.py:776-799); loads force triples; compliance force×length. `lattice_fill(cell, wall, h)` (:945-951). formfind q force/length, "Unit-agnostic" (formfind.py:24).

## 2. Absolute epsilons (ABS = fix; REL/DIMLESS = leave; counts/physical = leave)

### cad — must-fix ABS
- `LINEAR_EPS` 1e-6 (vec.py:30) — THE load-bearing one; used by: normalize guard (vec.py:181), merge_intervals/quadratic_le defaults (interval.py:28,36,91,100-102), every primitive's membership/dedup/culling (primitives.py:92-99,109,138-142,172,205,216,237,286-289,298-299,327,378-382,390,401,423-429,438,447-448,533,541-544,575 — incl. face-culling ValueError :429), fold material_intervals (fold.py:176,190,220), probe z-band (probe.py:293,308).
- torus imaginary filter 1e-7 (primitives.py:593) · `_project_onto` grad-norm 1e-9 (relate.py:245,327 — borderline, grad≈1 for true SDF) · `CONTACT_TOL_MM` 1e-2 (relate.py:474; `connectivity` default :564,587; handler default cad.py:1708) — `CONTACT_TOL_REL` 1e-3 (:482) is the shipped replacement pattern · `translational_dof(tol=1e-3)` (:626,693) · AABB reject 1e-9 (:643) · travel round(,4) (:699) · tessellate `_EPS` 1e-9 (:57,146,148) + halfspace margin `0.1*diag + 1.0` (+1.0 = a whole body at Å; :332) · scene gear/belt state 1e-9 (scene.py:1828) · catalog `_MAX_CUT_LENGTH` 4000 mm (:173,211) + bolt 2..300 (:245) — catalogue mm-domain, arguably stays absolute-with-unit · bulk ray offset −1.0 (bulk.py:96) + round(vol,4)/round(rel_err,5) (:149,152) · handler roundings :1575,:1647.
- Already REL (pattern to copy): `_GRAD_REL_EPS` 1e-7 ×governing length (relate.py:64,310,392), `_STEP_REL`/`_STEP_FLOOR_REL`/`_IMPROVE_REL` (:67-75), `_round_relative` (:223-233), `_region` pad 0.1×span (:198).
- DIMLESS exempt: `ANGULAR_EPS` (vec.py:33), cos 1e-9 (vec.py:163), rot atol 1e-9 (probe.py:286), `_CREASE_DEG` (gltf.py:43).

### se
- `_MIN_MEMBER_M` 1e-6 m (fasten.py:84) — ABS metres, decide: keep as stated-absolute or relativize.
- `translational_dof(tol=1e-4)` in DOF probe (drc.py:591) — ABS in kernel units (semi-relative post kernel_scale).
- `_KERNEL_BAND`/(1e-3,1e6) + `_KERNEL_TARGET` 100.0 (validate.py:77-83,132-139) — deliberate comfort band, stays.
- Already REL: `_RANK_RTOL`, sign-feasibility, eigen, `_PRESTRESS_RTOL`, `_PRELOAD_TRIPLE_RTOL`, `_COLLAPSE_RTOL`, `_MIN_ENGAGEMENT_D`, `_PROTRUSION_PITCHES`, `_LEAD_TOL_REL`, `_MAGNITUDE_FACTOR`, overlap-vs-resolution (validate.py:211).
- `_fmt` .9f precision floor (catalog.py:118,123).

### nm
- coincident-pose 1e-9 Å (validate.py:453) — ABS Å today; after m-cutover becomes 1e-9 m = WRONG; must go relative.
- Physical Å constants STAY (they are physics, get an explicit unit in name/comment): VDW_MARGIN_A, GRAPHENE_A, caps 500 Å, fullerene bond lengths/cutoffs, `_CD_VARIANTS`, `_CAVITY_WALL_RADIUS_A`, bonds, pucker, relax steps, RUPTURE_FORCE_NN, E_MODULUS_PA, TUBE_WALL_THICKNESS_A, K_THETA_EV_PER_RAD2, `_SCRATCH_CELL_A` (job.py:117), `_generated_cell` pad +20 Å (handler.py:1390).
- REL: OVERLAP_DEPTH_FRACTION, BOND_GAP_FRACTION, `_CAVITY_TOLERANCE`; angles: BOND_VECTOR_MAX_DEVIATION_DEG.

### structsolve — all DIMLESS/REL/counts; exempt list for the AST gate:
`_AM_P _AM_EPS _AM_STENCIL _AM_Q _AM_SMIN_EPS _OC_MOVE _OC_ETA _FILTER_GAMMA _GYROID_MEAN_GRAD _DEFAULT_NU emin cg_tol` OC bisection bounds, filter floor 1e-30, tol 0.01, overhang 0.5, rmin in elements. Plus `_SINGULAR_RTOL` (formfind.py:40) REL.

## 3. nm Å storage columns (Å→m migration targets)

- `nm_blocks.pose_xyz` double[] — Å translation (0001:59,63; persist.py:136,265; `_BLOCK_COLS` :79) → ×1e-10.
- `nm_blocks.envelope` text — DSL string, length tokens Å (0001:65-67) → rewrite token values ×1e-10 (r,h,w,d,rb,rt,R,size; NOT n, NOT angle).
- NO conversion: pose_rot (deg), nm_ports.direction (unit vec), names/flags.
- Audit-only (free passthrough may hide lengths): `nm_blocks.dof` jsonb, `nm_connects.objectives` jsonb (0002:42-45), `nm_ports.annotations` jsonb — sweep prod rows, warn, do not auto-convert.
- OUT-OF-BAND: nm L5 lives in Å-native `structure` designs via bound_design (handler.py:590, _generated_cell :1382). structure stays Å; convert at the seams: `envelope_fit` (validate.py:84), bind preflight (handler.py:462), `_generated_cell`, mechanics calls.
- se contrast (already m): se_blocks.pose_xyz/pose_rot/envelope/array_spec, se_measures.* (persist.py:61-71).

## 4. Display-format sites (route through the shared formatter)

- se handler: `_fmt_num` :949 · `_fmt_len` :959 (m + mm gloss — the one existing conversion) · `_fmt_in_unit` :969 · `_fmt_band` :979 · `_fmt3` :710 (no unit) · `_mm` :1370 (fasten view ×1000 .2f) · measure row :991 · array suffix :715-724 (no unit) · block line :733-739 · view=block :793-794 · bom :447-505 · stability :1305-1352 · fasten :1410-1461 · clearance :1612-1615 · drc travel (drc.py:598).
- nm handler: `_fmt3` :1428 · port fmts :1516-1525 · view=block :1539 (Å) + tree :1456 (unitless!) · clearance :1756-1758 · bind preflight :462 · mechanics :805,814,894 · validate findings (validate.py:307,474-478) · sp2 `_fmt_len` :237 + provenance prose :427-430 · job `_fmt_vec` :150.
- cad handler + kernel: interval :80-89 · dim echo :1303 · node pose/pattern :1237-1240 (no unit) · bbox card :1341 (mm) · interference :1409 · probe views :1474-1540 · clearance :1547 · dof travel_mm :1560-1565 · volume :1573-1575 · mass :1641-1661 (+1e-6 factor :1626) · payload :1695 · connectivity gap_mm :1759-1765 · sweep :995-1112 · bom :781,806 · `dsl._fmt_num` :188 (must re-parse!) · catalog `_g` :70 · export `_g` + `units = mm` header (export.py:168) · scene errors :1830-1839 · se catalog `_fmt` :118 (DSL-safe fixed point, no exponents).

Formatter design constraint from this list: TWO formatter contexts — (a)
human display (neat SI prefix, e-notation fallback) and (b) DSL
round-trip (must re-parse under `_NUM`; today exponent-capable in dsl.py
but se catalog `_fmt` avoids exponents — unify on the exponent-capable
grammar, one canonical emitter).
