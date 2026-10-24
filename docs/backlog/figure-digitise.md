---
status: in-progress
claimed-by: fig-digitise-1-3
title: Figure digitiser - calibrated, uncertainty-bearing numbers from a plotted series (first use pa186444 Fig. 1)
pillar: local-compute
prio: normal
---

# Figure digitiser (gr514185)

## Problem

Papers often print a result only as a plotted curve. precis can quote a
figure's caption (skill `precis-paper-help`, "Cite a figure (caption only -
no image binaries)": the `![](...)` marker is a relative path nothing
serves) but cannot turn the picture into numbers. Gripe gr514185 (Reto
2026-10-24): "keep A and digitize fig 1, do we have tools for that or is
there missing capability we need gripes for." Answer: no tool exists.
`corpus-quantitative-extraction.md` lists "OCR, supplements, figure
digitisation, curve refits" as out of scope, and
`class-lattice-similarity-spaces-and-laws.md` already assumes a
`curve(..., source: SI | digitised figure)` object that nothing can fill.

## First consumer

`kinetics-engine-v2.md`, "Decisions (slice 9, coordinator 2026-10-24)": the
CO/Pd(111) TPD reference keeps option A (2 and 5 K/s rows derived from
pa498800 via Polanyi-Wigner, `source_role: derived`). Option C - pa186444
(Yu et al.), beta = 0.5 / 1 / 2 / 4 K/s after a 10 L dose - replaces them
once its peak temperatures exist, and they exist only in Fig. 1. The
replacement is data-only: rows in `tests/fixtures/kinetics/tpd_reference.json`
(row shape in `tests/fixtures/kinetics/README.md`) with
`source_role: digitised`, a value, a sigma and a pointer to the digitiser
artifact.

Acceptance for the first consumer: four T_p values (one per beta), each with
an uncertainty in K, each within that uncertainty of an independent read by
Reto.

## Approach

Deterministic geometry does the measuring; a vision model only proposes.
Nothing a model says becomes a number without passing a pixel check.

1. **Input.** A raster crop of one figure panel: (a) a PDF page rendered
   with PyMuPDF (`pymupdf`, `paper` extra in `pyproject.toml`) clipped to a
   bbox at about 600 dpi, or (b) an image file the caller supplies. Ingest
   already extracts figure images (`src/precis/ingest/figures.py`:
   `match_figure_captions`, `encode_image`) but nothing serves them, and
   pa186444's PDF is Reto's file, not a stored one; the caller hands the digitiser a path or image (Decisions 1).
2. **Axis calibration from tick labels.** Find the axis lines (projection
   profile or Hough), tick marks along them, and tick labels (vision
   proposal, then verified: values must be monotone along the axis and the
   pixel spacing must fit a linear or log map with residual under 0.5 px,
   else refuse). Output: two or more `(pixel, value)` calibration points per
   axis plus the fitted map and residual. Log and reversed axes are declared
   by the caller or proposal and checked, never silently inferred.
3. **Series extraction.** Colour mask (k-means on non-grey pixels, or a
   legend swatch colour the caller names), then
   - markers: connected components, centroid each; or
   - curves and peaks: per-column median of mask pixels gives a polyline;
     peak is the argmax with a parabolic sub-pixel fit.
   Black-on-white overlays use a dark mask after removing the axes and text
   boxes found in step 2.
4. **Vision proposal, checked.** A Sonnet vision proposer (about $0.05 per
   figure) sees the crop and proposes
   axis ranges, tick labels, series count, legend mapping, rough peak
   positions. Each claim is checked against steps 2-3 (tick values against
   the fit; a peak must lie within 3 px of a local maximum of the extracted
   polyline). Failing claims are returned as `unverified` and never reach
   the table. An offline geometry-only mode skips the proposer: the caller
   supplies series colours and tick values, and steps 2-3 still verify them.
   Spend follows `docs/conventions/thresholds.md`.
