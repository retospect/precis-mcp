# figure kind — deferred slices (slice 1 shipped)

Ordered by value: PNG/animated-raster export (a figure_render derived-lane
job + resvg with declarative keyframes — no headless browser; PNG first);
three.js `scene3d` mode (meta.render ∈ {svg,scene3d}, declarative scene IR +
trusted client renderer — never eval raw three.js); per-node chunk split (one
chunk per top-level element); draft-embedding (rendered raster as asset +
figure-in→draft link); a `read(handle)` reference tool in the turn loop; pin
the full precis-figure-svg skill text into the turn prompt (polish); opt-in
palette-allowlist lint. Owner `src/precis/figure/`,
`src/precis/handlers/figure.py`.
