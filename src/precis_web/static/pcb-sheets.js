/* pcb-sheets.js — the workbench's schematic sheets (slice 2 of
   docs/backlog/pcb-workbench-layout.md): one tab per placement group
   plus the overview.

   This module owns the schematic <object>'s URL state: dataset.sheet
   ('' = overview), dataset.focus (pcb-focus.js's handle) and
   dataset.only ('1' = hide non-focus, gr520807) on the object element,
   rebuilt into ?sheet=&focus=&only= on refresh(). pcb-focus
   reloads through refresh() so a sheet switch keeps an active focus
   and a focus keeps the current sheet; the cross-probe calls pickSheet
   to switch the pane to the sheet holding a clicked object.

   The part→sheet map is the page's #pcb-sheet-map JSON — the same
   authored "group" key schematic.sheet_names reads server-side. */
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const obj = el('pcb-schematic-object');
  if (!obj || !obj.dataset.base) return;
  const tabs = el('pcb-sheet-tabs');
  const mapEl = el('pcb-sheet-map');
  let map = {};
  try { map = mapEl ? JSON.parse(mapEl.textContent) : {}; } catch (_) { map = {}; }

  // Rebuild the object's data URL from its dataset.sheet/.focus/.only —
  // the single place the three params are combined.
  function refresh() {
    const p = new URLSearchParams();
    if (obj.dataset.sheet) p.set('sheet', obj.dataset.sheet);
    if (obj.dataset.focus) p.set('focus', obj.dataset.focus);
    if (obj.dataset.only) p.set('only', obj.dataset.only);
    const q = p.toString();
    obj.setAttribute('data', obj.dataset.base + (q ? '?' + q : ''));
  }

  function paintTabs() {
    if (!tabs) return;
    const cur = obj.dataset.sheet || '';
    for (const btn of tabs.querySelectorAll('button[data-sheet]')) {
      btn.setAttribute('aria-pressed', String(btn.dataset.sheet === cur));
    }
  }

  function setSheet(name) {
    const want = name || '';
    if ((obj.dataset.sheet || '') === want) return false;
    if (want) obj.dataset.sheet = want;
    else delete obj.dataset.sheet;
    paintTabs();
    refresh();
    return true;
  }

  if (tabs) {
    tabs.addEventListener('click', (ev) => {
      const btn = ev.target.closest && ev.target.closest('button[data-sheet]');
      if (btn) setSheet(btn.dataset.sheet);
    });
    paintTabs();
  }

  window.PcbSchem = {
    refresh,
    // The sheet a cross-probed object lives on ('' = overview). parts:
    // candidate refdes list (a part's own, or a net's members). The
    // current sheet wins when it already holds one of them — a
    // cross-sheet net stays put rather than bouncing tabs; otherwise the
    // alphabetically first holding sheet, else the overview.
    pickSheet(parts) {
      const cur = obj.dataset.sheet || '';
      const hold = new Set();
      for (const r of parts || []) {
        if (map[r] !== undefined) hold.add(map[r]);
      }
      if (!parts || !parts.length) return cur;
      if (cur === '') return [...hold].sort()[0] || '';
      if (hold.has(cur)) return cur;
      return [...hold].sort()[0] || '';
    },
    setSheet,
    get sheet() { return obj.dataset.sheet || ''; },
    sheetOf: (refdes) => map[refdes] || '',
  };
})();
