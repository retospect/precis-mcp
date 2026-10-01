// blocktree-3d.js — the se/nm 3D reader's client glue (round 2a,
// gr335242 comment 5 / docs/backlog/multiscale-design-system-spec.md
// §5.8). Server side builds the whole bundle
// (precis_web.blocktree_3d.build_scene, served as scene3d.json); this
// file only wires the vendored three-cad-viewer to it and links the
// mermaid topology panel — no geometry/traversal logic lives here.
//
// Vertex/edge/face/solid picking, three-state visibility, and the
// hierarchical assembly tree are all native to the vendored viewer —
// nothing to wire for those, EXCEPT the "connections" group's own
// eyeball (gr337746 — see the connections-toggle section below: its
// tree-node visibility icon is permanently disabled by the vendored
// model itself for an edges-only leaf, so a working toggle needs our
// own page chrome driving the viewer's public setState() API instead).
//
// This file also budgets/measures the Display's own footprint against
// its shell (gr337753/gr337747, sizing bugs fixed gr340029 — see
// _fitViewerToShell/_fitShellHeight/_applyTreeHeightCap below): the
// vendored chrome (toolbar row, tree panel) is added AROUND the
// cadWidth/height canvas, not accounted for by it, AND the shell itself
// is now sized to the live browser viewport (gr340029 — a fixed 520px
// shell could put the vendored "Ready" status box and the tree panel's
// own picking-filter dropdown below/past the fold, with page scroll
// trapped by the canvas's own wheel-zoom whenever the pointer sits over
// it).
//
// gr340030 adds a scale-bar overlay (see the "display scale" section
// near the bottom): the server's own display-scale multiplier
// (``scene.scale`` — precis_web.blocktree_3d.scene_scale) means an
// on-screen length carries no real-world meaning by itself; the overlay
// reads the vendored camera's live zoom to convert a screen-pixel
// length back to real SI metres.
//
// KNOWN SIMPLIFICATION (documented, not silently assumed): the
// vendored viewer's single-click "select" tool only fires a
// notification once the SELECT tool is toggled on (its `selected`
// state carries topology sub-indices, not a leaf path — meant for
// jupyter-cadquery's own vertex/edge/face index readback, not "which
// block did I click"). A plain DOUBLE-CLICK pick, in contrast, is
// always on and reports `{path, name}` — the full leaf id with no
// extra tool state — so double-click is this reader's selection
// gesture for the connectivity/mermaid propagation below.
import {
  Display,
  Viewer,
} from "/static/three-cad-viewer/three-cad-viewer.esm.min.js";
import { renderTopologyCloud } from "/static/topology-cloud.js";

const HIGHLIGHT_COLOUR = "#f59e0b";
const EXPLODE_DURATION = 1.5;

function walkShapes(node, visit) {
  visit(node);
  if (node.parts) {
    for (const p of node.parts) walkShapes(p, visit);
  }
}

function findPart(root, path) {
  let found = null;
  walkShapes(root, (n) => {
    if (n.id === path) found = n;
  });
  return found;
}

// Client-side `isolate`: a PRUNED copy of the shapes tree keeping only
// the subtree named `name`, plus the chain of ancestor groups above it.
//
// The server used to prune for us via `plan_visibility` BEFORE any
// geometry was built, which is what made isolating slow — it re-paid
// tessellation, the SDF witness queries, a stability solve and a
// validator pass. The whole scene is already in hand here, so this is a
// tree walk instead of a rebuild.
//
// The ancestor chain is kept for a hard reason, not for looks: an `id`
// MUST equal the "/"-join of the `name`s above it
// (precis_web/blocktree_3d.py's module docstring), and the ids here are
// absolute from the design root. Re-rooting the subtree — returning the
// `fork` node as the new root — would leave every retained id reading
// `/Structural envelopes/fork/...` while the vendored treeview rebuilt
// its own paths as `/fork/...`, which is precisely the two-disjoint-key-
// spaces defect that made every visibility toggle in this viewer inert.
// Keeping the ancestors keeps every retained path byte-identical, and
// they contribute no geometry (their own `(envelope)` leaves are
// siblings of the subtree, so they prune away with everything else).
//
// Returns null when no such block is in the CURRENT scene — a real case,
// not a bug: a name that only exists at a deeper `level` than the one
// fetched. The caller reports it rather than blanking the viewer.
function isolateSubtree(root, name) {
  function prune(node) {
    if (node.name === name) return node;
    if (!node.parts) return null;
    const kept = node.parts.map(prune).filter(Boolean);
    return kept.length ? { ...node, parts: kept } : null;
  }
  return prune(root);
}

// A mermaid node id is `B<block_uid>` (blocktree_3d.mermaid_topology) —
// the SAME stable uid every block node in `data.shapes` now carries as
// its own explicit `uid` FIELD (viewer-toggles fix,
// precis_web/blocktree_3d.py's module docstring: `id` is a NAME path —
// what the vendored treeview/nestedGroup both key off — so it's no
// longer where identity lives; `uid` is). Resolving a uid back to a 3D
// path is still just a reverse tree walk, no lookup table. Prefers a
// GROUP match (a block with rendered children) over a bare leaf, since
// that's the "primary path" connectivity/explode key on the server
// side — a container block's doubled self-leaf carries the SAME uid as
// its enclosing group, so this preference still resolves to the group.
function findPathByUid(root, blockUid) {
  let leafMatch = null;
  let groupMatch = null;
  walkShapes(root, (n) => {
    if (n.uid !== undefined && String(n.uid) === String(blockUid)) {
      if (n.parts) groupMatch = n.id;
      else if (!leafMatch) leafMatch = n.id;
    }
  });
  return groupMatch || leafMatch;
}

//: The doubled container self-leaf's own name suffix (blocktree_3d.py's
//: module docstring) — kept in sync with the server's literal
//: ``f"{name} (envelope)"``.
const _CONTAINER_LEAF_SUFFIX = " (envelope)";

// A block with both its own geometry and visible children doubles its
// last path segment as `.../<name>/<name (envelope)>` (blocktree_3d's
// own module docstring, post viewer-toggles-fix: the path is
// name-derived, so the doubled segment is no longer a literal repeat —
// it carries the " (envelope)" suffix) — normalize a raw pick back to
// the primary (group) path so it matches the connectivity metadata's
// own a_path/b_path keys.
function primaryPathOf(fullPath) {
  const segs = fullPath.split("/");
  if (segs.length < 2) return fullPath;
  const last = segs[segs.length - 1];
  const parent = segs[segs.length - 2];
  if (last === `${parent}${_CONTAINER_LEAF_SUFFIX}`) {
    return segs.slice(0, -1).join("/");
  }
  return fullPath;
}

function connectionsTouching(connections, path) {
  return connections.filter((c) => c.a_path === path || c.b_path === path);
}

// Server error text (a JSON ``error`` field, or a caught exception's own
// message) is untrusted content — a bad ``overrides``/``isolate`` value
// can echo the raw payload back in a 400 body. Render it as TEXT, never
// ``innerHTML`` (a reflected-DOM-XSS sink), by building the element and
// setting ``textContent``.
function showError(container, message) {
  const p = document.createElement("p");
  p.className = "text-sm text-red-600 p-3";
  p.textContent = message;
  container.replaceChildren(p);
}

//: Room reserved (px) for the vendored tree panel's OWN sibling "info"
// box below it (Data Format.md's per-leaf property readout) when we
// hand the Display an explicit treeHeight — matches that box's own CSS
// default height (three-cad-viewer.css .tcv_cad_info) so we're not
// fighting its natural size, just no longer letting the TREE panel
// default to a flat 250px regardless of how tall our own shell is
// (gr337747 — a design with enough blocks/connects to overflow that
// fixed 250px never gets to scroll into view within it).
const _INFO_PANEL_HEIGHT = 146;
//: A conservative estimate of the vendored toolbar row's own height —
// only used to size the FIRST render pass before we can measure the
// real thing; :func:`_fitViewerToShell` corrects any remaining miss
// against the actual DOM after construction (gr337753).
const _TOOLBAR_HEIGHT_GUESS = 40;
const _MIN_CAD_WIDTH = 200;
const _MIN_CAD_HEIGHT = 200;
//: The vendored ``.tcv_cad_viewer`` rule ships ``margin: 4px`` (all four
//: sides, three-cad-viewer.css) — a real CSS margin sits OUTSIDE the
//: element's own border-box, so it's invisible to a width/height
//: comparison between that box and the shell (gr340029: that's exactly
//: why the old :func:`_fitViewerToShell` below never caught this case —
//: both boxes measured the SAME width, yet the margin still pushed the
//: outer element's right/bottom edge past the shell's). Baked into the
//: FIRST-paint budget here so the initial render already accounts for
//: it, on top of the edge-based backstop below for anything else the
//: vendored chrome adds that isn't captured by a fixed constant.
const _OUTER_MARGIN = 4;
//: Small breathing room below the fitted shell (gr340029) — a shell
//: sized to EXACTLY ``window.innerHeight`` would still put its very
//: last pixel flush against the viewport edge, easy to misread as "still
//: clipped" on a device with e.g. a bottom scrollbar or browser chrome
//: sliver.
const _SHELL_VIEWPORT_MARGIN = 24;
const _SHELL_MAX_HEIGHT = 520;
//: Floor so a very short window still leaves the vendored toolbar row +
//: a usable sliver of tree/canvas — below this the widget's own chrome
//: (toolbar + tree + info box) wouldn't fit at all regardless of shell
//: sizing.
const _SHELL_MIN_HEIGHT = 320;

