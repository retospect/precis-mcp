// se flow player — the three.js half of the Flow tab
// (docs/backlog/se-flow-kernel.md slice 2b; route routes/se_flow.py).
// Particles are one InstancedMesh of spheres whose positions interpolate
// between the payload's recorded frames; one directional light casts the
// particles' shadows onto the chamber floor (holes and pockets); walls
// and lid are translucent. Overlays: ports, the tracking plane with its
// crossings, short fading trails. The payload maths lives in
// se-flow-frames.js (node-tested); this file only draws.

import * as THREE from "three";
import {
  clockText,
  colormap,
  colourValues,
  crossedBy,
  decodePayload,
  frameAt,
  positionsAt,
  residenceRange,
  sceneY,
  speedRange,
} from "/static/se-flow-frames.js";

const TRAIL_FRAMES = 6;
const FLOOR_COLOUR = new THREE.Color(0xd9dde3);
const DEEP_COLOUR = new THREE.Color(0x8a94a3);

// ── chamber geometry ─────────────────────────────────────────────────

function quad(pos, col, a, b, c, d, colour) {
  pos.push(...a, ...b, ...c, ...a, ...c, ...d);
  for (let i = 0; i < 6; i++) col.push(colour.r, colour.g, colour.b);
}

function geometryOf(pos, col) {
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  if (col) g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  g.computeVertexNormals();
  return g;
}

/** Floor (cell tops at their depth + hole side walls, opaque, receives
 * shadows), outer walls and lid (translucent), and the per-cell floor
 * heights the overlays sit on. */
