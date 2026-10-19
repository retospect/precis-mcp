/* pane-split.js — the shared draggable split behind the two-pane
   workbenches (pcb, mermaid). Extracted from pcb-workbench.js
   (docs/backlog/pcb-workbench-layout.md): a column divider whose drag
   sets the left pane's flex-basis in %, and a row handle whose drag sets
   the container's height in px — both remembered in localStorage,
   double-click resets, arrow keys nudge, Home resets. */
window.PrecisPaneSplit = (function () {
  'use strict';

  /* Column divider. opts: {container, left, divider, key, min, max,
     reset} — `left` gets `flex-basis: <pct>%`, the split is stored under
     `key`, `reset` is the double-click/Home percentage. */
  function attach(opts) {
    const container = opts.container;
    const left = opts.left;
    const divider = opts.divider;
    if (!container || !left || !divider) return;
    const MIN = opts.min ?? 15, MAX = opts.max ?? 85, RESET = opts.reset ?? 50;

    const clamp = (p) => Math.min(MAX, Math.max(MIN, p));
    const apply = (p) => { left.style.flexBasis = p + '%'; };
    const store = (p) => {
      try {
        localStorage.setItem(opts.key, String(Math.round(p * 10) / 10));
      } catch (_) {
        /* private mode: remembering is a nicety, the split still works */
      }
    };
    try {
      const saved = parseFloat(localStorage.getItem(opts.key));
      if (isFinite(saved)) apply(clamp(saved));
    } catch (_) {
      /* same */
    }

    let drag = null;
    const set = (clientX) => {
      const r = container.getBoundingClientRect();
      if (!r.width) return drag;
      drag = clamp(((clientX - r.left) / r.width) * 100);
      apply(drag);
      return drag;
    };
    divider.addEventListener('pointerdown', (ev) => {
      drag = clamp(parseFloat(left.style.flexBasis) || RESET);
      set(ev.clientX);
      if (divider.setPointerCapture) divider.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    });
    divider.addEventListener('pointermove', (ev) => {
      if (drag != null) set(ev.clientX);
    });
    divider.addEventListener('pointerup', () => {
      if (drag != null) store(drag);
      drag = null;
    });
    divider.addEventListener('pointercancel', () => {
      drag = null;
    });
    divider.addEventListener('dblclick', () => {
      apply(RESET);
      store(RESET);
      drag = null;
    });
    divider.addEventListener('keydown', (ev) => {
      const cur = parseFloat(left.style.flexBasis) || RESET;
      let p = null;
      if (ev.key === 'ArrowLeft') p = cur - 2;
      else if (ev.key === 'ArrowRight') p = cur + 2;
      else if (ev.key === 'Home') p = RESET;
      if (p == null) return;
      ev.preventDefault();
      p = clamp(p);
      apply(p);
      store(p);
    });
  }

  /* Row handle (gr496860): drag vertically to set the container's height
     in px, remembered under `key`; double-click and Home restore the
     stylesheet default (inline height cleared, key removed). opts:
     {container, handle, key, min, max, step}. */
  function attachHeight(opts) {
    const container = opts.container;
    const handle = opts.handle;
    if (!container || !handle) return;
    const MIN = opts.min ?? 160, MAX = opts.max ?? 4000, STEP = opts.step ?? 24;

    const clamp = (h) => Math.min(MAX, Math.max(MIN, h));
    const apply = (h) => { container.style.height = Math.round(h) + 'px'; };
    const store = (h) => {
      try {
        localStorage.setItem(opts.key, String(Math.round(h)));
      } catch (_) {
        /* remembering is a nicety */
      }
    };
    const reset = () => {
      container.style.height = '';
      try {
        localStorage.removeItem(opts.key);
      } catch (_) {
        /* same */
      }
    };
    try {
      const saved = parseFloat(localStorage.getItem(opts.key));
      if (isFinite(saved)) apply(clamp(saved));
    } catch (_) {
      /* same */
    }

    let drag = null; // {y, h}
    handle.addEventListener('pointerdown', (ev) => {
      drag = {y: ev.clientY, h: container.getBoundingClientRect().height};
      if (handle.setPointerCapture) handle.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    });
    handle.addEventListener('pointermove', (ev) => {
      if (drag) apply(clamp(drag.h + ev.clientY - drag.y));
    });
    handle.addEventListener('pointerup', (ev) => {
      if (!drag) return;
      const h = clamp(drag.h + ev.clientY - drag.y);
      apply(h);
      store(h);
      drag = null;
    });
    handle.addEventListener('pointercancel', () => {
      drag = null;
    });
    handle.addEventListener('dblclick', reset);
    handle.addEventListener('keydown', (ev) => {
      const cur = container.getBoundingClientRect().height;
      let h = null;
      if (ev.key === 'ArrowUp') h = cur - STEP;
      else if (ev.key === 'ArrowDown') h = cur + STEP;
      else if (ev.key === 'Home') {
        ev.preventDefault();
        reset();
        return;
      }
      if (h == null) return;
      ev.preventDefault();
      h = clamp(h);
      apply(h);
      store(h);
    });
  }

  return {attach: attach, attachHeight: attachHeight};
})();
