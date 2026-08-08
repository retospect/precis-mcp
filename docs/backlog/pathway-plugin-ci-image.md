# pathway plugin has no CI coverage until the dev image carries autocatpath

`tests/test_pathway_plugin.py` opens with importorskip("autocatpath"); the
baked precis-dev image still carries the old `catpath` module, so the gate
silently skips all 18 tests (verified green manually via a UV_WITH editable
mount). `scripts/build-image` already threads AUTOCATPATH_REV — rebuild the
image on the next refresh and the file auto-runs. Mechanical.

test: tests/test_pathway_plugin.py runs (not skipped) in the gate.
