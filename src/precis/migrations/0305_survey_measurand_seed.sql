-- 0305_survey_measurand_seed.sql
--
-- Seeds the measurands the ALCHEM agreement diff found missing from the
-- survey_extract reader vocabulary (gr632955; diff run 2026-11-11 on the
-- 12 held full-text papers, td632753): 203 distinct ALCHEM measurand
-- terms never paired with a precis measure and 192 had no measurand
-- taxon at all — 608 of 712 drops (85%) were drop:no_fitting_measurand.
-- This file covers the standard electrochem/materials quantities of the
-- missing list, by frequency:
--
-- * overpotential (8 papers), charge transfer resistance (7),
--   particle size (6), mass fraction (6), XRD peak position (6),
--   areal loading (5), Raman peak position (5), interplanar spacing (5),
--   Tafel slope (4), cycle life (4), FTIR band position (4),
--   bond length (4), lattice parameter (4), oxidation state (4);
-- * then band gap, specific capacity, cell voltage, BET surface area
--   (specific surface area), pore volume, pore size, formation energy,
--   vacancy formation energy, coordination number, d-band center, ECSA,
--   double-layer capacitance, exchange current density, onset potential,
--   coulombic efficiency, capacity retention, dissolution rate, TOF;
-- * the overpotential variants (nucleation overpotential, coverage
--   transition overpotential, overpotential gap) specialise the new
--   `overpotential` node rather than flattening onto it (me628309); the
--   same for `vacancy formation energy` under `formation energy`.
--
-- Deliberately NOT seeded (me628309 convention hazards): adsorption free
-- energy and reaction energy (ΔG vs the stored E_ads / step-ΔG
-- conventions of `adsorption-energy` / `reaction-step-free-energy`), and
-- mass activity (current per mass is not `product-yield-rate-per-
-- catalyst-mass`). `areal loading` is its own mass/area measurand —
-- `catalyst-loading` is a molar fraction.
--
-- Same shape as 0182_se_measurand_seed.sql / 0231 / 0271 / 0274, plus
-- three columns the survey measurands need that the se seeds did not:
-- `display_unit` (the unit the literature prints; canonical stays the
-- coherent SI unit of si_vector, the put-guard rule of ontology-v2 slice
-- 0a), `higher_is_better` where the direction is unambiguous (0212), and
-- `required_conditions` where ruled (overpotential: product,
-- current-density; TOF: product — docs/backlog/ontology-v2.md slice 2).
-- `aliases` seeds the reader/diff lookup keys (VocabEntry.lookup_keys):
-- an ALCHEM term whose slug equals a slug, name or alias pairs with no
-- ALCHEM_MEASURAND_MAP entry.
--
-- `parent` is the slug of a taxon to specialise instead of the
-- `measurand` start node (resolved live under `measurand`, including a
-- parent this file just minted; absent parent → the start node).
-- INSERTs ONLY MISSING NODES: a seed row is skipped when a live taxon
-- with the same slug already sits under `measurand` (any depth), every
-- node lands status='proposed', one ord -1 `card_combined` chunk with no
-- embedding, one `specialises` link. NO `measurand` start node → no-op.
--
-- SI vector order: m, kg, s, A, K, mol, cd.
--
-- Forward-only (ADR 0005). Squawk: INSERTs only, no DDL on live tables.

BEGIN;

CREATE TEMP TABLE _measurand_seed ON COMMIT DROP AS
SELECT v.ord,
       v.name,
       trim(both '-' from regexp_replace(lower(v.name), '[^a-z0-9]+', '-', 'g'))
           AS slug,
       jsonb_strip_nulls(jsonb_build_object(
           'name', v.name,
           'norm_name', lower(regexp_replace(v.name, '\s+', ' ', 'g')),
           'slug', trim(both '-' from
                        regexp_replace(lower(v.name), '[^a-z0-9]+', '-', 'g')),
           'definition', v.definition,
           'aliases', v.aliases,
           'status', 'proposed',
           'dimension_kind', v.dkind,
           'si_vector', v.si_vector,
           'canonical_unit', v.unit,
           'display_unit', v.display,
           'higher_is_better', v.hib,
           'required_conditions', v.req,
           'value_type', v.value_type,
           'allowed_values', v.allowed
       )) AS meta,
       v.parent