function chamberMeshes(d, v) {
  const { nx, ny, dx, mask, depth } = d;
  const floorY = (c) => -depth[c] * v;
  const inside = (i, j) => i >= 0 && j >= 0 && i < nx && j < ny && mask[j * nx + i];
  let maxDepth = 0;
  for (let c = 0; c < nx * ny; c++) if (mask[c] && depth[c] > maxDepth) maxDepth = depth[c];
  const fp = [];
  const fc = [];
  const wp = [];
  const lp = [];
  const shade = new THREE.Color();
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const c = j * nx + i;
      if (!mask[c]) continue;
      const x0 = i * dx;
      const x1 = x0 + dx;
      const z0 = j * dx;
      const z1 = z0 + dx;
      const y = floorY(c);
      shade.copy(FLOOR_COLOUR).lerp(DEEP_COLOUR, maxDepth > 0 ? depth[c] / maxDepth : 0);
      quad(fp, fc, [x0, y, z0], [x0, y, z1], [x1, y, z1], [x1, y, z0], shade);
      lp.push(x0, v, z0, x1, v, z0, x1, v, z1, x0, v, z0, x1, v, z1, x0, v, z1);
      // the four edges: a step down to a deeper neighbour is that
      // neighbour's business; a step up (or the outside) is drawn here.
      const edges = [
        [i - 1, j, [x0, z1], [x0, z0]],
        [i + 1, j, [x1, z0], [x1, z1]],
        [i, j - 1, [x0, z0], [x1, z0]],
        [i, j + 1, [x1, z1], [x0, z1]],
      ];
      for (const [ni, nj, [ax, az], [bx, bz]] of edges) {
        const out = !inside(ni, nj);
        const top = out ? 0 : floorY(nj * nx + ni);
        if (top > y) {
          quad(fp, fc, [ax, y, az], [bx, y, bz], [bx, top, bz], [ax, top, az], DEEP_COLOUR);
        }
        if (out) {
          wp.push(ax, 0, az, bx, 0, bz, bx, v, bz, ax, 0, az, bx, v, bz, ax, v, az);
        }
      }
    }
  }
  const floor = new THREE.Mesh(
    geometryOf(fp, fc),
    new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.9, side: THREE.DoubleSide }),
  );
  floor.receiveShadow = true;
  const glass = (opacity) =>
    new THREE.MeshStandardMaterial({
      color: 0x9ec5e8,
      transparent: true,
      opacity,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
  const walls = new THREE.Mesh(geometryOf(wp), glass(0.22));
  const lid = new THREE.Mesh(geometryOf(lp), glass(0.04));
  return { floor, walls, lid };
}

function portMesh(d, cells, v, colour) {
  const pos = [];
  const { nx, ny, dx } = d;
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const c = j * nx + i;
      if (!cells[c]) continue;
      const x0 = i * dx;
      const x1 = x0 + dx;
      const z0 = j * dx;
      const z1 = z0 + dx;
      const y0 = -d.depth[c] * v;
      // a column through the gap: the port cells' four sides
      for (const [ax, az, bx, bz] of [
        [x0, z0, x1, z0],
        [x1, z0, x1, z1],
        [x1, z1, x0, z1],
        [x0, z1, x0, z0],
      ]) {
        pos.push(ax, y0, az, bx, y0, bz, bx, v, bz, ax, y0, az, bx, v, bz, ax, v, az);
      }
    }
  }
  return new THREE.Mesh(
    geometryOf(pos),
    new THREE.MeshBasicMaterial({
      color: colour,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
}

function planeMesh(d, v) {
  const pos = [];
  const { nx, ny, dx, mask, depth } = d;
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const c = j * nx + i;
      if (!mask[c]) continue;
      const h = 1 + depth[c];
      const y = (d.zPlane * h - (h - 1)) * v;
      const x0 = i * dx;
      const z0 = j * dx;
      pos.push(x0, y, z0, x0 + dx, y, z0, x0 + dx, y, z0 + dx, x0, y, z0, x0 + dx, y, z0 + dx, x0, y, z0 + dx);
    }
  }
  return new THREE.Mesh(
    geometryOf(pos),
    new THREE.MeshBasicMaterial({
      color: 0xf59e0b,
      transparent: true,
      opacity: 0.06,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
}

/** Crossing marks on the tracking plane, sorted by first-crossing time
 * so the draw range at t* is the prefix that has crossed. */
function crossingMarks(d, v, radius) {
  const order = [];
  for (let p = 0; p < d.n; p++) if (d.broke[p] && Number.isFinite(d.residence[p])) order.push(p);
  order.sort((a, b) => d.residence[a] - d.residence[b]);
  const geom = new THREE.CircleGeometry(radius, 12);
  geom.rotateX(-Math.PI / 2);
  const mesh = new THREE.InstancedMesh(
    geom,
    new THREE.MeshBasicMaterial({ color: 0xd97706, side: THREE.DoubleSide }),
    Math.max(1, order.length),
  );
  const m = new THREE.Matrix4();
  order.forEach((p, idx) => {
    const x = d.cross[2 * p];
    const z = d.cross[2 * p + 1];
    m.makeTranslation(x, sceneY(d, x, z, d.zPlane, v) + 1e-4, z);
    mesh.setMatrixAt(idx, m);
  });
  mesh.count = 0;
  return { mesh, times: order.map((p) => d.residence[p]) };
}

// ── a small orbit camera (drag rotate, shift/right-drag pan, wheel zoom) ─

function orbit(camera, el, target, onChange) {
  const s = new THREE.Spherical().setFromVector3(camera.position.clone().sub(target));
  let drag = null;
  const apply = () => {
    s.phi = Math.min(Math.PI / 2 - 0.02, Math.max(0.05, s.phi));
    camera.position.copy(target).add(new THREE.Vector3().setFromSpherical(s));
    camera.lookAt(target);
    onChange();
  };
  el.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 2 };
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener("pointerup", () => (drag = null));
  el.addEventListener("contextmenu", (e) => e.preventDefault());
  el.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dxp = e.clientX - drag.x;
    const dyp = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (drag.pan) {
      const k = s.radius / el.clientHeight;
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const fwd = new THREE.Vector3().crossVectors(camera.up, right).normalize();
      target.addScaledVector(right, -dxp * k).addScaledVector(fwd, dyp * k);
    } else {
      s.theta -= dxp * 0.006;
      s.phi -= dyp * 0.006;
    }
    apply();
  });
  el.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      s.radius = Math.min(10, Math.max(0.05, s.radius * Math.exp(e.deltaY * 0.001)));
      apply();
    },
    { passive: false },
  );
  apply();
}

// ── the player ───────────────────────────────────────────────────────

/**
 * flowPlayer({viewerEl, statusEl, cardEl, controls: {...}, payloadUrl})
 * — fetch the payload, build the scene, wire the controls. Exposes
 * `window.__flowPlayer` ({instanceCount, n, K, seek(t), ...}) for the
 * browser check.
 */