// gr337753/gr340029: the vendored Display ADDS its own toolbar row +
// tree panel AROUND the cadWidth/height canvas we hand it — its outer
// element's real footprint can end up bigger than the shell we gave it
// in either axis (the CSS margin above is one source; a future vendored-
// layout change could add another), and its z-index:100 chrome then
// paints OVER whatever sits next to it (the "Assembly"/"Topology"
// headings), or gets clipped mid-control by the shell's own
// ``overflow: hidden`` backstop (the tree panel's picking-filter
// dropdown, gr340029), instead of fitting inside it. Rather than
// hard-code the toolbar/tree chrome's own pixel sizes (version-fragile —
// they live in the vendored, never-edited, minified bundle), measure the
// REAL outer element's bounding box after construction and shrink
// cadWidth/height by the actual overflow via the viewer's own public
// resizeCadView() API until it fits the shell.
//
// gr340029: overflow is measured by comparing RIGHT/BOTTOM EDGES
// (``outerRect.right``/``bottom`` vs the shell's), not by diffing plain
// widths/heights — the old width-diff check silently missed any
// overflow caused by an offset between the two boxes' own top-left
// corners (exactly what the vendored margin above produces: same
// width, but the outer box's left edge already sits a few px inside the
// shell's, so its right edge pokes out an equal few px on the other
// side without the WIDTHS ever differing).
// Returns the height actually in effect afterwards, so the caller can
// re-stamp the tree-height cap against the POST-backstop budget — a cap
// stamped from the pre-shrink height would let the tree/info column poke
// past the widget's corrected bottom edge by exactly the backstop delta.
function _fitViewerToShell(viewer, shellEl, treeWidth, cadWidth, height) {
  const outer = shellEl.querySelector(".tcv_cad_viewer");
  if (!outer) return height;
  const shellRect = shellEl.getBoundingClientRect();
  const outerRect = outer.getBoundingClientRect();
  const overW = outerRect.right - shellRect.right;
  const overH = outerRect.bottom - shellRect.bottom;
  if (overW <= 0 && overH <= 0) return height;
  const nextCadWidth = Math.max(_MIN_CAD_WIDTH, cadWidth - Math.max(0, overW));
  const nextHeight = Math.max(_MIN_CAD_HEIGHT, height - Math.max(0, overH));
  try {
    viewer.resizeCadView(nextCadWidth, treeWidth, nextHeight);
  } catch (err) {
    // Best-effort — see recolour's own try/catch for the convention.
    console.error("blocktree-3d: resizeCadView failed", err);
    return height;
  }
  return nextHeight;
}

//: gr340029 — a SECOND, independent overflow source the vendored
//: ``resizeCadView``/``treeHeight`` display option can't reach: the
//: currently-bundled three-cad-viewer version's own tree/info column
//: (``.tcv_cad_tree``/``.tcv_cad_info_wrapper``) stamps itself a FIXED
//: inline height (the vendored ``DISPLAY_DEFAULTS.treeHeight`` of
//: ``400``px for the tree, its own natural content height for the info
//: box below it) regardless of the ``treeHeight`` we hand the Display at
//: construction — KNOWN SIMPLIFICATION, not chased further into the
//: vendored bundle (never edited) since a CSS backstop covers it
//: cleanly: an ``!important`` rule in an EXTERNAL stylesheet beats ANY
//: plain (non-``!important``) inline style on the cascade regardless of
//: which one was WRITTEN most recently, so a dynamically-updated
//: ``<style>`` tag reliably caps these two elements' real height and
//: gives them their own internal scrollbar, however the vendored inline
//: style keeps re-asserting itself. Without this, a shell shorter than
//: the vendored tree column's own forced ~400px+ natural height (routine
//: once the shell tracks a short browser viewport, gr340029) pushes the
//: tree/info column's content past the shell's bottom edge, past even
//: what :func:`_fitViewerToShell` above can reach (that one can only
//: shrink the CANVAS side of the widget, not this left-hand column).
function _applyTreeHeightCap(styleEl, treeMaxPx, infoMaxPx) {
  styleEl.textContent =
    `#bt3d-viewer .tcv_cad_tree { max-height: ${treeMaxPx}px !important; ` +
    "overflow-y: auto !important; }\n" +
    `#bt3d-viewer .tcv_cad_info_wrapper { max-height: ${infoMaxPx}px !important; ` +
    "overflow-y: auto !important; }";
}

//: gr340029 — the shell's own height, fit to whatever of the browser
//: viewport is left below it (a fixed 520px shell could put the
//: vendored "Ready" status box below the fold, with page scroll trapped
//: by the canvas's own wheel-zoom whenever the pointer sits over it).
//: ``shellEl.getBoundingClientRect().top`` is viewport-relative, so this
//: is only exactly right at the scroll position the page loads at
//: (top) — same convention as every other "fit to what's visible now"
//: computation in this file, re-run on resize below rather than tracked
//: continuously against scroll.
function _fitShellHeight(shellEl) {
  // Clamp to >= 0: on a RESIZE while the page is scrolled down, the
  // shell's viewport-relative top can be negative, which would size the
  // shell for a viewport it isn't actually constrained by.
  const top = Math.max(0, shellEl.getBoundingClientRect().top);
  const avail = window.innerHeight - top - _SHELL_VIEWPORT_MARGIN;
  return Math.max(_SHELL_MIN_HEIGHT, Math.min(_SHELL_MAX_HEIGHT, avail));
}

// ── scale bar overlay (gr340030) ────────────────────────────────────────
//
// precis_web.blocktree_3d.scene_scale multiplies every emitted coordinate
// by a power-of-ten display-only factor (a nanometre-scale design would
// otherwise sit outside three.js's comfortable camera range) — so an
// on-screen length carries no real-world meaning by itself once that's
// applied. This overlay reads the vendored camera's OWN live zoom state
// to turn a screen-pixel length back into world (display) units, then
// divides out ``scene.scale`` to recover real SI metres.
//
// There is no public "camera changed" notification on the vendored
// Viewer to hook (the ``notify`` callback threaded through its
// constructor only ever fires on a PICK — see this file's own module
// docstring on that). A throttled requestAnimationFrame poll is the
// documented fallback for exactly this case; :data:`_SCALE_BAR_MIN_MS`
// caps it at a few reads per second, cheap enough to run for the page's
// whole lifetime (this reader has no explicit teardown path either, same
// as the mermaid/explode/connections wiring above).
const _SCALE_BAR_MIN_MS = 150;
//: Aim for a bar this many CSS pixels wide before snapping its world
//: length to a round 1/2/5×10^n value — purely a "looks reasonable"
//: target, not a hard constraint (the actual bar can land anywhere in
//: roughly the 40-160px range once snapped).
const _SCALE_BAR_TARGET_PX = 90;
//: SI-prefix ladder for the real-metres label — picometres up through
//: kilometres covers every scale this reader plausibly shows (se
//: designs run from atomic-ish envelopes up to architectural ones).
const _SI_LADDER = [
  { exp: -12, suffix: "pm" },
  { exp: -9, suffix: "nm" },
  { exp: -6, suffix: "µm" },
  { exp: -3, suffix: "mm" },
  { exp: 0, suffix: "m" },
  { exp: 3, suffix: "km" },
];

function _formatRealLength(metres) {
  if (!(metres > 0) || !Number.isFinite(metres)) return null;
  let chosen = _SI_LADDER[0];
  for (const step of _SI_LADDER) {
    if (metres >= 10 ** step.exp) chosen = step;
  }
  const value = metres / 10 ** chosen.exp;
  // The value going in is already a snapped 1/2/5×10^n multiple of a
  // power of ten, so this is at most one decimal place (e.g. a snapped
  // "0.5 nm" bar) — never an ugly float tail.
  const rounded = Math.round(value * 10) / 10;
  return `${rounded} ${chosen.suffix}`;
}

//: The nearest 1/2/5×10^n multiple to ``raw`` (a "round real-world
//: length", per the round-2b spec) — compared in LOG space so e.g. a
//: ``raw`` of 3 picks 2 or 5 (whichever is proportionally closer), not
//: whichever happens to be numerically closer.
function _snapToNiceLength(raw) {
  if (!(raw > 0) || !Number.isFinite(raw)) return null;
  const exp = Math.floor(Math.log10(raw));
  const base = 10 ** exp;
  let best = base;
  let bestDist = Infinity;
  for (const mult of [1, 2, 5, 10]) {
    const candidate = mult * base;
    const dist = Math.abs(Math.log(candidate / raw));
    if (dist < bestDist) {
      bestDist = dist;
      best = candidate;
    }
  }
  return best;
}

