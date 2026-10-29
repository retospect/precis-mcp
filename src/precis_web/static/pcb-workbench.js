/* pcb-workbench.js — the two-pane pcb workbench's page glue
   (docs/backlog/pcb-workbench-layout.md, the "Decided" layout): the
   draggable divider whose split is remembered per user, the layout flip
   button, and attaching static/viewport.js to both panes. Focus mode and
   pinning (slice 1) live in static/pcb-focus.js. */
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

  // gr518348: a fab render is model assembly + gerber export + reparse —
  // seconds on a real board. Show an elapsed-time note over the pane
  // while the <object> is in flight: on first paint and again whenever
  // its data URL is swapped (flip, focus). Hidden again on load/error.
  const loadNote = el('pcb-board-loading');
  if (boardObj && loadNote) {
    const secs = loadNote.querySelector('span');
    let t0 = 0;
    let timer = null;
    const show = () => {
      t0 = Date.now();
      secs.textContent = '0';
      loadNote.hidden = false;
      if (timer) clearInterval(timer);
      timer = setInterval(() => {
        secs.textContent = String(Math.round((Date.now() - t0) / 1000));
      }, 500);
    };
    const hide = () => {
      loadNote.hidden = true;
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    boardObj.addEventListener('load', hide);
    boardObj.addEventListener('error', hide);
    new MutationObserver(show).observe(boardObj, {
      attributes: true,
      attributeFilter: ['data'],
    });
    // A fast first load can beat this deferred script — only show when
    // the document is not in yet.
    if (!(boardObj.contentDocument && boardObj.contentDocument.documentElement)) {
      show();
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

  // gr531205: the ratsnest master toggle — one button in the layout
  // toolbar hides/shows every airwire (the SVG's own legend row toggles
  // the same rs-off class; both stay in sync through it). Remembered per
  // user, re-applied on every load (flip reloads the object). Default ON:
  // the unrouted overlay is the point of the fab view.
  const rsBtn = el('pcb-ratsnest-toggle');
  const RS_KEY = 'precis:pcb-workbench:ratsnest-hidden';
  if (rsBtn && boardObj) {
    let hidden = false;
    try {
      hidden = localStorage.getItem(RS_KEY) === '1';
    } catch (_) {
      /* same */
    }
    const paint = () => {
      let doc = null;
      try {
        doc = boardObj.contentDocument;
      } catch (_) {
        doc = null;
      }
      const svg = doc && doc.documentElement;
      // A fully-routed board has no ratsnest group — the toggle does
      // nothing there, so say so on the button.
      const has = !!(doc && doc.getElementById('ratsnest'));
      rsBtn.disabled = !has;
      rsBtn.setAttribute('aria-pressed', String(has && !hidden));
      rsBtn.textContent = has ? `ratsnest: ${hidden ? 'off' : 'on'}` : 'ratsnest: none';
      if (svg) svg.classList.toggle('rs-off', hidden);
      const row = doc && doc.getElementById('legend-ratsnest');
      if (row) row.classList.toggle('off', hidden);
    };
    paint();
    boardObj.addEventListener('load', paint);
    rsBtn.addEventListener('click', () => {
      hidden = !hidden;
      try {
        localStorage.setItem(RS_KEY, hidden ? '1' : '0');
      } catch (_) {
        /* same */
      }
      paint();
    });
  }

  // gr519469: a stale shown token answers board.svg with a 409 whose text
  // body the <object> paints raw. When the loaded document isn't the svg
  // and its text says the board changed, offer a page reload — reloading
  // just the object would leave the embedded selection manifest stale.
  const stale = el('pcb-board-stale');
  if (stale && boardObj) {
    const staleText = /changed|reload/i;
    const check = () => {
      let doc = null;
      try {
        doc = boardObj.contentDocument;
      } catch (_) {
        doc = null;
      }
      const root = doc && doc.documentElement;
      const text = (doc && doc.body && doc.body.textContent) || '';
      const bad = !(root && root.localName === 'svg') && staleText.test(text);
      stale.classList.toggle('hidden', !bad);
    };
    check();
    boardObj.addEventListener('load', check);
    const reloadBtn = el('pcb-board-reload');
    if (reloadBtn) {
      reloadBtn.addEventListener('click', () => window.location.reload());
    }
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
