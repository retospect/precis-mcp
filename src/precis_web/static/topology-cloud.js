// topology-cloud.js — the se reader's topology panel: a force-directed,
// draggable node cloud that replaces the `graph LR` mermaid diagram
// (docs/backlog/se-topology-cloud-and-surface-notes.md slice 1; Reto
// 2026-09-14: "the graph LR is confusing, can it be a cloud that
// minimizes crossing links and is draggable, with mouseover details
// (forces etc)?").
//
// Why a force layout at all: mermaid's `graph LR` is a LAYERED renderer —
// it assigns ranks and routes edges between them, which for a structural
// design (where the interesting connects are lateral ties between blocks
// at the same depth) produces long edges crossing several ranks. A spring/
// repulsion equilibrium has no ranks to cross: connected blocks sit close,
// unconnected ones drift apart, and crossings fall out of the energy
// minimum rather than being routed around.
//
// TWO exports, split on testability (the gap gr338976 exposed: this reader
// had no client-JS coverage at all):
//   * `layoutTopology` — PURE: nodes/edges in, positions out. No DOM, no
//     randomness (seeded deterministically from the node order), so the
//     layout itself is testable headlessly under plain `node`.
//   * `renderTopologyCloud` — the DOM half: SVG build, drag, hover,
//     selection. Exercised by the same smoke test through a DOM stub.
//
// No d3, no CDN: the house is offline-first and the whole simulation is
// the ~60 lines below. Vendoring d3-force remains the fallback if this
// ever needs more (spec slice 1), but it does not yet.

const NODE_R = 6;
const BOX_R = 8; // a collapsed parent draws bigger — it stands for a subtree
const HULL_PAD = 18;
const LABEL_DY = -11;
const BADGE_R = 3;

const COL_NODE = "#64748b";
const COL_NODE_BOX = "#94a3b8";
const COL_LABEL = "#334155";
const COL_HULL = "#e2e8f0";
const COL_HIGHLIGHT = "#f59e0b";
// An OPENED parent (defect 1, docs/backlog/se-topology-cloud-and-surface-
// notes.md follow-up) draws no circle of its own — its name is the hull's
// own label instead, styled as a container label rather than a node
// label: italic, a touch bolder, and a step darker than COL_LABEL so it
// still reads as "this IS the group", not "here is one more node".
const COL_HULL_LABEL = "#475569";
// Containment (defect 2): a parent->child link is drawn now, not left as
// an invisible force — faint/thin/dashed so it stays visually subordinate
// to a real connect (solid, coloured, hoverable).
const COL_CONTAINMENT = "#cbd5e1";
// Per-block validator finding badges (defect 3): amber for a warn-tier
// finding, red for an error-tier one — worst severity wins when a block
// carries both. Distinct constants from COL_HIGHLIGHT on purpose (a
// badge and the amber selection ring are different signals that can
// legitimately coincide on one node).
const COL_BADGE_WARN = "#f59e0b";
const COL_BADGE_ERROR = "#dc2626";

// ── the simulation (pure) ──────────────────────────────────────────────

// Deterministic unit-circle seeding: a golden-angle spiral, which spreads
// the initial positions evenly instead of clumping them the way a naive
// `i / n` ring does for small n. No Math.random anywhere — the same design
// always lays out the same way, which is what makes the layout testable
// and what stops the panel from reshuffling itself on every re-render.
function seedPositions(nodes, width, height) {
  const cx = width / 2;
  const cy = height / 2;
  const r0 = Math.min(width, height) * 0.32;
  return nodes.map((n, i) => {
    const a = i * 2.399963229728653; // golden angle, radians
    const rad = r0 * Math.sqrt((i + 0.5) / Math.max(1, nodes.length));
    return {
      id: n.id,
      x: cx + rad * Math.cos(a),
      y: cy + rad * Math.sin(a),
      vx: 0,
      vy: 0,
      pinned: false,
    };
  });
}

