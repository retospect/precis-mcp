/* viewport.js — shared pan/zoom for the SVG viewer panes (the pcb
   workbench now, se's 2D views in a later slice; see
   docs/backlog/pcb-workbench-layout.md's gesture table).

   All zoom is a viewBox rewrite — geometry and text stay vector-crisp at
   any scale, and there is no transform state to drift. The gestures:

     wheel or pinch            zoom toward the cursor
     trackpad two-finger       pan
     drag on empty space       pan
     right/middle/space drag   pan (anywhere)
     double-click empty space  fit to view

   The keys (f fit, 0 reset, +/- zoom) are not handled here: each pane's
   toolbar buttons carry data-vp actions, and the ACTIVE pane's buttons
   carry data-shortcut so the outer document's shortcuts.js dispatches
   and lists them (`?`). Key events inside an <object> document never
   reach the outer document, so a keydown there is re-issued on it as an
   event — still shortcuts.js's one key handler, this is only a bridge.

   UMD: attach() in the browser, the pure parts unit-tested in node. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PrecisViewport = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const KEY_STEP = 1.25;    // one +/- press or toolbar click
  const WHEEL_PX = 0.0022;  // zoom exponent per deltaY pixel (wheel, pinch)
  const WHEEL_LINE = 0.09;  // per deltaY line (Firefox's deltaMode=1)
  const TRACKPAD_MAX = 40;  // |deltaY| below this reads as two-finger scroll
  const DRAG_MIN = 4;       // px of travel that turns a click into a pan
  const MAX_IN = 64, MAX_OUT = 0.25;  // zoom range relative to the base box

  const VP_KEYS = {in: '+', out: '-', fit: 'f', reset: '0'};

  const parseBox = (s) => {
    const p = String(s || '').trim().split(/[\s,]+/).map(Number);
    return p.length === 4 && p.every(isFinite) && p[2] > 0 && p[3] > 0
      ? {x: p[0], y: p[1], w: p[2], h: p[3]}
      : null;
  };
  const boxAttr = (b) =>
    [b.x, b.y, b.w, b.h].map((v) => +v.toFixed(5)).join(' ');

  /* The svg fits its viewport with xMidYMid meet letterboxing; screen
     (client) coordinates convert to viewBox coordinates through the
     letterboxed scale and offset. */
  function screenScale(svg, box) {
    const r = svg.getBoundingClientRect();
    if (!r.width || !r.height || !box.w || !box.h) {
      return {s: 1, ox: 0, oy: 0};
    }
    const s = Math.min(r.width / box.w, r.height / box.h);
    return {
      s,
      ox: r.left + (r.width - box.w * s) / 2,
      oy: r.top + (r.height - box.h * s) / 2,
    };
  }

  function toBox(svg, box, clientX, clientY) {
    const m = screenScale(svg, box);
    return {x: box.x + (clientX - m.ox) / m.s, y: box.y + (clientY - m.oy) / m.s};
  }

  function zoomAt(box, factor, px, py) {
    return {
      x: px - (px - box.x) / factor,
      y: py - (py - box.y) / factor,
      w: box.w / factor,
      h: box.h / factor,
    };
  }

  /* ctrl/cmd+wheel is a pinch on every platform; a notched mouse wheel
     (big deltaY, or line-mode) zooms; the small continuous pixel deltas
     of a trackpad two-finger scroll pan instead. */
  function classifyWheel(ev) {
    if (ev.ctrlKey || ev.metaKey) return 'zoom';
    if (
      ev.deltaMode === 0 &&
      (Math.abs(ev.deltaX) > 0 || Math.abs(ev.deltaY) < TRACKPAD_MAX)
    ) {
      return 'pan';
    }
    return 'zoom';
  }

  /* Objects a plain left-drag must NOT pan: anything with a handle or a
     selectable-object identity (click selects/argues there), and the
     legend rows (click toggles a layer). */
  const onObject = (t) =>
    !!(t && t.closest && t.closest('[data-handle],[data-pcb-object],.legend-row'));

  function Controller(svg, target) {
    this.svg = svg;
    this.target = target || svg;
    const doc = target && target.nodeType === 9 ? target : svg.ownerDocument;
    this.win = (doc && doc.defaultView) || null;
    const base =
      parseBox(svg.getAttribute('viewBox')) || {
        x: 0,
        y: 0,
        w: parseFloat(svg.getAttribute('width')) || 100,
        h: parseFloat(svg.getAttribute('height')) || 100,
      };
    this.base = base;
    this.box = {x: base.x, y: base.y, w: base.w, h: base.h};
    // The document fills its pane; from here on the viewBox alone is the
    // camera.
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    // A layer legend (.legend) is HUD, not board: pinned to the pane's
    // top-left at a fixed pixel size however the camera moves or the pane
    // is resized (gr497528).
    this.legend = svg.querySelector ? svg.querySelector('.legend') : null;
    this.space = false;
    this.pointers = new Map();
    this.drag = null;
    this.pinch = null;
    this.suppress = false;
    this._bind();
    this.pinLegend();
    if (this.win && this.win.addEventListener) {
      this.win.addEventListener('resize', () => this.pinLegend());
    }
  }

  /* The legend is authored in document pixels, but it lives inside the
     viewBox, so zoom would scale it and pan would carry it away. Counter
     the camera: put its origin at the svg element's top-left corner in
     viewBox units and scale by one CSS pixel in viewBox units. */
  Controller.prototype.pinLegend = function () {
    if (!this.legend) return;
    const r = this.svg.getBoundingClientRect();
    const m = screenScale(this.svg, this.box);
    const x = this.box.x - (m.ox - r.left) / m.s;
    const y = this.box.y - (m.oy - r.top) / m.s;
    this.legend.setAttribute(
      'transform',
      `translate(${+x.toFixed(5)} ${+y.toFixed(5)}) scale(${+(1 / m.s).toFixed(6)})`
    );
  };

  Controller.prototype.setLegendHidden = function (hidden) {
    if (this.legend) this.legend.style.display = hidden ? 'none' : '';
  };

  Controller.prototype.setBox = function (b) {
    const minW = this.base.w / MAX_IN;
    const maxW = this.base.w / MAX_OUT;
    if (b.w < minW || b.w > maxW) {
      const w = Math.min(maxW, Math.max(minW, b.w));
      const k = w / b.w;
      const cx = b.x + b.w / 2;
      const cy = b.y + b.h / 2;
      b = {x: cx - w / 2, y: cy - (b.h * k) / 2, w, h: b.h * k};
    }
    this.box = b;
    this.svg.setAttribute('viewBox', boxAttr(b));
    this.pinLegend();
  };

  Controller.prototype.zoomBy = function (factor, clientX, clientY) {
    let px, py;
    if (clientX == null) {
      px = this.box.x + this.box.w / 2;
      py = this.box.y + this.box.h / 2;
    } else {
      const p = toBox(this.svg, this.box, clientX, clientY);
      px = p.x;
      py = p.y;
    }
    this.setBox(zoomAt(this.box, factor, px, py));
  };

  Controller.prototype.panScreen = function (dx, dy) {
    const s = screenScale(this.svg, this.box).s;
    const b = this.box;
    this.setBox({x: b.x - dx / s, y: b.y - dy / s, w: b.w, h: b.h});
  };

  Controller.prototype.fit = function () {
    this.setBox({x: this.base.x, y: this.base.y, w: this.base.w, h: this.base.h});
  };
  /* The authored viewBox is the fitted view, so reset IS fit here. */
  Controller.prototype.reset = Controller.prototype.fit;

  Controller.prototype.action = function (name) {
    if (name === 'in') this.zoomBy(KEY_STEP);
    else if (name === 'out') this.zoomBy(1 / KEY_STEP);
    else this.fit();
  };

  Controller.prototype._down = function (ev) {
    const id = ev.pointerId == null ? 0 : ev.pointerId;
    this.pointers.set(id, {x: ev.clientX, y: ev.clientY});
    if (this.pointers.size === 2) {
      const [a, b] = [...this.pointers.values()];
      this.pinch = {d: Math.hypot(a.x - b.x, a.y - b.y) || 1};
      this.drag = null;
      return;
    }
    if (this.pointers.size > 2) return;
    const pan =
      ev.button === 1 ||
      ev.button === 2 ||
      this.space ||
      (ev.button === 0 && !onObject(ev.target));
    if (pan) {
      this.drag = {id, x: ev.clientX, y: ev.clientY, moved: 0};
      if (ev.button !== 0 || this.space) ev.preventDefault();
    }
  };

  Controller.prototype._move = function (ev) {
    const id = ev.pointerId == null ? 0 : ev.pointerId;
    const p = this.pointers.get(id);
    if (p) {
      p.x = ev.clientX;
      p.y = ev.clientY;
    }
    if (this.pinch && this.pointers.size >= 2) {
      const [a, b] = [...this.pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const factor = d / this.pinch.d;
      this.pinch.d = d;
      this.suppress = true;
      this.zoomBy(factor, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (this.drag && this.drag.id === id) {
      const dx = ev.clientX - this.drag.x;
      const dy = ev.clientY - this.drag.y;
      this.drag.x = ev.clientX;
      this.drag.y = ev.clientY;
      this.drag.moved += Math.abs(dx) + Math.abs(dy);
      this.panScreen(dx, dy);
    }
  };

  Controller.prototype._up = function (ev) {
    const id = ev.pointerId == null ? 0 : ev.pointerId;
    this.pointers.delete(id);
    if (this.pinch && this.pointers.size < 2) this.pinch = null;
    if (this.drag && this.drag.id === id) {
      if (this.drag.moved > DRAG_MIN) this.suppress = true;
      this.drag = null;
    }
  };

  /* Space is tracked locally for space+drag. Every other key inside an
     <object> document is re-issued on the outer document — shortcuts.js
     owns dispatch and the `?` list; a listener there would be a second
     key handler, an event here is only a bridge across the document
     boundary. */
  Controller.prototype._key = function (ev, down) {
    if (ev.key === ' ') {
      this.space = down;
      if (down) ev.preventDefault();
      return;
    }
    if (!down) return;
    const parent = this.win && this.win.parent;
    const sc = parent && parent.precisShortcuts;
    if (!sc || (sc.ignored && sc.ignored(ev))) return;
    ev.preventDefault();
    parent.document.dispatchEvent(
      new parent.KeyboardEvent('keydown', {key: ev.key})
    );
  };

  Controller.prototype._bind = function () {
    const t = this.target;
    t.addEventListener(
      'wheel',
      (ev) => {
        ev.preventDefault();
        if (classifyWheel(ev) === 'pan') {
          this.panScreen(ev.deltaX, ev.deltaY);
          return;
        }
        const unit = ev.deltaMode === 1 ? WHEEL_LINE : WHEEL_PX;
        this.zoomBy(Math.exp(-ev.deltaY * unit), ev.clientX, ev.clientY);
      },
      {passive: false}
    );
    t.addEventListener('pointerdown', (ev) => this._down(ev));
    t.addEventListener('pointermove', (ev) => this._move(ev));
    t.addEventListener('pointerup', (ev) => this._up(ev));
    t.addEventListener('pointercancel', (ev) => this._up(ev));
    t.addEventListener('dblclick', (ev) => {
      if (!onObject(ev.target)) this.fit();
    });
    /* Right-drag pans; the context menu would fire on release. */
    t.addEventListener('contextmenu', (ev) => ev.preventDefault());
    /* A drag that became a pan must not also land as a select click. */
    t.addEventListener(
      'click',
      (ev) => {
        if (this.suppress) {
          ev.preventDefault();
          ev.stopImmediatePropagation();
          this.suppress = false;
        }
      },
      true
    );
    /* Only an <object> document needs the key bridge; an inline svg's
       keys already land on the outer document. */
    if (t.nodeType === 9) {
      t.addEventListener('keydown', (ev) => this._key(ev, true));
      t.addEventListener('keyup', (ev) => this._key(ev, false));
    }
  };

  /* ── panes and their shortcut keys ─────────────────────────────── */
  const panes = [];

  /* data-shortcut lives on the ACTIVE pane's buttons only, so the outer
     shortcuts.js clicks into the pane under the pointer and `?` lists
     each key once. */
  function activate(entry) {
    for (const p of panes) {
      for (const btn of p.buttons) {
        if (p === entry) btn.setAttribute('data-shortcut', VP_KEYS[btn.dataset.vp]);
        else btn.removeAttribute('data-shortcut');
      }
    }
  }

  /* Attach to one viewer: an <object>/<iframe> whose document IS an svg,
     or an inline <svg> element. host is the pane element that carries the
     [data-vp] toolbar buttons; it activates on hover/touch. Re-attaches
     when the object reloads (the flip button). Returns nothing. */
  function attach(obj, opts) {
    if (!obj) return;
    opts = opts || {};
    const host = opts.host || obj;
    const entry = {
      obj,
      host,
      ctl: null,
      buttons: host.querySelectorAll
        ? [...host.querySelectorAll('[data-vp]')]
        : [],
    };
    panes.push(entry);
    for (const btn of entry.buttons) {
      btn.addEventListener('click', () => {
        if (entry.ctl) entry.ctl.action(btn.dataset.vp);
      });
    }
    const activateMe = () => activate(entry);
    host.addEventListener('pointerenter', activateMe);
    host.addEventListener('pointerdown', activateMe, true);
    host.addEventListener('focusin', activateMe);
    const wire = () => {
      let doc = null;
      let svg = null;
      if (obj.tagName === 'svg') {
        svg = obj;
        doc = null;
      } else {
        try {
          doc = obj.contentDocument;
        } catch (_) {
          doc = null;
        }
        svg = doc && doc.documentElement && doc.documentElement.tagName === 'svg'
          ? doc.documentElement
          : null;
      }
      if (!svg || svg.__precisVpEntry === entry) return;
      svg.__precisVpEntry = entry;
      entry.ctl = new Controller(svg, doc || svg);
      svg.__precisVp = entry.ctl;
    };
    if (obj.tagName !== 'svg') obj.addEventListener('load', wire);
    wire();
    if (opts.active) activate(entry);
  }

  return {attach, Controller, _parseBox: parseBox, _zoomAt: zoomAt, _toBox: toBox, _classifyWheel: classifyWheel, _VP_KEYS: VP_KEYS};
});
