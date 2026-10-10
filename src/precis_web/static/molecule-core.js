// Shared atom/bond drawing extracted from the SE viewer. Hosts supply live
// camera, clipping and redraw hooks; scientific records remain adapter-owned.
// No fetch, completion classification or geometry mutation is performed here.
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

//: The hover table's [field, value] rows for atom `i` of payload block
//: `b` (`pick.atom_hover_names`): for a realized chain the atom name with
//: its element, the residue spelled out, and the chain with its strand;
//: otherwise the scene label with its element.
function _hoverRows(b, i) {
  const el = b.elements[i];
  const h = b.hover;
  if (h && !Array.isArray(h) && h.atom) {
    const [residue, chain, strand] = h.residues[h.residue[i]] || [];
    const rows = [["atom", `${h.atom[i]} (${el})`]];
    if (residue) rows.push(["residue", residue]);
    if (chain) rows.push(["chain", strand ? `${chain} = strand ${strand}` : chain]);
    return rows;
  }
  return [["atom", `${(Array.isArray(h) && h[i]) || `#${i}`} (${el})`]];
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

//: gr461146 draw order. Group renderOrder is three.js's primary sort key
//: for the transparent pass (the vendored CAD tree sits at 0, the pick
//: markers at 1000); within the atomic group the surface goes before the
//: atoms and bonds. The surface writes depth from this slider position on.
const _ORDER_ATOMIC = 1;
const _ORDER_TARGET = 2;
const _ORDER_SURFACE = 0;
const _ORDER_ATOMS = 1;
const _SURFACE_BODY_T = 0.5;

function _nextFrame() {
  return new Promise((resolve) => requestAnimationFrame(resolve));
}
//: Atoms per slice of the overlay's mesh build (bonds proportionally).
const _BUILD_SLICE_ATOMS = 1000;

export async function createMoleculeCore(host, data, {
  progress = null, isStale = () => false, onLegend = () => {},
  loadTargets = async () => ({}), targetEnabled = () => true,
} = {}) {
  const THREE = await import("three");
  if (isStale() || !data.blocks?.length || !host.scene) return null;
  const scene = host.scene;
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

  // Scratch objects, reused by every matrix write (no per-atom allocation).
  const _mat = new THREE.Matrix4();
  const _pos = new THREE.Vector3();
  const _dir = new THREE.Vector3();
  const _quat = new THREE.Quaternion();
  const _scl = new THREE.Vector3();
  const _identityQuat = new THREE.Quaternion();
  const _tmpColor = new THREE.Color();
  const _bondGrey = new THREE.Color(_BOND_GREY);

  //: Writes bond `k`'s instance matrix: a unit cylinder moved to the
  //: midpoint of a→b, turned from +Y onto the bond, scaled (r, length, r).
  function orientBond(mesh, k, a, b, bondR) {
    const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
    const len = Math.hypot(dx, dy, dz) || 1e-12;
    _pos.set((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2);
    _scl.set(bondR, len, bondR);
    _dir.set(dx, dy, dz).normalize();
    _quat.setFromUnitVectors(yAxis, _dir);
    _mat.compose(_pos, _quat, _scl);
    mesh.setMatrixAt(k, _mat);
  }

  //: Raycasting an InstancedMesh tests against its bounding sphere, so it
  //: has to follow the instance matrices.
  function refreshBounds(mesh) {
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
    if (mesh.computeBoundingBox) mesh.computeBoundingBox();
  }

  let disposed = false;
  const superseded = isStale;
  isStale = () => disposed || superseded();
  function dispose() {
    if (disposed) return;
    disposed = true;
    restoreEnvelopes();
    const geometries = new Set([sphereGeo, cylGeo]);
    const materials = new Set();
    for (const root of [group, targetGroup]) {
      root?.traverse((mesh) => {
        if (mesh.geometry) geometries.add(mesh.geometry);
        if (mesh.material) materials.add(mesh.material);
        if (mesh.isInstancedMesh) mesh.dispose();
      });
      root?.removeFromParent();
    }
    geometries.forEach((geometry) => geometry.dispose());
    materials.forEach((material) => material.dispose());
  }
  let targetGroup = null;
  const group = new THREE.Group();
  group.name = "bt3d-atomic-overlay";
  // gr461146: three.js orders transparent objects by bounding-sphere
  // distance, and every mesh here shares one centre, so the surface, the
  // atoms and the teal target interleaved in an order that differed per
  // page load — a depth-writing mesh drawn first then cut holes into
  // whatever came after. A Group's renderOrder is the pass's primary sort
  // key: the vendored CAD tree (0), then this overlay, then the target.
  group.renderOrder = _ORDER_ATOMIC;
  scene.add(group);

  //: Clip only traverses the vendored CAD tree; our overlays live outside
  //: it (gr459593). Sync at draw time so plane replacements/intersection
  //: changes and meshes built after async yields all use the same controls.
  //: Plane objects stay live; disabling uses the renderer's existing flag.
  function followClipping(mesh) {
    let planeCount = -1;
    mesh.onBeforeRender = () => {
      const planes = host.clipPlanes();
      const intersection = host.clipIntersection();
      const material = mesh.material;
      if (planeCount !== planes.length || material.clipIntersection !== intersection) {
        material.needsUpdate = true;
        planeCount = planes.length;
      }
      material.clippingPlanes = planes;
      material.clipIntersection = intersection;
    };
    return mesh;
  }

  // Atoms and bonds are instanced: one InstancedMesh and one material per
  // block per kind, not one Mesh per atom/bond. A drum is ~15,000 of them,
  // and that many meshes and materials slowed the build (gr462703) and made
  // Chrome lose the WebGL context (gr462702). Colour is per instance.
  const sphereGeo = new THREE.SphereGeometry(1, 12, 8);
  const cylGeo = new THREE.CylinderGeometry(1, 1, 1, 8, 1);
  const blocks = [];

  const cageEnvelope = (uid) => host.cage(uid);
  const setEnvelopesCaged = (on) => host.setCaged(on);
  const restoreEnvelopes = () => host.restore();

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
      cageEnvelope(b.uid);
      const { atomR, bondR } = atomBondRadiiFor(b);
      const n = b.elements.length;
      // One InstancedMesh per block per kind: the material stays white
      // because the per-instance colour multiplies it. Instance order is
      // the payload's atom order — the order the pick route resolves
      // against.
      const atomMesh = new THREE.InstancedMesh(
        sphereGeo,
        new THREE.MeshStandardMaterial({ color: 0xffffff, transparent: true }),
        n
      );
      atomMesh.userData.blockIndex = blocks.length;
      atomMesh.renderOrder = _ORDER_ATOMS;
      const cpk = new Array(n);
      for (let i = 0; i < n; i++) {
        cpk[i] = new THREE.Color(_ATOMIC_CPK[b.elements[i]] || _ATOMIC_CPK_DEFAULT);
        atomMesh.setColorAt(i, cpk[i]);
        atomsDone++;
        if (atomsDone % _BUILD_SLICE_ATOMS === 0) await reportBuild();
      }
      group.add(followClipping(atomMesh));
      const bondList = b.bonds || [];
      const bondMesh = new THREE.InstancedMesh(
        cylGeo,
        new THREE.MeshStandardMaterial({ color: 0xffffff, transparent: true }),
        bondList.length
      );
      bondMesh.userData.blockIndex = blocks.length;
      bondMesh.renderOrder = _ORDER_ATOMS;
      const greyColour = new THREE.Color(_BOND_GREY);
      const bondEntries = [];
      const bondSlice = Math.max(1, Math.ceil((_BUILD_SLICE_ATOMS * bondList.length) / Math.max(1, n)));
      for (const [i, j] of bondList) {
        bondMesh.setColorAt(bondEntries.length, greyColour);
        bondEntries.push({ i, j, hot: false });
        bondsDone++;
        if (bondEntries.length % bondSlice === 0) await reportBuild();
      }
      group.add(followClipping(bondMesh));

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
          // gr461146: a ghost surface over the atoms must not write
          // depth; applyT turns it on once the surface is the body (and
          // turns the atoms' off at the same point).
          depthWrite: false,
        })
      );
      // Drawn before the atoms and bonds of the same slider position:
      // atoms in front blend over it instead of cutting it (gr461146).
      surfMesh.renderOrder = _ORDER_SURFACE;
      surfMesh.visible = false;
      group.add(followClipping(surfMesh));

      blocks.push({
        src: b,
        coords: b.coords,
        smooth: b.smooth,
        atomMesh,
        bondMesh,
        bondEntries,
        cpk,
        atomR,
        bondR,
        lerped: null,
        surfMesh,
        surfPositions: positions,
        surfColors: colors,
      });
    }
    // A newer render replaced the scene while this build yielded.
    if (isStale()) {
      dispose();
      return null;
    }
  } catch (err) {
    dispose();
    throw err;
  }

  //: Is this block's object eyed on? One answer from the host's shared
  //: visibility state, consulted by every mesh kind below.
  const objectShown = (uid) => (host.objectShown ? host.objectShown(uid) : true);
  let curT = 0;

  function applyT(t) {
    if (disposed) return;
    curT = t;
    for (const blk of blocks) {
      const shown = objectShown(blk.src.uid);
      const { coords, smooth, atomMesh, bondMesh, bondEntries, atomR, bondR, surfMesh, surfPositions } = blk;
      const n = coords.length;
      // Kept for `applyStrain`, which re-composes bond matrices at the
      // current slider position.
      const lerped = blk.lerped || (blk.lerped = Array.from({ length: n }, () => [0, 0, 0]));
      for (let i = 0; i < n; i++) {
        const c = coords[i], s = smooth[i];
        const x = c[0] + (s[0] - c[0]) * t;
        const y = c[1] + (s[1] - c[1]) * t;
        const z = c[2] + (s[2] - c[2]) * t;
        const p = lerped[i];
        p[0] = x; p[1] = y; p[2] = z;
        _pos.set(x, y, z);
        _scl.setScalar(atomR);
        _mat.compose(_pos, _identityQuat, _scl);
        atomMesh.setMatrixAt(i, _mat);
        surfPositions[i * 3] = x;
        surfPositions[i * 3 + 1] = y;
        surfPositions[i * 3 + 2] = z;
      }
      for (let k = 0; k < bondEntries.length; k++) {
        const { i, j, hot } = bondEntries[k];
        orientBond(bondMesh, k, lerped[i], lerped[j], hot ? bondR * _HOT_BOND_SCALE : bondR);
      }
      refreshBounds(atomMesh);
      refreshBounds(bondMesh);
      atomMesh.material.opacity = bondMesh.material.opacity = 1 - t;
      atomMesh.visible = bondMesh.visible = shown && t < 0.999;
      surfMesh.geometry.attributes.position.needsUpdate = true;
      surfMesh.geometry.computeVertexNormals();
      surfMesh.material.opacity = t;
      // gr461146: exactly one layer writes depth — the dominant one. With
      // depth off, a closed surface's back wall overdraws its front wall at
      // opacity t, so the surface writes once it is the body; and a
      // near-invisible atom that still wrote depth carved its sphere out of
      // the surface and the target behind it (measured: a bubble pattern
      // over the target at slider 90), so atoms and bonds stop writing
      // once they are the ghost.
      const surfaceIsBody = t >= _SURFACE_BODY_T;
      surfMesh.material.depthWrite = surfaceIsBody;
      atomMesh.material.depthWrite = bondMesh.material.depthWrite = !surfaceIsBody;
      surfMesh.visible = shown && t > 0.001;
    }
    try {
      host.redraw();
    } catch (err) {
      // Best-effort — see recolour's own try/catch for the convention.
      console.error("blocktree-3d: atomic overlay redraw failed", err);
    }
  }

  applyT(0);
  onLegend(devMax);

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
    if (disposed) return;
    group.visible = on;
    setEnvelopesCaged(on);
    try {
      // Same reason applyT ends with one: these mutate the scene graph
      // under the vendored viewer's redraw hook, which never observes
      // them — without this the canvas keeps the previous frame until
      // some unrelated interaction forces a repaint.
      host.redraw();
    } catch (err) {
      console.error("blocktree-3d: atom visibility redraw failed", err);
    }
  }

  //: Re-derive every mesh's visibility from the shared object state (an
  //: eye was clicked): atoms, bonds, surfaces and target surfaces alike.
  function refreshVisibility() {
    if (disposed) return;
    for (const blk of blocks) {
      const shown = objectShown(blk.src.uid);
      blk.atomMesh.visible = blk.bondMesh.visible = shown && curT < 0.999;
      blk.surfMesh.visible = shown && curT > 0.001;
    }
    for (const mesh of targetGroup ? targetGroup.children : []) {
      mesh.visible = objectShown(mesh.userData.uid);
    }
    setEnvelopesCaged(group.visible);
    try {
      host.redraw();
    } catch (err) {
      console.error("blocktree-3d: object visibility redraw failed", err);
    }
  }

  //: The atom under a point, as `{block, atom, hover}`, or null. The vendored
  //: viewer raycasts only its own parts tree, so the overlay casts its
  //: own ray, from the vendored live camera (the same private reach as
  //: the scale bar's `_worldPerPixel`). Hidden atoms — atoms off, or the
  //: slider at the smooth end — are not pickable.
  const raycaster = new THREE.Raycaster();
  function pickAtom(clientX, clientY, canvas, includeBonds = false) {
    if (disposed || !group.visible) return null;
    const camera = host.camera();
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
      if (blk.atomMesh.visible) atoms.push(blk.atomMesh);
      if (includeBonds && blk.bondMesh.visible) atoms.push(blk.bondMesh);
    }
    const planes = host.clipPlanes();
    const hit = raycaster.intersectObjects(atoms, false).find((hit) => {
      const outside = planes.map((plane) => plane.distanceToPoint(hit.point) < 0);
      return !outside.length || !(host.clipIntersection() ? outside.every(Boolean) : outside.some(Boolean));
    });
    if (!hit || hit.instanceId === undefined) return null;
    const b = blocks[hit.object.userData.blockIndex].src;
    const i = hit.instanceId;
    if (hit.object === blocks[hit.object.userData.blockIndex].bondMesh) {
      return { block: b.uid, atom: null, selection: b.bondKeys?.[i] ? {
        kind: "bond", scene: b.scene, key: b.bondKeys[i],
      } : null };
    }
    return {
      block: b.uid, atom: i, hover: _hoverRows(b, i),
      // Exact identity comes from the decoded adapter, never the mesh ordinal.
      selection: b.selections?.[i] ? {
        ...b.selections[i], displayed_host: blocks[hit.object.userData.blockIndex].lerped[i].slice(),
      } : null,
    };
  }

  // ── target surface (smooth_drum's surface_meridian, revolved server-side)
  // One translucent double-sided mesh per block that carries a target, in
  // its own group so it is independent of the atoms toggle. Off by default;
  // the checkbox is revealed only when some block has a target. The meshes
  // are built (and target3d.json fetched) the first time it is ticked: the
  // target is most of the atom payload and most pages never show it.
  targetGroup = new THREE.Group();
  targetGroup.name = "bt3d-target-overlay";
  // gr461146: always after the atomic overlay (see group.renderOrder).
  targetGroup.renderOrder = _ORDER_TARGET;
  targetGroup.visible = false;
  const hasTarget = data.blocks.some((b) => b.has_target);
  let targetBuilt = false;
  if (hasTarget) scene.add(targetGroup);

  function buildTargetMeshes(targets) {
    for (const b of data.blocks) {
      const t = targets[String(b.uid)];
      if (!t || !t.verts || !t.verts.length) continue;
      const pos = new Float32Array(t.verts.length * 3);
      t.verts.forEach((v, i) => {
        pos[i * 3] = v[0];
        pos[i * 3 + 1] = v[1];
        pos[i * 3 + 2] = v[2];
      });
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      geo.setIndex(t.tris.flat());
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
      mesh.userData.uid = b.uid;
      mesh.visible = objectShown(b.uid);
      targetGroup.add(followClipping(mesh));
    }
    targetBuilt = true;
  }

  async function setTargetVisible(on) {
    if (isStale() || !hasTarget) return;
    if (on && !targetBuilt) {
      try {
        const doc = await loadTargets();
        if (isStale()) return;
        // A second tick during the same fetch awaited it too: build once.
        if (!targetBuilt) buildTargetMeshes(doc.targets || {});
      } catch (err) {
        console.error("blocktree-3d: target surface fetch failed", err);
        host.targetFailed?.();
        return;
      }
    }
    // Unticked while the fetch was in flight: stay hidden.
    if (on && !targetEnabled()) return;
    targetGroup.visible = on;
    try {
      host.redraw();
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
    if (disposed) return null;
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
      const n = blk.cpk.length;
      for (let i = 0; i < n; i++) {
        const t = state.angle.on && angle && angleVals
          ? _aboveThreshold(angleVals[i], state.angle.thr, angle.max)
          : null;
        if (angleVals) tally("angle", angleVals[i], t);
        if (t === null) blk.atomMesh.setColorAt(i, blk.cpk[i]);
        else blk.atomMesh.setColorAt(i, _tmpColor.setRGB(..._rampColor(_ANGLE_STRAIN_STOPS, t)));
      }
      if (n) blk.atomMesh.instanceColor.needsUpdate = true;
      const bondEntries = blk.bondEntries;
      for (let k = 0; k < bondEntries.length; k++) {
        const entry = bondEntries[k];
        const t = state.bond.on && bond && b.bond_dev
          ? _aboveThreshold(b.bond_dev[k], state.bond.thr, bond.max)
          : null;
        if (b.bond_dev) tally("bond", b.bond_dev[k], t);
        entry.hot = t !== null;
        orientBond(
          blk.bondMesh, k, blk.lerped[entry.i], blk.lerped[entry.j],
          entry.hot ? blk.bondR * _HOT_BOND_SCALE : blk.bondR
        );
        if (t === null) blk.bondMesh.setColorAt(k, _bondGrey);
        else blk.bondMesh.setColorAt(k, _tmpColor.setRGB(..._rampColor(_BOND_STRAIN_STOPS, t)));
      }
      if (bondEntries.length) {
        blk.bondMesh.instanceColor.needsUpdate = true;
        refreshBounds(blk.bondMesh);
      }
      for (let i = 0; i < n; i++) {
        const t = dev ? _aboveThreshold(b.deviation[i], state.deviation.thr, dev.max) : null;
        tally("deviation", b.deviation[i], t);
        const rgb = t === null ? _BELOW_THRESHOLD_SURFACE : _deviationColor(t);
        blk.surfColors.set(rgb, i * 3);
      }
      blk.surfMesh.geometry.attributes.color.needsUpdate = true;
    }
    try {
      host.redraw();
    } catch (err) {
      console.error("blocktree-3d: strain layer redraw failed", err);
    }
    return counts;
  }

  const bindings = new Map(blocks.map((b) => [String(b.src.uid), b.src.binding]));
  const pickMolecule = (x, y, canvas) => pickAtom(x, y, canvas, true);
  return { applyT, setVisible, refreshVisibility, pickAtom, pickMolecule, hasTarget, setTargetVisible, strainStats, applyStrain, bindings, dispose };
}