/**
 * Run the spring/repulsion relaxation to equilibrium and return a
 * `{id: {x, y}}` map. Pure — same inputs, same output, every time.
 *
 * Three forces, all O(n²) per tick (a design with hundreds of visible
 * blocks is not a thing this reader shows — the abstraction ladder exists
 * precisely so it doesn't):
 *   * repulsion between every pair, so nodes don't pile up;
 *   * a spring along every drawn connect, pulling linked blocks together —
 *     this is what turns "few crossings" into an energy minimum;
 *   * a weaker spring along every parent→child link, so a subtree stays a
 *     visual cluster (what mermaid's subgraph box said structurally).
 * Plus a gentle pull to centre so a disconnected component can't drift off
 * the canvas.
 *
 * `alpha` decays geometrically: large early steps find the basin, small
 * late ones settle without jitter.
 */
export function layoutTopology({
  nodes,
  edges = [],
  width = 600,
  height = 520,
  ticks = 300,
  positions = null,
}) {
  const pts = positions || seedPositions(nodes, width, height);
  const byId = new Map(pts.map((p) => [p.id, p]));
  const springs = [];
  for (const e of edges) {
    const a = byId.get(e.a);
    const b = byId.get(e.b);
    if (a && b && a !== b) springs.push([a, b, 1.0]);
  }
  for (const n of nodes) {
    const a = byId.get(n.id);
    const b = n.parent ? byId.get(n.parent) : null;
    if (a && b && a !== b) springs.push([a, b, 0.45]);
  }
  const ideal = Math.min(width, height) / Math.max(2, Math.sqrt(pts.length) + 1);
  const repel = ideal * ideal * 0.55;
  const cx = width / 2;
  const cy = height / 2;

  let alpha = 1.0;
  for (let t = 0; t < ticks; t++) {
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const p = pts[i];
        const q = pts[j];
        let dx = q.x - p.x;
        let dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1e-6) {
          // Exactly-coincident pair (possible only from caller-supplied
          // positions): nudge deterministically by index rather than
          // dividing by zero.
          dx = (i % 2 ? 1 : -1) * 0.5;
          dy = (j % 2 ? 1 : -1) * 0.5;
          d2 = dx * dx + dy * dy;
        }
        const d = Math.sqrt(d2);
        const f = repel / d2;
        const ux = (dx / d) * f;
        const uy = (dy / d) * f;
        p.vx -= ux;
        p.vy -= uy;
        q.vx += ux;
        q.vy += uy;
      }
    }
    for (const [a, b, k] of springs) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1e-3;
      const f = (d - ideal) * 0.08 * k;
      const ux = (dx / d) * f;
      const uy = (dy / d) * f;
      a.vx += ux;
      a.vy += uy;
      b.vx -= ux;
      b.vy -= uy;
    }
    for (const p of pts) {
      p.vx += (cx - p.x) * 0.006;
      p.vy += (cy - p.y) * 0.006;
      if (p.pinned) {
        p.vx = 0;
        p.vy = 0;
        continue;
      }
      p.x += p.vx * alpha;
      p.y += p.vy * alpha;
      p.vx *= 0.82;
      p.vy *= 0.82;
      // Keep every node on the canvas: the panel has no pan/zoom, so a
      // node outside the viewBox is simply unreachable.
      p.x = Math.max(NODE_R + 2, Math.min(width - NODE_R - 2, p.x));
      p.y = Math.max(NODE_R + 2, Math.min(height - NODE_R - 2, p.y));
    }
    alpha *= 0.985;
  }
  return pts;
}

// Monotone-chain convex hull of `[{x, y}]`, counter-clockwise. Used to
// draw an opened parent as a soft shape BEHIND its children — the cloud's
// answer to mermaid's subgraph box.
export function convexHull(points) {
  if (points.length < 3) return points.slice();
  const pts = points.slice().sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o, a, b) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lower = [];
  for (const p of pts) {
    while (
      lower.length >= 2 &&
      cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0
    ) {
      lower.pop();
    }
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (
      upper.length >= 2 &&
      cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0
    ) {
      upper.pop();
    }
    upper.push(p);
  }
  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

// Push a hull outward from its centroid so it clears the node glyphs it
// wraps. A 1- or 2-node "hull" has no area to expand, so those get a
// square/capsule of padding around the points instead.
function padHull(hull, pad) {
  if (hull.length === 0) return [];
  if (hull.length < 3) {
    const out = [];
    for (const p of hull) {
      out.push({ x: p.x - pad, y: p.y - pad });
      out.push({ x: p.x + pad, y: p.y - pad });
      out.push({ x: p.x + pad, y: p.y + pad });
      out.push({ x: p.x - pad, y: p.y + pad });
    }
    return convexHull(out);
  }
  let sx = 0;
  let sy = 0;
  for (const p of hull) {
    sx += p.x;
    sy += p.y;
  }
  const cx = sx / hull.length;
  const cy = sy / hull.length;
  return hull.map((p) => {
    const dx = p.x - cx;
    const dy = p.y - cy;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    return { x: p.x + (dx / d) * pad, y: p.y + (dy / d) * pad };
  });
}

