// Keyboard shortcuts for the web UI. Plain JS, no dependencies, loaded
// `defer` from base.html.j2 beside tour.js.
//
// Contract with templates: an element opts in with
//   data-shortcut="<keys>"        "/", "e", or a chord such as "g d"
//   data-shortcut-label="<name>"  optional; falls back to aria-label,
//                                 title, then the element's text
// and the key does what a click (or, for inputs, a focus) on it does.
// `?` builds a <dialog> from whatever [data-shortcut] the page carries at
// that moment, so the help list can never drift from the page.

(function () {
  'use strict';

  const CHORD_WINDOW_MS = 1500;
  const CHORD_LEAD = 'g';

  // Same guard as the graph page's key handler: never steal typing, never
  // fight a browser/OS shortcut, never fire on auto-repeat or an IME.
  function ignored(ev) {
    const t = ev.target;
    return (
      ev.ctrlKey || ev.metaKey || ev.altKey ||
      ev.isComposing || ev.repeat || ev.defaultPrevented ||
      !!(t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)))
    );
  }

  function labelOf(el) {
    return (
      el.dataset.shortcutLabel ||
      el.getAttribute('aria-label') ||
      (el.getAttribute('title') || '').replace(/\s*\([^)]*\)\s*$/, '') ||
      (el.textContent || '').trim().replace(/\s+/g, ' ')
    );
  }

  function visible(el) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  // Elements bound to `keys`. The page's own control wins over the header's
  // (Drive's query box beats the global search), so page content is listed
  // before anything inside <header>.
  // `data-key` is the older single-key spelling (graph pages; space-separated
  // aliases such as "u Backspace"); it stays an alias for one slice.
  function bound(el, keys) {
    if (el.dataset.shortcut === keys) return true;
    return !!el.dataset.key && el.dataset.key.split(' ').includes(keys);
  }

  function targets(keys) {
    const all = [...document.querySelectorAll('[data-shortcut],[data-key]')]
      .filter((e) => bound(e, keys));
    const inHeader = (e) => (e.closest('header') ? 1 : 0);
    return all.sort((a, b) => inHeader(a) - inHeader(b));
  }

  function fire(el) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      el.focus();
      if (typeof el.select === 'function') el.select();
    } else {
      el.click();
    }
  }

  function trigger(keys) {
    const list = targets(keys);
    // Prefer a visible target; a collapsed dropdown anchor still navigates.
    const el = list.find(visible) || list[0];
    if (!el) return false;
    fire(el);
    return true;
  }

  // ── list cursor ────────────────────────────────────────────────────
  // j/k walk the [data-row] elements inside [data-shortcut-scope="list"];
  // the cursor is a .kb-cursor class, not focus, so a row's own links and
  // checkbox stay where the mouse expects them. Graph pages have no list
  // scope: there j/k walk the node anchors in ring order and use real focus,
  // because the peek card follows focusin.
  function listRows() {
    const rows = [];
    for (const scope of document.querySelectorAll('[data-shortcut-scope="list"]')) {
      for (const r of scope.querySelectorAll('[data-row]')) if (visible(r)) rows.push(r);
    }
    return rows;
  }

  function ringNodes() {
    return [...document.querySelectorAll('a[data-ring]')]
      .sort((x, y) => x.dataset.ring - y.dataset.ring);
  }

  function moveCursor(step) {
    const rows = listRows();
    if (!rows.length) {
      const nodes = ringNodes();
      if (!nodes.length) return false;
      const i = nodes.indexOf(document.activeElement);
      nodes[i < 0 ? 0 : (i + (step > 0 ? 1 : nodes.length - 1)) % nodes.length].focus();
      return true;
    }
    const cur = document.querySelector('.kb-cursor');
    const i = rows.indexOf(cur);
    const n = i < 0 ? (step > 0 ? 0 : rows.length - 1)
                    : Math.min(rows.length - 1, Math.max(0, i + step));
    if (cur) cur.classList.remove('kb-cursor');
    rows[n].classList.add('kb-cursor');
    rows[n].scrollIntoView({ block: 'nearest' });
    return true;
  }

  function cursorRow() {
    const cur = document.querySelector('.kb-cursor');
    return cur && listRows().includes(cur) ? cur : null;
  }

  function openRow(row) {
    if (row.dataset.rowHref) { window.location.assign(row.dataset.rowHref); return; }
    const a = row.matches('a[href]') ? row
      : (row.querySelector('[data-row-open]') || row.querySelector('a[href]'));
    if (a) a.click();
  }

  function toggleRow(row) {
    const box = row.querySelector('input[type="checkbox"]');
    if (box) box.click();
  }

  // ── help overlay ───────────────────────────────────────────────────
  let dialog = null;

  function closeHelp() {
    if (dialog && dialog.open) dialog.close();
  }

  function openHelp() {
    if (dialog) dialog.remove();
    const rows = new Map();
    for (const el of document.querySelectorAll('[data-shortcut]')) {
      const keys = el.dataset.shortcut;
      if (!rows.has(keys)) rows.set(keys, labelOf(el));
    }
    if (document.querySelector('[data-shortcut-scope="list"] [data-row]')) {
      rows.set('j', 'Next row');
      rows.set('k', 'Previous row');
      rows.set('Enter', 'Open row');
      rows.set('o', 'Open row');
      rows.set('x', 'Tick row');
    }
    rows.set('?', 'This help');
    rows.set('Esc', 'Close / leave a field');

    dialog = document.createElement('dialog');
    dialog.setAttribute('aria-label', 'Keyboard shortcuts');
    dialog.className =
      'rounded-lg border border-line bg-surface text-fg shadow-xl p-4 max-w-md w-full';
    const h = document.createElement('h2');
    h.className = 'text-sm font-semibold mb-2';
    h.textContent = 'Keyboard shortcuts';
    dialog.appendChild(h);
    const dl = document.createElement('dl');
    dl.className = 'grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm';
    for (const [keys, label] of rows) {
      const dt = document.createElement('dt');
      for (const k of keys.split(' ')) {
        const kbd = document.createElement('kbd');
        kbd.className = 'font-mono text-xs border border-line rounded px-1 mr-1';
        kbd.textContent = k;
        dt.appendChild(kbd);
      }
      const dd = document.createElement('dd');
      dd.className = 'text-fg-muted';
      dd.textContent = label;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    dialog.appendChild(dl);
    dialog.addEventListener('click', (e) => { if (e.target === dialog) closeHelp(); });
    dialog.addEventListener('close', () => { dialog.remove(); dialog = null; });
    document.body.appendChild(dialog);
    dialog.showModal();
  }

  // ── key handling ───────────────────────────────────────────────────
  let pending = null;  // timer id while a g-chord is armed

  function disarm() {
    if (pending !== null) { clearTimeout(pending); pending = null; }
  }

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      // Native <dialog> closes itself on Esc; otherwise leave a field.
      disarm();
      const t = document.activeElement;
      if (t && t !== document.body && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) t.blur();
      return;
    }
    if (ignored(ev)) return;

    if (pending !== null) {
      disarm();
      if (trigger(CHORD_LEAD + ' ' + ev.key)) ev.preventDefault();
      return;
    }
    if (ev.key === CHORD_LEAD) {
      pending = setTimeout(disarm, CHORD_WINDOW_MS);
      return;
    }
    if (ev.key === '?') {
      ev.preventDefault();
      if (dialog && dialog.open) closeHelp(); else openHelp();
      return;
    }
    if (document.querySelector('dialog[open]')) return;
    if (ev.key === 'j' || ev.key === 'k') {
      if (moveCursor(ev.key === 'j' ? 1 : -1)) ev.preventDefault();
      return;
    }
    if (ev.key === 'Enter' || ev.key === 'o' || ev.key === 'x') {
      // Enter on a deliberately focused link/button keeps its native action.
      const t = document.activeElement;
      if (ev.key === 'Enter' && t && t !== document.body) return;
      const row = cursorRow();
      if (row) {
        ev.preventDefault();
        if (ev.key === 'x') toggleRow(row); else openRow(row);
        return;
      }
    }
    if (trigger(ev.key)) ev.preventDefault();
  });

  window.precisShortcuts = { trigger, targets, ignored, labelOf };
})();
