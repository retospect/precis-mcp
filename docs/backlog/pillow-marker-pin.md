# Dependabot pillow #56–67 blocked on marker-pdf's Pillow<11 cap

The fix needs pillow>=12.3.0; marker-pdf 2.0.0 still pins pillow<11 — the
constraint intersection is empty (no patched Pillow exists below 11).
Tolerable: precis only feeds Marker/Pillow trusted PDFs behind the [paper]
extra; the specific vectors (PSD/FITS/JPEG2000/TGA/mmap font paths) aren't
reachable from precis code. Recheck cadence: re-run
`uv lock --upgrade-package pillow`; take the fix if it reaches ≥12.3.0, else
bump the recheck +2 weeks. Same shape: the transformers>=5.3.0 / marker-pdf
pin (Dependabot #44) — recheck alongside. Blocked upstream.
