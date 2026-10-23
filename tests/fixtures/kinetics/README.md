# TPD / adsorption reference dataset — kinetics v2 slice 9 (D11)

`tpd_reference.json` is the literature reference set for the TPD
validation and `tpd_corrected` adsorption-energy offset of
`docs/backlog/kinetics-engine-v2.md` §D11. Built 2026-10-23 by re-reading
every cited value in the papers through the session `precis` MCP
(`get(kind='paper', id=…)`); papers selected from §D11's starting set and
the TPD/adsorption papers linked by quest `qu500066`.

## Files

- `tpd_reference.json` — the table. `measurements[]` has one row per
  measurement (or per state within a spectrum).
- `tpd_reference_chunks.json` — provenance snapshot of every cited
  chunk, exported read-only from prod. Each entry is
  `{sha256, printeds}`: the sha256 of the full chunk text (checkable
  against prod) and the verbatim `printed` snippets cited from it.
  `tests/test_tpd_reference_fixtures.py` checks the table against it,
  so CI needs no prod access. **Standing rule (Reto 2026-10-23): never
  commit paper chunk text into this public repo — fixtures carry ids,
  hashes and numbers, not prose.**

## Row shape

Each measurement: `id`, `adsorbate`, `surface` (facet, or alloy
composition), `paper` (the `pa…` handle the numbers were read from),
`source_role`, `method`, `note`, and `values`.

`source_role`: `primary` (measured in the citing paper), `quoted` (the
paper reports another work's value), `reanalysis` (parameters re-derived
from published spectra), `review-compiled`, `simulated` (forward-simulated
TPD — a method template, not a measurement).

`method`: `tpd` (peak stated, no analysis given), `redhead`,
`leading-edge`, `ttpd-complete` / `inversion-complete`,
`peak-shape invariants` (Chan–Aris–Weinberg), `isosteric`,
`calorimetry`, `isothermal-vrk` / `arrhenius-isothermal+tst`
(velocity-resolved, no ramp), `isothermal+isosteric-rates`,
`dft-scaled-peak-family-fit`, `kmc-sim`,
`dft-microkinetic TPD simulation`, or a combination.

`values` maps a quantity name to `{value, unit, printed, chunk, note?}`:

- `value` — the interpreted number (a string for ranges, exponentials
  like `"10^15.65"`, or functional forms like `"19.2-1.9*theta"`).
- `unit` — `K`, `degC`, `kcal/mol`, `kJ/mol`, `eV`, `s-1`, `ML`, `MLE`,
  `%`, `cm2/(particle s)`, `ML-1 s-1`, or null.
- `uncertainty` — when the paper prints one (same unit as `value`).
- `printed` — the verbatim substring as it appears in the cited chunk,
  OCR artifacts included: the number plus a few words of label at most,
  never a prose window. The test requires `printed` to be recorded
  under the cited chunk in the snapshot, and that its leading number
  equals `value` unless the row sits in the test's reasoned exemption
  list (OCR-garbled digits, dropped `±`, orders printed as words). A
  `printed` under ~6 characters must carry the unit or a label —
  lengthen it from the source rather than storing a bare number. A
  value with no chunk does not go in.
- `chunk` — the `pc…` handle carrying the number.
- `dimension` — on every `nu*` (prefactor) quantity: `first_order`
  (units `s-1`) or `second_order` (units `cm2/(particle s)`,
  `ML-1 s-1`), so slice 9's converter cannot mix the two rate-law
  dimensions.

Quantity names: `t_p_K` (plus `t_p_K_sat`, `t_p_K_low_theta`, state
suffixes, `t_p_K_initial_abstract` for an abstract-stated initial Tp,
`t_p_low_C`/`t_p_bulk_C` when printed in °C), `fwhm_K`,
`beta_K_s` (or `beta_K_s_range`), `theta0`/`theta0_ML`/`theta_sat_ML`,
`dose_T_K` (dosing temperature, not coverage), `order`,
`e_des`/`e_ads` (plus `_initial`, `_low_theta`, `_theta_lo`,
`_theta_hi`, `_diss`, state suffixes; `e_des_theta_form` for printed
coverage-dependent forms), `nu_prefactor` (or `nu_theta_lo`,
`nu_diss`), and paper-specific extras (`e_diffusion`, `delta_est`,
`s0`, `bep_alpha`, `t_compensation_K`, `pumping_tau_s`).

## Units and conventions

- Values stay in the units printed in the paper: kcal/mol and kJ/mol
  coexist (1 kcal/mol = 4.184 kJ/mol ≈ 0.04336 eV); slice 9 converts when
  it builds `tests/validation/tpd/<surface>/<adsorbate>.yaml`.
- `θ` is in ML as printed (per surface atom, or per the paper's MLE
  calibration where stated).
- OCR artifacts in `printed` are deliberate: they are what the chunk
  contains (`lOI` = 10^16 exponents, `moie` = mole, `4OO` = 400, a
  dropped `±` giving `1267` = 126±7). The `note` field records the
  interpretation.

## What was excluded and why

- `pa498690` (de Jong–Niemantsverdriet) — the comparative test uses
  simulated spectra; the "correct" input parameters are not measurements.
- `pa498695` (Schmid–Parkinson–Diebold) — methods paper; its single
  experimental application (CO/Fe3O4(001)) is in the SI, not in the
  corpus.
- `pa50250` (Redhead 1962) — one-page ingest; only the method
  introduction is held.
- `pa498691` (Campbell–Sellers review) — card holds journal boilerplate
  only.
- `pa498692`, `pa498694`, `pa498697`, `pa498698` — methods/prefactor
  papers; abstracts carry no measurement rows (pa498692's
  S_ad = 0.70·S_gas − 3.3R prefactor correlation is noted here).
- `pa498702`, `pa498710`, `pa498713`, `pa498716`, `pa498805`,
  `pa186444` — abstract-only cards with no extractable numbers
  (qualitative statements only).
- `pa498696` — DFT+MC study of CO/Pt(111) desorption; comparison to
  calorimetry/TPD is its subject, but no reference numbers in held text
  beyond the abstract claim.
- `pa481739` — TPD methods review (experimental methods in chemical
  engineering); no new measurements.
- `pa498711` SAA rows are `simulated` rows, kept as method templates per
  §D11 ("Simulated-TPD benchmarks"), not as reference data.
- N→N2 on Pd: per §D11 no standalone recombinative-N2 TPD was located;
  `tpd.N.reference = null` stands (N2 appears only as an NO-TPD product
  in pa498706/pa498707).
- H/Pd(111) β-state rows carry the subsurface/bulk-reservoir caveat the
  D11 refusal rule names (pa498700, pa498701): the α and ~800 K features
  are reservoir states, flagged in `note`, not clean surface references.