//: World (display) units per screen pixel, read off the vendored
//: viewer's OWN live camera state. KNOWN SIMPLIFICATION: there is no
//: public accessor for the raw three.js camera (``getCameraPosition``/
//: ``getCameraTarget``/``getCameraType``/``getCameraZoom`` cover the
//: pick/selection use cases the vendored API was designed for, not this
//: one) — this reaches into the Viewer's own ``_rendered.camera``
//: instead, a private field, guarded end-to-end by the try/catch below
//: so a vendored-internals change degrades to "no scale bar this
//: render" rather than breaking the primary display. three-cad-viewer
//: defaults to an ORTHOGRAPHIC camera (this reader never overrides
//: that), so that's the primary, tested path: an orthographic camera's
//: visible world height is fixed at construction (``top``/``bottom``)
//: and only its ``zoom`` changes on scroll, so
//: ``(top - bottom) / zoom / canvasHeightPx`` is exact and needs no
//: distance term. The perspective branch (untested here — kept for
//: robustness against a future ``ortho: false`` override) follows the
//: round-2b spec's own formula instead:
//: ``2 · distance · tan(fov / 2) / canvasHeightPx``.
function _worldPerPixel(viewer, viewerEl) {
  try {
    const cc = viewer && viewer._rendered && viewer._rendered.camera;
    const canvas = viewerEl.querySelector("canvas");
    const canvasH = canvas && canvas.clientHeight;
    if (!cc || !canvasH) return null;
    if (cc.ortho) {
      const oc = cc.oCamera;
      if (!oc || !oc.zoom) return null;
      return (oc.top - oc.bottom) / oc.zoom / canvasH;
    }
    const pc = cc.pCamera;
    if (!pc || typeof pc.fov !== "number") return null;
    const pos = viewer.getCameraPosition();
    const tgt = viewer.getCameraTarget();
    const dx = pos[0] - tgt[0];
    const dy = pos[1] - tgt[1];
    const dz = pos[2] - tgt[2];
    const distance = Math.sqrt(dx * dx + dy * dy + dz * dz);
    const fovRad = (pc.fov * Math.PI) / 180;
    return (2 * distance * Math.tan(fovRad / 2)) / (pc.zoom || 1) / canvasH;
  } catch (err) {
    console.error("blocktree-3d: scale bar camera read failed", err);
    return null;
  }
}

//: Starts the scale-bar overlay's own rAF poll loop (module docstring
//: above) — a no-op, honest-absence overlay (hidden, never an error)
//: whenever ``sceneScale`` is missing/non-positive (an older cached
//: ``scene3d.json`` response, or a degenerate scene) or the camera read
//: fails, rather than ever showing a length that isn't real.
function _startScaleBar(viewer, viewerEl, sceneScale) {
  if (!(sceneScale > 0)) return;
  const bar = document.createElement("div");
  bar.className = "bt3d-scale-bar";
  bar.innerHTML =
    '<div class="bt3d-scale-bar-line"></div>' +
    '<div class="bt3d-scale-bar-label"></div>';
  viewerEl.appendChild(bar);
  const line = bar.querySelector(".bt3d-scale-bar-line");
  const label = bar.querySelector(".bt3d-scale-bar-label");

  let lastUpdate = 0;

  function update() {
    const wpp = _worldPerPixel(viewer, viewerEl);
    const niceWorld = wpp !== null ? _snapToNiceLength(wpp * _SCALE_BAR_TARGET_PX) : null;
    const text = niceWorld !== null ? _formatRealLength(niceWorld / sceneScale) : null;
    if (wpp === null || niceWorld === null || text === null) {
      bar.style.display = "none";
      return;
    }
    bar.style.display = "";
    line.style.width = `${niceWorld / wpp}px`;
    label.textContent = text;
  }

  // No explicit teardown — this reader has no lifecycle beyond the page
  // itself (same as the mermaid/explode/connections wiring above), so the
  // loop just runs for as long as the page does.
  function tick(now) {
    requestAnimationFrame(tick);
    if (now - lastUpdate < _SCALE_BAR_MIN_MS) return;
    lastUpdate = now;
    update();
  }
  requestAnimationFrame(tick);
}

// ── atomic ↔ smooth overlay (gr450675) ──────────────────────────────────
//
// scene3d.json's own ``Shapes`` tree only ever carries box/solid envelope
// geometry — an atomic block's real atoms/bonds/scaffold-deviation are a
// different shape (per-atom arrays, not a mesh tree) served separately by
// ``atomic3d.json`` (:func:`precis_web.routes.blocktree_view.
// _atomic3d_response`), so a plain (non-atomic) design fetches none of
// this. The slider LERPs every atom/bond/surface vertex between the raw
// atomic positions and the Taubin-smoothed ones the server already
// computed — no per-tick server round trip.
//
// KNOWN SIMPLIFICATION: the vendored three-cad-viewer bundle exports no
// ``THREE`` and no public scene accessor. It DOES construct its internal
// studio-manager with ``getScene:()=>this.rendered.scene`` (grep the
// bundle), which means ``viewer._rendered.scene`` is the live THREE.Scene
// — a private field, reached the SAME documented way this file's own
// scale-bar overlay already reaches ``viewer._rendered.camera`` above
// (guarded end-to-end, degrades to "no overlay" rather than breaking the
// primary viewer). The overlay's own three.js objects are built from a
// SEPARATE vendored copy (``/static/three/three.module.min.js`` — the
// ``cad`` viewer's own, a different revision than the one bundled inside
// three-cad-viewer): mixing two three.js module instances is not
// something either project promises to support, but a plain
// Mesh/LineSegments/BufferGeometry object only needs the long-stable,
// duck-typed ``isMesh``/``isObject3D`` contract ``WebGLRenderer``
// traverses by, not class identity — this rendered correctly end to end
// in manual verification. A future bump of either vendored copy that
// changes that contract would need this reach revisited.
const _ATOMIC_CPK = {
  H: "#ffffff", He: "#d9ffff", B: "#ffb5b5", C: "#909090",
  N: "#3050f8", O: "#ff0d0d", F: "#90e050", Si: "#f0c8a0",
  P: "#ff8000", S: "#ffff30", Cl: "#1ff01f", Br: "#a62929",
  I: "#940094", Ni: "#50d050", Cu: "#c88033", Pd: "#006985",
  Pt: "#d0d0e0", Au: "#ffd123",
}; // fmt: skip
const _ATOMIC_CPK_DEFAULT = "#ff2fa0";
//: Å → the atomic3d.json ``coords``/``smooth`` arrays' own units (scene
//: display units, already ``world metres × scene.scale`` — server side).
const _ATOMIC_A_TO_M = 1e-10;
const _ATOM_RADIUS_A = 0.3;
const _BOND_RADIUS_A = 0.12;
//: gr450675 Playwright investigation, Cause B — a LEGIBILITY floor, not a
//: physical van-der-Waals radius: the Å-true radii above render sub-pixel
//: once the camera auto-fits a multi-block assembly whose blocks differ
//: greatly in size. Floors the atom radius at this fraction of the
//: scene's own bounding-box diagonal (computed post-render off the same
//: private ``viewer._rendered.scene`` this overlay already reaches
//: below), never SHRINKING it below the true physical radius — a design
//: small enough that the physical radius already clears this floor is
//: left untouched. Bond radius keeps the Å-true 0.3:0.12 ratio to
//: whichever of the two (physical or floored) wins.
const _ATOM_LEGIBILITY_FRACTION = 0.01;
//: The deviation legend's sequential ramp — the SAME 3 stops the
//: template's legend swatch gradient uses (detail3d.html.j2), so the bar
//: and the surface colouring always agree.
const _DEVIATION_STOPS = [
  [0.0, [0xe0, 0xf2, 0xfe]],
  [0.5, [0x1d, 0x4e, 0xd8]],
  [1.0, [0x7f, 0x1d, 0x1d]],
];

function _deviationColor(t) {
  const clamped = Math.max(0, Math.min(1, t));
  for (let i = 0; i < _DEVIATION_STOPS.length - 1; i++) {
    const [t0, c0] = _DEVIATION_STOPS[i];
    const [t1, c1] = _DEVIATION_STOPS[i + 1];
    if (clamped >= t0 && clamped <= t1) {
      const f = t1 > t0 ? (clamped - t0) / (t1 - t0) : 0;
      return [0, 1, 2].map((k) => (c0[k] + (c1[k] - c0[k]) * f) / 255);
    }
  }
  return [1, 1, 1];
}