// ── hover text (pure, so the wording is testable) ──────────────────────

/** The hover lines for one block. `detail` is whatever the tree declared;
 * nothing is synthesized to fill the box out. `findings` (defect 3) are
 * the block's own validator findings — `{severity, rule, detail}` rows
 * from the server's per-block bucketing — appended severity-prefixed, so
 * a hover on a badged node says exactly what the badge is warning about
 * rather than just that something's wrong. */
export function nodeTooltipLines(node, degree, findings = []) {
  const lines = [node.name];
  if (node.kind === "box") lines.push("collapsed — stands for its subtree");
  for (const d of node.detail || []) lines.push(d);
  lines.push(`${degree} connect${degree === 1 ? "" : "s"}`);
  for (const f of findings) lines.push(`${f.severity}: ${f.rule} — ${f.detail}`);
  return lines;
}

// The worse of two severities wins the badge colour: an error anywhere
// outranks a warn, a warn anywhere outranks showing nothing. `null` means
// nothing badge-worthy (no findings, or info-only — info is still listed
// in the hover text above, just not enough to earn a coloured dot).
function worstSeverity(findings) {
  let worst = null;
  for (const f of findings) {
    if (f.severity === "error") return "error";
    if (f.severity === "warn") worst = "warn";
  }
  return worst;
}

/**
 * The hover lines for one connect. Honesty rule from the spec: member
 * forces appear ONLY when a stability solve produced them — with no solve
 * the tooltip says role and gap and stops, rather than showing a zero that
 * would read as "no force".
 */
export function edgeTooltipLines(edge, facts) {
  const lines = [edge.label];
  const f = facts || null;
  if (f && f.role) lines.push(`role: ${f.role}`);
  if (f && f.skipped) {
    lines.push(`not analysed: ${f.skipped}`);
  } else if (f) {
    if (typeof f.declared_n === "number") {
      lines.push(`declared preload: ${f.declared_n.toPrecision(3)} N`);
    }
    if (typeof f.implied_n === "number") {
      lines.push(`implied force: ${f.implied_n.toPrecision(3)} N`);
    }
    if (typeof f.self_stress === "number") {
      lines.push(
        `self-stress coefficient: ${f.self_stress.toFixed(2)} ` +
          "(normalized, not newtons)"
      );
    }
  }
  if (!f) lines.push("no stability solve — no member force to report");
  if (typeof edge.witness_gap === "number" && edge.witness_gap > 0) {
    lines.push(`gap: ${edge.witness_gap.toPrecision(3)}`);
  }
  return lines;
}

// ── the DOM half ───────────────────────────────────────────────────────

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(doc, tag, attrs) {
  const el = doc.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  return el;
}

/**
 * Build the cloud into `container` and return a small handle:
 *   `{ highlight(blockId), destroy() }`
 * `onSelect(blockId)` fires when a node is clicked — the caller drives the
 * 3D selection with it (the linked-selection contract the mermaid panel
 * had, unchanged).
 */