5. **Uncertainty in data units.** Per point, in quadrature: half-pixel
   quantisation times the local data-per-pixel, the calibration residual,
   and for peaks the width of the sub-pixel fit. The result is `value +/-
   sigma` in the axis unit, with a one-line statement of the dominant term.

### Candidate libraries (checked 2026-10-24)

Licences from PyPI JSON metadata and `uv.lock`; no `uv` dry-run install was
run.

| Library | Licence | Status in this repo | Use |
|---|---|---|---|
| numpy, scipy | BSD-3 | direct (`numpy>=1.24`, `scipy>=1.14` in `pyproject.toml`) | axis fit, peak fit, `scipy.ndimage` labelling, `scipy.cluster.vq` k-means |
| pillow | HPND | transitive; floor via `[tool.uv] override-dependencies`; `guide` extra | decode, crop |
| matplotlib | PSF-based | direct (`matplotlib>=3.7`) | overlay of extracted points on the crop, for review |
| pymupdf | AGPL-3.0 / commercial | `paper` extra | page-clip render; already shipped, no new exposure |
| opencv-python-headless | Apache-2.0 | `uv.lock` has 4.11.0.86 only via `surya-ocr` (marker chain); PyPI current 5.0.0.93; not a direct dep | Hough lines, `inRange`, components |
| scikit-image | BSD-3 (0.26.0) | not in `uv.lock` | `hough_line`, `label`; heavier than needed |
| plotdigitizer | LGPL-3.0-or-later (0.3.0) | not in `uv.lock` | not adopted: LGPL and manual-click workflow |

Decided (Decisions 3): numpy + scipy + pillow for the core (Hough on a
cropped panel is a short numpy accumulator). Add `opencv-python-headless` to
a new `digitise` extra only if slice 2 shows colour masking needs it.

## Output and storage

Existing kinds only; no new kind.

- **The numbers: `measure` rows** (`MeasureHandler` in
  `src/precis/handlers/measure.py`; write path
  `src/precis/handlers/_measure_write.py`, fields include `value_num`,
  `value_err`, `reported_unit`, `subject`, `anchor`, `tier`, `note`). One
  row per extracted value, `anchor = {chunk, anchor_scheme, span}` pointing
  at the paper's figure chunk (the `pc` block with the image marker and
  caption). beta goes in as a condition input row like any other run.
  `TIERS` in `src/precis/store/_measures_ops.py` is `measured | computed |
  derived | asserted`, enforced by the CHECK `measures_tier_check`
  (`0187_measures.sql`); a value read off a plot fits none, so this item adds
  tier `digitised` (Decisions 2).
- **The calibration record: a JSON sidecar** per panel, next to the
  consumer's fixture (slice 9 puts fixtures in catpath package data).
  Fields: `figure` (paper handle, figure chunk handle, page, bbox, dpi),
  `image_sha256`, `axes` (linear or log, calibration points `(px, value)`,
  fit, residual), `series` (colour, legend label, method), `points` (`px`,
  `value`, `sigma`, `verified`), tool version. The fixture row's
  `source_role: digitised` carries the sidecar id; the `measure` row's
  `note` carries id + sha, not a copy. Sidecars hold numbers and pixel
  coordinates, never the image (fixtures rule in
  `tests/fixtures/kinetics/README.md`: ids, hashes and numbers, not prose).
- **`source_role`** exists only in the kinetics fixture schema today
  (`tests/fixtures/kinetics/README.md` lists primary, quoted, reanalysis,
  review-compiled, simulated; `derived` is used by the slice-9 decision but
  is not in that list). This item adds `digitised` there and nowhere else.

## MCP surface

`kind='figure'` is the SVG drawing canvas (skill `precis-figure-help`), not
a paper figure, so it is the wrong home. Instead:

- **`get(kind='paper', id='pa186444~pc<fig>', view='digitise',
  args={bbox, dpi, series})`**: read-only; returns calibration, per-series
  table, uncertainty line, overlay image path, `unverified` claims. Refuses
  with a next step when no PDF or image is available.