export async function flowPlayer(opts) {
  const { viewerEl, statusEl, cardEl, controls: ui } = opts;
  const say = (text) => {
    if (statusEl) statusEl.textContent = text;
  };
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  viewerEl.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf8fafc);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 50);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.4));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.bias = -0.0005;
  scene.add(sun, sun.target);

  let state = null; // the loaded payload's scene objects
  let t = 0;
  let playing = true;
  let last = performance.now();
  const target = new THREE.Vector3();
  let orbited = false;

  const resize = () => {
    const w = viewerEl.clientWidth || 800;
    const h = viewerEl.clientHeight || 480;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  resize();
  window.addEventListener("resize", resize);

  const draw = () => renderer.render(scene, camera);

  function clear() {
    if (!state) return;
    for (const o of state.objects) {
      scene.remove(o);
      o.geometry?.dispose();
      o.material?.dispose();
    }
    state = null;
  }

  function build(d) {
    clear();
    const aspect = d.ny / d.nx;
    const scale = d.scale;
    const thin = scale.h0 && scale.L ? scale.h0 / scale.L : 0.01;
    const vx = Number(ui.exaggeration?.value || 4);
    const v = thin * vx; // scene height of one h0
    const { floor, walls, lid } = chamberMeshes(d, v);
    const radius = Math.max(0.0015, Math.min(0.25 * v, 0.6 * d.dx));
    const sphere = new THREE.SphereGeometry(radius, 10, 8);
    const particles = new THREE.InstancedMesh(
      sphere,
      new THREE.MeshStandardMaterial({ roughness: 0.5 }),
      d.n,
    );
    particles.castShadow = true;
    particles.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    particles.setColorAt(0, new THREE.Color());
    const inlet = portMesh(d, d.inlet, v, 0x16a34a);
    const outlet = portMesh(d, d.outlet, v, 0xdc2626);
    const plane = planeMesh(d, v);
    const marks = crossingMarks(d, v, radius * 0.8);
    const trailPos = new Float32Array(d.n * TRAIL_FRAMES * 2 * 3);
    const trailCol = new Float32Array(d.n * TRAIL_FRAMES * 2 * 4);
    const trailGeom = new THREE.BufferGeometry();
    trailGeom.setAttribute("position", new THREE.BufferAttribute(trailPos, 3).setUsage(THREE.DynamicDrawUsage));
    trailGeom.setAttribute("color", new THREE.BufferAttribute(trailCol, 4).setUsage(THREE.DynamicDrawUsage));
    const trails = new THREE.LineSegments(
      trailGeom,
      new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }),
    );
    const objects = [floor, walls, lid, particles, inlet, outlet, plane, marks.mesh, trails];
    // the instances move every frame: a bounding sphere taken at t* = 0
    // (all at the inlet) would cull them once they leave it
    for (const o of [particles, marks.mesh, trails]) o.frustumCulled = false;
    for (const o of objects) scene.add(o);
    // light: from above, tilted, its shadow box framing the chamber
    const cx = 0.5;
    const cz = aspect / 2;
    sun.position.set(cx - 0.25, 1.2, cz - 0.35);
    sun.target.position.set(cx, 0, cz);
    const cam = sun.shadow.camera;
    cam.left = -0.75;
    cam.right = 0.75;
    cam.top = 0.75;
    cam.bottom = -0.75;
    cam.near = 0.1;
    cam.far = 3;
    cam.updateProjectionMatrix();
    if (!orbited) {
      target.set(cx, 0, cz);
      camera.position.set(cx - 0.05, 0.75, cz + 0.6);
      orbit(camera, renderer.domElement, target, () => {
        if (!playing) draw();
      });
      orbited = true;
    }
    state = {
      d,
      v,
      objects,
      particles,
      marks,
      trails,
      planeObjs: [plane, marks.mesh],
      portObjs: [inlet, outlet],
      shellObjs: [walls, lid],
      pos: new Float32Array(d.n * 3),
      vals: new Float32Array(d.n),
      ranges: { speed: speedRange(d), residence: residenceRange(d) },
      tEnd: d.frameT.length ? d.frameT[d.frameT.length - 1] : 0,
    };
    if (ui.scrub) ui.scrub.max = String(state.tEnd);
    applyToggles();
    window.__flowPlayer = {
      instanceCount: particles.count,
      n: d.n,
      K: d.K,
      tEnd: state.tEnd,
      shadows: renderer.shadowMap.enabled && particles.castShadow && floor.receiveShadow,
      seek: (tt) => {
        t = tt;
        update();
        draw();
      },
      pause: () => setPlaying(false),
      time: () => t,
      crossed: () => state.marks.mesh.count,
    };
  }

  const m4 = new THREE.Matrix4();
  const col = new THREE.Color();

  function update() {
    if (!state) return;
    const { d, v, particles, pos, vals } = state;
    positionsAt(d, t, pos);
    const mode = ui.colour?.value || "speed";
    colourValues(d, mode, t, pos, state.ranges, vals);
    for (let p = 0; p < d.n; p++) {
      const x = pos[3 * p];
      const z = pos[3 * p + 1];
      m4.makeTranslation(x, sceneY(d, x, z, pos[3 * p + 2], v), z);
      particles.setMatrixAt(p, m4);
      const [r, g, b] = colormap(vals[p]);
      particles.setColorAt(p, col.setRGB(r, g, b));
    }
    particles.instanceMatrix.needsUpdate = true;
    if (particles.instanceColor) particles.instanceColor.needsUpdate = true;
    const shown = crossedBy(d, t);
    state.marks.mesh.count = Math.min(shown, state.marks.times.length);
    if (ui.trails?.checked) updateTrails();
    if (ui.scrub && document.activeElement !== ui.scrub) ui.scrub.value = String(t);
    if (ui.clock) ui.clock.textContent = clockText(t, d.scale);
    if (ui.crossed) ui.crossed.textContent = `${shown} / ${d.n} crossed the plane`;
  }

  function updateTrails() {
    const { d, v, trails } = state;
    const [k, f] = frameAt(d.frameT, t);
    const P = trails.geometry.attributes.position.array;
    const C = trails.geometry.attributes.color.array;
    const bg = 0;
    let s = 0;
    const at = (kk, p, out) => {
      const o = (kk * d.n + p) * 3;
      out[0] = d.frames[o];
      out[1] = d.frames[o + 1];
      out[2] = d.frames[o + 2];
    };
    const a = [0, 0, 0];
    const b = [0, 0, 0];
    for (let p = 0; p < d.n; p++) {
      // segment 0: the current (interpolated) point back to frame k;
      // then frame k → k-1, …
      b[0] = state.pos[3 * p];
      b[1] = state.pos[3 * p + 1];
      b[2] = state.pos[3 * p + 2];
      for (let j = 0; j < TRAIL_FRAMES; j++) {
        const kk = k - j;
        if (kk < 0) {
          for (let q = 0; q < 6; q++) P[s * 6 + q] = 0;
          for (let q = 0; q < 8; q++) C[s * 8 + q] = bg;
          s++;
          continue;
        }
        at(kk, p, a);
        const alpha0 = Math.max(0, 1 - (j + (j === 0 ? 0 : f)) / TRAIL_FRAMES) * 0.7;
        const alpha1 = Math.max(0, 1 - (j + 1) / TRAIL_FRAMES) * 0.7;
        P[s * 6] = b[0];
        P[s * 6 + 1] = sceneY(d, b[0], b[1], b[2], v);
        P[s * 6 + 2] = b[1];
        P[s * 6 + 3] = a[0];
        P[s * 6 + 4] = sceneY(d, a[0], a[1], a[2], v);
        P[s * 6 + 5] = a[1];
        C.set([0.2, 0.25, 0.35, alpha0, 0.2, 0.25, 0.35, alpha1], s * 8);
        b[0] = a[0];
        b[1] = a[1];
        b[2] = a[2];
        s++;
      }
    }
    trails.geometry.attributes.position.needsUpdate = true;
    trails.geometry.attributes.color.needsUpdate = true;
    trails.geometry.computeBoundingSphere();
  }

  function applyToggles() {
    if (!state) return;
    state.trails.visible = !!ui.trails?.checked;
    for (const o of state.planeObjs) o.visible = ui.plane ? ui.plane.checked : true;
    for (const o of state.portObjs) o.visible = ui.ports ? ui.ports.checked : true;
    for (const o of state.shellObjs) o.visible = ui.shell ? ui.shell.checked : true;
    update();
    draw();
  }

  function setPlaying(on) {
    playing = on;
    if (ui.play) ui.play.textContent = on ? "pause" : "play";
    last = performance.now();
  }

  function tick(now) {
    requestAnimationFrame(tick);
    if (!state || !playing) return;
    const dt = (now - last) / 1000;
    last = now;
    // one full run plays in ~12 s at speed ×1
    const rate = (state.tEnd / 12) * Number(ui.speed?.value || 1);
    t += dt * rate;
    if (t > state.tEnd) {
      if (ui.loop?.checked) t = 0;
      else {
        t = state.tEnd;
        setPlaying(false);
      }
    }
    update();
    draw();
  }

  function renderCard(d) {
    if (!cardEl) return;
    const card = d.card;
    const s = d.scale;
    const rows = [];
    const esc = (x) => String(x).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    rows.push(
      `<div class="text-xs text-fg-subtle mb-1">L = ${(+s.L).toPrecision(3)} m · h0 = ${(+s.h0).toPrecision(3)} m` +
        (s.u0 ? ` · U0 = ${(+s.u0).toPrecision(3)} m/s` : "") +
        `</div>`,
    );
    if (!card) {
      rows.push(
        `<div class="text-xs">regime card: not computed — give μ, ρ and U0 (SI) above.</div>`,
      );
    } else {
      rows.push(
        `<div class="text-xs font-semibold mb-1 ${card.ok ? "text-success" : "text-danger"}">` +
          `${card.ok ? "model holds" : "outside the model: " + esc(card.failures.join(", "))}</div>`,
      );
      rows.push(`<table class="w-full text-xs"><tbody>`);
      for (const ln of card.lines) {
        const verdict = !ln.applicable ? "n/a" : ln.ok ? "ok" : "FAIL";
        const cls = !ln.applicable ? "text-fg-subtle" : ln.ok ? "text-success" : "text-danger";
        const val = ln.value === null || ln.value === undefined ? "—" : Number(ln.value).toPrecision(3);
        rows.push(
          `<tr title="holds when ${esc(ln.holds_when)}; else ${esc(ln.otherwise)}">` +
            `<td class="pr-2">${esc(ln.name)}</td><td class="pr-2 tabular-nums">${val}</td>` +
            `<td class="${cls}">${verdict}</td></tr>`,
        );
      }
      rows.push(`</tbody></table>`);
    }
    if (d.subsampled && d.subsampled.note !== "full resolution") {
      rows.push(`<div class="text-xs text-warning mt-1">${esc(d.subsampled.note)}</div>`);
    }
    cardEl.innerHTML = rows.join("");
  }

  async function load(url) {
    say("computing the flow… (the first run of a chamber can take ~30 s)");
    const t0 = performance.now();
    let payload;
    try {
      const r = await fetch(url);
      payload = await r.json();
      if (!r.ok) {
        say(`flow refused: ${payload.error || r.status}${payload.next ? " — " + payload.next : ""}`);
        return;
      }
    } catch (err) {
      say(`flow payload failed: ${err}`);
      return;
    }
    const d = decodePayload(payload);
    build(d);
    renderCard(d);
    t = Math.min(t, state.tEnd);
    update();
    draw();
    say(
      `${d.n} particles × ${d.K} frames · profile ${payload.profile} · D_v* ${payload.d_v_star} · ` +
        `breakthrough ${(payload.breakthrough * 100).toFixed(1)} % · ${((performance.now() - t0) / 1000).toFixed(1)} s`,
    );
  }

  // controls
  ui.play?.addEventListener("click", () => setPlaying(!playing));
  ui.scrub?.addEventListener("input", () => {
    t = Number(ui.scrub.value);
    update();
    draw();
  });
  for (const el of [ui.colour, ui.trails, ui.plane, ui.ports, ui.shell]) {
    el?.addEventListener("change", applyToggles);
  }
  ui.exaggeration?.addEventListener("change", () => state && build(state.d));
  ui.form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = new URLSearchParams(new FormData(ui.form));
    for (const [k, val] of [...q.entries()]) if (!val.trim()) q.delete(k);
    const base = new URL(opts.payloadUrl, window.location.href);
    for (const [k, val] of q.entries()) base.searchParams.set(k, val);
    load(base.pathname + base.search);
  });

  requestAnimationFrame(tick);
  if (ui.form) ui.form.requestSubmit();
  else await load(opts.payloadUrl);
}