//: A hidden-at-load, honest-absence overlay (module docstring): no atomic
//: blocks, a fetch failure, or an unreachable private scene all degrade to
//: "the slider/legend never appear" (the template already omits them
//: server-side whenever ``has_atomic`` is false; this covers the rarer
//: rev-mismatch/fetch-failure cases too), never a broken primary viewer.
//:
//: Returns ``{applyT, setVisible}`` — the smooth-slider sink and the
//: atoms on/off switch — or ``null`` when the overlay degraded to
//: absence. The control LISTENERS live with the caller, not here: the
//: overlay's meshes are injected into ``viewer._rendered.scene``, which a
//: scene reload (``viewer.clear()``) drops, so the overlay has to be set
//: up again per scene — and a listener attached per setup would stack up
//: one duplicate handler per reload, each driving a dead ``applyT`` over
//: a scene graph that no longer holds its meshes.
async function _setupAtomicOverlay(viewer, atomicUrl, smoothEls, sceneShapes) {
  const [THREE, data] = await Promise.all([
    import("/static/three/three.module.min.js"),
    fetch(atomicUrl).then((r) => {
      if (!r.ok) throw new Error(`atomic3d fetch failed (${r.status})`);
      return r.json();
    }),
  ]);
  if (!data.blocks || !data.blocks.length) return null;
  const scene = viewer && viewer._rendered && viewer._rendered.scene;
  if (!scene) return null;

  const devMax = data.deviation_max || 0;
  const atomRPhysical = _ATOM_RADIUS_A * _ATOMIC_A_TO_M * (data.scale || 1);
  //: The scene-wide legibility floor (Cause B) — derived from the WHOLE
  //: multi-block assembly's own bounding diagonal, since that (not any
  //: one block's own size) is what the shared auto-fit camera actually
  //: frames.
  let legibilityFloorR = 0;
  const bbox = new THREE.Box3().setFromObject(scene);
  if (!bbox.isEmpty()) {
    legibilityFloorR = bbox.getSize(new THREE.Vector3()).length() * _ATOM_LEGIBILITY_FRACTION;
  }
  const yAxis = new THREE.Vector3(0, 1, 0);

  //: A block whose own atoms sit much closer together than the scene-wide
  //: legibility floor (gr450675 live verification: 60-atom C60 "cage" in
  //: the same assembly as a 920-atom "scaffold" — the floor sized for the
  //: bigger block inflated the cage's atoms past half its own ~1.4 Å bond
  //: length, fusing every atom into one solid blob, worse than the
  //: sub-pixel bug this floor exists to fix) needs its OWN per-block cap:
  //: never let atom radius exceed this fraction of the block's shortest
  //: bond, so neighbouring balls keep a visible stick between them —
  //: same convention real ball-and-stick renderers use (atoms smaller
  //: than bonds), just anchored to whichever radius wins above. Physical
  //: radius still always wins if even IT exceeds the cap (an
  //: honest render of a genuinely tight-bonded structure, not a bug).
  const _ATOM_MAX_BOND_FRACTION = 0.45;

  function atomBondRadiiFor(b) {
    let minBond = Infinity;
    for (const [i, j] of b.bonds || []) {
      const a = b.coords[i], c = b.coords[j];
      const d = Math.hypot(a[0] - c[0], a[1] - c[1], a[2] - c[2]);
      if (d > 0 && d < minBond) minBond = d;
    }
    let atomR = Math.max(atomRPhysical, legibilityFloorR);
    if (Number.isFinite(minBond)) {
      atomR = Math.min(atomR, Math.max(atomRPhysical, minBond * _ATOM_MAX_BOND_FRACTION));
    }
    const bondR = atomR * (_BOND_RADIUS_A / _ATOM_RADIUS_A);
    return { atomR, bondR };
  }

  function orientBond(mesh, a, b, bondR) {
    const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
    const len = Math.hypot(dx, dy, dz) || 1e-12;
    mesh.position.set((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2);
    mesh.scale.set(bondR, len, bondR);
    const dir = new THREE.Vector3(dx, dy, dz).normalize();
    mesh.quaternion.setFromUnitVectors(yAxis, dir);
  }

  const group = new THREE.Group();
  group.name = "bt3d-atomic-overlay";
  scene.add(group);

  const sphereGeo = new THREE.SphereGeometry(1, 12, 8);
  const cylGeo = new THREE.CylinderGeometry(1, 1, 1, 8, 1);
  const blocks = [];

  // gr450675 Playwright investigation, Cause A — a block's pre-existing
  // ENVELOPE solid (the vendored viewer's default "refined" abstraction
  // level) is opaque and sits AROUND the atoms/smoothed surface this
  // overlay draws, hiding them completely. Cage it instead: drop only
  // its SHAPE (fill) mesh, leaving its EDGE mesh, so it reads as a
  // containing wireframe rather than a solid — ONLY for blocks that
  // actually have an overlay (matched by the block's own stable `uid`
  // field via findPathByUid, not any part of the path — viewer-toggles
  // fix, precis_web/blocktree_3d.py's module docstring).
  //
  // NOT done via the public `viewer.setState()`/`getStates()` pair: since
  // the viewer-toggles fix, that API's own name-joined path (e.g.
  // ``"/Structural envelopes/cage"``) IS this file's `id`/`path` scheme
  // (they were deliberately unified — that's the fix), but `setState`
  // still isn't the right tool here: it round-trips through the tree
  // MODEL, which only ever restores a node to ITS OWN stored
  // shape/edge visibility, with no way to ask for "shape hidden, edge
  // shown" (this overlay's actual target). `updatePart` (the public API
  // `recolour` below already reaches through) also doesn't fit: its
  // unchanged-geometry fast path writes color/alpha onto the JSON model
  // only, never the live mesh material (confirmed live: no visual
  // change). The vendored per-leaf `ObjectGroup` itself — reached the
  // same `viewer._rendered.nestedGroup.groups[path]` map `updatePart`
  // uses internally, keyed by our own `id` (now the name-chain path) —
  // exposes the one method that actually does what we need:
  // `setShapeVisible(false)` flips just the fill mesh's
  // `material.visible`, leaving the edge mesh alone (confirmed live,
  // unlike a low-opacity material: a finely-tessellated envelope's OWN
  // many overlapping semi-transparent triangles would otherwise still
  // read as solid). `setShapeVisible(true)` is the exact restore target
  // on failure below, so a build error degrades to today's opaque
  // envelope rather than a permanently ghosted block.
  const cagedEnvelopePaths = [];
  function _envelopeGroup(path) {
    return viewer._rendered && viewer._rendered.nestedGroup
      ? viewer._rendered.nestedGroup.groups[path]
      : null;
  }
  function cageEnvelope(path) {
    if (!path) return;
    try {
      const grp = _envelopeGroup(path);
      if (!grp) return;
      grp.setShapeVisible(false);
      cagedEnvelopePaths.push(path);
    } catch (err) {
      console.error("blocktree-3d: envelope cage failed for", path, err);
    }
  }
  //: Caging is reversible, because the atoms can be switched off: with
  //: the overlay hidden, a block whose own envelope is still caged would
  //: render as nothing at all. So "atoms off" un-cages and "atoms on"
  //: re-cages, over the SAME recorded path list.
  function setEnvelopesCaged(caged) {
    for (const path of cagedEnvelopePaths) {
      try {
        const grp = _envelopeGroup(path);
        if (grp) grp.setShapeVisible(!caged);
      } catch (err) {
        console.error("blocktree-3d: envelope cage toggle failed for", path, err);
      }
    }
  }
  //: The build-failure path: un-cage AND forget, so a half-built overlay
  //: leaves no record behind to re-cage against.
  function restoreEnvelopes() {
    setEnvelopesCaged(false);
    cagedEnvelopePaths.length = 0;
  }

  try {
    for (const b of data.blocks) {
      cageEnvelope(sceneShapes ? findPathByUid(sceneShapes, b.uid) : null);
      const { atomR, bondR } = atomBondRadiiFor(b);
      const n = b.elements.length;
      const atomMeshes = [];
      for (let i = 0; i < n; i++) {
        const colour = _ATOMIC_CPK[b.elements[i]] || _ATOMIC_CPK_DEFAULT;
        const mat = new THREE.MeshStandardMaterial({
          color: colour,
          transparent: true,
        });
        const mesh = new THREE.Mesh(sphereGeo, mat);
        mesh.scale.setScalar(atomR);
        group.add(mesh);
        atomMeshes.push(mesh);
      }
      const bondMeshes = [];
      for (const [i, j] of b.bonds || []) {
        const mat = new THREE.MeshStandardMaterial({
          color: 0x808080,
          transparent: true,
        });
        const mesh = new THREE.Mesh(cylGeo, mat);
        group.add(mesh);
        bondMeshes.push({ mesh, i, j });
      }

      // The smoothed surface — fan-triangulated rings, coloured per vertex
      // by aberration (gr450675's own "colour by deviation" ask).
      const positions = new Float32Array(n * 3);
      const colors = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        const t = devMax > 0 ? (b.deviation[i] || 0) / devMax : 0;
        const [r, g, bl] = _deviationColor(t);
        colors[i * 3] = r;
        colors[i * 3 + 1] = g;
        colors[i * 3 + 2] = bl;
      }
      const indices = [];
      for (const face of b.faces || []) {
        for (let k = 1; k < face.length - 1; k++) {
          indices.push(face[0], face[k], face[k + 1]);
        }
      }
      const surfGeo = new THREE.BufferGeometry();
      surfGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      surfGeo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      surfGeo.setIndex(indices);
      const surfMesh = new THREE.Mesh(
        surfGeo,
        new THREE.MeshBasicMaterial({
          vertexColors: true,
          side: THREE.DoubleSide,
          transparent: true,
        })
      );
      surfMesh.visible = false;
      group.add(surfMesh);

      blocks.push({
        coords: b.coords,
        smooth: b.smooth,
        atomMeshes,
        bondMeshes,
        bondR,
        surfMesh,
        surfPositions: positions,
      });
    }
  } catch (err) {
    restoreEnvelopes();
    scene.remove(group);
    throw err;
  }

  function applyT(t) {
    for (const blk of blocks) {
      const { coords, smooth, atomMeshes, bondMeshes, bondR, surfMesh, surfPositions } = blk;
      const n = atomMeshes.length;
      const lerped = new Array(n);
      for (let i = 0; i < n; i++) {
        const c = coords[i], s = smooth[i];
        const x = c[0] + (s[0] - c[0]) * t;
        const y = c[1] + (s[1] - c[1]) * t;
        const z = c[2] + (s[2] - c[2]) * t;
        lerped[i] = [x, y, z];
        const mesh = atomMeshes[i];
        mesh.position.set(x, y, z);
        mesh.material.opacity = 1 - t;
        mesh.visible = t < 0.999;
        surfPositions[i * 3] = x;
        surfPositions[i * 3 + 1] = y;
        surfPositions[i * 3 + 2] = z;
      }
      for (const { mesh, i, j } of bondMeshes) {
        orientBond(mesh, lerped[i], lerped[j], bondR);
        mesh.material.opacity = 1 - t;
        mesh.visible = t < 0.999;
      }
      surfMesh.geometry.attributes.position.needsUpdate = true;
      surfMesh.geometry.computeVertexNormals();
      surfMesh.material.opacity = t;
      surfMesh.visible = t > 0.001;
    }
    try {
      viewer.update(true);
    } catch (err) {
      // Best-effort — see recolour's own try/catch for the convention.
      console.error("blocktree-3d: atomic overlay redraw failed", err);
    }
  }

  applyT(0);
  if (smoothEls.legend) {
    smoothEls.legend.classList.remove("hidden");
    // Tailwind's `hidden` (display:none) and a plain `flex` utility carry
    // equal specificity — an inline style always wins over either, so
    // this is the one reliable way to un-hide a flex row from JS without
    // depending on utility declaration order in the generated stylesheet.
    smoothEls.legend.style.display = "flex";
  }
  if (smoothEls.legendMin) smoothEls.legendMin.textContent = "0.00";
  if (smoothEls.legendMax) smoothEls.legendMax.textContent = devMax.toFixed(2);

  //: Atoms on/off (Reto, 2026-09-29, against /se/hexfold-join-dogfood):
  //: a structure-bound design renders as atoms with its own envelope
  //: caged away, and there was no way back to the plain block view
  //: without leaving the page. The slider does NOT cover this — its far
  //: end swaps atoms for the SMOOTHED SURFACE, which is still the
  //: structure, not the envelope.
  //:
  //: One `THREE.Group` holds every atom, bond and surface mesh, so
  //: hiding is one flag; the caged envelopes are the other half, and
  //: have to come back or the block renders as empty space.
  function setVisible(on) {
    group.visible = on;
    setEnvelopesCaged(on);
    try {
      // Same reason applyT ends with one: these mutate the scene graph
      // under the vendored viewer's redraw hook, which never observes
      // them — without this the canvas keeps the previous frame until
      // some unrelated interaction forces a repaint.
      viewer.update(true);
    } catch (err) {
      console.error("blocktree-3d: atom visibility redraw failed", err);
    }
  }

  return { applyT, setVisible };
}

