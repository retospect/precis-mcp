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

//: Amber = a partner of the selection, sky blue = the selection itself.
const HIGHLIGHT_COLOUR = "#f59e0b";
const SELECTED_COLOUR = "#0ea5e9";
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

// gr462702 — the vendored renderer is WebGL2-only, so a browser without it
// (hardware acceleration off, WebGL blocked after a graphics crash) fails
// at `new Display`/`new Viewer` with three's "Error creating WebGL context."
function _isWebglError(err) {
  return /webgl/i.test(String((err && err.message) || err));
}

// Replace the dead 3D viewer with a plain message, the design's 2D SVG
// inline, and a link to the 2D page; the 3D-only controls are disabled so
// they cannot throw. Built with createElement/textContent only (see
// showError). `reload` adds a "reload the 3D view" button (context lost);
// `message` replaces the generated text (the atom-view timeout).
function showViewerFallback(viewerEl, err, { reload = false, message = null } = {}) {
  const webgl = _isWebglError(err);
  const box = document.createElement("div");
  box.id = "bt3d-fallback";
  box.className = "p-3 text-sm text-slate-700 space-y-2 overflow-auto";
  box.style.height = "100%";
  const msg = document.createElement("p");
  msg.id = "bt3d-fallback-message";
  if (message) {
    msg.textContent = message;
  } else if (webgl) {
    msg.textContent = reload
      ? "The 3D view lost its WebGL 2 context (usually a graphics driver reset or the browser reclaiming it), so it cannot draw any more. Reloading the 3D view usually brings it back."
      : "The 3D view needs WebGL 2, which this browser could not start. Usual causes: hardware acceleration turned off, an older browser, or WebGL blocked for this site after a graphics crash — reloading or another browser may help.";
  } else {
    msg.textContent = "3D viewer failed to start: " + String(err);
  }
  box.appendChild(msg);
  if (reload) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "bt3d-fallback-reload";
    btn.className = "px-3 py-1 rounded border border-slate-300 hover:bg-slate-100";
    btn.textContent = "reload the 3D view";
    btn.addEventListener("click", () => window.location.reload());
    box.appendChild(btn);
  }
  const svgUrl = viewerEl.dataset.svgUrl;
  if (svgUrl) {
    const img = document.createElement("img");
    img.id = "bt3d-fallback-img";
    img.src = svgUrl;
    img.alt = "2D view of the design";
    img.className = "max-w-full border border-slate-200 rounded";
    box.appendChild(img);
  }
  const url2d = viewerEl.dataset["2dUrl"];
  if (url2d) {
    const a = document.createElement("a");
    a.href = url2d;
    a.className = "block text-xs text-blue-600 hover:underline";
    a.textContent = "2D view →";
    box.appendChild(a);
  }
  viewerEl.replaceChildren(box);
  for (const id of [
    "bt3d-level", "bt3d-explode", "bt3d-export-png", "bt3d-export-svg",
    "bt3d-connections", "bt3d-container-mode", "bt3d-atoms", "bt3d-target",
    "bt3d-smooth",
  ]) {
    const el = document.getElementById(id);
    if (el) el.disabled = true;
  }
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

// ── view export (Reto, 2026-10-01) ──────────────────────────────────────
//
// "export as vector or png … with scale bar as it is shown on screen".
// The vendored renderer runs without preserveDrawingBuffer, so the WebGL
// canvas only holds the picture right after a draw: `viewer.update(true)`
// draws synchronously, and the copy happens in the same task. The scale
// bar is the live overlay's own DOM — read back from its boxes and drawn
// at the same place, length and label, so the export says what the
// screen said. The SVG carries the scene as an embedded raster (a WebGL
// scene has no vector form to hand) and the scale bar as vector.

//: The live scale bar in the WebGL canvas's CSS pixels, or null when it
//: is hidden (no scene scale, or the camera read failed).
function _scaleBarBox(viewerEl, glRect) {
  const bar = viewerEl.querySelector(".bt3d-scale-bar");
  if (!bar || bar.style.display === "none") return null;
  const line = bar.querySelector(".bt3d-scale-bar-line");
  const label = bar.querySelector(".bt3d-scale-bar-label");
  if (!line || !label || !label.textContent) return null;
  const l = line.getBoundingClientRect();
  const t = label.getBoundingClientRect();
  if (!l.width) return null;
  const lineStyle = getComputedStyle(line);
  const labelStyle = getComputedStyle(label);
  return {
    // The bracket: left tick, bottom rule, right tick (the line's three
    // CSS borders), centred on the border width.
    x0: l.left - glRect.left + 1,
    x1: l.right - glRect.left - 1,
    yTop: l.top - glRect.top,
    yBottom: l.bottom - glRect.top - 1,
    stroke: lineStyle.borderBottomColor,
    label: label.textContent,
    lx: t.left - glRect.left,
    ly: t.top - glRect.top,
    lw: t.width,
    lh: t.height,
    labelBg: labelStyle.backgroundColor,
    labelColour: labelStyle.color,
    font: `${labelStyle.fontSize} ${labelStyle.fontFamily}`,
    padRight: parseFloat(labelStyle.paddingRight) || 0,
  };
}

//: A 2D canvas at the WebGL canvas's device resolution holding the view
//: as drawn now, on white; with `withBar`, the scale bar on top. Null
//: when there is no canvas yet.
function _exportCanvas(viewer, viewerEl, { withBar }) {
  const gl = viewerEl.querySelector("canvas");
  if (!gl || !gl.width || !gl.height) return null;
  const out = document.createElement("canvas");
  out.width = gl.width;
  out.height = gl.height;
  const ctx = out.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, out.width, out.height);
  viewer.update(true);
  ctx.drawImage(gl, 0, 0);
  if (!withBar) return out;
  const glRect = gl.getBoundingClientRect();
  const box = _scaleBarBox(viewerEl, glRect);
  if (!box) return out;
  const s = gl.width / glRect.width;
  ctx.save();
  ctx.scale(s, s);
  ctx.fillStyle = box.labelBg;
  ctx.fillRect(box.lx, box.ly, box.lw, box.lh);
  ctx.fillStyle = box.labelColour;
  ctx.font = box.font;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillText(box.label, box.lx + box.lw - box.padRight, box.ly + box.lh / 2);
  ctx.strokeStyle = box.stroke;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(box.x0, box.yTop);
  ctx.lineTo(box.x0, box.yBottom);
  ctx.lineTo(box.x1, box.yBottom);
  ctx.lineTo(box.x1, box.yTop);
  ctx.stroke();
  ctx.restore();
  return out;
}

