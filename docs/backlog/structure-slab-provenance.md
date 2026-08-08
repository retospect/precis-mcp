# structure IR lacks slab/adsorbate provenance

`src/precis/structure/preflight.py::_slab_adsorbate_indices` falls back to a
dominant-element heuristic when `atoms.info['n_slab']` is unset — and no
caller can set it (the Scene IR records no "these atoms came from the slab
op"). A doped slab (Cu/Ag dopant via set_element) risks the `detached` check
misreading the dopant as a floating adsorbate. Add n_slab (or richer
op-provenance) at slab-op time and thread it through preflight(). Owner
`src/precis/structure/scene.py`, `src/precis/structure/ops.py`. Polish.
