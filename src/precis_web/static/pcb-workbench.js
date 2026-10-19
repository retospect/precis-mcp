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
  // The column divider and the row-resize handle (gr496860) come from the
  // shared splitter, static/pane-split.js — same ids and localStorage keys
  // as before; pane-split.js must load ahead of this file (defer order).
  if (window.PrecisPaneSplit) {
    window.PrecisPaneSplit.attach({
      container: wb,
      left: left,
      divider: divider,
      key: 'precis:pcb-workbench:left-pct',
    });
    window.PrecisPaneSplit.attachHeight({
      container: wb,
      handle: el('pcb-height'),
      key: 'precis:pcb-workbench:height-px',
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
