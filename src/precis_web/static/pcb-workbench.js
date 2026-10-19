/* pcb-workbench.js — the two-pane pcb workbench's page glue
   (docs/backlog/pcb-workbench-layout.md slice 1): the draggable divider
   whose split is remembered per user, the layout flip button, and
   attaching static/viewport.js to both panes. */
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const wb = el('pcb-workbench');
  if (!wb) return;
  const left = el('pcb-pane-schematic');
  const layout = el('pcb-pane-layout');
  const divider = el('pcb-divider');
  const flip = el('pcb-flip');
  const boardObj = el('pcb-board-object');
  const schemObj = el('pcb-schematic-object');
  const LS_KEY = 'precis:pcb-workbench:left-pct';
  const MIN = 15, MAX = 85;

  const clamp = (p) => Math.min(MAX, Math.max(MIN, p));
  const apply = (p) => {
    if (left) left.style.flexBasis = p + '%';
  };
  const store = (p) => {
    try {
      localStorage.setItem(LS_KEY, String(Math.round(p * 10) / 10));
    } catch (_) {
      /* private mode: remembering is a nicety, the split still works */
    }
  };
  try {
    const saved = parseFloat(localStorage.getItem(LS_KEY));
    if (isFinite(saved)) apply(clamp(saved));
  } catch (_) {
    /* same */
  }

  if (divider && left) {
    let drag = null;
    const set = (clientX) => {
      const r = wb.getBoundingClientRect();
      if (!r.width) return drag;
      drag = clamp(((clientX - r.left) / r.width) * 100);
      apply(drag);
      return drag;
    };
    divider.addEventListener('pointerdown', (ev) => {
      drag = clamp(parseFloat(left.style.flexBasis) || 50);
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
      drag = 50;
      apply(50);
      store(50);
      drag = null;
    });
    divider.addEventListener('keydown', (ev) => {
      const cur = parseFloat(left.style.flexBasis) || 50;
      let p = null;
      if (ev.key === 'ArrowLeft') p = cur - 2;
      else if (ev.key === 'ArrowRight') p = cur + 2;
      else if (ev.key === 'Home') p = 50;
      if (p == null) return;
      ev.preventDefault();
      drag = clamp(p);
      apply(drag);
      store(drag);
      drag = null;
    });
  }

  // gr496860: the row-resize handle under the workbench, mirroring the
  // divider above — drag sets the height in px, remembered per user,
  // double-click resets to the stylesheet default, arrows adjust.
  const heightEl = el('pcb-height');
  const H_KEY = 'precis:pcb-workbench:height-px';
  const H_MIN = 160, H_MAX = 4000;
  const applyH = (h) => { wb.style.height = Math.round(h) + 'px'; };
  const storeH = (h) => {
    try {
      localStorage.setItem(H_KEY, String(Math.round(h)));
    } catch (_) {
      /* remembering is a nicety */
    }
  };
  const resetH = () => {
    wb.style.height = '';
    try {
      localStorage.removeItem(H_KEY);
    } catch (_) {
      /* same */
    }
  };
  if (heightEl) {
    const clampH = (h) => Math.min(H_MAX, Math.max(H_MIN, h));
    try {
      const saved = parseFloat(localStorage.getItem(H_KEY));
      if (isFinite(saved)) applyH(clampH(saved));
    } catch (_) {
      /* same */
    }
    let hdrag = null; // {y, h}
    heightEl.addEventListener('pointerdown', (ev) => {
      hdrag = {y: ev.clientY, h: wb.getBoundingClientRect().height};
      if (heightEl.setPointerCapture) heightEl.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    });
    heightEl.addEventListener('pointermove', (ev) => {
      if (hdrag) applyH(clampH(hdrag.h + ev.clientY - hdrag.y));
    });
    heightEl.addEventListener('pointerup', (ev) => {
      if (!hdrag) return;
      const h = clampH(hdrag.h + ev.clientY - hdrag.y);
      applyH(h);
      storeH(h);
      hdrag = null;
    });
    heightEl.addEventListener('pointercancel', () => {
      hdrag = null;
    });
    heightEl.addEventListener('dblclick', resetH);
    heightEl.addEventListener('keydown', (ev) => {
      const cur = wb.getBoundingClientRect().height;
      let h = null;
      if (ev.key === 'ArrowUp') h = cur - 24;
      else if (ev.key === 'ArrowDown') h = cur + 24;
      else if (ev.key === 'Home') {
        ev.preventDefault();
        resetH();
        return;
      }
      if (h == null) return;
      ev.preventDefault();
      h = clampH(h);
      applyH(h);
      storeH(h);
    });
  }

  // Both panes share viewport.js. The layout pane's toolbar holds the
  // data-shortcut keys until the pointer picks a pane — activation moves
  // them, so +/-, f and 0 always act under the cursor.
  if (window.PrecisViewport) {
    if (schemObj && left) window.PrecisViewport.attach(schemObj, {host: left});
    if (boardObj && layout) {
      window.PrecisViewport.attach(boardObj, {host: layout, active: true});
    }
  }

  // gr497528: the layer legend is pinned and fixed-size by viewport.js; this
  // button hides/shows it, remembered per user. The board <object> reloads
  // on flip, so the stored state is re-applied on every load.
  const layersBtn = el('pcb-layers-toggle');
  const LAYERS_KEY = 'precis:pcb-workbench:layers-hidden';
  if (layersBtn && boardObj) {
    let hidden = false;
    try {
      hidden = localStorage.getItem(LAYERS_KEY) === '1';
    } catch (_) {
      /* same */
    }
    const paint = () => {
      layersBtn.setAttribute('aria-pressed', String(!hidden));
      layersBtn.textContent = hidden ? 'show layers' : 'hide layers';
      let doc = null;
      try {
        doc = boardObj.contentDocument;
      } catch (_) {
        doc = null;
      }
      const svg = doc && doc.documentElement;
      if (svg && svg.__precisVp) svg.__precisVp.setLegendHidden(hidden);
    };
    paint();
    boardObj.addEventListener('load', paint);
    layersBtn.addEventListener('click', () => {
      hidden = !hidden;
      try {
        localStorage.setItem(LAYERS_KEY, hidden ? '1' : '0');
      } catch (_) {
        /* same */
      }
      paint();
    });
  }

  // Flip re-requests the same shown snapshot with flip=1: the bottom view,
  // mirrored, B.* layers on top (gerber_view._FLIPPED_STACK). Selection
  // identity is unchanged, so a selection survives the flip.
  if (flip && boardObj && boardObj.dataset.base) {
    flip.addEventListener('click', () => {
      const next = flip.getAttribute('aria-pressed') !== 'true';
      boardObj.setAttribute(
        'data',
        boardObj.dataset.base + (next ? '&flip=1' : '')
      );
      flip.setAttribute('aria-pressed', String(next));
      flip.textContent = next ? 'top view' : 'bottom view';
    });
  }
})();