FROM (VALUES
    -- ── electrochemical performance ─────────────────────────────────
    (1, 'Overpotential',
     'Overpotential is the electrode potential an electrode needs beyond the thermodynamic equilibrium potential to drive its reaction at a given rate.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'mV', false,
     '["product", "current-density"]'::jsonb, 'quantity', NULL::jsonb,
     '["η", "eta"]'::jsonb, NULL::text),
    (2, 'Onset potential',
     'Onset potential is the electrode potential at which a reaction''s current first becomes measurable.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'V', NULL,
     NULL::jsonb, 'quantity', NULL,
     '["onset voltage"]'::jsonb, NULL),
    (3, 'Tafel slope',
     'Tafel slope is the overpotential increase per decade of current density, the kinetic steepness of an electrode reaction, stored in volts.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'mV', false,
     NULL, 'quantity', NULL,
     '["Tafel"]'::jsonb, NULL),
    (4, 'Exchange current density',
     'Exchange current density is the current density at which a reversible electrode reaction runs equally in both directions at equilibrium.',
     'si', '-2,0,0,1,0,0,0', 'A/m^2', 'mA/cm^2', true,
     NULL, 'quantity', NULL,
     '["j0", "exchange current per geometric area"]'::jsonb, NULL),
    (5, 'Charge transfer resistance',
     'Charge transfer resistance is the small-signal resistance an electrode-electrolyte interface presents to a faradaic reaction, in ohms.',
     'si', '2,1,-3,-2,0,0,0', 'ohm', 'ohm', false,
     NULL, 'quantity', NULL,
     '["Rct", "R_ct", "charge-transfer resistance"]'::jsonb, NULL),
    (6, 'Double-layer capacitance',
     'Double-layer capacitance is the capacitance of the electrochemical double layer at an electrode surface, per unit area.',
     'si', '-4,-1,4,2,0,0,0', 'F/m^2', 'uF/cm^2', NULL,
     NULL, 'quantity', NULL,
     '["Cdl", "C_dl", "double layer capacitance"]'::jsonb, NULL),
    (7, 'Cell voltage',
     'Cell voltage is the potential difference a full electrochemical cell operates at, in volts.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'V', NULL,
     NULL, 'quantity', NULL,
     '["battery voltage", "operating voltage", "discharge voltage"]'::jsonb,
     NULL),
    (8, 'Coulombic efficiency',
     'Coulombic efficiency is the charge a cell returns on discharge over the charge passed on charge, stored as a fraction.',
     'dimensionless', NULL, '1', '%', true,
     NULL, 'ratio', NULL,
     '["CE", "coulomb efficiency"]'::jsonb, NULL),
    (9, 'Capacity retention',
     'Capacity retention is the share of a cell''s initial capacity remaining after cycling, stored as a fraction.',
     'dimensionless', NULL, '1', '%', true,
     NULL, 'ratio', NULL,
     '["capacity retention rate", "retention"]'::jsonb, NULL),
    (10, 'Cycle life',
     'Cycle life is the number of charge-discharge cycles a cell completes before its capacity falls to a stated cutoff.',
     'count', NULL, 'count', 'count', true,
     NULL, 'quantity', NULL,
     '["cycling life", "cycle number"]'::jsonb, NULL),
    (11, 'Specific capacity',
     'Specific capacity is the charge a battery material stores per unit mass, stored in ampere-seconds per kilogram (the mAh/g the literature prints).',
     'si', '0,-1,1,1,0,0,0', 'A*s/kg', 'mAh/g', true,
     NULL, 'quantity', NULL,
     '["gravimetric capacity", "discharge capacity", "specific discharge capacity"]'::jsonb,
     NULL),
    (12, 'Dissolution rate',
     'Dissolution rate is the rate a material loses atoms or mass into its environment, stored per second.',
     'si', '0,0,-1,0,0,0,0', '1/s', '%/h', false,
     NULL, 'quantity', NULL,
     '["metal dissolution rate", "dissolution"]'::jsonb, NULL),
    (13, 'TOF',
     'TOF is the turnover frequency of a catalyst: product molecules produced per active site per second.',
     'si', '0,0,-1,0,0,0,0', '1/s', '1/s', true,
     '["product"]'::jsonb, 'quantity', NULL,
     '["turnover frequency"]'::jsonb, NULL),
    (14, 'ECSA',
     'ECSA is the electrochemically active surface area of an electrode, the area that participates in its reactions.',
     'si', '2,0,0,0,0,0,0', 'm^2', 'cm^2', true,
     NULL, 'quantity', NULL,
     '["electrochemically active surface area", "electrochemical active surface area"]'::jsonb,
     NULL),
    -- ── overpotential variants (specialise `overpotential`) ──────────
    (15, 'Nucleation overpotential',
     'Nucleation overpotential is the overpotential at which nuclei of a new phase first form on an electrode.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'mV', false,
     NULL, 'quantity', NULL,
     '["eta_n"]'::jsonb, 'overpotential'),
    (16, 'Coverage transition overpotential',
     'Coverage transition overpotential is the overpotential at which an electrode''s dominant adsorbate coverage switches.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'mV', false,
     NULL, 'quantity', NULL,
     '[]'::jsonb, 'overpotential'),
    (17, 'Overpotential gap',
     'Overpotential gap is the separation between two characteristic overpotentials of one cell, such as the charge-discharge plateau gap.',
     'si', '2,1,-3,-1,0,0,0', 'V', 'mV', NULL,
     NULL, 'quantity', NULL,
     '[]'::jsonb, 'overpotential'),
    -- ── energetics ──────────────────────────────────────────────────
    (18, 'Band gap',
     'Band gap is the energy difference between a material''s valence and conduction band edges (the HOMO-LUMO gap of a molecule).',
     'si', '2,1,-2,0,0,0,0', 'J', 'eV', NULL,
     NULL, 'quantity', NULL,
     '["Eg", "E_g", "bandgap", "optical gap"]'::jsonb, NULL),
    (19, 'Formation energy',
     'Formation energy is the energy change when a compound or defect forms from its constituents in their reference states.',
     'si', '2,1,-2,0,0,0,0', 'J', 'eV', NULL,
     NULL, 'quantity', NULL,
     '["E_f"]'::jsonb, NULL),
    (20, 'Vacancy formation energy',
     'Vacancy formation energy is the energy cost of removing one atom from a crystal lattice, the formation energy of a monovacancy.',
     'si', '2,1,-2,0,0,0,0', 'J', 'eV', NULL,
     NULL, 'quantity', NULL,
     '["E_vac", "vacancy formation enthalpy"]'::jsonb, 'formation-energy'),
    (21, 'D-band center',
     'D-band center is the energy of the centroid of a metal''s d-band relative to its Fermi level, the chemisorption descriptor.',
     'si', '2,1,-2,0,0,0,0', 'J', 'eV', NULL,
     NULL, 'quantity', NULL,
     '["d-band centre", "d band center", "ε_d", "epsilon_d"]'::jsonb, NULL),
    -- ── bulk and structure / spectroscopy ───────────────────────────
    (22, 'BET surface area',
     'BET surface area is a material''s specific surface area measured by Brunauer-Emmett-Teller gas adsorption, per unit mass.',
     'si', '2,-1,0,0,0,0,0', 'm^2/kg', 'm^2/g', true,
     NULL, 'quantity', NULL,
     '["specific surface area", "BET area", "SSA"]'::jsonb, NULL),
    (23, 'Particle size',
     'Particle size is the characteristic diameter of a particle, crystallite or nanoparticle.',
     'si', '1,0,0,0,0,0,0', 'm', 'nm', NULL,
     NULL, 'quantity', NULL,
     '["particle diameter", "diameter", "nanoparticle size"]'::jsonb, NULL),
    (24, 'Pore volume',
     'Pore volume is the void volume inside a porous material per unit mass.',
     'si', '3,-1,0,0,0,0,0', 'm^3/kg', 'cm^3/g', NULL,
     NULL, 'quantity', NULL,
     '["total pore volume", "specific pore volume", "pore volume per mass"]'::jsonb,
     NULL),
    (25, 'Pore size',
     'Pore size is the characteristic width or diameter of the pores in a porous material.',
     'si', '1,0,0,0,0,0,0', 'm', 'nm', NULL,
     NULL, 'quantity', NULL,
     '["pore diameter", "average pore size", "mean pore size", "BJH pore size"]'::jsonb,
     NULL),
    (26, 'Interplanar spacing',
     'Interplanar spacing is the distance between adjacent planes of a crystal lattice, its d-spacing.',
     'si', '1,0,0,0,0,0,0', 'm', 'Å', NULL,
     NULL, 'quantity', NULL,
     '["d-spacing", "d spacing", "lattice spacing"]'::jsonb, NULL),
    (27, 'Bond length',
     'Bond length is the equilibrium distance between the nuclei of two bonded atoms.',
     'si', '1,0,0,0,0,0,0', 'm', 'Å', NULL,
     NULL, 'quantity', NULL,
     '["bond distance", "bondlength"]'::jsonb, NULL),
    (28, 'Lattice parameter',
     'Lattice parameter is the length of a crystal unit cell edge.',
     'si', '1,0,0,0,0,0,0', 'm', 'Å', NULL,
     NULL, 'quantity', NULL,
     '["lattice constant", "unit cell parameter", "cell parameter"]'::jsonb,
     NULL),
    (29, 'XRD peak position',
     'XRD peak position is the diffraction angle 2θ of an X-ray diffraction peak, in degrees.',
     'dimensionless', NULL, 'deg', 'deg', NULL,
     NULL, 'quantity', NULL,
     '["2theta", "two theta", "diffraction angle", "Bragg angle"]'::jsonb,
     NULL),
    (30, 'Raman peak position',
     'Raman peak position is the wavenumber shift of a Raman band from the excitation line, in inverse centimetres.',
     'si', '-1,0,0,0,0,0,0', '1/m', 'cm^-1', NULL,
     NULL, 'quantity', NULL,
     '["Raman shift", "Raman band position", "Raman band"]'::jsonb, NULL),
    (31, 'FTIR band position',
     'FTIR band position is the wavenumber of an infrared absorption band, in inverse centimetres.',
     'si', '-1,0,0,0,0,0,0', '1/m', 'cm^-1', NULL,
     NULL, 'quantity', NULL,
     '["FTIR peak position", "IR band position", "infrared band position"]'::jsonb,
     NULL),
    -- ── composition and structure descriptors ───────────────────────
    (32, 'Mass fraction',
     'Mass fraction is the share of a mixture''s mass carried by one component, stored as a fraction.',
     'dimensionless', NULL, '1', '%', NULL,
     NULL, 'ratio', NULL,
     '["weight fraction", "wt%", "mass percent", "weight percent"]'::jsonb,
     NULL),
    (33, 'Oxidation state',
     'Oxidation state is the formal charge an atom carries when its bonds are broken heterolytically, a signed number.',
     'dimensionless', NULL, '1', NULL, NULL,
     NULL, 'quantity', NULL,
     '["valence state", "ox state", "formal oxidation state"]'::jsonb, NULL),
    (34, 'Coordination number',
     'Coordination number is the number of nearest neighbours bound to an atom.',
     'count', NULL, 'count', 'count', NULL,
     NULL, 'quantity', NULL,
     '["CN"]'::jsonb, NULL),
    (35, 'Areal loading',
     'Areal loading is the mass of active material or catalyst deposited per unit electrode area, in kilograms per square metre (the mg/cm^2 the literature prints) — distinct from `catalyst-loading`, a molar fraction.',
     'si', '-2,1,0,0,0,0,0', 'kg/m^2', 'mg/cm^2', NULL,
     NULL, 'quantity', NULL,
     '["mass loading", "catalyst mass loading", "electrode areal loading", "areal mass loading"]'::jsonb,
     NULL)
) AS v (ord, name, definition, dkind, si_vector, unit, display, hib, req,
        value_type, allowed, aliases, parent);