function _xmlEscape(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

//: The view as an SVG document: the scene as an embedded PNG, the scale
//: bar as vector paths and text, both in the canvas's CSS pixels.
function _exportSvg(viewer, viewerEl) {
  const shot = _exportCanvas(viewer, viewerEl, { withBar: false });
  if (!shot) return null;
  const gl = viewerEl.querySelector("canvas");
  const glRect = gl.getBoundingClientRect();
  const w = Math.round(glRect.width);
  const h = Math.round(glRect.height);
  const parts = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`,
    `<image width="${w}" height="${h}" href="${shot.toDataURL("image/png")}"/>`,
  ];
  const box = _scaleBarBox(viewerEl, glRect);
  if (box) {
    const f = (n) => n.toFixed(2);
    parts.push(
      `<g class="scale-bar">`,
      `<rect x="${f(box.lx)}" y="${f(box.ly)}" width="${f(box.lw)}" height="${f(box.lh)}" fill="${_xmlEscape(box.labelBg)}"/>`,
      `<text x="${f(box.lx + box.lw - box.padRight)}" y="${f(box.ly + box.lh / 2)}" text-anchor="end" dominant-baseline="central" fill="${_xmlEscape(box.labelColour)}" style="font: ${_xmlEscape(box.font)}">${_xmlEscape(box.label)}</text>`,
      `<path d="M${f(box.x0)} ${f(box.yTop)}V${f(box.yBottom)}H${f(box.x1)}V${f(box.yTop)}" fill="none" stroke="${_xmlEscape(box.stroke)}" stroke-width="2"/>`,
      `</g>`
    );
  }
  parts.push("</svg>");
  return parts.join("\n") + "\n";
}

function _download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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

//: The strain layers' ramps (Reto, 2026-10-02): one
//: colour family per layer, so all three read at once — green on the
//: bonds, orange on the atoms. Same stops as the template's swatches.
//: Every coloured element sits at or above the threshold, so the ramp's
//: low end is already a hot spot and has to stand off the grey: a pale
//: start (#bbf7d0) left thin bond cylinders unreadable on the drum.
const _BOND_STRAIN_STOPS = [
  [0.0, [0x22, 0xc5, 0x5e]],
  [0.5, [0x16, 0xa3, 0x4a]],
  [1.0, [0x14, 0x53, 0x2d]],
];
const _ANGLE_STRAIN_STOPS = [
  [0.0, [0xfb, 0x92, 0x3c]],
  [0.5, [0xea, 0x58, 0x0c]],
  [1.0, [0x7c, 0x2d, 0x12]],
];
//: A surface vertex below its layer's threshold.
const _BELOW_THRESHOLD_SURFACE = [0xd4 / 255, 0xd4 / 255, 0xd8 / 255];
const _BOND_GREY = 0x808080;
//: A bond at or above the bond-strain threshold is drawn this much fatter:
//: at the legibility-floored atom radius, a plain-width bond is mostly
//: hidden between its two atoms, colour or not (measured on
//: se:hexa-smooth-drum-v2).
const _HOT_BOND_SCALE = 2;

function _rampColor(stops, t) {
  const clamped = Math.max(0, Math.min(1, t));
  for (let i = 0; i < stops.length - 1; i++) {
    const [t0, c0] = stops[i];
    const [t1, c1] = stops[i + 1];
    if (clamped >= t0 && clamped <= t1) {
      const f = t1 > t0 ? (clamped - t0) / (t1 - t0) : 0;
      return [0, 1, 2].map((k) => (c0[k] + (c1[k] - c0[k]) * f) / 255);
    }
  }
  return [1, 1, 1];
}

function _deviationColor(t) {
  return _rampColor(_DEVIATION_STOPS, t);
}

//: Where a value sits on its layer's ramp: null below the threshold
//: (uncoloured), else 0 at the threshold to 1 at the layer's max.
function _aboveThreshold(v, thr, max) {
  if (v === null || v === undefined || !(v >= thr)) return null;
  return max > thr ? (v - thr) / (max - thr) : 1;
}

//: `{min, max, p95}` over a layer's applicable values, or null when the
//: layer applies to nothing. p95 is the default threshold: the element at
//: rank floor(0.95·n) of the sorted values, so ~5% sit at or above it.
function _layerStats(values) {
  const v = values.filter((x) => x !== null && x !== undefined && Number.isFinite(x));
  if (!v.length) return null;
  v.sort((a, b) => a - b);
  return {
    min: v[0],
    max: v[v.length - 1],
    p95: v[Math.min(v.length - 1, Math.floor(0.95 * v.length))],
  };
}

//: A hidden-at-load, honest-absence overlay (module docstring): no atomic
//: blocks, a fetch failure, or an unreachable private scene all degrade to
//: "the slider/legend never appear" (the template already omits them
//: server-side whenever ``has_atomic`` is false; this covers the rarer
//: rev-mismatch/fetch-failure cases too), never a broken primary viewer.
//:
//: Returns ``{applyT, setVisible, pickAtom}`` — the smooth-slider sink,
//: the atoms on/off switch and the click raycast — or ``null`` when the overlay degraded to
//: absence. The control LISTENERS live with the caller, not here: the
//: overlay's meshes are injected into ``viewer._rendered.scene``, which a
//: scene reload (``viewer.clear()``) drops, so the overlay has to be set
//: up again per scene — and a listener attached per setup would stack up
//: one duplicate handler per reload, each driving a dead ``applyT`` over
//: a scene graph that no longer holds its meshes.
//: Phase-boundary timing marks (gr462703): `performance.getEntriesByType(
//: "mark")` then reads the wait's phases exactly. Never costs the page.
function _bt3dMark(name) {
  try {
    performance.mark(name);
  } catch (err) {
    /* timing is best-effort */
  }
}

//: The load progress bar (gr462703): `el` is the template's `#bt3d-progress`
//: (`data-phase` = scene|render|server|download|build|done|timeout, label
//: in `#bt3d-progress-label`, fill in `#bt3d-progress-fill`). Null when the
//: page has no bar; callers guard on that.
function _makeProgress(el) {
  if (!el) return null;
  const label = el.querySelector("#bt3d-progress-label");
  const fill = el.querySelector("#bt3d-progress-fill");
  let ticker = null;
  function stop() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }
  // `fraction` null = indeterminate stripe (no honest percentage exists).
  function set(phase, text, fraction = null) {
    stop();
    el.hidden = false;
    el.dataset.phase = phase;
    if (fraction === null) {
      el.dataset.mode = "indeterminate";
    } else {
      delete el.dataset.mode;
      fill.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
    }
    if (label) label.textContent = text;
  }
  return {
    set,
    // Indeterminate stripe with elapsed seconds; after `stallAfterS` the
    // label says what is known instead.
    waiting(phase, text, stallAfterS, stallText) {
      set(phase, `${text} · 0 s`);
      const t0 = performance.now();
      ticker = setInterval(() => {
        const secs = Math.floor((performance.now() - t0) / 1000);
        if (label) label.textContent = secs >= stallAfterS ? stallText : `${text} · ${secs} s`;
      }, 1000);
    },
    timeout(text) {
      set("timeout", text);
      delete el.dataset.mode;
    },
    done() {
      stop();
      el.dataset.phase = "done";
      el.hidden = true;
    },
    // A failed load: the error box says why; the bar just goes away.
    hide() {
      stop();
      el.hidden = true;
    },
  };
}

const _ATOMIC_STALL_S = 30;
const _ATOMIC_TIMEOUT_S = 120;
const _ATOMIC_STALL_TEXT =
  "No answer from the server for 30 s. The page keeps waiting until 2 min, then shows the 2D view instead.";
const _ATOMIC_TIMEOUT_TEXT =
  "The atom view timed out: no answer from the server after 2 min. Showing the 2D view instead.";

//: Fetch atomic3d.json as a stream so the bar can show real downloaded MB
//: (Content-Length is the gzip size while the stream yields decoded bytes,
//: so no percentage is computed). Aborts at 2 min; the thrown error then
//: has `.timedOut` set. `progress` may be null (a re-render's refetch).
async function _fetchAtomicPayload(url, progress) {
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctl.abort();
  }, _ATOMIC_TIMEOUT_S * 1000);
  try {
    if (progress) {
      progress.waiting("server", "building atom view on the server", _ATOMIC_STALL_S, _ATOMIC_STALL_TEXT);
    }
    const r = await fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error(`atomic3d fetch failed (${r.status})`);
    const chunks = [];
    let bytes = 0;
    const reader = r.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      bytes += value.length;
      if (progress) progress.set("download", `downloading ${(bytes / 1e6).toFixed(1)} MB`);
    }
    const all = new Uint8Array(bytes);
    let off = 0;
    for (const c of chunks) {
      all.set(c, off);
      off += c.length;
    }
    return JSON.parse(new TextDecoder().decode(all));
  } catch (err) {
    if (timedOut) {
      const e = new Error("atomic3d timed out");
      e.timedOut = true;
      throw e;
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

//: Yield to the browser so the bar repaints between build slices.
function _nextFrame() {
  return new Promise((resolve) => requestAnimationFrame(resolve));
}
//: Atoms per slice of the overlay's mesh build (bonds proportionally).
const _BUILD_SLICE_ATOMS = 1000;

async function _setupAtomicOverlay(viewer, atomicUrl, smoothEls, sceneShapes, progress = null, isStale = () => false) {
  const [THREE, data] = await Promise.all([
    import("/static/three/three.module.min.js"),
    _fetchAtomicPayload(atomicUrl, progress),
  ]);
  _bt3dMark("bt3d-atomic-fetched");
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

  // Progress over the whole payload: atoms are what the label counts, the
  // bar's fill also advances through the bonds.
  let totalAtoms = 0, totalBonds = 0;
  for (const b of data.blocks) {
    totalAtoms += b.elements.length;
    totalBonds += (b.bonds || []).length;
  }
  let atomsDone = 0, bondsDone = 0;
  const reportBuild = async () => {
    if (!progress) return;
    const frac = (atomsDone + bondsDone) / Math.max(1, totalAtoms + totalBonds);
    progress.set(
      "build",
      `building ${atomsDone.toLocaleString()} of ${totalAtoms.toLocaleString()} atoms`,
      frac
    );
    await _nextFrame();
  };

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
        mesh.userData.cpk = colour;
        mesh.scale.setScalar(atomR);
        // What `pickAtom` hands back: the block's uid and the atom's
        // ordinal in this payload, which is the bound scene's own atom
        // order — the same order the pick route resolves against.
        mesh.userData.pick = { block: b.uid, atom: i };
        // The hover readout: element, then the atom's name (residue and
        // chain for a realized chain, the scene label otherwise).
        mesh.userData.hover = `${b.elements[i]} · ${(b.hover && b.hover[i]) || `#${i}`}`;
        group.add(mesh);
        atomMeshes.push(mesh);
        atomsDone++;
        if (atomsDone % _BUILD_SLICE_ATOMS === 0) await reportBuild();
      }
      const bondMeshes = [];
      const bondSlice = Math.max(1, Math.ceil((_BUILD_SLICE_ATOMS * (b.bonds || []).length) / Math.max(1, n)));
      for (const [i, j] of b.bonds || []) {
        const mat = new THREE.MeshStandardMaterial({
          color: _BOND_GREY,
          transparent: true,
        });
        const mesh = new THREE.Mesh(cylGeo, mat);
        group.add(mesh);
        bondMeshes.push({ mesh, i, j, hot: false });
        bondsDone++;
        if (bondMeshes.length % bondSlice === 0) await reportBuild();
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
        src: b,
        coords: b.coords,
        smooth: b.smooth,
        atomMeshes,
        bondMeshes,
        bondR,
        surfMesh,
        surfPositions: positions,
        surfColors: colors,
      });
    }
    // A newer render replaced the scene while this build yielded.
    if (isStale()) {
      scene.remove(group);
      return null;
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
      for (const { mesh, i, j, hot } of bondMeshes) {
        orientBond(mesh, lerped[i], lerped[j], hot ? bondR * _HOT_BOND_SCALE : bondR);
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

  //: The atom under a point, as `{block, atom, hover}`, or null. The vendored
  //: viewer raycasts only its own parts tree, so the overlay casts its
  //: own ray, from the vendored live camera (the same private reach as
  //: the scale bar's `_worldPerPixel`). Hidden atoms — atoms off, or the
  //: slider at the smooth end — are not pickable.
  const raycaster = new THREE.Raycaster();
  function pickAtom(clientX, clientY, canvas) {
    if (!group.visible) return null;
    const cc = viewer && viewer._rendered && viewer._rendered.camera;
    const camera = cc ? (cc.ortho ? cc.oCamera : cc.pCamera) : null;
    if (!camera || !canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(ndc, camera);
    const atoms = [];
    for (const blk of blocks) {
      for (const mesh of blk.atomMeshes) if (mesh.visible) atoms.push(mesh);
    }
    const hit = raycaster.intersectObjects(atoms, false)[0];
    return hit ? { ...hit.object.userData.pick, hover: hit.object.userData.hover } : null;
  }

  // ── target surface (smooth_drum's surface_meridian, revolved server-side)
  // One translucent double-sided mesh per block that carries a target, in
  // its own group so it is independent of the atoms toggle. Off by default;
  // the checkbox is revealed only when some block has a target.
  const targetGroup = new THREE.Group();
  targetGroup.name = "bt3d-target-overlay";
  targetGroup.visible = false;
  let hasTarget = false;
  for (const b of data.blocks) {
    if (!b.target || !b.target.verts || !b.target.verts.length) continue;
    const pos = new Float32Array(b.target.verts.length * 3);
    b.target.verts.forEach((v, i) => {
      pos[i * 3] = v[0];
      pos[i * 3 + 1] = v[1];
      pos[i * 3 + 2] = v[2];
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setIndex(b.target.tris.flat());
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(
      geo,
      new THREE.MeshBasicMaterial({
        color: 0x14b8a6,
        opacity: 0.3,
        transparent: true,
        side: THREE.DoubleSide,
        depthWrite: false,
      })
    );
    targetGroup.add(mesh);
    hasTarget = true;
  }
  if (hasTarget) scene.add(targetGroup);

  function setTargetVisible(on) {
    if (!hasTarget) return;
    targetGroup.visible = on;
    try {
      viewer.update(true);
    } catch (err) {
      console.error("blocktree-3d: target surface redraw failed", err);
    }
  }

  // ── strain layers (Reto, 2026-10-02) ─────────────────
  // Three measures on three parts of the picture, so one pixel never has
  // to show two numbers: surface deviation on the smoothed surface, bond
  // strain on the bond cylinders, angle strain (θp or the 120° RMS) on the
  // atom spheres. Each colours only what sits at or above its threshold;
  // the thresholds and on/off state live with the caller, which outlives
  // this per-render overlay.
  const _LAYER_FIELDS = {
    deviation: "deviation",
    bond: "bond_dev",
    thetap: "angle_strain_thetap",
    a120: "angle_strain_120",
  };
  const strainStats = {};
  for (const [key, field] of Object.entries(_LAYER_FIELDS)) {
    strainStats[key] = _layerStats(data.blocks.flatMap((b) => b[field] || []));
  }

  //: `state` = `{deviation: {thr}, bond: {on, thr}, angle: {on, key, thr}}`
  //: with every `thr` already a number (the caller resolves defaults);
  //: `angle.key` is `thetap` or `a120`. Returns, per layer, how many
  //: elements it coloured out of how many it applies to.
  function applyStrain(state) {
    const dev = strainStats.deviation;
    const bond = strainStats.bond;
    const angle = strainStats[state.angle.key];
    const counts = {
      deviation: { coloured: 0, total: 0 },
      bond: { coloured: 0, total: 0 },
      angle: { coloured: 0, total: 0 },
    };
    const tally = (layer, v, t) => {
      if (v === null || v === undefined) return;
      counts[layer].total += 1;
      if (t !== null) counts[layer].coloured += 1;
    };
    for (const blk of blocks) {
      const b = blk.src;
      const angleVals = b[_LAYER_FIELDS[state.angle.key]];
      blk.atomMeshes.forEach((mesh, i) => {
        const t = state.angle.on && angle && angleVals
          ? _aboveThreshold(angleVals[i], state.angle.thr, angle.max)
          : null;
        if (angleVals) tally("angle", angleVals[i], t);
        if (t === null) mesh.material.color.set(mesh.userData.cpk);
        else mesh.material.color.setRGB(..._rampColor(_ANGLE_STRAIN_STOPS, t));
      });
      blk.bondMeshes.forEach((entry, k) => {
        const { mesh } = entry;
        const t = state.bond.on && bond && b.bond_dev
          ? _aboveThreshold(b.bond_dev[k], state.bond.thr, bond.max)
          : null;
        if (b.bond_dev) tally("bond", b.bond_dev[k], t);
        entry.hot = t !== null;
        mesh.scale.x = mesh.scale.z = entry.hot ? blk.bondR * _HOT_BOND_SCALE : blk.bondR;
        if (t === null) mesh.material.color.set(_BOND_GREY);
        else mesh.material.color.setRGB(..._rampColor(_BOND_STRAIN_STOPS, t));
      });
      const n = blk.atomMeshes.length;
      for (let i = 0; i < n; i++) {
        const t = dev ? _aboveThreshold(b.deviation[i], state.deviation.thr, dev.max) : null;
        tally("deviation", b.deviation[i], t);
        const rgb = t === null ? _BELOW_THRESHOLD_SURFACE : _deviationColor(t);
        blk.surfColors.set(rgb, i * 3);
      }
      blk.surfMesh.geometry.attributes.color.needsUpdate = true;
    }
    try {
      viewer.update(true);
    } catch (err) {
      console.error("blocktree-3d: strain layer redraw failed", err);
    }
    return counts;
  }

  return { applyT, setVisible, pickAtom, hasTarget, setTargetVisible, strainStats, applyStrain };
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
  // The load progress bar (`#bt3d-progress`, gr462703). Optional.
  progressEl,
  // Atoms on/off for a structure-bound design. Optional — the template
  // only renders it alongside the atomic↔smooth slider.
  atomsToggle,
  sceneUrl,
  atomicUrl,
  smoothEls,
  // Strain-layer rows `{bond, angle}` (the surface-deviation row is
  // `smoothEls.legend`); each holds `data-role` children — toggle,
  // threshold, value, min, max, and `measure` on the angle row. Optional.
  strainEls,
  noteUrls,
  noteEls,
  // Design chat (design-workbench build, slice 3): called with the block
  // NAME on every selection (viewer pick or topology click) so the page
  // can drop it into the chat box as a handle. Optional.
  onSelectBlock,
  // Pick panel (se-pick-hierarchy): `pickUrl` answers a click with every
  // level the picked atom or block belongs to; `onCiteToken(token)` takes
  // a level's token into the design chat. All optional.
  pickUrl,
  pickEls,
  onCiteToken,
  // View export: `{png, svg}` buttons, and the file name's stem (the
  // design slug). Optional.
  exportEls,
  exportName,
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
  const progress = _makeProgress(progressEl);
  if (progress) progress.set("scene", "loading design…");
  let data;
  try {
    const resp = await fetch(sceneUrl);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      if (progress) progress.hide();
      showError(viewerEl, body.error || `failed to load scene (${resp.status})`);
      return;
    }
    data = await resp.json();
    _bt3dMark("bt3d-scene-fetched");
    if (progress) progress.set("render", "building the 3D scene…");
  } catch (err) {
    if (progress) progress.hide();
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
  // collapse: 2 (EXPANDED) renders every block row up front; the vendored
  // default (COLLAPSED) shows only the root row, leaving the per-block
  // level chips nothing to attach to. Not -1 (LEAVES): with the root chipped
  // to `envelope` the whole design is one leaf, LEAVES folds its group, and
  // the chip that would undo it disappears.
  const viewerOptions = { up: "Z", collapse: 2 };

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

  //: The leaf that carries a block's colour on screen: the block itself
  //: when it is a leaf, its own `(envelope)` leaf when it is a container
  //: (`data.container_paths`), else null. A container's group has no
  //: material — tinting it is what left a selected axle's most important
  //: partners, the crank assemblies, unmarked.
  function tintLeafOf(path) {
    if (recolourable(path)) return path;
    const leaf = (data.container_paths || []).find(
      (p) => p.startsWith(path + "/") && !p.slice(path.length + 1).includes("/")
    );
    return leaf && recolourable(leaf) ? leaf : null;
  }

  function tint(path, colour) {
    const leaf = tintLeafOf(path);
    if (!leaf || highlighted.has(leaf)) return;
    const part = findPart(data.shapes, leaf);
    if (!part) return;
    highlighted.set(leaf, part.color);
    recolour(leaf, colour);
  }

  //: The current selection's path, so a re-render (level change, isolate)
  //: can put its tint back — `clear()` + `render()` rebuilds every
  //: material at its own colour.
  let selectedPath = null;

  function tintSelection(primaryPath) {
    // The selection itself first, in its own colour, so a partner that
    // shares its envelope leaf cannot claim it amber.
    tint(primaryPath, SELECTED_COLOUR);
    for (const c of connectionsTouching(data.connections, primaryPath)) {
      const other = c.a_path === primaryPath ? c.b_path : c.a_path;
      for (const p of [c.path, other]) tint(p, HIGHLIGHT_COLOUR);
    }
  }

  //: After a re-render: the old tints went with the old materials, so the
  //: bookkeeping is dropped (not reverted) and the selection re-tinted if
  //: its block is still in the scene shown.
  function retintAfterRender(shapes) {
    highlighted.clear();
    if (selectedPath && findPart(shapes, selectedPath)) tintSelection(selectedPath);
    repaint();
  }

  function selectPath(primaryPath) {
    clearHighlight();
    selectedPath = primaryPath;
    tintSelection(primaryPath);
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
    if (part && part.uid !== undefined) showPick({ token: `<se:${part.uid}>` });
  }

  // ── pick panel (se-pick-hierarchy) ───────────────────────────────────
  //
  // A pick — an atom click, or a block selection — lists every level it
  // belongs to, innermost first, one citable token per row
  // (`/se/<slug>/pick`, the same resolution as get(kind='se',
  // view='pick')). "cite" hands that row's token to the design chat.
  // Server text reaches the DOM through textContent only.
  let pickSeq = 0;
  async function showPick(params) {
    if (!pickUrl || !pickEls || !pickEls.panel) return;
    const seq = ++pickSeq;
    const qs = new URLSearchParams(params).toString();
    let body;
    try {
      const r = await fetch(`${pickUrl}?${qs}`);
      body = await r.json();
      if (!r.ok) throw new Error(body.error || `pick failed (${r.status})`);
    } catch (err) {
      if (seq !== pickSeq) return;
      pickEls.panel.classList.remove("hidden");
      pickEls.rows.textContent = "";
      pickEls.subject.textContent = "";
      pickEls.status.textContent = String(err.message || err);
      pickEls.status.classList.remove("hidden");
      return;
    }
    // A slower answer to an earlier click must not overwrite a newer one.
    if (seq !== pickSeq) return;
    pickEls.status.classList.add("hidden");
    // The innermost row names the pick in words; the server's subject line
    // for a block pick is the bare token, which says nothing to a person.
    const first = (body.levels || [])[0];
    pickEls.subject.textContent = first
      ? `${first.label} (${first.level})`
      : body.subject || "";
    pickEls.rows.textContent = "";
    for (const row of body.levels || []) {
      const tr = document.createElement("tr");
      tr.className = "border-t border-slate-100";
      const level = document.createElement("td");
      level.className = "py-0.5 pr-2 text-slate-400 whitespace-nowrap";
      level.textContent = row.level;
      const label = document.createElement("td");
      label.className = "py-0.5 pr-2 text-slate-700";
      label.textContent = row.label;
      const token = document.createElement("td");
      token.className = "py-0.5 pr-2 font-mono text-slate-500 whitespace-nowrap";
      token.textContent = row.token;
      const act = document.createElement("td");
      act.className = "py-0.5 text-right";
      if (typeof onCiteToken === "function") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "px-2 rounded border border-slate-300 hover:bg-slate-100";
        btn.textContent = "cite";
        btn.addEventListener("click", () => onCiteToken(row.token));
        act.appendChild(btn);
      }
      tr.append(level, label, token, act);
      pickEls.rows.appendChild(tr);
    }
    pickEls.panel.classList.remove("hidden");
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

  // An atom click opens the pick panel. A click, not a drag: the press
  // and release must land within a few pixels, or it was an orbit. Wired
  // once on the viewer element (capture phase — the vendored controls
  // own the canvas's own listeners) and reads whichever overlay the
  // current render built.
  const _CLICK_SLOP_PX = 4;
  let pressAt = null;
  viewerEl.addEventListener(
    "pointerdown",
    (e) => { pressAt = e.button === 0 ? [e.clientX, e.clientY] : null; },
    true
  );
  viewerEl.addEventListener(
    "pointerup",
    (e) => {
      const from = pressAt;
      pressAt = null;
      if (!from || !atomicOverlay || !pickUrl) return;
      if (Math.hypot(e.clientX - from[0], e.clientY - from[1]) > _CLICK_SLOP_PX) return;
      const canvas = viewerEl.querySelector("canvas");
      const hit = atomicOverlay.pickAtom(e.clientX, e.clientY, canvas);
      if (hit) showPick({ block: `#${hit.block}`, atom: String(hit.atom) });
    },
    true
  );

  // Atom hover readout: the atom under the pointer (element, then its
  // residue and chain or scene label), the way the vendored viewer
  // already reads out an edge's length. One raycast per animation frame
  // at most, none while a button is held (that is an orbit).
  // Attached on first show, and again if a render emptied the shell.
  const hoverTip = document.createElement("div");
  hoverTip.className = "bt3d-atom-tip";
  hoverTip.hidden = true;
  let hoverAt = null;
  let hoverQueued = false;
  function updateHover() {
    hoverQueued = false;
    const at = hoverAt;
    const canvas = viewerEl.querySelector("canvas");
    const hit = at && atomicOverlay ? atomicOverlay.pickAtom(at[0], at[1], canvas) : null;
    if (!hit) {
      hoverTip.hidden = true;
      return;
    }
    if (!hoverTip.isConnected) viewerEl.appendChild(hoverTip);
    const box = viewerEl.getBoundingClientRect();
    hoverTip.textContent = hit.hover;
    hoverTip.style.left = `${at[0] - box.left + 12}px`;
    hoverTip.style.top = `${at[1] - box.top + 12}px`;
    hoverTip.hidden = false;
  }
  viewerEl.addEventListener("pointermove", (e) => {
    hoverAt = e.buttons ? null : [e.clientX, e.clientY];
    if (!hoverQueued) {
      hoverQueued = true;
      requestAnimationFrame(updateHover);
    }
  });
  viewerEl.addEventListener("pointerleave", () => {
    hoverAt = null;
    hoverTip.hidden = true;
  });

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
    applyStrainState();
    fadeStrainRows(Number(smoothEls.slider.value) / 100);
    applyTargetState();
  }

  // ── strain-layer controls (Reto, 2026-10-02) ─────────
  // State lives here, not in the overlay, so a level change's rebuild keeps
  // what the user set. `thr: null` = the layer's default, the 95th
  // percentile of the loaded structure (top 5% coloured); switching the
  // angle measure resets it, since θp and the 120° RMS share no scale.
  const _THRESHOLD_STEPS = 1000;
  const strainState = {
    deviation: { thr: null },
    bond: { on: false, thr: null },
    angle: { on: false, measure: "thetap", thr: null },
  };
  const strainRows = {
    deviation: smoothEls && smoothEls.legend,
    bond: strainEls && strainEls.bond,
    angle: strainEls && strainEls.angle,
  };
  const _STRAIN_UNITS = {
    deviation: { unit: " Å", digits: 2 },
    bond: { unit: " Å", digits: 3 },
    angle: { unit: "°", digits: 1 },
  };
  const _role = (row, role) => (row ? row.querySelector(`[data-role="${role}"]`) : null);
  const _statsKey = (layer) =>
    layer === "angle" ? (strainState.angle.measure === "120" ? "a120" : "thetap") : layer;

  //: Reads the overlay's stats into the rows (hidden where a layer applies
  //: to nothing, or atoms are off) and repaints with resolved thresholds.
  function applyStrainState() {
    if (!atomicOverlay || !atomicOverlay.strainStats) return;
    const atomsOn = !atomsToggle || atomsToggle.checked;
    const resolved = {};
    for (const layer of ["deviation", "bond", "angle"]) {
      const row = strainRows[layer];
      const stats = atomicOverlay.strainStats[_statsKey(layer)];
      const st = strainState[layer];
      const thr = stats ? (st.thr ?? stats.p95) : 0;
      resolved[layer] = {
        on: !!(st.on ?? true),
        thr,
        key: _statsKey(layer),
      };
      if (!row) continue;
      if (layer !== "deviation") {
        const show = !!stats && atomsOn;
        row.classList.toggle("hidden", !show);
        row.style.display = show ? "flex" : "none";
      }
      if (!stats) continue;
      const { unit, digits } = _STRAIN_UNITS[layer];
      const span = stats.max - stats.min;
      const slider = _role(row, "threshold");
      if (slider) {
        slider.max = String(_THRESHOLD_STEPS);
        slider.value = String(
          span > 0 ? Math.round(((thr - stats.min) / span) * _THRESHOLD_STEPS) : _THRESHOLD_STEPS
        );
      }
      const put = (role, text) => {
        const el = _role(row, role);
        if (el) el.textContent = text;
      };
      put("value", thr.toFixed(digits) + unit);
      put("min", stats.min.toFixed(digits));
      put("max", stats.max.toFixed(digits) + unit);
      const toggle = _role(row, "toggle");
      if (toggle) toggle.checked = !!st.on;
    }
    // Each row carries what its layer coloured (`data-coloured` of
    // `data-total`), so a check can read the top-5% rule off the page.
    const counts = atomicOverlay.applyStrain(resolved);
    for (const [layer, row] of Object.entries(strainRows)) {
      if (!row || !counts[layer]) continue;
      row.dataset.coloured = String(counts[layer].coloured);
      row.dataset.total = String(counts[layer].total);
    }
  }

  //: Atoms and bonds fade out toward the smooth end of the slider, and the
  //: two layers drawn on them fade with them.
  function fadeStrainRows(t) {
    for (const layer of ["bond", "angle"]) {
      const row = strainRows[layer];
      if (row) row.style.opacity = String(Math.max(0.35, 1 - t));
    }
  }

  for (const [layer, row] of Object.entries(strainRows)) {
    if (!row) continue;
    const toggle = _role(row, "toggle");
    if (toggle) {
      toggle.addEventListener("change", () => {
        strainState[layer].on = toggle.checked;
        applyStrainState();
      });
    }
    const slider = _role(row, "threshold");
    if (slider) {
      let raf = null;
      slider.addEventListener("input", () => {
        if (raf !== null) return;
        raf = requestAnimationFrame(() => {
          raf = null;
          const stats = atomicOverlay && atomicOverlay.strainStats[_statsKey(layer)];
          if (!stats) return;
          strainState[layer].thr =
            stats.min + ((stats.max - stats.min) * Number(slider.value)) / _THRESHOLD_STEPS;
          applyStrainState();
        });
      });
    }
    const measure = _role(row, "measure");
    if (measure) {
      measure.addEventListener("change", () => {
        strainState.angle.measure = measure.value;
        strainState.angle.thr = null;
        applyStrainState();
      });
    }
  }
  //: The target-surface checkbox only exists when some block carries a
  //: target; it is independent of the atoms toggle.
  function applyTargetState() {
    const toggle = smoothEls && smoothEls.targetToggle;
    const label = smoothEls && smoothEls.targetLabel;
    const has = !!(atomicOverlay && atomicOverlay.hasTarget);
    if (label) {
      label.classList.toggle("hidden", !has);
      label.style.display = has ? "flex" : "none";
    }
    if (atomicOverlay && has) atomicOverlay.setTargetVisible(!!(toggle && toggle.checked));
  }
  // Declared here rather than beside the explode button's own listener:
  // `applyUiState` resets it on every render, and the first render runs
  // before that listener is wired.
  let exploded = false;

  let firstRenderMarked = false;
  let atomicBuiltMarked = false;
  //: The bar covers the first overlay load only.
  let progressSpent = false;
  //: Bumped per render: a build that yielded across a newer render is stale.
  let renderGen = 0;

  function renderScene(shapes, { camera = null, refit = true } = {}) {
    shownShapes = shapes;
    renderGen++;
    viewer.clear();
    viewer.render(shapes, renderOptions, viewerOptions);
    if (!firstRenderMarked) {
      firstRenderMarked = true;
      _bt3dMark("bt3d-first-render");
    }
    // Every render, not just the first: a reload that reintroduced a
    // second addressing scheme would otherwise pass the check once at
    // load and go quiet exactly when it started lying.
    _checkPathInvariant(viewer, shapes);
    applyUiState();
    retintAfterRender(shapes);
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
      const gen = renderGen;
      // A level-change refetch of the scene shows no server/download/build
      // phases: only the first overlay load drives the bar.
      const prog = progressSpent ? null : progress;
      progressSpent = true;
      _setupAtomicOverlay(viewer, atomicUrl, smoothEls, shapes, prog, () => gen !== renderGen)
        .then((overlay) => {
          if (gen !== renderGen) return;
          atomicOverlay = overlay;
          if (overlay) applyAtomState();
          // After the overlay meshes are in and the legend/strain rows are
          // stamped (applyAtomState redraws); mark once, on first paint.
          if (!atomicBuiltMarked) {
            atomicBuiltMarked = true;
            requestAnimationFrame(() => _bt3dMark("bt3d-atomic-built"));
          }
          if (prog) prog.done();
        })
        .catch((err) => {
          console.error("blocktree-3d: atomic overlay failed", err);
          if (err && err.timedOut) {
            // The envelope view already drawn is replaced by the 2D view,
            // as the bar's stall text promised.
            if (prog) prog.timeout(_ATOMIC_TIMEOUT_TEXT);
            showViewerFallback(viewerEl, err, { message: _ATOMIC_TIMEOUT_TEXT });
          } else if (prog) {
            prog.hide();
          }
        });
    } else if (progress) {
      progress.done();
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
    if (progress) progress.hide();
    showViewerFallback(viewerEl, err);
    console.error("blocktree-3d: viewer init failed", err);
    return;
  }
  // A context lost after a successful start leaves a dead canvas; no
  // restore attempt, just say so and offer a reload.
  const liveCanvas = viewerEl.querySelector("canvas");
  if (liveCanvas) {
    liveCanvas.addEventListener("webglcontextlost", () => {
      showViewerFallback(viewerEl, "webgl context lost", { reload: true });
    });
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

  // ── view export (Reto, 2026-10-01) ───────────────────────────────────
  // The view as on screen, scale bar included, as a PNG or an SVG. Wired
  // once: both read the live viewer and the live scale bar.
  if (exportEls) {
    const stem = () =>
      `${exportName || "view"}-${(levelSelect && levelSelect.value) || "view"}`;
    if (exportEls.png) {
      exportEls.png.addEventListener("click", () => {
        const shot = _exportCanvas(viewer, viewerEl, { withBar: true });
        if (!shot) return;
        shot.toBlob((blob) => blob && _download(blob, `${stem()}.png`), "image/png");
      });
    }
    if (exportEls.svg) {
      exportEls.svg.addEventListener("click", () => {
        const svg = _exportSvg(viewer, viewerEl);
        if (svg) _download(new Blob([svg], { type: "image/svg+xml" }), `${stem()}.svg`);
      });
    }
  }

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
        const t = Number(smoothEls.slider.value) / 100;
        if (atomicOverlay) atomicOverlay.applyT(t);
        fadeStrainRows(t);
      });
    });
  }
  if (atomsToggle) atomsToggle.addEventListener("change", applyAtomState);
  if (smoothEls && smoothEls.targetToggle) {
    smoothEls.targetToggle.addEventListener("change", applyTargetState);
  }

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
    // The per-block chips start the same refetch, so they go dark with
    // the two controls above.
    for (const el of document.querySelectorAll(".bt3d-chip")) el.disabled = busy;
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

  // ── per-block level chip ─────────────────────────────────────────────
  //
  // `[E·I·R·z]` appended to each vendored tree row (docs/backlog/
  // se-3d-viewer-ux-batch.md, td458168). `node.level_rungs[i]` says rung i
  // renders the block differently from the next-shallower one, so a
  // lettered rung is one you can usefully click and a dash is a rung that
  // would draw the same picture. The block's own override rides in the
  // (hidden) `#bt3d-overrides` input, which stays the state carrier for
  // `loadScene`.
  const CHIP_RUNGS = [
    ["envelope", "E"],
    ["interfaces", "I"],
    ["refined", "R"],
    ["realized", "z"],
  ];

  //: The lettered rung that draws `level` for this block: the nearest
  //: lettered rung at or above it (the shallowest member of its run).
  function chipMarkRung(rungs, level) {
    let i = CHIP_RUNGS.findIndex(([name]) => name === level);
    if (i < 0) return -1;
    for (; i >= 0; i--) if (rungs[i]) return i;
    return -1;
  }

  function parseOverridesText(raw) {
    const out = {};
    for (const seg of raw.split(",")) {
      const k = seg.lastIndexOf(":");
      if (k <= 0) continue;
      const name = seg.slice(0, k).trim();
      const lvl = seg.slice(k + 1).trim();
      if (name && lvl) out[name] = lvl;
    }
    return out;
  }

  function onChipClick(node, rungIdx) {
    if (!overridesInput || reloading) return;
    const map = parseOverridesText(overridesInput.value);
    if (rungIdx === chipMarkRung(node.level_rungs, node.level_active)) {
      // The active rung: drops the block's override; a no-op without one.
      if (!(node.name in map)) return;
      delete map[node.name];
    } else {
      map[node.name] = CHIP_RUNGS[rungIdx][0];
    }
    overridesInput.value = Object.entries(map)
      .map(([n, l]) => `${n}:${l}`)
      .join(", ");
    loadScene();
  }

  function buildChip(node) {
    const active = chipMarkRung(node.level_rungs, node.level_active);
    const busy = busyEl ? !busyEl.hidden : false;
    const chip = document.createElement("span");
    chip.className = "bt3d-chip-group";
    chip.dataset.block = node.name;
    CHIP_RUNGS.forEach(([level, letter], i) => {
      if (i > 0) chip.appendChild(document.createTextNode("·"));
      if (!node.level_rungs[i]) {
        const dash = document.createElement("span");
        dash.className = "bt3d-chip-dash";
        dash.textContent = "—";
        chip.appendChild(dash);
        return;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "bt3d-chip" + (i === active ? " bt3d-chip-active" : "");
      btn.dataset.block = node.name;
      btn.dataset.rung = level;
      btn.title = `${level}: show ${node.name} at this level`;
      btn.textContent = letter;
      btn.disabled = busy;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        onChipClick(node, i);
      });
      chip.appendChild(btn);
    });
    return chip;
  }

  //: Idempotent: only rows without a chip get one, so the observer below
  //: can call it on every tree mutation (including its own insertions).
  function ensureChips() {
    const byPath = new Map();
    for (const n of data.nodes || []) {
      if (Array.isArray(n.level_rungs)) byPath.set(n.path, n);
    }
    if (!byPath.size) return;
    for (const row of viewerEl.querySelectorAll(".tv-tree-node[data-path]")) {
      const node = byPath.get(row.getAttribute("data-path"));
      const content = row.querySelector(":scope > .tv-node-content");
      if (!node || !content || content.querySelector(".bt3d-chip-group")) continue;
      content.appendChild(buildChip(node));
    }
  }

  new MutationObserver(ensureChips).observe(viewerEl, { childList: true, subtree: true });
  ensureChips();

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
