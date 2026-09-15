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

// A mermaid node id is `B<block_id>` (blocktree_3d.mermaid_topology) —
// the SAME id every leaf/group path ends in, so resolving it back to a
// 3D path needs no lookup table either, just a reverse tree walk.
// Prefers a GROUP match (a block with rendered children) over a bare
// leaf, since that's the "primary path" connectivity/explode key on
// the server side.
function findPathEndingInId(root, blockId) {
  let leafMatch = null;
  let groupMatch = null;
  walkShapes(root, (n) => {
    const segs = n.id.split("/");
    if (segs[segs.length - 1] === String(blockId)) {
      if (n.parts) groupMatch = n.id;
      else if (!leafMatch) leafMatch = n.id;
    }
  });
  return groupMatch || leafMatch;
}

// A block with both its own geometry and visible children doubles its
// last path segment (blocktree_3d's own module docstring: `.../id/id`)
// — normalize a raw pick back to the primary (group) path so it
// matches the connectivity metadata's own a_path/b_path keys.
function primaryPathOf(fullPath) {
  const segs = fullPath.split("/");
  if (segs.length >= 2 && segs[segs.length - 1] === segs[segs.length - 2]) {
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

export async function blocktreeViewer3D({
  viewerEl,
  mermaidEl,
  explodeButton,
  connectionsToggle,
  sceneUrl,
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

  // ── mermaid topology panel ──────────────────────────────────────────
  if (mermaidEl) {
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
  function highlightMermaidNode(blockId) {
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

  function recolour(path, colour) {
    const part = findPart(data.shapes, path);
    if (!part || !viewer) return;
    try {
      viewer.updatePart(path, { ...part, color: colour }, { skipBounds: true });
    } catch (err) {
      // Best-effort: a recolour glitch must never break picking/explode.
      console.error("blocktree-3d: updatePart failed for", path, err);
    }
  }

  function clearHighlight() {
    for (const [path, colour] of highlighted) recolour(path, colour);
    highlighted.clear();
    if (viewer) {
      try {
        viewer.updateBounds();
      } catch {
        // non-fatal — see recolour's own try/catch
      }
    }
  }

  function selectPath(primaryPath) {
    clearHighlight();
    for (const c of connectionsTouching(data.connections, primaryPath)) {
      const other = c.a_path === primaryPath ? c.b_path : c.a_path;
      for (const p of [c.path, other]) {
        const part = findPart(data.shapes, p);
        if (!part || highlighted.has(p)) continue;
        highlighted.set(p, part.color);
        recolour(p, HIGHLIGHT_COLOUR);
      }
    }
    const blockId = primaryPath.split("/").pop();
    highlightMermaidNode(blockId);
  }

  function notify(change) {
    const pick = change && change.lastPick && change.lastPick.new;
    if (!pick) return;
    selectPath(primaryPathOf(`${pick.path}/${pick.name}`));
  }

  const treeHeightCapEl = document.createElement("style");
  document.head.appendChild(treeHeightCapEl);
  _applyTreeHeightCap(treeHeightCapEl, initialTreeHeight, _INFO_PANEL_HEIGHT);

  try {
    const display = new Display(viewerEl, displayOptions);
    viewer = new Viewer(display, viewerOptions, notify);
    viewer.render(data.shapes, renderOptions, viewerOptions);
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
      const path = findPathEndingInId(data.shapes, m[1]);
      if (path) selectPath(path);
    });
  }

  // ── explode along drawn attachment directions ───────────────────────
  // Deliberately NOT the vendored viewer's own `setExplode()` (radial
  // from the whole model's bbox centre) — `data.explode` is computed
  // server-side from the SAME drawn connectivity this reader shows
  // (blocktree_3d.explode_offsets's own docstring).
  let exploded = false;
  if (explodeButton) {
    explodeButton.addEventListener("click", () => {
      if (!viewer) return;
      try {
        if (!exploded) {
          for (const [path, offset] of Object.entries(data.explode || {})) {
            viewer.addPositionTrack(path, [0, EXPLODE_DURATION], [
              [0, 0, 0],
              offset,
            ]);
          }
          viewer.initAnimation(EXPLODE_DURATION, 1, "E", false);
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
  if (connectionsToggle) {
    connectionsToggle.addEventListener("change", () => {
      if (!viewer) return;
      const edgeState = connectionsToggle.checked ? 1 : 0;
      for (const c of data.connections || []) {
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

  // ── scale bar overlay (gr340030) ─────────────────────────────────────
  _startScaleBar(viewer, viewerEl, data.scale);
}