export function renderTopologyCloud({
  container,
  nodes,
  connections = [],
  forces = {},
  findings = {},
  onSelect = null,
  width = 0,
  height = 0,
  doc = null,
}) {
  const d = doc || (container && container.ownerDocument) || globalThis.document;
  const W = width || container.clientWidth || 600;
  const H = height || container.clientHeight || 520;

  const byName = new Map(nodes.map((n) => [n.name, n]));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  // `findings` (defect 3) comes from the server keyed by block NAME
  // (the same key TopoNode.name carries); re-key by id so the render
  // loops below — which all work in id space — can look a node's
  // findings up the same way they look up its position.
  const findingsById = new Map(
    nodes.map((n) => [n.id, findings[n.name] || []])
  );
  const edges = [];
  for (const c of connections) {
    const a = byName.get(c.a_name);
    const b = byName.get(c.b_name);
    if (!a || !b || a.id === b.id) continue;
    edges.push({
      a: a.id,
      b: b.id,
      label: c.label,
      colour: c.colour,
      subject: c.subject || "",
      witness_gap: c.witness_gap,
    });
  }
  const degree = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of edges) {
    degree.set(e.a, (degree.get(e.a) || 0) + 1);
    degree.set(e.b, (degree.get(e.b) || 0) + 1);
  }

  const pts = layoutTopology({ nodes, edges, width: W, height: H });
  const posById = new Map(pts.map((p) => [p.id, p]));

  const svg = svgEl(d, "svg", {
    width: "100%",
    height: "100%",
    viewBox: `0 0 ${W} ${H}`,
  });
  const hullG = svgEl(d, "g", {});
  // Containment (defect 2) draws behind connects but in front of hulls —
  // subordinate to a real connect, but still visible over the hull fill.
  const containG = svgEl(d, "g", {});
  const edgeG = svgEl(d, "g", {});
  const nodeG = svgEl(d, "g", {});
  svg.appendChild(hullG);
  svg.appendChild(containG);
  svg.appendChild(edgeG);
  svg.appendChild(nodeG);

  const tip = d.createElement("div");
  tip.className =
    "absolute z-10 hidden rounded border border-slate-300 bg-white px-2 py-1 " +
    "text-[11px] text-slate-700 shadow pointer-events-none whitespace-pre-line";
  container.replaceChildren(svg, tip);

  // Position from the CONTAINER's own box, not the event's offsetX/offsetY:
  // for an SVG child element those are measured against the target's own
  // bounding box (and differ between engines), which puts the tooltip in
  // the wrong place — the container is the element the tooltip is
  // absolutely positioned within, so measure against that.
  function showTip(lines, ev) {
    // textContent, never innerHTML — block names, descriptions and joint
    // labels are design-supplied text (same untrusted-content posture as
    // blocktree-3d.js's showError).
    tip.textContent = lines.join("\n");
    const rect = container.getBoundingClientRect
      ? container.getBoundingClientRect()
      : { left: 0, top: 0 };
    tip.style.left = `${(ev.clientX || 0) - rect.left + 12}px`;
    tip.style.top = `${(ev.clientY || 0) - rect.top + 12}px`;
    tip.classList.remove("hidden");
  }
  function hideTip() {
    tip.classList.add("hidden");
  }

  // hulls: one per parent that has at least one shown child. An OPENED
  // parent (kind !== "box" — a COLLAPSED parent stands in for its whole
  // subtree and keeps its own circle, defect 1) draws no free-floating
  // circle of its own: the hull IS its representation, carrying its name
  // as a container label and its click/hover/badge in place of a node's.
  const childrenOf = new Map();
  for (const n of nodes) {
    if (!n.parent) continue;
    if (!childrenOf.has(n.parent)) childrenOf.set(n.parent, []);
    childrenOf.get(n.parent).push(n.id);
  }
  const openedParentIds = new Set(
    [...childrenOf.keys()].filter((id) => {
      const p = byId.get(id);
      return !!p && p.kind !== "box";
    })
  );
  const hullShapes = [];
  for (const [parentId, kidIds] of childrenOf) {
    const members = [parentId, ...kidIds]
      .map((id) => posById.get(id))
      .filter(Boolean);
    if (members.length < 2) continue;
    const poly = svgEl(d, "polygon", {
      fill: COL_HULL,
      "fill-opacity": "0.55",
      stroke: COL_HULL,
      "stroke-width": "1",
    });
    hullG.appendChild(poly);
    const shape = { poly, ids: [parentId, ...kidIds], parentId, label: null, badge: null };
    const parentNode = byId.get(parentId);
    if (openedParentIds.has(parentId) && parentNode) {
      const label = svgEl(d, "text", {
        "text-anchor": "middle",
        "font-size": "10",
        "font-style": "italic",
        "font-weight": "600",
        fill: COL_HULL_LABEL,
      });
      label.textContent = parentNode.name;
      hullG.appendChild(label);
      shape.label = label;
      poly.setAttribute("cursor", "pointer");
      const nodeFindings = findingsById.get(parentId) || [];
      poly.addEventListener("mousemove", (ev) =>
        showTip(
          nodeTooltipLines(parentNode, degree.get(parentId) || 0, nodeFindings),
          ev
        )
      );
      poly.addEventListener("mouseleave", hideTip);
      poly.addEventListener("click", () => {
        if (onSelect) onSelect(parentId);
      });
      const worst = worstSeverity(nodeFindings);
      if (worst) {
        const badge = svgEl(d, "circle", {
          r: BADGE_R,
          fill: worst === "error" ? COL_BADGE_ERROR : COL_BADGE_WARN,
          stroke: "#ffffff",
          "stroke-width": "1",
        });
        hullG.appendChild(badge);
        shape.badge = badge;
      }
    }
    hullShapes.push(shape);
  }

  // containment (defect 2): every parent->child link drawn as a faint,
  // thin, dashed, UNHOVERABLE edge — visually subordinate to a real
  // connect. `pointer-events: none` and no mousemove/mouseleave handler
  // is what keeps it out of hover/connect-detail entirely, per spec.
  const containShapes = [];
  for (const n of nodes) {
    if (!n.parent) continue;
    const line = svgEl(d, "line", {
      stroke: COL_CONTAINMENT,
      "stroke-width": "1",
      "stroke-dasharray": "3,2",
      "stroke-opacity": "0.5",
      "pointer-events": "none",
    });
    containG.appendChild(line);
    containShapes.push({ line, a: n.id, b: n.parent });
  }

  const edgeShapes = edges.map((e) => {
    const line = svgEl(d, "line", {
      stroke: e.colour || COL_NODE,
      "stroke-width": "2",
      "stroke-opacity": "0.8",
    });
    line.addEventListener("mousemove", (ev) =>
      showTip(edgeTooltipLines(e, forces[e.subject]), ev)
    );
    line.addEventListener("mouseleave", hideTip);
    edgeG.appendChild(line);
    return { line, edge: e };
  });

  // A node that IS an opened parent draws no circle/label of its own —
  // the hull built above already carries its label, click and badge
  // (defect 1). It still went through layoutTopology above (posById has
  // it), so it stays a force-layout participant — the weak parent-child
  // spring still clusters its children around it — just an invisible one.
  const nodeShapes = nodes
    .filter((n) => !openedParentIds.has(n.id))
    .map((n) => {
      const g = svgEl(d, "g", { cursor: "grab" });
      const r = n.kind === "box" ? BOX_R : NODE_R;
      const circle = svgEl(d, "circle", {
        r,
        fill: n.kind === "box" ? COL_NODE_BOX : COL_NODE,
        stroke: "#ffffff",
        "stroke-width": "1.5",
      });
      const label = svgEl(d, "text", {
        "text-anchor": "middle",
        "font-size": "10",
        fill: COL_LABEL,
        dy: LABEL_DY,
      });
      label.textContent = n.name;
      g.appendChild(circle);
      g.appendChild(label);
      const nodeFindings = findingsById.get(n.id) || [];
      const worst = worstSeverity(nodeFindings);
      if (worst) {
        g.appendChild(
          svgEl(d, "circle", {
            r: BADGE_R,
            cx: r + BADGE_R,
            cy: -r - 1,
            fill: worst === "error" ? COL_BADGE_ERROR : COL_BADGE_WARN,
            stroke: "#ffffff",
            "stroke-width": "1",
          })
        );
      }
      nodeG.appendChild(g);
      const shape = { g, circle, label, node: n };

      g.addEventListener("mousemove", (ev) =>
        showTip(nodeTooltipLines(n, degree.get(n.id) || 0, nodeFindings), ev)
      );
      g.addEventListener("mouseleave", hideTip);
      g.addEventListener("click", () => {
        if (onSelect) onSelect(n.id);
      });
      // Drag pins: a layout the user arranged by hand must survive the
      // relaxation that follows, or dragging is pointless. Double-click
      // releases the pin back to the simulation.
      g.addEventListener("pointerdown", (ev) => {
        const p = posById.get(n.id);
        if (!p) return;
        dragging = { p, shape };
        p.pinned = true;
        if (ev.target && ev.target.setPointerCapture && ev.pointerId != null) {
          try {
            ev.target.setPointerCapture(ev.pointerId);
          } catch {
            // Best-effort: capture only improves drag-outside-the-node.
          }
        }
      });
      g.addEventListener("dblclick", () => {
        const p = posById.get(n.id);
        if (!p) return;
        p.pinned = false;
        relax();
      });
      return shape;
    });

  let dragging = null;

  function paint() {
    for (const { poly, ids, label, badge } of hullShapes) {
      const members = ids.map((id) => posById.get(id)).filter(Boolean);
      const hull = padHull(convexHull(members), HULL_PAD);
      poly.setAttribute("points", hull.map((p) => `${p.x},${p.y}`).join(" "));
      if (!hull.length) continue;
      // The container label sits at the hull's own top point (min y) — on
      // its boundary, not floating free inside it (defect 1).
      const top = hull.reduce((a, b) => (b.y < a.y ? b : a));
      if (label) {
        label.setAttribute("x", String(top.x));
        label.setAttribute("y", String(top.y - 4));
      }
      if (badge) {
        badge.setAttribute("cx", String(top.x + 16));
        badge.setAttribute("cy", String(top.y - 7));
      }
    }
    for (const { line, a, b } of containShapes) {
      const pa = posById.get(a);
      const pb = posById.get(b);
      if (!pa || !pb) continue;
      line.setAttribute("x1", String(pa.x));
      line.setAttribute("y1", String(pa.y));
      line.setAttribute("x2", String(pb.x));
      line.setAttribute("y2", String(pb.y));
    }
    for (const { line, edge } of edgeShapes) {
      const a = posById.get(edge.a);
      const b = posById.get(edge.b);
      if (!a || !b) continue;
      line.setAttribute("x1", String(a.x));
      line.setAttribute("y1", String(a.y));
      line.setAttribute("x2", String(b.x));
      line.setAttribute("y2", String(b.y));
    }
    for (const { g, node } of nodeShapes) {
      const p = posById.get(node.id);
      if (!p) continue;
      g.setAttribute("transform", `translate(${p.x},${p.y})`);
    }
  }

  // Re-settle after a pin/unpin, from the CURRENT positions (not a fresh
  // seeding) so the picture nudges instead of jumping.
  function relax(ticks = 60) {
    layoutTopology({ nodes, edges, width: W, height: H, ticks, positions: pts });
    paint();
  }

  function onMove(ev) {
    if (!dragging) return;
    const rect = svg.getBoundingClientRect
      ? svg.getBoundingClientRect()
      : { left: 0, top: 0, width: W, height: H };
    const sx = rect.width ? W / rect.width : 1;
    const sy = rect.height ? H / rect.height : 1;
    dragging.p.x = (ev.clientX - rect.left) * sx;
    dragging.p.y = (ev.clientY - rect.top) * sy;
    paint();
  }
  function onUp() {
    if (!dragging) return;
    dragging = null;
    relax(40);
  }
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointerleave", onUp);

  // Highlight resets are `reset()` closures rather than a hardcoded
  // circle-repaint: an opened parent has no circle to repaint (defect 1)
  // — it highlights via its hull's stroke instead — so the two cases
  // need different undo logic, not just different targets.
  let highlighted = [];
  function highlight(blockId) {
    for (const h of highlighted) h.reset();
    highlighted = [];
    const hit = nodeShapes.find((s) => s.node.id === blockId);
    if (hit) {
      hit.circle.setAttribute("fill", COL_HIGHLIGHT);
      hit.circle.setAttribute("stroke", COL_HIGHLIGHT);
      highlighted.push({
        reset: () => {
          hit.circle.setAttribute(
            "fill",
            hit.node.kind === "box" ? COL_NODE_BOX : COL_NODE
          );
          hit.circle.setAttribute("stroke", "#ffffff");
        },
      });
      return;
    }
    const hullHit = hullShapes.find((h) => h.parentId === blockId);
    if (!hullHit) return;
    hullHit.poly.setAttribute("stroke", COL_HIGHLIGHT);
    hullHit.poly.setAttribute("stroke-width", "2.5");
    highlighted.push({
      reset: () => {
        hullHit.poly.setAttribute("stroke", COL_HULL);
        hullHit.poly.setAttribute("stroke-width", "1");
      },
    });
  }

  paint();
  return {
    highlight,
    svg,
    positions: posById,
    destroy() {
      svg.removeEventListener("pointermove", onMove);
      svg.removeEventListener("pointerup", onUp);
      svg.removeEventListener("pointerleave", onUp);
    },
  };
}
