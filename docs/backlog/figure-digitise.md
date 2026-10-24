---
status: draft
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
   pa186444's PDF is Reto's file, not a stored one; see open question 1.
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
4. **Vision proposal, checked.** A vision model sees the crop and proposes
   axis ranges, tick labels, series count, legend mapping, rough peak
   positions. Each claim is checked against steps 2-3 (tick values against
   the fit; a peak must lie within 3 px of a local maximum of the extracted
   polyline). Failing claims are returned as `unverified` and never reach
   the table. Spend follows `docs/conventions/thresholds.md`.
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

Recommendation: numpy + scipy + pillow for the core (Hough on a cropped
panel is a short numpy accumulator). Add `opencv-python-headless` to a new
`digitise` extra only if slice 2 shows the numpy path is too slow or
brittle.

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
  derived | asserted`; a value read off a plot fits none cleanly (open
  question 2).
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
   seeded trials.
3. **Render + checked proposal** (`figdigit/page.py`, `figdigit/propose.py`).
   Test: with a fake proposer, a wrong tick label and a hallucinated peak
   are both returned `unverified`; no network.
4. **MCP view + write path** (`view='digitise'`, sidecar, measure rows).
   Test: the view writes no rows; the sidecar's rows pass `_measure_write`
   validation including `value_err`.
5. **First consumer**: digitise pa186444 Fig. 1; swap the derived 2 and
   5 K/s CO/Pd(111) rows for the measured four; commit the sidecar. Test:
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

## Open questions for Reto

1. Where does the PDF come from? pa186444 was read from your PDF, not a
   stored file. Should ingest keep the source PDF (or per-figure crops) for
   held papers, or is the digitiser handed a local path or image each time?
2. `measures.tier` has no value for a number read off a plot (`measured`
   needs an anchored span of printed text). Add a fifth tier `digitised`,
   or store these as `tier='derived'` with `derived_from` the figure chunk
   and mark digitised in `note`?
3. Dependency: numpy/scipy/pillow only, or accept `opencv-python-headless`
   (Apache-2.0, already locked via surya-ocr) as a direct dependency of a
   new `digitise` extra?
4. Vision proposer: which model tier and budget per figure, and is a fully
   offline geometry-only mode (caller names colours and tick values)
   acceptable when no model is available?
5. Acceptance: is "within stated sigma of your own read of Fig. 1" the bar,
   or should a second paper with a printed table of the same series
   validate the tool first?
