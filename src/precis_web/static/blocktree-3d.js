// blocktree-3d.js — the se/nm 3D reader's client glue (round 2a,
// gr335242 comment 5 / docs/backlog/multiscale-design-system-spec.md
// §5.8). Server side builds the whole bundle
// (precis_web.blocktree_3d.build_scene, served as scene3d.json); this
// file only wires the vendored three-cad-viewer to it and links the
// mermaid topology panel — no geometry/traversal logic lives here.
//
// Vertex/edge/face/solid picking, three-state visibility, and the
// hierarchical assembly tree are all native to the vendored viewer —
// nothing to wire for those.
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

export async function blocktreeViewer3D({
  viewerEl,
  mermaidEl,
  explodeButton,
  sceneUrl,
}) {
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
        window.mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "default",
        });
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
  const displayOptions = {
    cadWidth: viewerEl.clientWidth || 600,
    height: viewerEl.clientHeight || 500,
    treeWidth: 220,
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

  try {
    const display = new Display(viewerEl, displayOptions);
    viewer = new Viewer(display, viewerOptions, notify);
    viewer.render(data.shapes, renderOptions, viewerOptions);
  } catch (err) {
    showError(viewerEl, "3D viewer failed to start: " + String(err));
    console.error("blocktree-3d: viewer init failed", err);
    return;
  }

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
}
