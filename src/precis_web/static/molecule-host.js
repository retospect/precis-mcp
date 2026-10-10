// Host boundary: only the SE adapter reaches the CAD viewer's private scene.
// Capture that scene once: retiring an old overlay must never touch a new one.
export function focusMoleculePoints(points, setTarget) {
  const focus = [0, 1, 2].map((axis) => points.reduce((sum, point) => sum + point[axis], 0) / points.length);
  setTarget(focus);
  return focus;
}

export function createMoleculeMarkers(THREE, scene, points, radius, name) {
  const group = new THREE.Group();
  group.name = name;
  group.renderOrder = 1000;
  for (const point of points) {
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 12),
      // Join the transparent pass so envelope fills cannot cover the marker.
      new THREE.MeshBasicMaterial({ color: "#facc15", transparent: true, depthTest: false, depthWrite: false })
    );
    marker.position.set(...point);
    marker.renderOrder = 1000;
    group.add(marker);
  }
  scene.add(group);
  let disposed = false;
  group.dispose = () => {
    if (disposed) return;
    disposed = true;
    group.removeFromParent();
    for (const mesh of group.children) {
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
  };
  return group;
}

//: `visibility` is the page's shared object-visibility state; the caged
// envelope follows it, so atoms toggling never un-hides an eyed-off block.
export function createSEHost(viewer, pathForUid, visibility = null) {
  const rendered = viewer?._rendered;
  const caged = new Set();
  const groups = rendered?.nestedGroup?.groups || {};
  return {
    scene: rendered?.scene,
    camera: () => {
      const camera = rendered?.camera;
      return camera ? (camera.ortho ? camera.oCamera : camera.pCamera) : null;
    },
    clipPlanes: () => rendered?.clipping?.clipPlanes || [],
    clipIntersection: () => viewer.getClipIntersection(),
    redraw: () => {
      if (viewer._rendered === rendered) viewer.update(true);
    },
    cage(uid) {
      const path = pathForUid(uid);
      if (!path || !groups[path]) return;
      groups[path].setShapeVisible(false);
      caged.add(path);
    },
    objectShown(uid) {
      const path = pathForUid(uid);
      return !visibility || !path || visibility.shapeShown(path);
    },
    setCaged(on) {
      for (const path of caged) {
        groups[path]?.setShapeVisible(!on && (!visibility || visibility.shapeShown(path)));
      }
    },
    restore() {
      for (const path of caged) {
        groups[path]?.setShapeVisible(!visibility || visibility.shapeShown(path));
      }
      caged.clear();
    },
  };
}

// Standalone hosts use the same drawing core without a fabricated SE tree.
export function createStructureHost({ scene, camera, redraw, planes = [], intersection = false }) {
  return {
    scene,
    camera: () => camera,
    redraw,
    clipPlanes: () => planes,
    clipIntersection: () => intersection,
    cage() {},
    setCaged() {},
    restore() {},
  };
}

