# hexfold (export seed)

This folder is the pip re-export seed for the `hexfold` package. The code
lives in `src/hexfold/` and the format specification is
`src/hexfold/spec.md`. This folder holds only the parts a standalone
`hexfold` distribution needs alongside that code:

- `examples/` — sample `.hx` files (tubes, cones, sheets, fullerenes,
  nanobuds, seams).
- `docs/pillar.png` — rendered preview of `examples/pillar.hx`.
- `CITATION.cff` — citation metadata.
- `.zenodo.json` — Zenodo deposit metadata.
- `LICENSE` — MIT, same terms as `src/hexfold/LICENSE`.

Nothing here is imported by the monorepo; this is packaging material only.
