/* pcb-focus.js — focus mode and pinning on the schematic
   (docs/backlog/pcb-workbench-layout.md slice 1).

   Double-click a part box or a net label in the schematic to focus it: the
   <object> reloads with ?focus=<handle>, so the server dims everything
   outside the target's one-hop neighbourhood while positions stay put
   (static/pcb-focus never moves anything). Pin toggles the focused part's
   placement lock through POST /pcb/{slug}/pin — the lock is a persisted
   column, so the pushpin survives a reload. Esc or the exit button leaves
   focus. Focus is NEVER entered by hover, only a double-click (Reto's rule);
   the <object> is same-origin, so we reach into its contentDocument the way
   pcb-crossprobe.js / pcb-argue.js do. */
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const schemObj = el('pcb-schematic-object');
  if (!schemObj || !schemObj.dataset.base) return;
  const bar = el('pcb-focus-bar');
  const label = el('pcb-focus-label');
  const pinBtn = el('pcb-focus-pin');
  const exitBtn = el('pcb-focus-exit');
  const onlyBtn = el('pcb-focus-only');
  const base = schemObj.dataset.base;
  const pinUrl = schemObj.dataset.pinUrl;
  const cssEsc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);

  // Current focus: {handle, refdes|null}. refdes is set only for a part
  // focus — a net focus has no single part to pin.
  let cur = null;

  function schemDoc() {
    try {
      return schemObj.contentDocument;
    } catch (_) {
      return null;
    }
  }

  function focusUrl(handle) {
    return base + '?focus=' + encodeURIComponent(handle);
  }

  // Slice 2: pcb-sheets.js owns the object's ?sheet=&focus= state when
  // present — reload through it so a sheet switch keeps the focus and
  // focusing keeps the sheet. The direct URL is the fallback when the
  // sheets module is not loaded.
  function reload() {
    if (window.PcbSchem) {
      window.PcbSchem.refresh(schemObj);
      return;
    }
    schemObj.setAttribute('data', cur ? focusUrl(cur.handle) : base);
  }

  function partGroup(refdes) {
    const doc = schemDoc();
    return doc
      ? doc.querySelector('.sch-part[data-pcb-part="' + cssEsc(refdes) + '"]')
      : null;
  }

  function paintPin() {
    if (!pinBtn) return;
    pinBtn.style.display = cur && cur.refdes ? '' : 'none';
    if (!cur || !cur.refdes) return;
    const g = partGroup(cur.refdes);
    const pinned = !!(g && g.hasAttribute('data-pinned'));
    pinBtn.textContent = pinned ? 'unpin' : 'pin';
    pinBtn.setAttribute('aria-pressed', String(pinned));
    pinBtn.dataset.pinned = pinned ? '1' : '0';
  }

  function render() {
    if (bar) {
      bar.classList.toggle('hidden', !cur);
      bar.classList.toggle('inline-flex', !!cur);
    }
    if (cur && label) label.textContent = 'focus ' + cur.handle;
    paintPin();
    paintOnly();
  }

  // 'only connected' (gr520807): ?only=1 asks the renderer to drop the
  // dimmed parts and non-focus pins entirely — the compact relayout a
  // grey-out cannot give. The state rides dataset.only so a sheet
  // switch keeps it (pcb-sheets.js refresh()).
  function paintOnly() {
    if (!onlyBtn) return;
    onlyBtn.setAttribute('aria-pressed', String(!!schemObj.dataset.only));
  }

  function enter(target) {
    cur = target;
    schemObj.dataset.focus = target.handle;
    reload();
    render();
  }

  function exit() {
    if (!cur) return;
    cur = null;
    delete schemObj.dataset.focus;
    delete schemObj.dataset.only;
    reload();
    render();
  }

  // A net-label group carries data-pcb-net but NOT data-pcb-part; a part box
  // and a pin both carry data-pcb-part. So: double-click a net label focuses
  // the net, double-click a pin or box focuses the part.
  function targetFrom(node) {
    if (!node || !node.closest) return null;
    const net = node.closest('[data-pcb-net]');
    if (net && !net.hasAttribute('data-pcb-part')) {
      const h =
        net.getAttribute('data-handle') ||
        'net:' + net.getAttribute('data-pcb-net');
      return { handle: h, refdes: null };
    }
    const part = node.closest('[data-pcb-part]');
    if (part) {
      const rd = part.getAttribute('data-pcb-part');
      return { handle: rd, refdes: rd };
    }
    return null;
  }

  function bindDoc() {
    const doc = schemDoc();
    if (doc && !doc.__pcbFocusBound) {
      doc.__pcbFocusBound = true;
      doc.addEventListener('dblclick', (ev) => {
        const t = targetFrom(ev.target);
        if (t) {
          ev.preventDefault();
          enter(t);
        }
      });
    }
    // on every (re)load, re-sync the pin button with the fresh render
    paintPin();
  }

  schemObj.addEventListener('load', bindDoc);
  bindDoc();

  if (exitBtn) exitBtn.addEventListener('click', exit);
  if (onlyBtn) {
    onlyBtn.addEventListener('click', () => {
      if (schemObj.dataset.only) delete schemObj.dataset.only;
      else schemObj.dataset.only = '1';
      if (cur) reload();
      paintOnly();
    });
    paintOnly();
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') exit();
  });

  if (pinBtn) {
    pinBtn.addEventListener('click', () => {
      if (!cur || !cur.refdes || !pinUrl) return;
      const want = pinBtn.dataset.pinned !== '1';
      const body = new URLSearchParams();
      body.set('refdes', cur.refdes);
      body.set('pinned', want ? '1' : '0');
      pinBtn.disabled = true;
      fetch(pinUrl, { method: 'POST', body: body })
        .then((r) => (r.ok ? r.json() : Promise.reject(r)))
        .then(() => {
          // reload the focus render so the pushpin appears / disappears
          reload();
        })
        .catch(() => {
          /* leave the button unchanged on failure */
        })
        .finally(() => {
          pinBtn.disabled = false;
        });
    });
  }
})();