-- Live taxa under `measurand` (any depth): the dedup set AND the table
-- `parent` slugs resolve against.
CREATE TEMP TABLE _measurand_under ON COMMIT DROP AS
WITH RECURSIVE root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
),
under (ref_id) AS (
    SELECT ref_id FROM root
    UNION
    SELECT l.src_ref_id
      FROM links l
      JOIN under u ON l.dst_ref_id = u.ref_id
      JOIN refs c ON c.ref_id = l.src_ref_id
                 AND c.kind = 'taxon' AND c.retired_at IS NULL
     WHERE l.relation = 'specialises'
       AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
)
SELECT DISTINCT u.ref_id, r.meta->>'slug' AS slug
  FROM under u JOIN refs r ON r.ref_id = u.ref_id;

-- Nodes, card chunks and positions: only the missing ones. The ord -1
-- card_combined chunk has embedding NULL (the worker embeds); text =
-- precis.taxonomy.nodes.taxon_card_text(name, definition, aliases).
-- Position: `parent` slug → that taxon (live under `measurand`, or one
-- this file just minted); else the `measurand` start node.
WITH root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
     ORDER BY ref_id
     LIMIT 1
),
ins AS (
    INSERT INTO refs (kind, title, meta)
    SELECT 'taxon', s.name, s.meta
      FROM _measurand_seed s
     WHERE NOT EXISTS (SELECT 1 FROM _measurand_under u
                        WHERE u.slug = s.slug)
       AND EXISTS (SELECT 1 FROM root)
     ORDER BY s.ord
    RETURNING ref_id, meta
),
card AS (
    INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
    SELECT ins.ref_id, -1, 'card_combined',
           (ins.meta->>'name') || ' — ' || (ins.meta->>'definition') ||
           CASE WHEN jsonb_array_length(ins.meta -> 'aliases') > 0
                THEN ' (aka ' || (SELECT string_agg(a, ', ')
                                  FROM jsonb_array_elements_text(
                                       ins.meta -> 'aliases') AS a)
                     || ')'
                ELSE '' END,
           '{}'::jsonb
      FROM ins
    RETURNING ref_id
)
INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
SELECT ins.ref_id,
       COALESCE(
           (SELECT u.ref_id FROM _measurand_under u
             WHERE u.slug = s.parent
             ORDER BY u.ref_id LIMIT 1),
           (SELECT i2.ref_id FROM ins i2
             WHERE i2.meta->>'slug' = s.parent
             LIMIT 1),
           root.ref_id),
       'specialises', 'system'
  FROM ins
  JOIN _measurand_seed s ON s.slug = ins.meta->>'slug'
 CROSS JOIN root;

COMMIT;

-- End of 0305_survey_measurand_seed.sql
