// Render-only projection of shared-decoded records. This is neither the Hex
// tagged-wire codec nor a scientific classifier. Exact keys survive GPU conversion.
const INTEGER = /^(0|-?[1-9][0-9]*)$/;
const GPU_IMAGE_LIMIT = 1000000n;
function finiteVector(value, length) {
  if (!Array.isArray(value) || value.length !== length || !value.every(Number.isFinite)) {
    throw new Error("Unavailable: invalid display vector");
  }
  return value;
}
function exactImage(image, pbc) {
  if (!Array.isArray(image) || image.length !== 3) throw new Error("Unavailable: missing image identity");
  return image.map((value, axis) => {
    if (typeof value !== "string" || !INTEGER.test(value)) throw new Error("Unavailable: image identity is not exact");
    const integer = BigInt(value);
    if (integer < -GPU_IMAGE_LIMIT || integer > GPU_IMAGE_LIMIT) throw new Error("Unavailable: unsupported GPU image range");
    if (integer !== 0n && !pbc) throw new Error("Unavailable: historic cell unavailable");
    if (integer !== 0n && !pbc[axis]) throw new Error("Unavailable: image on nonperiodic axis");
    return Number(integer);
  });
}
const physicalKey = (geometry) => JSON.stringify([
  geometry.format, geometry.ref_id, geometry.version, geometry.token,
]);
const atomKey = (key) => JSON.stringify([physicalKey(key.geometry), key.row_id, key.image]);

export async function projectMoleculeGeometry({ scene, atoms, bonds, graph, cell, placement }) {
  const THREE = await import("three");
  if (!["current", "input-request", "adopted-output"].includes(scene.role)) {
    throw new Error("Unavailable: artifact has no persisted scene");
  }
  if (!scene.scene_token || !scene.placement_token || !scene.layer_id) throw new Error("Unavailable: missing scene identity");
  const translation = finiteVector(placement.translation_host, 3);
  const rotation = new THREE.Quaternion(...finiteVector(placement.quaternion_xyzw, 4));
  if (Math.abs(rotation.lengthSq() - 1) > 1e-8 || !(placement.angstrom_to_host > 0) || !Number.isFinite(placement.angstrom_to_host)) {
    throw new Error("Unavailable: placement requires rigid rotation and positive uniform scale");
  }
  const pbc = cell?.pbc || null;
  if (pbc && (pbc.length !== 3 || !pbc.every((value) => typeof value === "boolean"))) throw new Error("Unavailable: invalid PBC");
  const rows = cell?.lattice_rows_angstrom?.map((row) => finiteVector(row, 3));
  if (rows && rows.length !== 3) throw new Error("Unavailable: invalid row lattice");
  const lookup = new Map();
  const coords = [], selections = [];
  for (const atom of atoms) {
    if (physicalKey(atom.key.geometry) !== physicalKey(scene.physical)) throw new Error("Unavailable: atom geometry mismatch");
    const key = atomKey(atom.key);
    if (lookup.has(key)) throw new Error("Unavailable: ambiguous atom identity");
    const image = exactImage(atom.key.image, pbc);
    if (image.some(Boolean) && !rows) throw new Error("Unavailable: historic cell unavailable");
    const source = finiteVector(atom.cartesian_angstrom, 3).slice();
    for (let axis = 0; axis < 3; axis++) {
      if (image[axis]) for (let component = 0; component < 3; component++) source[component] += image[axis] * rows[axis][component];
    }
    const displayed = new THREE.Vector3(...source).multiplyScalar(placement.angstrom_to_host)
      .applyQuaternion(rotation).add(new THREE.Vector3(...translation)).toArray();
    lookup.set(key, coords.length);
    coords.push(displayed);
    selections.push({ kind: "atom", scene, key: atom.key, source_cartesian_angstrom: source, displayed_host: displayed });
  }
  // Graph identity is separately captured by the shared producer. Never infer
  // it from a bond, bless a validated prefix, or discard valid atom geometry.
  let indices = [], bondKeys = [], graphReason = null;
  const nonempty = (value) => typeof value === "string" && value.trim().length > 0;
  if (!graph || !nonempty(graph.token) || !graph.geometry) {
    graphReason = "missing captured graph identity";
  } else if (physicalKey(graph.geometry) !== physicalKey(scene.physical)) {
    graphReason = "graph geometry mismatch";
  } else if (graph.complete !== true || !["stored", "receipt-comparison", "inferred-display"].includes(graph.source)) {
    graphReason = "captured graph incomplete or unavailable";
  } else if (!Array.isArray(bonds)) {
    graphReason = "missing captured bonds";
  } else {
    const seen = new Set();
    for (const bond of bonds) {
      const key = bond?.key;
      if (!key?.geometry || physicalKey(key.geometry) !== physicalKey(scene.physical)) {
        graphReason = "bond geometry mismatch";
      } else if (key.graph_token !== graph.token) {
        graphReason = "graph token mismatch";
      } else if (!nonempty(key.edge_id)) {
        // edge_id is the shared captured edge key, not a renderer ordinal.
        graphReason = "missing edge identity";
      } else if (key.provenance !== graph.source) {
        graphReason = "bond provenance mismatch";
      } else if (!key.a?.geometry || !key.b?.geometry ||
        physicalKey(key.a.geometry) !== physicalKey(scene.physical) ||
        physicalKey(key.b.geometry) !== physicalKey(scene.physical)) {
        graphReason = "bond endpoint geometry mismatch";
      } else if (seen.has(key.edge_id)) {
        graphReason = "ambiguous edge identity";
      } else {
        const a = lookup.get(atomKey(key.a)), b = lookup.get(atomKey(key.b));
        if (a === undefined || b === undefined) graphReason = "unresolved bond endpoint image";
        else {
          indices.push([a, b]);
          bondKeys.push(key);
          seen.add(key.edge_id);
        }
      }
      if (graphReason) break;
    }
  }
  if (graphReason) { indices = []; bondKeys = []; }
  return {
    scale: placement.angstrom_to_host / 1e-10,
    blocks: [{
      uid: scene.block_uid ?? scene.layer_id, scene, selections,
      // Keep complete bond identities/provenance alongside drawing indices.
      bondKeys,
      graph_availability: {
        status: graphReason ? "unavailable" : "ready", reason: graphReason,
        token: graph?.token || null, source: graph?.source || "unknown",
      },
      elements: atoms.map((atom) => atom.element), coords, smooth: coords,
      bonds: indices, faces: [], deviation: atoms.map(() => null),
      diagnostics_availability: "unavailable",
    }],
  };
}