- **`put(kind='measure', ...)`**: writes the accepted rows (existing verb
  and validation).
- **`scripts/digitise`**: CLI over the same library for a local PDF or
  image; emits the sidecar.

The vision call is internal to the view, not an agent-facing verb.

## Slices

1. **Calibration core** (`src/precis/figdigit/axis.py`, numpy only). Test:
   a synthetic matplotlib figure with known ticks recovers the map within
   0.5 px; non-monotone tick labels are refused.
2. **Series extraction** (`figdigit/series.py`): colour mask, centroids,
   polyline, sub-pixel peak with sigma. Test: synthetic TPD-like traces
   (Gaussian peaks at known T, four rates, noise, similar colours); the
   recovered T_p lies within the reported sigma in at least 95 percent of
   seeded trials. If colour masking is too slow or brittle in numpy, this
   slice adds the `digitise` extra with `opencv-python-headless`.
3. **Render + checked proposal** (`figdigit/page.py`, `figdigit/propose.py`):
   Sonnet vision proposer plus the offline geometry-only mode (caller names
   colours and tick values). Test: with a fake proposer, a wrong tick label
   and a hallucinated peak are both returned `unverified`; the offline mode
   needs no proposer; no network.
4. **MCP view + write path + tier migration** (`view='digitise'`, sidecar,
   measure rows). Forward-only migration `0240_measures_tier_digitised.sql`
   (next free number after `0239_vault_key_table.sql`; recheck at build):
   `measures.tier` is `CHECK (tier IN ('measured','computed','derived',
   'asserted'))`, constraint `measures_tier_check` from `0187_measures.sql`,
   so the migration drops and re-adds it with `'digitised'`, updates the
   column comment, and `TIERS` in `src/precis/store/_measures_ops.py` gains
   `digitised`. `0188_measures_si.sql` reads `tier` in a view; check it
   still resolves. Test: the view writes no rows; the sidecar's rows pass
   `_measure_write` validation including `value_err` with `tier='digitised'`.
5. **Validation gate: a paper with a printed table.** Before the tool is
   trusted on pa186444, digitise a figure from a held paper that prints the
   same series in a table (pick the paper at build; a TPD or similar
   peak-position series is best). Gate: every digitised value lies within its
   reported sigma of the tabulated value; record the paper handle and the
   residuals in the sidecar. A miss blocks slice 6 and returns to slices 1-3.
6. **First consumer**: digitise pa186444 Fig. 1; swap the derived 2 and
   5 K/s CO/Pd(111) rows for the digitised four; commit the sidecar. Test:
   the fixture loader requires a sidecar id on every `digitised` row, and
   the slice-9 kinetics tests still pass.

## Out of scope

- Curve refits and model fitting (`corpus-quantitative-extraction.md`;
  `fit`/`law` in `class-lattice-similarity-spaces-and-laws.md`).
- OCR of scanned pages; bar charts, error-bar extraction, 3-D, contour and
  ternary plots. First version: 2-D scatter and line plots, linear or log
  axes.
- An interactive click-to-calibrate UI.
- Serving paper figure images through `get(kind='paper')` generally.

## Decisions (Reto 2026-10-24)

Answers "defaults", relayed by catpath-29.

1. **Input.** The caller hands the digitiser a path or image. Whether ingest
   keeps the source PDF or per-figure crops is **explicitly deferred**, a
   separate decision and not in scope here.
2. **Tier.** New `measures.tier` value `digitised` (migration in slice 4).
3. **Dependencies.** numpy, scipy, pillow first; `opencv-python-headless`
   (new `digitise` extra) only if colour masking proves to need it.
4. **Proposer.** Sonnet vision, about $0.05 per figure, plus an offline
   geometry-only mode where the caller supplies colours and tick values.
5. **Acceptance.** Validate against a paper that prints the same series in a
   table (slice 5) before trusting the tool on pa186444 Fig. 1 (slice 6,
   whose own bar is the first-consumer acceptance above).
