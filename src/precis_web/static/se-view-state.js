// Shared view state for the se 3D viewer (gr477845, gr458084).
//
// Two small pieces every object type goes through, so a new renderer
// inherits them instead of growing its own copy:
//
//   createObjectVisibility() — ONE per-object eye state, keyed by the
//     vendored tree path (the stable object id: the "/"-join of block
//     names). The viewer's tree eyes feed it (`update`), every renderer
//     asks it (`shapeShown` / `edgesShown`: envelope, atoms, surfaces,
//     targets, interface-shape grid lines), and it survives a scene
//     re-render (`restore`) — a revision step starts from default states.
//
//   createLatest() — ONE "latest request wins" gate for scene loads: a
//     new `begin()` aborts the previous request and marks it stale, so
//     overlapping revision steps render once, newest only.
//
// Pure data and callbacks — no DOM, no three.js — so node can test it.

// Vendored tree states: 0 hidden, 1 shown, 2 mixed (group), 3 empty.
const HIDDEN = 0;
const SHOWN = 1;

export function createObjectVisibility() {
  const states = new Map(); // path -> [shape, edges]
  const listeners = new Set();
  return {
    //: Merge the viewer's `{path: [shape, edges]}` dict (its `states`
    //: notification). Paths absent from `next` keep their last value: an
    //: object missing from one revision keeps its eye for the next.
    update(next) {
      if (!next || typeof next !== "object") return;
      for (const [path, st] of Object.entries(next)) {
        if (Array.isArray(st) && st.length >= 2) states.set(path, [st[0], st[1]]);
      }
      for (const fn of listeners) fn();
    },
    shapeShown(path) {
      const st = states.get(path);
      return !st || st[0] !== HIDDEN;
    },
    edgesShown(path) {
      const st = states.get(path);
      return !st || st[1] !== HIDDEN;
    },
    //: Entries worth re-applying after a fresh render: a plain 0/1 pair
    //: with something hidden. Mixed (2) groups follow from their children;
    //: shown-by-default entries need nothing.
    hiddenStates() {
      const out = {};
      for (const [path, st] of states) {
        const plain = st.every((v) => v === HIDDEN || v === SHOWN);
        if (plain && st.some((v) => v === HIDDEN)) out[path] = [st[0], st[1]];
      }
      return out;
    },
    //: Re-apply onto a freshly rendered viewer (`viewer.setStates`).
    restore(viewer) {
      const hidden = this.hiddenStates();
      if (Object.keys(hidden).length) viewer.setStates(hidden);
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export function createLatest() {
  let seq = 0;
  let ctl = null;
  return {
    //: Start a request: aborts the previous one. `isStale()` is true once a
    //: newer `begin()` (or `cancel()`) happened.
    begin() {
      if (ctl) ctl.abort();
      ctl = new AbortController();
      const id = ++seq;
      return { signal: ctl.signal, isStale: () => id !== seq };
    },
    cancel() {
      if (ctl) ctl.abort();
      ctl = null;
      seq++;
    },
  };
}
