# three-cad-viewer (vendored)

`three-cad-viewer.esm.min.js` is three-cad-viewer **5.0.6** built from its
source tag (github.com/bernhard-42/three-cad-viewer, `v5.0.6`) with one
change: bare `three` is external (`external: ["three"]` on the minified ES
build in `rollup.config.mjs`). The published npm bundle carries its own
three.js inside, and the se 3D page's atom overlay then loaded a second
copy beside it (Safari: "Multiple instances of Three.js being imported").

The page resolves `three` with an import map
(`templates/blocktree/detail3d.html.j2`) to `/static/three-r184/`: three
**0.184.0**, the version the viewer's `package.json` pins. The viewer's
three addons (`three/examples/jsm/...`), n8ao and postprocessing stay
bundled; they import bare `three` too, so they share the same copy.
`three-cad-viewer.css` is the published file, unchanged.

To rebuild, at the new tag: add the `external` line to the minified ES
build, `yarn install --frozen-lockfile --ignore-scripts`,
`SOURCEMAP=false yarn build`, copy `dist/three-cad-viewer.esm.min.js`
here and `node_modules/three/build/three.{module,core}.min.js` to
`../three-r184/` (renaming the directory to the new three version), then
run `scripts/viewer_check.py strain` and `probe`.

`/static/three/` (r160) is the cad page's own three.js and is unrelated.
