---
snooze-until: 2026-09-09
---

# Dependabot pillow #56–67 blocked on marker-pdf's Pillow<11 cap

The fix needs pillow>=12.3.0; marker-pdf 2.0.0 still pins pillow<11 — the
constraint intersection is empty (no patched Pillow exists below 11).
Tolerable: precis only feeds Marker/Pillow trusted PDFs behind the [paper]
extra; the specific vectors (PSD/FITS/JPEG2000/TGA/mmap font paths) aren't
reachable from precis code. Recheck cadence: re-run
`uv lock --upgrade-package pillow`; take the fix if it reaches ≥12.3.0, else
bump the recheck +2 weeks. Same shape: the transformers>=5.3.0 / marker-pdf
pin (Dependabot #44) — recheck alongside. Blocked upstream.

2026-08-10: rechecked, marker-pdf still pins pillow<11. `uv lock
--upgrade-package pillow` resolves 12.3.0 only under the unused
`sys_platform == 'win32'` fork; every darwin/linux fork still pins 10.4.0
because marker-pdf 2.0.0 (still latest on PyPI) requires `pillow<11,>=10.1.0`.
No newer marker-pdf release exists. Lock reverted; still blocked.

2026-08-26: rechecked PyPI — marker-pdf latest is still 2.0.0 with
`pillow<11,>=10.1.0`. Still blocked; snoozed +2 weeks.