// A standalone rendering host, currently exercised only by compatibility
// fixtures. Route integration awaits its own review; this does not read stores.
export async function mountStructureMoleculeHost(container) {
  const [THREE, { OrbitControls }, { createMoleculeCore }] = await Promise.all([
    import("three"),
    import("/static/three/examples/jsm/controls/OrbitControls.js"),
    import("/static/molecule-core.js"),
  ]);
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2));
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 1000);
  camera.position.set(0, 0, 8);
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setClearColor(0x101820);
  renderer.localClippingEnabled = true;
  container.appendChild(renderer.domElement);
  const graphStatus = document.createElement("div");
  graphStatus.setAttribute("role", "status");
  graphStatus.dataset.moleculeGraphStatus = "";
  container.appendChild(graphStatus);
  const controls = new OrbitControls(camera, renderer.domElement);
  let disposed = false, unavailable = false, generation = 0, overlay = null, marker = null;
  let selections = new Map();
  const selectionIdentity = ({ scene: identity, key }) => JSON.stringify([
    identity.scene_token, identity.run_id, identity.design?.ref_id,
    identity.design?.revision, identity.design?.token, identity.design?.source,
    identity.block_uid, identity.binding_token, identity.placement_token,
    identity.layer_id, identity.role, identity.physical,
    key.geometry, key.row_id, key.image,
  ]);
  let planes = [], intersection = false;
  const redraw = () => {
    if (!disposed && !unavailable) renderer.render(scene, camera);
  };
  const host = {
    ...createStructureHost({ scene, camera, redraw }),
    clipPlanes: () => planes,
    clipIntersection: () => intersection,
  };
  const resize = (width, height) => {
    if (disposed || !(width > 0 && height > 0)) return;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    redraw();
  };
  const observer = new ResizeObserver(() => resize(container.clientWidth, container.clientHeight));
  observer.observe(container);
  controls.addEventListener("change", redraw);
  const lost = (event) => {
    event.preventDefault();
    unavailable = true;
    generation++;
    selections.clear();
    marker?.dispose();
    marker = null;
    graphStatus.textContent = "Molecule view unavailable: WebGL context lost.";
    container.dataset.moleculeAvailability = "unavailable";
  };
  renderer.domElement.addEventListener("webglcontextlost", lost);
  resize(container.clientWidth, container.clientHeight);
  return {
    scene, camera, renderer, controls,
    async replace(data) {
      if (disposed || unavailable) return false;
      const current = ++generation;
      graphStatus.textContent = "";
      selections.clear();
      marker?.dispose();
      marker = null;
      overlay?.dispose();
      overlay = null;
      const replacement = await createMoleculeCore(host, data, {
        isStale: () => disposed || unavailable || generation !== current,
      });
      if (disposed || unavailable || generation !== current) {
        replacement?.dispose();
        return false;
      }
      overlay = replacement;
      const unavailableGraphs = data.blocks.filter((block) => block.graph_availability?.status === "unavailable");
      container.dataset.moleculeGraphAvailability = unavailableGraphs.length ? "unavailable" : "ready";
      graphStatus.textContent = unavailableGraphs.length
        ? `Bonds unavailable: ${[...new Set(unavailableGraphs.map((block) => block.graph_availability.reason))].join("; ")}. Atoms remain available.`
        : "";
      selections = new Map(data.blocks.flatMap((block) => block.selections || [])
        .map((selection) => [selectionIdentity(selection), structuredClone(selection)]));
      container.dataset.moleculeAvailability = replacement ? "ready" : "unavailable";
      redraw();
      return !!replacement;
    },
    pick(x, y) {
      if (disposed || unavailable) return null;
      return overlay?.pickMolecule(x, y, renderer.domElement)?.selection || null;
    },
    highlight(selection) {
      marker?.dispose();
      marker = null;
      if (disposed || unavailable || !selection || selection.kind !== "atom") return;
      const current = selections.get(selectionIdentity(selection));
      if (!current) return;
      const point = current.displayed_host;
      marker = createMoleculeMarkers(THREE, scene, [point], 0.12, "molecule-selection-marker");
      focusMoleculePoints([point], (focus) => {
        const offset = camera.position.clone().sub(controls.target);
        controls.target.set(...focus);
        camera.position.copy(controls.target).add(offset);
        controls.update();
      });
      redraw();
    },
    clip(next, nextIntersection = false) { planes = next; intersection = nextIntersection; redraw(); },
    resize,
    dispose() {
      if (disposed) return;
      disposed = true;
      generation++;
      selections.clear();
      observer.disconnect();
      controls.removeEventListener("change", redraw);
      controls.dispose();
      renderer.domElement.removeEventListener("webglcontextlost", lost);
      overlay?.dispose();
      marker?.dispose();
      overlay = null;
      renderer.dispose();
      renderer.domElement.remove();
      graphStatus.remove();
    },
  };
}