// ── load-time id/name path invariant self-check ─────────────────────────
//
// precis_web/blocktree_3d.py's module docstring: the id/name path
// divergence this whole module now guards against (module docstring —
// every emitted `id` must equal the "/"-join of `name`s from the root)
// sat behind a FULLY GREEN test suite and produced ZERO console output
// while every tree toggle in the viewer was silently dead. That's the
// actual failure mode worth designing against here: not "will the server
// ever regress this" (tests cover that directly) but "if it ever does,
// or a future change reintroduces a second addressing scheme for some
// new node kind, will anyone notice before a user reports a dead
// eyeball again". A loud runtime check turns that class of bug from
// invisible into obvious the moment it recurs.
//
// Two independent checks, both against the vendored viewer's OWN live
// state (never re-deriving what "should" be true, only comparing what we
// emitted against what the vendor actually built from it):
//   1. every `id` we emitted resolves in `viewer._rendered.nestedGroup.
//      groups` (the registry `Viewer.setObject`/every eyeball click
//      reaches, keyed verbatim by our own `id` — renderLoop's
//      `this.groups[t.id] = ...`);
//   2. the treeview's own key set (`viewer.getStates()`, built purely
//      from `name`s) has no key outside the `id` set we emitted — the
//      other direction of the same invariant, catching a node the
//      treeview built that we never even accounted for.
// Logs ONCE, with a few example divergent paths and both counts — never
// throws, since a mismatch must degrade the viewer VISIBLY (silently
// inert toggles are exactly what got this filed), not take down page
// load on top of that.
function _checkPathInvariant(viewer, shapes) {
  try {
    const rendered = viewer && viewer._rendered;
    const groups = rendered && rendered.nestedGroup && rendered.nestedGroup.groups;
    if (!groups || typeof viewer.getStates !== "function") return;
    const emittedIds = [];
    walkShapes(shapes, (n) => emittedIds.push(n.id));
    const emittedSet = new Set(emittedIds);
    const missingFromGroups = emittedIds.filter((id) => !(id in groups));
    const stateOnlyKeys = Object.keys(viewer.getStates()).filter(
      (k) => !emittedSet.has(k)
    );
    if (missingFromGroups.length === 0 && stateOnlyKeys.length === 0) return;
    const sample = (arr) => arr.slice(0, 5).join(", ");
    console.error(
      "blocktree-3d: id/name path invariant violated (precis_web/blocktree_3d.py's module docstring)" +
        " — tree visibility toggles will be silently inert.",
      `${missingFromGroups.length} emitted id(s) missing from viewer._rendered.nestedGroup.groups` +
        (missingFromGroups.length ? ` (e.g. ${sample(missingFromGroups)})` : "") +
        ";",
      `${stateOnlyKeys.length} treeview state key(s) with no matching emitted id` +
        (stateOnlyKeys.length ? ` (e.g. ${sample(stateOnlyKeys)})` : "") +
        "."
    );
  } catch (err) {
    console.error("blocktree-3d: path invariant self-check itself failed", err);
  }
}

