/* se-2d.js — the se 2D reader ("/se/{slug}/2d") on the shared pan/zoom
   module (pcb-workbench-layout.md slice 3, the gesture table's 2D
   column). One pane, so it holds the data-shortcut keys outright
   (active) — the same buttons the pcb workbench's two panes share. */
(function () {
  'use strict';
  const obj = document.getElementById('se-2d-object');
  const pane = document.getElementById('se-2d-pane');
  if (!obj || !pane || !window.PrecisViewport) return;
  window.PrecisViewport.attach(obj, {host: pane, active: true});
})();