export async function blocktreeViewer3D({
  viewerEl,
  mermaidEl,
  topologyEl,
  explodeButton,
  connectionsToggle,
  containerModeSelect,
  // The three scene-shaping controls. They were a plain GET form until
  // the live-scene slice; now the page drives them without a reload —
  // `level`/`overrides` refetch the scene, `isolate` filters the one
  // already in hand. All optional: absent = that control is not on the
  // page and the scene is whatever `sceneUrl` returns.
  levelSelect,
  isolateSelect,
  overridesInput,
  // Shown while `level`/`overrides` refetch. Optional, but without it a
  // multi-second rebuild is indistinguishable from a control that did
  // nothing — which is the reading gr458329 was filed under.
  busyEl,
  // Atoms on/off for a structure-bound design. Optional — the template
  // only renders it alongside the atomic↔smooth slider.
  atomsToggle,
  sceneUrl,
  atomicUrl,
  smoothEls,
  noteUrls,
  noteEls,
  // Design chat (design-workbench build, slice 3): called with the block
  // NAME on every selection (viewer pick or topology click) so the page
  // can drop it into the chat box as a handle. Optional.
  onSelectBlock,
}) {
  // gr338976 — disable mermaid's startOnLoad auto-run BEFORE the first
  // await: the vendored bundle defaults startOnLoad:true and runs on the
  // window 'load' event, which fires while the (slow) scene fetch is in
  // flight, renders the placeholder, and stamps data-processed — turning
  // the post-fetch run() below into a silent skip.
  if (window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "default",
    });
  }
  let data;
  try {
    const resp = await fetch(sceneUrl);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      showError(viewerEl, body.error || `failed to load scene (${resp.status})`);
      return;
    }
    data = await resp.json();
  } catch (err) {
    showError(viewerEl, "failed to load scene: " + String(err));
    return;
  }

  // ── topology panel ──────────────────────────────────────────────────
  // The force-directed cloud is the panel (spec slice 1 —
  // docs/backlog/se-topology-cloud-and-surface-notes.md); the mermaid
  // `graph LR` remains ONLY as the fallback when the cloud can't run or
  // the server sent no node list (an older/partial scene payload), and
  // dies a release after the cloud proves out.
  let cloud = null;
  if (topologyEl && Array.isArray(data.nodes) && data.nodes.length) {
    try {
      cloud = renderTopologyCloud({
        container: topologyEl,
        nodes: data.nodes,
        connections: data.connections || [],
        forces: data.forces || {},
        // Per-block validator findings (defect 3), keyed by block name —
        // an older/partial scene payload without it degrades to no
        // badges, never a broken panel.
        findings: data.findings || {},
        onSelect: (nodeId) => {
          const path = findPathByUid(data.shapes, nodeId.replace(/^B/, ""));
          if (path) selectPath(path);
        },
      });
      if (mermaidEl && mermaidEl.parentElement) {
        mermaidEl.parentElement.classList.add("hidden");
      }
    } catch (err) {
      console.error("blocktree-3d: topology cloud failed", err);
      cloud = null;
    }
  }
  if (!cloud && topologyEl) topologyEl.classList.add("hidden");
  if (!cloud && mermaidEl) {
    if (mermaidEl.parentElement) mermaidEl.parentElement.classList.remove("hidden");
    mermaidEl.textContent = data.mermaid || "graph LR";
    if (window.mermaid) {
      try {
        // gr338976 — if the auto-run still won a race (initialize above
        // came too late for this load), run() would skip a stamped
        // element; clearing the stamp makes this render unconditional.
        mermaidEl.removeAttribute("data-processed");
        await window.mermaid.run({ nodes: [mermaidEl] });
      } catch (err) {
        // A bad mermaid render must never take down the 3D panel next
        // to it — degrade to the raw graph source, honestly.
        console.error("blocktree-3d: mermaid render failed", err);
      }
    }
  }

  let lastMermaidNode = null;
  function highlightTopologyNode(blockId) {
    if (cloud) {
      cloud.highlight(`B${blockId}`);
      return;
    }
    if (!mermaidEl) return;
    if (lastMermaidNode) {
      lastMermaidNode
        .querySelectorAll("rect,polygon,circle,ellipse")
        .forEach((el) => {
          el.style.stroke = "";
          el.style.strokeWidth = "";
        });
      lastMermaidNode = null;
    }
    const g = mermaidEl.querySelector(`[id^="flowchart-B${blockId}-"]`);
    if (!g) return;
    g.querySelectorAll("rect,polygon,circle,ellipse").forEach((el) => {
      el.style.stroke = HIGHLIGHT_COLOUR;
      el.style.strokeWidth = "3px";
    });
    lastMermaidNode = g;
  }

  // ── 3D viewer ────────────────────────────────────────────────────────
  // gr340029: fit the SHELL itself to whatever of the viewport is left
  // below it BEFORE budgeting the vendored Display's own cadWidth/height
  // off its clientWidth/clientHeight — done here, synchronously, before
  // the Display ever paints, so there's no visible jump.
  viewerEl.style.height = `${_fitShellHeight(viewerEl)}px`;
  // gr337753: budget cadWidth/height so the toolbar row + tree panel the
  // vendored Display ADDS around them still fit inside our own shell,
  // rather than handing it the shell's own full clientWidth/clientHeight
  // (which is what used to overflow the shell on every axis). gr340029:
  // also reserve the vendored outer element's own CSS margin (see
  // _OUTER_MARGIN above) so the FIRST paint already accounts for it,
  // rather than relying solely on the post-construction backstop below.
  const treeWidth = 220;
  const shellWidth = viewerEl.clientWidth || 600;
  const shellHeight = viewerEl.clientHeight || 500;
  const initialCadWidth = Math.max(
    _MIN_CAD_WIDTH,
    shellWidth - treeWidth - 2 * _OUTER_MARGIN
  );
  const initialHeight = Math.max(
    _MIN_CAD_HEIGHT,
    shellHeight - _TOOLBAR_HEIGHT_GUESS - 2 * _OUTER_MARGIN
  );
  // gr337747: give the tree panel real vertical room proportional to OUR
  // shell instead of the vendored default's flat 250px — the info panel
  // below it keeps its own natural size. gr340029: the vendored Display
  // doesn't actually honour this option in the currently-bundled version
  // (see _applyTreeHeightCap's own docstring) — kept anyway (harmless,
  // and correct if a future vendor bump starts respecting it again), with
  // the CSS cap below as the backstop that actually works today.
  const initialTreeHeight = Math.max(80, initialHeight - _INFO_PANEL_HEIGHT);
  const displayOptions = {
    cadWidth: initialCadWidth,
    height: initialHeight,
    treeWidth,
    treeHeight: initialTreeHeight,
    theme: "browser",
    pinning: false,
  };
  const renderOptions = {
    ambientIntensity: 1.0,
    directIntensity: 1.1,
    metalness: 0.3,
    roughness: 0.65,
    edgeColor: 0x707070,
    defaultOpacity: 0.85,
    normalLen: 0,
  };
  const viewerOptions = { up: "Z" };

  let viewer;
  const highlighted = new Map(); // path -> original colour, for revert

  // ── container envelope mode (viewer fix: a design's ROOT can carry
  // both its own envelope AND every visible child — module docstring's
  // doubled-leaf "container" case, `data.container_paths`). An opaque
  // one then encloses the whole rendered subtree with no abstraction
  // level that avoids it, hiding everything inside. Default: translucent.
  //
  // `setTransparent`/`setOpacity` (NOT `setShapeVisible`) for the
  // translucent/solid modes — traced against the vendored bundle: the
  // per-leaf object reached via `viewer._rendered.nestedGroup.groups[path]`
  // (the same map `_envelopeGroup` in the atomic overlay above already
  // uses) exposes all three, but `Viewer.setObject` — the sink every
  // eyeball click / `setState` / `hideAll`/`showAll` / tree rebuild
  // reaches — re-applies `setShapeVisible` from the tree model's own
  // stored state with no awareness that anything was set manually, so a
  // manually-set `setShapeVisible` silently reverts on the next toggle.
  // `setTransparent`/`setOpacity` are never touched by that machinery,
  // so they survive later tree toggles. "hidden" mode is the one
  // exception: it has no transparency equivalent, so it uses
  // `setShapeVisible(false)` anyway — meaning it is NOT durable the same
  // way (a later eyeball/setState/tree-rebuild can silently bring the
  // container back), a real gap, accepted for now since it is an
  // explicit opt-in, not the default.
  const _CONTAINER_OPACITY = 0.25;

  //: The vendored ObjectGroup at a scene path, or null. Private reach
  //: (`_rendered.nestedGroup`), guarded by every caller; the vendored API
  //: has no public accessor for a group's materials.
  function _groupAt(path) {
    return viewer && viewer._rendered && viewer._rendered.nestedGroup
      ? viewer._rendered.nestedGroup.groups[path]
      : null;
  }

  function _containerGroup(path) {
    return _groupAt(path);
  }

  function applyContainerMode(mode) {
    for (const path of data.container_paths || []) {
      // Under a client-side isolate the rendered tree is a PRUNE of the
      // fetched one, so a container outside the isolated subtree is
      // legitimately absent. Absent from the rendered shapes = skip
      // quietly; present in the shapes but missing a group = the real
      // id/name mismatch worth shouting about.
      if (!findPart(shownShapes, path)) continue;
      try {
        const grp = _containerGroup(path);
        if (!grp) {
          console.error("blocktree-3d: container envelope group not found for", path);
          continue;
        }
        if (mode === "hidden") {
          grp.setShapeVisible(false);
        } else {
          grp.setShapeVisible(true);
          grp.setTransparent(mode === "translucent");
          grp.setOpacity(mode === "translucent" ? _CONTAINER_OPACITY : 1.0);
        }
      } catch (err) {
        // Best-effort — see recolour's own try/catch for the convention.
        console.error("blocktree-3d: container envelope mode failed for", path, err);
      }
    }
    // The setters above mutate the per-leaf ObjectGroup directly — same
    // low-level path the atomic overlay's per-frame lerp uses (`applyT`
    // above) — which the vendored `Viewer.setObject`/`setState` redraw
    // hook never observes. Without an explicit repaint here the change
    // is applied to the scene graph but the canvas keeps showing the
    // previous frame until the next unrelated interaction (orbit drag,
    // eyeball click, …) forces one. Match the atomic overlay's own
    // `viewer.update(true)` call for the same reason.
    if (viewer) {
      try {
        viewer.update(true);
      } catch (err) {
        console.error("blocktree-3d: container envelope redraw failed", err);
      }
    }
  }

  //: Can `updatePart` recolour this path at all? Two kinds it rejects by
  //: construction, and asking anyway spends a console error per click:
  //: a drawn connection is an edges-only leaf (no shape geometry to tint),
  //: and a block with children is a group, not a leaf ObjectGroup. Neither
  //: is an error worth reporting — they are skipped, not failed. (Not
  //: `data.container_paths`: that lists each container's `(envelope)`
  //: LEAF, which is recolourable — the group above it is not.)
  function recolourable(path) {
    const part = findPart(data.shapes, path);
    if (!part || (Array.isArray(part.parts) && part.parts.length)) return false;
    return !(data.connections || []).some((c) => c.path === path);
  }

  //: `updatePart` mutates the scene graph and does not ask for a frame, so
  //: without this the colour lands and the canvas keeps showing the old
  //: one until some unrelated interaction repaints it — measured n=0 on a
  //: real design. Same reason, and same call, as the container-mode setter.
  function repaint() {
    if (!viewer) return;
    try {
      viewer.update(true);
    } catch (err) {
      console.error("blocktree-3d: highlight redraw failed", err);
    }
  }

  //: Tint a leaf's own materials. NOT `viewer.updatePart`: that writes
  //: `color` into the vendored parts bookkeeping and rebuilds geometry, and
  //: never touches a material — every highlight routed through it changed
  //: nothing on screen (0 pixels, no error). The vendored `highlight()`
  //: recolours here, the same way.
  function recolour(path, colour) {
    if (!viewer || !recolourable(path)) return;
    try {
      const grp = _groupAt(path);
      if (!grp) return;
      for (const mesh of [grp.front, grp.back]) {
        if (!mesh || !mesh.material || !mesh.material.color) continue;
        mesh.material.color.set(colour);
        mesh.material.needsUpdate = true;
      }
    } catch (err) {
      // Best-effort: a recolour glitch must never break picking/explode.
      console.error("blocktree-3d: recolour failed for", path, err);
    }
  }

  function clearHighlight() {
    for (const [path, colour] of highlighted) recolour(path, colour);
    highlighted.clear();
    repaint();
  }

  function selectPath(primaryPath) {
    clearHighlight();
    for (const c of connectionsTouching(data.connections, primaryPath)) {
      const other = c.a_path === primaryPath ? c.b_path : c.a_path;
      for (const p of [c.path, other]) {
        const part = findPart(data.shapes, p);
        if (!part || highlighted.has(p) || !recolourable(p)) continue;
        highlighted.set(p, part.color);
        recolour(p, HIGHLIGHT_COLOUR);
      }
    }
    repaint();
    // The path's own last segment is now the block's NAME (viewer-toggles
    // fix, precis_web/blocktree_3d.py's module docstring — id is a "/"-
    // joined name chain), not its uid, so the mermaid/topology-cloud node
    // id (`B<uid>`) has to come off the part's own `uid` field instead of
    // being parsed back out of the path.
    const part = findPart(data.shapes, primaryPath);
    if (part && part.uid !== undefined) highlightTopologyNode(part.uid);
    showNotePanel(primaryPath);
    if (typeof onSelectBlock === "function" && part && part.name) {
      onSelectBlock(part.name);
    }
  }

  // ── comment on the selection → interview note (slice 2 of
  // docs/backlog/se-topology-cloud-and-surface-notes.md). The flow keeps
  // the user's words sovereign: raw comment → server-side AI rewrite →
  // shown EDITABLE for accept/edit → save posts the (possibly edited)
  // text plus the verbatim original. Every status/error string lands via
  // textContent (see showError — server text is untrusted).
  let selectedBlock = null;

  function noteStatus(msg) {
    if (noteEls && noteEls.status) noteEls.status.textContent = msg;
  }

  function showNotePanel(primaryPath) {
    if (!noteEls || !noteUrls) return;
    const part = findPart(data.shapes, primaryPath);
    const name = part ? part.name : null;
    if (!name) return;
    if (name !== selectedBlock) {
      // A fresh selection restarts the capture — a half-typed comment
      // about another block must not silently attach here.
      noteEls.raw.value = "";
      noteEls.proposal.classList.add("hidden");
      noteStatus("");
    }
    selectedBlock = name;
    noteEls.blockLabel.textContent = name;
    noteEls.panel.classList.remove("hidden");
  }

  async function postJson(url, payload) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || `request failed (${resp.status})`);
    return body;
  }

  if (noteEls && noteUrls) {
    noteEls.suggestButton.addEventListener("click", async () => {
      const comment = noteEls.raw.value.trim();
      if (!comment || !selectedBlock) {
        noteStatus(comment ? "select a block first" : "type a comment first");
        return;
      }
      noteEls.suggestButton.disabled = true;
      noteStatus("rewriting…");
      try {
        const j = await postJson(noteUrls.rewrite, {
          comment,
          block: selectedBlock,
        });
        noteEls.text.value = j.text || comment;
        noteEls.kind.value = j.kind === "decision" ? "decision" : "question";
        noteEls.proposal.classList.remove("hidden");
        noteStatus(
          j.degraded
            ? "AI rewrite unavailable — raw text kept; edit and save."
            : "AI-rewritten — edit if needed, then save."
        );
      } catch (err) {
        noteStatus("rewrite failed: " + String(err.message || err));
      } finally {
        noteEls.suggestButton.disabled = false;
      }
    });

    noteEls.saveButton.addEventListener("click", async () => {
      const text = noteEls.text.value.trim();
      if (!text || !selectedBlock) {
        noteStatus("nothing to save");
        return;
      }
      noteEls.saveButton.disabled = true;
      noteStatus("saving…");
      try {
        const j = await postJson(noteUrls.save, {
          text,
          kind: noteEls.kind.value,
          block: selectedBlock,
          verbatim: noteEls.raw.value.trim(),
        });
        noteStatus(`saved as ${j.name}`);
        noteEls.raw.value = "";
        noteEls.proposal.classList.add("hidden");
      } catch (err) {
        noteStatus("save failed: " + String(err.message || err));
      } finally {
        noteEls.saveButton.disabled = false;
      }
    });
  }

  function notify(change) {
    const pick = change && change.lastPick && change.lastPick.new;
    if (!pick) return;
    selectPath(primaryPathOf(`${pick.path}/${pick.name}`));
  }

  const treeHeightCapEl = document.createElement("style");
  document.head.appendChild(treeHeightCapEl);
  _applyTreeHeightCap(treeHeightCapEl, initialTreeHeight, _INFO_PANEL_HEIGHT);

  // ── the re-render seam ───────────────────────────────────────────────
  //
  // `level`/`overrides` used to be a plain GET form, so changing either
  // reloaded the whole page — camera reset, topology panel re-laid-out,
  // and nothing on screen until a full rebuild (tessellation + SDF
  // witness points + stability solve + validator) finished. Everything
  // below the first `new Viewer` is now re-runnable against a freshly
  // fetched `data`, so a level change is a scene swap instead.
  //
  // The Viewer INSTANCE is reused across renders (`clear()` + `render()`,
  // the vendored pair) rather than reconstructed. That is what lets the
  // control listeners, the scale-bar tick loop and the resize handler
  // keep their `viewer` reference across a reload — they read the live
  // object each time, so they need no re-wiring. Things that live in
  // `viewer._rendered` do NOT survive, because `clear()` drops it: the
  // atomic overlay's injected meshes are the one case, re-established by
  // `renderScene` below.
  let shownShapes = data.shapes;
  //: `{applyT, setVisible}` from the current scene's atomic overlay, or
  //: null while one is being built / when the design has no atoms.
  let atomicOverlay = null;

  //: Drives the overlay from whatever the two atomic controls currently
  //: say. Called after every (re)build and on every control change, so
  //: the two never disagree — the atoms checkbox wins over the slider,
  //: and the slider is disabled while atoms are off rather than left
  //: looking live over a hidden overlay.
  function applyAtomState() {
    if (!atomicOverlay) return;
    const on = !atomsToggle || atomsToggle.checked;
    atomicOverlay.setVisible(on);
    if (on) atomicOverlay.applyT(Number(smoothEls.slider.value) / 100);
    if (smoothEls.slider) smoothEls.slider.disabled = !on;
    if (smoothEls.legend) smoothEls.legend.style.display = on ? "flex" : "none";
  }
  // Declared here rather than beside the explode button's own listener:
  // `applyUiState` resets it on every render, and the first render runs
  // before that listener is wired.
  let exploded = false;

  function renderScene(shapes, { camera = null, refit = true } = {}) {
    shownShapes = shapes;
    viewer.clear();
    viewer.render(shapes, renderOptions, viewerOptions);
    // Every render, not just the first: a reload that reintroduced a
    // second addressing scheme would otherwise pass the check once at
    // load and go quiet exactly when it started lying.
    _checkPathInvariant(viewer, shapes);
    applyUiState();
    if (camera) {
      try {
        viewer.setCameraLocationSettings(
          camera.position, camera.quaternion, camera.target, camera.zoom
        );
      } catch (err) {
        // A camera we failed to restore is a moved view, not a broken
        // one — never let it cost the render.
        console.error("blocktree-3d: camera restore failed", err);
      }
    }
    // The overlay's meshes live in the `_rendered` scene `clear()` just
    // dropped, so it is re-established per render. It re-reads its own
    // atomic payload each time (browser-cached), and the slider keeps
    // whatever position the user left it at.
    if (atomicUrl && smoothEls && smoothEls.slider) {
      atomicOverlay = null;
      _setupAtomicOverlay(viewer, atomicUrl, smoothEls, shapes)
        .then((overlay) => {
          atomicOverlay = overlay;
          if (overlay) applyAtomState();
        })
        .catch((err) => {
          console.error("blocktree-3d: atomic overlay failed", err);
        });
    }
    if (!refit) return;
    const fittedHeight = _fitViewerToShell(
      viewer, viewerEl, treeWidth, initialCadWidth, initialHeight
    );
    if (fittedHeight !== initialHeight) {
      _applyTreeHeightCap(
        treeHeightCapEl,
        Math.max(80, fittedHeight - _INFO_PANEL_HEIGHT),
        _INFO_PANEL_HEIGHT
      );
    }
  }

  // Re-applied after EVERY render. A re-render that silently dropped the
  // container mode or the connections checkbox would leave the page
  // showing one thing and its own controls claiming another — the same
  // class of quiet wrongness as the inert toggles this viewer just had
  // fixed, so this is part of the seam, not a nicety.
  function applyUiState() {
    applyContainerMode(containerModeSelect ? containerModeSelect.value : "translucent");
    if (connectionsToggle && !connectionsToggle.checked) {
      for (const c of data.connections || []) {
        if (!findPart(shownShapes, c.path)) continue;
        try {
          viewer.setState(c.path, [3, 0]);
        } catch (err) {
          console.error("blocktree-3d: connections re-apply failed for", c.path, err);
        }
      }
    }
    // A fresh scene is never exploded — the animation lived on the
    // Viewer's previous `_rendered`. Say so on the button rather than
    // leaving it reading "un-explode" over an un-exploded scene.
    exploded = false;
    if (explodeButton) explodeButton.textContent = "explode";
  }

  try {
    const display = new Display(viewerEl, displayOptions);
    viewer = new Viewer(display, viewerOptions, notify);
    renderScene(data.shapes);
  } catch (err) {
    showError(viewerEl, "3D viewer failed to start: " + String(err));
    console.error("blocktree-3d: viewer init failed", err);
    return;
  }

  // gr340029 — re-fit the shell (and re-budget/re-measure the Display
  // against it) on every window resize, not just at init: a shell sized
  // once at load time would drift back out of the viewport the moment
  // the window itself is resized shorter.
  let resizeRAF = null;
  window.addEventListener("resize", () => {
    if (resizeRAF) return;
    resizeRAF = requestAnimationFrame(() => {
      resizeRAF = null;
      viewerEl.style.height = `${_fitShellHeight(viewerEl)}px`;
      const nextShellWidth = viewerEl.clientWidth || shellWidth;
      const nextShellHeight = viewerEl.clientHeight || shellHeight;
      const nextCadWidth = Math.max(
        _MIN_CAD_WIDTH,
        nextShellWidth - treeWidth - 2 * _OUTER_MARGIN
      );
      const nextHeight = Math.max(
        _MIN_CAD_HEIGHT,
        nextShellHeight - _TOOLBAR_HEIGHT_GUESS - 2 * _OUTER_MARGIN
      );
      try {
        viewer.resizeCadView(nextCadWidth, treeWidth, nextHeight);
      } catch (err) {
        // Best-effort — see recolour's own try/catch for the convention.
        console.error("blocktree-3d: resizeCadView on resize failed", err);
      }
      const fittedH = _fitViewerToShell(
        viewer, viewerEl, treeWidth, nextCadWidth, nextHeight
      );
      _applyTreeHeightCap(
        treeHeightCapEl,
        Math.max(80, fittedH - _INFO_PANEL_HEIGHT),
        _INFO_PANEL_HEIGHT
      );
    });
  });

  // mermaid -> 3D linked selection (the other direction).
  if (mermaidEl) {
    mermaidEl.addEventListener("click", (ev) => {
      const g = ev.target.closest('[id^="flowchart-B"]');
      if (!g) return;
      const m = /^flowchart-B(\d+)-/.exec(g.id);
      if (!m) return;
      const path = findPathByUid(data.shapes, m[1]);
      if (path) selectPath(path);
    });
  }

  // ── explode: spread from the assembly centre, per axis ──────────────
  // Deliberately NOT the vendored viewer's own `setExplode()` (one uniform
  // factor from the bbox centre) — `data.explode` is computed server-side
  // with a factor per axis, so order is kept on every axis and a thin axis
  // still separates (blocktree_3d.explode_offsets's own docstring).
  if (explodeButton) {
    explodeButton.addEventListener("click", () => {
      if (!viewer) return;
      try {
        if (!exploded) {
          for (const [path, offset] of Object.entries(data.explode || {})) {
            // Pruned away by a client-side isolate — see applyContainerMode.
            if (!findPart(shownShapes, path)) continue;
            viewer.addPositionTrack(path, [0, EXPLODE_DURATION], [
              [0, 0, 0],
              offset,
            ]);
          }
          // 4th arg is the LOOP MODE, not autoplay: LoopOnce here, so
          // the scrub bar this puts on screen runs the clip once rather
          // than pulsing. `initAnimation` never plays anything by itself.
          viewer.initAnimation(EXPLODE_DURATION, 1, "E", false);
          // ...so go to the end of the clip and hold there. Without this
          // the button was inert for its whole life: tracks built, bar
          // shown, label flipped, model untouched.
          viewer.setRelativeTime(1);
          explodeButton.textContent = "un-explode";
        } else {
          viewer.clearAnimation();
          viewer.update(true);
          explodeButton.textContent = "explode";
        }
        exploded = !exploded;
      } catch (err) {
        console.error("blocktree-3d: explode toggle failed", err);
      }
    });
  }

  // ── connections visibility toggle (gr337746) ─────────────────────────
  // A drawn connect is an edges-only leaf (blocktree_3d.connectivity_leaf
  // ships ``state: [3, 1]`` — slot 0/SHAPE is 3, "not applicable"). The
  // vendored tree's own eyeball for that slot reads permanently disabled
  // by design: its model-level toggle bails immediately whenever the
  // CURRENT value of the slot it's asked to flip is 3, no matter what
  // gets pushed at it — there is no vendored way to re-enable it. Slot 1/
  // EDGE is a real, working visible/hidden flag though, so this checkbox
  // drives THAT slot directly via the viewer's own public ``setState``
  // API, one connectivity leaf at a time — no vendored code touched.
  //
  // ``c.path`` is now the SAME name-derived path the vendored treeview
  // resolves internally (viewer-toggles fix,
  // precis_web/blocktree_3d.py's module docstring) — before that fix,
  // ``c.path`` carried the old uid-suffixed scheme, so every ``setState``
  // call here silently missed (``findNodeByPath`` walks by ``name``,
  // never found a segment matching a uid) and this checkbox was as inert
  // as every tree eyeball. No change needed here beyond the id scheme
  // itself unifying — this call was already the right shape.
  if (connectionsToggle) {
    connectionsToggle.addEventListener("change", () => {
      if (!viewer) return;
      const edgeState = connectionsToggle.checked ? 1 : 0;
      for (const c of data.connections || []) {
        // Pruned away by a client-side isolate — see applyContainerMode.
        if (!findPart(shownShapes, c.path)) continue;
        try {
          viewer.setState(c.path, [3, edgeState]);
        } catch (err) {
          console.error(
            "blocktree-3d: connections toggle failed for",
            c.path,
            err
          );
        }
      }
    });
  }

  // ── container envelope mode select (viewer fix) ──────────────────────
  // No page reload — same pattern as connectionsToggle above, just
  // re-running applyContainerMode (defined above, next to the viewer's
  // own construction) against the newly-chosen mode.
  if (containerModeSelect) {
    containerModeSelect.addEventListener("change", () => {
      applyContainerMode(containerModeSelect.value);
    });
  }

  // ── scale bar overlay (gr340030) ─────────────────────────────────────
  // Started once. It ticks off the LIVE `viewer` and re-reads the scene
  // each frame, so it needs no re-wiring across a scene reload — and
  // starting it again would append a second bar and a second rAF loop.
  _startScaleBar(viewer, viewerEl, data.scale);

  // ── atomic ↔ smooth overlay slider (gr450675) ────────────────────────
  // The overlay itself is (re)built inside `renderScene`; this listener
  // owns the slider and drives whichever `applyT` is current. Coalesces a
  // drag's rapid-fire `input` events to at most one `applyT` (full
  // per-atom lerp + mesh update + `viewer.update(true)`) per animation
  // frame — same requestAnimationFrame-gated-by-a-pending-flag idiom as
  // _startScaleBar's tick loop and the resize handler. The flush reads
  // `slider.value` fresh rather than caching it at schedule time, so the
  // last event before the frame — including the drag's trailing edge —
  // is the one that lands; no separate flush-on-end handler needed.
  if (atomicUrl && smoothEls && smoothEls.slider) {
    let sliderRAF = null;
    smoothEls.slider.addEventListener("input", () => {
      if (sliderRAF !== null) return;
      sliderRAF = requestAnimationFrame(() => {
        sliderRAF = null;
        if (atomicOverlay) atomicOverlay.applyT(Number(smoothEls.slider.value) / 100);
      });
    });
  }
  if (atomsToggle) atomsToggle.addEventListener("change", applyAtomState);

  // ── live level / overrides / isolate (no page reload) ────────────────
  //
  // `isolate` is a CLIENT-side filter over the already-fetched shapes
  // tree: the whole scene is in hand, so pruning it costs no round trip
  // and no rebuild. `level` and `overrides` still refetch, because the
  // server's plan is what decides which blocks get tessellated at all —
  // they just swap the scene in place now instead of reloading the page.
  //
  // The three stay in the URL (replaceState, never pushState — this is
  // not navigation) so a link still reproduces the view. The server
  // ignores `isolate` on the scene endpoint; the client is what honours
  // it.
  function currentIsolate() {
    return isolateSelect && isolateSelect.value ? isolateSelect.value : null;
  }

  function syncUrl() {
    try {
      const url = new URL(window.location.href);
      const set = (k, v) => {
        if (v) url.searchParams.set(k, v);
        else url.searchParams.delete(k);
      };
      if (levelSelect) set("level", levelSelect.value);
      if (overridesInput) set("overrides", overridesInput.value.trim());
      set("isolate", currentIsolate());
      window.history.replaceState(null, "", url);
    } catch (err) {
      // A URL we failed to rewrite costs shareability, never the view.
      console.error("blocktree-3d: URL sync failed", err);
    }
  }

  function applyIsolate() {
    const name = currentIsolate();
    const shapes = name ? isolateSubtree(data.shapes, name) : data.shapes;
    if (!shapes) {
      console.error("blocktree-3d: no subtree named", name);
      return;
    }
    // Refit deliberately: recentring on the subtree is the whole point
    // of isolating, so this is the one path that MOVES the camera.
    renderScene(shapes);
    syncUrl();
  }

  //: `level`/`overrides` are in flight. The two controls that trigger a
  //: refetch are disabled for the duration rather than left live over a
  //: guard that silently drops the second change.
  let reloading = false;

  //: What to re-focus once the refetch ends, or null. Disabling a focused
  //: element blurs it and re-enabling does NOT give focus back, so for the
  //: overrides box — a text field whose Enter key is what starts the
  //: refetch in the first place — the caret has to be carried across by
  //: hand. Measured: without this, `document.activeElement` is `(none)` for
  //: the whole rebuild and after it.
  let busyFocus = null;

  function setBusy(busy) {
    if (busyEl) busyEl.hidden = !busy;
    if (busy) {
      const active = document.activeElement;
      if (active === overridesInput) {
        busyFocus = {
          el: active,
          start: active.selectionStart,
          end: active.selectionEnd,
        };
      } else if (active === levelSelect) {
        busyFocus = { el: active, start: null, end: null };
      } else {
        busyFocus = null;
      }
    }
    for (const el of [levelSelect, overridesInput]) {
      if (el) el.disabled = busy;
    }
    if (!busy && busyFocus) {
      const { el, start, end } = busyFocus;
      busyFocus = null;
      try {
        el.focus();
        if (start !== null && el.setSelectionRange) {
          el.setSelectionRange(start, end);
        }
      } catch (err) {
        // Focus is a courtesy; never let it cost the render that just
        // finished.
        console.error("blocktree-3d: refocus after refetch failed", err);
      }
    }
  }

  async function loadScene() {
    if (reloading) return;
    reloading = true;
    setBusy(true);
    const camera = (() => {
      try {
        return viewer.getCameraLocationSettings();
      } catch {
        return null;
      }
    })();
    try {
      const url = new URL(sceneUrl, window.location.href);
      if (levelSelect) url.searchParams.set("level", levelSelect.value);
      if (overridesInput) {
        const raw = overridesInput.value.trim();
        if (raw) url.searchParams.set("overrides", raw);
        else url.searchParams.delete("overrides");
      }
      // Server-side isolate is retired: the client filters instead, so
      // the fetched scene is always the whole design.
      url.searchParams.delete("isolate");
      const resp = await fetch(url);
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        showError(viewerEl, body.error || `failed to load scene (${resp.status})`);
        return;
      }
      data = await resp.json();
      const name = currentIsolate();
      const shapes = name ? isolateSubtree(data.shapes, name) || data.shapes : data.shapes;
      renderScene(shapes, { camera, refit: false });
      syncUrl();
    } catch (err) {
      showError(viewerEl, "failed to load scene: " + String(err));
    } finally {
      reloading = false;
      setBusy(false);
    }
  }

  if (levelSelect) levelSelect.addEventListener("change", loadScene);
  if (isolateSelect) isolateSelect.addEventListener("change", applyIsolate);
  if (overridesInput) {
    overridesInput.addEventListener("change", loadScene);
    overridesInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        loadScene();
      }
    });
  }
  // An `isolate` carried in on the URL is applied client-side at load —
  // the server no longer did it for us.
  if (currentIsolate()) applyIsolate();
}
